use std::collections::BTreeMap;

use axum::{
    http::{header, HeaderValue, StatusCode},
    response::{IntoResponse, Response},
    Json,
};
use serde::{Deserialize, Serialize};

use crate::{runtime::RuntimeError, sched::SchedError};

pub const API_SCHEMA_VERSION: u32 = 1;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum EngineErrorCode {
    ModelNotLoaded,
    ModelCorrupt,
    BackendUnavailable,
    ContextOverflow,
    QueueFull,
    Draining,
    Oom,
    AllocationFailed,
    QuotaExhausted,
    GrammarInvalid,
    TemplateUntrusted,
    VersionMismatch,
    Unauthorized,
    SessionUnknown,
    UnsupportedParam,
    EvalTimeout,
    Cancelled,
    EvalReceiptUnavailable,
    EvalAttemptConflict,
    EvalReceiptError,
    Internal,
}

impl EngineErrorCode {
    pub const ALL: [Self; 21] = [
        Self::ModelNotLoaded,
        Self::ModelCorrupt,
        Self::BackendUnavailable,
        Self::ContextOverflow,
        Self::QueueFull,
        Self::Draining,
        Self::Oom,
        Self::AllocationFailed,
        Self::QuotaExhausted,
        Self::GrammarInvalid,
        Self::TemplateUntrusted,
        Self::VersionMismatch,
        Self::Unauthorized,
        Self::SessionUnknown,
        Self::UnsupportedParam,
        Self::EvalTimeout,
        Self::Cancelled,
        Self::EvalReceiptUnavailable,
        Self::EvalAttemptConflict,
        Self::EvalReceiptError,
        Self::Internal,
    ];

    pub const fn status(self) -> StatusCode {
        match self {
            Self::Unauthorized => StatusCode::UNAUTHORIZED,
            Self::ModelNotLoaded | Self::SessionUnknown => StatusCode::NOT_FOUND,
            Self::ContextOverflow => StatusCode::PAYLOAD_TOO_LARGE,
            Self::QueueFull | Self::QuotaExhausted => StatusCode::TOO_MANY_REQUESTS,
            Self::Draining
            | Self::Oom
            | Self::AllocationFailed
            | Self::BackendUnavailable
            | Self::EvalReceiptUnavailable => StatusCode::SERVICE_UNAVAILABLE,
            Self::EvalTimeout => StatusCode::GATEWAY_TIMEOUT,
            Self::Cancelled | Self::EvalAttemptConflict => StatusCode::CONFLICT,
            Self::ModelCorrupt
            | Self::GrammarInvalid
            | Self::TemplateUntrusted
            | Self::VersionMismatch
            | Self::UnsupportedParam => StatusCode::UNPROCESSABLE_ENTITY,
            Self::EvalReceiptError | Self::Internal => StatusCode::INTERNAL_SERVER_ERROR,
        }
    }

    pub const fn retryable(self) -> bool {
        matches!(
            self,
            Self::BackendUnavailable
                | Self::QueueFull
                | Self::Draining
                | Self::Oom
                | Self::AllocationFailed
                | Self::QuotaExhausted
                | Self::EvalReceiptUnavailable
                | Self::Internal
        )
    }
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ErrorBody {
    pub code: EngineErrorCode,
    pub message: String,
    pub retryable: bool,
    #[serde(default, skip_serializing_if = "BTreeMap::is_empty")]
    pub details: BTreeMap<String, String>,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub struct ErrorEnvelope {
    pub schema_version: u32,
    pub error: ErrorBody,
}

#[derive(Clone, Debug)]
pub struct ApiError {
    pub body: ErrorBody,
    status_override: Option<StatusCode>,
    retry_after_seconds: Option<u64>,
}

impl ApiError {
    pub fn new(code: EngineErrorCode, message: impl Into<String>) -> Self {
        Self {
            body: ErrorBody {
                code,
                message: message.into(),
                retryable: code.retryable(),
                details: BTreeMap::new(),
            },
            status_override: None,
            retry_after_seconds: None,
        }
    }

    pub fn forbidden(message: impl Into<String>) -> Self {
        let mut error = Self::new(EngineErrorCode::Unauthorized, message);
        error.status_override = Some(StatusCode::FORBIDDEN);
        error
    }

    /// Constructs the stable envelope used when a known path rejects its HTTP method.
    pub fn method_not_allowed(message: impl Into<String>) -> Self {
        let mut error = Self::new(EngineErrorCode::UnsupportedParam, message);
        error.status_override = Some(StatusCode::METHOD_NOT_ALLOWED);
        error
    }

    /// Constructs a retryable response for the bounded bearer failure limiter.
    pub fn authentication_throttled(retry_after_seconds: u64) -> Self {
        let mut error = Self::new(
            EngineErrorCode::Unauthorized,
            "too many invalid bearer-token attempts",
        );
        error.body.retryable = true;
        error.status_override = Some(StatusCode::TOO_MANY_REQUESTS);
        error.retry_after_seconds = Some(retry_after_seconds.max(1));
        error
    }

    /// Constructs a retryable response for bounded HTTP request/body admission.
    pub fn admission_throttled() -> Self {
        let mut error = Self::new(
            EngineErrorCode::QuotaExhausted,
            "engine HTTP admission capacity is exhausted",
        );
        error.retry_after_seconds = Some(1);
        error
    }

    /// Rejects an authenticated request whose body was not delivered within the bounded window.
    pub fn request_body_timeout() -> Self {
        let mut error = Self::new(
            EngineErrorCode::UnsupportedParam,
            "request body was not received before the read deadline",
        );
        error.body.retryable = true;
        error.status_override = Some(StatusCode::REQUEST_TIMEOUT);
        error
    }

    pub fn detail(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.body.details.insert(key.into(), value.into());
        self
    }
}

impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        let code = self.body.code;
        let status = self
            .status_override
            .unwrap_or_else(|| self.body.code.status());
        let mut response = (
            status,
            Json(ErrorEnvelope {
                schema_version: API_SCHEMA_VERSION,
                error: self.body,
            }),
        )
            .into_response();
        if let Some(retry_after_seconds) = self.retry_after_seconds {
            if let Ok(value) = HeaderValue::from_str(&retry_after_seconds.to_string()) {
                response.headers_mut().insert(header::RETRY_AFTER, value);
            }
        } else if code == EngineErrorCode::QueueFull {
            response
                .headers_mut()
                .insert(header::RETRY_AFTER, HeaderValue::from_static("1"));
        }
        response
    }
}

impl From<SchedError> for ApiError {
    fn from(error: SchedError) -> Self {
        RuntimeError::from(error).into()
    }
}

impl From<RuntimeError> for ApiError {
    fn from(error: RuntimeError) -> Self {
        let readmission_attempts = match &error {
            RuntimeError::BackgroundReadmissionLimit { attempts } => Some(*attempts),
            _ => None,
        };
        tracing::warn!(
            runtime_error_code = runtime_error_log_code(&error),
            "runtime error mapped to sanitized public envelope"
        );
        let mut api_error = match &error {
            RuntimeError::ModelNotLoaded(_) => Self::new(
                EngineErrorCode::ModelNotLoaded,
                "requested model is not loaded",
            ),
            RuntimeError::ModelCorrupt { .. } => Self::new(
                EngineErrorCode::ModelCorrupt,
                "model is corrupt or unreadable",
            ),
            RuntimeError::NativeUnavailable => Self::new(
                EngineErrorCode::BackendUnavailable,
                "native inference backend is unavailable",
            ),
            RuntimeError::ContextOverflow { requested, limit } => Self::new(
                EngineErrorCode::ContextOverflow,
                format!("request context exceeds the {limit}-token limit ({requested} requested)"),
            ),
            RuntimeError::QueueFull => {
                Self::new(EngineErrorCode::QueueFull, "engine request queue is full")
            }
            RuntimeError::Draining => Self::new(EngineErrorCode::Draining, "engine is draining"),
            RuntimeError::Oom(_) => Self::new(
                EngineErrorCode::AllocationFailed,
                "native allocation failed",
            ),
            RuntimeError::BackgroundReadmissionLimit { .. } => Self::new(
                EngineErrorCode::Oom,
                "background request memory readmission was exhausted",
            ),
            RuntimeError::QuotaExhausted(_) => Self::new(
                EngineErrorCode::QuotaExhausted,
                "engine resource quota exhausted",
            ),
            RuntimeError::GrammarInvalid(_) => {
                Self::new(EngineErrorCode::GrammarInvalid, "grammar is invalid")
            }
            RuntimeError::TemplateUntrusted => Self::new(
                EngineErrorCode::TemplateUntrusted,
                "model chat template is not trusted by local policy",
            ),
            RuntimeError::SessionUnknown(_) => Self::new(
                EngineErrorCode::SessionUnknown,
                "requested session is unknown",
            ),
            RuntimeError::UnsupportedParam(message) => Self::new(
                EngineErrorCode::UnsupportedParam,
                format!("unsupported parameter: {message}"),
            ),
            RuntimeError::AdapterInvalid => Self::new(
                EngineErrorCode::UnsupportedParam,
                "adapter registration or resolution failed validation",
            ),
            RuntimeError::EvalTimeout => {
                Self::new(EngineErrorCode::EvalTimeout, "engine evaluation timed out")
            }
            RuntimeError::Cancelled => {
                Self::new(EngineErrorCode::Cancelled, "request was cancelled")
            }
            RuntimeError::Unauthorized => {
                Self::forbidden("the requested resource is not accessible to this principal")
            }
            RuntimeError::EvalReceiptUnavailable => Self::new(
                EngineErrorCode::EvalReceiptUnavailable,
                "engine evaluation receipt authority is unavailable",
            ),
            RuntimeError::EvalReceiptAuthority(_) => Self::new(
                EngineErrorCode::EvalReceiptUnavailable,
                "engine evaluation receipt authority is unavailable",
            ),
            RuntimeError::EvalAttemptConflict => Self::new(
                EngineErrorCode::EvalAttemptConflict,
                "evaluation request or attempt was already consumed",
            ),
            RuntimeError::EvalReceiptCommit(_) => Self::new(
                EngineErrorCode::EvalReceiptError,
                "engine evaluation receipt could not be committed",
            ),
            RuntimeError::Internal(_) => Self::new(
                EngineErrorCode::Internal,
                "engine request failed internally",
            ),
        };
        if let Some(attempts) = readmission_attempts {
            api_error = api_error
                .detail("reason", "background_readmission_limit")
                .detail("attempts", attempts.to_string());
        }
        api_error
    }
}

fn runtime_error_log_code(error: &RuntimeError) -> &'static str {
    match error {
        RuntimeError::ModelNotLoaded(_) => "model_not_loaded",
        RuntimeError::ModelCorrupt { .. } => "model_corrupt",
        RuntimeError::NativeUnavailable => "backend_unavailable",
        RuntimeError::ContextOverflow { .. } => "context_overflow",
        RuntimeError::QueueFull => "queue_full",
        RuntimeError::QuotaExhausted(_) => "quota_exhausted",
        RuntimeError::Draining => "draining",
        RuntimeError::Oom(_) => "allocation_failed",
        RuntimeError::BackgroundReadmissionLimit { .. } => "background_readmission_limit",
        RuntimeError::GrammarInvalid(_) => "grammar_invalid",
        RuntimeError::TemplateUntrusted => "template_untrusted",
        RuntimeError::SessionUnknown(_) => "session_unknown",
        RuntimeError::UnsupportedParam(_) => "unsupported_param",
        RuntimeError::AdapterInvalid => "adapter_invalid",
        RuntimeError::EvalTimeout => "eval_timeout",
        RuntimeError::Cancelled => "cancelled",
        RuntimeError::Unauthorized => "unauthorized",
        RuntimeError::EvalReceiptUnavailable => "eval_receipt_unavailable",
        RuntimeError::EvalReceiptAuthority(_) => "eval_receipt_authority",
        RuntimeError::EvalAttemptConflict => "eval_attempt_conflict",
        RuntimeError::EvalReceiptCommit(_) => "eval_receipt_error",
        RuntimeError::Internal(_) => "internal",
    }
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    #[test]
    fn background_readmission_limit_preserves_pinned_oom_code_with_details() {
        let error = ApiError::from(RuntimeError::BackgroundReadmissionLimit { attempts: 3 });

        assert_eq!(error.body.code, EngineErrorCode::Oom);
        assert_eq!(error.body.code.status(), StatusCode::SERVICE_UNAVAILABLE);
        assert_eq!(
            error.body.details.get("reason").map(String::as_str),
            Some("background_readmission_limit")
        );
        assert_eq!(
            error.body.details.get("attempts").map(String::as_str),
            Some("3")
        );
    }

    #[test]
    fn untrusted_template_reaches_the_distinct_public_error_code() {
        let error = ApiError::from(RuntimeError::TemplateUntrusted);
        assert_eq!(error.body.code, EngineErrorCode::TemplateUntrusted);
        assert_eq!(error.body.code.status(), StatusCode::UNPROCESSABLE_ENTITY);
    }

    #[test]
    fn quota_exhaustion_is_redacted_and_uses_http_429() {
        let secret = "C:\\private\\tenant-secret".repeat(16);
        let error = ApiError::from(RuntimeError::QuotaExhausted(secret.clone()));

        assert_eq!(error.body.code, EngineErrorCode::QuotaExhausted);
        assert_eq!(error.body.code.status(), StatusCode::TOO_MANY_REQUESTS);
        assert!(error.body.retryable);
        assert_eq!(error.body.message, "engine resource quota exhausted");
        assert!(error.body.details.is_empty());
        assert!(!serde_json::to_string(&error.body)
            .unwrap()
            .contains(&secret));
        assert_eq!(
            error.into_response().status(),
            StatusCode::TOO_MANY_REQUESTS
        );
    }

    #[test]
    fn method_rejection_keeps_the_error_envelope_and_http_405() {
        let error = ApiError::method_not_allowed("route does not accept this method");

        assert_eq!(error.body.code, EngineErrorCode::UnsupportedParam);
        assert!(!error.body.retryable);
        assert_eq!(
            error.into_response().status(),
            StatusCode::METHOD_NOT_ALLOWED
        );
    }

    #[test]
    fn public_runtime_failures_keep_distinct_stable_codes() {
        assert_eq!(
            ApiError::from(RuntimeError::NativeUnavailable).body.code,
            EngineErrorCode::BackendUnavailable
        );
        assert_eq!(
            ApiError::from(RuntimeError::Oom("allocator refused request".to_owned()))
                .body
                .code,
            EngineErrorCode::AllocationFailed
        );
        assert_eq!(
            ApiError::from(RuntimeError::Cancelled).body.code,
            EngineErrorCode::Cancelled
        );
    }

    #[test]
    fn sensitive_runtime_variants_never_reach_public_message_or_details() {
        let secret = r"C:\private\tenant-secret\native.gguf";
        let errors = [
            RuntimeError::ModelNotLoaded(secret.to_owned()),
            RuntimeError::ModelCorrupt {
                path: PathBuf::from(secret),
                reason: secret.to_owned(),
            },
            RuntimeError::Oom(secret.to_owned()),
            RuntimeError::QuotaExhausted(secret.to_owned()),
            RuntimeError::SessionUnknown(secret.to_owned()),
            RuntimeError::Internal(secret.to_owned()),
        ];

        for error in errors {
            let body = ApiError::from(error).body;
            let serialized = serde_json::to_string(&body).unwrap();
            assert!(!serialized.contains("tenant-secret"), "{serialized}");
            assert!(!serialized.contains("native.gguf"), "{serialized}");
            assert!(!serialized.contains("C:\\\\private"), "{serialized}");
        }
    }

    #[test]
    fn public_model_errors_do_not_expose_owned_paths_or_native_details() {
        let error = ApiError::from(RuntimeError::ModelCorrupt {
            path: PathBuf::from(r"C:\private\model-cas\secret.gguf"),
            reason: "native loader mentioned C:\\private\\model-cas".to_owned(),
        });

        assert_eq!(error.body.code, EngineErrorCode::ModelCorrupt);
        assert_eq!(error.body.message, "model is corrupt or unreadable");
        assert!(!error.body.message.contains("private"));
    }

    #[test]
    fn public_missing_model_error_does_not_expose_owned_path() {
        let error = ApiError::from(RuntimeError::ModelNotLoaded(
            r"C:\private\model-cas\tenant-secret.gguf".to_owned(),
        ));

        assert_eq!(error.body.code, EngineErrorCode::ModelNotLoaded);
        assert_eq!(error.body.message, "requested model is not loaded");
        assert!(!error.body.message.contains("private"));
        assert!(!error.body.message.contains("tenant-secret"));
    }

    #[test]
    fn queue_saturation_is_429_with_bounded_retry_advice() {
        let response = ApiError::from(SchedError::QueueFull).into_response();

        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(response.headers().get(header::RETRY_AFTER).unwrap(), "1");
    }

    #[test]
    fn authentication_throttle_is_retryable_429_with_exact_retry_advice() {
        let response = ApiError::authentication_throttled(7).into_response();

        assert_eq!(response.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(response.headers().get(header::RETRY_AFTER).unwrap(), "7");
    }
}
