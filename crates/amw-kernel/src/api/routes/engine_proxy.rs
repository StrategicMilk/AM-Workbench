//! Closed allowlist proxy from the native kernel to the owned AM Engine.

use axum::{
    body::Body,
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use futures_util::TryStreamExt;
use serde::Serialize;

use crate::engine_runtime::{read_engine_connection, EngineLifecycleState, EngineRuntimeError};

const STREAM_TEE_DEFAULT_URL: &str = "http://127.0.0.1:8639";

#[derive(Serialize)]
struct UpstreamErrorEnvelope {
    schema_version: u32,
    code: &'static str,
    engine_state: EngineLifecycleState,
    message: String,
}

pub fn routes() -> Router {
    Router::new()
        .route("/api/v1/engine/health", get(engine_health))
        .route("/api/v1/engine/metrics", get(engine_metrics))
        .route("/api/v1/engine/version", get(engine_version))
        .route("/api/v1/engine/agent-stream", get(engine_agent_stream))
        .route(
            "/api/v1/engine/agent-stream/cancel",
            post(engine_agent_stream_cancel),
        )
}

pub async fn engine_health() -> Response {
    proxy_engine_get("/health", false).await
}

pub async fn engine_metrics() -> Response {
    proxy_engine_get("/metrics", true).await
}

pub async fn engine_version() -> Response {
    proxy_engine_get("/version", true).await
}

pub async fn engine_agent_stream() -> Response {
    proxy_stream_tee(reqwest::Method::GET, "/agent-stream").await
}

pub async fn engine_agent_stream_cancel() -> Response {
    proxy_stream_tee(reqwest::Method::POST, "/agent-stream/cancel").await
}

async fn proxy_engine_get(path: &str, authenticated: bool) -> Response {
    let connection = match read_engine_connection() {
        Ok(connection) => connection,
        Err(error) => return runtime_error_response(error),
    };
    let client = match reqwest::Client::builder()
        .connect_timeout(std::time::Duration::from_secs(2))
        .timeout(std::time::Duration::from_secs(5))
        .build()
    {
        Ok(client) => client,
        Err(error) => {
            return upstream_error(
                StatusCode::BAD_GATEWAY,
                format!("engine client initialization failed: {error}"),
            )
        }
    };
    let mut request = client.get(format!("{}{path}", connection.base_url));
    if authenticated {
        request = request.bearer_auth(connection.auth_token);
    }
    proxy_response(request.send().await, "engine").await
}

async fn proxy_stream_tee(method: reqwest::Method, path: &str) -> Response {
    let base_url = std::env::var("AMW_KERNEL_ENGINE_STREAM_TEE_URL")
        .unwrap_or_else(|_| STREAM_TEE_DEFAULT_URL.to_owned());
    let client = reqwest::Client::new();
    proxy_response(
        client
            .request(method, format!("{}{path}", base_url.trim_end_matches('/')))
            .send()
            .await,
        "engine stream tee",
    )
    .await
}

async fn proxy_response(
    result: Result<reqwest::Response, reqwest::Error>,
    owner: &str,
) -> Response {
    let upstream = match result {
        Ok(response) => response,
        Err(error) => {
            return upstream_error(
                StatusCode::BAD_GATEWAY,
                format!("{owner} is unreachable: {error}"),
            )
        }
    };
    let status =
        StatusCode::from_u16(upstream.status().as_u16()).unwrap_or(StatusCode::BAD_GATEWAY);
    let content_type = upstream.headers().get(header::CONTENT_TYPE).cloned();
    let stream = upstream.bytes_stream().map_err(std::io::Error::other);
    let mut response = Response::builder().status(status);
    if let Some(value) = content_type {
        response = response.header(header::CONTENT_TYPE, value);
    }
    response
        .header("x-amw-proxy", HeaderValue::from_static("engine"))
        .body(Body::from_stream(stream))
        .unwrap_or_else(|error| {
            upstream_error(
                StatusCode::BAD_GATEWAY,
                format!("invalid upstream response: {error}"),
            )
        })
}

fn runtime_error_response(error: EngineRuntimeError) -> Response {
    let status = match error.state {
        EngineLifecycleState::Missing | EngineLifecycleState::Stopped => {
            StatusCode::SERVICE_UNAVAILABLE
        }
        EngineLifecycleState::VersionMismatch | EngineLifecycleState::Degraded => {
            StatusCode::BAD_GATEWAY
        }
        EngineLifecycleState::Running => StatusCode::BAD_GATEWAY,
    };
    (
        status,
        Json(UpstreamErrorEnvelope {
            schema_version: 1,
            code: "ENGINE_UPSTREAM_UNAVAILABLE",
            engine_state: error.state,
            message: error.reason,
        }),
    )
        .into_response()
}

fn upstream_error(status: StatusCode, message: String) -> Response {
    (
        status,
        Json(UpstreamErrorEnvelope {
            schema_version: 1,
            code: "ENGINE_UPSTREAM_UNAVAILABLE",
            engine_state: EngineLifecycleState::Degraded,
            message,
        }),
    )
        .into_response()
}

#[cfg(test)]
mod tests {
    use super::*;
    use axum::http::{Method, Request};
    use tower::ServiceExt;

    #[tokio::test]
    async fn engine_absent_returns_typed_502_or_503() {
        let response = routes()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/engine/health")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert!(matches!(
            response.status(),
            StatusCode::BAD_GATEWAY | StatusCode::SERVICE_UNAVAILABLE
        ));
    }

    #[tokio::test]
    async fn non_allowlisted_path_and_method_return_404_or_405() {
        let missing = routes()
            .clone()
            .oneshot(
                Request::builder()
                    .uri("/api/v1/engine/admin/slots")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(missing.status(), StatusCode::NOT_FOUND);
        let wrong_method = routes()
            .oneshot(
                Request::builder()
                    .method(Method::POST)
                    .uri("/api/v1/engine/metrics")
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(wrong_method.status(), StatusCode::METHOD_NOT_ALLOWED);
    }
}
