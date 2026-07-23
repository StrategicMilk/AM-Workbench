use std::convert::Infallible;

use axum::{
    body::Body,
    extract::{rejection::QueryRejection, Query, State},
    http::{header, HeaderMap, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use futures_util::stream;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use super::{
    error::{ApiError, EngineErrorCode, API_SCHEMA_VERSION},
    ApiState,
};
#[cfg(all(feature = "contract-test-controls", debug_assertions))]
use crate::telemetry::events::{EngineEvent, EventEnvelope};
use crate::telemetry::{ring::EventCursor, SubscriptionItem, SubscriptionStart};

pub async fn health() -> Json<Value> {
    Json(json!({"schema_version":API_SCHEMA_VERSION,"status":"ok"}))
}

pub async fn readyz(State(state): State<ApiState>) -> impl IntoResponse {
    let status = state.runtime.status();
    readiness_response(
        status.draining,
        status.models.len(),
        state.runtime.receipt_ledger_is_ready(),
    )
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
enum ReadinessReasonCode {
    Draining,
    NoModelsLoaded,
    ReceiptLedgerUnready,
}

#[derive(Debug, Serialize)]
struct ReadinessBody {
    schema_version: u32,
    ready: bool,
    control_ready: bool,
    data_ready: bool,
    draining: bool,
    models_loaded: usize,
    reason_codes: Vec<ReadinessReasonCode>,
}

fn readiness_response(
    draining: bool,
    models_loaded: usize,
    receipt_ledger_ready: bool,
) -> (StatusCode, Json<ReadinessBody>) {
    let mut reason_codes = Vec::new();
    if draining {
        reason_codes.push(ReadinessReasonCode::Draining);
    }
    if models_loaded == 0 {
        reason_codes.push(ReadinessReasonCode::NoModelsLoaded);
    }
    if !receipt_ledger_ready {
        reason_codes.push(ReadinessReasonCode::ReceiptLedgerUnready);
    }
    let ready = reason_codes.is_empty();
    (
        if ready {
            StatusCode::OK
        } else {
            StatusCode::SERVICE_UNAVAILABLE
        },
        Json(ReadinessBody {
            schema_version: API_SCHEMA_VERSION,
            ready,
            control_ready: receipt_ledger_ready,
            data_ready: ready,
            draining,
            models_loaded,
            reason_codes,
        }),
    )
}

pub async fn metrics(State(state): State<ApiState>) -> Result<Json<Value>, ApiError> {
    let metrics = serde_json::to_value(state.runtime.metrics().snapshot()).map_err(|error| {
        ApiError::new(
            EngineErrorCode::Internal,
            format!("engine metrics serialization failed: {error}"),
        )
    })?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "metrics": metrics,
    })))
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EventsQuery {
    pub generation: Option<String>,
    pub after_cursor: Option<u64>,
}

pub async fn events(
    State(state): State<ApiState>,
    headers: HeaderMap,
    query: Result<Query<EventsQuery>, QueryRejection>,
) -> Result<Response, ApiError> {
    let Query(query) = query.map_err(|error| {
        ApiError::new(
            EngineErrorCode::UnsupportedParam,
            format!("invalid event resume query: {error}"),
        )
    })?;
    if query.generation.is_some() != query.after_cursor.is_some() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "event resume requires both generation and after_cursor",
        ));
    }
    let start = match (query.generation.as_deref(), query.after_cursor) {
        (Some(generation), Some(cursor)) if generation == state.event_generation.as_ref() => {
            SubscriptionStart::After(EventCursor::new(cursor))
        }
        _ => SubscriptionStart::Earliest,
    };
    inject_test_events(&state, &headers)?;
    let subscription = state
        .runtime
        .telemetry()
        .subscribe(start)
        .map_err(|error| {
            ApiError::new(
                EngineErrorCode::Internal,
                format!("engine event subscription failed: {error}"),
            )
        })?;
    let start_cursor = subscription.cursor().get();
    let initial_pause = test_event_pause(&headers);
    let events = stream::unfold(
        (subscription, initial_pause),
        |(mut subscription, pause)| async move {
            if let Some(pause) = pause {
                tokio::time::sleep(pause).await;
            }
            match subscription.recv().await {
                SubscriptionItem::Event(item) => match item.value.to_ndjson() {
                    Ok(line) => Some((Ok::<_, Infallible>(line), (subscription, None))),
                    Err(_) => None,
                },
                SubscriptionItem::Lagged {
                    missed,
                    resume_after,
                } => Some((
                    Ok(format!(
                        "{{\"transport_error\":{{\"code\":\"lagged\",\"missed\":{missed},\"resume_after\":{}}}}}\n",
                        resume_after.get()
                    )),
                    (subscription, None),
                )),
                SubscriptionItem::Closed => None,
            }
        },
    );
    let mut response = (
        StatusCode::OK,
        [
            (header::CONTENT_TYPE, "application/x-ndjson"),
            (header::CACHE_CONTROL, "no-store"),
        ],
        Body::from_stream(events),
    )
        .into_response();
    response.headers_mut().insert(
        "x-engine-event-generation",
        HeaderValue::from_str(&state.event_generation).map_err(|_| {
            ApiError::new(
                EngineErrorCode::Internal,
                "event generation could not be encoded as an HTTP header",
            )
        })?,
    );
    response.headers_mut().insert(
        "x-engine-event-start-cursor",
        HeaderValue::from_str(&start_cursor.to_string()).expect("u64 is always a valid header"),
    );
    Ok(response)
}

#[cfg(all(feature = "contract-test-controls", debug_assertions))]
fn inject_test_events(state: &ApiState, headers: &HeaderMap) -> Result<(), ApiError> {
    if std::env::var_os("AMW_ENGINE_ENABLE_TEST_CONTROLS").as_deref()
        != Some(std::ffi::OsStr::new("1"))
    {
        return Ok(());
    }
    let Some(count) = headers
        .get("x-amw-test-event-count")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<usize>().ok())
    else {
        return Ok(());
    };
    if count > 8192 {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "test event injection exceeds its bounded limit",
        ));
    }
    for index in 0..count {
        state
            .runtime
            .telemetry()
            .emit(EventEnvelope::new(
                1_000_000.0 + index as f64,
                EngineEvent::Gauges {
                    slots_busy: 0,
                    queue_depth: 0,
                    vram_used_mb: None,
                    kv_occupancy_pct: 0,
                },
            ))
            .map_err(|error| {
                ApiError::new(
                    EngineErrorCode::Internal,
                    format!("test event injection failed: {error}"),
                )
            })?;
    }
    Ok(())
}

#[cfg(not(all(feature = "contract-test-controls", debug_assertions)))]
fn inject_test_events(_: &ApiState, _: &HeaderMap) -> Result<(), ApiError> {
    Ok(())
}

#[cfg(all(feature = "contract-test-controls", debug_assertions))]
fn test_event_pause(headers: &HeaderMap) -> Option<std::time::Duration> {
    if std::env::var_os("AMW_ENGINE_ENABLE_TEST_CONTROLS").as_deref()
        != Some(std::ffi::OsStr::new("1"))
    {
        return None;
    }
    headers
        .get("x-amw-test-event-pause-ms")
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.parse::<u64>().ok())
        .map(|value| std::time::Duration::from_millis(value.min(5_000)))
}

#[cfg(not(all(feature = "contract-test-controls", debug_assertions)))]
fn test_event_pause(_: &HeaderMap) -> Option<std::time::Duration> {
    None
}

#[derive(Serialize)]
struct Version<'a> {
    schema_version: u32,
    engine_version: &'a str,
    libllama_rev: &'a str,
    cuda_version: &'a str,
    build_flags: Vec<&'a str>,
    target_os: &'a str,
    target_arch: &'a str,
    build_profile: &'a str,
    #[serde(skip_serializing_if = "Option::is_none")]
    receipt_trust: Option<crate::runtime::ReceiptTrustReport>,
}

pub async fn version(State(state): State<ApiState>) -> Json<impl Serialize> {
    let mut build_flags = Vec::new();
    if cfg!(feature = "cpu") {
        build_flags.push("cpu");
    }
    if cfg!(feature = "cuda") {
        build_flags.push("cuda");
    }
    if cfg!(feature = "nvml") {
        build_flags.push("nvml");
    }
    if cfg!(all(feature = "contract-test-controls", debug_assertions)) {
        build_flags.push("contract-test-controls");
    }
    Json(Version {
        schema_version: API_SCHEMA_VERSION,
        engine_version: env!("CARGO_PKG_VERSION"),
        libllama_rev: option_env!("AMW_LIBLLAMA_REV")
            .unwrap_or("86a9c79f866799eb0e7e89c03578ccfbcc5d808e"),
        cuda_version: option_env!("AMW_CUDA_VERSION").unwrap_or("none"),
        build_flags,
        target_os: std::env::consts::OS,
        target_arch: std::env::consts::ARCH,
        build_profile: if cfg!(debug_assertions) {
            "debug"
        } else {
            "release"
        },
        receipt_trust: state.runtime.receipt_trust_report(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readiness_fails_closed_with_stable_reason_codes() {
        let (status, Json(body)) = readiness_response(true, 0, true);

        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert!(!body.ready);
        assert!(body.control_ready);
        assert!(!body.data_ready);
        assert_eq!(
            body.reason_codes,
            vec![
                ReadinessReasonCode::Draining,
                ReadinessReasonCode::NoModelsLoaded
            ]
        );
    }

    #[test]
    fn readiness_is_ok_only_with_a_loaded_model_and_no_drain() {
        let (status, Json(body)) = readiness_response(false, 1, true);

        assert_eq!(status, StatusCode::OK);
        assert!(body.ready);
        assert!(body.data_ready);
        assert!(body.reason_codes.is_empty());
    }

    #[test]
    fn readiness_fails_control_plane_when_receipt_ledger_is_unreadable() {
        let (status, Json(body)) = readiness_response(false, 1, false);

        assert_eq!(status, StatusCode::SERVICE_UNAVAILABLE);
        assert!(!body.ready);
        assert!(!body.control_ready);
        assert_eq!(
            body.reason_codes,
            vec![ReadinessReasonCode::ReceiptLedgerUnready]
        );
    }
}
