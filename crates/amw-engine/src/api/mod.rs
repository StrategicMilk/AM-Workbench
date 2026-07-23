use std::{
    error::Error as _,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use axum::{
    body::{to_bytes, Body},
    extract::{rejection::JsonRejection, DefaultBodyLimit, FromRequest, Request, State},
    http::{header, header::HeaderName, HeaderValue, Method},
    middleware::{self, Next},
    response::Response,
    routing::{get, post},
    Router,
};
use http_body_util::LengthLimitError;
use tokio::sync::Semaphore;
use tokio::time::timeout;

use crate::{runtime::EngineRuntime, telemetry::TraceContext};

pub mod admin;
pub mod auth;
pub mod dto;
pub mod error;
pub mod health;
pub mod model_catalog;
pub mod openai;

static EVENT_GENERATION_COUNTER: AtomicU64 = AtomicU64::new(1);
pub const MAX_INFLIGHT_HTTP_REQUESTS: usize = 64;
pub const MAX_INFLIGHT_HTTP_BODY_BYTES: usize = 2 * dto::MAX_REQUEST_BODY_BYTES;
const REQUEST_BODY_READ_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Clone)]
struct AdmissionState {
    requests: Arc<Semaphore>,
    body_bytes: Arc<Semaphore>,
}

impl Default for AdmissionState {
    fn default() -> Self {
        Self {
            requests: Arc::new(Semaphore::new(MAX_INFLIGHT_HTTP_REQUESTS)),
            body_bytes: Arc::new(Semaphore::new(MAX_INFLIGHT_HTTP_BODY_BYTES)),
        }
    }
}

pub const OBSERVABILITY_ROUTES: &[&str] = &["/readyz", "/metrics", "/version", "/events"];
pub const INFERENCE_ROUTES: &[&str] = &[
    "/v1/completions",
    "/v1/chat/completions",
    "/v1/infill",
    "/v1/embeddings",
    "/v1/models",
    "/v1/tokenize",
    "/v1/count",
    "/v1/cancel",
];
pub const ADMIN_ROUTES: &[&str] = &[
    "/admin/models/load",
    "/admin/models/unload",
    "/admin/models/status",
    "/admin/models/catalog",
    "/admin/lora/register",
    "/admin/lora/swap",
    "/admin/slots",
    "/admin/drain",
    "/admin/config/reload",
    "/admin/prefix",
    "/admin/sessions",
];

pub struct ApiJson<T>(pub T);

impl<S, T> FromRequest<S> for ApiJson<T>
where
    S: Send + Sync,
    T: serde::de::DeserializeOwned,
{
    type Rejection = error::ApiError;

    async fn from_request(request: Request, state: &S) -> Result<Self, Self::Rejection> {
        let axum::Json(value) = axum::Json::<T>::from_request(request, state)
            .await
            .map_err(|rejection: JsonRejection| {
                let message = rejection.body_text();
                if message.to_ascii_lowercase().contains("length limit") {
                    error::ApiError::new(
                        error::EngineErrorCode::ContextOverflow,
                        "request body exceeds 16 MiB",
                    )
                } else {
                    error::ApiError::new(error::EngineErrorCode::UnsupportedParam, message)
                }
            })?;
        Ok(Self(value))
    }
}

#[derive(Clone)]
pub struct ApiState {
    pub runtime: EngineRuntime,
    pub auth_credentials: auth::CredentialSet,
    pub event_generation: Arc<str>,
}

impl ApiState {
    pub fn new(
        auth_token: impl AsRef<[u8]>,
        runtime: EngineRuntime,
    ) -> Result<Self, auth::AuthPolicyError> {
        let event_generation: Arc<str> = Arc::from(new_event_generation());
        Ok(Self {
            runtime,
            auth_credentials: auth::CredentialSet::new([auth::Credential::owner(auth_token)?])?,
            event_generation,
        })
    }

    pub fn with_credentials(credentials: auth::CredentialSet, runtime: EngineRuntime) -> Self {
        let event_generation: Arc<str> = Arc::from(new_event_generation());
        Self {
            runtime,
            auth_credentials: credentials,
            event_generation,
        }
    }
}

fn new_event_generation() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or_default();
    let sequence = EVENT_GENERATION_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!("{:x}-{nanos:032x}-{sequence:016x}", std::process::id())
}

pub fn router(state: ApiState) -> Router {
    let auth_state = auth::AuthState::new(
        state.auth_credentials.clone(),
        state.runtime.metrics().clone(),
    );
    let runtime = state.runtime.clone();
    let admission = AdmissionState::default();
    let observability = Router::new()
        .route("/readyz", get(health::readyz))
        .route("/metrics", get(health::metrics))
        .route("/version", get(health::version))
        .route("/events", get(health::events))
        .layer(middleware::from_fn_with_state(
            auth::Scope::Observability,
            auth::require_scope,
        ));
    let inference = Router::new()
        .route("/v1/completions", post(openai::completions))
        .route("/v1/chat/completions", post(openai::chat_completions))
        .route("/v1/infill", post(openai::infill))
        .route("/v1/embeddings", post(openai::embeddings))
        .route("/v1/models", get(openai::models))
        .route("/v1/tokenize", post(openai::tokenize))
        .route("/v1/count", post(openai::count))
        .route("/v1/cancel", post(openai::cancel))
        .layer(middleware::from_fn_with_state(
            auth::Scope::Inference,
            auth::require_scope,
        ));
    let admin = Router::new()
        .route("/admin/models/load", post(admin::load_model))
        .route("/admin/models/unload", post(admin::unload_model))
        .route("/admin/models/status", get(admin::model_status))
        .route("/admin/models/catalog", get(model_catalog::catalog))
        .route("/admin/lora/register", post(admin::register_lora))
        .route("/admin/lora/swap", post(admin::swap_lora))
        .route("/admin/slots", get(admin::slots))
        .route("/admin/drain", post(admin::drain))
        .route("/admin/config/reload", post(admin::reload_config))
        .route("/admin/prefix", post(admin::prefix))
        .route("/admin/sessions", post(admin::sessions))
        .layer(middleware::from_fn_with_state(
            auth::Scope::Admin,
            auth::require_scope,
        ));
    let unknown = Router::new()
        .fallback(unknown_route)
        .layer(middleware::from_fn_with_state(
            auth::Scope::Inference,
            auth::require_scope,
        ));
    let protected = Router::new()
        .merge(observability)
        .merge(inference)
        .merge(admin)
        .merge(unknown)
        .method_not_allowed_fallback(method_not_allowed)
        .layer(middleware::from_fn_with_state(runtime, request_context))
        .layer(middleware::from_fn_with_state(admission, admit_request))
        .layer(middleware::from_fn_with_state(
            auth_state,
            auth::require_bearer,
        ));
    Router::new()
        .route("/health", get(health::health))
        .merge(protected)
        .layer(DefaultBodyLimit::max(dto::MAX_REQUEST_BODY_BYTES))
        .with_state(state)
}

async fn admit_request(
    State(admission): State<AdmissionState>,
    request: Request,
    next: Next,
) -> Result<Response, error::ApiError> {
    let declared_length = request
        .headers()
        .get(header::CONTENT_LENGTH)
        .map(|value| {
            value
                .to_str()
                .ok()
                .and_then(|value| value.parse::<usize>().ok())
                .ok_or(())
        })
        .transpose()
        .map_err(|_| {
            error::ApiError::new(
                error::EngineErrorCode::UnsupportedParam,
                "Content-Length must be a non-negative decimal byte count",
            )
        })?;
    if declared_length.is_some_and(|length| length > dto::MAX_REQUEST_BODY_BYTES) {
        return Err(error::ApiError::new(
            error::EngineErrorCode::ContextOverflow,
            "request body exceeds 16 MiB",
        ));
    }
    let reserved_body_bytes = declared_length.unwrap_or_else(|| {
        if matches!(*request.method(), Method::GET | Method::HEAD) {
            0
        } else {
            dto::MAX_REQUEST_BODY_BYTES
        }
    });
    let _request_permit = admission
        .requests
        .clone()
        .try_acquire_owned()
        .map_err(|_| error::ApiError::admission_throttled())?;
    let _body_permit = if reserved_body_bytes == 0 {
        None
    } else {
        Some(
            admission
                .body_bytes
                .clone()
                .try_acquire_many_owned(reserved_body_bytes as u32)
                .map_err(|_| error::ApiError::admission_throttled())?,
        )
    };
    if reserved_body_bytes == 0 {
        return Ok(next.run(request).await);
    }
    let (parts, body) = request.into_parts();
    let bytes = timeout(
        request_body_read_timeout(),
        to_bytes(body, dto::MAX_REQUEST_BODY_BYTES),
    )
    .await
    .map_err(|_| error::ApiError::request_body_timeout())?
    .map_err(|body_error| {
        if body_error
            .source()
            .is_some_and(|source| source.is::<LengthLimitError>())
        {
            error::ApiError::new(
                error::EngineErrorCode::ContextOverflow,
                "request body exceeds 16 MiB",
            )
        } else {
            error::ApiError::new(
                error::EngineErrorCode::UnsupportedParam,
                "request body could not be read",
            )
        }
    })?;
    if bytes.len() > dto::MAX_REQUEST_BODY_BYTES {
        return Err(error::ApiError::new(
            error::EngineErrorCode::ContextOverflow,
            "request body exceeds 16 MiB",
        ));
    }
    let request = Request::from_parts(parts, Body::from(bytes));
    Ok(next.run(request).await)
}

#[cfg(all(feature = "contract-test-controls", debug_assertions))]
fn request_body_read_timeout() -> Duration {
    if std::env::var_os("AMW_ENGINE_ENABLE_TEST_CONTROLS").as_deref()
        == Some(std::ffi::OsStr::new("1"))
    {
        if let Some(milliseconds) = std::env::var("AMW_ENGINE_TEST_BODY_READ_TIMEOUT_MS")
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .filter(|value| (100..=60_000).contains(value))
        {
            return Duration::from_millis(milliseconds);
        }
    }
    REQUEST_BODY_READ_TIMEOUT
}

#[cfg(not(all(feature = "contract-test-controls", debug_assertions)))]
const fn request_body_read_timeout() -> Duration {
    REQUEST_BODY_READ_TIMEOUT
}

async fn request_context(
    State(runtime): State<EngineRuntime>,
    mut request: Request,
    next: Next,
) -> Response {
    let request_id =
        header_id(&request, "x-request-id").unwrap_or_else(|| runtime.next_request_id());
    let trace_id = traceparent_trace_id(&request)
        .or_else(|| header_id(&request, "x-trace-id"))
        .unwrap_or_else(|| request_id.clone());
    request
        .extensions_mut()
        .insert(TraceContext::new(request_id.clone(), trace_id.clone()));
    let mut response = next.run(request).await;
    if let Ok(value) = HeaderValue::from_str(&request_id) {
        response
            .headers_mut()
            .insert(HeaderName::from_static("x-request-id"), value);
    }
    if let Ok(value) = HeaderValue::from_str(&trace_id) {
        response
            .headers_mut()
            .insert(HeaderName::from_static("x-trace-id"), value);
    }
    response
}

fn traceparent_trace_id(request: &Request) -> Option<String> {
    let value = request.headers().get("traceparent")?.to_str().ok()?;
    let mut fields = value.split('-');
    let version = fields.next()?;
    let trace_id = fields.next()?;
    let parent_id = fields.next()?;
    let flags = fields.next()?;
    if fields.next().is_some()
        || version.len() != 2
        || version.eq_ignore_ascii_case("ff")
        || trace_id.len() != 32
        || parent_id.len() != 16
        || flags.len() != 2
        || ![version, trace_id, parent_id, flags]
            .iter()
            .all(|field| field.bytes().all(|byte| byte.is_ascii_hexdigit()))
        || trace_id.bytes().all(|byte| byte == b'0')
        || parent_id.bytes().all(|byte| byte == b'0')
    {
        return None;
    }
    Some(trace_id.to_ascii_lowercase())
}

fn header_id(request: &Request, name: &'static str) -> Option<String> {
    request
        .headers()
        .get(name)
        .and_then(|value| value.to_str().ok())
        .filter(|value| {
            !value.is_empty()
                && value.len() <= 128
                && value
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        })
        .map(str::to_owned)
}

async fn unknown_route() -> error::ApiError {
    error::ApiError::new(
        error::EngineErrorCode::UnsupportedParam,
        "requested engine route does not exist",
    )
}

async fn method_not_allowed() -> error::ApiError {
    error::ApiError::method_not_allowed("requested engine route does not accept this HTTP method")
}

#[cfg(test)]
mod tests {
    use std::collections::BTreeSet;

    use super::*;

    #[test]
    fn every_registered_protected_route_has_exactly_one_explicit_scope() {
        let routes = OBSERVABILITY_ROUTES
            .iter()
            .chain(INFERENCE_ROUTES)
            .chain(ADMIN_ROUTES)
            .copied()
            .collect::<Vec<_>>();
        let unique = routes.iter().copied().collect::<BTreeSet<_>>();
        assert_eq!(routes.len(), unique.len());
        assert_eq!(routes.len(), 23);
        assert!(OBSERVABILITY_ROUTES
            .iter()
            .all(|route| !route.starts_with("/admin/")));
        assert!(ADMIN_ROUTES
            .iter()
            .all(|route| route.starts_with("/admin/")));
        assert!(INFERENCE_ROUTES
            .iter()
            .all(|route| route.starts_with("/v1/")));
    }

    #[test]
    fn event_transport_generation_is_unique_per_api_state() {
        assert_ne!(new_event_generation(), new_event_generation());
    }

    #[test]
    fn w3c_traceparent_accepts_canonical_context_and_rejects_invalid_ids() {
        let request = Request::builder()
            .header(
                "traceparent",
                "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            )
            .body(axum::body::Body::empty())
            .unwrap();
        assert_eq!(
            traceparent_trace_id(&request).as_deref(),
            Some("4bf92f3577b34da6a3ce929d0e0e4736")
        );

        for traceparent in [
            "00-00000000000000000000000000000000-00f067aa0ba902b7-01",
            "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01",
            "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
        ] {
            let request = Request::builder()
                .header("traceparent", traceparent)
                .body(axum::body::Body::empty())
                .unwrap();
            assert!(traceparent_trace_id(&request).is_none());
        }
    }
}
