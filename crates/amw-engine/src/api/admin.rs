use std::path::{Component, Path, PathBuf};

use axum::{
    extract::{rejection::QueryRejection, Query, State},
    Extension, Json,
};
use serde::Deserialize;
use serde_json::{json, Value};

use super::{
    auth::Principal,
    dto::{self, ControlVersion},
    error::{ApiError, EngineErrorCode, API_SCHEMA_VERSION},
    ApiJson, ApiState,
};
use crate::{
    runtime::{PrefixCommand as RuntimePrefixCommand, SessionAction},
    store::adapter::AdapterRegistration,
};

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub model_id: String,
}

pub async fn load_model(
    State(state): State<ApiState>,
    ApiJson(command): ApiJson<ModelCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    if command.model_id.trim().is_empty() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "model_id must not be empty",
        ));
    }
    let model = state.runtime.load_model(&command.model_id).await?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "loaded": model.id,
        "model": model,
    })))
}

pub async fn unload_model(
    State(state): State<ApiState>,
    ApiJson(command): ApiJson<ModelCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    state.runtime.unload_model(&command.model_id).await?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "unloaded": command.model_id,
    })))
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelStatusQuery {
    pub schema_version: u32,
    pub model_id: Option<String>,
}

pub async fn model_status(
    State(state): State<ApiState>,
    query: Result<Query<ModelStatusQuery>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(query) = query.map_err(|error| {
        ApiError::new(
            EngineErrorCode::UnsupportedParam,
            format!("invalid model status query: {error}"),
        )
    })?;
    dto::require_schema(query.schema_version)?;
    let status = state.runtime.status();
    let models = match query.model_id {
        Some(model_id) => {
            let selected: Vec<_> = status
                .models
                .into_iter()
                .filter(|model| model.id == model_id)
                .collect();
            if selected.is_empty() {
                return Err(ApiError::new(
                    EngineErrorCode::ModelNotLoaded,
                    format!("model is not loaded: {model_id}"),
                ));
            }
            selected
        }
        None => status.models,
    };
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "models": models,
        "draining": status.draining,
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoraCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub model_id: String,
    pub adapter_id: Option<String>,
}

pub async fn swap_lora(
    State(state): State<ApiState>,
    ApiJson(command): ApiJson<LoraCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    state
        .runtime
        .swap_registered_lora(&command.model_id, command.adapter_id.as_deref())
        .await?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "model_id": command.model_id,
        "adapter_id": command.adapter_id,
        "swapped": true,
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LoraRegistrationCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub id: String,
    pub root_id: String,
    pub relative_path: PathBuf,
    pub size_bytes: u64,
    pub sha256: String,
    pub base_model_sha256: String,
    pub scale: f32,
}

pub async fn register_lora(
    State(state): State<ApiState>,
    ApiJson(command): ApiJson<LoraRegistrationCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    validate_adapter_relative_path(&command.relative_path)?;
    let registered = state.runtime.register_lora(AdapterRegistration {
        id: command.id,
        root_id: command.root_id,
        relative_path: command.relative_path,
        size_bytes: command.size_bytes,
        sha256: command.sha256,
        base_model_sha256: command.base_model_sha256,
        scale: command.scale,
    })?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "adapter_id": registered.id,
        "sha256": registered.sha256,
    })))
}

fn validate_adapter_relative_path(path: &Path) -> Result<(), ApiError> {
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "adapter relative_path must be a normalized relative path",
        ));
    }
    Ok(())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ControlQuery {
    pub schema_version: u32,
}

pub async fn slots(
    State(state): State<ApiState>,
    query: Result<Query<ControlQuery>, QueryRejection>,
) -> Result<Json<Value>, ApiError> {
    let Query(query) = query.map_err(|error| {
        ApiError::new(
            EngineErrorCode::UnsupportedParam,
            format!("invalid slots query: {error}"),
        )
    })?;
    dto::require_schema(query.schema_version)?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "slots": state.runtime.slots(),
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct DrainCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub enabled: bool,
}

pub async fn drain(
    State(state): State<ApiState>,
    ApiJson(command): ApiJson<DrainCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    state.runtime.set_draining(command.enabled);
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "draining": command.enabled,
    })))
}

#[derive(Default, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReloadCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub log_level: Option<String>,
    pub keep_alive: Option<String>,
}

pub async fn reload_config(
    ApiJson(command): ApiJson<ReloadCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    if command
        .log_level
        .as_deref()
        .is_some_and(|level| !["trace", "debug", "info", "warn", "error"].contains(&level))
    {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "log_level must be trace, debug, info, warn, or error",
        ));
    }
    if command.log_level.is_some() || command.keep_alive.is_some() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "the requested config fields require a process restart",
        ));
    }
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "reloaded": [],
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PrefixCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub action: String,
    pub name: String,
    pub content: Option<String>,
    pub content_hash: String,
    pub model: Option<String>,
}

pub async fn prefix(
    State(state): State<ApiState>,
    ApiJson(command): ApiJson<PrefixCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    if command.name.is_empty() || command.content_hash.is_empty() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "prefix name and content_hash must not be empty",
        ));
    }
    let runtime_command = match command.action.as_str() {
        "register" => RuntimePrefixCommand::Register {
            name: command.name,
            content: command.content.ok_or_else(|| {
                ApiError::new(
                    EngineErrorCode::UnsupportedParam,
                    "prefix registration requires content",
                )
            })?,
            content_hash: command.content_hash,
        },
        "pin" => RuntimePrefixCommand::Pin {
            name: command.name,
            content_hash: command.content_hash,
        },
        "unpin" => RuntimePrefixCommand::Unpin {
            name: command.name,
            content_hash: command.content_hash,
        },
        _ => {
            return Err(ApiError::new(
                EngineErrorCode::UnsupportedParam,
                "prefix action must be register, pin, or unpin",
            ))
        }
    };
    let result = state
        .runtime
        .prefix(command.model.as_deref(), runtime_command)
        .await?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "name": result.name,
        "content_hash": result.content_hash,
        "token_count": result.token_count,
        "pinned": result.pinned,
    })))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SessionCommand {
    #[serde(flatten)]
    pub version: ControlVersion,
    pub action: String,
    pub session_id: String,
    pub model: Option<String>,
}

pub async fn sessions(
    State(state): State<ApiState>,
    Extension(principal): Extension<Principal>,
    ApiJson(command): ApiJson<SessionCommand>,
) -> Result<Json<Value>, ApiError> {
    command.version.validate()?;
    let action = match command.action.as_str() {
        "create" => SessionAction::Create,
        "resume" => SessionAction::Resume,
        "save" => SessionAction::Save,
        "delete" => SessionAction::Delete,
        _ => {
            return Err(ApiError::new(
                EngineErrorCode::UnsupportedParam,
                "session action must be create, resume, save, or delete",
            ))
        }
    };
    state
        .runtime
        .session_owned(
            command.model.as_deref(),
            action,
            command.session_id.clone(),
            principal.id.to_string(),
        )
        .await?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "session_id": command.session_id,
        "action": command.action,
    })))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn adapter_registration_refuses_absolute_and_traversal_paths() {
        assert!(validate_adapter_relative_path(Path::new("adapters/one.gguf")).is_ok());
        for invalid in [
            "",
            "/absolute/one.gguf",
            "../one.gguf",
            "adapters/../one.gguf",
        ] {
            let error = validate_adapter_relative_path(Path::new(invalid)).unwrap_err();
            assert_eq!(error.body.code, EngineErrorCode::UnsupportedParam);
        }
    }
}
