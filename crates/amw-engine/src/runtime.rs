//! Live AM Engine control plane and blocking native-model workers.

use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    io::{self, Read},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        mpsc::{self, Receiver, RecvTimeoutError, SyncSender, TrySendError},
        Arc, Mutex, RwLock, Weak,
    },
    time::{Duration, Instant, SystemTime, UNIX_EPOCH},
};

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::oneshot;

#[cfg(any(feature = "cpu", feature = "cuda"))]
use std::sync::mpsc::TryRecvError;
#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
use std::sync::{Condvar, OnceLock};

use crate::{
    config::{EngineConfig, KvRootPolicy, ReceiptConfig},
    gen::{
        bounded_generation_stream, GenerationControl, GenerationReceiver, GenerationSender,
        SamplerParams,
    },
    hw::budget::{MemoryAmount, MemoryLedger},
    receipt::{
        verify_receipt_signature, AttemptIdentity, Digest32, EvalReceiptClaims,
        KeyRotationPredecessor, LedgerError, ReceiptLedger, ReceiptReservation, ReceiptSigner,
        SignedEvalReceipt, SignerProvider, SignerTrust, SIGNATURE_ALGORITHM,
    },
    sched::{PriorityClass, SchedError},
    store::{
        adapter::{
            AdapterRegistration, AdapterRegistry, AdapterRegistryError, VerifiedAdapterRecord,
            VerifiedLoadGuard, DEFAULT_MAX_ADAPTER_BYTES,
        },
        gguf_meta::GgufMetadata,
        loader::{
            KeepAlive as LoaderKeepAlive, LoaderError, ModelLoader, MonotonicClock,
            VerifiedModelFile,
        },
        registry::{CatalogDiagnostic, CatalogModel, ModelRecord, ModelRegistry, RegistryError},
        scan::ScanLimits,
        session::{
            ensure_private_directory, prepare_managed_private_root, verify_secure_directory,
            SessionStore, SessionStoreError,
        },
    },
    telemetry::{
        events::{EngineEvent, EventEnvelope},
        metrics::MetricsHub,
        TelemetryHub,
    },
    watchdog::{
        MemoryLeakMonitor, SystemUptimeClock, Watchdog, WatchdogAction, WatchdogCallbackError,
        WatchdogEvent, WATCHDOG_POLL_INTERVAL_SECS,
    },
};

#[cfg(any(windows, test))]
use crate::receipt::{
    KeyExportPolicy, PlatformKeyAttestation, ProtectedKeyReference, ProtectedReceiptSigner,
    ProtectedSignerBinding, ProtectedSigningBackend, SignerError,
};

pub use crate::gen::GenerationEvent;

#[cfg(any(feature = "cpu", feature = "cuda"))]
use crate::{
    ffi::{
        Batch, ChatMessage, ChatRole, Context, ContextOptions, EmbeddingPooling, LoraAdapter,
        Model, MAX_NATIVE_SEQUENCES,
    },
    gen::{
        assemble_infill, execute_embedding_batch, resolve_draft_mode, CompiledGrammar, DraftJob,
        DraftMode, DraftModelCompatibility, DraftModelProposer, DraftProposal, DraftResult,
        EmbeddingInput, EmbeddingOptions, ExternalBundlePreview, ExternalBundleToken, FimTokenMap,
        GenError, GenerationControlState, GenerationExecutor, GenerationFailureCode,
        GenerationStep, GenerationUsage, ModelFamily, NativeDecodeBackend, NativeDraftBackend,
        NativeEmbeddingBackend, NativeGenerationConfig, NativeSamplerTxn, PoolingMode,
        PromptLookupProposer, ProposalSource, SamplerCapabilities, SpeculationDecision,
        SpeculationEligibility, SpeculationPlan, StepOutcome, StopEvaluator, StopReason,
        TargetKvFork, TargetProbe, TargetVerification, MAX_DRAFT_ACTOR_IN_FLIGHT, MAX_DRAFT_TOKENS,
        MAX_SPECULATION_WORKER_RETAINED_BYTES,
    },
    sched::{
        AdmissionRequest, CoreSessionRestoreOptions, DecodeProgress, SeqId, SequencePhase,
        SessionContinuation, TerminationReason,
    },
    sched::{SchedEvent, SchedulerCore, SchedulerCoreConfig},
    store::template::{TemplatePolicy, TemplateVerdict},
    telemetry::{
        metrics::{RequestMetrics, TerminalOutcome},
        TraceContext,
    },
    watchdog::WorkClass,
};

#[cfg(any(feature = "cpu", feature = "cuda"))]
use crate::receipt::{
    absent_sha256, original_messages_sha256, system_messages_sha256, AbsentDigestField,
};

#[cfg(any(feature = "cpu", feature = "cuda"))]
use crate::store::session::SessionKey;

pub const PER_REQUEST_DEADLINE: Duration = Duration::from_secs(600);
const MODEL_COMMAND_QUEUE_MULTIPLIER: usize = 4;
const MODEL_LOAD_BASE_TIMEOUT: Duration = Duration::from_secs(20);
const MODEL_LOAD_SECS_PER_GIB: u64 = 60;
#[cfg(any(feature = "cpu", feature = "cuda"))]
const MAX_COMMANDS_PER_BOUNDARY: usize = 64;
#[cfg(any(feature = "cpu", feature = "cuda"))]
const MAX_BATCH_ACTUAL_TOKENS: usize = 262_144;
#[cfg(any(feature = "cpu", feature = "cuda"))]
const MAX_EMBEDDING_INTERMEDIATE_BYTES: usize = 128 * 1024 * 1024;
#[cfg(any(feature = "cpu", feature = "cuda"))]
const MAX_EMBEDDING_RESPONSE_COMPONENTS: usize = 262_144;
/// Anti-thrash ceiling for repeated pressure recomputation of one Background request.
#[cfg(any(feature = "cpu", feature = "cuda"))]
const MAX_BACKGROUND_READMISSIONS: u8 = 2;
#[cfg(any(feature = "cpu", feature = "cuda"))]
const LLAMA_NATIVE_SEQUENCE_LIMIT: usize = MAX_NATIVE_SEQUENCES as usize;
#[cfg(any(feature = "cpu", feature = "cuda"))]
const DRAFT_ACTOR_COMMAND_CAPACITY: usize =
    MAX_DRAFT_ACTOR_IN_FLIGHT + LLAMA_NATIVE_SEQUENCE_LIMIT * 2;
#[cfg(all(test, any(feature = "cpu", feature = "cuda")))]
static DRAFT_ACTOR_TARGET_YIELD_MS: AtomicU64 = AtomicU64::new(0);
const GIB: f64 = 1024.0 * 1024.0 * 1024.0;
static ENGINE_INSTANCE_COUNTER: AtomicU64 = AtomicU64::new(1);

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
const CONTRACT_STOP_CANCEL_REQUEST_ID: &str = "stop-cancel-boundary";

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
#[derive(Default)]
struct ContractStopCancelState {
    reached_stop_boundary: bool,
    cancellation_applied: bool,
}

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
fn contract_stop_cancel_barriers(
) -> &'static (Mutex<BTreeMap<String, ContractStopCancelState>>, Condvar) {
    static BARRIERS: OnceLock<(Mutex<BTreeMap<String, ContractStopCancelState>>, Condvar)> =
        OnceLock::new();
    BARRIERS.get_or_init(|| (Mutex::new(BTreeMap::new()), Condvar::new()))
}

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
fn contract_stop_cancel_enabled(request_id: &str) -> bool {
    request_id == CONTRACT_STOP_CANCEL_REQUEST_ID
        && std::env::var_os("AMW_ENGINE_ENABLE_TEST_CONTROLS").as_deref()
            == Some(std::ffi::OsStr::new("1"))
}

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
fn contract_wait_for_stop_boundary(request_id: &str) -> Result<(), RuntimeError> {
    if !contract_stop_cancel_enabled(request_id) {
        return Ok(());
    }
    let (states, changed) = contract_stop_cancel_barriers();
    let mut states = states
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    let deadline = Instant::now() + Duration::from_secs(5);
    while !states
        .entry(request_id.to_owned())
        .or_default()
        .reached_stop_boundary
    {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            states.remove(request_id);
            return Err(RuntimeError::Internal(
                "contract stop-boundary barrier timed out".to_owned(),
            ));
        }
        let (next, timeout) = changed
            .wait_timeout(states, remaining)
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        states = next;
        if timeout.timed_out()
            && !states
                .get(request_id)
                .is_some_and(|state| state.reached_stop_boundary)
        {
            states.remove(request_id);
            return Err(RuntimeError::Internal(
                "contract stop-boundary barrier timed out".to_owned(),
            ));
        }
    }
    Ok(())
}

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
fn contract_release_stop_boundary(request_id: &str) {
    if !contract_stop_cancel_enabled(request_id) {
        return;
    }
    let (states, changed) = contract_stop_cancel_barriers();
    let mut states = states
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    states
        .entry(request_id.to_owned())
        .or_default()
        .cancellation_applied = true;
    changed.notify_all();
}

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
fn contract_hold_stop_boundary(request_id: &str) {
    if !contract_stop_cancel_enabled(request_id) {
        return;
    }
    let (states, changed) = contract_stop_cancel_barriers();
    let mut states = states
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    states
        .entry(request_id.to_owned())
        .or_default()
        .reached_stop_boundary = true;
    changed.notify_all();
    let deadline = Instant::now() + Duration::from_secs(5);
    while !states
        .get(request_id)
        .is_some_and(|state| state.cancellation_applied)
    {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            break;
        }
        let (next, timeout) = changed
            .wait_timeout(states, remaining)
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        states = next;
        if timeout.timed_out() {
            break;
        }
    }
    states.remove(request_id);
}

#[derive(Clone, Debug, Error)]
pub enum RuntimeError {
    #[error("model is not loaded: {0}")]
    ModelNotLoaded(String),
    #[error("model is corrupt or unreadable")]
    ModelCorrupt { path: PathBuf, reason: String },
    #[error("this engine binary was built without a native CPU or CUDA backend")]
    NativeUnavailable,
    #[error("context overflow: requested {requested} tokens, limit {limit}")]
    ContextOverflow { requested: u32, limit: u32 },
    #[error("model worker queue is full")]
    QueueFull,
    #[error("session persistence quota is exhausted: {0}")]
    QuotaExhausted(String),
    #[error("engine is draining")]
    Draining,
    #[error("native allocation was refused: {0}")]
    Oom(String),
    #[error("Background KV readmission limit exhausted after {attempts} attempts")]
    BackgroundReadmissionLimit { attempts: u8 },
    #[error("invalid grammar: {0}")]
    GrammarInvalid(String),
    #[error("model chat template is not trusted by local policy")]
    TemplateUntrusted,
    #[error("unknown session: {0}")]
    SessionUnknown(String),
    #[error("unsupported parameter: {0}")]
    UnsupportedParam(String),
    #[error("adapter registration or resolution failed validation")]
    AdapterInvalid,
    #[error("evaluation deadline expired")]
    EvalTimeout,
    #[error("request was cancelled")]
    Cancelled,
    #[error("the requested resource is not accessible to this principal")]
    Unauthorized,
    #[error("engine evaluation receipt authority is unavailable")]
    EvalReceiptUnavailable,
    #[error("engine evaluation receipt authority initialization failed: {0}")]
    EvalReceiptAuthority(String),
    #[error("evaluation attempt identity was already consumed")]
    EvalAttemptConflict,
    #[error("engine evaluation receipt commitment failed: {0}")]
    EvalReceiptCommit(String),
    #[error("engine runtime invariant failed: {0}")]
    Internal(String),
}

impl From<SchedError> for RuntimeError {
    fn from(error: SchedError) -> Self {
        match error {
            SchedError::ContextOverflow { requested, limit } => {
                Self::ContextOverflow { requested, limit }
            }
            SchedError::Oom { requested_bytes } => Self::Oom(format!("{requested_bytes} bytes")),
            SchedError::QueueFull | SchedError::QuotaFull { .. } => Self::QueueFull,
            SchedError::Draining => Self::Draining,
            SchedError::SessionUnknown(session) => Self::SessionUnknown(session),
            SchedError::EvalTimeout => Self::EvalTimeout,
            other => Self::Internal(other.to_string()),
        }
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct ModelInfo {
    pub id: String,
    #[serde(skip_serializing)]
    pub path: PathBuf,
    pub architecture: String,
    pub quant: String,
    pub context_length: u32,
    pub embedding_length: u32,
    pub supports_embeddings: bool,
    pub supports_fim: bool,
    #[serde(skip_serializing)]
    pub chat_template: Option<String>,
    #[serde(skip_serializing)]
    model_fingerprint: [u8; 32],
}

#[derive(Clone, Debug, Serialize)]
pub struct RuntimeStatus {
    pub draining: bool,
    pub models: Vec<ModelInfo>,
}

#[derive(Clone, Debug, Serialize)]
pub struct SlotStatus {
    pub model_id: String,
    pub busy: usize,
    pub queue_depth: usize,
    pub slot_count: usize,
    pub max_batch_sequences: usize,
    pub background_evicted: usize,
}

/// Canonical product actor associated with an inference workload.
///
/// This role is deliberately independent from scheduler priority: it identifies
/// which product pipeline actor caused the work, while [`PriorityClass`]
/// controls when the scheduler runs it. Unrecognized wire values fail into the
/// bounded `Unknown` bucket instead of creating attacker-controlled metric
/// cardinality.
#[derive(Clone, Copy, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkloadRole {
    Foreman,
    Worker,
    Inspector,
    #[default]
    #[serde(other)]
    Unknown,
}

impl WorkloadRole {
    /// Returns the stable, bounded telemetry label for this workload role.
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Foreman => "foreman",
            Self::Worker => "worker",
            Self::Inspector => "inspector",
            Self::Unknown => "unknown",
        }
    }
}

#[derive(Clone, Debug)]
pub struct GenerateRequest {
    pub request_id: String,
    pub trace_id: String,
    pub principal_id: String,
    pub model: Option<String>,
    pub prompt: String,
    pub infill_suffix: Option<String>,
    pub max_tokens: u32,
    pub stop: Vec<String>,
    pub sampling: SamplerParams,
    pub grammar: Option<String>,
    pub priority: PriorityClass,
    pub role: WorkloadRole,
    pub eval_slot: Option<usize>,
    pub eval_context: Option<crate::receipt::EvalContext>,
    pub endpoint: String,
    pub original_messages: Vec<(String, String)>,
    pub session_id: Option<String>,
    pub prefix_refs: Vec<(String, String)>,
    pub deadline: Instant,
    #[cfg(all(feature = "contract-test-controls", debug_assertions))]
    pub(crate) contract_failure: Option<ContractProducerFailure>,
}

/// Installation/build identity loaded from a separately verified trust anchor.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiptRuntimeIdentity {
    pub installation_id: String,
    pub anchor_sha256: Digest32,
    pub authority_pin_sha256: Digest32,
    pub engine_release: String,
    pub source_commit: String,
    pub libllama_revision: String,
    pub release_manifest_sha256: Digest32,
    pub engine_binary_sha256: Digest32,
}

const RECEIPT_ANCHOR_SCHEMA_VERSION: u16 = 2;
const MAX_RECEIPT_ANCHOR_BYTES: usize = 64 * 1024;
const RECEIPT_BOOTSTRAP_CHALLENGE_DOMAIN: &[u8] = b"AMW\0receipt-authority-bootstrap-v1\0";
const RECEIPT_ANCHOR_V2_DOMAIN: &[u8] = b"AMW\0engine-installation-trust-anchor-v2\0";

/// Strict protected installer record used to bootstrap engine receipt trust.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiptTrustAnchor {
    pub schema_version: u16,
    pub installation_id: String,
    pub key_id: Digest32,
    pub key_epoch: u64,
    pub algorithm: String,
    pub public_key_spki_der: String,
    pub provider: SignerProvider,
    pub trust: SignerTrust,
    pub service_identity: String,
    pub engine_release: String,
    pub source_commit: String,
    pub libllama_revision: String,
    pub release_manifest_sha256: Digest32,
    pub engine_binary_sha256: Digest32,
    pub authenticode_signer_identity: Option<String>,
    pub created_at: String,
    pub predecessor_key_id: Option<Digest32>,
    pub predecessor_key_epoch: Option<u64>,
    pub predecessor_anchor_sha256: Option<Digest32>,
    pub authority_key_id: Digest32,
    pub authority_public_key_spki_der: String,
    pub proof_of_possession: String,
    pub authority_signature: String,
}

struct UniqueAnchorFields(BTreeSet<String>);

impl<'de> Deserialize<'de> for UniqueAnchorFields {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct UniqueAnchorFieldsVisitor;

        impl<'de> serde::de::Visitor<'de> for UniqueAnchorFieldsVisitor {
            type Value = UniqueAnchorFields;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a duplicate-free engine trust anchor object")
            }

            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: serde::de::MapAccess<'de>,
            {
                use serde::de::Error as _;

                let mut fields = BTreeSet::new();
                while let Some(field) = map.next_key::<String>()? {
                    if !fields.insert(field.clone()) {
                        return Err(A::Error::custom(format!(
                            "duplicate engine trust anchor field: {field}"
                        )));
                    }
                    map.next_value::<serde::de::IgnoredAny>()?;
                }
                Ok(UniqueAnchorFields(fields))
            }
        }

        deserializer.deserialize_map(UniqueAnchorFieldsVisitor)
    }
}

#[derive(Clone, Debug)]
struct VerifiedReceiptTrustAnchor {
    record: ReceiptTrustAnchor,
    anchor_sha256: Digest32,
    public_key_spki_der: Vec<u8>,
    #[cfg(target_os = "linux")]
    anchor_path: PathBuf,
}

struct ResolvedProtectedSigner {
    signer: Arc<dyn ReceiptSigner>,
    service_identity: String,
}

trait ProtectedSignerResolver: Send + Sync {
    fn resolve(
        &self,
        anchor: &VerifiedReceiptTrustAnchor,
    ) -> Result<ResolvedProtectedSigner, RuntimeError>;
}

#[derive(Clone, Copy, Debug, Default)]
struct PlatformProtectedSignerResolver;

impl ProtectedSignerResolver for PlatformProtectedSignerResolver {
    fn resolve(
        &self,
        anchor: &VerifiedReceiptTrustAnchor,
    ) -> Result<ResolvedProtectedSigner, RuntimeError> {
        resolve_platform_protected_signer(anchor)
    }
}

fn load_verified_receipt_anchor(
    config: &ReceiptConfig,
) -> Result<VerifiedReceiptTrustAnchor, RuntimeError> {
    let anchor_path = config
        .trust_anchor_path
        .as_deref()
        .ok_or(RuntimeError::EvalReceiptUnavailable)?;
    let expected_anchor_sha256 = Digest32::from_lower_hex(
        config
            .anchor_sha256
            .as_deref()
            .ok_or(RuntimeError::EvalReceiptUnavailable)?,
    )
    .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
    let expected_authority_pin = Digest32::from_lower_hex(
        config
            .authority_pin_sha256
            .as_deref()
            .ok_or(RuntimeError::EvalReceiptUnavailable)?,
    )
    .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
    let bytes = read_receipt_anchor_bytes(anchor_path)?;
    let anchor_sha256 = Digest32::sha256(&bytes);
    if anchor_sha256 != expected_anchor_sha256 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor digest does not match configuration".to_owned(),
        ));
    }
    let record: ReceiptTrustAnchor = serde_json::from_slice(&bytes).map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!("protected trust anchor is invalid: {error}"))
    })?;
    let fields: UniqueAnchorFields = serde_json::from_slice(&bytes).map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!("protected trust anchor is invalid: {error}"))
    })?;
    const REQUIRED_FIELDS: [&str; 23] = [
        "schema_version",
        "installation_id",
        "key_id",
        "key_epoch",
        "algorithm",
        "public_key_spki_der",
        "provider",
        "trust",
        "service_identity",
        "engine_release",
        "source_commit",
        "libllama_revision",
        "release_manifest_sha256",
        "engine_binary_sha256",
        "authenticode_signer_identity",
        "created_at",
        "predecessor_key_id",
        "predecessor_key_epoch",
        "predecessor_anchor_sha256",
        "authority_key_id",
        "authority_public_key_spki_der",
        "proof_of_possession",
        "authority_signature",
    ];
    if fields.0.len() != REQUIRED_FIELDS.len()
        || REQUIRED_FIELDS
            .iter()
            .any(|field| !fields.0.contains(*field))
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor fields do not match schema version 2".to_owned(),
        ));
    }
    if record.schema_version != RECEIPT_ANCHOR_SCHEMA_VERSION
        || record.algorithm != SIGNATURE_ALGORITHM
        || record.provider == SignerProvider::SoftwareTest
        || record.trust != SignerTrust::ProductionProtected
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor declares unsupported trust semantics".to_owned(),
        ));
    }
    for (field, value) in [
        ("installation_id", record.installation_id.as_str()),
        ("service_identity", record.service_identity.as_str()),
        ("engine_release", record.engine_release.as_str()),
        ("source_commit", record.source_commit.as_str()),
        ("libllama_revision", record.libllama_revision.as_str()),
        ("created_at", record.created_at.as_str()),
    ] {
        validate_receipt_anchor_string(field, value, 4_096)?;
    }
    if let Some(authenticode) = record.authenticode_signer_identity.as_deref() {
        validate_receipt_anchor_string("authenticode_signer_identity", authenticode, 4_096)?;
    }
    if record.provider == SignerProvider::WindowsCngMachine
        && record.authenticode_signer_identity.is_none()
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "Windows protected trust anchors require an Authenticode signer identity".to_owned(),
        ));
    }
    if !valid_rfc3339_utc(&record.created_at) {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor created_at is not RFC3339 UTC".to_owned(),
        ));
    }
    match (
        record.key_epoch,
        record.predecessor_key_id,
        record.predecessor_key_epoch,
        record.predecessor_anchor_sha256,
    ) {
        (1, None, None, None) => {}
        (epoch, Some(_), Some(predecessor_epoch), Some(_))
            if epoch > 1 && predecessor_epoch < epoch => {}
        _ => {
            return Err(RuntimeError::EvalReceiptAuthority(
                "protected trust anchor rotation lineage is invalid".to_owned(),
            ))
        }
    }
    if record.engine_release != env!("CARGO_PKG_VERSION") {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor engine release does not match this binary".to_owned(),
        ));
    }
    let libllama_revision =
        option_env!("AMW_LIBLLAMA_REV").unwrap_or("86a9c79f866799eb0e7e89c03578ccfbcc5d808e");
    if record.libllama_revision != libllama_revision {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor libllama revision does not match this binary".to_owned(),
        ));
    }
    if let Some(source_commit) = option_env!("AMW_SOURCE_COMMIT") {
        if record.source_commit != source_commit {
            return Err(RuntimeError::EvalReceiptAuthority(
                "protected trust anchor source commit does not match this binary".to_owned(),
            ));
        }
    }
    let current_exe = std::env::current_exe().map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!("running engine path is unavailable: {error}"))
    })?;
    let engine_binary_sha256 = sha256_file(&current_exe).map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!("running engine digest failed: {error}"))
    })?;
    if record.engine_binary_sha256 != engine_binary_sha256 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor engine digest does not match the running binary".to_owned(),
        ));
    }
    let public_key_spki_der =
        decode_anchor_base64url("public_key_spki_der", &record.public_key_spki_der)?;
    if URL_SAFE_NO_PAD.encode(&public_key_spki_der) != record.public_key_spki_der
        || Digest32::sha256(&public_key_spki_der) != record.key_id
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor SPKI does not match its key identity".to_owned(),
        ));
    }
    let authority_public_key_spki_der = decode_anchor_base64url(
        "authority_public_key_spki_der",
        &record.authority_public_key_spki_der,
    )?;
    if Digest32::sha256(&authority_public_key_spki_der) != expected_authority_pin
        || record.authority_key_id != expected_authority_pin
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor authority key does not match configuration".to_owned(),
        ));
    }
    let statement = canonical_receipt_anchor_statement(
        &record,
        &public_key_spki_der,
        &authority_public_key_spki_der,
    )?;
    verify_receipt_signature(
        &public_key_spki_der,
        &statement,
        &record.proof_of_possession,
    )
    .map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor proof of possession is invalid: {error}"
        ))
    })?;
    verify_receipt_signature(
        &authority_public_key_spki_der,
        &statement,
        &record.authority_signature,
    )
    .map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor authority signature is invalid: {error}"
        ))
    })?;
    Ok(VerifiedReceiptTrustAnchor {
        record,
        anchor_sha256,
        public_key_spki_der,
        #[cfg(target_os = "linux")]
        anchor_path: anchor_path.to_path_buf(),
    })
}

fn read_receipt_anchor_bytes(path: &Path) -> Result<Vec<u8>, RuntimeError> {
    for component in path.ancestors() {
        let metadata = fs::symlink_metadata(component).map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!(
                "protected trust anchor path is unreadable: {error}"
            ))
        })?;
        if metadata.file_type().is_symlink() || receipt_path_is_reparse(&metadata) {
            return Err(RuntimeError::EvalReceiptAuthority(
                "protected trust anchor path traverses a link or reparse point".to_owned(),
            ));
        }
    }
    let before = fs::metadata(path).map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!("protected trust anchor is unreadable: {error}"))
    })?;
    if !before.is_file()
        || before.len() == 0
        || before.len() > u64::try_from(MAX_RECEIPT_ANCHOR_BYTES).expect("anchor cap fits u64")
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor has an invalid bounded size".to_owned(),
        ));
    }
    let mut file = fs::File::open(path).map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!("protected trust anchor is unreadable: {error}"))
    })?;
    let mut bytes = Vec::with_capacity(before.len() as usize);
    file.by_ref()
        .take(u64::try_from(MAX_RECEIPT_ANCHOR_BYTES + 1).expect("anchor cap fits u64"))
        .read_to_end(&mut bytes)
        .map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!(
                "protected trust anchor read failed: {error}"
            ))
        })?;
    let after = file.metadata().map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor metadata failed: {error}"
        ))
    })?;
    if bytes.len() != before.len() as usize || before.len() != after.len() {
        return Err(RuntimeError::EvalReceiptAuthority(
            "protected trust anchor changed while it was read".to_owned(),
        ));
    }
    Ok(bytes)
}

#[cfg(windows)]
fn receipt_path_is_reparse(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt as _;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
const fn receipt_path_is_reparse(_metadata: &fs::Metadata) -> bool {
    false
}

fn decode_anchor_base64url(field: &'static str, value: &str) -> Result<Vec<u8>, RuntimeError> {
    if value.is_empty() || value.contains('=') {
        return Err(RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor {field} is not unpadded base64url"
        )));
    }
    let decoded = URL_SAFE_NO_PAD.decode(value).map_err(|error| {
        RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor {field} is invalid: {error}"
        ))
    })?;
    if URL_SAFE_NO_PAD.encode(&decoded) != value {
        return Err(RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor {field} is not canonical base64url"
        )));
    }
    Ok(decoded)
}

fn valid_rfc3339_utc(value: &str) -> bool {
    let bytes = value.as_bytes();
    let base = bytes.len() >= 20
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[10] == b'T'
        && bytes[13] == b':'
        && bytes[16] == b':'
        && bytes.last() == Some(&b'Z')
        && bytes[..19]
            .iter()
            .enumerate()
            .all(|(index, byte)| matches!(index, 4 | 7 | 10 | 13 | 16) || byte.is_ascii_digit());
    if !base {
        return false;
    }
    match bytes.len() {
        20 => true,
        22..=30 if bytes[19] == b'.' => bytes[20..bytes.len() - 1].iter().all(u8::is_ascii_digit),
        _ => false,
    }
}

fn canonical_receipt_anchor_statement(
    record: &ReceiptTrustAnchor,
    public_key_spki_der: &[u8],
    authority_public_key_spki_der: &[u8],
) -> Result<Vec<u8>, RuntimeError> {
    let mut statement = Vec::with_capacity(2_048);
    statement.extend_from_slice(RECEIPT_ANCHOR_V2_DOMAIN);
    let predecessor_key = record
        .predecessor_key_id
        .map_or([0_u8; 32], |digest| *digest.as_bytes());
    let predecessor_anchor = record
        .predecessor_anchor_sha256
        .map_or([0_u8; 32], |digest| *digest.as_bytes());
    let predecessor_epoch = record.predecessor_key_epoch.unwrap_or(0);
    let schema_version = record.schema_version.to_be_bytes();
    let key_epoch = record.key_epoch.to_be_bytes();
    let predecessor_epoch = predecessor_epoch.to_be_bytes();
    let provider = signer_provider_name(record.provider);
    let trust = signer_trust_name(record.trust);
    let authenticode = record.authenticode_signer_identity.as_deref().unwrap_or("");
    let values: [&[u8]; 21] = [
        &schema_version,
        record.installation_id.as_bytes(),
        record.key_id.as_bytes(),
        &key_epoch,
        record.algorithm.as_bytes(),
        public_key_spki_der,
        provider.as_bytes(),
        trust.as_bytes(),
        record.service_identity.as_bytes(),
        record.engine_release.as_bytes(),
        record.source_commit.as_bytes(),
        record.libllama_revision.as_bytes(),
        record.release_manifest_sha256.as_bytes(),
        record.engine_binary_sha256.as_bytes(),
        authenticode.as_bytes(),
        record.created_at.as_bytes(),
        &predecessor_key,
        &predecessor_epoch,
        &predecessor_anchor,
        record.authority_key_id.as_bytes(),
        authority_public_key_spki_der,
    ];
    for (index, value) in values.into_iter().enumerate() {
        let tag = u16::try_from(index + 1).expect("anchor tag count fits u16");
        let length = u32::try_from(value.len()).map_err(|_| {
            RuntimeError::EvalReceiptAuthority(
                "protected trust anchor canonical field is too large".to_owned(),
            )
        })?;
        statement.extend_from_slice(&tag.to_be_bytes());
        statement.extend_from_slice(&length.to_be_bytes());
        statement.extend_from_slice(value);
    }
    Ok(statement)
}

const fn signer_provider_name(provider: SignerProvider) -> &'static str {
    match provider {
        SignerProvider::WindowsCngMachine => "windows_cng_machine",
        SignerProvider::Tpm => "tpm",
        SignerProvider::Pkcs11 => "pkcs11",
        SignerProvider::Hsm => "hsm",
        SignerProvider::SoftwareTest => "software_test",
    }
}

const fn signer_trust_name(trust: SignerTrust) -> &'static str {
    match trust {
        SignerTrust::ProductionProtected => "production_protected",
        SignerTrust::UntrustedSoftwareTest => "untrusted_software_test",
    }
}

fn validate_receipt_anchor_string(
    field: &'static str,
    value: &str,
    max_bytes: usize,
) -> Result<(), RuntimeError> {
    if value.is_empty()
        || value.len() > max_bytes
        || value
            .chars()
            .any(|character| character.is_control() || character == '\0')
    {
        return Err(RuntimeError::EvalReceiptAuthority(format!(
            "protected trust anchor {field} is invalid"
        )));
    }
    Ok(())
}

fn sha256_file(path: &Path) -> io::Result<Digest32> {
    let mut file = fs::File::open(path)?;
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(Digest32::from_bytes(hasher.finalize().into()))
}

fn receipt_bootstrap_challenge(anchor: &VerifiedReceiptTrustAnchor) -> Vec<u8> {
    let mut challenge = Vec::with_capacity(
        RECEIPT_BOOTSTRAP_CHALLENGE_DOMAIN.len() + 64 + anchor.record.installation_id.len(),
    );
    challenge.extend_from_slice(RECEIPT_BOOTSTRAP_CHALLENGE_DOMAIN);
    challenge.extend_from_slice(anchor.anchor_sha256.as_bytes());
    challenge.extend_from_slice(anchor.record.authority_key_id.as_bytes());
    challenge.extend_from_slice(anchor.record.installation_id.as_bytes());
    challenge
}

fn build_protected_receipt_authority<R, F>(
    config: &ReceiptConfig,
    resolver: &R,
    open_ledger: F,
) -> Result<EvalReceiptAuthority, RuntimeError>
where
    R: ProtectedSignerResolver,
    F: FnOnce(&Path, &str) -> Result<ReceiptLedger, LedgerError>,
{
    let anchor = load_verified_receipt_anchor(config)?;
    let resolved = resolver.resolve(&anchor)?;
    let signer = resolved.signer.identity();
    if signer.trust != SignerTrust::ProductionProtected
        || signer.provider != anchor.record.provider
        || signer.key_id != anchor.record.key_id
        || signer.key_epoch != anchor.record.key_epoch
        || signer.public_key_spki_der != anchor.public_key_spki_der
        || signer.anchor_sha256 != Some(anchor.anchor_sha256)
        || signer.authority_pin_sha256 != Some(anchor.record.authority_key_id)
        || resolved.service_identity != anchor.record.service_identity
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "resolved signer identity does not match the protected trust anchor".to_owned(),
        ));
    }
    resolved
        .signer
        .sign_canonical(&receipt_bootstrap_challenge(&anchor))
        .map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!(
                "protected signer proof of possession failed: {error}"
            ))
        })?;
    let ledger_path = config
        .ledger_path
        .as_deref()
        .ok_or(RuntimeError::EvalReceiptUnavailable)?;
    let ledger = Arc::new(
        open_ledger(ledger_path, &anchor.record.service_identity).map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!("protected receipt ledger failed: {error}"))
        })?,
    );
    ledger
        .bind_authority(
            &anchor.record.installation_id,
            anchor.record.authority_key_id,
        )
        .map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!(
                "protected receipt ledger identity binding failed: {error}"
            ))
        })?;
    let predecessor = match (
        anchor.record.predecessor_key_epoch,
        anchor.record.predecessor_key_id,
        anchor.record.predecessor_anchor_sha256,
    ) {
        (None, None, None) => None,
        (Some(key_epoch), Some(key_id), Some(anchor_sha256)) => Some(KeyRotationPredecessor {
            key_epoch,
            key_id,
            anchor_sha256,
        }),
        _ => {
            return Err(RuntimeError::EvalReceiptAuthority(
                "protected receipt rotation lineage is incomplete".to_owned(),
            ))
        }
    };
    ledger
        .register_key_anchor(
            anchor.record.key_epoch,
            anchor.record.key_id,
            anchor.anchor_sha256,
            predecessor,
        )
        .map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!(
                "protected receipt key history registration failed: {error}"
            ))
        })?;
    let identity = ReceiptRuntimeIdentity {
        installation_id: anchor.record.installation_id,
        anchor_sha256: anchor.anchor_sha256,
        authority_pin_sha256: anchor.record.authority_key_id,
        engine_release: anchor.record.engine_release,
        source_commit: anchor.record.source_commit,
        libllama_revision: anchor.record.libllama_revision,
        release_manifest_sha256: anchor.record.release_manifest_sha256,
        engine_binary_sha256: anchor.record.engine_binary_sha256,
    };
    EvalReceiptAuthority::new(ledger, resolved.signer, identity, resolved.service_identity)
}

#[cfg(windows)]
struct WindowsCngSigningBackend {
    provider: windows_sys::Win32::Security::Cryptography::NCRYPT_PROV_HANDLE,
    key: windows_sys::Win32::Security::Cryptography::NCRYPT_KEY_HANDLE,
    signer_provider: SignerProvider,
    key_reference: String,
    public_key_spki_der: Vec<u8>,
    service_identity: String,
    key_security: CngKeySecuritySnapshot,
    signing_lock: Mutex<()>,
}

#[cfg(any(windows, test))]
const CNG_KEY_FULL_CONTROL_MASK: u32 = 0x001f_019b;

#[cfg(any(windows, test))]
const WINDOWS_SYSTEM_SID: &str = "S-1-5-18";

#[cfg(any(windows, test))]
#[derive(Clone, Debug, Eq, PartialEq)]
struct CngKeyExplicitAllowAce {
    identity: String,
    access_mask: u32,
}

#[cfg(any(windows, test))]
#[derive(Clone, Debug, Eq, PartialEq)]
struct CngKeySecuritySnapshot {
    owner_identity: String,
    owner_defaulted: bool,
    explicit_allow_aces: Vec<CngKeyExplicitAllowAce>,
}

#[cfg(any(windows, test))]
fn validate_cng_key_security_snapshot(
    expected_service_identity: &str,
    snapshot: &CngKeySecuritySnapshot,
) -> Result<Vec<String>, RuntimeError> {
    crate::receipt::ledger::validate_dedicated_service_sid(expected_service_identity).map_err(
        |_| {
            RuntimeError::EvalReceiptAuthority(
                "CNG key service identity is not a dedicated Windows service SID".to_owned(),
            )
        },
    )?;
    if snapshot.owner_defaulted {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key owner is defaulted rather than explicitly bound".to_owned(),
        ));
    }
    if snapshot.owner_identity != expected_service_identity
        && snapshot.owner_identity != WINDOWS_SYSTEM_SID
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key owner is not the dedicated service SID or SYSTEM".to_owned(),
        ));
    }
    if snapshot.explicit_allow_aces.len() != 2
        || snapshot
            .explicit_allow_aces
            .iter()
            .any(|ace| ace.access_mask != CNG_KEY_FULL_CONTROL_MASK)
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key DACL must contain exactly two canonical full-control entries".to_owned(),
        ));
    }
    let mut identities = snapshot
        .explicit_allow_aces
        .iter()
        .map(|ace| ace.identity.clone())
        .collect::<Vec<_>>();
    identities.sort();
    identities.dedup();
    let mut expected = vec![
        expected_service_identity.to_owned(),
        WINDOWS_SYSTEM_SID.to_owned(),
    ];
    expected.sort();
    if identities != expected {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key DACL identities are not exactly the service SID and SYSTEM".to_owned(),
        ));
    }
    Ok(identities)
}

#[cfg(any(windows, test))]
fn validate_cng_private_key_export_policy(export_policy: u32) -> Result<(), RuntimeError> {
    if export_policy != 0 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG receipt key permits private-key export or archiving".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(windows)]
impl WindowsCngSigningBackend {
    fn open(
        signer_provider: SignerProvider,
        key_reference: &str,
        expected_service_identity: &str,
    ) -> Result<Self, RuntimeError> {
        use windows_sys::Win32::Security::Cryptography::{
            NCryptFreeObject, NCryptGetProperty, NCryptOpenKey, NCryptOpenStorageProvider,
            NCRYPT_EXPORT_POLICY_PROPERTY, NCRYPT_MACHINE_KEY_FLAG, NCRYPT_SILENT_FLAG,
        };

        let provider_name = wide_string(cng_provider_name(signer_provider)?)?;
        let key_name = wide_string(key_reference)?;
        let mut provider = 0;
        // SAFETY: both UTF-16 inputs are NUL-terminated and the output points to
        // a valid handle-sized value for the duration of the call.
        let open_provider =
            unsafe { NCryptOpenStorageProvider(&mut provider, provider_name.as_ptr(), 0) };
        if open_provider != 0 {
            return Err(cng_runtime_error(
                "storage provider unavailable",
                open_provider,
            ));
        }
        let mut key = 0;
        // SAFETY: provider is a live NCrypt handle, key_name is NUL-terminated,
        // and the output pointer remains valid for the call.
        let open_key = unsafe {
            NCryptOpenKey(
                provider,
                &mut key,
                key_name.as_ptr(),
                0,
                NCRYPT_MACHINE_KEY_FLAG | NCRYPT_SILENT_FLAG,
            )
        };
        if open_key != 0 {
            // SAFETY: provider was returned by NCryptOpenStorageProvider and is
            // not used after this release.
            unsafe { NCryptFreeObject(provider) };
            return Err(cng_runtime_error("machine key unavailable", open_key));
        }
        let mut export_policy = 0_u32;
        let mut bytes_written = 0_u32;
        // SAFETY: key is live and the output buffer is exactly one u32.
        let property = unsafe {
            NCryptGetProperty(
                key,
                NCRYPT_EXPORT_POLICY_PROPERTY,
                (&mut export_policy as *mut u32).cast::<u8>(),
                u32::try_from(std::mem::size_of::<u32>()).expect("u32 property size fits u32"),
                &mut bytes_written,
                0,
            )
        };
        if property != 0
            || bytes_written != u32::try_from(std::mem::size_of::<u32>()).unwrap_or(4)
            || validate_cng_private_key_export_policy(export_policy).is_err()
        {
            // SAFETY: both handles are live and are not used after release.
            unsafe {
                NCryptFreeObject(key);
                NCryptFreeObject(provider);
            }
            return Err(RuntimeError::EvalReceiptAuthority(
                "CNG receipt key is exportable or its export policy is unreadable".to_owned(),
            ));
        }
        let attested = (|| {
            let public_key_spki_der = cng_public_key_spki(key)?;
            let service_identity = current_process_service_sid(expected_service_identity)?;
            let key_security = cng_key_security_snapshot(key, &service_identity)?;
            Ok::<_, RuntimeError>((public_key_spki_der, service_identity, key_security))
        })();
        let (public_key_spki_der, service_identity, key_security) = match attested {
            Ok(attested) => attested,
            Err(error) => {
                // SAFETY: both handles are live and are not used after release.
                unsafe {
                    NCryptFreeObject(key);
                    NCryptFreeObject(provider);
                }
                return Err(error);
            }
        };
        Ok(Self {
            provider,
            key,
            signer_provider,
            key_reference: key_reference.to_owned(),
            public_key_spki_der,
            service_identity,
            key_security,
            signing_lock: Mutex::new(()),
        })
    }
}

#[cfg(windows)]
impl ProtectedSigningBackend for WindowsCngSigningBackend {
    fn attest_key(&self) -> Result<PlatformKeyAttestation, SignerError> {
        let service_acl_identities =
            validate_cng_key_security_snapshot(&self.service_identity, &self.key_security)
                .map_err(|_| SignerError::ProtectedServiceAclMismatch)?;
        Ok(PlatformKeyAttestation {
            provider: self.signer_provider,
            key_handle_reference: self.key_reference.clone(),
            public_key_spki_der: self.public_key_spki_der.clone(),
            export_policy: KeyExportPolicy::NonExportable,
            service_identity: self.service_identity.clone(),
            service_acl_identities,
        })
    }

    fn sign_p256_sha256_p1363(
        &self,
        key: &ProtectedKeyReference,
        message: &[u8],
    ) -> Result<[u8; 64], SignerError> {
        use windows_sys::Win32::Security::Cryptography::{NCryptSignHash, NCRYPT_SILENT_FLAG};

        if key.opaque_key_reference() != self.key_reference {
            return Err(SignerError::ProtectedProvider(
                "CNG key reference mismatch".to_owned(),
            ));
        }
        let _guard = self
            .signing_lock
            .lock()
            .map_err(|_| SignerError::ProtectedProvider("CNG signing lock poisoned".to_owned()))?;
        let digest: [u8; 32] = Sha256::digest(message).into();
        let mut required = 0_u32;
        // SAFETY: key is live, digest has the declared length, and a null output
        // buffer is the documented size-query form.
        let size_status = unsafe {
            NCryptSignHash(
                self.key,
                std::ptr::null(),
                digest.as_ptr(),
                u32::try_from(digest.len()).expect("SHA-256 length fits u32"),
                std::ptr::null_mut(),
                0,
                &mut required,
                NCRYPT_SILENT_FLAG,
            )
        };
        if size_status != 0 || required != 64 {
            return Err(cng_signer_error(
                "P-256 signature size query failed",
                size_status,
            ));
        }
        let mut signature = [0_u8; 64];
        let mut written = 0_u32;
        // SAFETY: all pointers reference live buffers with the declared lengths.
        let sign_status = unsafe {
            NCryptSignHash(
                self.key,
                std::ptr::null(),
                digest.as_ptr(),
                u32::try_from(digest.len()).expect("SHA-256 length fits u32"),
                signature.as_mut_ptr(),
                u32::try_from(signature.len()).expect("P-256 signature length fits u32"),
                &mut written,
                NCRYPT_SILENT_FLAG,
            )
        };
        if sign_status != 0 || written != 64 {
            return Err(cng_signer_error("P-256 signing failed", sign_status));
        }
        Ok(signature)
    }
}

#[cfg(windows)]
impl Drop for WindowsCngSigningBackend {
    fn drop(&mut self) {
        use windows_sys::Win32::Security::Cryptography::NCryptFreeObject;

        // SAFETY: these handles were opened by this value and are released once.
        unsafe {
            NCryptFreeObject(self.key);
            NCryptFreeObject(self.provider);
        }
    }
}

#[cfg(windows)]
fn cng_provider_name(provider: SignerProvider) -> Result<&'static str, RuntimeError> {
    match provider {
        SignerProvider::WindowsCngMachine => Ok("Microsoft Software Key Storage Provider"),
        SignerProvider::Tpm => Ok("Microsoft Platform Crypto Provider"),
        _ => Err(RuntimeError::EvalReceiptAuthority(
            "configured protected signer provider is not a Windows CNG provider".to_owned(),
        )),
    }
}

#[cfg(windows)]
fn cng_public_key_spki(
    key: windows_sys::Win32::Security::Cryptography::NCRYPT_KEY_HANDLE,
) -> Result<Vec<u8>, RuntimeError> {
    use p256::{pkcs8::EncodePublicKey as _, PublicKey};
    use windows_sys::Win32::Security::Cryptography::{
        NCryptExportKey, BCRYPT_ECCPUBLIC_BLOB, BCRYPT_ECDSA_PUBLIC_P256_MAGIC,
    };

    let mut required = 0_u32;
    // SAFETY: key is live and a null output is the documented size query.
    let sizing = unsafe {
        NCryptExportKey(
            key,
            0,
            BCRYPT_ECCPUBLIC_BLOB,
            std::ptr::null(),
            std::ptr::null_mut(),
            0,
            &mut required,
            0,
        )
    };
    if sizing != 0 || required != 72 {
        return Err(cng_runtime_error("P-256 public key query failed", sizing));
    }
    let mut blob = vec![0_u8; required as usize];
    let mut written = 0_u32;
    // SAFETY: blob has the declared writable size and key remains live.
    let exported = unsafe {
        NCryptExportKey(
            key,
            0,
            BCRYPT_ECCPUBLIC_BLOB,
            std::ptr::null(),
            blob.as_mut_ptr(),
            required,
            &mut written,
            0,
        )
    };
    if exported != 0 || written != required {
        return Err(cng_runtime_error(
            "P-256 public key export failed",
            exported,
        ));
    }
    let magic = u32::from_le_bytes(blob[0..4].try_into().expect("CNG blob has four-byte magic"));
    let coordinate_bytes =
        u32::from_le_bytes(blob[4..8].try_into().expect("CNG blob has four-byte size"));
    if magic != BCRYPT_ECDSA_PUBLIC_P256_MAGIC || coordinate_bytes != 32 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG receipt key is not ECDSA P-256".to_owned(),
        ));
    }
    let mut sec1 = [0_u8; 65];
    sec1[0] = 4;
    sec1[1..].copy_from_slice(&blob[8..72]);
    let public_key = PublicKey::from_sec1_bytes(&sec1).map_err(|_| {
        RuntimeError::EvalReceiptAuthority("CNG receipt public key is invalid".to_owned())
    })?;
    public_key
        .to_public_key_der()
        .map(|document| document.as_bytes().to_vec())
        .map_err(|error| {
            RuntimeError::EvalReceiptAuthority(format!(
                "CNG receipt public key encoding failed: {error}"
            ))
        })
}

#[cfg(windows)]
fn cng_key_security_snapshot(
    key: windows_sys::Win32::Security::Cryptography::NCRYPT_KEY_HANDLE,
    expected_service_identity: &str,
) -> Result<CngKeySecuritySnapshot, RuntimeError> {
    use std::{ffi::c_void, mem::size_of};
    use windows_sys::Win32::{
        Security::Cryptography::{NCryptGetProperty, NCRYPT_SECURITY_DESCR_PROPERTY},
        Security::{
            GetAce, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
            GetSecurityDescriptorOwner, ACCESS_ALLOWED_ACE, DACL_SECURITY_INFORMATION,
            OWNER_SECURITY_INFORMATION, SE_DACL_PROTECTED,
        },
        System::SystemServices::ACCESS_ALLOWED_ACE_TYPE,
    };

    let security_information = OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION;
    let mut required = 0_u32;
    // SAFETY: key is live and a null output requests the descriptor size.
    let _ = unsafe {
        NCryptGetProperty(
            key,
            NCRYPT_SECURITY_DESCR_PROPERTY,
            std::ptr::null_mut(),
            0,
            &mut required,
            security_information,
        )
    };
    if required == 0 || required > 64 * 1024 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key security descriptor is unavailable".to_owned(),
        ));
    }
    let mut descriptor = vec![0_u8; required as usize];
    let mut written = 0_u32;
    // SAFETY: descriptor has the declared writable size and key remains live.
    let status = unsafe {
        NCryptGetProperty(
            key,
            NCRYPT_SECURITY_DESCR_PROPERTY,
            descriptor.as_mut_ptr(),
            required,
            &mut written,
            security_information,
        )
    };
    if status != 0 || written == 0 || written > required {
        return Err(cng_runtime_error(
            "key security descriptor query failed",
            status,
        ));
    }
    let mut control = 0_u16;
    let mut revision = 0_u32;
    // SAFETY: descriptor contains a live self-relative security descriptor.
    if unsafe {
        GetSecurityDescriptorControl(
            descriptor.as_mut_ptr().cast::<c_void>(),
            &mut control,
            &mut revision,
        )
    } == 0
        || control & SE_DACL_PROTECTED == 0
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key DACL is not protected from inheritance".to_owned(),
        ));
    }
    let mut owner = std::ptr::null_mut();
    let mut owner_defaulted = 0;
    // SAFETY: descriptor contains a live self-relative security descriptor and
    // both outputs are writable for the duration of the call.
    if unsafe {
        GetSecurityDescriptorOwner(
            descriptor.as_mut_ptr().cast::<c_void>(),
            &mut owner,
            &mut owner_defaulted,
        )
    } == 0
        || owner.is_null()
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key owner is absent or unreadable".to_owned(),
        ));
    }
    let owner_identity = sid_to_string(owner)?;
    let mut present = 0;
    let mut dacl = std::ptr::null_mut();
    let mut defaulted = 0;
    // SAFETY: descriptor contains a live self-relative security descriptor.
    if unsafe {
        GetSecurityDescriptorDacl(
            descriptor.as_mut_ptr().cast::<c_void>(),
            &mut present,
            &mut dacl,
            &mut defaulted,
        )
    } == 0
        || present == 0
        || defaulted != 0
        || dacl.is_null()
    {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key DACL is absent or defaulted".to_owned(),
        ));
    }
    // SAFETY: dacl points inside the live descriptor and has a valid ACL header.
    let ace_count = unsafe { (*dacl).AceCount };
    if ace_count != 2 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG key DACL must contain exactly service and SYSTEM entries".to_owned(),
        ));
    }
    let mut explicit_allow_aces = Vec::with_capacity(2);
    for index in 0..u32::from(ace_count) {
        let mut raw_ace = std::ptr::null_mut();
        // SAFETY: index is bounded by AceCount and output storage is valid.
        if unsafe { GetAce(dacl, index, &mut raw_ace) } == 0 {
            return Err(RuntimeError::EvalReceiptAuthority(
                "CNG key DACL entry is unreadable".to_owned(),
            ));
        }
        let ace = raw_ace.cast::<ACCESS_ALLOWED_ACE>();
        // SAFETY: GetAce returned a live ACE header inside descriptor.
        let header = unsafe { (*ace).Header };
        if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE
            || header.AceFlags != 0
            || usize::from(header.AceSize) < size_of::<ACCESS_ALLOWED_ACE>()
        {
            return Err(RuntimeError::EvalReceiptAuthority(
                "CNG key DACL contains a non-canonical explicit allow entry".to_owned(),
            ));
        }
        // SAFETY: the checked allow ACE has ACCESS_ALLOWED_ACE layout.
        let sid = unsafe {
            std::ptr::addr_of!((*ace).SidStart)
                .cast_mut()
                .cast::<c_void>()
        };
        // SAFETY: the checked allow ACE has ACCESS_ALLOWED_ACE layout.
        let access_mask = unsafe { (*ace).Mask };
        explicit_allow_aces.push(CngKeyExplicitAllowAce {
            identity: sid_to_string(sid)?,
            access_mask,
        });
    }
    let snapshot = CngKeySecuritySnapshot {
        owner_identity,
        owner_defaulted: owner_defaulted != 0,
        explicit_allow_aces,
    };
    validate_cng_key_security_snapshot(expected_service_identity, &snapshot)?;
    Ok(snapshot)
}

#[cfg(any(windows, test))]
fn validate_windows_service_token(
    expected_service_identity: &str,
    token_user: &str,
    token_groups: &[(String, u32)],
) -> Result<String, RuntimeError> {
    crate::receipt::ledger::validate_service_token_snapshot(
        expected_service_identity,
        token_user,
        token_groups,
    )
    .map_err(|_| {
        RuntimeError::EvalReceiptAuthority(
            "receipt service SID is not one enabled non-deny-only process token group".to_owned(),
        )
    })?;
    Ok(expected_service_identity.to_owned())
}

#[cfg(windows)]
fn current_process_service_sid(expected_service_identity: &str) -> Result<String, RuntimeError> {
    use std::{ffi::c_void, mem::size_of};
    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError, ERROR_INSUFFICIENT_BUFFER},
        Security::{
            GetTokenInformation, TokenGroups, TokenUser, TOKEN_GROUPS, TOKEN_INFORMATION_CLASS,
            TOKEN_QUERY, TOKEN_USER,
        },
        System::Threading::{GetCurrentProcess, OpenProcessToken},
    };

    fn information_buffer(
        token: windows_sys::Win32::Foundation::HANDLE,
        information_class: TOKEN_INFORMATION_CLASS,
    ) -> Result<Vec<usize>, RuntimeError> {
        let mut required = 0_u32;
        // SAFETY: this is the documented sizing query for a live token handle.
        let sizing = unsafe {
            GetTokenInformation(
                token,
                information_class,
                std::ptr::null_mut(),
                0,
                &mut required,
            )
        };
        // SAFETY: reads the last-error state immediately after the sizing query.
        let error = unsafe { GetLastError() };
        if sizing != 0 || error != ERROR_INSUFFICIENT_BUFFER || required == 0 {
            return Err(RuntimeError::EvalReceiptAuthority(
                "current process token identity query failed".to_owned(),
            ));
        }
        let mut storage = vec![0_usize; (required as usize).div_ceil(size_of::<usize>())];
        // SAFETY: storage is aligned and spans the size returned by the sizing query.
        if unsafe {
            GetTokenInformation(
                token,
                information_class,
                storage.as_mut_ptr().cast::<c_void>(),
                required,
                &mut required,
            )
        } == 0
        {
            return Err(RuntimeError::EvalReceiptAuthority(
                "current process token identity query failed".to_owned(),
            ));
        }
        Ok(storage)
    }

    let mut token = std::ptr::null_mut();
    // SAFETY: current process is always a live pseudo-handle and token is writable.
    if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "current process service identity is unavailable".to_owned(),
        ));
    }
    struct OwnedToken(windows_sys::Win32::Foundation::HANDLE);
    impl Drop for OwnedToken {
        fn drop(&mut self) {
            // SAFETY: this value owns the process token handle exactly once.
            unsafe { CloseHandle(self.0) };
        }
    }
    let token = OwnedToken(token);
    let user_storage = information_buffer(token.0, TokenUser)?;
    if user_storage.len() * size_of::<usize>() < size_of::<TOKEN_USER>() {
        return Err(RuntimeError::EvalReceiptAuthority(
            "current process TokenUser record is truncated".to_owned(),
        ));
    }
    // SAFETY: the buffer is aligned and was populated for TokenUser.
    let token_user = unsafe { &*user_storage.as_ptr().cast::<TOKEN_USER>() };
    let user_sid = sid_to_string(token_user.User.Sid)?;

    let group_storage = information_buffer(token.0, TokenGroups)?;
    let group_bytes = group_storage.len() * size_of::<usize>();
    let group_offset = std::mem::offset_of!(TOKEN_GROUPS, Groups);
    if group_bytes < group_offset {
        return Err(RuntimeError::EvalReceiptAuthority(
            "current process TokenGroups record is truncated".to_owned(),
        ));
    }
    // SAFETY: the buffer is aligned and was populated for TokenGroups.
    let token_groups = unsafe { &*group_storage.as_ptr().cast::<TOKEN_GROUPS>() };
    let group_count = usize::try_from(token_groups.GroupCount).map_err(|_| {
        RuntimeError::EvalReceiptAuthority("process token group count overflowed".to_owned())
    })?;
    let available_groups = (group_bytes - group_offset)
        / size_of::<windows_sys::Win32::Security::SID_AND_ATTRIBUTES>();
    if group_count > available_groups {
        return Err(RuntimeError::EvalReceiptAuthority(
            "current process TokenGroups record is truncated".to_owned(),
        ));
    }
    // SAFETY: group_count is bounded by the variable-length array bytes above.
    let groups = unsafe {
        std::slice::from_raw_parts(token_groups.Groups.as_ptr(), group_count)
            .iter()
            .map(|group| Ok((sid_to_string(group.Sid)?, group.Attributes)))
            .collect::<Result<Vec<_>, RuntimeError>>()?
    };
    validate_windows_service_token(expected_service_identity, &user_sid, &groups)
}

#[cfg(windows)]
fn sid_to_string(sid: windows_sys::Win32::Security::PSID) -> Result<String, RuntimeError> {
    use std::ffi::c_void;
    use windows_sys::Win32::{
        Foundation::LocalFree, Security::Authorization::ConvertSidToStringSidW,
    };

    let mut text = std::ptr::null_mut();
    // SAFETY: sid points into a live token or descriptor and text is writable.
    if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
        return Err(RuntimeError::EvalReceiptAuthority(
            "Windows service SID conversion failed".to_owned(),
        ));
    }
    let mut length = 0;
    // SAFETY: conversion returned a live NUL-terminated UTF-16 allocation.
    while unsafe { *text.add(length) } != 0 {
        length += 1;
    }
    // SAFETY: length was measured within the live allocation.
    let result = String::from_utf16(unsafe { std::slice::from_raw_parts(text, length) })
        .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()));
    // SAFETY: ConvertSidToStringSidW allocated text with LocalAlloc.
    unsafe { LocalFree(text.cast::<c_void>()) };
    result
}

#[cfg(windows)]
fn wide_string(value: &str) -> Result<Vec<u16>, RuntimeError> {
    if value.contains('\0') {
        return Err(RuntimeError::EvalReceiptAuthority(
            "CNG provider identity contains an embedded NUL".to_owned(),
        ));
    }
    Ok(value.encode_utf16().chain(std::iter::once(0)).collect())
}

#[cfg(windows)]
fn cng_runtime_error(context: &'static str, status: i32) -> RuntimeError {
    RuntimeError::EvalReceiptAuthority(format!("CNG {context} (status 0x{status:08x})"))
}

#[cfg(windows)]
fn cng_signer_error(context: &'static str, status: i32) -> SignerError {
    SignerError::ProtectedProvider(format!("CNG {context} (status 0x{status:08x})"))
}

#[cfg(windows)]
fn resolve_platform_protected_signer(
    anchor: &VerifiedReceiptTrustAnchor,
) -> Result<ResolvedProtectedSigner, RuntimeError> {
    if anchor.record.provider != SignerProvider::WindowsCngMachine {
        return Err(RuntimeError::EvalReceiptAuthority(
            "configured protected signer provider has no platform resolver".to_owned(),
        ));
    }
    let key_handle_reference = format!("Vetinari.AMEngine.Receipt.{}", anchor.record.key_id);
    let backend = WindowsCngSigningBackend::open(
        anchor.record.provider,
        &key_handle_reference,
        &anchor.record.service_identity,
    )?;
    let binding = ProtectedSignerBinding {
        provider: anchor.record.provider,
        key_epoch: anchor.record.key_epoch,
        public_key_spki_der: anchor.public_key_spki_der.clone(),
        key_handle_reference,
        service_identity: anchor.record.service_identity.clone(),
        anchor_sha256: anchor.anchor_sha256,
        authority_pin_sha256: anchor.record.authority_key_id,
    };
    let signer = ProtectedReceiptSigner::from_attested_backend(binding, backend)
        .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
    Ok(ResolvedProtectedSigner {
        signer: Arc::new(signer),
        service_identity: anchor.record.service_identity.clone(),
    })
}

#[cfg(target_os = "linux")]
fn resolve_platform_protected_signer(
    anchor: &VerifiedReceiptTrustAnchor,
) -> Result<ResolvedProtectedSigner, RuntimeError> {
    if !matches!(
        anchor.record.provider,
        SignerProvider::Pkcs11 | SignerProvider::Hsm
    ) {
        return Err(RuntimeError::EvalReceiptAuthority(
            "configured protected signer provider is not available through Linux PKCS#11"
                .to_owned(),
        ));
    }
    let resolved = crate::receipt::pkcs11::resolve_linux_pkcs11_signer(
        &anchor.anchor_path,
        &anchor.record.installation_id,
        anchor.record.provider,
        anchor.record.key_epoch,
        &anchor.public_key_spki_der,
        &anchor.record.service_identity,
        anchor.anchor_sha256,
        anchor.record.authority_key_id,
    )
    .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
    Ok(ResolvedProtectedSigner {
        signer: resolved.signer,
        service_identity: resolved.service_identity,
    })
}

#[cfg(not(any(windows, target_os = "linux")))]
fn resolve_platform_protected_signer(
    _anchor: &VerifiedReceiptTrustAnchor,
) -> Result<ResolvedProtectedSigner, RuntimeError> {
    Err(RuntimeError::EvalReceiptAuthority(
        "configured protected signer provider has no platform resolver on this host".to_owned(),
    ))
}

/// Complete protected receipt identity reported for supervisor cross-checking.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct ReceiptTrustReport {
    pub installation_id: String,
    pub anchor_sha256: Digest32,
    pub key_id: Digest32,
    pub key_epoch: u64,
    pub algorithm: &'static str,
    pub provider: SignerProvider,
    pub service_identity: String,
    pub engine_release: String,
    pub source_commit: String,
    pub libllama_revision: String,
    pub release_manifest_sha256: Digest32,
    pub engine_binary_sha256: Digest32,
    pub engine_instance_id: String,
}

/// Durable receipt authority injected only by the protected platform host.
#[derive(Clone)]
pub struct EvalReceiptAuthority {
    ledger: Arc<ReceiptLedger>,
    signer: Arc<dyn ReceiptSigner>,
    identity: ReceiptRuntimeIdentity,
    service_identity: String,
}

impl EvalReceiptAuthority {
    /// Binds a durable ledger and signer to an externally verified installation identity.
    ///
    /// Software signers remain explicitly untrusted in their receipt envelope and
    /// are accepted only so contract tests can exercise the terminal protocol.
    pub fn new(
        ledger: Arc<ReceiptLedger>,
        signer: Arc<dyn ReceiptSigner>,
        identity: ReceiptRuntimeIdentity,
        service_identity: String,
    ) -> Result<Self, RuntimeError> {
        if service_identity.is_empty() {
            return Err(RuntimeError::EvalReceiptUnavailable);
        }
        let signer_identity = signer.identity();
        if signer_identity.trust == SignerTrust::ProductionProtected
            && (signer_identity.anchor_sha256 != Some(identity.anchor_sha256)
                || signer_identity.authority_pin_sha256 != Some(identity.authority_pin_sha256))
        {
            return Err(RuntimeError::EvalReceiptUnavailable);
        }
        Ok(Self {
            ledger,
            signer,
            identity,
            service_identity,
        })
    }

    fn trust_report(&self, engine_instance_id: &str) -> Option<ReceiptTrustReport> {
        let signer = self.signer.identity();
        (signer.trust == SignerTrust::ProductionProtected).then(|| ReceiptTrustReport {
            installation_id: self.identity.installation_id.clone(),
            anchor_sha256: self.identity.anchor_sha256,
            key_id: signer.key_id,
            key_epoch: signer.key_epoch,
            algorithm: SIGNATURE_ALGORITHM,
            provider: signer.provider,
            service_identity: self.service_identity.clone(),
            engine_release: self.identity.engine_release.clone(),
            source_commit: self.identity.source_commit.clone(),
            libllama_revision: self.identity.libllama_revision.clone(),
            release_manifest_sha256: self.identity.release_manifest_sha256,
            engine_binary_sha256: self.identity.engine_binary_sha256,
            engine_instance_id: engine_instance_id.to_owned(),
        })
    }
}

#[cfg(all(feature = "contract-test-controls", debug_assertions))]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ContractProducerFailure {
    BackendUnavailable,
    AllocationFailed,
    QueueFull,
    EvalTimeout,
    QuotaExhausted,
    Cancelled,
    Internal,
    SessionUnknown,
    ModelCorrupt,
    ModelNotLoaded,
}

#[cfg(all(feature = "contract-test-controls", debug_assertions))]
impl ContractProducerFailure {
    pub(crate) fn from_name(name: &str) -> Option<Self> {
        match name {
            "backend_unavailable" => Some(Self::BackendUnavailable),
            "allocation_failed" => Some(Self::AllocationFailed),
            "queue_full" => Some(Self::QueueFull),
            "eval_timeout" => Some(Self::EvalTimeout),
            "quota_exhausted" => Some(Self::QuotaExhausted),
            "cancelled" => Some(Self::Cancelled),
            "internal" => Some(Self::Internal),
            "session_unknown" => Some(Self::SessionUnknown),
            "model_corrupt" => Some(Self::ModelCorrupt),
            "model_not_loaded" => Some(Self::ModelNotLoaded),
            _ => None,
        }
    }
}

#[cfg(all(
    feature = "contract-test-controls",
    debug_assertions,
    any(feature = "cpu", feature = "cuda")
))]
const CONTRACT_PRODUCER_SECRET: &str = r"C:\private\native\contract-secret-native-detail.gguf";

/// API-facing handle around the authoritative byte-bounded generation receiver.
pub struct GenerationStream {
    request_id: String,
    trace_id: String,
    model: String,
    receiver: GenerationReceiver,
    pending_receipt: Option<PendingEvalReceipt>,
    emitted_output: Vec<u8>,
    engine_receipt: Option<SignedEvalReceipt>,
    receipt_error: Option<RuntimeError>,
    #[cfg(test)]
    _receipt_test_dir: Option<Arc<tempfile::TempDir>>,
}

#[cfg(test)]
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ReceiptTerminalTestFailure {
    Signer,
    Ledger,
}

#[cfg(test)]
struct InjectedFailingReceiptSigner {
    identity: crate::receipt::SignerIdentity,
}

#[cfg(test)]
impl ReceiptSigner for InjectedFailingReceiptSigner {
    fn identity(&self) -> &crate::receipt::SignerIdentity {
        &self.identity
    }

    fn sign_canonical(&self, _canonical: &[u8]) -> Result<[u8; 64], SignerError> {
        Err(SignerError::ProtectedProvider(
            "injected terminal signer failure".to_owned(),
        ))
    }
}

#[cfg(test)]
pub(crate) fn receipt_terminal_test_stream(
    failure: ReceiptTerminalTestFailure,
) -> (GenerationStream, Arc<ReceiptLedger>) {
    let directory = Arc::new(tempfile::tempdir().expect("temporary receipt test directory"));
    let ledger_path = directory.path().join("terminal-receipt.sqlite3");
    let ledger =
        Arc::new(ReceiptLedger::open_for_test(&ledger_path).expect("terminal test ledger opens"));
    let software = crate::receipt::SoftwareTestSigner::from_secret_bytes([23; 32], 1)
        .expect("terminal test signer identity");
    let signer: Arc<dyn ReceiptSigner> = match failure {
        ReceiptTerminalTestFailure::Signer => Arc::new(InjectedFailingReceiptSigner {
            identity: software.identity().clone(),
        }),
        ReceiptTerminalTestFailure::Ledger => Arc::new(software),
    };
    let authority = EvalReceiptAuthority::new(
        Arc::clone(&ledger),
        signer,
        ReceiptRuntimeIdentity {
            installation_id: "terminal-failure-installation".to_owned(),
            anchor_sha256: Digest32::sha256(b"terminal-anchor"),
            authority_pin_sha256: Digest32::sha256(b"terminal-authority"),
            engine_release: "terminal-test-release".to_owned(),
            source_commit: "terminal-test-source".to_owned(),
            libllama_revision: "terminal-test-libllama".to_owned(),
            release_manifest_sha256: Digest32::sha256(b"terminal-manifest"),
            engine_binary_sha256: Digest32::sha256(b"terminal-binary"),
        },
        "untrusted-terminal-test".to_owned(),
    )
    .expect("terminal test authority binds");
    let request_id = match failure {
        ReceiptTerminalTestFailure::Signer => "terminal-signer-failure",
        ReceiptTerminalTestFailure::Ledger => "terminal-ledger-failure",
    };
    let eval_context = crate::receipt::EvalContext {
        schema_version: 1,
        run_id: "terminal-run".to_owned(),
        suite_id: "terminal-suite".to_owned(),
        suite_revision_sha256: Digest32::sha256(b"terminal-suite"),
        case_id: request_id.to_owned(),
        ordinal: 0,
        case_spec_sha256: Digest32::sha256(request_id.as_bytes()),
    };
    let request = GenerateRequest {
        request_id: request_id.to_owned(),
        trace_id: format!("{request_id}-trace"),
        principal_id: "terminal-principal".to_owned(),
        model: Some("terminal-model".to_owned()),
        prompt: "terminal prompt".to_owned(),
        infill_suffix: None,
        max_tokens: 1,
        stop: Vec::new(),
        sampling: SamplerParams {
            seed: 29,
            ..SamplerParams::default()
        },
        grammar: None,
        priority: PriorityClass::Eval,
        role: WorkloadRole::Worker,
        eval_slot: Some(0),
        eval_context: Some(eval_context.clone()),
        endpoint: "/v1/completions".to_owned(),
        original_messages: Vec::new(),
        session_id: None,
        prefix_refs: Vec::new(),
        deadline: Instant::now() + Duration::from_secs(30),
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        contract_failure: None,
    };
    let reservation = ledger
        .reserve_attempt(
            request_id,
            &AttemptIdentity {
                installation_id: authority.identity.installation_id.clone(),
                run_id: eval_context.run_id,
                suite_id: eval_context.suite_id,
                case_id: eval_context.case_id,
                ordinal: eval_context.ordinal,
            },
        )
        .expect("terminal test attempt reserves");
    let (identity_sender, identity_receiver) = oneshot::channel();
    identity_sender
        .send(Ok(ReceiptExecutionIdentity {
            model_id: "terminal-model".to_owned(),
            model_sha256: Digest32::sha256(b"terminal-model"),
            adapter_set_sha256: crate::receipt::absent_sha256(
                crate::receipt::AbsentDigestField::AdapterSet,
            ),
            template_sha256: crate::receipt::absent_sha256(
                crate::receipt::AbsentDigestField::Template,
            ),
            system_messages_sha256: crate::receipt::absent_sha256(
                crate::receipt::AbsentDigestField::SystemMessages,
            ),
            grammar_sha256: crate::receipt::absent_sha256(
                crate::receipt::AbsentDigestField::Grammar,
            ),
            sampler_sha256: Digest32::from_bytes(request.sampling.identity_sha256()),
            generation_control_sha256: Digest32::sha256(b"terminal-generation-control"),
            original_messages_sha256: crate::receipt::original_messages_sha256(&[])
                .expect("terminal message digest"),
            rendered_prompt_sha256: Digest32::sha256(request.prompt.as_bytes()),
        }))
        .expect("terminal identity receiver remains open");
    if failure == ReceiptTerminalTestFailure::Ledger {
        let tamper =
            rusqlite::Connection::open(&ledger_path).expect("terminal ledger tamper opens");
        tamper
            .execute_batch("DROP TABLE eval_receipt_attempts;")
            .expect("terminal ledger table removal");
    }
    let (sender, receiver) = bounded_generation_stream(GenerationControl::default());
    sender
        .try_send(GenerationEvent::Delta {
            token_id: 31,
            bytes: b"terminal-output".to_vec(),
            logprob: None,
            top_logprobs: Vec::new(),
        })
        .expect("terminal delta enqueues");
    sender
        .try_send(GenerationEvent::Finished {
            reason: crate::gen::StopReason::MaxTokens,
            usage: crate::gen::GenerationUsage {
                prompt_tokens: 2,
                completion_tokens: 1,
            },
            confidence: None,
        })
        .expect("terminal finish enqueues");
    (
        GenerationStream {
            request_id: request.request_id.clone(),
            trace_id: request.trace_id.clone(),
            model: "terminal-model".to_owned(),
            receiver,
            pending_receipt: Some(PendingEvalReceipt {
                authority,
                reservation,
                request,
                engine_instance_id: "terminal-engine-instance".to_owned(),
                execution_identity: identity_receiver,
            }),
            emitted_output: Vec::new(),
            engine_receipt: None,
            receipt_error: None,
            _receipt_test_dir: Some(directory),
        },
        ledger,
    )
}

impl GenerationStream {
    /// Returns the stable request identifier assigned before scheduler submission.
    pub fn request_id(&self) -> &str {
        &self.request_id
    }

    /// Returns the trace identifier propagated to terminal telemetry.
    pub fn trace_id(&self) -> &str {
        &self.trace_id
    }

    /// Returns the canonical loaded-model identifier serving this request.
    pub fn model(&self) -> &str {
        &self.model
    }

    /// Returns the durably committed terminal receipt, when this was an EVAL request.
    pub fn engine_receipt(&self) -> Option<&SignedEvalReceipt> {
        self.engine_receipt.as_ref()
    }

    /// Takes the explicit receipt failure retained for API error mapping.
    pub fn take_receipt_error(&mut self) -> Option<RuntimeError> {
        self.receipt_error.take()
    }

    /// Receives the next authoritative P14 generation event.
    pub async fn recv(&mut self) -> Option<GenerationEvent> {
        let event = self.receiver.recv().await?;
        match &event {
            GenerationEvent::Delta { bytes, .. } if self.pending_receipt.is_some() => {
                self.emitted_output.extend_from_slice(bytes);
            }
            GenerationEvent::Finished { reason, usage, .. } if self.pending_receipt.is_some() => {
                if let Err(error) = self.commit_eval_receipt(reason, *usage).await {
                    self.receipt_error = Some(error);
                    return Some(GenerationEvent::Failed(
                        crate::gen::GenError::RuntimeFailure {
                            code: crate::gen::GenerationFailureCode::Internal,
                            message: "evaluation receipt commitment failed".to_owned(),
                        },
                    ));
                }
            }
            GenerationEvent::Delta { .. }
            | GenerationEvent::Finished { .. }
            | GenerationEvent::Failed(_) => {}
        }
        Some(event)
    }

    async fn commit_eval_receipt(
        &mut self,
        reason: &crate::gen::StopReason,
        usage: crate::gen::GenerationUsage,
    ) -> Result<(), RuntimeError> {
        let pending = self
            .pending_receipt
            .take()
            .ok_or_else(|| RuntimeError::EvalReceiptCommit("reservation is missing".to_owned()))?;
        let execution = pending.execution_identity.await.map_err(|_| {
            RuntimeError::EvalReceiptCommit("model worker dropped receipt identity".to_owned())
        })??;
        let finish_reason = receipt_finish_reason(reason)?;
        let signer = pending.authority.signer.identity();
        let context =
            pending.request.eval_context.as_ref().ok_or_else(|| {
                RuntimeError::EvalReceiptCommit("eval_context is missing".to_owned())
            })?;
        let eval_slot = pending
            .request
            .eval_slot
            .and_then(|slot| u32::try_from(slot).ok())
            .ok_or_else(|| RuntimeError::EvalReceiptCommit("eval_slot is invalid".to_owned()))?;
        let claims = EvalReceiptClaims {
            schema_version: 1,
            installation_id: pending.authority.identity.installation_id.clone(),
            anchor_sha256: pending.authority.identity.anchor_sha256,
            key_id: signer.key_id,
            key_epoch: signer.key_epoch,
            engine_release: pending.authority.identity.engine_release.clone(),
            source_commit: pending.authority.identity.source_commit.clone(),
            libllama_revision: pending.authority.identity.libllama_revision.clone(),
            release_manifest_sha256: pending.authority.identity.release_manifest_sha256,
            engine_binary_sha256: pending.authority.identity.engine_binary_sha256,
            engine_instance_id: pending.engine_instance_id,
            principal_id: pending.request.principal_id.clone(),
            request_id: pending.request.request_id.clone(),
            trace_id: pending.request.trace_id.clone(),
            endpoint: pending.request.endpoint.clone(),
            run_id: context.run_id.clone(),
            suite_id: context.suite_id.clone(),
            suite_revision_sha256: context.suite_revision_sha256,
            case_id: context.case_id.clone(),
            ordinal: context.ordinal,
            attempt_key: pending.reservation.attempt_key,
            eval_slot,
            seed: pending.request.sampling.seed,
            case_spec_sha256: context.case_spec_sha256,
            model_id: execution.model_id,
            model_sha256: execution.model_sha256,
            adapter_set_sha256: execution.adapter_set_sha256,
            template_sha256: execution.template_sha256,
            system_messages_sha256: execution.system_messages_sha256,
            grammar_sha256: execution.grammar_sha256,
            sampler_sha256: execution.sampler_sha256,
            generation_control_sha256: execution.generation_control_sha256,
            original_messages_sha256: execution.original_messages_sha256,
            rendered_prompt_sha256: execution.rendered_prompt_sha256,
            output_sha256: Digest32::sha256(&self.emitted_output),
            prompt_tokens: u64::try_from(usage.prompt_tokens).map_err(|_| {
                RuntimeError::EvalReceiptCommit("prompt token count overflowed".to_owned())
            })?,
            completion_tokens: u64::try_from(usage.completion_tokens).map_err(|_| {
                RuntimeError::EvalReceiptCommit("completion token count overflowed".to_owned())
            })?,
            finish_reason: finish_reason.to_owned(),
        };
        let receipt = pending
            .authority
            .ledger
            .commit_terminal_receipt(
                &pending.reservation,
                &claims,
                pending.authority.signer.as_ref(),
            )
            .map_err(|error| RuntimeError::EvalReceiptCommit(error.to_string()))?;
        self.engine_receipt = Some(receipt);
        Ok(())
    }
}

struct PendingEvalReceipt {
    authority: EvalReceiptAuthority,
    reservation: ReceiptReservation,
    request: GenerateRequest,
    engine_instance_id: String,
    execution_identity: oneshot::Receiver<Result<ReceiptExecutionIdentity, RuntimeError>>,
}

struct ReservedEvalReceipt {
    authority: EvalReceiptAuthority,
    reservation: ReceiptReservation,
    engine_instance_id: String,
}

#[derive(Clone, Debug)]
struct ReceiptExecutionIdentity {
    model_id: String,
    model_sha256: Digest32,
    adapter_set_sha256: Digest32,
    template_sha256: Digest32,
    system_messages_sha256: Digest32,
    grammar_sha256: Digest32,
    sampler_sha256: Digest32,
    generation_control_sha256: Digest32,
    original_messages_sha256: Digest32,
    rendered_prompt_sha256: Digest32,
}

#[cfg(any(test, feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug, Eq, PartialEq)]
enum SpeculationReceiptIdentity {
    PromptLookup,
    DraftModel {
        model_id: String,
        model_sha256: Digest32,
        minimum_context: Option<u32>,
        vocabulary_fingerprint: Digest32,
    },
}

/// Chat prompt rendered by, and cryptographically bound to, one loaded model worker.
///
/// The private worker handle prevents a model unload/reload between template rendering and
/// generation submission from silently switching the request to a different model generation.
pub struct PinnedChatPrompt {
    prompt: String,
    requested_model: String,
    handle: ModelHandle,
}

impl PinnedChatPrompt {
    /// Returns the rendered prompt for request validation at the API boundary.
    pub fn prompt(&self) -> &str {
        &self.prompt
    }
}

#[derive(Clone)]
pub struct EngineRuntime {
    inner: Arc<RuntimeInner>,
}

struct RuntimeInner {
    config: EngineConfig,
    receipts: Option<EvalReceiptAuthority>,
    engine_instance_id: String,
    catalog: Mutex<ModelRegistry>,
    loader: Mutex<ModelLoader<MonotonicClock>>,
    models: RwLock<BTreeMap<String, ModelHandle>>,
    draining: AtomicBool,
    requests: Arc<RequestRegistry>,
    request_counter: AtomicU64,
    scheduler_counter: AtomicU64,
    telemetry: TelemetryHub,
    metrics: MetricsHub,
    watchdog: Arc<Mutex<Watchdog<SystemUptimeClock>>>,
    leak_monitor: Mutex<MemoryLeakMonitor>,
    global_slots: Arc<GlobalSlotArbiter>,
    adapters: Arc<Mutex<AdapterRegistry>>,
    sessions: Arc<SessionStore>,
    watchdog_thread: Mutex<Option<std::thread::JoinHandle<()>>>,
}

#[derive(Clone)]
struct RequestRegistration {
    scheduler_id: u64,
    model_id: String,
    principal_id: String,
    control: GenerationControl,
}

#[derive(Default)]
struct RequestRegistry {
    state: Mutex<RequestRegistryState>,
}

#[derive(Default)]
struct RequestRegistryState {
    by_correlation: BTreeMap<RequestKey, RequestRegistration>,
    by_scheduler: BTreeMap<u64, RequestKey>,
}

#[derive(Clone, Debug, Eq, Ord, PartialEq, PartialOrd)]
struct RequestKey {
    principal_id: String,
    correlation_id: String,
}

impl RequestKey {
    fn new(principal_id: &str, correlation_id: &str) -> Self {
        Self {
            principal_id: principal_id.to_owned(),
            correlation_id: correlation_id.to_owned(),
        }
    }
}

#[cfg_attr(not(any(feature = "cpu", feature = "cuda")), allow(dead_code))]
struct GlobalSlotArbiter {
    limit: usize,
    active: AtomicUsize,
}

impl GlobalSlotArbiter {
    fn new(limit: usize) -> Self {
        Self {
            limit: limit.max(1),
            active: AtomicUsize::new(0),
        }
    }

    #[cfg(any(feature = "cpu", feature = "cuda"))]
    fn try_acquire(self: &Arc<Self>, dynamic_limit: usize) -> Option<GlobalSlotPermit> {
        let limit = self.limit.min(dynamic_limit.max(1));
        let mut active = self.active.load(Ordering::Acquire);
        loop {
            if active >= limit {
                return None;
            }
            match self.active.compare_exchange_weak(
                active,
                active + 1,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => {
                    return Some(GlobalSlotPermit {
                        arbiter: Arc::clone(self),
                    });
                }
                Err(observed) => active = observed,
            }
        }
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct GlobalSlotPermit {
    arbiter: Arc<GlobalSlotArbiter>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl Drop for GlobalSlotPermit {
    fn drop(&mut self) {
        self.arbiter.active.fetch_sub(1, Ordering::AcqRel);
    }
}

impl RequestRegistry {
    fn insert(
        &self,
        correlation_id: String,
        registration: RequestRegistration,
    ) -> Result<(), RuntimeError> {
        let key = RequestKey::new(&registration.principal_id, &correlation_id);
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.by_correlation.contains_key(&key)
            || state.by_scheduler.contains_key(&registration.scheduler_id)
        {
            return Err(RuntimeError::UnsupportedParam(
                "request correlation id is already active for this principal".to_owned(),
            ));
        }
        state
            .by_scheduler
            .insert(registration.scheduler_id, key.clone());
        state.by_correlation.insert(key, registration);
        Ok(())
    }

    fn registration(
        &self,
        correlation_id: &str,
        principal_id: &str,
    ) -> Option<RequestRegistration> {
        let key = RequestKey::new(principal_id, correlation_id);
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .by_correlation
            .get(&key)
            .cloned()
    }

    fn unambiguous_registration(&self, correlation_id: &str) -> Option<RequestRegistration> {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let mut matches = state
            .by_correlation
            .iter()
            .filter(|(key, _)| key.correlation_id == correlation_id)
            .map(|(_, registration)| registration);
        let registration = matches.next()?.clone();
        matches.next().is_none().then_some(registration)
    }

    fn registration_by_scheduler(&self, scheduler_id: u64) -> Option<RequestRegistration> {
        let state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let key = state.by_scheduler.get(&scheduler_id)?;
        state
            .by_correlation
            .get(key)
            .filter(|registration| registration.scheduler_id == scheduler_id)
            .cloned()
    }

    fn remove_scheduler(&self, scheduler_id: u64) -> Option<RequestRegistration> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let key = state.by_scheduler.get(&scheduler_id)?.clone();
        if state
            .by_correlation
            .get(&key)
            .is_none_or(|registration| registration.scheduler_id != scheduler_id)
        {
            return None;
        }
        let registration = state.by_correlation.remove(&key)?;
        state.by_scheduler.remove(&scheduler_id);
        Some(registration)
    }
}

#[cfg(test)]
mod control_plane_tests {
    use super::*;
    use crate::gen::GenerationControlState;
    use p256::{
        ecdsa::{signature::Signer as _, Signature, SigningKey},
        pkcs8::EncodePublicKey as _,
    };
    use std::cell::Cell;

    struct FakeProtectedBackend {
        attestation: PlatformKeyAttestation,
        signing_key: SigningKey,
    }

    impl ProtectedSigningBackend for FakeProtectedBackend {
        fn attest_key(&self) -> Result<PlatformKeyAttestation, SignerError> {
            Ok(self.attestation.clone())
        }

        fn sign_p256_sha256_p1363(
            &self,
            _key: &ProtectedKeyReference,
            message: &[u8],
        ) -> Result<[u8; 64], SignerError> {
            let signature: Signature = self.signing_key.sign(message);
            Ok(signature
                .normalize_s()
                .unwrap_or(signature)
                .to_bytes()
                .into())
        }
    }

    struct FakeProtectedResolver {
        signing_key: SigningKey,
    }

    impl ProtectedSignerResolver for FakeProtectedResolver {
        fn resolve(
            &self,
            anchor: &VerifiedReceiptTrustAnchor,
        ) -> Result<ResolvedProtectedSigner, RuntimeError> {
            let key_handle_reference =
                format!("Vetinari.AMEngine.Receipt.{}", anchor.record.key_id);
            let backend = FakeProtectedBackend {
                attestation: PlatformKeyAttestation {
                    provider: SignerProvider::WindowsCngMachine,
                    key_handle_reference: key_handle_reference.clone(),
                    public_key_spki_der: anchor.public_key_spki_der.clone(),
                    export_policy: KeyExportPolicy::NonExportable,
                    service_identity: anchor.record.service_identity.clone(),
                    service_acl_identities: vec![
                        anchor.record.service_identity.clone(),
                        "S-1-5-18".to_owned(),
                    ],
                },
                signing_key: self.signing_key.clone(),
            };
            let signer = ProtectedReceiptSigner::from_attested_backend(
                ProtectedSignerBinding {
                    provider: anchor.record.provider,
                    key_epoch: anchor.record.key_epoch,
                    public_key_spki_der: anchor.public_key_spki_der.clone(),
                    key_handle_reference,
                    service_identity: anchor.record.service_identity.clone(),
                    anchor_sha256: anchor.anchor_sha256,
                    authority_pin_sha256: anchor.record.authority_key_id,
                },
                backend,
            )
            .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
            Ok(ResolvedProtectedSigner {
                signer: Arc::new(signer),
                service_identity: anchor.record.service_identity.clone(),
            })
        }
    }

    struct SoftwareResolver;

    impl ProtectedSignerResolver for SoftwareResolver {
        fn resolve(
            &self,
            anchor: &VerifiedReceiptTrustAnchor,
        ) -> Result<ResolvedProtectedSigner, RuntimeError> {
            let signer = crate::receipt::SoftwareTestSigner::from_secret_bytes([19; 32], 1)
                .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
            Ok(ResolvedProtectedSigner {
                signer: Arc::new(signer),
                service_identity: anchor.record.service_identity.clone(),
            })
        }
    }

    struct UnavailableResolver;

    impl ProtectedSignerResolver for UnavailableResolver {
        fn resolve(
            &self,
            _anchor: &VerifiedReceiptTrustAnchor,
        ) -> Result<ResolvedProtectedSigner, RuntimeError> {
            Err(RuntimeError::EvalReceiptAuthority(
                "test protected provider unavailable".to_owned(),
            ))
        }
    }

    fn sign_anchor_statement(key: &SigningKey, statement: &[u8]) -> String {
        let signature: Signature = key.sign(statement);
        let normalized = signature.normalize_s().unwrap_or(signature);
        URL_SAFE_NO_PAD.encode(normalized.to_bytes())
    }

    fn provisioned_server_config(
        temp: &tempfile::TempDir,
    ) -> (EngineConfig, SigningKey, SigningKey, ReceiptTrustAnchor) {
        let model_dir = temp.path().join("models");
        fs::create_dir_all(&model_dir).expect("model directory must be created");
        let engine_key = SigningKey::from_bytes((&[7_u8; 32]).into()).expect("valid test key");
        let authority_key =
            SigningKey::from_bytes((&[11_u8; 32]).into()).expect("valid authority key");
        let engine_spki = engine_key
            .verifying_key()
            .to_public_key_der()
            .expect("engine SPKI encodes")
            .as_bytes()
            .to_vec();
        let authority_spki = authority_key
            .verifying_key()
            .to_public_key_der()
            .expect("authority SPKI encodes")
            .as_bytes()
            .to_vec();
        let current_exe = std::env::current_exe().expect("test executable path");
        let mut anchor = ReceiptTrustAnchor {
            schema_version: 2,
            installation_id: "installation-bootstrap-test".to_owned(),
            key_id: Digest32::sha256(&engine_spki),
            key_epoch: 1,
            algorithm: SIGNATURE_ALGORITHM.to_owned(),
            public_key_spki_der: URL_SAFE_NO_PAD.encode(&engine_spki),
            provider: SignerProvider::WindowsCngMachine,
            trust: SignerTrust::ProductionProtected,
            service_identity: "S-1-5-80-12345".to_owned(),
            engine_release: env!("CARGO_PKG_VERSION").to_owned(),
            source_commit: option_env!("AMW_SOURCE_COMMIT")
                .unwrap_or("test-source-commit")
                .to_owned(),
            libllama_revision: option_env!("AMW_LIBLLAMA_REV")
                .unwrap_or("86a9c79f866799eb0e7e89c03578ccfbcc5d808e")
                .to_owned(),
            release_manifest_sha256: Digest32::sha256(b"test-release-manifest"),
            engine_binary_sha256: sha256_file(&current_exe).expect("test executable hashes"),
            authenticode_signer_identity: Some("CN=Vetinari Test Signer".to_owned()),
            created_at: "2026-07-22T00:00:00Z".to_owned(),
            predecessor_key_id: None,
            predecessor_key_epoch: None,
            predecessor_anchor_sha256: None,
            authority_key_id: Digest32::sha256(&authority_spki),
            authority_public_key_spki_der: URL_SAFE_NO_PAD.encode(&authority_spki),
            proof_of_possession: String::new(),
            authority_signature: String::new(),
        };
        let statement = canonical_receipt_anchor_statement(&anchor, &engine_spki, &authority_spki)
            .expect("anchor statement canonicalizes");
        anchor.proof_of_possession = sign_anchor_statement(&engine_key, &statement);
        anchor.authority_signature = sign_anchor_statement(&authority_key, &statement);
        let anchor_bytes = serde_json::to_vec(&anchor).expect("anchor serializes");
        let anchor_path = temp.path().join("receipt-anchor.json");
        fs::write(&anchor_path, &anchor_bytes).expect("anchor writes");
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.budgets.ram_gb = 0.25;
        config.kv.session_dir = temp.path().join("sessions");
        config.log.dir = temp.path().join("logs");
        config.receipts = ReceiptConfig {
            trust_anchor_path: Some(anchor_path),
            ledger_path: Some(temp.path().join("receipt-ledger.sqlite3")),
            anchor_sha256: Some(Digest32::sha256(&anchor_bytes).to_string()),
            authority_pin_sha256: Some(anchor.authority_key_id.to_string()),
        };
        (config, engine_key, authority_key, anchor)
    }

    fn write_signed_anchor(
        config: &mut EngineConfig,
        anchor: &mut ReceiptTrustAnchor,
        engine_key: &SigningKey,
        authority_key: &SigningKey,
    ) {
        let engine_spki = engine_key
            .verifying_key()
            .to_public_key_der()
            .expect("engine SPKI encodes")
            .as_bytes()
            .to_vec();
        let authority_spki = authority_key
            .verifying_key()
            .to_public_key_der()
            .expect("authority SPKI encodes")
            .as_bytes()
            .to_vec();
        anchor.key_id = Digest32::sha256(&engine_spki);
        anchor.public_key_spki_der = URL_SAFE_NO_PAD.encode(&engine_spki);
        anchor.authority_key_id = Digest32::sha256(&authority_spki);
        anchor.authority_public_key_spki_der = URL_SAFE_NO_PAD.encode(&authority_spki);
        let statement = canonical_receipt_anchor_statement(anchor, &engine_spki, &authority_spki)
            .expect("rotated anchor statement canonicalizes");
        anchor.proof_of_possession = sign_anchor_statement(engine_key, &statement);
        anchor.authority_signature = sign_anchor_statement(authority_key, &statement);
        let bytes = serde_json::to_vec(anchor).expect("rotated anchor serializes");
        fs::write(
            config
                .receipts
                .trust_anchor_path
                .as_deref()
                .expect("anchor path"),
            &bytes,
        )
        .expect("rotated anchor writes");
        config.receipts.anchor_sha256 = Some(Digest32::sha256(&bytes).to_string());
        config.receipts.authority_pin_sha256 = Some(anchor.authority_key_id.to_string());
    }

    fn runtime_for_test(temp: &tempfile::TempDir) -> EngineRuntime {
        let model_dir = temp.path().join("models");
        std::fs::create_dir_all(&model_dir).expect("model directory must be created");
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.budgets.ram_gb = 0.25;
        config.kv.session_dir = temp.path().join("sessions");
        config.log.dir = temp.path().join("logs");
        EngineRuntime::new(config, TelemetryHub::default(), MetricsHub::default())
            .expect("control-plane test runtime must initialize")
    }

    #[test]
    fn server_factory_preserves_unprovisioned_ordinary_inference() {
        let temp = tempfile::tempdir().expect("temporary directory");
        let model_dir = temp.path().join("models");
        fs::create_dir_all(&model_dir).expect("model directory");
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.kv.session_dir = temp.path().join("sessions");

        let ledger_opened = Cell::new(false);
        let result = EngineRuntime::new_for_server_with(
            config,
            TelemetryHub::default(),
            MetricsHub::default(),
            &UnavailableResolver,
            |_path, _service_identity| {
                ledger_opened.set(true);
                Err(LedgerError::UnsafeLedgerPath(
                    "negative control: unprovisioned server opened receipt ledger".to_owned(),
                ))
            },
        );
        assert!(!ledger_opened.get());
        let runtime = result.expect("ordinary inference remains available");

        assert!(runtime.receipt_trust_report().is_none());
        assert!(runtime.receipt_ledger_is_ready());
    }

    #[test]
    fn cng_export_policy_rejects_every_known_private_key_export_permission() {
        assert!(validate_cng_private_key_export_policy(0).is_ok());
        for (name, bit) in [
            ("allow_export", 0x0000_0001),
            ("allow_plaintext_export", 0x0000_0002),
            ("allow_archiving", 0x0000_0004),
            ("allow_plaintext_archiving", 0x0000_0008),
        ] {
            assert!(
                validate_cng_private_key_export_policy(bit).is_err(),
                "{name} must prevent protected signer construction"
            );
        }
        assert!(validate_cng_private_key_export_policy(0x0000_000f).is_err());
        assert!(validate_cng_private_key_export_policy(0x8000_0000).is_err());
    }

    fn cng_key_security_fixture(
        owner_identity: &str,
        service_access_mask: u32,
        system_access_mask: u32,
    ) -> CngKeySecuritySnapshot {
        CngKeySecuritySnapshot {
            owner_identity: owner_identity.to_owned(),
            owner_defaulted: false,
            explicit_allow_aces: vec![
                CngKeyExplicitAllowAce {
                    identity: "S-1-5-80-1-2-3-4-5".to_owned(),
                    access_mask: service_access_mask,
                },
                CngKeyExplicitAllowAce {
                    identity: WINDOWS_SYSTEM_SID.to_owned(),
                    access_mask: system_access_mask,
                },
            ],
        }
    }

    #[test]
    fn cng_key_security_accepts_service_or_system_owner_with_exact_full_control() {
        let service_sid = "S-1-5-80-1-2-3-4-5";
        for owner in [service_sid, WINDOWS_SYSTEM_SID] {
            let snapshot = cng_key_security_fixture(
                owner,
                CNG_KEY_FULL_CONTROL_MASK,
                CNG_KEY_FULL_CONTROL_MASK,
            );
            let identities = validate_cng_key_security_snapshot(service_sid, &snapshot)
                .expect("canonical owner and DACL confer protected key trust");
            assert_eq!(identities, vec![WINDOWS_SYSTEM_SID, service_sid]);
        }
    }

    #[test]
    fn cng_key_security_rejects_foreign_owner() {
        let snapshot = cng_key_security_fixture(
            "S-1-5-21-1-2-3-1001",
            CNG_KEY_FULL_CONTROL_MASK,
            CNG_KEY_FULL_CONTROL_MASK,
        );

        assert!(validate_cng_key_security_snapshot("S-1-5-80-1-2-3-4-5", &snapshot).is_err());
    }

    #[test]
    fn cng_key_security_rejects_defaulted_owner() {
        let service_sid = "S-1-5-80-1-2-3-4-5";
        let mut snapshot = cng_key_security_fixture(
            service_sid,
            CNG_KEY_FULL_CONTROL_MASK,
            CNG_KEY_FULL_CONTROL_MASK,
        );
        snapshot.owner_defaulted = true;

        assert!(validate_cng_key_security_snapshot(service_sid, &snapshot).is_err());
    }

    #[test]
    fn cng_key_security_rejects_under_or_over_scoped_ace_masks() {
        let service_sid = "S-1-5-80-1-2-3-4-5";
        for (service_mask, system_mask) in [
            (
                CNG_KEY_FULL_CONTROL_MASK & !0x0000_0001,
                CNG_KEY_FULL_CONTROL_MASK,
            ),
            (
                CNG_KEY_FULL_CONTROL_MASK,
                CNG_KEY_FULL_CONTROL_MASK | 0x0000_0004,
            ),
            (0x001f_01ff, CNG_KEY_FULL_CONTROL_MASK),
            (CNG_KEY_FULL_CONTROL_MASK, 0x1000_0000),
        ] {
            let snapshot = cng_key_security_fixture(service_sid, service_mask, system_mask);
            assert!(validate_cng_key_security_snapshot(service_sid, &snapshot).is_err());
        }
    }

    #[test]
    fn windows_service_token_accepts_exact_enabled_service_sid_group() {
        let service_sid = "S-1-5-80-123-456-789-101112-131415";
        let groups = vec![(
            service_sid.to_owned(),
            crate::receipt::ledger::TOKEN_GROUP_ENABLED_ATTRIBUTE,
        )];

        assert_eq!(
            validate_windows_service_token(service_sid, "S-1-5-18", &groups)
                .expect("enabled dedicated service SID is authoritative"),
            service_sid
        );
    }

    #[test]
    fn windows_service_token_rejects_token_user_only_identity() {
        let service_sid = "S-1-5-80-123-456-789-101112-131415";

        assert!(matches!(
            validate_windows_service_token(service_sid, service_sid, &[]),
            Err(RuntimeError::EvalReceiptAuthority(_))
        ));
    }

    #[test]
    fn windows_service_token_rejects_disabled_or_noncanonical_service_group() {
        let service_sid = "S-1-5-80-123-456-789-101112-131415";
        for attributes in [0, crate::receipt::ledger::TOKEN_GROUP_DENY_ONLY_ATTRIBUTE] {
            let groups = vec![(service_sid.to_owned(), attributes)];
            assert!(
                validate_windows_service_token(service_sid, "S-1-5-21-1-2-3-1001", &groups)
                    .is_err()
            );
        }

        assert!(validate_windows_service_token("S-1-5-80-123", "S-1-5-18", &[]).is_err());
        assert!(validate_windows_service_token(
            "S-1-5-80-00123-456-789-101112-131415",
            "S-1-5-18",
            &[]
        )
        .is_err());
    }

    #[test]
    fn server_factory_constructs_attested_production_authority() {
        let temp = tempfile::tempdir().expect("temporary directory");
        let (config, signing_key, _, anchor) = provisioned_server_config(&temp);

        let runtime = EngineRuntime::new_for_server_with(
            config,
            TelemetryHub::default(),
            MetricsHub::default(),
            &FakeProtectedResolver { signing_key },
            |path, _service_identity| ReceiptLedger::open_for_test(path),
        )
        .expect("attested protected authority initializes");
        let trust = runtime
            .receipt_trust_report()
            .expect("protected authority is advertised");

        assert_eq!(trust.installation_id, anchor.installation_id);
        assert_eq!(trust.key_id, anchor.key_id);
        assert_eq!(trust.provider, SignerProvider::WindowsCngMachine);
        assert!(!trust.engine_instance_id.is_empty());
        let wire = serde_json::to_value(&trust).expect("trust report serializes");
        assert_eq!(wire["engine_instance_id"], trust.engine_instance_id);
        assert!(runtime.receipt_ledger_is_ready());
    }

    #[test]
    fn server_factory_accepts_exact_rotation_and_rejects_stale_predecessor() {
        let temp = tempfile::tempdir().expect("temporary directory");
        let (mut config, first_key, authority_key, first_anchor) = provisioned_server_config(&temp);
        let first_anchor_sha256 = Digest32::from_lower_hex(
            config
                .receipts
                .anchor_sha256
                .as_deref()
                .expect("first anchor digest"),
        )
        .expect("first anchor digest parses");
        let first_runtime = EngineRuntime::new_for_server_with(
            config.clone(),
            TelemetryHub::default(),
            MetricsHub::default(),
            &FakeProtectedResolver {
                signing_key: first_key,
            },
            |path, _service_identity| ReceiptLedger::open_for_test(path),
        )
        .expect("first key registers");
        drop(first_runtime);

        let second_key = SigningKey::from_bytes((&[13_u8; 32]).into()).expect("valid rotated key");
        let mut second_anchor = first_anchor.clone();
        second_anchor.key_epoch = 2;
        second_anchor.created_at = "2026-07-22T00:00:01Z".to_owned();
        second_anchor.predecessor_key_id = Some(first_anchor.key_id);
        second_anchor.predecessor_key_epoch = Some(first_anchor.key_epoch);
        second_anchor.predecessor_anchor_sha256 = Some(first_anchor_sha256);
        write_signed_anchor(&mut config, &mut second_anchor, &second_key, &authority_key);
        let second_anchor_sha256 = Digest32::from_lower_hex(
            config
                .receipts
                .anchor_sha256
                .as_deref()
                .expect("second anchor digest"),
        )
        .expect("second anchor digest parses");
        let second_runtime = EngineRuntime::new_for_server_with(
            config.clone(),
            TelemetryHub::default(),
            MetricsHub::default(),
            &FakeProtectedResolver {
                signing_key: second_key,
            },
            |path, _service_identity| ReceiptLedger::open_for_test(path),
        )
        .expect("exact signed rotation registers");
        assert_eq!(
            second_runtime
                .receipt_trust_report()
                .expect("rotated trust is advertised")
                .key_epoch,
            2
        );
        drop(second_runtime);

        let third_key = SigningKey::from_bytes((&[17_u8; 32]).into()).expect("valid third key");
        let mut stale_anchor = second_anchor;
        stale_anchor.key_epoch = 3;
        stale_anchor.created_at = "2026-07-22T00:00:02Z".to_owned();
        stale_anchor.predecessor_key_id = Some(first_anchor.key_id);
        stale_anchor.predecessor_key_epoch = Some(first_anchor.key_epoch);
        stale_anchor.predecessor_anchor_sha256 = Some(first_anchor_sha256);
        write_signed_anchor(&mut config, &mut stale_anchor, &third_key, &authority_key);
        let error = EngineRuntime::new_for_server_with(
            config,
            TelemetryHub::default(),
            MetricsHub::default(),
            &FakeProtectedResolver {
                signing_key: third_key,
            },
            |path, _service_identity| ReceiptLedger::open_for_test(path),
        )
        .err()
        .expect("stale predecessor cannot register");

        assert!(matches!(error, RuntimeError::EvalReceiptAuthority(_)));
        assert_ne!(second_anchor_sha256, first_anchor_sha256);
    }

    #[test]
    fn server_factory_rejects_software_and_unavailable_providers() {
        let software_temp = tempfile::tempdir().expect("temporary directory");
        let (software_config, _, _, _) = provisioned_server_config(&software_temp);
        let software_ledger_opened = Cell::new(false);
        let software_result = EngineRuntime::new_for_server_with(
            software_config,
            TelemetryHub::default(),
            MetricsHub::default(),
            &SoftwareResolver,
            |_path, _service_identity| {
                software_ledger_opened.set(true);
                Err(LedgerError::UnsafeLedgerPath(
                    "negative control: software signer opened receipt ledger".to_owned(),
                ))
            },
        );
        assert!(!software_ledger_opened.get());
        let software = software_result
            .err()
            .expect("software signer cannot confer production trust");
        assert!(matches!(&software, RuntimeError::EvalReceiptAuthority(_)));
        assert!(software
            .to_string()
            .contains("resolved signer identity does not match"));

        let unavailable_temp = tempfile::tempdir().expect("temporary directory");
        let (unavailable_config, _, _, _) = provisioned_server_config(&unavailable_temp);
        let unavailable_ledger_opened = Cell::new(false);
        let unavailable_result = EngineRuntime::new_for_server_with(
            unavailable_config,
            TelemetryHub::default(),
            MetricsHub::default(),
            &UnavailableResolver,
            |_path, _service_identity| {
                unavailable_ledger_opened.set(true);
                Err(LedgerError::UnsafeLedgerPath(
                    "negative control: unavailable provider opened receipt ledger".to_owned(),
                ))
            },
        );
        assert!(!unavailable_ledger_opened.get());
        let unavailable = unavailable_result
            .err()
            .expect("unavailable protected provider fails startup");
        assert!(matches!(
            &unavailable,
            RuntimeError::EvalReceiptAuthority(_)
        ));
        assert!(unavailable
            .to_string()
            .contains("test protected provider unavailable"));
    }

    #[test]
    fn server_factory_rejects_substituted_authority_signature() {
        let temp = tempfile::tempdir().expect("temporary directory");
        let (mut config, signing_key, _, mut anchor) = provisioned_server_config(&temp);
        anchor.authority_signature = sign_anchor_statement(&signing_key, b"wrong statement");
        let bytes = serde_json::to_vec(&anchor).expect("tampered anchor serializes");
        fs::write(
            config
                .receipts
                .trust_anchor_path
                .as_deref()
                .expect("anchor path"),
            &bytes,
        )
        .expect("tampered anchor writes");
        config.receipts.anchor_sha256 = Some(Digest32::sha256(&bytes).to_string());

        let ledger_opened = Cell::new(false);
        let result = EngineRuntime::new_for_server_with(
            config,
            TelemetryHub::default(),
            MetricsHub::default(),
            &FakeProtectedResolver { signing_key },
            |_path, _service_identity| {
                ledger_opened.set(true);
                Err(LedgerError::UnsafeLedgerPath(
                    "negative control: invalid authority signature opened receipt ledger"
                        .to_owned(),
                ))
            },
        );
        assert!(!ledger_opened.get());
        let error = result
            .err()
            .expect("substituted authority signature fails closed");

        assert!(matches!(&error, RuntimeError::EvalReceiptAuthority(_)));
        assert!(error.to_string().contains("authority signature is invalid"));
    }

    #[test]
    fn trust_anchor_loader_rejects_duplicate_json_fields() {
        let temp = tempfile::tempdir().expect("temporary directory");
        let (mut config, _, _, _) = provisioned_server_config(&temp);
        let path = config
            .receipts
            .trust_anchor_path
            .as_deref()
            .expect("anchor path");
        let mut text =
            String::from_utf8(fs::read(path).expect("anchor reads")).expect("anchor is UTF-8 JSON");
        assert_eq!(text.pop(), Some('}'));
        text.push_str(",\"schema_version\":2}");
        fs::write(path, text.as_bytes()).expect("duplicate anchor writes");
        config.receipts.anchor_sha256 = Some(Digest32::sha256(text.as_bytes()).to_string());

        let error = load_verified_receipt_anchor(&config.receipts)
            .err()
            .expect("duplicate anchor field fails closed");

        assert!(matches!(error, RuntimeError::EvalReceiptAuthority(_)));
        assert!(error.to_string().contains("duplicate"));
    }

    #[test]
    fn external_kv_root_requires_preprovisioned_namespaces_without_creating_them() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("external-kv");
        prepare_managed_private_root(&root).unwrap();
        let mut config = EngineConfig::default();
        config.kv.session_dir = root.clone();
        config.kv.root_policy = KvRootPolicy::ExternalPreprovisioned;

        prepare_kv_storage(&config).expect_err("external namespaces must be preprovisioned");

        assert!(!root.join("durable").exists());
        assert!(!root.join("scheduler").exists());
        assert!(!root.join("adapter-cas").exists());
        verify_secure_directory(&root).unwrap();
    }

    #[test]
    fn external_kv_root_uses_preprovisioned_adapter_namespace_without_sibling_mutation() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("external-kv");
        prepare_managed_private_root(&root).unwrap();
        for namespace in ["durable", "scheduler", "adapter-cas"] {
            ensure_private_directory(&root.join(namespace)).unwrap();
        }
        let model_dir = temp.path().join("models");
        std::fs::create_dir(&model_dir).unwrap();
        let sibling = temp.path().join("adapter-cas");
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.budgets.ram_gb = 0.25;
        config.kv.session_dir = root.clone();
        config.kv.root_policy = KvRootPolicy::ExternalPreprovisioned;

        let runtime = EngineRuntime::new(config, TelemetryHub::default(), MetricsHub::default())
            .expect("preprovisioned external namespaces must initialize without mutation");
        drop(runtime);

        assert!(!sibling.exists());
        for namespace in ["durable", "scheduler", "adapter-cas"] {
            verify_secure_directory(&root.join(namespace)).unwrap();
        }
    }

    #[test]
    fn managed_kv_root_creates_exact_private_fixed_namespaces() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("managed-kv");
        let mut config = EngineConfig::default();
        config.kv.session_dir = root.clone();

        prepare_kv_storage(&config).unwrap();

        verify_secure_directory(&root).unwrap();
        verify_secure_directory(&root.join("durable")).unwrap();
        verify_secure_directory(&root.join("scheduler")).unwrap();
        verify_secure_directory(&root.join("adapter-cas")).unwrap();
    }

    fn registration(
        scheduler_id: u64,
        principal_id: &str,
        control: GenerationControl,
    ) -> RequestRegistration {
        RequestRegistration {
            scheduler_id,
            model_id: "model-a".to_owned(),
            principal_id: principal_id.to_owned(),
            control,
        }
    }

    fn chat_generate_request(prompt: String) -> GenerateRequest {
        GenerateRequest {
            request_id: "chat-pinning".to_owned(),
            trace_id: "trace-chat-pinning".to_owned(),
            principal_id: "principal-a".to_owned(),
            model: Some("model-a".to_owned()),
            prompt,
            infill_suffix: None,
            max_tokens: 1,
            stop: Vec::new(),
            sampling: SamplerParams::default(),
            grammar: None,
            priority: PriorityClass::Interactive,
            role: WorkloadRole::Worker,
            eval_slot: None,
            eval_context: None,
            endpoint: "/v1/chat/completions".to_owned(),
            original_messages: vec![("user".to_owned(), "hello".to_owned())],
            session_id: None,
            prefix_refs: Vec::new(),
            deadline: Instant::now() + Duration::from_secs(1),
            #[cfg(all(feature = "contract-test-controls", debug_assertions))]
            contract_failure: None,
        }
    }

    #[test]
    fn generation_control_distinguishes_verified_draft_artifacts_and_pair_policy() {
        let request = chat_generate_request("draft identity".to_owned());
        let vocabulary = Digest32::sha256(b"shared-vocabulary");
        let first = SpeculationReceiptIdentity::DraftModel {
            model_id: "draft-a".to_owned(),
            model_sha256: Digest32::sha256(b"draft-artifact-a"),
            minimum_context: Some(4_096),
            vocabulary_fingerprint: vocabulary,
        };
        let different_artifact = SpeculationReceiptIdentity::DraftModel {
            model_id: "draft-a".to_owned(),
            model_sha256: Digest32::sha256(b"draft-artifact-b"),
            minimum_context: Some(4_096),
            vocabulary_fingerprint: vocabulary,
        };
        let different_policy = SpeculationReceiptIdentity::DraftModel {
            model_id: "draft-a".to_owned(),
            model_sha256: Digest32::sha256(b"draft-artifact-a"),
            minimum_context: Some(8_192),
            vocabulary_fingerprint: vocabulary,
        };

        let first_digest = generation_control_sha256(&request, &first).unwrap();

        assert_ne!(
            first_digest,
            generation_control_sha256(&request, &different_artifact).unwrap()
        );
        assert_ne!(
            first_digest,
            generation_control_sha256(&request, &different_policy).unwrap()
        );
        assert_ne!(
            first_digest,
            generation_control_sha256(&request, &SpeculationReceiptIdentity::PromptLookup).unwrap()
        );
    }

    fn model_handle(id: &str) -> ModelHandle {
        let (handle, receiver) = model_handle_with_receiver(id, [0; 32]);
        drop(receiver);
        handle
    }

    fn model_handle_with_receiver(
        id: &str,
        model_fingerprint: [u8; 32],
    ) -> (ModelHandle, Receiver<ModelCommand>) {
        let (sender, receiver) = mpsc::sync_channel(4);
        let handle = ModelHandle {
            info: ModelInfo {
                id: id.to_owned(),
                path: PathBuf::from(format!("{id}.gguf")),
                architecture: "test".to_owned(),
                quant: "test".to_owned(),
                context_length: 128,
                embedding_length: 0,
                supports_embeddings: false,
                supports_fim: false,
                chat_template: None,
                model_fingerprint,
            },
            sender,
            healthy: Arc::new(AtomicBool::new(true)),
            queued: Arc::new(AtomicUsize::new(0)),
            busy: Arc::new(AtomicUsize::new(0)),
            max_batch_sequences: Arc::new(AtomicUsize::new(1)),
            background_evicted: Arc::new(AtomicUsize::new(0)),
            slot_count: 1,
        };
        (handle, receiver)
    }

    #[test]
    fn runtime_retains_watchdog_thread_and_counts_invalid_telemetry() {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let runtime = runtime_for_test(&temp);
        assert!(runtime
            .inner
            .watchdog_thread
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .is_some());

        emit_observable(
            runtime.telemetry(),
            runtime.metrics(),
            EngineEvent::Gauges {
                slots_busy: 0,
                queue_depth: 0,
                vram_used_mb: None,
                kv_occupancy_pct: 101,
            },
            "known_bad_test_event",
        );

        assert_eq!(runtime.metrics().snapshot().telemetry_emission_failures, 1);
        assert_eq!(runtime.telemetry().len(), 0);
    }

    #[test]
    fn production_watchdog_cancels_stuck_work_and_types_empty_eviction() {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let runtime = runtime_for_test(&temp);
        let control = GenerationControl::new(None);
        runtime
            .inner
            .requests
            .insert(
                "watchdog-request".to_owned(),
                registration(41, "principal-a", control.clone()),
            )
            .expect("watchdog request registration must succeed");

        execute_watchdog_events(
            &runtime.inner,
            [WatchdogEvent {
                sequence_id: 41,
                action: WatchdogAction::KillSequence,
                detail: "known stuck sequence",
                trace: crate::telemetry::TraceContext::new("watchdog-request", "trace-41"),
            }],
        );

        assert_eq!(control.state(), GenerationControlState::Cancelled);
        let eviction_error = evict_idle_model(&runtime.inner)
            .expect_err("an empty runtime must expose the watchdog eviction failure");
        assert!(eviction_error
            .to_string()
            .contains("no idle model was available"));
    }

    #[test]
    fn duplicate_correlations_are_principal_scoped_and_scheduler_owned() {
        let registry = RequestRegistry::default();
        let first = GenerationControl::new(None);
        let second = GenerationControl::new(None);
        registry
            .insert(
                "shared-correlation".to_owned(),
                registration(1, "principal-a", first.clone()),
            )
            .expect("first registration must succeed");
        registry
            .insert(
                "shared-correlation".to_owned(),
                registration(2, "principal-b", second.clone()),
            )
            .expect("another principal may reuse correlation metadata");

        let collision = registry.insert(
            "shared-correlation".to_owned(),
            registration(3, "principal-a", GenerationControl::new(None)),
        );
        assert!(matches!(collision, Err(RuntimeError::UnsupportedParam(_))));
        assert!(registry.remove_scheduler(3).is_none());
        assert_eq!(
            registry
                .registration("shared-correlation", "principal-a")
                .expect("same-principal collision must preserve original registration")
                .principal_id,
            "principal-a"
        );
        assert_eq!(first.state(), GenerationControlState::Running);
        assert_eq!(second.state(), GenerationControlState::Running);

        assert!(registry.remove_scheduler(1).is_some());
        assert!(registry
            .registration("shared-correlation", "principal-a")
            .is_none());
        assert!(registry
            .registration("shared-correlation", "principal-b")
            .is_some());
    }

    #[test]
    fn cancellation_isolated_when_principals_reuse_correlation_id() {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let runtime = runtime_for_test(&temp);
        let first = GenerationControl::new(None);
        let second = GenerationControl::new(None);
        runtime
            .inner
            .requests
            .insert(
                "shared-correlation".to_owned(),
                registration(1, "principal-a", first.clone()),
            )
            .expect("first principal registration must succeed");
        runtime
            .inner
            .requests
            .insert(
                "shared-correlation".to_owned(),
                registration(2, "principal-b", second.clone()),
            )
            .expect("second principal registration must succeed");

        assert!(!runtime.cancel("shared-correlation"));
        assert!(matches!(
            runtime.cancel_owned("shared-correlation", "principal-c"),
            Err(RuntimeError::SessionUnknown(_))
        ));
        assert_eq!(first.state(), GenerationControlState::Running);
        assert_eq!(second.state(), GenerationControlState::Running);

        assert!(runtime
            .cancel_owned("shared-correlation", "principal-b")
            .expect("the owning principal must be able to cancel"));
        assert_eq!(first.state(), GenerationControlState::Running);
        assert_eq!(second.state(), GenerationControlState::Cancelled);
    }

    #[test]
    fn omitted_model_fails_closed_with_multiple_loaded_models() -> Result<(), String> {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let runtime = runtime_for_test(&temp);
        {
            let mut models = runtime
                .inner
                .models
                .write()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            models.insert("model-a".to_owned(), model_handle("model-a"));
            models.insert("model-b".to_owned(), model_handle("model-b"));
        }

        let error = if let Err(error) = runtime.model_handle(None) {
            error
        } else {
            return Err("an omitted model must not select by map order".to_owned());
        };
        assert!(matches!(
            error,
            RuntimeError::UnsupportedParam(message)
                if message.contains("no governed default model")
        ));
        assert_eq!(runtime.status().models.len(), 2);
        Ok(())
    }

    #[tokio::test]
    async fn chat_generation_stays_on_rendering_worker_across_reload_window() {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let runtime = runtime_for_test(&temp);
        let (generation_a, commands_a) = model_handle_with_receiver("model-a", [0xAA; 32]);
        let (generation_b, commands_b) = model_handle_with_receiver("model-a", [0xBB; 32]);
        runtime
            .inner
            .models
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert("model-a".to_owned(), generation_a.clone());

        let (observed, observation) = mpsc::sync_channel(1);
        let worker = std::thread::spawn(move || -> Result<(), String> {
            let render = commands_a
                .recv_timeout(Duration::from_secs(1))
                .map_err(|error| {
                    format!("original worker did not receive chat rendering: {error}")
                })?;
            let ModelCommand::RenderChat { reply, .. } = render else {
                return Err("original worker received generation before chat rendering".to_owned());
            };
            reply
                .send(Ok("rendered-by-generation-a".to_owned()))
                .map_err(|_| "render reply receiver disconnected".to_owned())?;

            let generate = commands_a
                .recv_timeout(Duration::from_secs(1))
                .map_err(|error| {
                    format!("original worker did not receive pinned generation: {error}")
                })?;
            let ModelCommand::Generate {
                scheduler_id,
                request,
                ..
            } = generate
            else {
                return Err(
                    "original worker received an unexpected command after rendering".to_owned(),
                );
            };
            assert_eq!(request.prompt, "rendered-by-generation-a");
            observed
                .send(scheduler_id)
                .map_err(|_| "test observation receiver disconnected".to_owned())?;
            Ok(())
        });

        let rendered = EngineRuntime::render_chat_on_handle(
            "model-a".to_owned(),
            generation_a,
            vec![("user".to_owned(), "hello".to_owned())],
        )
        .await
        .expect("generation A must render the chat prompt");
        let prompt = rendered.prompt().to_owned();

        let reload_runtime = runtime.clone();
        tokio::spawn(async move {
            reload_runtime
                .inner
                .models
                .write()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .insert("model-a".to_owned(), generation_b);
        })
        .await
        .expect("deterministic reload task must complete");

        let generation = runtime
            .generate_chat(chat_generate_request(prompt), rendered)
            .await
            .expect("pinned submission must stay on generation A");

        assert_eq!(generation.model(), "model-a");
        let scheduler_id = observation
            .recv_timeout(Duration::from_secs(1))
            .expect("original worker must observe the generation");
        assert!(matches!(
            commands_b.recv_timeout(Duration::from_millis(20)),
            Err(RecvTimeoutError::Timeout)
        ));
        runtime.inner.requests.remove_scheduler(scheduler_id);
        drop(generation);
        worker
            .join()
            .expect("test worker must exit cleanly")
            .expect("test worker command contract must hold");
    }

    #[tokio::test]
    async fn chat_generation_fails_closed_when_rendering_worker_is_unloaded() {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let runtime = runtime_for_test(&temp);
        let (generation_a, commands_a) = model_handle_with_receiver("model-a", [0xAA; 32]);
        let (generation_b, commands_b) = model_handle_with_receiver("model-a", [0xBB; 32]);
        runtime
            .inner
            .models
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert("model-a".to_owned(), generation_a.clone());

        let render_worker = std::thread::spawn(move || -> Result<(), String> {
            let render = commands_a
                .recv_timeout(Duration::from_secs(1))
                .map_err(|error| {
                    format!("original worker did not receive chat rendering: {error}")
                })?;
            let ModelCommand::RenderChat { reply, .. } = render else {
                return Err("original worker received an unexpected command".to_owned());
            };
            reply
                .send(Ok("rendered-by-generation-a".to_owned()))
                .map_err(|_| "render reply receiver disconnected".to_owned())
        });

        let rendered = EngineRuntime::render_chat_on_handle(
            "model-a".to_owned(),
            generation_a.clone(),
            vec![("user".to_owned(), "hello".to_owned())],
        )
        .await
        .expect("generation A must render the chat prompt");
        let prompt = rendered.prompt().to_owned();
        render_worker
            .join()
            .expect("render worker must exit cleanly")
            .expect("render worker command contract must hold");

        generation_a.healthy.store(false, Ordering::Release);
        runtime
            .inner
            .models
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert("model-a".to_owned(), generation_b);

        let result = runtime
            .generate_chat(chat_generate_request(prompt), rendered)
            .await;
        assert!(
            result.is_err(),
            "an unloaded rendering worker must fail closed"
        );
        let error = result
            .err()
            .expect("the asserted failure must include its runtime error");
        assert!(matches!(
            error,
            RuntimeError::Internal(message) if message.contains("model worker is unhealthy")
        ));
        assert!(matches!(
            commands_b.recv_timeout(Duration::from_millis(20)),
            Err(RecvTimeoutError::Timeout)
        ));
        assert!(runtime
            .inner
            .requests
            .registration("chat-pinning", "principal-a")
            .is_none());
    }

    #[tokio::test]
    async fn eval_stream_commits_receipt_before_exposing_finished() {
        let temp = tempfile::tempdir().expect("temporary directory must be created");
        let ledger = Arc::new(
            ReceiptLedger::open_for_test(temp.path().join("receipt.sqlite3"))
                .expect("receipt ledger must open"),
        );
        let signer = Arc::new(
            crate::receipt::SoftwareTestSigner::from_secret_bytes([9; 32], 1)
                .expect("test signer must be valid"),
        );
        let runtime_identity = ReceiptRuntimeIdentity {
            installation_id: "installation-test".to_owned(),
            anchor_sha256: Digest32::sha256(b"anchor"),
            authority_pin_sha256: Digest32::sha256(b"authority"),
            engine_release: "test-release".to_owned(),
            source_commit: "test-source".to_owned(),
            libllama_revision: "test-libllama".to_owned(),
            release_manifest_sha256: Digest32::sha256(b"manifest"),
            engine_binary_sha256: Digest32::sha256(b"binary"),
        };
        let authority = EvalReceiptAuthority::new(
            Arc::clone(&ledger),
            signer,
            runtime_identity,
            "untrusted-test".to_owned(),
        )
        .expect("test authority must bind");
        let mut request = chat_generate_request("rendered prompt".to_owned());
        request.endpoint = "/v1/completions".to_owned();
        request.original_messages.clear();
        request.priority = PriorityClass::Eval;
        request.eval_slot = Some(2);
        request.sampling.seed = 17;
        request.eval_context = Some(crate::receipt::EvalContext {
            schema_version: 1,
            run_id: "run-a".to_owned(),
            suite_id: "suite-a".to_owned(),
            suite_revision_sha256: Digest32::sha256(b"suite"),
            case_id: "case-a".to_owned(),
            ordinal: 3,
            case_spec_sha256: Digest32::sha256(b"case"),
        });
        let attempt = AttemptIdentity {
            installation_id: authority.identity.installation_id.clone(),
            run_id: "run-a".to_owned(),
            suite_id: "suite-a".to_owned(),
            case_id: "case-a".to_owned(),
            ordinal: 3,
        };
        let reservation = ledger
            .reserve_attempt(&request.request_id, &attempt)
            .expect("attempt must reserve");
        let (identity_sender, identity_receiver) = oneshot::channel();
        identity_sender
            .send(Ok(ReceiptExecutionIdentity {
                model_id: "model-a".to_owned(),
                model_sha256: Digest32::sha256(b"model"),
                adapter_set_sha256: crate::receipt::absent_sha256(
                    crate::receipt::AbsentDigestField::AdapterSet,
                ),
                template_sha256: crate::receipt::absent_sha256(
                    crate::receipt::AbsentDigestField::Template,
                ),
                system_messages_sha256: crate::receipt::absent_sha256(
                    crate::receipt::AbsentDigestField::SystemMessages,
                ),
                grammar_sha256: crate::receipt::absent_sha256(
                    crate::receipt::AbsentDigestField::Grammar,
                ),
                sampler_sha256: Digest32::from_bytes(request.sampling.identity_sha256()),
                generation_control_sha256: Digest32::sha256(b"generation-control"),
                original_messages_sha256: crate::receipt::original_messages_sha256(&[])
                    .expect("empty message digest must be valid"),
                rendered_prompt_sha256: Digest32::sha256(request.prompt.as_bytes()),
            }))
            .expect("identity receiver must remain open");
        let (sender, receiver) = bounded_generation_stream(GenerationControl::default());
        let mut stream = GenerationStream {
            request_id: request.request_id.clone(),
            trace_id: request.trace_id.clone(),
            model: "model-a".to_owned(),
            receiver,
            pending_receipt: Some(PendingEvalReceipt {
                authority,
                reservation,
                request: request.clone(),
                engine_instance_id: "engine-test".to_owned(),
                execution_identity: identity_receiver,
            }),
            emitted_output: Vec::new(),
            engine_receipt: None,
            receipt_error: None,
            _receipt_test_dir: None,
        };
        sender
            .try_send(GenerationEvent::Delta {
                token_id: 7,
                bytes: b"answer".to_vec(),
                logprob: None,
                top_logprobs: Vec::new(),
            })
            .expect("delta must enqueue");
        sender
            .try_send(GenerationEvent::Finished {
                reason: crate::gen::StopReason::MaxTokens,
                usage: crate::gen::GenerationUsage {
                    prompt_tokens: 4,
                    completion_tokens: 1,
                },
                confidence: None,
            })
            .expect("terminal must enqueue");

        assert!(matches!(
            stream.recv().await,
            Some(GenerationEvent::Delta { .. })
        ));
        assert!(matches!(
            stream.recv().await,
            Some(GenerationEvent::Finished { .. })
        ));
        let receipt = stream
            .engine_receipt()
            .expect("finished must not escape before durable receipt commit");
        assert_eq!(receipt.claims.output_sha256, Digest32::sha256(b"answer"));
        assert!(ledger
            .receipt_for_request(&request.request_id)
            .expect("ledger read must succeed")
            .is_some());
    }

    #[tokio::test]
    async fn eval_stream_signer_failure_replaces_finished_with_terminal_failure() {
        let (mut stream, ledger) = receipt_terminal_test_stream(ReceiptTerminalTestFailure::Signer);

        assert!(matches!(
            stream.recv().await,
            Some(GenerationEvent::Delta { .. })
        ));
        assert!(matches!(
            stream.recv().await,
            Some(GenerationEvent::Failed(
                crate::gen::GenError::RuntimeFailure {
                    code: crate::gen::GenerationFailureCode::Internal,
                    ..
                }
            ))
        ));
        assert!(stream.engine_receipt().is_none());
        assert!(matches!(
            stream.take_receipt_error(),
            Some(RuntimeError::EvalReceiptCommit(_))
        ));
        assert!(ledger
            .receipt_for_request("terminal-signer-failure")
            .expect("intact ledger remains readable")
            .is_none());
        assert!(stream.recv().await.is_none());
    }

    #[tokio::test]
    async fn eval_stream_ledger_failure_replaces_finished_with_terminal_failure() {
        let (mut stream, ledger) = receipt_terminal_test_stream(ReceiptTerminalTestFailure::Ledger);

        assert!(matches!(
            stream.recv().await,
            Some(GenerationEvent::Delta { .. })
        ));
        assert!(matches!(
            stream.recv().await,
            Some(GenerationEvent::Failed(
                crate::gen::GenError::RuntimeFailure {
                    code: crate::gen::GenerationFailureCode::Internal,
                    ..
                }
            ))
        ));
        assert!(stream.engine_receipt().is_none());
        assert!(matches!(
            stream.take_receipt_error(),
            Some(RuntimeError::EvalReceiptCommit(_))
        ));
        assert!(ledger
            .receipt_for_request("terminal-ledger-failure")
            .is_err());
        assert!(stream.recv().await.is_none());
    }
}

#[derive(Clone)]
struct ModelHandle {
    info: ModelInfo,
    sender: SyncSender<ModelCommand>,
    healthy: Arc<AtomicBool>,
    queued: Arc<AtomicUsize>,
    busy: Arc<AtomicUsize>,
    max_batch_sequences: Arc<AtomicUsize>,
    background_evicted: Arc<AtomicUsize>,
    slot_count: usize,
}

struct ModelWorkerOwner {
    handle: ModelHandle,
    join: Mutex<Option<std::thread::JoinHandle<()>>>,
}

impl ModelWorkerOwner {
    fn shutdown(&self) -> Result<(), String> {
        // Close admission before queuing Shutdown so a concurrently pinned chat request cannot
        // be accepted behind the terminal command and disappear without a terminal event.
        self.handle.healthy.store(false, Ordering::Release);
        self.handle.queued.fetch_add(1, Ordering::AcqRel);
        if self.handle.sender.send(ModelCommand::Shutdown).is_err() {
            self.handle.queued.fetch_sub(1, Ordering::AcqRel);
        }
        if let Some(join) = self
            .join
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .take()
        {
            if join.join().is_err() {
                tracing::error!("model worker panicked before shutdown completed");
            }
        }
        Ok(())
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct WorkerHealthGuard {
    healthy: Arc<AtomicBool>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
trait LoraApply<A: ?Sized> {
    fn apply_lora(&mut self, adapters: &A) -> Result<(), String>;
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl<'a> LoraApply<[(&'a LoraAdapter, f32)]> for Context {
    fn apply_lora(&mut self, adapters: &[(&'a LoraAdapter, f32)]) -> Result<(), String> {
        self.set_lora_adapters(adapters)
            .map_err(|error| error.to_string())
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Debug)]
struct LoraTransactionError {
    apply_error: String,
    rollback_errors: Vec<String>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl LoraTransactionError {
    fn rollback_failed(&self) -> bool {
        !self.rollback_errors.is_empty()
    }

    fn detail(&self) -> String {
        if self.rollback_errors.is_empty() {
            self.apply_error.clone()
        } else {
            format!(
                "{}; rollback failed: {}",
                self.apply_error,
                self.rollback_errors.join("; ")
            )
        }
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn apply_lora_transaction<A: ?Sized, C: LoraApply<A>>(
    serving: &mut C,
    embedding: Option<&mut C>,
    previous: &A,
    replacement: &A,
) -> Result<(), LoraTransactionError> {
    serving
        .apply_lora(replacement)
        .map_err(|apply_error| LoraTransactionError {
            apply_error: format!("serving LoRA apply failed: {apply_error}"),
            rollback_errors: Vec::new(),
        })?;
    let Some(embedding) = embedding else {
        return Ok(());
    };
    if let Err(error) = embedding.apply_lora(replacement) {
        let mut rollback_errors = Vec::new();
        if let Err(rollback) = embedding.apply_lora(previous) {
            rollback_errors.push(format!("embedding context: {rollback}"));
        }
        if let Err(rollback) = serving.apply_lora(previous) {
            rollback_errors.push(format!("serving context: {rollback}"));
        }
        return Err(LoraTransactionError {
            apply_error: format!("embedding LoRA apply failed: {error}"),
            rollback_errors,
        });
    }
    Ok(())
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl Drop for WorkerHealthGuard {
    fn drop(&mut self) {
        self.healthy.store(false, Ordering::Release);
    }
}

impl Drop for ModelWorkerOwner {
    fn drop(&mut self) {
        let _ = self.shutdown();
    }
}

#[cfg_attr(not(any(feature = "cpu", feature = "cuda")), allow(dead_code))]
enum ModelCommand {
    Generate {
        scheduler_id: u64,
        request: Box<GenerateRequest>,
        control: GenerationControl,
        events: GenerationSender,
        receipt_identity: Option<oneshot::Sender<Result<ReceiptExecutionIdentity, RuntimeError>>>,
    },
    ResetSequence {
        scheduler_id: u64,
    },
    Tokenize {
        items: Vec<String>,
        add_special: bool,
        reply: oneshot::Sender<Result<Vec<Vec<i32>>, RuntimeError>>,
    },
    CountTokens {
        items: Vec<String>,
        add_special: bool,
        reply: oneshot::Sender<Result<Vec<u32>, RuntimeError>>,
    },
    Embed {
        items: Vec<String>,
        reply: oneshot::Sender<Result<Vec<Vec<f32>>, RuntimeError>>,
    },
    Prefix {
        command: PrefixCommand,
        reply: oneshot::Sender<Result<PrefixResult, RuntimeError>>,
    },
    Session {
        action: SessionAction,
        session_id: String,
        principal_id: String,
        reply: oneshot::Sender<Result<(), RuntimeError>>,
    },
    Lora {
        adapter: Option<VerifiedLoadGuard>,
        reply: oneshot::Sender<Result<(), RuntimeError>>,
    },
    RenderChat {
        messages: Vec<(String, String)>,
        reply: oneshot::Sender<Result<String, RuntimeError>>,
    },
    #[cfg(test)]
    TerminateWorkerForTest {
        reply: oneshot::Sender<()>,
    },
    Shutdown,
}

#[derive(Clone, Debug)]
pub enum PrefixCommand {
    Register {
        name: String,
        content: String,
        content_hash: String,
    },
    Pin {
        name: String,
        content_hash: String,
    },
    Unpin {
        name: String,
        content_hash: String,
    },
}

#[derive(Clone, Debug, Serialize)]
pub struct PrefixResult {
    pub name: String,
    pub content_hash: String,
    pub token_count: u32,
    pub pinned: bool,
}

#[derive(Clone, Copy, Debug)]
pub enum SessionAction {
    Create,
    Resume,
    Save,
    Delete,
}

impl EngineRuntime {
    pub fn new(
        config: EngineConfig,
        telemetry: TelemetryHub,
        metrics: MetricsHub,
    ) -> Result<Self, RuntimeError> {
        Self::new_inner(config, telemetry, metrics, None)
    }

    /// Creates the server runtime and resolves configured protected receipt trust.
    ///
    /// Completely absent receipt provisioning preserves ordinary inference. Any
    /// partial or unusable provisioning fails startup before the runtime exists.
    pub fn new_for_server(
        config: EngineConfig,
        telemetry: TelemetryHub,
        metrics: MetricsHub,
    ) -> Result<Self, RuntimeError> {
        Self::new_for_server_with(
            config,
            telemetry,
            metrics,
            &PlatformProtectedSignerResolver,
            |path: &Path, service_identity: &str| ReceiptLedger::open(path, service_identity),
        )
    }

    fn new_for_server_with<R, F>(
        config: EngineConfig,
        telemetry: TelemetryHub,
        metrics: MetricsHub,
        resolver: &R,
        open_ledger: F,
    ) -> Result<Self, RuntimeError>
    where
        R: ProtectedSignerResolver,
        F: FnOnce(&Path, &str) -> Result<ReceiptLedger, LedgerError>,
    {
        let receipt_fields_present = config.receipts.trust_anchor_path.is_some()
            || config.receipts.ledger_path.is_some()
            || config.receipts.anchor_sha256.is_some()
            || config.receipts.authority_pin_sha256.is_some();
        let authority = if config.receipts.is_provisioned() {
            Some(build_protected_receipt_authority(
                &config.receipts,
                resolver,
                open_ledger,
            )?)
        } else if receipt_fields_present {
            return Err(RuntimeError::EvalReceiptAuthority(
                "receipt provisioning is partial".to_owned(),
            ));
        } else {
            None
        };
        Self::new_inner(config, telemetry, metrics, authority)
    }

    /// Creates a runtime with a platform-provisioned receipt authority.
    ///
    /// The ordinary server constructor intentionally has no software or file-key
    /// fallback. A protected host may call this constructor only after it has
    /// independently verified the anchor and resolved its non-exportable key.
    pub fn new_with_receipt_authority(
        config: EngineConfig,
        telemetry: TelemetryHub,
        metrics: MetricsHub,
        authority: EvalReceiptAuthority,
    ) -> Result<Self, RuntimeError> {
        Self::new_inner(config, telemetry, metrics, Some(authority))
    }

    fn new_inner(
        config: EngineConfig,
        telemetry: TelemetryHub,
        metrics: MetricsHub,
        receipts: Option<EvalReceiptAuthority>,
    ) -> Result<Self, RuntimeError> {
        if config.slots.count == 0 || config.slots.default_ctx == 0 {
            return Err(RuntimeError::Internal(
                "slots.count and slots.default_ctx must be positive".to_owned(),
            ));
        }
        prepare_kv_storage(&config)?;
        let catalog = ModelRegistry::bootstrap(config.models.dirs.clone(), ScanLimits::default())
            .map_err(registry_error)?;
        let ram_capacity = gib_bytes(config.budgets.ram_gb)?;
        let vram_capacity = gib_bytes_allow_zero(config.budgets.vram_gb)?;
        let loader = ModelLoader::new(
            MonotonicClock::new(),
            MemoryLedger::new(MemoryAmount::ram(ram_capacity).with_vram(0, vram_capacity)),
            true,
        );
        let adapter_roots = config
            .models
            .dirs
            .iter()
            .enumerate()
            .map(|(index, path)| (format!("models-{index}"), path.clone()));
        let adapter_cas_root = config.kv.session_dir.join("adapter-cas");
        let adapter_registry = match config.kv.root_policy {
            KvRootPolicy::Managed => {
                AdapterRegistry::new(adapter_roots, adapter_cas_root, DEFAULT_MAX_ADAPTER_BYTES)
            }
            KvRootPolicy::ExternalPreprovisioned => AdapterRegistry::new_preprovisioned(
                adapter_roots,
                adapter_cas_root,
                DEFAULT_MAX_ADAPTER_BYTES,
            ),
        };
        let adapters = Arc::new(Mutex::new(
            adapter_registry.map_err(adapter_registry_error)?,
        ));
        let sessions =
            Arc::new(SessionStore::open(config.kv.durable_dir()).map_err(session_store_error)?);
        let watchdog = Arc::new(Mutex::new(Watchdog::new(
            SystemUptimeClock,
            config.slots.count,
        )));
        let global_slots = Arc::new(GlobalSlotArbiter::new(config.slots.count));
        let inner = Arc::new(RuntimeInner {
            config,
            receipts,
            engine_instance_id: new_engine_instance_id(),
            catalog: Mutex::new(catalog),
            loader: Mutex::new(loader),
            models: RwLock::new(BTreeMap::new()),
            draining: AtomicBool::new(false),
            requests: Arc::new(RequestRegistry::default()),
            request_counter: AtomicU64::new(1),
            scheduler_counter: AtomicU64::new(1),
            telemetry,
            metrics,
            watchdog,
            leak_monitor: Mutex::new(MemoryLeakMonitor::default()),
            global_slots,
            adapters,
            sessions,
            watchdog_thread: Mutex::new(None),
        });
        let watchdog_thread = start_watchdog_poll(Arc::downgrade(&inner))?;
        *inner
            .watchdog_thread
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(watchdog_thread);
        Ok(Self { inner })
    }

    pub fn telemetry(&self) -> &TelemetryHub {
        &self.inner.telemetry
    }

    pub fn metrics(&self) -> &MetricsHub {
        &self.inner.metrics
    }

    /// Returns the complete protected receipt identity, or `None` when unavailable.
    pub fn receipt_trust_report(&self) -> Option<ReceiptTrustReport> {
        self.inner
            .receipts
            .as_ref()
            .and_then(|authority| authority.trust_report(&self.inner.engine_instance_id))
    }

    /// Returns whether the configured receipt ledger passes its live integrity check.
    ///
    /// An unprovisioned ordinary-inference runtime has no receipt ledger and is
    /// therefore unaffected. A provisioned runtime fails closed on any ledger
    /// path, schema, trigger, durability, authority-binding, or row-integrity fault.
    #[must_use]
    pub fn receipt_ledger_is_ready(&self) -> bool {
        self.inner
            .receipts
            .as_ref()
            .is_none_or(|authority| authority.ledger.readiness_check().is_ok())
    }

    pub fn next_request_id(&self) -> String {
        let sequence = self.inner.request_counter.fetch_add(1, Ordering::Relaxed);
        format!("req-{sequence:016x}")
    }

    /// Returns an owned, point-in-time view of admitted models and rejected entries.
    pub(crate) fn model_catalog_snapshot(&self) -> (Vec<CatalogModel>, Vec<CatalogDiagnostic>) {
        let catalog = self
            .inner
            .catalog
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        (catalog.list(), catalog.diagnostics().to_vec())
    }

    pub async fn load_model(&self, model_id: &str) -> Result<ModelInfo, RuntimeError> {
        let canonical_id = self.canonical_model_id(model_id)?;
        let existing = self
            .inner
            .models
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .get(&canonical_id)
            .cloned();
        if let Some(handle) = existing {
            if handle.healthy.load(Ordering::Acquire) {
                return Ok(handle.info);
            }
            self.reap_unhealthy_model(&canonical_id).await?;
        }
        let (record, metadata, draft_record) = {
            let catalog = self
                .inner
                .catalog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let record = catalog
                .try_resolve(&canonical_id)
                .map_err(registry_error)?
                .ok_or_else(|| RuntimeError::ModelNotLoaded(model_id.to_owned()))?;
            let metadata = catalog
                .metadata(&canonical_id)
                .map_err(registry_error)?
                .cloned()
                .ok_or_else(|| RuntimeError::ModelCorrupt {
                    path: record.path.clone(),
                    reason: "catalog metadata is missing".to_owned(),
                })?;
            let draft_record = record
                .draft_pair
                .as_ref()
                .map(|pair| {
                    let draft = catalog
                        .try_resolve(&pair.draft_model_id)
                        .map_err(registry_error)?
                        .ok_or_else(|| RuntimeError::ModelCorrupt {
                            path: record.path.clone(),
                            reason: format!(
                                "configured draft model is unavailable: {}",
                                pair.draft_model_id
                            ),
                        })?;
                    let draft_metadata = catalog
                        .metadata(&draft.id)
                        .map_err(registry_error)?
                        .cloned()
                        .ok_or_else(|| RuntimeError::ModelCorrupt {
                            path: draft.path.clone(),
                            reason: "configured draft model metadata is missing".to_owned(),
                        })?;
                    Ok::<_, RuntimeError>((draft.clone(), draft_metadata))
                })
                .transpose()?;
            (record.clone(), metadata, draft_record)
        };
        let context_overhead = u64::from(self.inner.config.slots.default_ctx)
            .saturating_mul(self.inner.config.slots.count as u64)
            .saturating_mul(16);
        let draft_bytes = draft_record
            .as_ref()
            .map(|(_, metadata)| metadata.file_size_bytes.saturating_add(context_overhead))
            .unwrap_or(0);
        let amount = MemoryAmount::ram(
            metadata
                .file_size_bytes
                .saturating_add(draft_bytes)
                .saturating_add(context_overhead)
                .saturating_add(64 * 1024 * 1024),
        );
        let config = self.inner.config.clone();
        let services = ModelWorkerServices {
            requests: Arc::clone(&self.inner.requests),
            watchdog: Arc::clone(&self.inner.watchdog),
            global_slots: Arc::clone(&self.inner.global_slots),
            telemetry: self.inner.telemetry.clone(),
            metrics: self.inner.metrics.clone(),
            adapters: Arc::clone(&self.inner.adapters),
            sessions: Arc::clone(&self.inner.sessions),
        };
        let inner = Arc::clone(&self.inner);
        let keep_alive = loader_keep_alive(self.inner.config.idle.keep_alive);
        let handle_result = tokio::task::spawn_blocking(move || {
            let mut loader = inner
                .loader
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            let worker_error = Arc::new(Mutex::new(None));
            let captured_worker_error = Arc::clone(&worker_error);
            let load_result =
                loader.load_record_with(&record, amount, keep_alive, |record, verified| {
                    match start_model_worker(
                        record.clone(),
                        verified,
                        draft_record.clone(),
                        config,
                        services,
                    ) {
                        Ok(worker) => Ok(worker),
                        Err(error) => {
                            let message = error.to_string();
                            *captured_worker_error
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner()) = Some(error);
                            Err(message)
                        }
                    }
                });
            if let Err(error) = load_result {
                if matches!(error, LoaderError::Allocation(_)) {
                    if let Some(worker_error) = worker_error
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .take()
                    {
                        return Err(worker_error);
                    }
                }
                return Err(loader_error(error));
            }
            let handle = loader
                .resident_resource(&record.id)
                .and_then(|resource| resource.downcast_ref::<ModelWorkerOwner>())
                .map(|owner| owner.handle.clone())
                .ok_or_else(|| {
                    RuntimeError::Internal(
                        "resident model worker resource has an unexpected type".to_owned(),
                    )
                })?;
            let resident_ids = loader
                .resident_ids()
                .map(str::to_owned)
                .collect::<BTreeSet<_>>();
            Ok::<_, RuntimeError>((handle, resident_ids))
        })
        .await
        .map_err(|error| RuntimeError::Internal(format!("model worker panicked: {error}")))?;
        let (handle, resident_ids) = match handle_result {
            Ok(result) => result,
            Err(error) => {
                if matches!(error, RuntimeError::Oom(_)) {
                    let events = self
                        .inner
                        .watchdog
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .handle_load_oom();
                    execute_watchdog_events(&self.inner, events);
                }
                return Err(error);
            }
        };
        let info = handle.info.clone();
        let mut models = self
            .inner
            .models
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        models.retain(|id, _| resident_ids.contains(id));
        models.insert(info.id.clone(), handle);
        drop(models);
        self.emit(EngineEvent::ModelLoaded {
            model_id: info.id.clone(),
            vram_mb: 0,
        })?;
        Ok(info)
    }

    pub async fn unload_model(&self, model_id: &str) -> Result<(), RuntimeError> {
        let canonical_id = self.canonical_model_id(model_id)?;
        if !self
            .inner
            .models
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .contains_key(&canonical_id)
        {
            return Err(RuntimeError::ModelNotLoaded(model_id.to_owned()));
        }
        let inner = Arc::clone(&self.inner);
        let unload_id = canonical_id.clone();
        tokio::task::spawn_blocking(move || {
            inner
                .loader
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .unload_with(&unload_id, |resource| {
                    resource
                        .downcast_ref::<ModelWorkerOwner>()
                        .ok_or_else(|| "resident worker resource type mismatch".to_owned())?
                        .shutdown()
                })
                .map_err(loader_error)
        })
        .await
        .map_err(|error| {
            RuntimeError::Internal(format!("model unload worker panicked: {error}"))
        })??;
        self.inner
            .models
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(&canonical_id);
        self.record_post_unload_memory();
        self.emit(EngineEvent::ModelUnloaded {
            model_id: canonical_id,
            vram_mb: 0,
        })?;
        Ok(())
    }

    async fn reap_unhealthy_model(&self, model_id: &str) -> Result<(), RuntimeError> {
        let inner = Arc::clone(&self.inner);
        let unload_id = model_id.to_owned();
        tokio::task::spawn_blocking(move || {
            inner
                .loader
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .unload_with(&unload_id, |resource| {
                    resource
                        .downcast_ref::<ModelWorkerOwner>()
                        .ok_or_else(|| "resident worker resource type mismatch".to_owned())?
                        .shutdown()
                })
                .map_err(loader_error)
        })
        .await
        .map_err(|error| {
            RuntimeError::Internal(format!("model reap worker panicked: {error}"))
        })??;
        self.inner
            .models
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .remove(model_id);
        self.record_post_unload_memory();
        self.emit(EngineEvent::ModelUnloaded {
            model_id: model_id.to_owned(),
            vram_mb: 0,
        })?;
        Ok(())
    }

    pub fn status(&self) -> RuntimeStatus {
        let models = self
            .inner
            .models
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .values()
            .filter(|handle| handle.healthy.load(Ordering::Acquire))
            .map(|handle| handle.info.clone())
            .collect();
        RuntimeStatus {
            draining: self.inner.draining.load(Ordering::Acquire),
            models,
        }
    }

    pub fn slots(&self) -> Vec<SlotStatus> {
        self.inner
            .models
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .values()
            .filter(|handle| handle.healthy.load(Ordering::Acquire))
            .map(|handle| SlotStatus {
                model_id: handle.info.id.clone(),
                busy: handle.busy.load(Ordering::Acquire),
                queue_depth: handle.queued.load(Ordering::Acquire),
                slot_count: handle.slot_count,
                max_batch_sequences: handle.max_batch_sequences.load(Ordering::Acquire),
                background_evicted: handle.background_evicted.load(Ordering::Acquire),
            })
            .collect()
    }

    pub fn set_draining(&self, enabled: bool) {
        self.inner.draining.store(enabled, Ordering::Release);
    }

    pub async fn generate(
        &self,
        mut request: GenerateRequest,
    ) -> Result<GenerationStream, RuntimeError> {
        if self.inner.draining.load(Ordering::Acquire) {
            return Err(RuntimeError::Draining);
        }
        self.assign_request_ids(&mut request);
        let receipt = self.reserve_eval_receipt(&request)?;
        let handle = self.model_handle(request.model.as_deref())?;
        self.submit_generation(request, handle, receipt)
    }

    /// Submits a chat request to the same model worker that rendered its prompt.
    ///
    /// A concurrent unload/reload may disconnect the pinned worker, in which case submission
    /// fails closed. The request is never re-resolved against the mutable loaded-model map.
    pub async fn generate_chat(
        &self,
        mut request: GenerateRequest,
        rendered: PinnedChatPrompt,
    ) -> Result<GenerationStream, RuntimeError> {
        if self.inner.draining.load(Ordering::Acquire) {
            return Err(RuntimeError::Draining);
        }
        if request.model.as_deref() != Some(rendered.requested_model.as_str())
            || request.prompt != rendered.prompt
        {
            return Err(RuntimeError::UnsupportedParam(
                "chat generation request does not match its pinned rendered prompt".to_owned(),
            ));
        }
        self.assign_request_ids(&mut request);
        let receipt = self.reserve_eval_receipt(&request)?;
        self.submit_generation(request, rendered.handle, receipt)
    }

    fn submit_generation(
        &self,
        mut request: GenerateRequest,
        handle: ModelHandle,
        reserved_receipt: Option<ReservedEvalReceipt>,
    ) -> Result<GenerationStream, RuntimeError> {
        request.model = Some(handle.info.id.clone());
        let control = GenerationControl::new(Some(request.deadline));
        let (events, receiver) = bounded_generation_stream(control.clone());
        let request_id = request.request_id.clone();
        let trace_id = request.trace_id.clone();
        let model = handle.info.id.clone();
        let (receipt_identity, execution_identity) = if reserved_receipt.is_some() {
            let (sender, receiver) = oneshot::channel();
            (Some(sender), Some(receiver))
        } else {
            (None, None)
        };
        let pending_receipt =
            reserved_receipt
                .zip(execution_identity)
                .map(|(reserved, execution_identity)| PendingEvalReceipt {
                    authority: reserved.authority,
                    reservation: reserved.reservation,
                    request: request.clone(),
                    engine_instance_id: reserved.engine_instance_id,
                    execution_identity,
                });
        let scheduler_id = self.inner.scheduler_counter.fetch_add(1, Ordering::Relaxed);
        self.inner.requests.insert(
            request_id.clone(),
            RequestRegistration {
                scheduler_id,
                model_id: model.clone(),
                principal_id: request.principal_id.clone(),
                control: control.clone(),
            },
        )?;
        match handle.try_command(ModelCommand::Generate {
            scheduler_id,
            request: Box::new(request),
            control,
            events,
            receipt_identity,
        }) {
            Ok(()) => Ok(GenerationStream {
                request_id,
                trace_id,
                model,
                receiver,
                pending_receipt,
                emitted_output: Vec::new(),
                engine_receipt: None,
                receipt_error: None,
                #[cfg(test)]
                _receipt_test_dir: None,
            }),
            Err(error) => {
                self.inner.requests.remove_scheduler(scheduler_id);
                Err(error)
            }
        }
    }

    fn assign_request_ids(&self, request: &mut GenerateRequest) {
        if request.request_id.is_empty() {
            request.request_id = self.next_request_id();
        }
        if request.trace_id.is_empty() {
            request.trace_id.clone_from(&request.request_id);
        }
    }

    fn reserve_eval_receipt(
        &self,
        request: &GenerateRequest,
    ) -> Result<Option<ReservedEvalReceipt>, RuntimeError> {
        let is_eval = request.priority == PriorityClass::Eval;
        if is_eval != request.eval_context.is_some() || is_eval != request.eval_slot.is_some() {
            return Err(RuntimeError::UnsupportedParam(
                "eval priority, slot, and context must be supplied together".to_owned(),
            ));
        }
        if !is_eval {
            return Ok(None);
        }
        let authority = self
            .inner
            .receipts
            .as_ref()
            .cloned()
            .ok_or(RuntimeError::EvalReceiptUnavailable)?;
        let context = request
            .eval_context
            .as_ref()
            .ok_or(RuntimeError::EvalReceiptUnavailable)?;
        let identity = AttemptIdentity {
            installation_id: authority.identity.installation_id.clone(),
            run_id: context.run_id.clone(),
            suite_id: context.suite_id.clone(),
            case_id: context.case_id.clone(),
            ordinal: context.ordinal,
        };
        let reservation = authority
            .ledger
            .reserve_attempt(&request.request_id, &identity)
            .map_err(|error| match error {
                LedgerError::ReservationConflict => RuntimeError::EvalAttemptConflict,
                other => RuntimeError::EvalReceiptCommit(other.to_string()),
            })?;
        Ok(Some(ReservedEvalReceipt {
            authority,
            reservation,
            engine_instance_id: self.inner.engine_instance_id.clone(),
        }))
    }

    pub fn cancel(&self, request_id: &str) -> bool {
        self.inner
            .requests
            .unambiguous_registration(request_id)
            .is_some_and(|registration| {
                registration.control.cancel();
                true
            })
    }

    pub fn cancel_owned(&self, request_id: &str, principal_id: &str) -> Result<bool, RuntimeError> {
        let registration = self
            .inner
            .requests
            .registration(request_id, principal_id)
            .ok_or_else(|| RuntimeError::SessionUnknown(request_id.to_owned()))?;
        #[cfg(all(
            feature = "contract-test-controls",
            debug_assertions,
            any(feature = "cpu", feature = "cuda")
        ))]
        contract_wait_for_stop_boundary(request_id)?;
        registration.control.cancel();
        #[cfg(all(
            feature = "contract-test-controls",
            debug_assertions,
            any(feature = "cpu", feature = "cuda")
        ))]
        contract_release_stop_boundary(request_id);
        Ok(true)
    }

    pub async fn tokenize(
        &self,
        model: Option<&str>,
        items: Vec<String>,
        add_special: bool,
    ) -> Result<Vec<Vec<i32>>, RuntimeError> {
        let handle = self.model_handle(model)?;
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::Tokenize {
            items,
            add_special,
            reply,
        })?;
        response
            .await
            .map_err(|_| RuntimeError::Internal("model worker dropped tokenize reply".to_owned()))?
    }

    pub async fn embeddings(
        &self,
        model: Option<&str>,
        items: Vec<String>,
    ) -> Result<Vec<Vec<f32>>, RuntimeError> {
        let handle = self.model_handle(model)?;
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::Embed { items, reply })?;
        response.await.map_err(|_| {
            RuntimeError::Internal("model worker dropped embedding reply".to_owned())
        })?
    }

    pub async fn count_tokens(
        &self,
        model: Option<&str>,
        items: Vec<String>,
        add_special: bool,
    ) -> Result<Vec<u32>, RuntimeError> {
        let handle = self.model_handle(model)?;
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::CountTokens {
            items,
            add_special,
            reply,
        })?;
        response.await.map_err(|_| {
            RuntimeError::Internal("model worker dropped token-count reply".to_owned())
        })?
    }

    pub async fn prefix(
        &self,
        model: Option<&str>,
        command: PrefixCommand,
    ) -> Result<PrefixResult, RuntimeError> {
        let handle = self.model_handle(model)?;
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::Prefix { command, reply })?;
        response
            .await
            .map_err(|_| RuntimeError::Internal("model worker dropped prefix reply".to_owned()))?
    }

    pub async fn session(
        &self,
        model: Option<&str>,
        action: SessionAction,
        session_id: String,
    ) -> Result<(), RuntimeError> {
        self.session_owned(model, action, session_id, "local-supervisor".to_owned())
            .await
    }

    pub async fn session_owned(
        &self,
        model: Option<&str>,
        action: SessionAction,
        session_id: String,
        principal_id: String,
    ) -> Result<(), RuntimeError> {
        let handle = self.model_handle(model)?;
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::Session {
            action,
            session_id,
            principal_id,
            reply,
        })?;
        response
            .await
            .map_err(|_| RuntimeError::Internal("model worker dropped session reply".to_owned()))?
    }

    pub fn register_lora(
        &self,
        registration: AdapterRegistration,
    ) -> Result<VerifiedAdapterRecord, RuntimeError> {
        self.inner
            .adapters
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .register(registration)
            .map_err(adapter_registry_error)
    }

    async fn apply_registered_lora(
        &self,
        handle: ModelHandle,
        adapter: Option<VerifiedLoadGuard>,
    ) -> Result<(), RuntimeError> {
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::Lora { adapter, reply })?;
        response
            .await
            .map_err(|_| RuntimeError::Internal("model worker dropped LoRA reply".to_owned()))?
    }

    pub async fn swap_registered_lora(
        &self,
        model_id: &str,
        adapter_id: Option<&str>,
    ) -> Result<(), RuntimeError> {
        let handle = self.model_handle(Some(model_id))?;
        let adapter = adapter_id
            .map(|adapter_id| {
                let base_model_sha256 = digest_hex(handle.info.model_fingerprint);
                self.inner
                    .adapters
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .resolve_for_base(adapter_id, &base_model_sha256)
                    .map_err(adapter_registry_error)
            })
            .transpose()?;
        self.apply_registered_lora(handle, adapter).await
    }

    pub async fn render_chat(
        &self,
        model: Option<&str>,
        messages: Vec<(String, String)>,
    ) -> Result<PinnedChatPrompt, RuntimeError> {
        let handle = self.model_handle(model)?;
        let requested_model = model.ok_or_else(|| {
            RuntimeError::Internal(
                "model selection succeeded without a requested model identifier".to_owned(),
            )
        })?;
        Self::render_chat_on_handle(requested_model.to_owned(), handle, messages).await
    }

    async fn render_chat_on_handle(
        requested_model: String,
        handle: ModelHandle,
        messages: Vec<(String, String)>,
    ) -> Result<PinnedChatPrompt, RuntimeError> {
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::RenderChat { messages, reply })?;
        let prompt = response.await.map_err(|_| {
            RuntimeError::Internal("model worker dropped chat-template reply".to_owned())
        })??;
        Ok(PinnedChatPrompt {
            prompt,
            requested_model,
            handle,
        })
    }

    #[cfg(all(test, any(feature = "cpu", feature = "cuda")))]
    async fn terminate_worker_for_test(&self, model_id: &str) -> Result<(), RuntimeError> {
        let handle = self.model_handle(Some(model_id))?;
        let (reply, response) = oneshot::channel();
        handle.try_command(ModelCommand::TerminateWorkerForTest { reply })?;
        response.await.map_err(|_| {
            RuntimeError::Internal("model worker dropped test termination reply".to_owned())
        })
    }

    fn model_handle(&self, requested: Option<&str>) -> Result<ModelHandle, RuntimeError> {
        let requested = requested.ok_or_else(|| {
            RuntimeError::UnsupportedParam(
                "model must be specified because no governed default model is configured"
                    .to_owned(),
            )
        })?;
        let canonical = self.canonical_model_id(requested)?;
        let models = self
            .inner
            .models
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        models
            .get(&canonical)
            .cloned()
            .ok_or_else(|| RuntimeError::ModelNotLoaded(requested.to_owned()))
    }

    fn canonical_model_id(&self, requested: &str) -> Result<String, RuntimeError> {
        self.inner
            .catalog
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .try_resolve(requested)
            .map_err(registry_error)?
            .map(|record| record.id.clone())
            .ok_or_else(|| RuntimeError::ModelNotLoaded(requested.to_owned()))
    }

    fn record_post_unload_memory(&self) {
        let resident_bytes = process_resident_bytes();
        let observation = self
            .inner
            .leak_monitor
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .record_post_unload(resident_bytes);
        if let Some(observation) = observation {
            tracing::warn!(
                first_resident_bytes = observation.first_resident_bytes,
                latest_resident_bytes = observation.latest_resident_bytes,
                growth_bytes = observation.growth_bytes,
                sample_count = observation.sample_count,
                "sustained resident-memory growth detected after model unload"
            );
        }
    }

    fn emit(&self, event: EngineEvent) -> Result<(), RuntimeError> {
        self.inner
            .telemetry
            .emit(EventEnvelope::new(now(), event))
            .map(|_| ())
            .map_err(|error| {
                self.inner.metrics.record_telemetry_emission_failure();
                tracing::error!(error = %error, "engine telemetry emission failed");
                RuntimeError::Internal(format!("telemetry emission failed: {error}"))
            })
    }
}

fn emit_observable(
    telemetry: &TelemetryHub,
    metrics: &MetricsHub,
    event: EngineEvent,
    operation: &'static str,
) {
    if let Err(error) = telemetry.emit(EventEnvelope::new(now(), event)) {
        metrics.record_telemetry_emission_failure();
        tracing::error!(error = %error, operation, "engine telemetry emission failed");
    }
}

impl ModelHandle {
    fn try_command(&self, command: ModelCommand) -> Result<(), RuntimeError> {
        if !self.healthy.load(Ordering::Acquire) {
            return Err(RuntimeError::Internal(
                "model worker is unhealthy; unload and reload the model".to_owned(),
            ));
        }
        self.queued.fetch_add(1, Ordering::AcqRel);
        match self.sender.try_send(command) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(_)) => {
                self.queued.fetch_sub(1, Ordering::AcqRel);
                Err(RuntimeError::QueueFull)
            }
            Err(TrySendError::Disconnected(_)) => {
                self.queued.fetch_sub(1, Ordering::AcqRel);
                Err(RuntimeError::Internal(
                    "model worker is unavailable".to_owned(),
                ))
            }
        }
    }
}

fn start_watchdog_poll(
    inner: Weak<RuntimeInner>,
) -> Result<std::thread::JoinHandle<()>, RuntimeError> {
    std::thread::Builder::new()
        .name("amw-engine-watchdog".to_owned())
        .spawn(move || loop {
            std::thread::sleep(Duration::from_secs(WATCHDOG_POLL_INTERVAL_SECS));
            let Some(inner) = inner.upgrade() else {
                break;
            };
            let events = inner
                .watchdog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .poll();
            execute_watchdog_events(&inner, events);
        })
        .map_err(|error| RuntimeError::Internal(format!("cannot start watchdog thread: {error}")))
}

fn execute_watchdog_events(
    inner: &Arc<RuntimeInner>,
    events: impl IntoIterator<Item = WatchdogEvent>,
) {
    for event in events {
        tracing::warn!(
            sequence_id = event.sequence_id,
            action = ?event.action,
            detail = event.detail,
            trace_id = event.trace.trace_id.as_str(),
            request_id = event.trace.request_id.as_str(),
            "engine watchdog action"
        );
        let registration = inner.requests.registration_by_scheduler(event.sequence_id);
        match event.action {
            WatchdogAction::FailForegroundForRetry
            | WatchdogAction::KillSequence
            | WatchdogAction::EmitOom => {
                if let Some(registration) = registration {
                    registration.control.cancel();
                }
            }
            WatchdogAction::ResetSlot => {
                if let Some(registration) = registration {
                    registration.control.cancel();
                    if let Some(handle) = inner
                        .models
                        .read()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .get(&registration.model_id)
                        .cloned()
                    {
                        let _ = handle.try_command(ModelCommand::ResetSequence {
                            scheduler_id: registration.scheduler_id,
                        });
                    }
                }
            }
            WatchdogAction::EvictLeastRecentlyUsed => {
                if let Err(error) = evict_idle_model(inner) {
                    tracing::error!(
                        sequence_id = event.sequence_id,
                        error = %error,
                        "watchdog background eviction failed"
                    );
                }
            }
            WatchdogAction::ExitProcess => std::process::exit(75),
            WatchdogAction::ResumedFromSleep
            | WatchdogAction::ResumeBackground
            | WatchdogAction::ReduceActiveSlots
            | WatchdogAction::RestoreActiveSlots => {}
        }
    }
}

fn evict_idle_model(inner: &Arc<RuntimeInner>) -> Result<String, WatchdogCallbackError> {
    let candidate = inner
        .models
        .read()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .iter()
        .find(|(_, handle)| {
            handle.busy.load(Ordering::Acquire) == 0 && handle.queued.load(Ordering::Acquire) == 0
        })
        .map(|(model_id, _)| model_id.clone());
    let Some(model_id) = candidate else {
        return Err(WatchdogCallbackError::new(
            "no idle model was available for watchdog eviction",
        ));
    };
    let unload_result = inner
        .loader
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .unload_with(&model_id, |resource| {
            resource
                .downcast_ref::<ModelWorkerOwner>()
                .ok_or_else(|| "resident worker resource type mismatch".to_owned())?
                .shutdown()
        });
    unload_result.map_err(|error| {
        WatchdogCallbackError::new(format!(
            "idle model {model_id} could not be evicted: {error}"
        ))
    })?;
    inner
        .models
        .write()
        .unwrap_or_else(|poisoned| poisoned.into_inner())
        .remove(&model_id);
    emit_observable(
        &inner.telemetry,
        &inner.metrics,
        EngineEvent::ModelUnloaded {
            model_id: model_id.clone(),
            vram_mb: 0,
        },
        "watchdog_model_eviction",
    );
    Ok(model_id)
}

struct ModelWorkerServices {
    requests: Arc<RequestRegistry>,
    watchdog: Arc<Mutex<Watchdog<SystemUptimeClock>>>,
    global_slots: Arc<GlobalSlotArbiter>,
    telemetry: TelemetryHub,
    metrics: MetricsHub,
    adapters: Arc<Mutex<AdapterRegistry>>,
    sessions: Arc<SessionStore>,
}

fn start_model_worker(
    model_record: ModelRecord,
    verified: VerifiedModelFile,
    draft_record: Option<(ModelRecord, GgufMetadata)>,
    config: EngineConfig,
    services: ModelWorkerServices,
) -> Result<ModelWorkerOwner, RuntimeError> {
    let path = verified.source_path().to_owned();
    let metadata = verified.metadata().clone();
    let load_timeout = model_load_timeout(metadata.file_size_bytes);
    let mut info = model_info(
        model_record.id.clone(),
        path.clone(),
        &metadata,
        config.slots.default_ctx,
    )?;
    info.model_fingerprint = verified.sha256();
    let draft_record = draft_record
        .map(|(record, _)| {
            let source = VerifiedModelFile::open(&record.path).map_err(|error| {
                RuntimeError::ModelCorrupt {
                    path: record.path.clone(),
                    reason: error.to_string(),
                }
            })?;
            Ok::<_, RuntimeError>((record, source))
        })
        .transpose()?;
    let capacity = config
        .slots
        .count
        .saturating_mul(MODEL_COMMAND_QUEUE_MULTIPLIER)
        .max(1);
    let slot_count = config.slots.count;
    let (sender, receiver) = mpsc::sync_channel(capacity);
    let (ready, loaded) = mpsc::sync_channel(1);
    let queued = Arc::new(AtomicUsize::new(0));
    let busy = Arc::new(AtomicUsize::new(0));
    let healthy = Arc::new(AtomicBool::new(false));
    let max_batch_sequences = Arc::new(AtomicUsize::new(0));
    let background_evicted = Arc::new(AtomicUsize::new(0));
    let worker_queued = Arc::clone(&queued);
    let worker_busy = Arc::clone(&busy);
    let worker_healthy = Arc::clone(&healthy);
    let worker_max_batch_sequences = Arc::clone(&max_batch_sequences);
    let worker_background_evicted = Arc::clone(&background_evicted);
    let worker_info = info.clone();
    let ModelWorkerServices {
        requests,
        watchdog,
        global_slots,
        telemetry,
        metrics,
        adapters,
        sessions,
    } = services;
    let join = std::thread::Builder::new()
        .name(format!("amw-model-{}", info.id))
        .spawn(move || {
            run_model_worker(
                verified,
                worker_info,
                model_record,
                draft_record,
                config,
                receiver,
                ready,
                worker_queued,
                worker_busy,
                worker_healthy,
                worker_max_batch_sequences,
                worker_background_evicted,
                requests,
                watchdog,
                global_slots,
                telemetry,
                metrics,
                adapters,
                sessions,
            );
        })
        .map_err(|error| RuntimeError::Internal(format!("cannot start model worker: {error}")))?;
    match loaded.recv_timeout(load_timeout) {
        Ok(Ok(())) => {}
        Ok(Err(error)) => {
            drop(sender);
            let _ = join.join();
            return Err(error);
        }
        Err(RecvTimeoutError::Disconnected) => {
            drop(sender);
            join.join().map_err(|_| {
                RuntimeError::Internal("model worker panicked during load".to_owned())
            })?;
            return Err(RuntimeError::Internal(
                "model worker failed before load handshake".to_owned(),
            ));
        }
        Err(RecvTimeoutError::Timeout) => {
            drop(sender);
            std::thread::Builder::new()
                .name(format!("amw-model-load-reaper-{}", info.id))
                .spawn(move || {
                    if join.join().is_err() {
                        tracing::error!("timed-out model worker panicked during cleanup");
                    }
                })
                .map_err(|error| {
                    RuntimeError::Internal(format!(
                        "cannot supervise timed-out model worker cleanup: {error}"
                    ))
                })?;
            return Err(RuntimeError::EvalTimeout);
        }
    }
    let handle = ModelHandle {
        info,
        sender,
        healthy,
        queued,
        busy,
        max_batch_sequences,
        background_evicted,
        slot_count,
    };
    Ok(ModelWorkerOwner {
        handle,
        join: Mutex::new(Some(join)),
    })
}

#[cfg(not(any(feature = "cpu", feature = "cuda")))]
#[allow(clippy::too_many_arguments)]
fn run_model_worker(
    _source: VerifiedModelFile,
    _info: ModelInfo,
    _model_record: ModelRecord,
    _draft_record: Option<(ModelRecord, VerifiedModelFile)>,
    _config: EngineConfig,
    _receiver: Receiver<ModelCommand>,
    ready: SyncSender<Result<(), RuntimeError>>,
    _queued: Arc<AtomicUsize>,
    _busy: Arc<AtomicUsize>,
    _healthy: Arc<AtomicBool>,
    _max_batch_sequences: Arc<AtomicUsize>,
    _background_evicted: Arc<AtomicUsize>,
    _requests: Arc<RequestRegistry>,
    _watchdog: Arc<Mutex<Watchdog<SystemUptimeClock>>>,
    _global_slots: Arc<GlobalSlotArbiter>,
    _telemetry: TelemetryHub,
    _metrics: MetricsHub,
    _adapters: Arc<Mutex<AdapterRegistry>>,
    _sessions: Arc<SessionStore>,
) {
    let _ = ready.send(Err(RuntimeError::NativeUnavailable));
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone, Debug, Eq, PartialEq)]
enum ReplayOrigin {
    FreshPrompt,
    RestoredSession { session_id: String },
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Debug, Eq, PartialEq)]
struct ReadmissionProjection {
    replay_tokens: Vec<i32>,
    remaining_outputs: u32,
    scheduler_decode_steps: u32,
    next_token: Option<i32>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn project_background_readmission(
    replay_origin: &ReplayOrigin,
    original_tokens: &[i32],
    generated_tokens: &[i32],
    next_token: Option<i32>,
    max_tokens: u32,
    context_limit: u32,
) -> Result<ReadmissionProjection, RuntimeError> {
    if replay_origin != &ReplayOrigin::FreshPrompt {
        return Err(RuntimeError::Internal(
            "restored-session generation was pressure-evicted despite being ineligible".to_owned(),
        ));
    }
    let pending_token_count = usize::from(next_token.is_some());
    if next_token.is_some() && generated_tokens.last().copied() != next_token {
        return Err(RuntimeError::Internal(
            "scheduler readmission pending token is not the latest sampled token".to_owned(),
        ));
    }
    if next_token.is_none() && !generated_tokens.is_empty() {
        return Err(RuntimeError::Internal(
            "scheduler evicted completed output without a pending decode token".to_owned(),
        ));
    }
    let generated_count = u32::try_from(generated_tokens.len()).map_err(|_| {
        RuntimeError::Internal("scheduler readmission token history exceeds u32".to_owned())
    })?;
    let remaining_outputs = max_tokens.checked_sub(generated_count).ok_or_else(|| {
        RuntimeError::Internal(
            "scheduler readmission exceeded the request output budget".to_owned(),
        )
    })?;
    if remaining_outputs == 0 {
        return Err(RuntimeError::Internal(
            "scheduler evicted a generation with no remaining output".to_owned(),
        ));
    }
    let scheduler_decode_steps = if pending_token_count == 1 {
        remaining_outputs
    } else {
        remaining_outputs.saturating_sub(1)
    };
    let committed_generated = generated_tokens.len().saturating_sub(pending_token_count);
    let mut replay_tokens = original_tokens.to_vec();
    replay_tokens.extend_from_slice(&generated_tokens[..committed_generated]);
    let replay_prompt_tokens =
        u32::try_from(replay_tokens.len()).map_err(|_| RuntimeError::ContextOverflow {
            requested: u32::MAX,
            limit: context_limit,
        })?;
    let accounted_tokens = replay_prompt_tokens.checked_add(remaining_outputs).ok_or(
        RuntimeError::ContextOverflow {
            requested: u32::MAX,
            limit: context_limit,
        },
    )?;
    if accounted_tokens > context_limit {
        return Err(RuntimeError::ContextOverflow {
            requested: accounted_tokens,
            limit: context_limit,
        });
    }
    Ok(ReadmissionProjection {
        replay_tokens,
        remaining_outputs,
        scheduler_decode_steps,
        next_token,
    })
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct PendingGeneration {
    request: GenerateRequest,
    tokens: Vec<i32>,
    original_tokens: Option<Vec<i32>>,
    request_prompt_tokens: usize,
    scheduler_max_tokens: u32,
    scheduler_decode_steps: u32,
    grammar: Option<CompiledGrammar>,
    prefix_name: Option<String>,
    session_payload: Option<Vec<u8>>,
    control: GenerationControl,
    events: GenerationSender,
    queued_at: Instant,
    executor: Option<GenerationExecutor<NativeDecodeBackend>>,
    next_token: Option<i32>,
    generated_tokens: Vec<i32>,
    readmission_count: u8,
    admitted_at: Option<Instant>,
    prefill_completed_at: Option<Instant>,
    prefix_hit_tokens: u32,
    speculation_proposed_tokens: u32,
    speculation_accepted_tokens: u32,
    speculation: SpeculationPlan,
    speculation_version: u64,
    replay_origin: ReplayOrigin,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct ActiveGeneration {
    scheduler_id: u64,
    request: GenerateRequest,
    tokens: Vec<i32>,
    prefill_tokens: Vec<i32>,
    prompt_cursor: usize,
    position_base: u32,
    next_token: Option<i32>,
    executor: GenerationExecutor<NativeDecodeBackend>,
    control: GenerationControl,
    grammar: Option<CompiledGrammar>,
    generated_tokens: Vec<i32>,
    readmission_count: u8,
    suppress_replay_sample: bool,
    replay_origin: ReplayOrigin,
    events: GenerationSender,
    queued_at: Instant,
    admitted_at: Instant,
    prefill_completed_at: Option<Instant>,
    slot_id: Option<usize>,
    prefix_hit_tokens: u32,
    speculation_proposed_tokens: u32,
    speculation_accepted_tokens: u32,
    speculation: SpeculationPlan,
    speculation_version: u64,
    optimistic_bonus: Option<i32>,
    permit: Option<GlobalSlotPermit>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct AdmissionPermit {
    permit: GlobalSlotPermit,
    borrowed_from_suspended: bool,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct DraftActor {
    commands: Option<SyncSender<DraftActorCommand>>,
    results: Receiver<DraftActorResult>,
    minimum_versions: Arc<Mutex<BTreeMap<SeqId, u64>>>,
    pending_removals: Arc<Mutex<BTreeSet<(SeqId, u64)>>>,
    jobs_in_flight: Arc<AtomicUsize>,
    join: Option<std::thread::JoinHandle<()>>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
enum DraftActorCommand {
    Configure {
        sequence_id: SeqId,
        owner_id: u64,
        version: u64,
        params: SamplerParams,
        grammar: Option<CompiledGrammar>,
    },
    Propose {
        owner_id: u64,
        job: DraftJob,
    },
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct DraftActorResult {
    sequence_id: SeqId,
    owner_id: u64,
    version: u64,
    started_at: Instant,
    finished_at: Instant,
    result: Result<DraftResult, GenError>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl DraftActor {
    fn spawn<F>(factory: F) -> Result<Self, RuntimeError>
    where
        F: FnOnce() -> Result<DraftModelProposer<NativeDraftBackend>, GenError> + Send + 'static,
    {
        let (commands, command_receiver) =
            mpsc::sync_channel::<DraftActorCommand>(DRAFT_ACTOR_COMMAND_CAPACITY);
        let (results, result_receiver) = mpsc::sync_channel(MAX_DRAFT_ACTOR_IN_FLIGHT);
        let (initialized, initialization) = mpsc::sync_channel(1);
        let minimum_versions = Arc::new(Mutex::new(BTreeMap::<SeqId, u64>::new()));
        let actor_versions = Arc::clone(&minimum_versions);
        let pending_removals = Arc::new(Mutex::new(BTreeSet::<(SeqId, u64)>::new()));
        let actor_removals = Arc::clone(&pending_removals);
        let jobs_in_flight = Arc::new(AtomicUsize::new(0));
        let actor_jobs_in_flight = Arc::clone(&jobs_in_flight);
        let join = std::thread::Builder::new()
            .name("amw-draft-actor".to_owned())
            .spawn(move || {
                let mut proposer = match factory() {
                    Ok(proposer) => {
                        let _ = initialized.send(Ok::<(), GenError>(()));
                        proposer
                    }
                    Err(error) => {
                        let _ = initialized.send(Err(error));
                        return;
                    }
                };
                let mut configured_owners = BTreeMap::<SeqId, u64>::new();
                loop {
                    let removals = actor_removals
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .iter()
                        .copied()
                        .collect::<Vec<_>>();
                    for (sequence_id, owner_id) in removals {
                        match configured_owners.get(&sequence_id).copied() {
                            Some(configured_owner) if configured_owner == owner_id => {
                                if proposer.backend_mut().remove_sequence(sequence_id).is_err() {
                                    return;
                                }
                                configured_owners.remove(&sequence_id);
                                actor_removals
                                    .lock()
                                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                                    .remove(&(sequence_id, owner_id));
                            }
                            Some(_) => {
                                actor_removals
                                    .lock()
                                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                                    .remove(&(sequence_id, owner_id));
                            }
                            None => {}
                        }
                    }
                    let command = match command_receiver.recv_timeout(Duration::from_millis(1)) {
                        Ok(command) => command,
                        Err(RecvTimeoutError::Timeout) => continue,
                        Err(RecvTimeoutError::Disconnected) => break,
                    };
                    let job = match command {
                        DraftActorCommand::Configure {
                            sequence_id,
                            owner_id,
                            version,
                            params,
                            grammar,
                        } => {
                            let retired_before_configure = actor_removals
                                .lock()
                                .unwrap_or_else(|poisoned| poisoned.into_inner())
                                .remove(&(sequence_id, owner_id));
                            if retired_before_configure {
                                continue;
                            }
                            let started_at = Instant::now();
                            let configured = proposer.backend_mut().configure_sequence(
                                sequence_id,
                                &params,
                                grammar.as_ref(),
                            );
                            if let Err(error) = configured {
                                let _ = results.try_send(DraftActorResult {
                                    sequence_id,
                                    owner_id,
                                    version,
                                    started_at,
                                    finished_at: Instant::now(),
                                    result: Err(error),
                                });
                            } else {
                                configured_owners.insert(sequence_id, owner_id);
                            }
                            continue;
                        }
                        DraftActorCommand::Propose { owner_id, job } => (owner_id, job),
                    };
                    let (owner_id, job) = job;
                    let minimum_version = actor_versions
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .get(&job.sequence_id)
                        .copied()
                        .unwrap_or(0);
                    if job.version < minimum_version {
                        actor_jobs_in_flight.fetch_sub(1, Ordering::AcqRel);
                        continue;
                    }
                    let sequence_id = job.sequence_id;
                    let version = job.version;
                    let started_at = Instant::now();
                    let result = proposer.propose(&job).and_then(|result| {
                        job.validate_result(&result)?;
                        if result.retained_bytes() > MAX_SPECULATION_WORKER_RETAINED_BYTES {
                            return Err(GenError::SpeculationInvalid(
                                "draft actor result exceeds its retained-memory bound",
                            ));
                        }
                        Ok(result)
                    });
                    let finished_at = Instant::now();
                    let still_current = actor_versions
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .get(&sequence_id)
                        .copied()
                        .unwrap_or(0)
                        <= version;
                    if still_current {
                        let _ = results.try_send(DraftActorResult {
                            sequence_id,
                            owner_id,
                            version,
                            started_at,
                            finished_at,
                            result,
                        });
                    }
                    actor_jobs_in_flight.fetch_sub(1, Ordering::AcqRel);
                }
            })
            .map_err(|error| {
                RuntimeError::Internal(format!("failed to start draft actor: {error}"))
            })?;
        initialization
            .recv()
            .map_err(|_| RuntimeError::Internal("draft actor failed during startup".to_owned()))?
            .map_err(gen_error)?;
        Ok(Self {
            commands: Some(commands),
            results: result_receiver,
            minimum_versions,
            pending_removals,
            jobs_in_flight,
            join: Some(join),
        })
    }

    fn try_submit(&self, owner_id: u64, job: DraftJob) -> Result<bool, RuntimeError> {
        let Some(commands) = self.commands.as_ref() else {
            return Ok(false);
        };
        if self
            .jobs_in_flight
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
                (current < MAX_DRAFT_ACTOR_IN_FLIGHT).then_some(current + 1)
            })
            .is_err()
        {
            return Ok(false);
        }
        match commands.try_send(DraftActorCommand::Propose { owner_id, job }) {
            Ok(()) => Ok(true),
            Err(TrySendError::Full(_)) => {
                self.jobs_in_flight.fetch_sub(1, Ordering::AcqRel);
                Ok(false)
            }
            Err(TrySendError::Disconnected(_)) => {
                self.jobs_in_flight.fetch_sub(1, Ordering::AcqRel);
                Err(RuntimeError::Internal(
                    "draft actor disconnected".to_owned(),
                ))
            }
        }
    }

    fn configure(
        &self,
        sequence_id: SeqId,
        owner_id: u64,
        minimum_version: u64,
        params: SamplerParams,
        grammar: Option<CompiledGrammar>,
    ) -> Result<(), RuntimeError> {
        let Some(commands) = self.commands.as_ref() else {
            return Err(RuntimeError::Internal(
                "draft actor is unavailable".to_owned(),
            ));
        };
        self.minimum_versions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert(sequence_id, minimum_version);
        match commands.try_send(DraftActorCommand::Configure {
            sequence_id,
            owner_id,
            version: minimum_version,
            params,
            grammar,
        }) {
            Ok(()) => Ok(()),
            Err(TrySendError::Full(_)) => Err(RuntimeError::QueueFull),
            Err(TrySendError::Disconnected(_)) => Err(RuntimeError::Internal(
                "draft actor disconnected".to_owned(),
            )),
        }
    }

    fn remove(&self, sequence_id: SeqId, owner_id: u64) {
        self.pending_removals
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .insert((sequence_id, owner_id));
    }

    fn invalidate(&self, sequence_id: SeqId, minimum_version: u64) {
        let mut versions = self
            .minimum_versions
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let current = versions.entry(sequence_id).or_default();
        *current = (*current).max(minimum_version);
    }

    fn try_result(&self) -> Result<Option<DraftActorResult>, RuntimeError> {
        match self.results.try_recv() {
            Ok(result) => Ok(Some(result)),
            Err(TryRecvError::Empty) => Ok(None),
            Err(TryRecvError::Disconnected) => Err(RuntimeError::Internal(
                "draft actor result channel disconnected".to_owned(),
            )),
        }
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl Drop for DraftActor {
    fn drop(&mut self) {
        self.commands.take();
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct NativePrefix {
    content_hash: String,
    tokens: Vec<i32>,
    pinned: bool,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct NativeWorker {
    info: ModelInfo,
    model_record: ModelRecord,
    draft_actor: Option<DraftActor>,
    draft_receipt_identity: Option<SpeculationReceiptIdentity>,
    draft_results: BTreeMap<(SeqId, u64), DraftActorResult>,
    prompt_lookup: PromptLookupProposer,
    config: EngineConfig,
    model: Model,
    context: Context,
    embedding_context: Option<Context>,
    batch: Batch,
    scheduler: SchedulerCore,
    pending: BTreeMap<u64, PendingGeneration>,
    active: BTreeMap<SeqId, ActiveGeneration>,
    prefixes: BTreeMap<String, NativePrefix>,
    sessions: Arc<SessionStore>,
    model_fingerprint: [u8; 32],
    native_sequence_capacity: u32,
    lora: Option<ActiveNativeLora>,
    adapters: Arc<Mutex<AdapterRegistry>>,
    chat_template: TemplateVerdict,
    step: u64,
    requests: Arc<RequestRegistry>,
    watchdog: Arc<Mutex<Watchdog<SystemUptimeClock>>>,
    global_slots: Arc<GlobalSlotArbiter>,
    suspended_permits: Vec<GlobalSlotPermit>,
    max_batch_sequences: Arc<AtomicUsize>,
    background_evicted: Arc<AtomicUsize>,
    telemetry: TelemetryHub,
    metrics: MetricsHub,
    unhealthy: bool,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct ActiveNativeLora {
    adapter: LoraAdapter,
    guard: VerifiedLoadGuard,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct DecodeOutput {
    seq_id: SeqId,
    output_index: i32,
    phase: SequencePhase,
    compute_grant: u32,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct SpeculativeSeed {
    output: DecodeOutput,
    proposal: DraftProposal,
    sampler: NativeSamplerTxn,
    probes: Vec<TargetProbe>,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct SpeculativeCandidate {
    output: DecodeOutput,
    proposal: DraftProposal,
    fork: TargetKvFork,
    sampler: NativeSamplerTxn,
    probes: Vec<TargetProbe>,
    verification_rows: Vec<i32>,
    optimistic_queued: bool,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
struct PreparedSpeculativeCandidate {
    candidate: SpeculativeCandidate,
    decision: SpeculationDecision,
    bundle: Vec<ExternalBundleToken>,
    preview: ExternalBundlePreview,
    compute_tokens: u32,
    next_version: u64,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[derive(Clone)]
enum TerminalKind {
    Normal(StopReason),
    Failed,
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
#[allow(clippy::too_many_arguments)]
fn run_model_worker(
    source: VerifiedModelFile,
    info: ModelInfo,
    model_record: ModelRecord,
    draft_record: Option<(ModelRecord, VerifiedModelFile)>,
    config: EngineConfig,
    receiver: Receiver<ModelCommand>,
    ready: SyncSender<Result<(), RuntimeError>>,
    queued: Arc<AtomicUsize>,
    busy: Arc<AtomicUsize>,
    healthy: Arc<AtomicBool>,
    max_batch_sequences: Arc<AtomicUsize>,
    background_evicted: Arc<AtomicUsize>,
    requests: Arc<RequestRegistry>,
    watchdog: Arc<Mutex<Watchdog<SystemUptimeClock>>>,
    global_slots: Arc<GlobalSlotArbiter>,
    telemetry: TelemetryHub,
    metrics: MetricsHub,
    adapters: Arc<Mutex<AdapterRegistry>>,
    sessions: Arc<SessionStore>,
) {
    let mut worker = match NativeWorker::load(
        source,
        info,
        model_record,
        draft_record,
        config,
        requests,
        watchdog,
        global_slots,
        max_batch_sequences,
        background_evicted,
        telemetry,
        metrics,
        adapters,
        sessions,
    ) {
        Ok(worker) => worker,
        Err(error) => {
            let _ = ready.send(Err(error));
            return;
        }
    };
    healthy.store(true, Ordering::Release);
    let health_guard = WorkerHealthGuard {
        healthy: Arc::clone(&healthy),
    };
    if ready.send(Ok(())).is_err() {
        return;
    }
    worker.run(receiver, &queued, &busy);
    drop(health_guard);
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl NativeWorker {
    #[allow(clippy::too_many_arguments)]
    fn load(
        source: VerifiedModelFile,
        info: ModelInfo,
        model_record: ModelRecord,
        draft_record: Option<(ModelRecord, VerifiedModelFile)>,
        config: EngineConfig,
        requests: Arc<RequestRegistry>,
        watchdog: Arc<Mutex<Watchdog<SystemUptimeClock>>>,
        global_slots: Arc<GlobalSlotArbiter>,
        max_batch_sequences: Arc<AtomicUsize>,
        background_evicted: Arc<AtomicUsize>,
        telemetry: TelemetryHub,
        metrics: MetricsHub,
        adapters: Arc<Mutex<AdapterRegistry>>,
        sessions: Arc<SessionStore>,
    ) -> Result<Self, RuntimeError> {
        let model_fingerprint = info.model_fingerprint;
        let source_path = source.source_path().to_owned();
        let model = Model::load_verified(source).map_err(|error| RuntimeError::ModelCorrupt {
            path: source_path,
            reason: error.to_string(),
        })?;
        let context_tokens = config
            .slots
            .default_ctx
            .checked_mul(config.slots.count as u32)
            .ok_or_else(|| RuntimeError::Internal("native context size overflows".to_owned()))?;
        let batch_tokens = config.scheduler.batch_token_budget.max(1);
        let native_sequence_capacity = native_sequence_capacity(&config)?;
        let context = model
            .context_with(ContextOptions {
                context_tokens,
                batch_tokens,
                micro_batch_tokens: batch_tokens,
                max_sequences: native_sequence_capacity,
                unified_kv: true,
                embeddings: false,
                pooling: None,
            })
            .map_err(|error| RuntimeError::Internal(format!("native context failed: {error}")))?;
        let batch = Batch::tokens(
            i32::try_from(batch_tokens).map_err(|_| {
                RuntimeError::Internal("batch size exceeds native range".to_owned())
            })?,
            i32::try_from(config.slots.count).map_err(|_| {
                RuntimeError::Internal("slot count exceeds native range".to_owned())
            })?,
        )
        .map_err(|error| RuntimeError::Internal(format!("native batch failed: {error}")))?;
        let target_vocabulary_fingerprint = model.vocabulary_fingerprint().map_err(|error| {
            RuntimeError::Internal(format!("target vocabulary fingerprint failed: {error}"))
        })?;
        let target_context_capacity = context.metadata().context_tokens as usize;
        let draft = draft_record
            .map(|(draft_record, draft_source)| {
                let draft_model_id = draft_record.id;
                let draft_model_sha256 = Digest32::from_bytes(draft_source.sha256());
                let pair_record = model_record.clone();
                let pair = pair_record.draft_pair.as_ref().ok_or_else(|| {
                    RuntimeError::EvalReceiptAuthority(
                        "loaded draft model has no governed pair identity".to_owned(),
                    )
                })?;
                let vocabulary_fingerprint = Digest32::from_lower_hex(
                    pair.vocabulary_fingerprint.as_deref().ok_or_else(|| {
                        RuntimeError::EvalReceiptAuthority(
                            "loaded draft model has no vocabulary identity".to_owned(),
                        )
                    })?,
                )
                .map_err(|error| RuntimeError::EvalReceiptAuthority(error.to_string()))?;
                let draft_receipt_identity = SpeculationReceiptIdentity::DraftModel {
                    model_id: draft_model_id.clone(),
                    model_sha256: draft_model_sha256,
                    minimum_context: pair.minimum_context,
                    vocabulary_fingerprint,
                };
                let expected_fingerprint = target_vocabulary_fingerprint.clone();
                let draft_context = ContextOptions {
                    context_tokens,
                    batch_tokens,
                    micro_batch_tokens: batch_tokens,
                    max_sequences: native_sequence_capacity,
                    unified_kv: true,
                    embeddings: false,
                    pooling: None,
                };
                let actor = DraftActor::spawn(move || {
                    let backend = NativeDraftBackend::load_verified(draft_source, draft_context)?;
                    let compatibility = DraftModelCompatibility {
                        model_id: draft_model_id,
                        vocabulary_fingerprint: backend
                            .model()
                            .vocabulary_fingerprint()
                            .map_err(|error| GenError::Backend(error.to_string()))?,
                        context_capacity: backend.context_capacity(),
                    };
                    let mode = resolve_draft_mode(
                        &pair_record,
                        &expected_fingerprint,
                        target_context_capacity,
                        Some(&compatibility),
                    )?;
                    let DraftMode::DraftModel(model_id) = mode else {
                        return Err(GenError::SpeculationInvalid(
                            "configured draft pair resolved to prompt lookup",
                        ));
                    };
                    DraftModelProposer::new(model_id, backend)
                })?;
                Ok::<_, RuntimeError>((actor, draft_receipt_identity))
            })
            .transpose()?;
        let (draft_actor, draft_receipt_identity) = draft
            .map(|(actor, identity)| (Some(actor), Some(identity)))
            .unwrap_or((None, None));
        let scheduler = scheduler(&config, &info.id)?;
        let chat_template = TemplatePolicy.evaluate(&info.id, info.chat_template.as_deref());
        Ok(Self {
            info,
            model_record,
            draft_actor,
            draft_receipt_identity,
            draft_results: BTreeMap::new(),
            prompt_lookup: PromptLookupProposer::default(),
            config,
            model,
            context,
            embedding_context: None,
            batch,
            scheduler,
            pending: BTreeMap::new(),
            active: BTreeMap::new(),
            prefixes: BTreeMap::new(),
            sessions,
            model_fingerprint,
            native_sequence_capacity,
            lora: None,
            adapters,
            chat_template,
            step: 0,
            requests,
            watchdog,
            global_slots,
            suspended_permits: Vec::new(),
            max_batch_sequences,
            background_evicted,
            telemetry,
            metrics,
            unhealthy: false,
        })
    }

    fn run(&mut self, receiver: Receiver<ModelCommand>, queued: &AtomicUsize, busy: &AtomicUsize) {
        let mut running = true;
        while running {
            let mut handled_command = false;
            for _ in 0..MAX_COMMANDS_PER_BOUNDARY {
                match receiver.try_recv() {
                    Ok(command) => {
                        queued.fetch_sub(1, Ordering::AcqRel);
                        handled_command = true;
                        if !self.handle_command(command) {
                            running = false;
                            break;
                        }
                    }
                    Err(TryRecvError::Empty) => break,
                    Err(TryRecvError::Disconnected) => {
                        running = false;
                        break;
                    }
                }
            }
            if !running {
                break;
            }
            self.sweep_controls();
            self.admit_available();
            if let Err(error) = self.poll_draft_results() {
                tracing::error!(error = %error, "draft actor failed; invalidating model worker");
                self.unhealthy = true;
                self.fail_all_active(error);
                break;
            }
            busy.store(self.physically_active_count(), Ordering::Release);
            let progressed = if self.active.is_empty() {
                false
            } else {
                self.execute_scheduler_step()
            };
            if self.unhealthy {
                break;
            }
            if let Err(error) = self.resume_suspended() {
                tracing::error!(error = %error, "failed to resume suspended generation");
                self.unhealthy = true;
                break;
            }
            busy.store(self.physically_active_count(), Ordering::Release);
            self.update_gauges();
            if !handled_command && !progressed {
                let wait = if self.pending.is_empty() {
                    Duration::from_secs(WATCHDOG_POLL_INTERVAL_SECS)
                } else {
                    Duration::from_millis(10)
                };
                match receiver.recv_timeout(wait) {
                    Ok(command) => {
                        queued.fetch_sub(1, Ordering::AcqRel);
                        running = self.handle_command(command);
                    }
                    Err(RecvTimeoutError::Timeout) => {}
                    Err(RecvTimeoutError::Disconnected) => running = false,
                }
            }
        }
        self.shutdown_all();
        busy.store(0, Ordering::Release);
    }

    fn handle_command(&mut self, command: ModelCommand) -> bool {
        match command {
            ModelCommand::Generate {
                scheduler_id,
                request,
                control,
                events,
                receipt_identity,
            } => self.enqueue_generation(scheduler_id, *request, control, events, receipt_identity),
            ModelCommand::ResetSequence { scheduler_id } => {
                if let Some(active) = self
                    .active
                    .values()
                    .find(|active| active.scheduler_id == scheduler_id)
                {
                    active.executor.control().cancel();
                }
                if let Some(pending) = self.pending.get(&scheduler_id) {
                    pending.control.cancel();
                }
            }
            ModelCommand::Tokenize {
                items,
                add_special,
                reply,
            } => {
                let result = self.tokenize_batch(&items, add_special);
                let _ = reply.send(result);
            }
            ModelCommand::CountTokens {
                items,
                add_special,
                reply,
            } => {
                let result = self.count_tokenized(&items, add_special);
                let _ = reply.send(result);
            }
            ModelCommand::Embed { items, reply } => {
                let result = self.embeddings(&items);
                let _ = reply.send(result);
            }
            ModelCommand::Prefix { command, reply } => {
                let result = self.prefix(command);
                let _ = reply.send(result);
            }
            ModelCommand::Session {
                action,
                session_id,
                principal_id,
                reply,
            } => {
                let result = self.session(action, &session_id, &principal_id);
                let _ = reply.send(result);
            }
            ModelCommand::Lora { adapter, reply } => {
                let result = self.set_lora(adapter);
                let _ = reply.send(result);
            }
            ModelCommand::RenderChat { messages, reply } => {
                let result = self.render_chat(&messages);
                let _ = reply.send(result);
            }
            #[cfg(test)]
            ModelCommand::TerminateWorkerForTest { reply } => {
                let _ = reply.send(());
                return false;
            }
            ModelCommand::Shutdown => return false,
        }
        true
    }

    fn enqueue_generation(
        &mut self,
        scheduler_id: u64,
        request: GenerateRequest,
        control: GenerationControl,
        events: GenerationSender,
        receipt_identity: Option<oneshot::Sender<Result<ReceiptExecutionIdentity, RuntimeError>>>,
    ) {
        if let Some(reply) = receipt_identity {
            match self.receipt_execution_identity(&request) {
                Ok(identity) => {
                    if reply.send(Ok(identity)).is_err() {
                        self.fail_unadmitted(
                            scheduler_id,
                            &request,
                            &events,
                            RuntimeError::Cancelled,
                        );
                        return;
                    }
                }
                Err(error) => {
                    let public_error = RuntimeError::EvalReceiptCommit(error.to_string());
                    let _ = reply.send(Err(public_error));
                    self.fail_unadmitted(scheduler_id, &request, &events, error);
                    return;
                }
            }
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::Cancelled) {
            self.fail_unadmitted(scheduler_id, &request, &events, RuntimeError::Cancelled);
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::SessionUnknown) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::SessionUnknown(CONTRACT_PRODUCER_SECRET.to_owned()),
            );
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::QuotaExhausted) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::QuotaExhausted(CONTRACT_PRODUCER_SECRET.to_owned()),
            );
            return;
        }
        let session_payload = match self.request_session_payload(&request) {
            Ok(payload) => payload,
            Err(error) => {
                self.fail_unadmitted(scheduler_id, &request, &events, error);
                return;
            }
        };
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::BackendUnavailable) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::NativeUnavailable,
            );
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::AllocationFailed) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::Oom(CONTRACT_PRODUCER_SECRET.to_owned()),
            );
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::EvalTimeout) {
            self.fail_unadmitted(scheduler_id, &request, &events, RuntimeError::EvalTimeout);
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::Internal) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::Internal(CONTRACT_PRODUCER_SECRET.to_owned()),
            );
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::ModelCorrupt) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::ModelCorrupt {
                    path: PathBuf::from(CONTRACT_PRODUCER_SECRET),
                    reason: CONTRACT_PRODUCER_SECRET.to_owned(),
                },
            );
            return;
        }
        #[cfg(all(feature = "contract-test-controls", debug_assertions))]
        if request.contract_failure == Some(ContractProducerFailure::ModelNotLoaded) {
            self.fail_unadmitted(
                scheduler_id,
                &request,
                &events,
                RuntimeError::ModelNotLoaded(CONTRACT_PRODUCER_SECRET.to_owned()),
            );
            return;
        }
        let result = self.prepare_generation(
            scheduler_id,
            &request,
            session_payload.is_some(),
            control.clone(),
            events.clone(),
        );
        match result {
            Ok(mut pending) => {
                if let Some(session_id) = request.session_id.as_deref() {
                    if self.session_in_use(session_id, &request.principal_id) {
                        self.fail_unadmitted(
                            scheduler_id,
                            &request,
                            &events,
                            RuntimeError::UnsupportedParam(format!(
                                "session is already in use: {session_id}"
                            )),
                        );
                        return;
                    }
                }
                pending.session_payload = session_payload;
                let prompt_tokens = match u32::try_from(pending.tokens.len()) {
                    Ok(value) if value > 0 => value,
                    _ => {
                        self.fail_unadmitted(
                            scheduler_id,
                            &request,
                            &events,
                            RuntimeError::ContextOverflow {
                                requested: u32::MAX,
                                limit: self.info.context_length,
                            },
                        );
                        return;
                    }
                };
                let cells = prompt_tokens.saturating_add(request.max_tokens);
                let admission = AdmissionRequest {
                    request_id: scheduler_id,
                    principal_id: request.principal_id.clone(),
                    priority: request.priority,
                    prompt_tokens,
                    max_tokens: pending.scheduler_max_tokens,
                    decode_steps: pending.scheduler_decode_steps,
                    context_limit: self.info.context_length,
                    kv_bytes: u64::from(cells).saturating_mul(16),
                };
                #[cfg(all(feature = "contract-test-controls", debug_assertions))]
                if request.contract_failure == Some(ContractProducerFailure::QueueFull) {
                    self.fail_unadmitted(scheduler_id, &request, &events, RuntimeError::QueueFull);
                    return;
                }
                if let Err(error) = self.scheduler.submit(admission, self.step) {
                    self.fail_unadmitted(
                        scheduler_id,
                        &request,
                        &events,
                        RuntimeError::from(error),
                    );
                    return;
                }
                pending.queued_at = Instant::now();
                self.pending.insert(scheduler_id, pending);
            }
            Err(error) => self.fail_unadmitted(scheduler_id, &request, &events, error),
        }
    }

    fn receipt_execution_identity(
        &self,
        request: &GenerateRequest,
    ) -> Result<ReceiptExecutionIdentity, RuntimeError> {
        let adapter_set_sha256 = self.lora.as_ref().map_or_else(
            || absent_sha256(AbsentDigestField::AdapterSet),
            |active| Digest32::from_bytes(active.guard.record().identity_sha256()),
        );
        let template_sha256 = if request.endpoint == "/v1/chat/completions" {
            self.chat_template
                .trusted_sha256()
                .map(Digest32::from_bytes)
                .ok_or(RuntimeError::TemplateUntrusted)?
        } else {
            absent_sha256(AbsentDigestField::Template)
        };
        let system_digest = system_messages_sha256(&request.original_messages)
            .map_err(|error| RuntimeError::EvalReceiptCommit(error.to_string()))?;
        let original_digest = original_messages_sha256(&request.original_messages)
            .map_err(|error| RuntimeError::EvalReceiptCommit(error.to_string()))?;
        let grammar_sha256 = request.grammar.as_deref().map_or_else(
            || absent_sha256(AbsentDigestField::Grammar),
            |grammar| Digest32::sha256(grammar.as_bytes()),
        );
        let speculation = match (
            self.model_record.draft_pair.as_ref(),
            self.draft_receipt_identity.as_ref(),
        ) {
            (None, None) => SpeculationReceiptIdentity::PromptLookup,
            (Some(_), Some(identity @ SpeculationReceiptIdentity::DraftModel { .. })) => {
                identity.clone()
            }
            _ => {
                return Err(RuntimeError::EvalReceiptCommit(
                    "configured draft artifact identity is unavailable".to_owned(),
                ))
            }
        };
        Ok(ReceiptExecutionIdentity {
            model_id: self.info.id.clone(),
            model_sha256: Digest32::from_bytes(self.model_fingerprint),
            adapter_set_sha256,
            template_sha256,
            system_messages_sha256: system_digest,
            grammar_sha256,
            sampler_sha256: Digest32::from_bytes(request.sampling.identity_sha256()),
            generation_control_sha256: generation_control_sha256(request, &speculation)?,
            original_messages_sha256: original_digest,
            rendered_prompt_sha256: Digest32::sha256(request.prompt.as_bytes()),
        })
    }

    fn request_session_payload(
        &self,
        request: &GenerateRequest,
    ) -> Result<Option<Vec<u8>>, RuntimeError> {
        let Some(session_id) = request.session_id.as_deref() else {
            return Ok(None);
        };
        let key = SessionKey::new(&request.principal_id, self.model_fingerprint, session_id)
            .map_err(session_store_error)?;
        let payload = self
            .sessions
            .read(&key)
            .map_err(|error| session_access_error(error, session_id))?;
        Ok((!payload.is_empty()).then_some(payload))
    }

    fn physically_active_count(&self) -> usize {
        self.active
            .values()
            .filter(|active| active.permit.is_some())
            .count()
    }

    fn suspended_count(&self) -> usize {
        self.scheduler
            .snapshot()
            .active
            .iter()
            .filter(|receipt| receipt.slot_id.is_none())
            .count()
    }

    fn trim_suspended_permits(&mut self) {
        let suspended = self.suspended_count();
        while self.suspended_permits.len() > suspended {
            drop(self.suspended_permits.pop());
        }
    }

    fn retain_permit_for_suspended(&mut self, permit: GlobalSlotPermit) {
        if self.suspended_permits.len() < self.suspended_count() {
            self.suspended_permits.push(permit);
        } else {
            drop(permit);
        }
    }

    fn acquire_admission_permit(
        &mut self,
        priority: PriorityClass,
        active_limit: usize,
    ) -> Option<AdmissionPermit> {
        if priority == PriorityClass::InteractiveBlocking {
            if let Some(permit) = self.suspended_permits.pop() {
                return Some(AdmissionPermit {
                    permit,
                    borrowed_from_suspended: true,
                });
            }
        }
        self.global_slots
            .try_acquire(active_limit)
            .map(|permit| AdmissionPermit {
                permit,
                borrowed_from_suspended: false,
            })
    }

    fn release_unused_admission_permit(&mut self, permit: AdmissionPermit) {
        if permit.borrowed_from_suspended {
            self.retain_permit_for_suspended(permit.permit);
        } else {
            drop(permit.permit);
        }
        self.trim_suspended_permits();
    }

    fn suspend_runtime_sequences(
        &mut self,
        suspensions: &[crate::sched::CoreSuspensionReceipt],
    ) -> Result<(), RuntimeError> {
        for suspension in suspensions {
            let active = self.active.get_mut(&suspension.seq_id).ok_or_else(|| {
                RuntimeError::Internal("scheduler suspended an unknown runtime sequence".to_owned())
            })?;
            if active.slot_id.take() != Some(suspension.released_slot_id) {
                return Err(RuntimeError::Internal(
                    "scheduler suspension released a different physical slot".to_owned(),
                ));
            }
            let permit = active.permit.take().ok_or_else(|| {
                RuntimeError::Internal(
                    "scheduler suspended a sequence without a global permit".to_owned(),
                )
            })?;
            self.suspended_permits.push(permit);
        }
        self.trim_suspended_permits();
        Ok(())
    }

    fn resume_suspended(&mut self) -> Result<(), RuntimeError> {
        self.trim_suspended_permits();
        loop {
            if self.suspended_count() == 0 {
                self.suspended_permits.clear();
                return Ok(());
            }
            let active_limit = self
                .watchdog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .active_slot_target();
            let Some(permit) = self
                .suspended_permits
                .pop()
                .or_else(|| self.global_slots.try_acquire(active_limit))
            else {
                return Ok(());
            };
            let mut scheduler_events = Vec::new();
            let resumed = self.scheduler.resume_next(&mut scheduler_events);
            self.emit_scheduler_events(scheduler_events);
            match resumed {
                Ok(Some(receipt)) => {
                    let active = self.active.get_mut(&receipt.seq_id).ok_or_else(|| {
                        RuntimeError::Internal(
                            "scheduler resumed an unknown runtime sequence".to_owned(),
                        )
                    })?;
                    if active.permit.is_some() || active.slot_id.is_some() {
                        return Err(RuntimeError::Internal(
                            "scheduler resumed a sequence that already owned a slot".to_owned(),
                        ));
                    }
                    active.permit = Some(permit);
                    active.slot_id = Some(receipt.slot_id);
                }
                Ok(None) => {
                    self.retain_permit_for_suspended(permit);
                    return Ok(());
                }
                Err(error) => {
                    self.retain_permit_for_suspended(permit);
                    return Err(RuntimeError::from(error));
                }
            }
        }
    }

    fn prepare_generation(
        &self,
        scheduler_id: u64,
        request: &GenerateRequest,
        restoring_session: bool,
        control: GenerationControl,
        events: GenerationSender,
    ) -> Result<PendingGeneration, RuntimeError> {
        request
            .sampling
            .validate_for_vocab(self.model.vocab_size())
            .map_err(gen_error)?;
        if request.session_id.is_some() && request.infill_suffix.is_some() {
            return Err(RuntimeError::UnsupportedParam(
                "session continuation and fill-in-the-middle cannot be combined".to_owned(),
            ));
        }
        let tokens = if let Some(suffix) = request.infill_suffix.as_deref() {
            let prefix = self.tokenize(&request.prompt, false)?;
            let suffix = self.tokenize(suffix, false)?;
            let family = model_family(&self.info.architecture).ok_or_else(|| {
                RuntimeError::UnsupportedParam(
                    "loaded model architecture has no governed FIM convention".to_owned(),
                )
            })?;
            let sentinels = FimTokenMap::from_model(family, &self.model).map_err(gen_error)?;
            assemble_infill(Some(sentinels), &prefix, &suffix).map_err(gen_error)?
        } else {
            let add_special = !restoring_session;
            self.tokenize(&request.prompt, add_special)?
        };
        if tokens.is_empty() {
            return Err(RuntimeError::UnsupportedParam(
                "prompt tokenization produced no tokens".to_owned(),
            ));
        }
        let grammar = request
            .grammar
            .as_deref()
            .map(CompiledGrammar::compile)
            .transpose()
            .map_err(gen_error)?;
        let prefix_name = self.resolve_prefix_ref(request, &tokens)?;
        if request.session_id.is_some() && prefix_name.is_some() {
            return Err(RuntimeError::UnsupportedParam(
                "session resume and prefix reuse cannot be combined".to_owned(),
            ));
        }
        let request_prompt_tokens = tokens.len();
        let speculation_mode = self
            .model_record
            .draft_pair
            .as_ref()
            .map_or(DraftMode::PromptLookup, |pair| {
                DraftMode::DraftModel(pair.draft_model_id.clone())
            });
        let speculation =
            SpeculationPlan::new(speculation_mode, request.sampling.seed).map_err(gen_error)?;
        Ok(PendingGeneration {
            request: request.clone(),
            tokens,
            original_tokens: None,
            request_prompt_tokens,
            scheduler_max_tokens: request.max_tokens,
            scheduler_decode_steps: request.max_tokens.saturating_sub(1),
            grammar,
            prefix_name,
            session_payload: None,
            control,
            events,
            queued_at: Instant::now(),
            executor: None,
            next_token: None,
            generated_tokens: Vec::new(),
            readmission_count: 0,
            admitted_at: None,
            prefill_completed_at: None,
            prefix_hit_tokens: 0,
            speculation_proposed_tokens: 0,
            speculation_accepted_tokens: 0,
            speculation,
            speculation_version: scheduler_id,
            replay_origin: ReplayOrigin::FreshPrompt,
        })
    }

    fn admit_available(&mut self) {
        loop {
            let Some(scheduler_id) = self.scheduler.next_admissible_request_id(self.step) else {
                break;
            };
            let Some(priority) = self
                .pending
                .get(&scheduler_id)
                .map(|pending| pending.request.priority)
            else {
                let _ = self.scheduler.drop_queued(scheduler_id);
                continue;
            };
            let active_limit = self
                .watchdog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .active_slot_target();
            let Some(permit) = self.acquire_admission_permit(priority, active_limit) else {
                break;
            };
            let Some(pending) = self.pending.get(&scheduler_id) else {
                let _ = self.scheduler.drop_queued(scheduler_id);
                self.release_unused_admission_permit(permit);
                continue;
            };
            if let Some(session_payload) = pending.session_payload.clone() {
                let Some(session_id) = pending.request.session_id.clone() else {
                    self.reject_admission(
                        scheduler_id,
                        RuntimeError::Internal(
                            "session restore was queued without a session id".to_owned(),
                        ),
                    );
                    self.release_unused_admission_permit(permit);
                    continue;
                };
                let owner_fingerprint = match principal_fingerprint(&pending.request.principal_id) {
                    Ok(owner) => owner,
                    Err(error) => {
                        self.reject_admission(scheduler_id, error);
                        self.release_unused_admission_permit(permit);
                        continue;
                    }
                };
                let appended_prompt_tokens = match u32::try_from(pending.tokens.len()) {
                    Ok(tokens) => tokens,
                    Err(_) => {
                        self.reject_admission(
                            scheduler_id,
                            RuntimeError::ContextOverflow {
                                requested: u32::MAX,
                                limit: self.info.context_length,
                            },
                        );
                        self.release_unused_admission_permit(permit);
                        continue;
                    }
                };
                let continuation = match SchedulerCore::session_payload_continuation(
                    &session_payload,
                    self.model_fingerprint,
                    owner_fingerprint,
                ) {
                    Ok((_, continuation)) => continuation,
                    Err(error) => {
                        self.reject_admission(scheduler_id, RuntimeError::from(error));
                        self.release_unused_admission_permit(permit);
                        continue;
                    }
                };
                let Some(accounted_prompt_tokens) = continuation
                    .next_position()
                    .checked_add(appended_prompt_tokens)
                else {
                    self.reject_admission(
                        scheduler_id,
                        RuntimeError::ContextOverflow {
                            requested: u32::MAX,
                            limit: self.info.context_length,
                        },
                    );
                    self.release_unused_admission_permit(permit);
                    continue;
                };
                let cells = accounted_prompt_tokens.saturating_add(pending.scheduler_max_tokens);
                let admission_request = AdmissionRequest {
                    request_id: scheduler_id,
                    principal_id: pending.request.principal_id.clone(),
                    priority: pending.request.priority,
                    prompt_tokens: accounted_prompt_tokens,
                    max_tokens: pending.scheduler_max_tokens,
                    decode_steps: pending.scheduler_decode_steps,
                    context_limit: self.info.context_length,
                    kv_bytes: u64::from(cells).saturating_mul(16),
                };
                if let Err(error) = self.scheduler.drop_queued(scheduler_id) {
                    self.reject_admission(scheduler_id, RuntimeError::from(error));
                    self.release_unused_admission_permit(permit);
                    continue;
                }
                let mut scheduler_events = Vec::new();
                let restored = self.scheduler.restore_session_payload(
                    &mut self.context,
                    &session_payload,
                    CoreSessionRestoreOptions {
                        request: admission_request,
                        expected_model_fingerprint: self.model_fingerprint,
                        expected_owner_fingerprint: owner_fingerprint,
                        appended_prompt_tokens,
                        now_step: self.step,
                    },
                    &mut scheduler_events,
                );
                self.emit_scheduler_events(scheduler_events);
                match restored {
                    Ok(restored) => {
                        let Some(mut pending) = self.pending.remove(&scheduler_id) else {
                            let _ = self.scheduler.terminate(
                                &mut self.context,
                                restored.admission.seq_id,
                                TerminationReason::BackendFailure,
                                &mut Vec::new(),
                            );
                            self.release_unused_admission_permit(permit);
                            continue;
                        };
                        pending.replay_origin = ReplayOrigin::RestoredSession { session_id };
                        pending.tokens.insert(0, restored.continuation_token);
                        match self.activate(
                            restored.admission,
                            pending,
                            permit,
                            restored.continuation_position,
                            0,
                        ) {
                            Ok((seq_id, active)) => {
                                self.active.insert(seq_id, active);
                                let version = self.active[&seq_id].speculation_version;
                                if let Err(error) =
                                    self.queue_draft_job(seq_id, version, &[], MAX_DRAFT_TOKENS)
                                {
                                    self.unhealthy = true;
                                    self.fail_all_active(error);
                                    break;
                                }
                            }
                            Err((receipt, pending, permit, error)) => {
                                let _ = self.scheduler.terminate(
                                    &mut self.context,
                                    receipt.seq_id,
                                    TerminationReason::BackendFailure,
                                    &mut Vec::new(),
                                );
                                self.fail_unadmitted(
                                    receipt.request_id,
                                    &pending.request,
                                    &pending.events,
                                    error,
                                );
                                self.release_unused_admission_permit(permit);
                            }
                        }
                    }
                    Err(error) => {
                        self.reject_admission(scheduler_id, RuntimeError::from(error));
                        self.release_unused_admission_permit(permit);
                    }
                }
                continue;
            }
            let mut scheduler_events = Vec::new();
            let prefix = pending.prefix_name.as_deref().and_then(|name| {
                self.scheduler
                    .match_prefix_for_reuse(
                        scheduler_id,
                        name,
                        &pending.tokens,
                        &mut scheduler_events,
                    )
                    .transpose()
            });
            let prefix = match prefix {
                Some(Ok(plan)) => Some(plan),
                Some(Err(error)) => {
                    self.reject_admission(scheduler_id, RuntimeError::from(error));
                    self.release_unused_admission_permit(permit);
                    continue;
                }
                None => None,
            };
            let admission = if priority == PriorityClass::InteractiveBlocking {
                self.scheduler.admit_with_prefix_under_pressure_identified(
                    &mut self.context,
                    self.step,
                    prefix,
                    &mut scheduler_events,
                )
            } else {
                self.scheduler
                    .admit_with_prefix_identified(
                        &mut self.context,
                        self.step,
                        prefix,
                        &mut scheduler_events,
                    )
                    .map(|admitted| crate::sched::CoreAdmissionOutcome {
                        admitted,
                        readmissions: Vec::new(),
                    })
            };
            self.emit_scheduler_events(scheduler_events);
            match admission {
                Ok(outcome) => {
                    if let Err(error) = self.apply_readmissions(outcome.readmissions) {
                        self.release_unused_admission_permit(permit);
                        self.unhealthy = true;
                        self.fail_all_active(error);
                        break;
                    }
                    let Some(receipt) = outcome.admitted else {
                        self.release_unused_admission_permit(permit);
                        break;
                    };
                    let Some(pending) = self.pending.remove(&receipt.request_id) else {
                        let _ = self.scheduler.terminate(
                            &mut self.context,
                            receipt.seq_id,
                            TerminationReason::BackendFailure,
                            &mut Vec::new(),
                        );
                        self.release_unused_admission_permit(permit);
                        continue;
                    };
                    let prompt_cursor = receipt.prefix_hit_tokens as usize;
                    match self.activate(receipt, pending, permit, 0, prompt_cursor) {
                        Ok((seq_id, active)) => {
                            self.active.insert(seq_id, active);
                            let version = self.active[&seq_id].speculation_version;
                            if let Err(error) =
                                self.queue_draft_job(seq_id, version, &[], MAX_DRAFT_TOKENS)
                            {
                                self.unhealthy = true;
                                self.fail_all_active(error);
                                break;
                            }
                        }
                        Err((receipt, pending, permit, error)) => {
                            let _ = self.scheduler.terminate(
                                &mut self.context,
                                receipt.seq_id,
                                TerminationReason::BackendFailure,
                                &mut Vec::new(),
                            );
                            self.fail_unadmitted(
                                receipt.request_id,
                                &pending.request,
                                &pending.events,
                                error,
                            );
                            self.release_unused_admission_permit(permit);
                        }
                    }
                }
                Err(failure) => {
                    if let Err(error) = self.apply_readmissions(failure.readmissions) {
                        self.release_unused_admission_permit(permit);
                        self.unhealthy = true;
                        self.fail_all_active(error);
                        break;
                    }
                    let _ = self.scheduler.drop_queued(failure.request_id);
                    self.reject_admission(failure.request_id, RuntimeError::from(failure.error));
                    self.release_unused_admission_permit(permit);
                }
            }
        }
    }

    fn apply_readmissions(
        &mut self,
        receipts: Vec<crate::sched::CoreReadmissionReceipt>,
    ) -> Result<(), RuntimeError> {
        for receipt in &receipts {
            self.validate_readmission(receipt)?;
        }
        for receipt in receipts {
            self.apply_readmission(receipt)?;
        }
        self.trim_suspended_permits();
        Ok(())
    }

    fn validate_readmission(
        &self,
        receipt: &crate::sched::CoreReadmissionReceipt,
    ) -> Result<(), RuntimeError> {
        let active = self.active.get(&receipt.sequence.seq_id).ok_or_else(|| {
            RuntimeError::Internal(
                "scheduler evicted an unknown runtime sequence for readmission".to_owned(),
            )
        })?;
        if receipt.request_id != active.scheduler_id
            || receipt.admission.request_id != active.scheduler_id
            || receipt.sequence.reason != crate::sched::ReadmissionReason::KvPressure
            || receipt.sequence.evicted_cells == 0
            || active.request.priority != PriorityClass::Background
            || active.slot_id.is_some()
            || active.permit.is_some()
        {
            return Err(RuntimeError::Internal(
                "scheduler readmission receipt does not match suspended Background ownership"
                    .to_owned(),
            ));
        }
        if active.position_base != 0 {
            return Err(RuntimeError::Internal(
                "restored-session generation was pressure-evicted despite being ineligible"
                    .to_owned(),
            ));
        }
        let expected_prefill_remaining = active
            .prefill_tokens
            .len()
            .saturating_sub(active.prompt_cursor);
        if usize::try_from(receipt.remaining_prefill_tokens).ok()
            != Some(expected_prefill_remaining)
        {
            return Err(RuntimeError::Internal(
                "scheduler readmission prefill progress diverged from runtime".to_owned(),
            ));
        }
        let usage = active.executor.usage();
        if usage.completion_tokens != active.generated_tokens.len() {
            return Err(RuntimeError::Internal(
                "scheduler readmission token history diverged from executor usage".to_owned(),
            ));
        }
        let projection = project_background_readmission(
            &active.replay_origin,
            &active.tokens,
            &active.generated_tokens,
            active.next_token,
            active.request.max_tokens,
            self.info.context_length,
        )?;
        if receipt.remaining_decode_steps != projection.scheduler_decode_steps {
            return Err(RuntimeError::Internal(
                "scheduler readmission decode progress diverged from runtime".to_owned(),
            ));
        }
        Ok(())
    }

    fn apply_readmission(
        &mut self,
        receipt: crate::sched::CoreReadmissionReceipt,
    ) -> Result<(), RuntimeError> {
        self.validate_readmission(&receipt)?;
        let active = self.active.get(&receipt.sequence.seq_id).ok_or_else(|| {
            RuntimeError::Internal("validated readmission sequence disappeared".to_owned())
        })?;
        let projection = project_background_readmission(
            &active.replay_origin,
            &active.tokens,
            &active.generated_tokens,
            active.next_token,
            active.request.max_tokens,
            self.info.context_length,
        )?;

        let mut active = self
            .active
            .remove(&receipt.sequence.seq_id)
            .ok_or_else(|| RuntimeError::Internal("readmission sequence disappeared".to_owned()))?;
        active.speculation_version = active.speculation_version.saturating_add(1);
        self.retire_draft_sequence(
            receipt.sequence.seq_id,
            active.scheduler_id,
            active.speculation_version,
        );
        self.background_evicted.fetch_add(1, Ordering::AcqRel);
        if active.readmission_count >= MAX_BACKGROUND_READMISSIONS {
            let error = RuntimeError::BackgroundReadmissionLimit {
                attempts: active.readmission_count.saturating_add(1),
            };
            self.fail_evicted_active(active, error);
            return Ok(());
        }
        let replay_prompt_tokens = u32::try_from(projection.replay_tokens.len()).map_err(|_| {
            RuntimeError::ContextOverflow {
                requested: u32::MAX,
                limit: self.info.context_length,
            }
        })?;
        let scheduler_max_tokens = projection.remaining_outputs;
        let scheduler_decode_steps = projection.scheduler_decode_steps;
        let accounted_tokens = replay_prompt_tokens
            .checked_add(scheduler_max_tokens)
            .ok_or(RuntimeError::ContextOverflow {
                requested: u32::MAX,
                limit: self.info.context_length,
            })?;
        if accounted_tokens > self.info.context_length {
            let error = RuntimeError::ContextOverflow {
                requested: accounted_tokens,
                limit: self.info.context_length,
            };
            self.fail_evicted_active(active, error);
            return Ok(());
        }
        let admission = AdmissionRequest {
            request_id: active.scheduler_id,
            principal_id: active.request.principal_id.clone(),
            priority: PriorityClass::Background,
            prompt_tokens: replay_prompt_tokens,
            max_tokens: scheduler_max_tokens,
            decode_steps: scheduler_decode_steps,
            context_limit: self.info.context_length,
            kv_bytes: u64::from(accounted_tokens).saturating_mul(16),
        };
        if let Err(error) = self.scheduler.submit(admission, self.step) {
            let error = RuntimeError::from(error);
            self.fail_evicted_active(active, error);
            return Ok(());
        }
        active.readmission_count = active.readmission_count.saturating_add(1);
        let request_prompt_tokens = active.executor.usage().prompt_tokens;
        self.pending.insert(
            active.scheduler_id,
            PendingGeneration {
                request: active.request,
                tokens: projection.replay_tokens,
                original_tokens: Some(active.tokens),
                request_prompt_tokens,
                scheduler_max_tokens,
                scheduler_decode_steps,
                grammar: active.grammar,
                prefix_name: None,
                session_payload: None,
                control: active.control,
                events: active.events,
                queued_at: active.queued_at,
                executor: Some(active.executor),
                next_token: projection.next_token,
                generated_tokens: active.generated_tokens,
                readmission_count: active.readmission_count,
                admitted_at: Some(active.admitted_at),
                prefill_completed_at: active.prefill_completed_at,
                prefix_hit_tokens: active.prefix_hit_tokens,
                speculation_proposed_tokens: active.speculation_proposed_tokens,
                speculation_accepted_tokens: active.speculation_accepted_tokens,
                speculation: active.speculation,
                speculation_version: active.speculation_version,
                replay_origin: active.replay_origin,
            },
        );
        Ok(())
    }

    fn fail_evicted_active(&mut self, active: ActiveGeneration, error: RuntimeError) {
        self.record_terminal_metrics(active.scheduler_id, TerminalOutcome::Failed);
        self.fail_unadmitted(active.scheduler_id, &active.request, &active.events, error);
        self.watchdog
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .complete(active.scheduler_id);
        if let Some(permit) = active.permit {
            self.retain_permit_for_suspended(permit);
        }
    }

    fn activate(
        &mut self,
        receipt: crate::sched::CoreAdmissionReceipt,
        mut pending: PendingGeneration,
        permit: AdmissionPermit,
        position_base: u32,
        prompt_cursor: usize,
    ) -> Result<
        (SeqId, ActiveGeneration),
        (
            crate::sched::CoreAdmissionReceipt,
            PendingGeneration,
            AdmissionPermit,
            RuntimeError,
        ),
    > {
        if receipt.slot_id.is_none() {
            return Err((
                receipt,
                pending,
                permit,
                RuntimeError::Internal(
                    "scheduler admitted generation without a physical slot".to_owned(),
                ),
            ));
        }
        let retained_executor = pending.executor.is_some();
        let executor = if let Some(executor) = pending.executor.take() {
            executor
        } else {
            let stop = match StopEvaluator::new(
                pending.request.stop.clone(),
                vec![self.model.end_token()],
                pending.request.max_tokens as usize,
            ) {
                Ok(stop) => stop,
                Err(error) => return Err((receipt, pending, permit, gen_error(error))),
            };
            match GenerationExecutor::new_native(
                &self.model,
                NativeGenerationConfig {
                    params: &pending.request.sampling,
                    capabilities: SamplerCapabilities::pinned_revision(),
                    grammar: pending.grammar.as_ref(),
                    top_logprobs: 0,
                    prompt_tokens: pending.request_prompt_tokens,
                },
                stop,
                pending.events.clone(),
                pending.control.clone(),
            ) {
                Ok(executor) => executor,
                Err(error) => return Err((receipt, pending, permit, gen_error(error))),
            }
        };
        let first_admission = pending.admitted_at.is_none();
        let admitted_at = pending.admitted_at.unwrap_or_else(Instant::now);
        let seq_id = receipt.seq_id;
        if let Some(draft_actor) = self.draft_actor.as_ref() {
            if let Err(error) = draft_actor.configure(
                seq_id,
                receipt.request_id,
                pending.speculation_version,
                pending.request.sampling.clone(),
                pending.grammar.clone(),
            ) {
                return Err((receipt, pending, permit, error));
            }
        }
        if first_admission {
            if let Err(error) = self.metrics.record_admission(receipt.request_id) {
                return Err((
                    receipt,
                    pending,
                    permit,
                    RuntimeError::Internal(format!(
                        "request metrics admission invariant failed: {error}"
                    )),
                ));
            }
            self.watchdog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .register_traced(
                    receipt.request_id,
                    if pending.request.priority == PriorityClass::Background {
                        WorkClass::Background
                    } else {
                        WorkClass::Foreground
                    },
                    TraceContext::new(
                        pending.request.request_id.clone(),
                        pending.request.trace_id.clone(),
                    ),
                );
        }
        let prefill_tokens = pending.tokens;
        let original_tokens = pending
            .original_tokens
            .take()
            .unwrap_or_else(|| prefill_tokens.clone());
        let next_token = pending.next_token;
        Ok((
            seq_id,
            ActiveGeneration {
                scheduler_id: receipt.request_id,
                request: pending.request,
                tokens: original_tokens,
                prefill_tokens,
                prompt_cursor,
                position_base,
                next_token,
                executor,
                control: pending.control,
                grammar: pending.grammar,
                generated_tokens: pending.generated_tokens,
                readmission_count: pending.readmission_count,
                suppress_replay_sample: retained_executor && next_token.is_some(),
                replay_origin: pending.replay_origin,
                events: pending.events,
                queued_at: pending.queued_at,
                admitted_at,
                prefill_completed_at: pending.prefill_completed_at,
                slot_id: receipt.slot_id,
                prefix_hit_tokens: pending
                    .prefix_hit_tokens
                    .saturating_add(receipt.prefix_hit_tokens),
                speculation_proposed_tokens: pending.speculation_proposed_tokens,
                speculation_accepted_tokens: pending.speculation_accepted_tokens,
                speculation: pending.speculation,
                speculation_version: pending.speculation_version,
                optimistic_bonus: None,
                permit: Some(permit.permit),
            },
        ))
    }

    fn sweep_controls(&mut self) {
        let pending_terminal: Vec<_> = self
            .pending
            .iter()
            .filter_map(|(&scheduler_id, pending)| {
                (pending.control.state() != GenerationControlState::Running).then_some(scheduler_id)
            })
            .collect();
        for scheduler_id in pending_terminal {
            let _ = self.scheduler.drop_queued(scheduler_id);
            if let Some(pending) = self.pending.remove(&scheduler_id) {
                let reason = control_stop_reason(pending.control.state());
                if pending.admitted_at.is_some() {
                    self.record_terminal_metrics(
                        scheduler_id,
                        if reason == StopReason::DeadlineExceeded {
                            TerminalOutcome::Failed
                        } else {
                            TerminalOutcome::Cancelled
                        },
                    );
                }
                if let Some(executor) = pending.executor.as_ref() {
                    if finish_retained_pending_executor(executor).is_err() {
                        let _ = pending
                            .events
                            .try_send(GenerationEvent::Failed(GenError::StreamDisconnected));
                    }
                } else {
                    let _ = pending.events.try_send(GenerationEvent::Finished {
                        reason: reason.clone(),
                        usage: GenerationUsage {
                            prompt_tokens: pending.request_prompt_tokens,
                            completion_tokens: 0,
                        },
                        confidence: None,
                    });
                }
                emit_observable(
                    &self.telemetry,
                    &self.metrics,
                    EngineEvent::RequestFailed {
                        request_id: pending.request.request_id.clone(),
                        trace_id: pending.request.trace_id.clone(),
                        model_id: pending
                            .request
                            .model
                            .clone()
                            .unwrap_or_else(|| self.info.id.clone()),
                        code: if reason == StopReason::DeadlineExceeded {
                            "eval_timeout".to_owned()
                        } else {
                            "cancelled".to_owned()
                        },
                        priority_class: priority_name(pending.request.priority).to_owned(),
                    },
                    "pending_request_terminal",
                );
                self.watchdog
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .complete(scheduler_id);
                self.requests.remove_scheduler(scheduler_id);
            }
        }

        let active_terminal: Vec<_> = self
            .active
            .iter_mut()
            .filter_map(
                |(&seq_id, active)| match active.executor.finish_from_control_try() {
                    Ok(Some(StepOutcome::Finished(reason))) => Some((seq_id, reason, false)),
                    Ok(Some(StepOutcome::Continue { .. })) | Ok(None) => None,
                    Err(_) => Some((seq_id, StopReason::Disconnected, true)),
                },
            )
            .collect();
        for (seq_id, reason, failed) in active_terminal {
            let termination = if failed {
                TerminationReason::BackendFailure
            } else {
                termination_reason(&reason)
            };
            if self
                .scheduler
                .terminate(&mut self.context, seq_id, termination, &mut Vec::new())
                .is_ok()
            {
                self.finalize_active_and_recycle(
                    seq_id,
                    if failed {
                        TerminalKind::Failed
                    } else {
                        TerminalKind::Normal(reason)
                    },
                );
            }
        }
    }

    fn build_draft_job(
        &self,
        seq_id: SeqId,
        version: u64,
        additional_history: &[i32],
        scheduler_budget: usize,
    ) -> Result<Option<DraftJob>, RuntimeError> {
        let Some(active) = self.active.get(&seq_id) else {
            return Ok(None);
        };
        let usage = active.executor.usage();
        let remaining_outputs = (active.request.max_tokens as usize)
            .saturating_sub(usage.completion_tokens)
            .saturating_sub(additional_history.len());
        let occupied = (active.position_base as usize)
            .saturating_add(active.tokens.len())
            .saturating_add(active.generated_tokens.len())
            .saturating_add(additional_history.len());
        let mut history = Vec::with_capacity(
            active
                .tokens
                .len()
                .saturating_add(active.generated_tokens.len())
                .saturating_add(additional_history.len()),
        );
        history.extend_from_slice(&active.tokens);
        history.extend_from_slice(&active.generated_tokens);
        history.extend_from_slice(additional_history);
        let row_bytes = self
            .model
            .vocab_size()
            .saturating_mul(std::mem::size_of::<crate::gen::TokenProbability>());
        let eligibility = SpeculationEligibility::for_request_with_limits(
            &self.model_record,
            occupied,
            remaining_outputs,
            self.info.context_length as usize,
            scheduler_budget.min(active.speculation.draft_budget),
            row_bytes,
        );
        if !eligibility.eligible {
            return Ok(None);
        }
        DraftJob::new(
            seq_id,
            version,
            &history,
            eligibility.maximum_budget,
            self.model.vocab_size(),
        )
        .map(Some)
        .map_err(gen_error)
    }

    fn queue_draft_job(
        &self,
        seq_id: SeqId,
        version: u64,
        additional_history: &[i32],
        scheduler_budget: usize,
    ) -> Result<bool, RuntimeError> {
        let Some(actor) = self.draft_actor.as_ref() else {
            return Ok(false);
        };
        let Some(owner_id) = self.active.get(&seq_id).and_then(|active| {
            matches!(active.speculation.mode, DraftMode::DraftModel(_))
                .then_some(active.scheduler_id)
        }) else {
            return Ok(false);
        };
        let Some(job) =
            self.build_draft_job(seq_id, version, additional_history, scheduler_budget)?
        else {
            return Ok(false);
        };
        actor.try_submit(owner_id, job)
    }

    fn poll_draft_results(&mut self) -> Result<(), RuntimeError> {
        let Some(actor) = self.draft_actor.as_ref() else {
            return Ok(());
        };
        let mut results = Vec::new();
        while let Some(result) = actor.try_result()? {
            results.push(result);
        }
        let mut request_failures = Vec::new();
        let mut reconciliation_requeues = Vec::new();
        for mut result in results {
            let is_current = self.active.get(&result.sequence_id).is_some_and(|active| {
                active.scheduler_id == result.owner_id
                    && active.speculation_version == result.version
                    && active.control.state() == GenerationControlState::Running
            });
            if !is_current {
                continue;
            }
            tracing::debug!(
                sequence_id = result.sequence_id,
                version = result.version,
                draft_ms = duration_ms(
                    result
                        .finished_at
                        .saturating_duration_since(result.started_at)
                ),
                "draft actor completed a versioned proposal"
            );
            if let Err(error) = &result.result {
                if matches!(
                    error,
                    GenError::SpeculationContextInvalidated(_) | GenError::Backend(_)
                ) {
                    return Err(gen_error(error.clone()));
                }
                request_failures.push((result.sequence_id, error.clone()));
                continue;
            }
            let optimistic_bonus = self
                .active
                .get_mut(&result.sequence_id)
                .and_then(|active| active.optimistic_bonus.take());
            if let Some(bonus) = optimistic_bonus {
                let reconciled = result
                    .result
                    .as_ref()
                    .expect("draft result error was handled above")
                    .reconcile_optimistic_bonus(bonus);
                match reconciled {
                    Ok(Some(reconciled)) => result.result = Ok(reconciled),
                    Ok(None) => {
                        reconciliation_requeues.push((result.sequence_id, result.version));
                        continue;
                    }
                    Err(error) => {
                        request_failures.push((result.sequence_id, error));
                        continue;
                    }
                }
            }
            self.draft_results
                .insert((result.sequence_id, result.version), result);
        }
        for (sequence_id, error) in request_failures {
            if let Some(active) = self.active.get(&sequence_id) {
                let _ = active.events.try_send(GenerationEvent::Failed(error));
            }
            if self
                .scheduler
                .terminate(
                    &mut self.context,
                    sequence_id,
                    TerminationReason::BackendFailure,
                    &mut Vec::new(),
                )
                .is_ok()
            {
                self.finalize_active_and_recycle(sequence_id, TerminalKind::Failed);
            }
        }
        for (sequence_id, version) in reconciliation_requeues {
            self.queue_draft_job(sequence_id, version, &[], MAX_DRAFT_TOKENS)?;
        }
        Ok(())
    }

    fn retire_draft_sequence(&mut self, seq_id: SeqId, owner_id: u64, minimum_version: u64) {
        if let Some(actor) = self.draft_actor.as_ref() {
            actor.invalidate(seq_id, minimum_version);
            actor.remove(seq_id, owner_id);
        }
        self.draft_results
            .retain(|(sequence_id, _), _| *sequence_id != seq_id);
    }

    fn rollback_speculative_fork(&mut self, fork: &TargetKvFork) -> Result<(), RuntimeError> {
        fork.rollback(&mut self.context).map_err(gen_error)?;
        self.scheduler
            .release_empty_scratch_sequence(
                &mut self.context,
                fork.scratch_sequence_id as u32,
                &mut Vec::new(),
            )
            .map(|_| ())
            .map_err(RuntimeError::from)
    }

    fn release_committed_speculative_fork(
        &mut self,
        fork: &TargetKvFork,
    ) -> Result<(), RuntimeError> {
        self.scheduler
            .release_empty_scratch_sequence(
                &mut self.context,
                fork.scratch_sequence_id as u32,
                &mut Vec::new(),
            )
            .map(|_| ())
            .map_err(RuntimeError::from)
    }

    fn consume_normal_output(
        &mut self,
        output: &DecodeOutput,
    ) -> (
        Option<TerminalKind>,
        Option<(SeqId, u64)>,
        Option<DecodeProgress>,
    ) {
        let Some(active) = self.active.get_mut(&output.seq_id) else {
            return (None, None, None);
        };
        let before = active.executor.usage().completion_tokens;
        let outcome = active.executor.after_native_decode_try(
            &self.model,
            &mut self.context,
            GenerationStep {
                output_index: output.output_index,
            },
        );
        let after = active.executor.usage().completion_tokens;
        let output_tokens = after.saturating_sub(before);
        let progress = (output.phase == SequencePhase::Decode).then_some(DecodeProgress {
            seq_id: output.seq_id,
            compute_tokens: 1,
            output_tokens: u32::try_from(output_tokens).unwrap_or(u32::MAX),
        });
        let outcome = match outcome {
            Ok(outcome) if output_tokens <= 1 => outcome,
            Ok(_) | Err(_) => return (Some(TerminalKind::Failed), None, progress),
        };
        if output_tokens == 1 && Self::record_generated_token(active).is_err() {
            return (Some(TerminalKind::Failed), None, progress);
        }
        match outcome {
            StepOutcome::Continue { token_id } => {
                if output_tokens != 1 {
                    return (Some(TerminalKind::Failed), None, progress);
                }
                active.next_token = Some(token_id);
                if active.prefill_completed_at.is_none() {
                    active.prefill_completed_at = Some(Instant::now());
                }
                self.watchdog
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .progress(active.scheduler_id);
                active.optimistic_bonus = None;
                active.speculation_version = active.speculation_version.saturating_add(1);
                (
                    None,
                    Some((output.seq_id, active.speculation_version)),
                    progress,
                )
            }
            StepOutcome::Finished(reason) => (Some(TerminalKind::Normal(reason)), None, progress),
        }
    }

    fn execute_scheduler_step(&mut self) -> bool {
        let plan = self.scheduler.plan_step();
        if plan.work.is_empty()
            && plan.timeout_receipts.is_empty()
            && plan.preempted_sequence_ids.is_empty()
        {
            return false;
        }
        let batch_sequences = plan
            .work
            .iter()
            .map(|work| work.seq_id)
            .collect::<BTreeSet<_>>()
            .len();
        self.max_batch_sequences
            .fetch_max(batch_sequences, Ordering::AcqRel);
        let mut outputs = Vec::new();
        self.batch.clear();
        let mut row_index = 0_i32;
        let mut fill_error = None;
        let mut replay_prefill_completed = Vec::new();
        for work in &plan.work {
            let Some(active) = self.active.get_mut(&work.seq_id) else {
                fill_error = Some(RuntimeError::Internal(
                    "scheduler planned work for an unknown runtime sequence".to_owned(),
                ));
                break;
            };
            match work.phase {
                SequencePhase::Prefill => {
                    let count = work.token_count as usize;
                    let end = active.prompt_cursor.saturating_add(count);
                    if end > active.prefill_tokens.len() {
                        fill_error = Some(RuntimeError::Internal(
                            "scheduler prefill exceeded tokenized prompt".to_owned(),
                        ));
                        break;
                    }
                    for offset in active.prompt_cursor..end {
                        let final_prompt_token = offset + 1 == active.prefill_tokens.len();
                        let sample_output = final_prompt_token && !active.suppress_replay_sample;
                        let seq = [work.seq_id as i32];
                        let position = match native_position(active.position_base, offset) {
                            Ok(position) => position,
                            Err(error) => {
                                fill_error = Some(error);
                                break;
                            }
                        };
                        if let Err(error) = self.batch.add_token(
                            active.prefill_tokens[offset],
                            position,
                            &seq,
                            sample_output,
                        ) {
                            fill_error = Some(RuntimeError::Internal(format!(
                                "native prefill batch failed: {error}"
                            )));
                            break;
                        }
                        if sample_output {
                            outputs.push(DecodeOutput {
                                seq_id: work.seq_id,
                                output_index: row_index,
                                phase: work.phase,
                                compute_grant: work.token_count,
                            });
                        } else if final_prompt_token && active.suppress_replay_sample {
                            replay_prefill_completed.push(work.seq_id);
                        }
                        row_index += 1;
                    }
                    active.prompt_cursor = end;
                }
                SequencePhase::Decode => {
                    let Some(token) = active.next_token.take() else {
                        fill_error = Some(RuntimeError::Internal(
                            "decode step has no previously sampled token".to_owned(),
                        ));
                        break;
                    };
                    let usage = active.executor.usage();
                    let relative_position = active
                        .tokens
                        .len()
                        .saturating_add(usage.completion_tokens)
                        .saturating_sub(1);
                    let position = match native_position(active.position_base, relative_position) {
                        Ok(position) => position,
                        Err(error) => {
                            fill_error = Some(error);
                            break;
                        }
                    };
                    let seq = [work.seq_id as i32];
                    if let Err(error) = self.batch.add_token(token, position, &seq, true) {
                        fill_error = Some(RuntimeError::Internal(format!(
                            "native decode batch failed: {error}"
                        )));
                        break;
                    }
                    outputs.push(DecodeOutput {
                        seq_id: work.seq_id,
                        output_index: row_index,
                        phase: work.phase,
                        compute_grant: work.token_count,
                    });
                    row_index += 1;
                }
            }
        }
        if let Some(error) = fill_error {
            self.fail_planned_sequences(&plan, error);
            return true;
        }
        if row_index > 0 {
            if let Err(error) = self.context.decode(&mut self.batch) {
                self.handle_decode_failure(&plan, error.to_string());
                return true;
            }
        }
        #[cfg(test)]
        {
            let delay_ms = DRAFT_ACTOR_TARGET_YIELD_MS.load(Ordering::Acquire);
            if delay_ms > 0 {
                std::thread::sleep(Duration::from_millis(delay_ms));
            }
        }
        for seq_id in replay_prefill_completed {
            if let Some(active) = self.active.get_mut(&seq_id) {
                active.suppress_replay_sample = false;
            }
        }
        if let Err(error) = self.poll_draft_results() {
            self.unhealthy = true;
            self.fail_all_active(error);
            return true;
        }
        let mut early_terminal = Vec::new();
        let mut decode_progress = plan
            .work
            .iter()
            .filter(|work| work.phase == SequencePhase::Decode)
            .map(|work| {
                (
                    work.seq_id,
                    DecodeProgress {
                        seq_id: work.seq_id,
                        compute_tokens: 1,
                        output_tokens: 0,
                    },
                )
            })
            .collect::<BTreeMap<_, _>>();
        let mut draft_requeues = Vec::new();
        let mut speculative_seeds = Vec::new();
        for output in outputs {
            let speculative_facts = self.active.get(&output.seq_id).and_then(|active| {
                (output.phase == SequencePhase::Decode
                    && active.control.state() == GenerationControlState::Running)
                    .then(|| (active.speculation.mode.clone(), active.speculation_version))
            });
            let Some((mode, version)) = speculative_facts else {
                let (terminal, requeue, progress) = self.consume_normal_output(&output);
                early_terminal.extend(terminal.map(|terminal| (output.seq_id, terminal)));
                draft_requeues.extend(requeue);
                if let Some(progress) = progress {
                    decode_progress.insert(output.seq_id, progress);
                }
                continue;
            };
            let proposal_budget = match mode {
                DraftMode::DraftModel(_) => output.compute_grant.saturating_sub(1) / 2,
                DraftMode::PromptLookup => output.compute_grant.saturating_sub(1),
            } as usize;
            let job = match self.build_draft_job(output.seq_id, version, &[], proposal_budget) {
                Ok(Some(job)) => job,
                Ok(None) => {
                    let (terminal, requeue, progress) = self.consume_normal_output(&output);
                    early_terminal.extend(terminal.map(|terminal| (output.seq_id, terminal)));
                    draft_requeues.extend(requeue);
                    if let Some(progress) = progress {
                        decode_progress.insert(output.seq_id, progress);
                    }
                    continue;
                }
                Err(error) => {
                    if let Some(active) = self.active.get(&output.seq_id) {
                        let _ = active
                            .events
                            .try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
                    }
                    early_terminal.push((output.seq_id, TerminalKind::Failed));
                    continue;
                }
            };
            let proposal_result = match mode {
                DraftMode::DraftModel(_) => self
                    .draft_results
                    .remove(&(output.seq_id, version))
                    .map(|result| result.result)
                    .transpose(),
                DraftMode::PromptLookup => self.prompt_lookup.propose(&job).map(Some),
            };
            let result = match proposal_result {
                Ok(Some(result)) => result,
                Ok(None) => {
                    let (terminal, requeue, progress) = self.consume_normal_output(&output);
                    early_terminal.extend(terminal.map(|terminal| (output.seq_id, terminal)));
                    draft_requeues.extend(requeue);
                    if let Some(progress) = progress {
                        decode_progress.insert(output.seq_id, progress);
                    }
                    continue;
                }
                Err(error) => {
                    if let Some(active) = self.active.get(&output.seq_id) {
                        let _ = active.events.try_send(GenerationEvent::Failed(error));
                    }
                    early_terminal.push((output.seq_id, TerminalKind::Failed));
                    continue;
                }
            };
            if result.sequence_id != output.seq_id || result.version != version {
                if let Some(active) = self.active.get(&output.seq_id) {
                    let _ = active.events.try_send(GenerationEvent::Failed(
                        GenError::SpeculationInvalid(
                            "draft result identity changed after actor validation",
                        ),
                    ));
                }
                early_terminal.push((output.seq_id, TerminalKind::Failed));
                continue;
            }
            let proposal = match DraftProposal::new(
                mode,
                result
                    .proposal
                    .tokens
                    .iter()
                    .take(job.budget)
                    .cloned()
                    .collect(),
            ) {
                Ok(proposal) if !proposal.tokens.is_empty() => proposal,
                Ok(_) => {
                    let (terminal, requeue, progress) = self.consume_normal_output(&output);
                    early_terminal.extend(terminal.map(|terminal| (output.seq_id, terminal)));
                    draft_requeues.extend(requeue);
                    if let Some(progress) = progress {
                        decode_progress.insert(output.seq_id, progress);
                    }
                    continue;
                }
                Err(error) => {
                    if let Some(active) = self.active.get(&output.seq_id) {
                        let _ = active.events.try_send(GenerationEvent::Failed(error));
                    }
                    early_terminal.push((output.seq_id, TerminalKind::Failed));
                    continue;
                }
            };
            let Some(active) = self.active.get(&output.seq_id) else {
                continue;
            };
            let mut sampler = match active.executor.begin_speculative_sampler() {
                Ok(sampler) => sampler,
                Err(_) => {
                    let (terminal, requeue, progress) = self.consume_normal_output(&output);
                    early_terminal.extend(terminal.map(|terminal| (output.seq_id, terminal)));
                    draft_requeues.extend(requeue);
                    if let Some(progress) = progress {
                        decode_progress.insert(output.seq_id, progress);
                    }
                    continue;
                }
            };
            let initial_probe = match active.executor.probe_distribution(
                &mut sampler,
                &mut self.context,
                output.output_index,
            ) {
                Ok(probe) => probe,
                Err(error) => {
                    let _ = active.events.try_send(GenerationEvent::Failed(error));
                    early_terminal.push((output.seq_id, TerminalKind::Failed));
                    continue;
                }
            };
            speculative_seeds.push(SpeculativeSeed {
                output,
                proposal,
                sampler,
                probes: vec![initial_probe],
            });
        }
        let mut reserved_seeds = Vec::new();
        for seed in speculative_seeds {
            let Some(active) = self.active.get(&seed.output.seq_id) else {
                continue;
            };
            let mut scheduler_events = Vec::new();
            match self.scheduler.reserve_scratch_sequence(
                seed.proposal.tokens.len() as u32,
                active.request.priority,
                &mut scheduler_events,
            ) {
                Ok(scratch) => {
                    self.emit_scheduler_events(scheduler_events);
                    reserved_seeds.push((seed, scratch));
                }
                Err(_) => {
                    self.emit_scheduler_events(scheduler_events);
                    let (terminal, requeue, progress) = self.consume_normal_output(&seed.output);
                    early_terminal.extend(terminal.map(|terminal| (seed.output.seq_id, terminal)));
                    draft_requeues.extend(requeue);
                    if let Some(progress) = progress {
                        decode_progress.insert(seed.output.seq_id, progress);
                    }
                }
            }
        }
        let mut speculative = Vec::new();
        self.batch.clear();
        for (seed, scratch) in reserved_seeds {
            let SpeculativeSeed {
                output,
                proposal,
                sampler,
                probes,
            } = seed;
            let fork = match TargetKvFork::begin(
                &mut self.context,
                output.seq_id as i32,
                scratch.seq_id as i32,
                proposal.tokens.len(),
            ) {
                Ok(fork) => fork,
                Err(error) => {
                    if self
                        .scheduler
                        .release_scratch_sequence(
                            &mut self.context,
                            scratch.seq_id,
                            &mut Vec::new(),
                        )
                        .is_err()
                    {
                        self.unhealthy = true;
                        self.fail_all_active(RuntimeError::Internal(
                            "failed to release an uncommitted speculative scratch sequence"
                                .to_owned(),
                        ));
                        return true;
                    }
                    if let Some(active) = self.active.get(&output.seq_id) {
                        let _ = active.events.try_send(GenerationEvent::Failed(error));
                    }
                    early_terminal.push((output.seq_id, TerminalKind::Failed));
                    continue;
                }
            };
            let optimistic_queued = if matches!(proposal.mode, DraftMode::DraftModel(_)) {
                let optimistic_version = self
                    .active
                    .get(&output.seq_id)
                    .map_or(0, |active| active.speculation_version.saturating_add(1));
                match self.queue_draft_job(
                    output.seq_id,
                    optimistic_version,
                    &proposal.token_ids(),
                    proposal.tokens.len(),
                ) {
                    Ok(queued) => queued,
                    Err(error) => {
                        if self.rollback_speculative_fork(&fork).is_err() {
                            self.unhealthy = true;
                        }
                        self.fail_all_active(error);
                        return true;
                    }
                }
            } else {
                false
            };
            let verification_rows =
                match fork.append_proposals(&mut self.batch, &proposal.token_ids()) {
                    Ok(rows) => rows,
                    Err(error) => {
                        if let Err(rollback) = self.rollback_speculative_fork(&fork) {
                            self.unhealthy = true;
                            self.fail_all_active(rollback);
                            return true;
                        }
                        if let Some(active) = self.active.get(&output.seq_id) {
                            let _ = active.events.try_send(GenerationEvent::Failed(error));
                        }
                        early_terminal.push((output.seq_id, TerminalKind::Failed));
                        continue;
                    }
                };
            speculative.push(SpeculativeCandidate {
                output,
                proposal,
                fork,
                sampler,
                probes,
                verification_rows,
                optimistic_queued,
            });
        }
        for (seq_id, version) in draft_requeues.drain(..) {
            if let Some(actor) = self.draft_actor.as_ref() {
                actor.invalidate(seq_id, version);
            }
            if let Err(error) = self.queue_draft_job(seq_id, version, &[], MAX_DRAFT_TOKENS) {
                for candidate in &speculative {
                    let _ = self.rollback_speculative_fork(&candidate.fork);
                }
                self.unhealthy = true;
                self.fail_all_active(error);
                return true;
            }
        }
        if !speculative.is_empty() && self.context.decode(&mut self.batch).is_err() {
            for candidate in &speculative {
                let _ = self.rollback_speculative_fork(&candidate.fork);
            }
            self.handle_decode_failure(
                &plan,
                "shared speculative verification decode failed".to_owned(),
            );
            return true;
        }
        let mut prepared = Vec::new();
        let mut evaluation_failures = Vec::new();
        let mut evaluation_cancellations = Vec::new();
        for mut candidate in speculative {
            let sequence_id = candidate.output.seq_id;
            let compute_tokens =
                1_u32.saturating_add((candidate.proposal.tokens.len() as u32).saturating_mul(
                    if matches!(candidate.proposal.mode, DraftMode::DraftModel(_)) {
                        2
                    } else {
                        1
                    },
                ));
            decode_progress.insert(
                sequence_id,
                DecodeProgress {
                    seq_id: sequence_id,
                    compute_tokens,
                    output_tokens: 0,
                },
            );
            let evaluation = (|| -> Result<_, GenError> {
                let active = self.active.get(&sequence_id).ok_or_else(|| {
                    GenError::SpeculationInvalid("active sequence disappeared during verification")
                })?;
                for (index, row) in candidate.verification_rows.iter().enumerate() {
                    candidate
                        .sampler
                        .accept_proposal(candidate.proposal.tokens[index].token_id)?;
                    candidate.probes.push(active.executor.probe_distribution(
                        &mut candidate.sampler,
                        &mut self.context,
                        *row,
                    )?);
                }
                let verification =
                    TargetVerification::new(&candidate.proposal, candidate.probes.clone())?;
                let decision = active
                    .speculation
                    .decide(&candidate.proposal, &verification)?;
                let pending_probe = decision.accepted;
                let mut bundle = decision
                    .kv_tokens
                    .iter()
                    .enumerate()
                    .map(|(index, token_id)| ExternalBundleToken {
                        token_id: *token_id,
                        distribution: verification.probes[index].distribution.clone(),
                        sampler_probe_index: index,
                    })
                    .collect::<Vec<_>>();
                bundle.push(ExternalBundleToken {
                    token_id: decision.pending_token,
                    distribution: verification.probes[pending_probe].distribution.clone(),
                    sampler_probe_index: pending_probe,
                });
                let preview = active
                    .executor
                    .preview_external_bundle(&self.model, &bundle)?;
                Ok((decision, bundle, preview))
            })();
            match evaluation {
                Ok((decision, bundle, preview))
                    if self.active.get(&sequence_id).is_some_and(|active| {
                        active.control.state() == GenerationControlState::Running
                    }) =>
                {
                    let next_version = self.active[&sequence_id]
                        .speculation_version
                        .saturating_add(1);
                    prepared.push(PreparedSpeculativeCandidate {
                        candidate,
                        decision,
                        bundle,
                        preview,
                        compute_tokens,
                        next_version,
                    });
                }
                Ok(_) => evaluation_cancellations.push((candidate, compute_tokens)),
                Err(_)
                    if self.active.get(&sequence_id).is_some_and(|active| {
                        active.control.state() != GenerationControlState::Running
                    }) =>
                {
                    evaluation_cancellations.push((candidate, compute_tokens));
                }
                Err(error) => evaluation_failures.push((candidate, compute_tokens, error)),
            }
        }
        for (candidate, _, error) in evaluation_failures {
            let sequence_id = candidate.output.seq_id;
            if candidate.optimistic_queued {
                if let Some(actor) = self.draft_actor.as_ref() {
                    let minimum = self.active.get(&sequence_id).map_or(u64::MAX, |active| {
                        active.speculation_version.saturating_add(2)
                    });
                    actor.invalidate(sequence_id, minimum);
                }
            }
            if let Err(rollback) = self.rollback_speculative_fork(&candidate.fork) {
                self.unhealthy = true;
                self.fail_all_active(rollback);
                return true;
            }
            if let Some(active) = self.active.get(&sequence_id) {
                let _ = active.events.try_send(GenerationEvent::Failed(error));
            }
            early_terminal.push((sequence_id, TerminalKind::Failed));
        }
        for (candidate, _) in evaluation_cancellations {
            let sequence_id = candidate.output.seq_id;
            if candidate.optimistic_queued {
                if let Some(actor) = self.draft_actor.as_ref() {
                    let minimum = self.active.get(&sequence_id).map_or(u64::MAX, |active| {
                        active.speculation_version.saturating_add(2)
                    });
                    actor.invalidate(sequence_id, minimum);
                }
            }
            if let Err(rollback) = self.rollback_speculative_fork(&candidate.fork) {
                self.unhealthy = true;
                self.fail_all_active(rollback);
                return true;
            }
            if let Some(active) = self.active.get_mut(&sequence_id) {
                match active.executor.finish_from_control_try() {
                    Ok(Some(StepOutcome::Finished(reason))) => {
                        early_terminal.push((sequence_id, TerminalKind::Normal(reason)));
                    }
                    Ok(_) | Err(_) => early_terminal.push((sequence_id, TerminalKind::Failed)),
                }
            }
        }
        let mut committed_requeues = Vec::new();
        for prepared_candidate in prepared {
            let PreparedSpeculativeCandidate {
                candidate,
                decision,
                bundle,
                preview,
                compute_tokens,
                next_version,
            } = prepared_candidate;
            let sequence_id = candidate.output.seq_id;
            let optimistic_queued = candidate.optimistic_queued;
            if self
                .active
                .get(&sequence_id)
                .is_none_or(|active| active.control.state() != GenerationControlState::Running)
            {
                if let Some(actor) = self.draft_actor.as_ref() {
                    actor.invalidate(sequence_id, next_version.saturating_add(1));
                }
                if let Err(rollback) = self.rollback_speculative_fork(&candidate.fork) {
                    self.unhealthy = true;
                    self.fail_all_active(rollback);
                    return true;
                }
                if let Some(active) = self.active.get_mut(&sequence_id) {
                    match active.executor.finish_from_control_try() {
                        Ok(Some(StepOutcome::Finished(reason))) => {
                            early_terminal.push((sequence_id, TerminalKind::Normal(reason)));
                        }
                        Ok(_) | Err(_) => early_terminal.push((sequence_id, TerminalKind::Failed)),
                    }
                }
                continue;
            }
            if let Err(error) = candidate
                .fork
                .commit(&mut self.context, preview.kv_tokens())
            {
                if let Some(actor) = self.draft_actor.as_ref() {
                    actor.invalidate(sequence_id, next_version.saturating_add(1));
                }
                if let Err(rollback) = self.rollback_speculative_fork(&candidate.fork) {
                    self.unhealthy = true;
                    self.fail_all_active(rollback);
                    return true;
                }
                if let Some(active) = self.active.get(&sequence_id) {
                    let _ = active.events.try_send(GenerationEvent::Failed(error));
                }
                early_terminal.push((sequence_id, TerminalKind::Failed));
                continue;
            }
            if self
                .active
                .get(&sequence_id)
                .is_none_or(|active| active.control.state() != GenerationControlState::Running)
            {
                if let Some(actor) = self.draft_actor.as_ref() {
                    actor.invalidate(sequence_id, next_version.saturating_add(1));
                }
                if let Err(rollback) = self.rollback_speculative_fork(&candidate.fork) {
                    self.unhealthy = true;
                    self.fail_all_active(rollback);
                    return true;
                }
                if let Some(active) = self.active.get_mut(&sequence_id) {
                    match active.executor.finish_from_control_try() {
                        Ok(Some(StepOutcome::Finished(reason))) => {
                            early_terminal.push((sequence_id, TerminalKind::Normal(reason)));
                        }
                        Ok(_) | Err(_) => early_terminal.push((sequence_id, TerminalKind::Failed)),
                    }
                }
                continue;
            }
            let outcome = self.active.get_mut(&sequence_id).map(|active| {
                active
                    .executor
                    .commit_external_bundle_try(candidate.sampler, preview)
            });
            let outcome = match outcome {
                Some(Ok(outcome)) => outcome,
                Some(Err(error)) => {
                    if let Some(actor) = self.draft_actor.as_ref() {
                        actor.invalidate(sequence_id, next_version.saturating_add(1));
                    }
                    if let Err(rollback) = self.rollback_speculative_fork(&candidate.fork) {
                        self.unhealthy = true;
                        self.fail_all_active(rollback);
                        return true;
                    }
                    if let Some(active) = self.active.get(&sequence_id) {
                        let _ = active.events.try_send(GenerationEvent::Failed(error));
                    }
                    early_terminal.push((sequence_id, TerminalKind::Failed));
                    continue;
                }
                None => continue,
            };
            if let Err(error) = self.release_committed_speculative_fork(&candidate.fork) {
                self.unhealthy = true;
                self.fail_all_active(error);
                return true;
            }
            let Some(active) = self.active.get_mut(&sequence_id) else {
                continue;
            };
            if active
                .speculation
                .record_commit_prefix(&decision, outcome.output_tokens)
                .is_err()
            {
                early_terminal.push((sequence_id, TerminalKind::Failed));
                continue;
            }
            active.generated_tokens.extend(
                bundle
                    .iter()
                    .take(outcome.output_tokens)
                    .map(|token| token.token_id),
            );
            if active.executor.usage().completion_tokens != active.generated_tokens.len() {
                early_terminal.push((sequence_id, TerminalKind::Failed));
                continue;
            }
            active.speculation_proposed_tokens =
                u32::try_from(active.speculation.counters.proposed).unwrap_or(u32::MAX);
            active.speculation_accepted_tokens =
                u32::try_from(active.speculation.counters.accepted).unwrap_or(u32::MAX);
            let preserve_optimistic = optimistic_queued
                && decision.accepted == decision.proposed
                && outcome.output_tokens == decision.accepted.saturating_add(1)
                && matches!(&outcome.outcome, StepOutcome::Continue { .. });
            let committed_version = if optimistic_queued && !preserve_optimistic {
                next_version.saturating_add(1)
            } else {
                next_version
            };
            if optimistic_queued && !preserve_optimistic {
                if let Some(actor) = self.draft_actor.as_ref() {
                    actor.invalidate(sequence_id, committed_version);
                }
            }
            active.speculation_version = committed_version;
            active.optimistic_bonus = preserve_optimistic.then_some(outcome.pending_token);
            decode_progress.insert(
                sequence_id,
                DecodeProgress {
                    seq_id: sequence_id,
                    compute_tokens,
                    output_tokens: u32::try_from(outcome.output_tokens).unwrap_or(u32::MAX),
                },
            );
            match outcome.outcome {
                StepOutcome::Continue { token_id } => {
                    active.next_token = Some(token_id);
                    self.watchdog
                        .lock()
                        .unwrap_or_else(|poisoned| poisoned.into_inner())
                        .progress(active.scheduler_id);
                    if !preserve_optimistic {
                        committed_requeues.push((sequence_id, committed_version));
                    }
                }
                StepOutcome::Finished(reason) => {
                    early_terminal.push((sequence_id, TerminalKind::Normal(reason)));
                }
            }
        }
        for (seq_id, version) in committed_requeues {
            if let Some(actor) = self.draft_actor.as_ref() {
                actor.invalidate(seq_id, version);
            }
            if let Err(error) = self.queue_draft_job(seq_id, version, &[], MAX_DRAFT_TOKENS) {
                self.unhealthy = true;
                self.fail_all_active(error);
                return true;
            }
        }
        for (seq_id, terminal) in &mut early_terminal {
            if !matches!(
                terminal,
                TerminalKind::Normal(
                    StopReason::StopString(_) | StopReason::EndToken(_) | StopReason::MaxTokens
                )
            ) {
                continue;
            }
            #[cfg(all(feature = "contract-test-controls", debug_assertions))]
            if matches!(terminal, TerminalKind::Normal(StopReason::StopString(_))) {
                if let Some(active) = self.active.get(seq_id) {
                    contract_hold_stop_boundary(&active.request.request_id);
                }
            }
            if let Err(error) = self.save_active_session(*seq_id) {
                if let Some(active) = self.active.get(seq_id) {
                    let _ = active
                        .events
                        .try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
                }
                tracing::error!(
                    sequence_id = *seq_id,
                    error = %error,
                    "failed to persist generation session"
                );
                *terminal = TerminalKind::Failed;
            }
        }
        let mut scheduler_events = Vec::new();
        let decode_progress = decode_progress.values().copied().collect::<Vec<_>>();
        let receipt = match self.scheduler.commit_step_with_progress(
            &mut self.context,
            plan,
            &decode_progress,
            &mut scheduler_events,
        ) {
            Ok(receipt) => receipt,
            Err(error) => {
                self.unhealthy = true;
                self.fail_all_active(RuntimeError::from(error));
                return true;
            }
        };
        self.emit_scheduler_events(scheduler_events);
        if let Err(error) = self.suspend_runtime_sequences(&receipt.suspended) {
            tracing::error!(error = %error, "runtime suspension ownership diverged from scheduler");
            self.unhealthy = true;
            self.fail_all_active(error);
            return true;
        }
        let released: BTreeSet<_> = receipt
            .released
            .iter()
            .map(|release| release.sequence.seq_id)
            .collect();
        for release in receipt.released {
            let early_kind = early_terminal
                .iter()
                .find(|(seq_id, _)| *seq_id == release.sequence.seq_id)
                .map(|(_, kind)| kind.clone());
            let kind = early_kind.clone().unwrap_or_else(|| {
                terminal_from_scheduler(release.sequence.reason, self.model.end_token())
            });
            if early_kind.is_none() {
                self.emit_scheduler_terminal(release.sequence.seq_id, &kind);
            }
            self.finalize_active_and_recycle(release.sequence.seq_id, kind);
        }
        for (seq_id, kind) in early_terminal {
            if released.contains(&seq_id) {
                continue;
            }
            let reason = match &kind {
                TerminalKind::Normal(reason) => termination_reason(reason),
                TerminalKind::Failed => TerminationReason::BackendFailure,
            };
            if self
                .scheduler
                .terminate(&mut self.context, seq_id, reason, &mut Vec::new())
                .is_ok()
            {
                self.finalize_active_and_recycle(seq_id, kind);
            }
        }
        self.step = self.step.saturating_add(1);
        true
    }

    fn record_generated_token(active: &mut ActiveGeneration) -> Result<(), RuntimeError> {
        let usage = active.executor.usage();
        if usage.completion_tokens != active.generated_tokens.len().saturating_add(1) {
            return Err(RuntimeError::Internal(
                "generation token history diverged from executor usage".to_owned(),
            ));
        }
        if active.generated_tokens.len() >= active.request.max_tokens as usize {
            return Err(RuntimeError::Internal(
                "generation token history exceeded the request budget".to_owned(),
            ));
        }
        let token = active.executor.last_sampled_token().ok_or_else(|| {
            RuntimeError::Internal("executor usage advanced without a sampled token".to_owned())
        })?;
        active.generated_tokens.push(token);
        Ok(())
    }

    fn tokenize(&self, text: &str, add_special: bool) -> Result<Vec<i32>, RuntimeError> {
        self.model
            .tokenize(text, add_special, true)
            .map_err(|error| RuntimeError::ModelCorrupt {
                path: self.info.path.clone(),
                reason: format!("native tokenization failed: {error}"),
            })
    }

    fn tokenize_batch(
        &self,
        items: &[String],
        add_special: bool,
    ) -> Result<Vec<Vec<i32>>, RuntimeError> {
        let mut total_tokens = 0_usize;
        let mut results = Vec::with_capacity(items.len());
        for item in items {
            let tokens = self.tokenize(item, add_special)?;
            total_tokens = checked_batch_token_total(total_tokens, tokens.len())?;
            results.push(tokens);
        }
        Ok(results)
    }

    fn count_tokenized(
        &self,
        items: &[String],
        add_special: bool,
    ) -> Result<Vec<u32>, RuntimeError> {
        let mut total_tokens = 0_usize;
        let mut counts = Vec::with_capacity(items.len());
        for item in items {
            let count = self.tokenize(item, add_special)?.len();
            total_tokens = checked_batch_token_total(total_tokens, count)?;
            counts.push(
                u32::try_from(count).map_err(|_| RuntimeError::ContextOverflow {
                    requested: u32::MAX,
                    limit: MAX_BATCH_ACTUAL_TOKENS as u32,
                })?,
            );
        }
        Ok(counts)
    }

    fn embeddings(&mut self, items: &[String]) -> Result<Vec<Vec<f32>>, RuntimeError> {
        if !self.info.supports_embeddings || self.info.embedding_length == 0 {
            return Err(RuntimeError::UnsupportedParam(
                "loaded model does not advertise embedding output".to_owned(),
            ));
        }
        if items.is_empty() {
            return Err(RuntimeError::UnsupportedParam(
                "embedding input must not be empty".to_owned(),
            ));
        }
        validate_embedding_response_shape(
            items.len(),
            self.info.embedding_length as usize,
            self.native_sequence_capacity as usize,
        )?;
        let tokenized = self.tokenize_batch(items, true)?;
        let mut total_tokens = 0_usize;
        let mut inputs = Vec::with_capacity(tokenized.len());
        for tokens in tokenized {
            if tokens.len() > self.info.context_length as usize {
                return Err(RuntimeError::ContextOverflow {
                    requested: u32::try_from(tokens.len()).unwrap_or(u32::MAX),
                    limit: self.info.context_length,
                });
            }
            total_tokens =
                total_tokens
                    .checked_add(tokens.len())
                    .ok_or(RuntimeError::ContextOverflow {
                        requested: u32::MAX,
                        limit: self.info.context_length,
                    })?;
            inputs.push(EmbeddingInput { tokens });
        }
        validate_embedding_intermediate(total_tokens, self.info.embedding_length as usize)?;
        let total_tokens = inputs.iter().try_fold(0_u32, |total, input| {
            u32::try_from(input.tokens.len())
                .ok()
                .and_then(|length| total.checked_add(length))
        });
        let total_tokens = total_tokens.ok_or_else(|| RuntimeError::ContextOverflow {
            requested: u32::MAX,
            limit: self.info.context_length,
        })?;
        if total_tokens > self.info.context_length {
            return Err(RuntimeError::ContextOverflow {
                requested: total_tokens,
                limit: self.info.context_length,
            });
        }
        if self.embedding_context.is_none() {
            let mut context = self
                .model
                .context_with(ContextOptions {
                    context_tokens: self.info.context_length,
                    batch_tokens: self.info.context_length,
                    micro_batch_tokens: self.info.context_length.min(512),
                    max_sequences: self.native_sequence_capacity,
                    unified_kv: true,
                    embeddings: true,
                    pooling: Some(EmbeddingPooling::None),
                })
                .map_err(|error| {
                    RuntimeError::Internal(format!("embedding context failed: {error}"))
                })?;
            if let Some(adapter) = &self.lora {
                context
                    .set_lora_adapters(&[(&adapter.adapter, adapter.guard.record().scale)])
                    .map_err(|error| {
                        RuntimeError::Internal(format!("embedding LoRA failed: {error}"))
                    })?;
            }
            self.embedding_context = Some(context);
        }
        let result = (|| {
            let context = self.embedding_context.as_mut().ok_or_else(|| {
                RuntimeError::Internal("embedding context was not retained".to_owned())
            })?;
            for sequence in 0..items.len() {
                context
                    .memory_seq_rm(sequence as i32, -1, -1)
                    .map_err(|error| {
                        RuntimeError::Internal(format!(
                            "embedding preflight cleanup failed: {error}"
                        ))
                    })?;
            }
            let mut backend = NativeEmbeddingBackend::new(context);
            let embeddings = execute_embedding_batch(
                &mut backend,
                &inputs,
                EmbeddingOptions {
                    pooling: PoolingMode::Mean,
                    normalize: true,
                },
            )
            .map_err(gen_error)?;
            for sequence in 0..items.len() {
                context
                    .memory_seq_rm(sequence as i32, -1, -1)
                    .map_err(|error| {
                        RuntimeError::Internal(format!("embedding cleanup failed: {error}"))
                    })?;
            }
            Ok(embeddings)
        })();
        if result.is_err() {
            self.embedding_context = None;
        }
        result
    }

    fn resolve_prefix_ref(
        &self,
        request: &GenerateRequest,
        tokens: &[i32],
    ) -> Result<Option<String>, RuntimeError> {
        let mut best: Option<(&String, usize)> = None;
        for (name, content_hash) in &request.prefix_refs {
            let prefix = self.prefixes.get(name).ok_or_else(|| {
                RuntimeError::UnsupportedParam(format!("unknown prefix reference: {name}"))
            })?;
            if prefix.content_hash != *content_hash {
                return Err(RuntimeError::UnsupportedParam(format!(
                    "prefix content hash does not match registration: {name}"
                )));
            }
            if !prefix.pinned {
                return Err(RuntimeError::UnsupportedParam(format!(
                    "prefix is not pinned: {name}"
                )));
            }
            if !tokens.starts_with(&prefix.tokens) || prefix.tokens.len() >= tokens.len() {
                return Err(RuntimeError::UnsupportedParam(format!(
                    "prompt does not extend registered prefix: {name}"
                )));
            }
            if best.is_none_or(|(_, length)| prefix.tokens.len() > length) {
                best = Some((name, prefix.tokens.len()));
            }
        }
        Ok(best.map(|(name, _)| name.clone()))
    }

    fn prefix(&mut self, command: PrefixCommand) -> Result<PrefixResult, RuntimeError> {
        let mut scheduler_events = Vec::new();
        let result = match command {
            PrefixCommand::Register {
                name,
                content,
                content_hash,
            } => {
                let actual_hash = format!("{:x}", Sha256::digest(content.as_bytes()));
                if actual_hash != content_hash.to_ascii_lowercase() {
                    return Err(RuntimeError::UnsupportedParam(
                        "prefix content_hash does not match SHA-256(content)".to_owned(),
                    ));
                }
                let tokens = self.tokenize(&content, true)?;
                let token_count =
                    u32::try_from(tokens.len()).map_err(|_| RuntimeError::ContextOverflow {
                        requested: u32::MAX,
                        limit: self.info.context_length,
                    })?;
                self.scheduler.register_prefix(
                    name.clone(),
                    tokens.clone(),
                    token_count,
                    &mut scheduler_events,
                )?;
                self.prefixes.insert(
                    name.clone(),
                    NativePrefix {
                        content_hash: content_hash.clone(),
                        tokens,
                        pinned: false,
                    },
                );
                PrefixResult {
                    name,
                    content_hash,
                    token_count,
                    pinned: false,
                }
            }
            PrefixCommand::Pin { name, content_hash } => {
                let (tokens, already_pinned) = {
                    let prefix = self.valid_prefix(&name, &content_hash)?;
                    (prefix.tokens.clone(), prefix.pinned)
                };
                if !already_pinned {
                    let seq_id = self.scheduler.pin_prefix(&name, &mut scheduler_events)?;
                    if let Err(error) = self.decode_prefix(seq_id, &tokens) {
                        let _ = self.scheduler.unpin_prefix(
                            &mut self.context,
                            &name,
                            &mut scheduler_events,
                        );
                        return Err(error);
                    }
                    if let Some(prefix) = self.prefixes.get_mut(&name) {
                        prefix.pinned = true;
                    }
                }
                PrefixResult {
                    name,
                    content_hash,
                    token_count: tokens.len() as u32,
                    pinned: true,
                }
            }
            PrefixCommand::Unpin { name, content_hash } => {
                let (token_count, pinned) = {
                    let prefix = self.valid_prefix(&name, &content_hash)?;
                    (prefix.tokens.len() as u32, prefix.pinned)
                };
                if pinned {
                    self.scheduler
                        .unpin_prefix(&mut self.context, &name, &mut scheduler_events)?;
                    if let Some(prefix) = self.prefixes.get_mut(&name) {
                        prefix.pinned = false;
                    }
                }
                PrefixResult {
                    name,
                    content_hash,
                    token_count,
                    pinned: false,
                }
            }
        };
        self.emit_scheduler_events(scheduler_events);
        Ok(result)
    }

    fn valid_prefix(&self, name: &str, content_hash: &str) -> Result<&NativePrefix, RuntimeError> {
        let prefix = self
            .prefixes
            .get(name)
            .ok_or_else(|| RuntimeError::UnsupportedParam(format!("unknown prefix: {name}")))?;
        if prefix.content_hash != content_hash {
            return Err(RuntimeError::UnsupportedParam(format!(
                "prefix content hash does not match registration: {name}"
            )));
        }
        Ok(prefix)
    }

    fn decode_prefix(&mut self, seq_id: SeqId, tokens: &[i32]) -> Result<(), RuntimeError> {
        let chunk_size = self.config.scheduler.batch_token_budget.max(1) as usize;
        for (chunk_index, chunk) in tokens.chunks(chunk_size).enumerate() {
            self.batch.clear();
            let start = chunk_index.saturating_mul(chunk_size);
            for (offset, token) in chunk.iter().enumerate() {
                self.batch
                    .add_token(*token, (start + offset) as i32, &[seq_id as i32], false)
                    .map_err(|error| {
                        RuntimeError::Internal(format!("prefix batch failed: {error}"))
                    })?;
            }
            if let Err(error) = self.context.decode(&mut self.batch) {
                self.unhealthy = true;
                return Err(RuntimeError::Internal(format!(
                    "prefix decode failed and invalidated the model worker: {error}"
                )));
            }
        }
        Ok(())
    }

    fn session(
        &mut self,
        action: SessionAction,
        session_id: &str,
        principal_id: &str,
    ) -> Result<(), RuntimeError> {
        if session_id.is_empty() {
            return Err(RuntimeError::UnsupportedParam(
                "session_id must not be empty".to_owned(),
            ));
        }
        let key = SessionKey::new(principal_id, self.model_fingerprint, session_id)
            .map_err(session_store_error)?;
        match action {
            SessionAction::Create => match self.sessions.read(&key) {
                Ok(_) => Ok(()),
                Err(SessionStoreError::Unknown) => {
                    let reservation = self
                        .sessions
                        .reserve(&key, 0)
                        .map_err(session_store_error)?;
                    self.sessions
                        .write(reservation, &[])
                        .map(|_| ())
                        .map_err(session_store_error)
                }
                Err(error) => Err(session_access_error(error, session_id)),
            },
            SessionAction::Save => {
                self.sessions
                    .read(&key)
                    .map_err(|error| session_access_error(error, session_id))?;
                if self.pending.values().any(|pending| {
                    pending.request.session_id.as_deref() == Some(session_id)
                        && pending.request.principal_id == principal_id
                        && pending.executor.is_some()
                }) {
                    return Err(RuntimeError::UnsupportedParam(
                        "session cannot be saved while KV readmission is pending".to_owned(),
                    ));
                }
                let active_seq = self.active.iter().find_map(|(&seq_id, active)| {
                    (active.request.session_id.as_deref() == Some(session_id)
                        && active.request.principal_id == principal_id)
                        .then_some(seq_id)
                });
                if let Some(seq_id) = active_seq {
                    self.save_active_session(seq_id)?;
                }
                Ok(())
            }
            SessionAction::Resume => self
                .sessions
                .read(&key)
                .map(|_| ())
                .map_err(|error| session_access_error(error, session_id)),
            SessionAction::Delete => {
                if self.session_in_use(session_id, principal_id) {
                    return Err(RuntimeError::UnsupportedParam(
                        "session cannot be deleted while generation work is active".to_owned(),
                    ));
                }
                self.sessions
                    .delete(&key)
                    .map_err(|error| session_access_error(error, session_id))
            }
        }
    }

    fn session_in_use(&self, session_id: &str, principal_id: &str) -> bool {
        self.pending.values().any(|pending| {
            pending.request.session_id.as_deref() == Some(session_id)
                && pending.request.principal_id == principal_id
        }) || self.active.values().any(|active| {
            active.request.session_id.as_deref() == Some(session_id)
                && active.request.principal_id == principal_id
        })
    }

    fn save_active_session(&mut self, seq_id: SeqId) -> Result<(), RuntimeError> {
        let Some(active) = self.active.get(&seq_id) else {
            return Err(RuntimeError::Internal(
                "cannot save an unknown active sequence".to_owned(),
            ));
        };
        let Some(session_id) = active.request.session_id.clone() else {
            return Ok(());
        };
        let Some(last_token) = active.executor.last_sampled_token() else {
            return Err(RuntimeError::UnsupportedParam(
                "session has not produced a continuation token".to_owned(),
            ));
        };
        let token_positions = u32::try_from(active.tokens.len())
            .ok()
            .and_then(|tokens| active.position_base.checked_add(tokens))
            .and_then(|position| {
                u32::try_from(active.executor.usage().completion_tokens)
                    .ok()
                    .and_then(|completion| position.checked_add(completion))
            })
            .ok_or_else(|| RuntimeError::ContextOverflow {
                requested: u32::MAX,
                limit: self.info.context_length,
            })?;
        let continuation =
            SessionContinuation::new(self.model_fingerprint, last_token, token_positions)?;
        let owner = principal_fingerprint(&active.request.principal_id)?;
        let key = SessionKey::new(
            &active.request.principal_id,
            self.model_fingerprint,
            &session_id,
        )
        .map_err(session_store_error)?;
        let payload_size = self
            .scheduler
            .session_payload_size(&mut self.context, seq_id)?;
        let reservation = self
            .sessions
            .reserve(&key, payload_size)
            .map_err(session_store_error)?;
        let payload = self.scheduler.export_session_payload(
            &mut self.context,
            seq_id,
            continuation,
            owner,
        )?;
        self.sessions
            .write(reservation, &payload)
            .map(|_| ())
            .map_err(session_store_error)
    }

    fn set_lora(&mut self, adapter: Option<VerifiedLoadGuard>) -> Result<(), RuntimeError> {
        if !self.active.is_empty() || !self.pending.is_empty() {
            return Err(RuntimeError::UnsupportedParam(
                "LoRA cannot change while generation work is active".to_owned(),
            ));
        }
        let loaded = adapter
            .map(|guard| {
                let adapter = self
                    .model
                    .lora_adapter(guard.native_path())
                    .map_err(|error| {
                        RuntimeError::Internal(format!("LoRA load failed: {error}"))
                    })?;
                self.adapters
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .verify_loaded_for_base(&guard, &digest_hex(self.model_fingerprint))
                    .map_err(adapter_registry_error)?;
                Ok::<_, RuntimeError>(ActiveNativeLora { adapter, guard })
            })
            .transpose()?;
        let replacement: Vec<_> = loaded
            .as_ref()
            .map(|adapter| vec![(&adapter.adapter, adapter.guard.record().scale)])
            .unwrap_or_default();
        let previous: Vec<_> = self
            .lora
            .as_ref()
            .map(|adapter| vec![(&adapter.adapter, adapter.guard.record().scale)])
            .unwrap_or_default();
        if let Err(error) = apply_lora_transaction(
            &mut self.context,
            self.embedding_context.as_mut(),
            previous.as_slice(),
            replacement.as_slice(),
        ) {
            if error.rollback_failed() {
                self.unhealthy = true;
            }
            return Err(RuntimeError::Internal(error.detail()));
        }
        self.lora = loaded;
        Ok(())
    }

    fn render_chat(&self, messages: &[(String, String)]) -> Result<String, RuntimeError> {
        let messages = messages
            .iter()
            .map(|(role, content)| {
                let role = match role.as_str() {
                    "system" => ChatRole::System,
                    "user" => ChatRole::User,
                    "assistant" => ChatRole::Assistant,
                    "tool" => ChatRole::Tool,
                    _ => {
                        return Err(RuntimeError::UnsupportedParam(format!(
                            "chat role is not supported: {role}"
                        )))
                    }
                };
                Ok(ChatMessage { role, content })
            })
            .collect::<Result<Vec<_>, _>>()?;
        let rendered = self
            .chat_template
            .render_chat(&self.model, &messages, true)
            .map_err(|_| RuntimeError::TemplateUntrusted)?;
        String::from_utf8(rendered).map_err(|error| RuntimeError::ModelCorrupt {
            path: self.info.path.clone(),
            reason: format!("chat template produced invalid UTF-8: {error}"),
        })
    }

    fn reject_admission(&mut self, scheduler_id: u64, error: RuntimeError) {
        if let Some(pending) = self.pending.remove(&scheduler_id) {
            if pending.admitted_at.is_some() {
                self.record_terminal_metrics(scheduler_id, TerminalOutcome::Failed);
            }
            self.fail_unadmitted(scheduler_id, &pending.request, &pending.events, error);
        }
    }

    fn record_terminal_metrics(&self, scheduler_id: u64, outcome: TerminalOutcome) {
        if let Err(error) = self.metrics.record_terminal(scheduler_id, outcome) {
            tracing::error!(
                scheduler_id,
                error = %error,
                "request metrics terminal invariant failed"
            );
        }
    }

    fn fail_unadmitted(
        &self,
        scheduler_id: u64,
        request: &GenerateRequest,
        events: &GenerationSender,
        error: RuntimeError,
    ) {
        tracing::warn!(
            runtime_error_code = runtime_code(&error),
            request_id = request.request_id,
            trace_id = request.trace_id,
            model_id = request.model.as_deref().unwrap_or(self.info.id.as_str()),
            "generation request failed at model-worker boundary"
        );
        let _ = events.try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
        emit_observable(
            &self.telemetry,
            &self.metrics,
            EngineEvent::RequestFailed {
                request_id: request.request_id.clone(),
                trace_id: request.trace_id.clone(),
                model_id: request
                    .model
                    .clone()
                    .unwrap_or_else(|| self.info.id.clone()),
                code: runtime_code(&error).to_owned(),
                priority_class: priority_name(request.priority).to_owned(),
            },
            "request_rejection",
        );
        self.requests.remove_scheduler(scheduler_id);
    }

    fn fail_planned_sequences(&mut self, plan: &crate::sched::BatchPlan, error: RuntimeError) {
        let seq_ids: BTreeSet<_> = plan.work.iter().map(|work| work.seq_id).collect();
        for seq_id in seq_ids {
            if let Some(active) = self.active.get(&seq_id) {
                let _ = active
                    .events
                    .try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
            }
            if self
                .scheduler
                .terminate(
                    &mut self.context,
                    seq_id,
                    TerminationReason::BackendFailure,
                    &mut Vec::new(),
                )
                .is_ok()
            {
                self.finalize_active_and_recycle(seq_id, TerminalKind::Failed);
            }
        }
    }

    fn handle_decode_failure(&mut self, plan: &crate::sched::BatchPlan, detail: String) {
        if let Some(scheduler_id) = self
            .active
            .values()
            .map(|active| active.scheduler_id)
            .next()
        {
            let events = self
                .watchdog
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .handle_oom(scheduler_id);
            for event in events {
                tracing::warn!(
                    sequence_id = event.sequence_id,
                    action = ?event.action,
                    detail = event.detail,
                    "decode OOM watchdog action"
                );
            }
        }
        tracing::error!(
            planned_sequences = plan.work.len(),
            error = detail,
            "shared native decode failed; invalidating model worker"
        );
        self.unhealthy = true;
        self.fail_all_active(RuntimeError::Oom(detail));
    }

    fn fail_all_active(&mut self, error: RuntimeError) {
        let pending_ids: Vec<_> = self.pending.keys().copied().collect();
        for scheduler_id in pending_ids {
            let _ = self.scheduler.drop_queued(scheduler_id);
            if let Some(pending) = self.pending.remove(&scheduler_id) {
                if pending.admitted_at.is_some() {
                    self.record_terminal_metrics(scheduler_id, TerminalOutcome::Failed);
                }
                self.fail_unadmitted(
                    scheduler_id,
                    &pending.request,
                    &pending.events,
                    error.clone(),
                );
                self.watchdog
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .complete(scheduler_id);
            }
        }
        let seq_ids: Vec<_> = self.active.keys().copied().collect();
        for seq_id in seq_ids {
            if let Some(active) = self.active.get(&seq_id) {
                let _ = active
                    .events
                    .try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
            }
            if let Err(cleanup_error) = self.scheduler.terminate(
                &mut self.context,
                seq_id,
                TerminationReason::BackendFailure,
                &mut Vec::new(),
            ) {
                tracing::error!(
                    sequence_id = seq_id,
                    error = %cleanup_error,
                    "failed to clean sequence after shared worker failure"
                );
            }
            self.finalize_active_and_recycle(seq_id, TerminalKind::Failed);
        }
    }

    fn emit_scheduler_terminal(&self, seq_id: SeqId, terminal: &TerminalKind) {
        let Some(active) = self.active.get(&seq_id) else {
            return;
        };
        match terminal {
            TerminalKind::Normal(reason) => {
                let _ = active.events.try_send(GenerationEvent::Finished {
                    reason: reason.clone(),
                    usage: active.executor.usage(),
                    confidence: None,
                });
            }
            TerminalKind::Failed => {
                let _ = active
                    .events
                    .try_send(GenerationEvent::Failed(GenError::Backend(
                        "scheduler terminated generation".to_owned(),
                    )));
            }
        }
    }

    fn finalize_active_and_recycle(&mut self, seq_id: SeqId, terminal: TerminalKind) {
        if let Some(permit) = self.finalize_active(seq_id, terminal) {
            self.retain_permit_for_suspended(permit);
        }
        self.trim_suspended_permits();
    }

    fn finalize_active(
        &mut self,
        seq_id: SeqId,
        terminal: TerminalKind,
    ) -> Option<GlobalSlotPermit> {
        let Some(mut active) = self.active.remove(&seq_id) else {
            return None;
        };
        self.retire_draft_sequence(
            seq_id,
            active.scheduler_id,
            active.speculation_version.saturating_add(1),
        );
        let completed_at = Instant::now();
        let usage = active.executor.usage();
        let queue_ms = duration_ms(
            active
                .admitted_at
                .saturating_duration_since(active.queued_at),
        );
        let prefill_completed = active.prefill_completed_at.unwrap_or(completed_at);
        let prefill_ms =
            duration_ms(prefill_completed.saturating_duration_since(active.admitted_at));
        let decode_ms = duration_ms(completed_at.saturating_duration_since(prefill_completed));
        let prompt_tokens = u32::try_from(usage.prompt_tokens).unwrap_or(u32::MAX);
        let completion_tokens = u32::try_from(usage.completion_tokens).unwrap_or(u32::MAX);
        let elapsed = completed_at
            .saturating_duration_since(active.admitted_at)
            .as_secs_f64();
        let tokens_per_second = if completion_tokens == 0 {
            0.0
        } else {
            f64::from(completion_tokens) / elapsed.max(0.000_001)
        };
        let successful = matches!(
            &terminal,
            TerminalKind::Normal(
                StopReason::StopString(_) | StopReason::EndToken(_) | StopReason::MaxTokens
            )
        );
        if successful {
            let request_metrics = RequestMetrics {
                queue_ms,
                prefill_ms,
                decode_ms,
                prompt_tokens,
                completion_tokens,
                tokens_per_second,
                prefix_hit_tokens: active.prefix_hit_tokens,
                speculation_proposed_tokens: active.speculation_proposed_tokens,
                speculation_accepted_tokens: active.speculation_accepted_tokens,
                speculation_acceptance_rate: (active.speculation_proposed_tokens > 0).then(|| {
                    f64::from(active.speculation_accepted_tokens)
                        / f64::from(active.speculation_proposed_tokens)
                }),
            };
            if let Err(error) = self.metrics.record_success(
                active.scheduler_id,
                active.request.role.as_str(),
                &request_metrics,
            ) {
                tracing::error!(
                    scheduler_id = active.scheduler_id,
                    error = %error,
                    "request metrics success invariant failed"
                );
            }
            emit_observable(
                &self.telemetry,
                &self.metrics,
                EngineEvent::RequestComplete {
                    request_id: active.request.request_id.clone(),
                    trace_id: active.request.trace_id.clone(),
                    model_id: active
                        .request
                        .model
                        .clone()
                        .unwrap_or_else(|| self.info.id.clone()),
                    queue_ms,
                    prefill_ms,
                    decode_ms,
                    input_tokens: prompt_tokens,
                    output_tokens: completion_tokens,
                    tok_per_s: tokens_per_second,
                    prefix_hit_tokens: active.prefix_hit_tokens,
                    speculation_proposed_tokens: active.speculation_proposed_tokens,
                    speculation_accepted_tokens: active.speculation_accepted_tokens,
                    spec_accept_rate: (active.speculation_proposed_tokens > 0).then(|| {
                        f64::from(active.speculation_accepted_tokens)
                            / f64::from(active.speculation_proposed_tokens)
                    }),
                    priority_class: priority_name(active.request.priority).to_owned(),
                    eval_slot: active.request.eval_slot.or(active.slot_id).unwrap_or(0),
                },
                "request_complete",
            );
        } else {
            let (code, outcome) = match terminal {
                TerminalKind::Normal(StopReason::DeadlineExceeded) => {
                    ("eval_timeout", TerminalOutcome::Failed)
                }
                TerminalKind::Normal(StopReason::Cancelled | StopReason::Disconnected) => {
                    ("cancelled", TerminalOutcome::Cancelled)
                }
                TerminalKind::Normal(_) | TerminalKind::Failed => {
                    ("internal", TerminalOutcome::Failed)
                }
            };
            self.record_terminal_metrics(active.scheduler_id, outcome);
            emit_observable(
                &self.telemetry,
                &self.metrics,
                EngineEvent::RequestFailed {
                    request_id: active.request.request_id.clone(),
                    trace_id: active.request.trace_id.clone(),
                    model_id: active
                        .request
                        .model
                        .clone()
                        .unwrap_or_else(|| self.info.id.clone()),
                    code: code.to_owned(),
                    priority_class: priority_name(active.request.priority).to_owned(),
                },
                "request_failed",
            );
        }
        self.watchdog
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .complete(active.scheduler_id);
        self.requests.remove_scheduler(active.scheduler_id);
        active.permit.take()
    }

    fn emit_scheduler_events(&self, events: Vec<SchedEvent>) {
        for event in events {
            let telemetry_event = match event {
                SchedEvent::SlotState { slot_id, to, .. } => Some(EngineEvent::SlotState {
                    slot_id,
                    state: format!("{to:?}").to_ascii_lowercase(),
                }),
                SchedEvent::PrefixRegistered { name } => Some(EngineEvent::PrefixRegistered {
                    tokens: self
                        .prefixes
                        .get(&name)
                        .and_then(|prefix| u32::try_from(prefix.tokens.len()).ok())
                        .unwrap_or(0),
                    name,
                }),
                SchedEvent::PrefixHit {
                    name,
                    prefix_hit_tokens,
                } => Some(EngineEvent::PrefixHit {
                    name,
                    tokens: u32::try_from(prefix_hit_tokens).unwrap_or(u32::MAX),
                }),
                SchedEvent::Admission { .. }
                | SchedEvent::BatchStep { .. }
                | SchedEvent::KvOccupancy { .. }
                | SchedEvent::BackgroundEvicted { .. } => None,
            };
            if let Some(event) = telemetry_event {
                emit_observable(&self.telemetry, &self.metrics, event, "scheduler_event");
            }
        }
    }

    fn update_gauges(&self) {
        let snapshot = self.scheduler.snapshot();
        let kv = kv_percent(snapshot.kv_used_cells, &self.config);
        let physically_active = snapshot
            .active
            .iter()
            .filter(|receipt| receipt.slot_id.is_some())
            .count();
        self.metrics
            .update_gauges(snapshot.queue_depth, physically_active, kv);
        emit_observable(
            &self.telemetry,
            &self.metrics,
            EngineEvent::Gauges {
                slots_busy: physically_active,
                queue_depth: snapshot.queue_depth,
                vram_used_mb: None,
                kv_occupancy_pct: kv,
            },
            "scheduler_gauges",
        );
    }

    fn shutdown_all(&mut self) {
        let pending_ids: Vec<_> = self.pending.keys().copied().collect();
        for scheduler_id in pending_ids {
            let _ = self.scheduler.drop_queued(scheduler_id);
            if let Some(pending) = self.pending.remove(&scheduler_id) {
                if pending.admitted_at.is_some() {
                    self.record_terminal_metrics(scheduler_id, TerminalOutcome::Failed);
                }
                let error = RuntimeError::Draining;
                let _ = pending
                    .events
                    .try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
                self.requests.remove_scheduler(scheduler_id);
            }
        }
        let seq_ids: Vec<_> = self.active.keys().copied().collect();
        for seq_id in seq_ids {
            if let Some(active) = self.active.get(&seq_id) {
                let error = RuntimeError::Draining;
                let _ = active
                    .events
                    .try_send(GenerationEvent::Failed(runtime_gen_error(&error)));
            }
            if let Err(error) = self.scheduler.terminate(
                &mut self.context,
                seq_id,
                TerminationReason::Drained,
                &mut Vec::new(),
            ) {
                tracing::error!(
                    sequence_id = seq_id,
                    error = %error,
                    "failed to clean active sequence during worker shutdown"
                );
            }
            self.finalize_active_and_recycle(seq_id, TerminalKind::Failed);
        }
        self.suspended_permits.clear();
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn gen_error(error: GenError) -> RuntimeError {
    match error {
        GenError::GrammarInvalid(message) => RuntimeError::GrammarInvalid(message),
        GenError::GrammarResourceLimit(detail) => RuntimeError::GrammarInvalid(detail.to_owned()),
        GenError::ContextOverflow { requested, limit } => {
            RuntimeError::ContextOverflow { requested, limit }
        }
        GenError::RuntimeFailure { code, message } => match code {
            GenerationFailureCode::BackendUnavailable => RuntimeError::NativeUnavailable,
            GenerationFailureCode::AllocationFailed => RuntimeError::Oom(message),
            GenerationFailureCode::QueueFull => RuntimeError::QueueFull,
            GenerationFailureCode::Draining => RuntimeError::Draining,
            GenerationFailureCode::Oom => RuntimeError::Oom(message),
            GenerationFailureCode::SessionUnknown => RuntimeError::SessionUnknown(message),
            GenerationFailureCode::EvalTimeout => RuntimeError::EvalTimeout,
            GenerationFailureCode::ModelCorrupt => RuntimeError::ModelCorrupt {
                path: PathBuf::new(),
                reason: message,
            },
            GenerationFailureCode::ModelNotLoaded => RuntimeError::ModelNotLoaded(message),
            GenerationFailureCode::QuotaExhausted => RuntimeError::QuotaExhausted(message),
            GenerationFailureCode::Cancelled => RuntimeError::Cancelled,
            GenerationFailureCode::Internal => RuntimeError::Internal(message),
        },
        GenError::FimUnsupported
        | GenError::InvalidFimSentinels(_)
        | GenError::UnsupportedParam(_)
        | GenError::InvalidSamplerParam(_, _)
        | GenError::InvalidStop(_) => RuntimeError::UnsupportedParam(error.to_string()),
        GenError::Backpressure | GenError::StreamDisconnected | GenError::EventTooLarge => {
            RuntimeError::Cancelled
        }
        GenError::NativeSampler(_)
        | GenError::InvalidEmbedding
        | GenError::InvalidLogits(_)
        | GenError::Backend(_)
        | GenError::SpeculationInvalid(_)
        | GenError::SpeculationContextInvalidated(_) => RuntimeError::Internal(error.to_string()),
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn runtime_gen_error(error: &RuntimeError) -> GenError {
    match error {
        RuntimeError::GrammarInvalid(_) => {
            GenError::GrammarInvalid("grammar is invalid".to_owned())
        }
        RuntimeError::ContextOverflow { requested, limit } => GenError::ContextOverflow {
            requested: *requested,
            limit: *limit,
        },
        RuntimeError::ModelNotLoaded(_) => GenError::RuntimeFailure {
            code: GenerationFailureCode::ModelNotLoaded,
            message: "requested model is not loaded".to_owned(),
        },
        RuntimeError::ModelCorrupt { .. } => GenError::RuntimeFailure {
            code: GenerationFailureCode::ModelCorrupt,
            message: "model is corrupt or unreadable".to_owned(),
        },
        RuntimeError::NativeUnavailable => GenError::RuntimeFailure {
            code: GenerationFailureCode::BackendUnavailable,
            message: "native inference backend is unavailable".to_owned(),
        },
        RuntimeError::QueueFull => GenError::RuntimeFailure {
            code: GenerationFailureCode::QueueFull,
            message: "engine request queue is full".to_owned(),
        },
        RuntimeError::Draining => GenError::RuntimeFailure {
            code: GenerationFailureCode::Draining,
            message: "engine is draining".to_owned(),
        },
        RuntimeError::Oom(_) => GenError::RuntimeFailure {
            code: GenerationFailureCode::AllocationFailed,
            message: "native allocation failed".to_owned(),
        },
        RuntimeError::BackgroundReadmissionLimit { .. } => GenError::RuntimeFailure {
            code: GenerationFailureCode::Oom,
            message: "background request memory readmission was exhausted".to_owned(),
        },
        RuntimeError::QuotaExhausted(_) => GenError::RuntimeFailure {
            code: GenerationFailureCode::QuotaExhausted,
            message: "engine resource quota exhausted".to_owned(),
        },
        RuntimeError::SessionUnknown(_) => GenError::RuntimeFailure {
            code: GenerationFailureCode::SessionUnknown,
            message: "requested session is unknown".to_owned(),
        },
        RuntimeError::EvalTimeout => GenError::RuntimeFailure {
            code: GenerationFailureCode::EvalTimeout,
            message: "engine evaluation timed out".to_owned(),
        },
        RuntimeError::UnsupportedParam(_) | RuntimeError::AdapterInvalid => {
            GenError::UnsupportedParam("runtime request validation failed")
        }
        RuntimeError::Cancelled => GenError::RuntimeFailure {
            code: GenerationFailureCode::Cancelled,
            message: "request was cancelled".to_owned(),
        },
        RuntimeError::Internal(_)
        | RuntimeError::TemplateUntrusted
        | RuntimeError::Unauthorized
        | RuntimeError::EvalReceiptUnavailable
        | RuntimeError::EvalAttemptConflict
        | RuntimeError::EvalReceiptCommit(_) => GenError::RuntimeFailure {
            code: GenerationFailureCode::Internal,
            message: "engine request failed internally".to_owned(),
        },
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn model_family(architecture: &str) -> Option<ModelFamily> {
    let architecture = architecture.to_ascii_lowercase();
    if architecture.contains("code") && architecture.contains("llama") {
        Some(ModelFamily::CodeLlama)
    } else if architecture.contains("deepseek") {
        Some(ModelFamily::DeepSeekCoder)
    } else if architecture.contains("starcoder") || architecture.contains("starcoder2") {
        Some(ModelFamily::StarCoder)
    } else if architecture.contains("qwen") {
        Some(ModelFamily::QwenCoder)
    } else {
        None
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn control_stop_reason(state: GenerationControlState) -> StopReason {
    match state {
        GenerationControlState::Cancelled => StopReason::Cancelled,
        GenerationControlState::Disconnected => StopReason::Disconnected,
        GenerationControlState::DeadlineExceeded => StopReason::DeadlineExceeded,
        GenerationControlState::Running => StopReason::Cancelled,
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn finish_retained_pending_executor<B>(
    executor: &GenerationExecutor<B>,
) -> Result<Option<StepOutcome>, GenError> {
    executor.finish_from_control_try()
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn termination_reason(reason: &StopReason) -> TerminationReason {
    match reason {
        StopReason::StopString(_) => TerminationReason::StopSequence,
        StopReason::EndToken(_) => TerminationReason::EndOfGeneration,
        StopReason::MaxTokens => TerminationReason::Completed,
        StopReason::Cancelled | StopReason::Disconnected => TerminationReason::Cancelled,
        StopReason::DeadlineExceeded => TerminationReason::Deadline,
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn terminal_from_scheduler(reason: TerminationReason, end_token: i32) -> TerminalKind {
    match reason {
        TerminationReason::Completed => TerminalKind::Normal(StopReason::MaxTokens),
        TerminationReason::EndOfGeneration => TerminalKind::Normal(StopReason::EndToken(end_token)),
        TerminationReason::StopSequence => {
            TerminalKind::Normal(StopReason::StopString(String::new()))
        }
        TerminationReason::Deadline | TerminationReason::EvalTimeout => {
            TerminalKind::Normal(StopReason::DeadlineExceeded)
        }
        TerminationReason::Cancelled | TerminationReason::Drained => {
            TerminalKind::Normal(StopReason::Cancelled)
        }
        TerminationReason::BackendFailure => TerminalKind::Failed,
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn principal_fingerprint(principal_id: &str) -> Result<[u8; 32], RuntimeError> {
    if principal_id.is_empty()
        || principal_id.len() > 256
        || principal_id.chars().any(char::is_control)
    {
        return Err(RuntimeError::Unauthorized);
    }
    let fingerprint: [u8; 32] = Sha256::digest(principal_id.as_bytes()).into();
    if fingerprint == [0; 32] {
        return Err(RuntimeError::Unauthorized);
    }
    Ok(fingerprint)
}

fn digest_hex(digest: [u8; 32]) -> String {
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn validate_embedding_response_shape(
    item_count: usize,
    embedding_length: usize,
    native_sequence_capacity: usize,
) -> Result<(), RuntimeError> {
    if item_count > native_sequence_capacity {
        return Err(RuntimeError::UnsupportedParam(format!(
            "embedding batch contains {item_count} items, but the native sequence capacity is {native_sequence_capacity}"
        )));
    }
    let response_components =
        item_count
            .checked_mul(embedding_length)
            .ok_or(RuntimeError::ContextOverflow {
                requested: u32::MAX,
                limit: MAX_EMBEDDING_RESPONSE_COMPONENTS as u32,
            })?;
    if response_components > MAX_EMBEDDING_RESPONSE_COMPONENTS {
        return Err(RuntimeError::ContextOverflow {
            requested: u32::try_from(response_components).unwrap_or(u32::MAX),
            limit: MAX_EMBEDDING_RESPONSE_COMPONENTS as u32,
        });
    }
    Ok(())
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn checked_batch_token_total(current: usize, additional: usize) -> Result<usize, RuntimeError> {
    let total = current
        .checked_add(additional)
        .ok_or(RuntimeError::ContextOverflow {
            requested: u32::MAX,
            limit: MAX_BATCH_ACTUAL_TOKENS as u32,
        })?;
    if total > MAX_BATCH_ACTUAL_TOKENS {
        return Err(RuntimeError::ContextOverflow {
            requested: u32::try_from(total).unwrap_or(u32::MAX),
            limit: MAX_BATCH_ACTUAL_TOKENS as u32,
        });
    }
    Ok(total)
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn validate_embedding_intermediate(
    total_tokens: usize,
    embedding_length: usize,
) -> Result<(), RuntimeError> {
    let intermediate_bytes = total_tokens
        .checked_mul(embedding_length)
        .and_then(|components| components.checked_mul(std::mem::size_of::<f32>()))
        .ok_or(RuntimeError::ContextOverflow {
            requested: u32::MAX,
            limit: MAX_EMBEDDING_INTERMEDIATE_BYTES as u32,
        })?;
    if intermediate_bytes > MAX_EMBEDDING_INTERMEDIATE_BYTES {
        return Err(RuntimeError::ContextOverflow {
            requested: u32::try_from(intermediate_bytes).unwrap_or(u32::MAX),
            limit: MAX_EMBEDDING_INTERMEDIATE_BYTES as u32,
        });
    }
    Ok(())
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn native_position(base: u32, offset: usize) -> Result<i32, RuntimeError> {
    let offset = u32::try_from(offset).map_err(|_| RuntimeError::ContextOverflow {
        requested: u32::MAX,
        limit: u32::MAX,
    })?;
    let position = base
        .checked_add(offset)
        .ok_or_else(|| RuntimeError::ContextOverflow {
            requested: u32::MAX,
            limit: u32::MAX,
        })?;
    i32::try_from(position).map_err(|_| RuntimeError::ContextOverflow {
        requested: position,
        limit: i32::MAX as u32,
    })
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn native_sequence_capacity(config: &EngineConfig) -> Result<u32, RuntimeError> {
    let context_tokens = config
        .slots
        .default_ctx
        .checked_mul(config.slots.count as u32)
        .ok_or_else(|| RuntimeError::Internal("native context size overflows".to_owned()))?;
    let available = LLAMA_NATIVE_SEQUENCE_LIMIT
        .min(config.scheduler.batch_token_budget as usize)
        .min(context_tokens as usize);
    let reserved =
        config.slots.count.checked_mul(2).ok_or_else(|| {
            RuntimeError::Internal("native sequence capacity overflows".to_owned())
        })?;
    if reserved >= available {
        return Err(RuntimeError::UnsupportedParam(format!(
            "slot count requires {reserved} reserved native sequences, but the configured native batch supports only {available}"
        )));
    }
    u32::try_from(available)
        .map_err(|_| RuntimeError::Internal("native sequence capacity exceeds u32".to_owned()))
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn prefix_sequence_capacity(config: &EngineConfig) -> Result<usize, RuntimeError> {
    let capacity = native_sequence_capacity(config)? as usize;
    let reserved =
        config.slots.count.checked_mul(2).ok_or_else(|| {
            RuntimeError::Internal("native sequence capacity overflows".to_owned())
        })?;
    capacity
        .checked_sub(reserved)
        .filter(|capacity| *capacity > 0)
        .ok_or_else(|| {
            RuntimeError::UnsupportedParam(
                "configured slots leave no native sequence capacity for prefixes".to_owned(),
            )
        })
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn duration_ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1_000.0
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn scheduler(config: &EngineConfig, model_id: &str) -> Result<SchedulerCore, RuntimeError> {
    let context_cells = config
        .slots
        .default_ctx
        .checked_mul(config.slots.count as u32)
        .ok_or_else(|| {
            RuntimeError::Internal("configured KV cell capacity overflows".to_owned())
        })?;
    let ram_bytes = gib_bytes(config.budgets.ram_gb)?;
    SchedulerCore::new(SchedulerCoreConfig {
        slot_count: config.slots.count,
        native_sequence_capacity: native_sequence_capacity(config)?,
        queue_capacity: config
            .slots
            .count
            .saturating_mul(MODEL_COMMAND_QUEUE_MULTIPLIER)
            .max(1),
        batch_token_budget: config.scheduler.batch_token_budget,
        preemption_enabled: config.scheduler.preemption,
        kv_capacity_cells: context_cells,
        kv_bytes_per_cell: 16,
        admission_memory: crate::hw::budget::MemoryAmount::ram(ram_bytes),
        kv_memory: crate::hw::budget::MemoryAmount::ram(ram_bytes),
        session_dir: config
            .kv
            .scheduler_dir()
            .join(format!("{:x}", Sha256::digest(model_id.as_bytes()))),
        prefix_capacity: prefix_sequence_capacity(config)?,
    })
    .map_err(Into::into)
}

fn prepare_kv_storage(config: &EngineConfig) -> Result<(), RuntimeError> {
    let root = &config.kv.session_dir;
    match config.kv.root_policy {
        KvRootPolicy::Managed => prepare_managed_private_root(root),
        KvRootPolicy::ExternalPreprovisioned => verify_secure_directory(root),
    }
    .map_err(|source| {
        session_store_error(SessionStoreError::Io {
            operation: "prepare KV storage root",
            path: root.clone(),
            source,
        })
    })?;
    let durable = config.kv.durable_dir();
    let scheduler = config.kv.scheduler_dir();
    for (operation, path) in [
        ("prepare durable namespace", durable),
        ("prepare scheduler namespace", scheduler),
        (
            "prepare adapter CAS namespace",
            config.kv.session_dir.join("adapter-cas"),
        ),
    ] {
        let result = match config.kv.root_policy {
            KvRootPolicy::Managed => ensure_private_directory(&path),
            KvRootPolicy::ExternalPreprovisioned => verify_secure_directory(&path),
        };
        result.map_err(|source| {
            session_store_error(SessionStoreError::Io {
                operation,
                path,
                source,
            })
        })?;
    }
    Ok(())
}

fn model_info(
    id: String,
    path: PathBuf,
    metadata: &GgufMetadata,
    configured_context: u32,
) -> Result<ModelInfo, RuntimeError> {
    let metadata_context = metadata
        .context_length
        .and_then(|value| u32::try_from(value).ok())
        .unwrap_or(configured_context);
    Ok(ModelInfo {
        id,
        path,
        architecture: metadata
            .architecture
            .clone()
            .unwrap_or_else(|| "unknown".to_owned()),
        quant: metadata
            .quantization
            .clone()
            .unwrap_or_else(|| "unknown".to_owned()),
        context_length: configured_context.min(metadata_context).max(1),
        embedding_length: metadata
            .embedding_length
            .and_then(|value| u32::try_from(value).ok())
            .unwrap_or(0),
        supports_embeddings: metadata.supports_embeddings,
        supports_fim: metadata.supports_fim,
        chat_template: metadata.chat_template.clone(),
        model_fingerprint: [0; 32],
    })
}

fn adapter_registry_error(error: AdapterRegistryError) -> RuntimeError {
    let diagnostic = match &error {
        AdapterRegistryError::EmptyRoots => "empty_roots",
        AdapterRegistryError::InvalidRoot(_) => "invalid_root",
        AdapterRegistryError::InvalidStore(_) => "invalid_store",
        AdapterRegistryError::InvalidId(_) => "invalid_id",
        AdapterRegistryError::Duplicate(_) => "duplicate",
        AdapterRegistryError::Unknown(_) => "unknown",
        AdapterRegistryError::InvalidScale => "invalid_scale",
        AdapterRegistryError::InvalidSize => "invalid_size",
        AdapterRegistryError::InvalidDigest => "invalid_digest",
        AdapterRegistryError::InvalidRelativePath(_) => "invalid_relative_path",
        AdapterRegistryError::InvalidFile(_) => "invalid_file",
        AdapterRegistryError::PathEscape(_) => "path_escape",
        AdapterRegistryError::InvalidExtension(_) => "invalid_extension",
        AdapterRegistryError::Oversize { .. } => "oversize",
        AdapterRegistryError::SizeMismatch(_) => "size_mismatch",
        AdapterRegistryError::DigestMismatch(_) => "digest_mismatch",
        AdapterRegistryError::BaseModelMismatch(_) => "base_model_mismatch",
        AdapterRegistryError::IdentityMismatch(_) => "identity_mismatch",
        AdapterRegistryError::IdentityGuardUnsupported => "identity_guard_unsupported",
        AdapterRegistryError::Io { .. } => "io",
    };
    tracing::warn!(
        adapter_error_code = diagnostic,
        "adapter registry failure mapped to a redacted runtime error"
    );
    RuntimeError::AdapterInvalid
}

fn session_store_error(error: SessionStoreError) -> RuntimeError {
    match error {
        SessionStoreError::Unknown | SessionStoreError::Corrupt(_) => {
            RuntimeError::SessionUnknown("session".to_owned())
        }
        SessionStoreError::Quota { .. } => RuntimeError::QuotaExhausted(error.to_string()),
        SessionStoreError::InvalidInput(_) | SessionStoreError::Conflict => {
            RuntimeError::UnsupportedParam(error.to_string())
        }
        SessionStoreError::Disabled(_) | SessionStoreError::Io { .. } => {
            RuntimeError::Internal(error.to_string())
        }
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn session_access_error(error: SessionStoreError, session_id: &str) -> RuntimeError {
    match error {
        SessionStoreError::Unknown | SessionStoreError::Corrupt(_) => {
            RuntimeError::SessionUnknown(session_id.to_owned())
        }
        other => session_store_error(other),
    }
}

fn registry_error(error: RegistryError) -> RuntimeError {
    match error {
        RegistryError::Integrity { path, source } => RuntimeError::ModelCorrupt {
            path,
            reason: source.to_string(),
        },
        RegistryError::MissingModel(_) => {
            RuntimeError::ModelNotLoaded("requested model".to_owned())
        }
        other => RuntimeError::Internal(other.to_string()),
    }
}

#[cfg(test)]
mod registry_error_tests {
    use std::path::PathBuf;

    use super::{adapter_registry_error, registry_error, RuntimeError};
    use crate::{
        api::error::{ApiError, EngineErrorCode},
        store::{adapter::AdapterRegistryError, registry::RegistryError},
    };

    #[test]
    fn missing_registry_model_does_not_expose_its_owned_path() {
        let owned_path = PathBuf::from(r"C:\private\model-cas\tenant-secret.gguf");
        let runtime_error = registry_error(RegistryError::MissingModel(owned_path.clone()));

        assert!(matches!(
            &runtime_error,
            RuntimeError::ModelNotLoaded(reference) if reference == "requested model"
        ));
        assert!(!runtime_error
            .to_string()
            .contains(&owned_path.display().to_string()));

        let api_error = ApiError::from(runtime_error);
        assert_eq!(api_error.body.code, EngineErrorCode::ModelNotLoaded);
        assert_eq!(api_error.body.message, "requested model is not loaded");
        assert!(!api_error.body.message.contains("private"));
        assert!(!api_error.body.message.contains("tenant-secret"));
    }

    #[test]
    fn adapter_registry_diagnostics_do_not_become_public_validation_text() {
        let owned_path = PathBuf::from(r"C:\private\adapter-cas\tenant-secret.gguf");
        let runtime_error =
            adapter_registry_error(AdapterRegistryError::InvalidFile(owned_path.clone()));
        let api_error = ApiError::from(runtime_error);

        assert_eq!(api_error.body.code, EngineErrorCode::UnsupportedParam);
        assert_eq!(
            api_error.body.message,
            "adapter registration or resolution failed validation"
        );
        let serialized = serde_json::to_string(&api_error.body).unwrap();
        assert!(!serialized.contains("tenant-secret"));
        assert!(!serialized.contains(&owned_path.display().to_string()));
    }
}

fn loader_error(error: LoaderError) -> RuntimeError {
    match error {
        LoaderError::MissingModel { model_id, .. } | LoaderError::NotLoaded(model_id) => {
            RuntimeError::ModelNotLoaded(model_id)
        }
        LoaderError::CorruptModel { path, source, .. } => RuntimeError::ModelCorrupt {
            path,
            reason: source.to_string(),
        },
        LoaderError::Budget(error) => RuntimeError::Oom(error.to_string()),
        LoaderError::Allocation(reason) => RuntimeError::ModelCorrupt {
            path: PathBuf::new(),
            reason,
        },
        LoaderError::Release(reason) => RuntimeError::Internal(reason),
    }
}

fn loader_keep_alive(value: crate::config::KeepAlive) -> LoaderKeepAlive {
    match value {
        crate::config::KeepAlive::Immediate => LoaderKeepAlive::Immediate,
        crate::config::KeepAlive::Forever => LoaderKeepAlive::Never,
        crate::config::KeepAlive::Duration(duration) => {
            LoaderKeepAlive::DurationMs(u64::try_from(duration.as_millis()).unwrap_or(u64::MAX))
        }
    }
}

fn gib_bytes(value: f64) -> Result<u64, RuntimeError> {
    let bytes = value * GIB;
    if !bytes.is_finite() || bytes <= 0.0 || bytes > u64::MAX as f64 {
        return Err(RuntimeError::Internal(
            "configured RAM budget is not a positive finite value".to_owned(),
        ));
    }
    Ok(bytes as u64)
}

fn gib_bytes_allow_zero(value: f64) -> Result<u64, RuntimeError> {
    let bytes = value * GIB;
    if !bytes.is_finite() || bytes < 0.0 || bytes > u64::MAX as f64 {
        return Err(RuntimeError::Internal(
            "configured VRAM budget is not a finite non-negative value".to_owned(),
        ));
    }
    Ok(bytes as u64)
}

fn model_load_timeout(file_size_bytes: u64) -> Duration {
    let gibibytes = file_size_bytes / (1024 * 1024 * 1024);
    MODEL_LOAD_BASE_TIMEOUT.saturating_add(Duration::from_secs(
        gibibytes.saturating_mul(MODEL_LOAD_SECS_PER_GIB),
    ))
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn kv_percent(used_cells: u32, config: &EngineConfig) -> u8 {
    let capacity = config
        .slots
        .default_ctx
        .saturating_mul(config.slots.count as u32)
        .max(1);
    ((u64::from(used_cells) * 100) / u64::from(capacity)).min(100) as u8
}

fn now() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0.0, |duration| duration.as_secs_f64())
}

fn new_engine_instance_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_nanos());
    let sequence = ENGINE_INSTANCE_COUNTER.fetch_add(1, Ordering::Relaxed);
    format!(
        "engine-{:x}-{nanos:032x}-{sequence:016x}",
        std::process::id()
    )
}

fn receipt_finish_reason(reason: &crate::gen::StopReason) -> Result<&'static str, RuntimeError> {
    match reason {
        crate::gen::StopReason::StopString(_) | crate::gen::StopReason::EndToken(_) => Ok("stop"),
        crate::gen::StopReason::MaxTokens => Ok("length"),
        crate::gen::StopReason::DeadlineExceeded => Err(RuntimeError::EvalTimeout),
        crate::gen::StopReason::Cancelled | crate::gen::StopReason::Disconnected => {
            Err(RuntimeError::Cancelled)
        }
    }
}

#[cfg(any(test, feature = "cpu", feature = "cuda"))]
fn generation_control_sha256(
    request: &GenerateRequest,
    speculation: &SpeculationReceiptIdentity,
) -> Result<Digest32, RuntimeError> {
    let mut hasher = Sha256::new();
    hasher.update(b"AMW\0generation-control-v2\0");
    hasher.update(request.max_tokens.to_be_bytes());
    match speculation {
        SpeculationReceiptIdentity::PromptLookup => hasher.update([0]),
        SpeculationReceiptIdentity::DraftModel {
            model_id,
            model_sha256,
            minimum_context,
            vocabulary_fingerprint,
        } => {
            hasher.update([1]);
            hash_len_prefixed(&mut hasher, model_id.as_bytes())?;
            hasher.update(model_sha256.as_bytes());
            match minimum_context {
                Some(value) => {
                    hasher.update([1]);
                    hasher.update(value.to_be_bytes());
                }
                None => hasher.update([0]),
            }
            hasher.update(vocabulary_fingerprint.as_bytes());
        }
    }
    hasher.update(
        u32::try_from(request.stop.len())
            .map_err(|_| RuntimeError::EvalReceiptCommit("too many stop strings".to_owned()))?
            .to_be_bytes(),
    );
    for stop in &request.stop {
        hash_len_prefixed(&mut hasher, stop.as_bytes())?;
    }
    match request.session_id.as_deref() {
        Some(session_id) => {
            hasher.update([1]);
            hash_len_prefixed(&mut hasher, session_id.as_bytes())?;
        }
        None => hasher.update([0]),
    }
    hasher.update(
        u32::try_from(request.prefix_refs.len())
            .map_err(|_| RuntimeError::EvalReceiptCommit("too many prefix refs".to_owned()))?
            .to_be_bytes(),
    );
    for (name, content_hash) in &request.prefix_refs {
        hash_len_prefixed(&mut hasher, name.as_bytes())?;
        hash_len_prefixed(&mut hasher, content_hash.as_bytes())?;
    }
    match request.infill_suffix.as_deref() {
        Some(suffix) => {
            hasher.update([1]);
            hash_len_prefixed(&mut hasher, suffix.as_bytes())?;
        }
        None => hasher.update([0]),
    }
    Ok(Digest32::from_bytes(hasher.finalize().into()))
}

#[cfg(any(test, feature = "cpu", feature = "cuda"))]
fn hash_len_prefixed(hasher: &mut Sha256, value: &[u8]) -> Result<(), RuntimeError> {
    let length = u32::try_from(value.len()).map_err(|_| {
        RuntimeError::EvalReceiptCommit("generation-control field exceeds u32".to_owned())
    })?;
    hasher.update(length.to_be_bytes());
    hasher.update(value);
    Ok(())
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn priority_name(priority: PriorityClass) -> &'static str {
    match priority {
        PriorityClass::InteractiveBlocking => "interactive_blocking",
        PriorityClass::Interactive => "interactive",
        PriorityClass::Worker => "worker",
        PriorityClass::Background => "background",
        PriorityClass::Eval => "eval",
    }
}

#[cfg(all(test, any(feature = "cpu", feature = "cuda")))]
mod tests {
    use std::{
        collections::VecDeque,
        fs,
        time::{Duration, Instant},
    };

    use super::{
        apply_lora_transaction, checked_batch_token_total, finish_retained_pending_executor,
        native_sequence_capacity, prefix_sequence_capacity, project_background_readmission,
        validate_embedding_intermediate, validate_embedding_response_shape, EngineConfig,
        EngineRuntime, GenerateRequest, LoraApply, ReplayOrigin, RuntimeError, WorkloadRole,
        DRAFT_ACTOR_TARGET_YIELD_MS, LLAMA_NATIVE_SEQUENCE_LIMIT, MAX_BATCH_ACTUAL_TOKENS,
    };
    use crate::gen::{
        bounded_generation_stream, DecodeBackend, DistributionCandidate, GenError,
        GenerationControl, GenerationEvent, GenerationExecutor, GenerationPlan, GenerationStep,
        GenerationUsage, SamplerCapabilities, SamplerParams, SamplingResult, StepOutcome,
        StopEvaluator, StopReason,
    };
    use crate::{
        ffi::Model,
        sched::PriorityClass,
        telemetry::{events::EngineEvent, metrics::MetricsHub, TelemetryHub},
    };

    struct ScriptedBackend {
        samples: VecDeque<(i32, Vec<u8>)>,
        current: Option<(i32, Vec<u8>)>,
    }

    struct FakeLoraContext {
        state: u8,
        fail_replacement_once: bool,
        fail_rollback: bool,
    }

    impl LoraApply<u8> for FakeLoraContext {
        fn apply_lora(&mut self, adapter: &u8) -> Result<(), String> {
            if *adapter == 1 && self.fail_replacement_once {
                self.fail_replacement_once = false;
                return Err("injected replacement failure".to_owned());
            }
            if *adapter == 0 && self.fail_rollback {
                return Err("injected rollback failure".to_owned());
            }
            self.state = *adapter;
            Ok(())
        }
    }

    impl ScriptedBackend {
        fn new(samples: impl IntoIterator<Item = (i32, &'static [u8])>) -> Self {
            Self {
                samples: samples
                    .into_iter()
                    .map(|(token, bytes)| (token, bytes.to_vec()))
                    .collect(),
                current: None,
            }
        }
    }

    impl DecodeBackend for ScriptedBackend {
        fn transform_sample_accept(
            &mut self,
            _output_index: i32,
        ) -> Result<SamplingResult, GenError> {
            let (token_id, bytes) = self
                .samples
                .pop_front()
                .ok_or_else(|| GenError::Backend("scripted samples exhausted".to_owned()))?;
            self.current = Some((token_id, bytes));
            Ok(SamplingResult {
                token_id,
                probability: 1.0,
                candidates: vec![DistributionCandidate {
                    token_id,
                    logit: 0.0,
                    probability: 1.0,
                }],
            })
        }

        fn accept(&mut self, token: i32) -> Result<(), GenError> {
            if self
                .current
                .as_ref()
                .is_some_and(|sample| sample.0 == token)
            {
                Ok(())
            } else {
                Err(GenError::InvalidLogits("unexpected scripted token"))
            }
        }

        fn token_piece(&mut self, token: i32) -> Result<Vec<u8>, GenError> {
            self.current
                .as_ref()
                .filter(|sample| sample.0 == token)
                .map(|sample| sample.1.clone())
                .ok_or(GenError::InvalidLogits("unexpected scripted token"))
        }
    }

    fn scripted_plan() -> GenerationPlan {
        GenerationPlan::build(
            &SamplerParams::default(),
            SamplerCapabilities::pinned_revision(),
            1,
        )
        .expect("scripted generation plan must be valid")
    }

    #[test]
    fn native_sequence_budget_stays_within_llama_limit() {
        let mut config = EngineConfig::default();
        config.slots.count = 2;

        assert_eq!(
            native_sequence_capacity(&config).expect("two slots must fit"),
            LLAMA_NATIVE_SEQUENCE_LIMIT as u32
        );
        assert_eq!(
            prefix_sequence_capacity(&config).expect("prefix budget must remain"),
            LLAMA_NATIVE_SEQUENCE_LIMIT - (2 * config.slots.count)
        );

        config.scheduler.batch_token_budget = 64;
        assert_eq!(
            native_sequence_capacity(&config).expect("batch-bounded pool must fit"),
            64
        );
        assert_eq!(
            prefix_sequence_capacity(&config).expect("batch-bounded prefix budget must fit"),
            64 - (2 * config.slots.count)
        );

        config.slots.count = LLAMA_NATIVE_SEQUENCE_LIMIT / 2;
        assert!(native_sequence_capacity(&config).is_err());
        assert!(prefix_sequence_capacity(&config).is_err());
    }

    #[test]
    fn batch_token_and_embedding_allocation_caps_fail_before_native_work() {
        assert_eq!(
            checked_batch_token_total(MAX_BATCH_ACTUAL_TOKENS - 1, 1).unwrap(),
            MAX_BATCH_ACTUAL_TOKENS
        );
        assert!(matches!(
            checked_batch_token_total(MAX_BATCH_ACTUAL_TOKENS, 1),
            Err(RuntimeError::ContextOverflow { .. })
        ));
        assert!(validate_embedding_response_shape(64, 4_096, 64).is_ok());
        assert!(matches!(
            validate_embedding_response_shape(65, 1, 64),
            Err(RuntimeError::UnsupportedParam(_))
        ));
        assert!(matches!(
            validate_embedding_response_shape(64, 4_097, 64),
            Err(RuntimeError::ContextOverflow { .. })
        ));
        assert!(validate_embedding_intermediate(8_192, 4_096).is_ok());
        assert!(matches!(
            validate_embedding_intermediate(8_193, 4_096),
            Err(RuntimeError::ContextOverflow { .. })
        ));
    }

    #[test]
    fn lora_embedding_failure_rolls_both_contexts_to_previous_state() {
        let mut serving = FakeLoraContext {
            state: 0,
            fail_replacement_once: false,
            fail_rollback: false,
        };
        let mut embedding = FakeLoraContext {
            state: 0,
            fail_replacement_once: true,
            fail_rollback: false,
        };

        let error = apply_lora_transaction(&mut serving, Some(&mut embedding), &0, &1)
            .expect_err("second-context failure must fail the transaction");

        assert!(!error.rollback_failed());
        assert_eq!(serving.state, 0);
        assert_eq!(embedding.state, 0);
    }

    #[test]
    fn lora_rollback_failure_is_reported_as_worker_fatal() {
        let mut serving = FakeLoraContext {
            state: 0,
            fail_replacement_once: false,
            fail_rollback: true,
        };
        let mut embedding = FakeLoraContext {
            state: 0,
            fail_replacement_once: true,
            fail_rollback: false,
        };

        let error = apply_lora_transaction(&mut serving, Some(&mut embedding), &0, &1)
            .expect_err("failed rollback must fail closed");

        assert!(error.rollback_failed());
        assert!(error.detail().contains("serving context"));
        assert_eq!(
            serving.state, 1,
            "failed rollback leaves unknown native state"
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn fatal_worker_is_reaped_and_same_model_reload_succeeds() {
        let fixture = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
            .map(std::path::PathBuf::from)
            .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed GGUF fixture");
        let temp = tempfile::tempdir().unwrap();
        let model_dir = temp.path().join("models");
        fs::create_dir_all(&model_dir).unwrap();
        let model_path = model_dir.join("fatal.gguf");
        fs::copy(&fixture, &model_path).unwrap();
        let canonical_path = model_path.canonicalize().unwrap();
        fs::write(
            format!("{}.meta.json", model_path.display()),
            serde_json::to_vec(&serde_json::json!({
                "id": "fatal",
                "path": canonical_path,
                "aliases": [],
                "draft_pair": null,
            }))
            .unwrap(),
        )
        .unwrap();
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.budgets.ram_gb = 0.25;
        config.slots.default_ctx = 64;
        config.scheduler.batch_token_budget = 64;
        config.kv.session_dir = temp.path().join("sessions");
        config.log.dir = temp.path().join("logs");
        let runtime = EngineRuntime::new(
            config,
            crate::telemetry::TelemetryHub::default(),
            crate::telemetry::metrics::MetricsHub::default(),
        )
        .unwrap();
        runtime.load_model("fatal").await.unwrap();
        runtime.terminate_worker_for_test("fatal").await.unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        while runtime
            .status()
            .models
            .iter()
            .any(|model| model.id == "fatal")
        {
            assert!(tokio::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(10)).await;
        }
        assert!(matches!(
            runtime
                .tokenize(Some("fatal"), vec!["before reload".to_owned()], true)
                .await,
            Err(RuntimeError::Internal(message)) if message.contains("unhealthy")
        ));

        runtime.load_model("fatal").await.unwrap();
        let tokens = runtime
            .tokenize(Some("fatal"), vec!["after reload".to_owned()], true)
            .await
            .unwrap();
        assert_eq!(tokens.len(), 1);
        assert!(!tokens[0].is_empty());
        let counts = runtime
            .count_tokens(Some("fatal"), vec!["after reload".to_owned()], true)
            .await
            .unwrap();
        assert_eq!(counts, [u32::try_from(tokens[0].len()).unwrap()]);
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn governed_prompt_lookup_commits_live_speculation_with_exact_telemetry() {
        let fixture = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
            .map(std::path::PathBuf::from)
            .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed GGUF fixture");
        let native_model = Model::load(&fixture).expect("governed model must load");
        let temp = tempfile::tempdir().unwrap();
        let model_dir = temp.path().join("models");
        fs::create_dir_all(&model_dir).unwrap();
        let model_path = model_dir.join("prompt-lookup.gguf");
        fs::copy(&fixture, &model_path).unwrap();
        let canonical_path = model_path.canonicalize().unwrap();
        fs::write(
            format!("{}.meta.json", model_path.display()),
            serde_json::to_vec(&serde_json::json!({
                "id": "prompt-lookup",
                "path": canonical_path,
                "aliases": [],
                "draft_pair": null,
            }))
            .unwrap(),
        )
        .unwrap();
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.budgets.ram_gb = 0.25;
        config.slots.count = 1;
        config.slots.default_ctx = 64;
        config.scheduler.batch_token_budget = 64;
        config.kv.session_dir = temp.path().join("sessions");
        config.log.dir = temp.path().join("logs");
        let telemetry = TelemetryHub::default();
        let metrics = MetricsHub::default();
        let runtime = EngineRuntime::new(config, telemetry.clone(), metrics.clone()).unwrap();
        runtime.load_model("prompt-lookup").await.unwrap();
        let prompt = "governed prompt lookup runtime proof";
        let prompt_tokens = runtime
            .tokenize(Some("prompt-lookup"), vec![prompt.to_owned()], false)
            .await
            .unwrap()
            .remove(0);
        let end_token = native_model.end_token();
        let forced_token = prompt_tokens
            .into_iter()
            .find(|token| *token >= 0 && *token != end_token)
            .expect("governed prompt must contain a non-terminal token");
        let mut sampling = SamplerParams::default();
        sampling.temperature = 0.0;
        sampling.logit_bias.insert(forced_token, 100.0);
        let mut stream = runtime
            .generate(GenerateRequest {
                request_id: "runtime-speculation".to_owned(),
                trace_id: "runtime-speculation-trace".to_owned(),
                principal_id: "runtime-test".to_owned(),
                model: Some("prompt-lookup".to_owned()),
                prompt: prompt.to_owned(),
                infill_suffix: None,
                max_tokens: 16,
                stop: Vec::new(),
                sampling,
                grammar: None,
                priority: PriorityClass::Worker,
                role: WorkloadRole::Worker,
                eval_slot: None,
                eval_context: None,
                endpoint: "/v1/completions".to_owned(),
                original_messages: Vec::new(),
                session_id: None,
                prefix_refs: Vec::new(),
                deadline: Instant::now() + Duration::from_secs(30),
                #[cfg(all(feature = "contract-test-controls", debug_assertions))]
                contract_failure: None,
            })
            .await
            .unwrap();
        let mut emitted_tokens = Vec::new();
        let usage = loop {
            let event = tokio::time::timeout(Duration::from_secs(30), stream.recv())
                .await
                .expect("governed generation timed out")
                .expect("governed generation stream disconnected");
            match event {
                GenerationEvent::Delta { token_id, .. } => emitted_tokens.push(token_id),
                GenerationEvent::Finished { reason, usage, .. } => {
                    assert_eq!(reason, StopReason::MaxTokens);
                    break usage;
                }
                GenerationEvent::Failed(error) => {
                    assert!(false, "governed generation failed: {error}")
                }
            }
        };
        assert_eq!(usage.completion_tokens, 16);
        assert_eq!(emitted_tokens.len(), usage.completion_tokens);
        assert!(emitted_tokens.iter().all(|token| *token == forced_token));

        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let aggregate = loop {
            if let Some(aggregate) = metrics.snapshot().per_role.get("worker").cloned() {
                break aggregate;
            }
            assert!(tokio::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(10)).await;
        };
        assert_eq!(aggregate.requests, 1);
        assert_eq!(aggregate.completion_tokens, usage.completion_tokens as u64);
        assert!(aggregate.speculation_proposed_tokens > 0);
        assert!(aggregate.speculation_accepted_tokens <= aggregate.speculation_proposed_tokens);
        let complete = telemetry
            .try_recent_events()
            .into_iter()
            .find_map(|event| match event.event {
                EngineEvent::RequestComplete {
                    request_id,
                    output_tokens,
                    speculation_proposed_tokens,
                    speculation_accepted_tokens,
                    spec_accept_rate,
                    ..
                } if request_id == "runtime-speculation" => Some((
                    output_tokens,
                    speculation_proposed_tokens,
                    speculation_accepted_tokens,
                    spec_accept_rate,
                )),
                _ => None,
            })
            .expect("request completion telemetry must be retained");
        assert_eq!(complete.0, usage.completion_tokens as u32);
        assert_eq!(u64::from(complete.1), aggregate.speculation_proposed_tokens);
        assert_eq!(u64::from(complete.2), aggregate.speculation_accepted_tokens);
        assert_eq!(
            complete.3,
            Some(f64::from(complete.2) / f64::from(complete.1))
        );
    }

    #[tokio::test(flavor = "multi_thread", worker_threads = 2)]
    async fn governed_draft_pair_commits_live_actor_proposals_with_exact_telemetry() {
        struct TargetYieldReset;

        impl Drop for TargetYieldReset {
            fn drop(&mut self) {
                DRAFT_ACTOR_TARGET_YIELD_MS.store(0, std::sync::atomic::Ordering::Release);
            }
        }

        let fixture = std::env::var_os("AMW_ENGINE_NATIVE_TEST_MODEL")
            .map(std::path::PathBuf::from)
            .expect("AMW_ENGINE_NATIVE_TEST_MODEL must name the governed GGUF fixture");
        let native_model = Model::load(&fixture).expect("governed model must load");
        let vocabulary_fingerprint = native_model
            .vocabulary_fingerprint()
            .expect("governed model vocabulary must be readable");
        let temp = tempfile::tempdir().unwrap();
        let model_dir = temp.path().join("models");
        fs::create_dir_all(&model_dir).unwrap();
        let target_path = model_dir.join("target.gguf");
        let draft_path = model_dir.join("draft.gguf");
        fs::copy(&fixture, &target_path).unwrap();
        fs::copy(&fixture, &draft_path).unwrap();
        let canonical_target = target_path.canonicalize().unwrap();
        let canonical_draft = draft_path.canonicalize().unwrap();
        fs::write(
            format!("{}.meta.json", target_path.display()),
            serde_json::to_vec(&serde_json::json!({
                "id": "target",
                "path": canonical_target,
                "aliases": [],
                "draft_pair": {
                    "draft_model_id": "draft",
                    "minimum_context": 64,
                    "vocabulary_fingerprint": vocabulary_fingerprint,
                },
            }))
            .unwrap(),
        )
        .unwrap();
        fs::write(
            format!("{}.meta.json", draft_path.display()),
            serde_json::to_vec(&serde_json::json!({
                "id": "draft",
                "path": canonical_draft,
                "aliases": [],
                "draft_pair": null,
            }))
            .unwrap(),
        )
        .unwrap();
        let mut config = EngineConfig::default();
        config.models.dirs = vec![model_dir];
        config.budgets.ram_gb = 0.5;
        config.slots.count = 1;
        config.slots.default_ctx = 64;
        config.scheduler.batch_token_budget = 64;
        config.kv.session_dir = temp.path().join("sessions");
        config.log.dir = temp.path().join("logs");
        let telemetry = TelemetryHub::default();
        let metrics = MetricsHub::default();
        let runtime = EngineRuntime::new(config, telemetry.clone(), metrics.clone()).unwrap();
        runtime.load_model("target").await.unwrap();
        let prompt = "governed draft actor runtime proof";
        let prompt_tokens = runtime
            .tokenize(Some("target"), vec![prompt.to_owned()], false)
            .await
            .unwrap()
            .remove(0);
        let end_token = native_model.end_token();
        let forced_token = prompt_tokens
            .into_iter()
            .find(|token| *token >= 0 && *token != end_token)
            .expect("governed prompt must contain a non-terminal token");
        let mut sampling = SamplerParams::default();
        sampling.temperature = 0.0;
        sampling.logit_bias.insert(forced_token, 100.0);

        DRAFT_ACTOR_TARGET_YIELD_MS.store(20, std::sync::atomic::Ordering::Release);
        let _yield_reset = TargetYieldReset;
        let mut stream = runtime
            .generate(GenerateRequest {
                request_id: "runtime-draft-pair".to_owned(),
                trace_id: "runtime-draft-pair-trace".to_owned(),
                principal_id: "runtime-test".to_owned(),
                model: Some("target".to_owned()),
                prompt: prompt.to_owned(),
                infill_suffix: None,
                max_tokens: 16,
                stop: Vec::new(),
                sampling,
                grammar: None,
                priority: PriorityClass::Worker,
                role: WorkloadRole::Worker,
                eval_slot: None,
                eval_context: None,
                endpoint: "/v1/completions".to_owned(),
                original_messages: Vec::new(),
                session_id: None,
                prefix_refs: Vec::new(),
                deadline: Instant::now() + Duration::from_secs(30),
                #[cfg(all(feature = "contract-test-controls", debug_assertions))]
                contract_failure: None,
            })
            .await
            .unwrap();
        let mut emitted_tokens = Vec::new();
        let usage = loop {
            let event = tokio::time::timeout(Duration::from_secs(30), stream.recv())
                .await
                .expect("governed draft-pair generation timed out")
                .expect("governed draft-pair stream disconnected");
            match event {
                GenerationEvent::Delta { token_id, .. } => emitted_tokens.push(token_id),
                GenerationEvent::Finished { reason, usage, .. } => {
                    assert_eq!(reason, StopReason::MaxTokens);
                    break usage;
                }
                GenerationEvent::Failed(error) => {
                    assert!(false, "governed draft-pair generation failed: {error}")
                }
            }
        };
        assert_eq!(usage.completion_tokens, 16);
        assert_eq!(emitted_tokens.len(), usage.completion_tokens);
        assert!(emitted_tokens.iter().all(|token| *token == forced_token));

        let deadline = tokio::time::Instant::now() + Duration::from_secs(2);
        let aggregate = loop {
            if let Some(aggregate) = metrics.snapshot().per_role.get("worker").cloned() {
                break aggregate;
            }
            assert!(tokio::time::Instant::now() < deadline);
            tokio::time::sleep(Duration::from_millis(10)).await;
        };
        assert_eq!(aggregate.requests, 1);
        assert_eq!(aggregate.completion_tokens, usage.completion_tokens as u64);
        assert!(
            aggregate.speculation_proposed_tokens > 0,
            "the configured draft actor must win at least one live proposal race"
        );
        assert!(aggregate.speculation_accepted_tokens <= aggregate.speculation_proposed_tokens);
        let complete = telemetry
            .try_recent_events()
            .into_iter()
            .find_map(|event| match event.event {
                EngineEvent::RequestComplete {
                    request_id,
                    output_tokens,
                    speculation_proposed_tokens,
                    speculation_accepted_tokens,
                    spec_accept_rate,
                    ..
                } if request_id == "runtime-draft-pair" => Some((
                    output_tokens,
                    speculation_proposed_tokens,
                    speculation_accepted_tokens,
                    spec_accept_rate,
                )),
                _ => None,
            })
            .expect("draft-pair completion telemetry must be retained");
        assert_eq!(complete.0, usage.completion_tokens as u32);
        assert_eq!(u64::from(complete.1), aggregate.speculation_proposed_tokens);
        assert_eq!(u64::from(complete.2), aggregate.speculation_accepted_tokens);
        assert_eq!(
            complete.3,
            Some(f64::from(complete.2) / f64::from(complete.1))
        );
    }

    #[test]
    fn readmission_projection_replays_the_full_prompt_during_mid_prefill() {
        let projection = project_background_readmission(
            &ReplayOrigin::FreshPrompt,
            &[1, 2, 3],
            &[],
            None,
            4,
            16,
        )
        .expect("fresh mid-prefill work must be replayable");

        assert_eq!(projection.replay_tokens, vec![1, 2, 3]);
        assert_eq!(projection.remaining_outputs, 4);
        assert_eq!(projection.scheduler_decode_steps, 3);
        assert_eq!(projection.next_token, None);
    }

    #[test]
    fn readmission_projection_excludes_the_pending_mid_decode_token() {
        let projection = project_background_readmission(
            &ReplayOrigin::FreshPrompt,
            &[1, 2, 3],
            &[11, 12],
            Some(12),
            5,
            16,
        )
        .expect("fresh mid-decode work must be replayable");

        assert_eq!(projection.replay_tokens, vec![1, 2, 3, 11]);
        assert_eq!(projection.remaining_outputs, 3);
        assert_eq!(projection.scheduler_decode_steps, 3);
        assert_eq!(projection.next_token, Some(12));
    }

    #[test]
    fn readmission_projection_rejects_restored_session_state() {
        let error = project_background_readmission(
            &ReplayOrigin::RestoredSession {
                session_id: "session-1".to_owned(),
            },
            &[1, 2],
            &[],
            None,
            2,
            16,
        )
        .expect_err("restored KV must never enter the replay path");

        assert!(matches!(
            error,
            RuntimeError::Internal(message) if message.contains("restored-session")
        ));
    }

    #[test]
    fn retained_executor_cancellation_emits_one_authoritative_terminal() {
        let control = GenerationControl::default();
        let (sender, mut receiver) = bounded_generation_stream(control.clone());
        let mut executor = GenerationExecutor::new(
            ScriptedBackend::new([(1, b"hello E".as_slice())]),
            scripted_plan(),
            StopEvaluator::new(vec!["END".to_owned()], vec![], 4)
                .expect("stop matcher must compile"),
            sender,
            control.clone(),
            3,
        );
        assert_eq!(
            executor
                .after_decode_try(GenerationStep { output_index: 0 })
                .expect("first sample must succeed"),
            StepOutcome::Continue { token_id: 1 }
        );

        let mut pending_executor = Some(executor);
        control.cancel();
        let executor = pending_executor
            .take()
            .expect("retained executor must survive pending readmission");
        assert_eq!(
            finish_retained_pending_executor(&executor)
                .expect("control finish must fit the bounded stream"),
            Some(StepOutcome::Finished(StopReason::Cancelled))
        );
        assert_eq!(
            executor.usage(),
            GenerationUsage {
                prompt_tokens: 3,
                completion_tokens: 1,
            }
        );
        assert!(matches!(
            receiver.try_recv().expect("delta read must succeed"),
            Some(GenerationEvent::Delta { bytes, .. }) if bytes == b"hello "
        ));
        assert!(matches!(
            receiver.try_recv().expect("terminal read must succeed"),
            Some(GenerationEvent::Finished {
                reason: StopReason::Cancelled,
                usage: GenerationUsage {
                    prompt_tokens: 3,
                    completion_tokens: 1,
                },
                confidence: Some(confidence),
            }) if (confidence - 1.0).abs() < f32::EPSILON
        ));
        assert_eq!(
            receiver.try_recv().expect("stream drain must succeed"),
            None,
            "retained cancellation must emit exactly one terminal"
        );
    }

    #[test]
    fn executor_move_preserves_withheld_stop_prefix_across_readmission() {
        let control = GenerationControl::default();
        let (sender, mut receiver) = bounded_generation_stream(control.clone());
        let mut executor = GenerationExecutor::new(
            ScriptedBackend::new([(1, b"hello E".as_slice()), (2, b"NDignored".as_slice())]),
            scripted_plan(),
            StopEvaluator::new(vec!["END".to_owned()], vec![], 4)
                .expect("stop matcher must compile"),
            sender,
            control,
            3,
        );
        assert_eq!(
            executor
                .after_decode_try(GenerationStep { output_index: 0 })
                .expect("first sample must succeed"),
            StepOutcome::Continue { token_id: 1 }
        );

        let mut pending_executor = Some(executor);
        let mut resumed_executor = pending_executor
            .take()
            .expect("executor state must move intact through readmission");
        assert_eq!(
            resumed_executor
                .after_decode_try(GenerationStep { output_index: 0 })
                .expect("resumed sample must succeed"),
            StepOutcome::Finished(StopReason::StopString("END".to_owned()))
        );
        assert_eq!(
            resumed_executor.usage(),
            GenerationUsage {
                prompt_tokens: 3,
                completion_tokens: 2,
            }
        );
        assert!(matches!(
            receiver.try_recv().expect("delta read must succeed"),
            Some(GenerationEvent::Delta { bytes, .. }) if bytes == b"hello "
        ));
        assert!(matches!(
            receiver.try_recv().expect("terminal read must succeed"),
            Some(GenerationEvent::Finished {
                reason: StopReason::StopString(stop),
                usage: GenerationUsage {
                    completion_tokens: 2,
                    ..
                },
                ..
            }) if stop == "END"
        ));
        assert_eq!(
            receiver.try_recv().expect("stream drain must succeed"),
            None,
            "withheld stop-prefix bytes must never leak or duplicate"
        );
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
fn runtime_code(error: &RuntimeError) -> &'static str {
    match error {
        RuntimeError::ModelNotLoaded(_) => "model_not_loaded",
        RuntimeError::ModelCorrupt { .. } => "model_corrupt",
        RuntimeError::NativeUnavailable => "backend_unavailable",
        RuntimeError::ContextOverflow { .. } => "context_overflow",
        RuntimeError::QueueFull => "queue_full",
        RuntimeError::QuotaExhausted(_) => "quota_exhausted",
        RuntimeError::Draining => "draining",
        RuntimeError::Oom(_) => "allocation_failed",
        RuntimeError::BackgroundReadmissionLimit { .. } => "oom",
        RuntimeError::GrammarInvalid(_) => "grammar_invalid",
        RuntimeError::TemplateUntrusted => "template_untrusted",
        RuntimeError::SessionUnknown(_) => "session_unknown",
        RuntimeError::UnsupportedParam(_) => "unsupported_param",
        RuntimeError::AdapterInvalid => "adapter_invalid",
        RuntimeError::EvalTimeout => "eval_timeout",
        RuntimeError::Unauthorized => "unauthorized",
        RuntimeError::Cancelled => "cancelled",
        RuntimeError::EvalReceiptUnavailable => "eval_receipt_unavailable",
        RuntimeError::EvalAttemptConflict => "eval_attempt_conflict",
        RuntimeError::EvalReceiptCommit(_) => "eval_receipt_error",
        RuntimeError::Internal(_) => "internal",
    }
}

fn process_resident_bytes() -> u64 {
    let system = sysinfo::System::new_all();
    system
        .process(sysinfo::Pid::from_u32(std::process::id()))
        .map_or(0, sysinfo::Process::memory)
}
