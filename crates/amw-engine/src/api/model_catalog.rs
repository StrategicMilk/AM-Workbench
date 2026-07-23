//! Bounded, path-redacted model discovery for authenticated operators.

use std::path::Path;

use axum::{
    extract::{rejection::QueryRejection, Query, State},
    Json,
};
use serde::{Deserialize, Serialize};

use super::{
    dto,
    error::{ApiError, EngineErrorCode, API_SCHEMA_VERSION},
    ApiState,
};
use crate::store::registry::{CatalogDiagnostic, CatalogModel};

const DEFAULT_PAGE_SIZE: usize = 100;
const MAX_PAGE_SIZE: usize = 256;
const MAX_DISPLAY_FIELD_BYTES: usize = 256;

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CatalogQuery {
    pub schema_version: u32,
    pub offset: Option<usize>,
    pub limit: Option<usize>,
    pub rejected_offset: Option<usize>,
    pub rejected_limit: Option<usize>,
}

#[derive(Debug, Eq, PartialEq, Serialize)]
struct DiscoveredModel {
    id: String,
    architecture: Option<String>,
    quantization: Option<String>,
    context_length: Option<u64>,
    embedding_length: Option<u64>,
    supports_embeddings: bool,
    supports_fim: bool,
}

#[derive(Debug, Eq, PartialEq, Serialize)]
struct RejectedModel {
    candidate_name: String,
    reason_code: &'static str,
    reason: &'static str,
}

#[derive(Debug, Eq, PartialEq, Serialize)]
pub struct CatalogResponse {
    schema_version: u32,
    models: Vec<DiscoveredModel>,
    rejected: Vec<RejectedModel>,
    model_count: usize,
    rejected_count: usize,
    next_model_offset: Option<usize>,
    next_rejected_offset: Option<usize>,
}

/// Lists admitted local models and isolated bootstrap failures without filesystem paths.
pub async fn catalog(
    State(state): State<ApiState>,
    query: Result<Query<CatalogQuery>, QueryRejection>,
) -> Result<Json<CatalogResponse>, ApiError> {
    let Query(query) = query.map_err(|error| {
        ApiError::new(
            EngineErrorCode::UnsupportedParam,
            format!("invalid model catalog query: {error}"),
        )
    })?;
    dto::require_schema(query.schema_version)?;
    let (models, diagnostics) = state.runtime.model_catalog_snapshot();
    Ok(Json(build_response(query, models, diagnostics)?))
}

fn build_response(
    query: CatalogQuery,
    models: Vec<CatalogModel>,
    diagnostics: Vec<CatalogDiagnostic>,
) -> Result<CatalogResponse, ApiError> {
    let model_limit = validate_limit("limit", query.limit)?;
    let rejected_limit = validate_limit("rejected_limit", query.rejected_limit)?;
    let model_offset = query.offset.unwrap_or_default();
    let rejected_offset = query.rejected_offset.unwrap_or_default();
    let model_count = models.len();
    let rejected_count = diagnostics.len();
    let models = models
        .into_iter()
        .skip(model_offset)
        .take(model_limit)
        .map(discovered_model)
        .collect::<Vec<_>>();
    let rejected = diagnostics
        .into_iter()
        .skip(rejected_offset)
        .take(rejected_limit)
        .map(rejected_model)
        .collect::<Vec<_>>();
    Ok(CatalogResponse {
        schema_version: API_SCHEMA_VERSION,
        next_model_offset: next_offset(model_offset, models.len(), model_count),
        next_rejected_offset: next_offset(rejected_offset, rejected.len(), rejected_count),
        models,
        rejected,
        model_count,
        rejected_count,
    })
}

fn validate_limit(name: &'static str, value: Option<usize>) -> Result<usize, ApiError> {
    let value = value.unwrap_or(DEFAULT_PAGE_SIZE);
    if !(1..=MAX_PAGE_SIZE).contains(&value) {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            format!("{name} must be between 1 and {MAX_PAGE_SIZE}"),
        ));
    }
    Ok(value)
}

fn next_offset(offset: usize, returned: usize, total: usize) -> Option<usize> {
    let consumed = offset.saturating_add(returned);
    (returned > 0 && consumed < total).then_some(consumed)
}

fn discovered_model(model: CatalogModel) -> DiscoveredModel {
    DiscoveredModel {
        id: model.id,
        architecture: bounded_display_field(model.architecture),
        quantization: bounded_display_field(model.quantization),
        context_length: model.context_length,
        embedding_length: model.embedding_length,
        supports_embeddings: model.supports_embeddings,
        supports_fim: model.supports_fim,
    }
}

fn bounded_display_field(value: Option<String>) -> Option<String> {
    value.filter(|value| {
        value.len() <= MAX_DISPLAY_FIELD_BYTES
            && value.bytes().all(|byte| {
                byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'+')
            })
    })
}

fn rejected_model(diagnostic: CatalogDiagnostic) -> RejectedModel {
    RejectedModel {
        candidate_name: safe_candidate_name(&diagnostic.path),
        reason_code: diagnostic.kind,
        reason: rejection_reason(diagnostic.kind),
    }
}

fn safe_candidate_name(path: &Path) -> String {
    let rendered = path.to_string_lossy();
    let candidate = rendered
        .rsplit(['/', '\\'])
        .find(|component| !component.is_empty())
        .unwrap_or("unavailable");
    if candidate.len() > MAX_DISPLAY_FIELD_BYTES || candidate.chars().any(char::is_control) {
        "unavailable".to_owned()
    } else {
        candidate.to_owned()
    }
}

fn rejection_reason(kind: &str) -> &'static str {
    match kind {
        "integrity" => "GGUF metadata or tensor bounds failed validation",
        "sidecar_metadata" => "model sidecar metadata is invalid",
        "sidecar_path" => "model sidecar points to a different file",
        "duplicate_name" => "model ID or alias duplicates another catalog entry",
        "invalid_id" => "model ID or alias is invalid",
        "path_escape" => "model resolves outside the configured model directories",
        "missing_model" => "model file is unavailable",
        "io" => "model or sidecar could not be read",
        _ => "model registry rejected the entry",
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        path::{Path, PathBuf},
    };

    use super::*;
    use crate::{
        config::EngineConfig,
        runtime::EngineRuntime,
        telemetry::{metrics::MetricsHub, TelemetryHub},
    };

    fn fixture(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join(name)
    }

    fn model(id: &str) -> CatalogModel {
        CatalogModel {
            id: id.to_owned(),
            aliases: Vec::new(),
            architecture: Some("llama".to_owned()),
            quantization: Some("Q4_K".to_owned()),
            context_length: Some(4096),
            embedding_length: Some(128),
            supports_embeddings: true,
            supports_fim: false,
        }
    }

    #[test]
    fn catalog_pages_models_and_rejections_independently() {
        let response = build_response(
            CatalogQuery {
                schema_version: API_SCHEMA_VERSION,
                offset: Some(1),
                limit: Some(1),
                rejected_offset: Some(0),
                rejected_limit: Some(1),
            },
            vec![model("alpha"), model("beta"), model("gamma")],
            vec![
                CatalogDiagnostic {
                    path: PathBuf::from("/srv/private/models/bad.gguf"),
                    kind: "integrity",
                    detail: "private diagnostic detail".to_owned(),
                },
                CatalogDiagnostic {
                    path: PathBuf::from("/srv/private/models/other.gguf"),
                    kind: "io",
                    detail: "private diagnostic detail".to_owned(),
                },
            ],
        )
        .unwrap();
        assert_eq!(response.models[0].id, "beta");
        assert_eq!(response.rejected[0].candidate_name, "bad.gguf");
        assert_eq!(response.next_model_offset, Some(2));
        assert_eq!(response.next_rejected_offset, Some(1));
        assert_eq!(response.model_count, 3);
        assert_eq!(response.rejected_count, 2);
    }

    #[test]
    fn catalog_rejects_unbounded_pages_and_never_serializes_raw_paths_or_details() {
        assert!(validate_limit("limit", Some(0)).is_err());
        assert!(validate_limit("limit", Some(MAX_PAGE_SIZE + 1)).is_err());
        let mut model_with_private_metadata = model("safe-id");
        model_with_private_metadata.architecture = Some(r"C:\Users\alice\private".to_owned());
        let response = build_response(
            CatalogQuery {
                schema_version: API_SCHEMA_VERSION,
                rejected_limit: Some(1),
                ..CatalogQuery::default()
            },
            vec![model_with_private_metadata],
            vec![CatalogDiagnostic {
                path: PathBuf::from(r"C:\Users\alice\private\broken.gguf"),
                kind: "sidecar_metadata",
                detail: r"registry metadata is invalid for C:\Users\alice\private\broken.gguf"
                    .to_owned(),
            }],
        )
        .unwrap();
        let payload = serde_json::to_string(&response).unwrap();
        assert!(payload.contains("broken.gguf"));
        assert!(payload.contains("sidecar_metadata"));
        assert_eq!(response.models[0].architecture, None);
        assert!(!payload.contains("alice"));
        assert!(!payload.contains("registry metadata is invalid"));
    }

    #[tokio::test]
    async fn catalog_handler_reads_the_runtime_bootstrap_snapshot() {
        let root = tempfile::tempdir().unwrap();
        fs::copy(fixture("tiny-cpu.gguf"), root.path().join("admitted.gguf")).unwrap();
        fs::write(root.path().join("rejected.gguf"), b"not a GGUF model").unwrap();
        let mut config = EngineConfig::default();
        config.models.dirs = vec![root.path().to_owned()];
        config.kv.session_dir = root.path().join("sessions");
        config.budgets.ram_gb = 1.0;
        let runtime =
            EngineRuntime::new(config, TelemetryHub::default(), MetricsHub::default()).unwrap();
        let Json(response) = catalog(
            State(ApiState::new("catalog-test-token", runtime).unwrap()),
            Ok(Query(CatalogQuery {
                schema_version: API_SCHEMA_VERSION,
                ..CatalogQuery::default()
            })),
        )
        .await
        .unwrap();
        assert_eq!(response.model_count, 1);
        assert_eq!(response.models[0].id, "admitted");
        assert_eq!(response.rejected_count, 1);
        assert_eq!(response.rejected[0].candidate_name, "rejected.gguf");
        assert_eq!(response.rejected[0].reason_code, "integrity");
    }
}
