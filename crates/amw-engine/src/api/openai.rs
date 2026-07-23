use std::{collections::BTreeMap, convert::Infallible, time::Instant};

use axum::{
    extract::{Extension, State},
    http::HeaderMap,
    response::{sse::Event, IntoResponse, Response, Sse},
    Json,
};
use futures_util::stream;
use serde::Deserialize;
use serde_json::{json, Value};

use super::{
    auth::{Principal, Scope},
    dto::{
        self, BatchTextRequest, CompletionRequest, CompletionResponse, InfillRequest,
        PrefixReference,
    },
    error::{ApiError, EngineErrorCode, API_SCHEMA_VERSION},
    ApiJson, ApiState,
};
use crate::{
    gen::{
        GenError, GenerationEvent, GenerationFailureCode, SamplerParams, StopReason, TokenLogprob,
    },
    runtime::{GenerateRequest, GenerationStream, WorkloadRole, PER_REQUEST_DEADLINE},
    sched::PriorityClass,
    telemetry::TraceContext,
};

fn sampler(request: &CompletionRequest) -> Result<SamplerParams, ApiError> {
    let mut params = SamplerParams::default();
    params.temperature = request.temperature.unwrap_or(params.temperature);
    params.top_k = request.top_k.unwrap_or(params.top_k);
    params.top_p = request.top_p.unwrap_or(params.top_p);
    params.min_p = request.min_p.unwrap_or(params.min_p);
    params.typical_p = request.typical_p.unwrap_or(params.typical_p);
    params.repetition_penalty = request.repeat_penalty.unwrap_or(params.repetition_penalty);
    params.presence_penalty = request.presence_penalty.unwrap_or(params.presence_penalty);
    params.frequency_penalty = request
        .frequency_penalty
        .unwrap_or(params.frequency_penalty);
    params.seed = request.seed.unwrap_or(params.seed);
    params.dry_multiplier = request.dry_multiplier.unwrap_or(params.dry_multiplier);
    params.dry_base = request.dry_base.unwrap_or(params.dry_base);
    params.dry_allowed_length = request
        .dry_allowed_length
        .unwrap_or(params.dry_allowed_length);
    params.xtc_probability = request.xtc_probability.unwrap_or(params.xtc_probability);
    params.xtc_threshold = request.xtc_threshold.unwrap_or(params.xtc_threshold);
    params.top_n_sigma = request.top_n_sigma.unwrap_or(params.top_n_sigma);
    params.logit_bias = request
        .logit_bias
        .as_ref()
        .map(|biases| {
            biases
                .iter()
                .map(|(token, value)| {
                    token
                        .parse::<i32>()
                        .map(|token| (token, *value))
                        .map_err(|_| {
                            ApiError::new(
                                EngineErrorCode::UnsupportedParam,
                                "logit_bias keys must be signed token ids",
                            )
                        })
                })
                .collect::<Result<BTreeMap<_, _>, _>>()
        })
        .transpose()?
        .unwrap_or_default();
    Ok(params)
}

fn priority(value: Option<&str>) -> Result<PriorityClass, ApiError> {
    match value.unwrap_or("interactive") {
        "interactive_blocking" => Ok(PriorityClass::InteractiveBlocking),
        "interactive" => Ok(PriorityClass::Interactive),
        "worker" => Ok(PriorityClass::Worker),
        "eval" => Ok(PriorityClass::Eval),
        "background" => Ok(PriorityClass::Background),
        _ => Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "priority_class is not recognized",
        )),
    }
}

fn authorized_priority(
    principal: &Principal,
    priority_class: Option<&str>,
    eval_slot: Option<usize>,
) -> Result<PriorityClass, ApiError> {
    let priority = priority(priority_class)?;
    if matches!(
        priority,
        PriorityClass::InteractiveBlocking | PriorityClass::Eval
    ) && !principal.permits(Scope::Admin)
    {
        return Err(ApiError::forbidden(
            "authenticated principal cannot request the privileged scheduler class",
        ));
    }
    if (priority == PriorityClass::Eval) != eval_slot.is_some() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "eval priority and eval_slot must be supplied together",
        ));
    }
    Ok(priority)
}

fn generate_request(
    context: &TraceContext,
    principal: &Principal,
    request: &CompletionRequest,
    prompt: String,
    suffix: Option<String>,
    endpoint: &'static str,
    original_messages: Vec<(String, String)>,
) -> Result<GenerateRequest, ApiError> {
    request.validate()?;
    if request.json_schema.is_some() && request.grammar.is_none() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "json_schema must be compiled to grammar before engine submission",
        ));
    }
    let priority = authorized_priority(
        principal,
        request.priority_class.as_deref(),
        request.eval_slot,
    )?;
    if (priority == PriorityClass::Eval) != request.eval_context.is_some() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "eval priority and eval_context must be supplied together",
        ));
    }
    if priority == PriorityClass::Eval && request.stream {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "streaming EVAL requests are not supported",
        ));
    }
    if priority == PriorityClass::Eval && request.seed.is_none() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "EVAL requests require an explicit seed",
        ));
    }
    if priority == PriorityClass::Eval && request.session_id.is_some() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "EVAL requests do not support session restoration because restored state is not receipt-bound",
        ));
    }
    if priority == PriorityClass::Eval && !request.prefix_refs.is_empty() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "EVAL requests do not support prefix references because resolved prefix state is not receipt-bound",
        ));
    }
    if request
        .eval_slot
        .is_some_and(|slot| u32::try_from(slot).is_err())
    {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "eval_slot exceeds the receipt field width",
        ));
    }
    Ok(GenerateRequest {
        request_id: context.request_id.clone(),
        trace_id: context.trace_id.clone(),
        principal_id: principal.id.to_string(),
        model: request.model.clone(),
        prompt,
        infill_suffix: suffix,
        max_tokens: request.max_tokens,
        stop: request.stop.clone(),
        sampling: sampler(request)?,
        grammar: request.grammar.clone(),
        priority,
        role: request.role.unwrap_or_default(),
        eval_slot: request.eval_slot,
        eval_context: request.eval_context.clone(),
        endpoint: endpoint.to_owned(),
        original_messages,
        session_id: request.session_id.clone(),
        prefix_refs: request
            .prefix_refs
            .iter()
            .map(|PrefixReference { name, content_hash }| (name.clone(), content_hash.clone()))
            .collect(),
        deadline: Instant::now() + PER_REQUEST_DEADLINE,
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        contract_failure: None,
    })
}

fn finish_reason(reason: &StopReason) -> &'static str {
    match reason {
        StopReason::StopString(_) | StopReason::EndToken(_) => "stop",
        StopReason::MaxTokens => "length",
        StopReason::Cancelled => "cancelled",
        StopReason::Disconnected => "disconnected",
        StopReason::DeadlineExceeded => "timeout",
    }
}

fn gen_error(error: GenError) -> ApiError {
    let code = match &error {
        GenError::GrammarInvalid(_) | GenError::GrammarResourceLimit(_) => {
            EngineErrorCode::GrammarInvalid
        }
        GenError::UnsupportedParam(_)
        | GenError::InvalidSamplerParam(_, _)
        | GenError::FimUnsupported
        | GenError::InvalidFimSentinels(_)
        | GenError::InvalidStop(_) => EngineErrorCode::UnsupportedParam,
        GenError::ContextOverflow { .. } => EngineErrorCode::ContextOverflow,
        GenError::RuntimeFailure { code, .. } => match code {
            GenerationFailureCode::BackendUnavailable => EngineErrorCode::BackendUnavailable,
            GenerationFailureCode::AllocationFailed => EngineErrorCode::AllocationFailed,
            GenerationFailureCode::QueueFull => EngineErrorCode::QueueFull,
            GenerationFailureCode::Draining => EngineErrorCode::Draining,
            GenerationFailureCode::Oom => EngineErrorCode::Oom,
            GenerationFailureCode::SessionUnknown => EngineErrorCode::SessionUnknown,
            GenerationFailureCode::EvalTimeout => EngineErrorCode::EvalTimeout,
            GenerationFailureCode::ModelCorrupt => EngineErrorCode::ModelCorrupt,
            GenerationFailureCode::ModelNotLoaded => EngineErrorCode::ModelNotLoaded,
            GenerationFailureCode::QuotaExhausted => EngineErrorCode::QuotaExhausted,
            GenerationFailureCode::Cancelled => EngineErrorCode::Cancelled,
            GenerationFailureCode::Internal => EngineErrorCode::Internal,
        },
        GenError::NativeSampler(_)
        | GenError::InvalidEmbedding
        | GenError::InvalidLogits(_)
        | GenError::Backend(_)
        | GenError::SpeculationInvalid(_)
        | GenError::SpeculationContextInvalidated(_)
        | GenError::StreamDisconnected
        | GenError::Backpressure
        | GenError::EventTooLarge => EngineErrorCode::Internal,
    };
    tracing::warn!(
        generation_error_code = ?code,
        "generation failure mapped to sanitized public envelope"
    );
    let message = match code {
        EngineErrorCode::ModelNotLoaded => "requested model is not loaded",
        EngineErrorCode::ModelCorrupt => "model is corrupt or unreadable",
        EngineErrorCode::BackendUnavailable => "native inference backend is unavailable",
        EngineErrorCode::ContextOverflow => "generation context exceeds the model limit",
        EngineErrorCode::QueueFull => "engine request queue is full",
        EngineErrorCode::Draining => "engine is draining",
        EngineErrorCode::Oom | EngineErrorCode::AllocationFailed => "native allocation failed",
        EngineErrorCode::QuotaExhausted => "engine resource quota exhausted",
        EngineErrorCode::GrammarInvalid => "grammar is invalid",
        EngineErrorCode::TemplateUntrusted => "model chat template is not trusted by local policy",
        EngineErrorCode::VersionMismatch => "request schema version is not supported",
        EngineErrorCode::Unauthorized => "request is not authorized",
        EngineErrorCode::SessionUnknown => "requested session is unknown",
        EngineErrorCode::UnsupportedParam => "generation request is invalid",
        EngineErrorCode::EvalTimeout => "engine evaluation timed out",
        EngineErrorCode::Cancelled => "request was cancelled",
        EngineErrorCode::EvalReceiptUnavailable => {
            "engine evaluation receipt authority is unavailable"
        }
        EngineErrorCode::EvalAttemptConflict => {
            "evaluation request or attempt was already consumed"
        }
        EngineErrorCode::EvalReceiptError => "engine evaluation receipt could not be committed",
        EngineErrorCode::Internal => "engine request failed internally",
    };
    ApiError::new(code, message)
}

fn wire_error(error: GenError) -> Value {
    wire_api_error(gen_error(error))
}

fn wire_api_error(error: ApiError) -> Value {
    json!({
        "schema_version": API_SCHEMA_VERSION,
        "error": {
            "code": error.body.code,
            "message": error.body.message,
            "retryable": error.body.retryable,
            "details": error.body.details,
        }
    })
}

fn terminal_generation_error(generation: &mut GenerationStream, error: GenError) -> ApiError {
    generation
        .take_receipt_error()
        .map_or_else(|| gen_error(error), ApiError::from)
}

#[cfg(all(feature = "contract-test-controls", debug_assertions))]
fn inject_contract_producer_failure(headers: &HeaderMap, request: &mut GenerateRequest) {
    if std::env::var_os("AMW_ENGINE_ENABLE_TEST_CONTROLS").as_deref()
        != Some(std::ffi::OsStr::new("1"))
    {
        return;
    }
    let Some(name) = headers
        .get("x-amw-test-runtime-error")
        .and_then(|value| value.to_str().ok())
    else {
        return;
    };
    request.contract_failure = crate::runtime::ContractProducerFailure::from_name(name);
}

#[cfg(not(all(feature = "contract-test-controls", debug_assertions)))]
fn inject_contract_producer_failure(_: &HeaderMap, _: &mut GenerateRequest) {}

fn decode_utf8_delta(
    pending: &mut Vec<u8>,
    bytes: Vec<u8>,
    terminal: bool,
) -> Result<String, ApiError> {
    pending.extend(bytes);
    match std::str::from_utf8(pending) {
        Ok(text) => {
            let decoded = text.to_owned();
            pending.clear();
            Ok(decoded)
        }
        Err(error) if error.error_len().is_none() && !terminal => {
            let valid_up_to = error.valid_up_to();
            let decoded = std::str::from_utf8(&pending[..valid_up_to])
                .expect("valid_up_to must delimit valid UTF-8")
                .to_owned();
            pending.drain(..valid_up_to);
            Ok(decoded)
        }
        Err(error) => Err(ApiError::new(
            EngineErrorCode::Internal,
            format!(
                "native generation returned invalid UTF-8 at byte {}",
                error.valid_up_to()
            ),
        )
        .detail("reason", "invalid_utf8")),
    }
}

fn wire_logprobs(entries: Vec<TokenLogprob>) -> Vec<Value> {
    entries
        .into_iter()
        .map(|entry| {
            json!({
                "token_id": entry.token_id,
                "text": String::from_utf8_lossy(&entry.bytes),
                "logprob": entry.logprob,
            })
        })
        .collect()
}

async fn collect(mut generation: GenerationStream) -> Result<CompletionResponse, ApiError> {
    let request_id = generation.request_id().to_owned();
    let trace_id = generation.trace_id().to_owned();
    let model = generation.model().to_owned();
    let mut output = Vec::new();
    while let Some(event) = generation.recv().await {
        match event {
            GenerationEvent::Delta { bytes, .. } => output.extend(bytes),
            GenerationEvent::Finished {
                reason,
                usage,
                confidence,
            } => {
                let text = String::from_utf8(output).map_err(|error| {
                    ApiError::new(
                        EngineErrorCode::Internal,
                        format!(
                            "native generation returned invalid UTF-8 at byte {}",
                            error.utf8_error().valid_up_to()
                        ),
                    )
                    .detail("reason", "invalid_utf8")
                })?;
                let prompt_tokens = u32::try_from(usage.prompt_tokens).map_err(|_| {
                    ApiError::new(
                        EngineErrorCode::Internal,
                        "prompt token count overflowed v1",
                    )
                })?;
                let completion_tokens = u32::try_from(usage.completion_tokens).map_err(|_| {
                    ApiError::new(
                        EngineErrorCode::Internal,
                        "completion token count overflowed v1",
                    )
                })?;
                let engine_receipt = generation.engine_receipt().cloned();
                return Ok(CompletionResponse {
                    schema_version: API_SCHEMA_VERSION,
                    id: request_id.clone(),
                    request_id,
                    trace_id,
                    object: "text_completion".into(),
                    model,
                    text,
                    prompt_tokens,
                    completion_tokens,
                    confidence: confidence.unwrap_or(0.0),
                    finish_reason: finish_reason(&reason).to_owned(),
                    engine_receipt,
                });
            }
            GenerationEvent::Failed(error) => {
                return Err(terminal_generation_error(&mut generation, error));
            }
        }
    }
    Err(ApiError::new(
        EngineErrorCode::Internal,
        "generation stream closed without a terminal event",
    ))
}

fn stream_response(generation: GenerationStream) -> Response {
    let request_id = generation.request_id().to_owned();
    let trace_id = generation.trace_id().to_owned();
    let model = generation.model().to_owned();
    let events = stream::unfold(
        (generation, request_id, trace_id, model, 0_u8, Vec::new()),
        |(mut generation, request_id, trace_id, model, stage, mut pending_utf8)| async move {
            if stage == 2 {
                return None;
            }
            if stage == 1 {
                return Some((
                    Ok::<Event, Infallible>(Event::default().data("[DONE]")),
                    (generation, request_id, trace_id, model, 2, pending_utf8),
                ));
            }
            let (data, next_stage) = match generation.recv().await {
                Some(GenerationEvent::Delta {
                    token_id,
                    bytes,
                    logprob,
                    top_logprobs,
                }) => match decode_utf8_delta(&mut pending_utf8, bytes, false) {
                    Ok(delta) => (
                        json!({
                            "schema_version": API_SCHEMA_VERSION,
                            "type": "delta",
                            "request_id": request_id,
                            "trace_id": trace_id,
                            "model": model,
                            "token_id": token_id,
                            "delta": delta,
                            "logprob": logprob,
                            "top_logprobs": wire_logprobs(top_logprobs),
                        }),
                        0,
                    ),
                    Err(error) => (wire_api_error(error), 1),
                },
                Some(GenerationEvent::Finished {
                    reason,
                    usage,
                    confidence,
                }) => match decode_utf8_delta(&mut pending_utf8, Vec::new(), true) {
                    Ok(_) => (
                        json!({
                            "schema_version": API_SCHEMA_VERSION,
                            "type": "finished",
                            "request_id": request_id,
                            "trace_id": trace_id,
                            "model": model,
                            "finish_reason": finish_reason(&reason),
                            "usage": {
                                "prompt_tokens": usage.prompt_tokens,
                                "completion_tokens": usage.completion_tokens,
                            },
                            "confidence": confidence.unwrap_or(0.0),
                        }),
                        1,
                    ),
                    Err(error) => (wire_api_error(error), 1),
                },
                Some(GenerationEvent::Failed(error)) => (
                    wire_api_error(terminal_generation_error(&mut generation, error)),
                    1,
                ),
                None => (
                    wire_error(GenError::Backend(
                        "generation stream closed without a terminal event".to_owned(),
                    )),
                    1,
                ),
            };
            Some((
                Ok::<Event, Infallible>(Event::default().data(data.to_string())),
                (
                    generation,
                    request_id,
                    trace_id,
                    model,
                    next_stage,
                    pending_utf8,
                ),
            ))
        },
    );
    Sse::new(events)
        .keep_alive(axum::response::sse::KeepAlive::default())
        .into_response()
}

pub async fn completions(
    State(state): State<ApiState>,
    Extension(context): Extension<TraceContext>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    ApiJson(request): ApiJson<CompletionRequest>,
) -> Result<Response, ApiError> {
    let mut runtime_request = generate_request(
        &context,
        &principal,
        &request,
        request.prompt.clone(),
        None,
        "/v1/completions",
        Vec::new(),
    )?;
    inject_contract_producer_failure(&headers, &mut runtime_request);
    let generation = state.runtime.generate(runtime_request).await?;
    if request.stream {
        Ok(stream_response(generation))
    } else {
        Ok(Json(collect(generation).await?).into_response())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChatRequest {
    #[serde(default = "chat_schema")]
    pub schema_version: u32,
    pub model: Option<String>,
    pub messages: Vec<ChatMessage>,
    #[serde(default = "chat_max_tokens")]
    pub max_tokens: u32,
    pub temperature: Option<f32>,
    pub top_k: Option<u32>,
    pub top_p: Option<f32>,
    pub min_p: Option<f32>,
    pub typical_p: Option<f32>,
    pub repeat_penalty: Option<f32>,
    pub presence_penalty: Option<f32>,
    pub frequency_penalty: Option<f32>,
    pub logit_bias: Option<BTreeMap<String, f32>>,
    pub seed: Option<u64>,
    pub dry_multiplier: Option<f32>,
    pub dry_base: Option<f32>,
    pub dry_allowed_length: Option<u32>,
    pub xtc_probability: Option<f32>,
    pub xtc_threshold: Option<f32>,
    pub top_n_sigma: Option<f32>,
    pub grammar: Option<String>,
    #[serde(default)]
    pub stop: Vec<String>,
    pub priority_class: Option<String>,
    pub role: Option<WorkloadRole>,
    pub eval_slot: Option<usize>,
    pub eval_context: Option<crate::receipt::EvalContext>,
    pub session_id: Option<String>,
    #[serde(default)]
    pub prefix_refs: Vec<PrefixReference>,
    #[serde(default)]
    pub stream: bool,
}

#[derive(Clone, Copy, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ChatRole {
    System,
    User,
    Assistant,
    Tool,
}

impl ChatRole {
    const fn as_str(self) -> &'static str {
        match self {
            Self::System => "system",
            Self::User => "user",
            Self::Assistant => "assistant",
            Self::Tool => "tool",
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ChatMessage {
    pub role: ChatRole,
    pub content: String,
}

const fn chat_schema() -> u32 {
    API_SCHEMA_VERSION
}
const fn chat_max_tokens() -> u32 {
    256
}

fn chat_completion(
    request: ChatRequest,
) -> Result<(CompletionRequest, Vec<(String, String)>), ApiError> {
    dto::require_schema(request.schema_version)?;
    if request.messages.is_empty() {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "chat messages must not be empty",
        ));
    }
    let content = request
        .messages
        .iter()
        .map(|message| message.content.clone())
        .collect::<Vec<_>>();
    dto::validate_batch_strings(&content)?;
    if request
        .messages
        .iter()
        .any(|message| message.content.trim().is_empty())
    {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "chat message content must not be empty",
        ));
    }
    let messages = request
        .messages
        .iter()
        .map(|message| (message.role.as_str().to_owned(), message.content.clone()))
        .collect();
    Ok((
        CompletionRequest {
            schema_version: request.schema_version,
            model: request.model,
            prompt: String::new(),
            max_tokens: request.max_tokens,
            temperature: request.temperature,
            top_k: request.top_k,
            top_p: request.top_p,
            min_p: request.min_p,
            typical_p: request.typical_p,
            repeat_penalty: request.repeat_penalty,
            presence_penalty: request.presence_penalty,
            frequency_penalty: request.frequency_penalty,
            logit_bias: request.logit_bias,
            seed: request.seed,
            dry_multiplier: request.dry_multiplier,
            dry_base: request.dry_base,
            dry_allowed_length: request.dry_allowed_length,
            xtc_probability: request.xtc_probability,
            xtc_threshold: request.xtc_threshold,
            top_n_sigma: request.top_n_sigma,
            grammar: request.grammar,
            json_schema: None,
            stop: request.stop,
            priority_class: request.priority_class,
            role: request.role,
            eval_slot: request.eval_slot,
            eval_context: request.eval_context,
            session_id: request.session_id,
            prefix_refs: request.prefix_refs,
            stream: request.stream,
        },
        messages,
    ))
}

pub async fn chat_completions(
    State(state): State<ApiState>,
    Extension(context): Extension<TraceContext>,
    Extension(principal): Extension<Principal>,
    ApiJson(request): ApiJson<ChatRequest>,
) -> Result<Response, ApiError> {
    let (mut request, messages) = chat_completion(request)?;
    if request.stream && request.priority_class.as_deref() == Some("eval") {
        return Err(ApiError::new(
            EngineErrorCode::UnsupportedParam,
            "streaming EVAL requests are not supported",
        ));
    }
    let rendered = state
        .runtime
        .render_chat(request.model.as_deref(), messages.clone())
        .await?;
    request.prompt = rendered.prompt().to_owned();
    let runtime_request = generate_request(
        &context,
        &principal,
        &request,
        request.prompt.clone(),
        None,
        "/v1/chat/completions",
        messages,
    )?;
    let generation = state
        .runtime
        .generate_chat(runtime_request, rendered)
        .await?;
    if request.stream {
        return Ok(stream_response(generation));
    }
    let completion = collect(generation).await?;
    let mut response = json!({
        "schema_version": API_SCHEMA_VERSION,
        "id": completion.id,
        "request_id": completion.request_id,
        "trace_id": completion.trace_id,
        "object": "chat.completion",
        "model": completion.model,
        "content": completion.text,
        "confidence": completion.confidence,
        "choices": [{"index":0,"message":{"role":"assistant","content":completion.text},"finish_reason":completion.finish_reason}],
        "usage": {"prompt_tokens":completion.prompt_tokens,"completion_tokens":completion.completion_tokens}
    });
    if let Some(receipt) = completion.engine_receipt {
        response["engine_receipt"] = json!(receipt);
    }
    Ok(Json(response).into_response())
}

pub async fn infill(
    State(state): State<ApiState>,
    Extension(context): Extension<TraceContext>,
    Extension(principal): Extension<Principal>,
    ApiJson(request): ApiJson<InfillRequest>,
) -> Result<Response, ApiError> {
    if request.suffix.len() > dto::MAX_PROMPT_BYTES {
        return Err(ApiError::new(
            EngineErrorCode::ContextOverflow,
            "suffix exceeds prompt limit",
        ));
    }
    let runtime_request = generate_request(
        &context,
        &principal,
        &request.completion,
        request.completion.prompt.clone(),
        Some(request.suffix),
        "/v1/infill",
        Vec::new(),
    )?;
    let generation = state.runtime.generate(runtime_request).await?;
    if request.completion.stream {
        Ok(stream_response(generation))
    } else {
        Ok(Json(collect(generation).await?).into_response())
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EmbeddingRequest {
    #[serde(default = "chat_schema")]
    schema_version: u32,
    #[serde(alias = "model_id")]
    model: Option<String>,
    input: Vec<String>,
}

pub async fn embeddings(
    State(state): State<ApiState>,
    ApiJson(request): ApiJson<EmbeddingRequest>,
) -> Result<Json<Value>, ApiError> {
    dto::require_schema(request.schema_version)?;
    dto::validate_batch_strings(&request.input)?;
    let vectors = state
        .runtime
        .embeddings(request.model.as_deref(), request.input)
        .await?;
    Ok(Json(json!({
        "schema_version": API_SCHEMA_VERSION,
        "object":"list",
        "data": vectors.iter().enumerate().map(|(index, embedding)| json!({"object":"embedding","index":index,"embedding":embedding})).collect::<Vec<_>>()
    })))
}

pub async fn models(State(state): State<ApiState>) -> Json<Value> {
    Json(
        json!({"schema_version":API_SCHEMA_VERSION,"object":"list","data":state.runtime.status().models}),
    )
}

pub async fn tokenize(
    State(state): State<ApiState>,
    ApiJson(request): ApiJson<BatchTextRequest>,
) -> Result<Json<Value>, ApiError> {
    request.validate()?;
    let results = state
        .runtime
        .tokenize(request.model.as_deref(), request.items, request.add_special)
        .await?;
    Ok(Json(
        json!({"schema_version":API_SCHEMA_VERSION,"results":results}),
    ))
}

pub async fn count(
    State(state): State<ApiState>,
    ApiJson(request): ApiJson<BatchTextRequest>,
) -> Result<Json<Value>, ApiError> {
    request.validate()?;
    let counts = state
        .runtime
        .count_tokens(request.model.as_deref(), request.items, request.add_special)
        .await?;
    Ok(Json(
        json!({"schema_version":API_SCHEMA_VERSION,"counts":counts}),
    ))
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct CancelRequest {
    #[serde(flatten)]
    pub version: dto::ControlVersion,
    pub request_id: String,
}

pub async fn cancel(
    State(state): State<ApiState>,
    Extension(principal): Extension<Principal>,
    ApiJson(request): ApiJson<CancelRequest>,
) -> Result<Json<Value>, ApiError> {
    request.version.validate()?;
    state
        .runtime
        .cancel_owned(&request.request_id, principal.id.as_ref())?;
    Ok(Json(
        json!({"schema_version":API_SCHEMA_VERSION,"request_id":request.request_id,"cancelled":true}),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;

    use crate::runtime::{receipt_terminal_test_stream, ReceiptTerminalTestFailure};
    use http_body_util::BodyExt as _;

    async fn receipt_failure_stream_body(failure: ReceiptTerminalTestFailure) -> String {
        let (generation, _ledger) = receipt_terminal_test_stream(failure);
        let response = stream_response(generation);
        let body = response
            .into_body()
            .collect()
            .await
            .expect("finite receipt failure stream is readable")
            .to_bytes();
        String::from_utf8(body.to_vec()).expect("SSE receipt failure envelope is UTF-8")
    }

    #[tokio::test]
    async fn nonstreaming_eval_signer_failure_returns_only_receipt_error() {
        let (generation, ledger) = receipt_terminal_test_stream(ReceiptTerminalTestFailure::Signer);

        let error = collect(generation)
            .await
            .expect_err("terminal signer failure must not produce a completion response");

        assert_eq!(error.body.code, EngineErrorCode::EvalReceiptError);
        assert!(ledger
            .receipt_for_request("terminal-signer-failure")
            .expect("intact ledger remains readable")
            .is_none());
    }

    #[tokio::test]
    async fn nonstreaming_eval_ledger_failure_returns_only_receipt_error() {
        let (generation, ledger) = receipt_terminal_test_stream(ReceiptTerminalTestFailure::Ledger);

        let error = collect(generation)
            .await
            .expect_err("terminal ledger failure must not produce a completion response");

        assert_eq!(error.body.code, EngineErrorCode::EvalReceiptError);
        assert!(ledger
            .receipt_for_request("terminal-ledger-failure")
            .is_err());
    }

    #[tokio::test]
    async fn streaming_eval_signer_failure_preserves_nonretryable_receipt_error() {
        let body = receipt_failure_stream_body(ReceiptTerminalTestFailure::Signer).await;

        assert!(body.contains("\"code\":\"eval_receipt_error\""));
        assert!(body.contains("\"retryable\":false"));
        assert!(!body.contains("\"code\":\"internal\""));
        assert!(!body.contains("\"type\":\"finished\""));
        assert!(!body.contains("engine_receipt"));
    }

    #[tokio::test]
    async fn streaming_eval_ledger_failure_preserves_nonretryable_receipt_error() {
        let body = receipt_failure_stream_body(ReceiptTerminalTestFailure::Ledger).await;

        assert!(body.contains("\"code\":\"eval_receipt_error\""));
        assert!(body.contains("\"retryable\":false"));
        assert!(!body.contains("\"code\":\"internal\""));
        assert!(!body.contains("\"type\":\"finished\""));
        assert!(!body.contains("engine_receipt"));
    }

    #[test]
    fn incremental_utf8_preserves_split_scalar_and_refuses_incomplete_terminal() {
        let mut pending = Vec::new();
        assert_eq!(
            decode_utf8_delta(&mut pending, vec![0xe2], false).unwrap(),
            ""
        );
        assert_eq!(
            decode_utf8_delta(&mut pending, vec![0x82], false).unwrap(),
            ""
        );
        assert_eq!(
            decode_utf8_delta(&mut pending, vec![0xac], false).unwrap(),
            "\u{20ac}"
        );
        assert!(pending.is_empty());

        assert_eq!(
            decode_utf8_delta(&mut pending, vec![0xf0, 0x9f], false).unwrap(),
            ""
        );
        let error = decode_utf8_delta(&mut pending, Vec::new(), true).unwrap_err();
        assert_eq!(error.body.code, EngineErrorCode::Internal);
        assert_eq!(
            error.body.details.get("reason").map(String::as_str),
            Some("invalid_utf8")
        );
    }

    #[test]
    fn chat_dto_rejects_unknown_roles_and_preserves_sampler_controls() {
        assert!(serde_json::from_value::<ChatRequest>(json!({
            "schema_version": 1,
            "messages": [{"role": "unique-attacker-role", "content": "x"}]
        }))
        .is_err());

        let request: ChatRequest = serde_json::from_value(json!({
            "schema_version": 1,
            "model": "m",
            "messages": [{"role": "user", "content": "hello"}],
            "repeat_penalty": 1.1,
            "presence_penalty": 0.2,
            "frequency_penalty": 0.3,
            "dry_multiplier": 0.4,
            "dry_base": 1.2,
            "dry_allowed_length": 4,
            "xtc_probability": 0.5,
            "xtc_threshold": 0.6,
            "top_n_sigma": 0.7
        }))
        .unwrap();
        let (completion, messages) = chat_completion(request).unwrap();
        assert_eq!(messages, vec![("user".to_owned(), "hello".to_owned())]);
        assert_eq!(completion.repeat_penalty, Some(1.1));
        assert_eq!(completion.dry_allowed_length, Some(4));
        assert_eq!(completion.xtc_threshold, Some(0.6));
    }

    #[test]
    fn invalidated_speculation_context_uses_retryable_internal_envelope() {
        let secret = r"C:\private\native\context-secret.gguf";
        let error = gen_error(GenError::SpeculationContextInvalidated(secret.to_owned()));

        assert_eq!(error.body.code, EngineErrorCode::Internal);
        assert!(error.body.retryable);
        assert_eq!(
            wire_api_error(error.clone()),
            json!({
                "schema_version": API_SCHEMA_VERSION,
                "error": {
                    "code": "internal",
                    "message": "engine request failed internally",
                    "retryable": true,
                    "details": {}
                }
            })
        );
        assert!(!serde_json::to_string(&wire_api_error(error))
            .unwrap()
            .contains(secret));
    }

    #[test]
    fn streamed_runtime_oom_preserves_the_typed_public_error_code() {
        let secret = r"C:\private\native\allocator-secret.gguf";
        let error = gen_error(GenError::RuntimeFailure {
            code: GenerationFailureCode::Oom,
            message: secret.to_owned(),
        });

        assert_eq!(error.body.code, EngineErrorCode::Oom);
        assert_eq!(
            wire_api_error(error.clone()),
            json!({
                "schema_version": API_SCHEMA_VERSION,
                "error": {
                    "code": "oom",
                    "message": "native allocation failed",
                    "retryable": true,
                    "details": {}
                }
            })
        );
        assert!(!serde_json::to_string(&wire_api_error(error))
            .unwrap()
            .contains(secret));
    }

    #[test]
    fn every_stream_failure_variant_redacts_native_paths_and_diagnostics() {
        let secret = r"C:\private\native\stream-secret.gguf";
        let failures = [
            GenError::GrammarInvalid(secret.to_owned()),
            GenError::GrammarResourceLimit(secret),
            GenError::UnsupportedParam(secret),
            GenError::InvalidSamplerParam("temperature", secret),
            GenError::InvalidFimSentinels(secret),
            GenError::InvalidStop(secret),
            GenError::RuntimeFailure {
                code: GenerationFailureCode::SessionUnknown,
                message: secret.to_owned(),
            },
            GenError::NativeSampler(secret.to_owned()),
            GenError::InvalidLogits(secret),
            GenError::Backend(secret.to_owned()),
            GenError::SpeculationInvalid(secret),
            GenError::SpeculationContextInvalidated(secret.to_owned()),
        ];

        for failure in failures {
            let serialized = serde_json::to_string(&wire_error(failure)).unwrap();
            assert!(
                !serialized.contains(secret),
                "stream failure leaked: {serialized}"
            );
        }
    }

    #[test]
    fn privileged_priority_classes_require_admin_scope() {
        let inference = Principal::new("inference", Arc::from([Scope::Inference]));
        for (priority, eval_slot) in [("interactive_blocking", None), ("eval", Some(0))] {
            let error = authorized_priority(&inference, Some(priority), eval_slot).unwrap_err();
            assert_eq!(error.body.code, EngineErrorCode::Unauthorized);
            assert_eq!(
                error.into_response().status(),
                axum::http::StatusCode::FORBIDDEN
            );
        }
        assert_eq!(
            authorized_priority(&inference, Some("interactive"), None).unwrap(),
            PriorityClass::Interactive
        );
    }

    #[test]
    fn eval_priority_and_slot_are_authorized_as_one_server_side_class() {
        let admin = Principal::new("admin", Arc::from([Scope::Inference, Scope::Admin]));
        assert_eq!(
            authorized_priority(&admin, Some("eval"), Some(3)).unwrap(),
            PriorityClass::Eval
        );
        for (priority, eval_slot) in [("eval", None), ("worker", Some(3))] {
            let error = authorized_priority(&admin, Some(priority), eval_slot).unwrap_err();
            assert_eq!(error.body.code, EngineErrorCode::UnsupportedParam);
        }
    }
}
