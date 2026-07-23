use std::{
    collections::{BTreeMap, BTreeSet, HashMap},
    fs::{self, File, OpenOptions},
    io::{self, Read, Seek, SeekFrom},
    net::{IpAddr, SocketAddr, ToSocketAddrs},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::{Duration, Instant},
};

use axum::{
    extract::{Extension, Request, State},
    http::{header::AUTHORIZATION, HeaderMap},
    middleware::Next,
    response::Response,
};
use serde::Deserialize;
use sha2::{Digest, Sha256};
use thiserror::Error;
use tokio::sync::{Mutex as AsyncMutex, OwnedSemaphorePermit, Semaphore};

use crate::telemetry::metrics::MetricsHub;

use super::error::{ApiError, EngineErrorCode};

#[derive(Clone)]
pub struct AuthState {
    credentials: CredentialSet,
    throttle: AuthFailureThrottle,
}

impl AuthState {
    pub(crate) fn new(credentials: CredentialSet, metrics: MetricsHub) -> Self {
        Self {
            credentials,
            throttle: AuthFailureThrottle::new(
                Arc::new(SystemMonotonicClock::default()),
                AuthThrottleConfig::default(),
                metrics,
                VerifierGateConfig::default(),
            ),
        }
    }

    #[cfg(test)]
    fn with_throttle(credentials: CredentialSet, throttle: AuthFailureThrottle) -> Self {
        Self {
            credentials,
            throttle,
        }
    }
}

#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd)]
#[serde(rename_all = "snake_case")]
pub enum Scope {
    Inference,
    Observability,
    Admin,
}

const MAX_POLICY_BYTES: u64 = 64 * 1024;
const AUTH_POLICY_SCHEMA_VERSION: u32 = 2;
const MAX_CREDENTIALS: usize = 64;
const MAX_PRINCIPAL_ID_BYTES: usize = 64;
const MAX_BEARER_TOKEN_BYTES: usize = 512;
const MAX_AUTH_FAILURE_SOURCES: usize = 128;
const AUTH_FAILURE_LIMIT: u32 = 5;
const AUTH_FAILURE_WINDOW: Duration = Duration::from_secs(60);
const AUTH_BLOCK_DURATION: Duration = Duration::from_secs(30);
const AUTH_FAILURE_ENTRY_TTL: Duration = Duration::from_secs(5 * 60);
const AUTH_SECURITY_WARNING_INTERVAL: Duration = Duration::from_secs(60);
const MAX_CONCURRENT_AUTH_VERIFIERS: usize = 4;
const MAX_AUTH_VERIFIER_ADMISSIONS: usize = 64;
const AUTH_VERIFIER_ADMISSION_INTERVAL: Duration = Duration::from_millis(2);

trait MonotonicClock: Send + Sync {
    fn now(&self) -> Duration;
}

struct SystemMonotonicClock {
    origin: Instant,
}

impl Default for SystemMonotonicClock {
    fn default() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl MonotonicClock for SystemMonotonicClock {
    fn now(&self) -> Duration {
        self.origin.elapsed()
    }
}

#[derive(Clone, Copy)]
struct AuthThrottleConfig {
    maximum_sources: usize,
    failure_limit: u32,
    failure_window: Duration,
    block_duration: Duration,
    entry_ttl: Duration,
    security_warning_interval: Duration,
}

#[derive(Clone, Copy)]
struct VerifierGateConfig {
    concurrent_verifiers: usize,
    maximum_admissions: usize,
    admission_interval: Duration,
}

impl Default for VerifierGateConfig {
    fn default() -> Self {
        Self {
            concurrent_verifiers: MAX_CONCURRENT_AUTH_VERIFIERS,
            maximum_admissions: MAX_AUTH_VERIFIER_ADMISSIONS,
            admission_interval: AUTH_VERIFIER_ADMISSION_INTERVAL,
        }
    }
}

impl Default for AuthThrottleConfig {
    fn default() -> Self {
        Self {
            maximum_sources: MAX_AUTH_FAILURE_SOURCES,
            failure_limit: AUTH_FAILURE_LIMIT,
            failure_window: AUTH_FAILURE_WINDOW,
            block_duration: AUTH_BLOCK_DURATION,
            entry_ttl: AUTH_FAILURE_ENTRY_TTL,
            security_warning_interval: AUTH_SECURITY_WARNING_INTERVAL,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Ord, PartialEq, PartialOrd)]
/// Fixed-width cache identity for repeated invalid credential responses.
///
/// Loopback TCP addresses identify the host, not the calling process. This key
/// deliberately makes no per-process attribution claim; OS-authenticated IPC is
/// required before local callers can receive distinct trustworthy identities.
enum InvalidCredentialKey {
    Digest([u8; 32]),
    MissingOrMalformed,
}

impl InvalidCredentialKey {
    fn from_digest(candidate: Option<BearerDigest>) -> Self {
        candidate.map_or(Self::MissingOrMalformed, |candidate| {
            Self::Digest(candidate.digest)
        })
    }
}

#[derive(Clone, Copy)]
struct AuthFailureRecord {
    failures: u32,
    window_started: Duration,
    blocked_until: Option<Duration>,
    last_seen: Duration,
}

#[derive(Default)]
struct AuthThrottleState {
    sources: BTreeMap<InvalidCredentialKey, AuthFailureRecord>,
    warning: AuthSecurityWarningState,
}

#[derive(Default)]
struct AuthSecurityWarningState {
    last_emitted_at: Option<Duration>,
    pending_evictions: u64,
    pending_throttle_transitions: u64,
    total_evictions: u64,
    total_throttle_transitions: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct AuthSecurityWarning {
    evictions_since_warning: u64,
    throttle_transitions_since_warning: u64,
    total_evictions: u64,
    total_throttle_transitions: u64,
    tracked_sources: usize,
}

struct VerifierGateInner {
    semaphore: Arc<Semaphore>,
    in_system: AtomicUsize,
    maximum_admissions: usize,
    admission_interval: Duration,
    next_admission: AsyncMutex<tokio::time::Instant>,
}

#[derive(Clone)]
/// Bounded FIFO admission for invalid-candidate state mutation and responses.
///
/// The queue cap bounds retained waiter state, the semaphore bounds concurrent
/// invalid-cache work, and pacing bounds its aggregate rate. Candidate hashing
/// and one keyed immutable digest-index lookup are a separate fixed-cost fast path so known
/// valid principals cannot be rejected merely because invalid work saturated
/// this queue.
struct VerifierGate {
    inner: Arc<VerifierGateInner>,
}

struct VerifierAdmission {
    gate: Arc<VerifierGateInner>,
    _permit: Option<OwnedSemaphorePermit>,
}

impl Drop for VerifierAdmission {
    fn drop(&mut self) {
        self.gate.in_system.fetch_sub(1, Ordering::AcqRel);
    }
}

impl VerifierGate {
    fn new(config: VerifierGateConfig) -> Self {
        Self {
            inner: Arc::new(VerifierGateInner {
                semaphore: Arc::new(Semaphore::new(config.concurrent_verifiers)),
                in_system: AtomicUsize::new(0),
                maximum_admissions: config.maximum_admissions,
                admission_interval: config.admission_interval,
                next_admission: AsyncMutex::new(tokio::time::Instant::now()),
            }),
        }
    }

    async fn admit(&self) -> Option<VerifierAdmission> {
        if self
            .inner
            .in_system
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
                (current < self.inner.maximum_admissions).then_some(current + 1)
            })
            .is_err()
        {
            return None;
        }
        let mut admission = VerifierAdmission {
            gate: self.inner.clone(),
            _permit: None,
        };
        let permit = match self.inner.semaphore.clone().acquire_owned().await {
            Ok(permit) => permit,
            Err(_) => return None,
        };
        admission._permit = Some(permit);

        if !self.inner.admission_interval.is_zero() {
            let slot = {
                let mut next = self.inner.next_admission.lock().await;
                let now = tokio::time::Instant::now();
                let slot = (*next).max(now);
                *next = slot + self.inner.admission_interval;
                slot
            };
            tokio::time::sleep_until(slot).await;
        }
        Some(admission)
    }
}

struct AuthThrottleInner {
    clock: Arc<dyn MonotonicClock>,
    config: AuthThrottleConfig,
    state: Mutex<AuthThrottleState>,
    metrics: MetricsHub,
    verifier_gate: VerifierGate,
    #[cfg(test)]
    credential_lookups: AtomicUsize,
}

#[derive(Clone)]
struct AuthFailureThrottle {
    inner: Arc<AuthThrottleInner>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum AuthFailureDisposition {
    Unauthorized,
    Throttled { retry_after_seconds: u64 },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct AuthFailureObservation {
    disposition: AuthFailureDisposition,
    newly_blocked: bool,
    tracked_sources: usize,
    source_evictions: u64,
    security_warning: Option<AuthSecurityWarning>,
}

#[derive(Debug)]
enum AuthThrottleRejection {
    AdmissionLimited,
    Unauthorized {
        error: Box<ApiError>,
        security_warning: Option<AuthSecurityWarning>,
    },
    Throttled {
        retry_after_seconds: u64,
        security_warning: Option<AuthSecurityWarning>,
    },
}

impl AuthFailureThrottle {
    fn new(
        clock: Arc<dyn MonotonicClock>,
        config: AuthThrottleConfig,
        metrics: MetricsHub,
        verifier_gate_config: VerifierGateConfig,
    ) -> Self {
        Self {
            inner: Arc::new(AuthThrottleInner {
                clock,
                config,
                state: Mutex::new(AuthThrottleState::default()),
                metrics,
                verifier_gate: VerifierGate::new(verifier_gate_config),
                #[cfg(test)]
                credential_lookups: AtomicUsize::new(0),
            }),
        }
    }

    #[cfg(test)]
    fn record_failure(&self, source: InvalidCredentialKey) -> AuthFailureObservation {
        let mut state = self
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let now = self.inner.clock.now();
        self.prune_expired_locked(&mut state, now);
        let observation = self.record_failure_locked(&mut state, source, now);
        self.inner.metrics.record_authentication_failure(
            matches!(
                observation.disposition,
                AuthFailureDisposition::Throttled { .. }
            ),
            observation.tracked_sources,
            observation.source_evictions,
        );
        observation
    }

    async fn authenticate(
        &self,
        headers: &HeaderMap,
        credentials: &CredentialSet,
    ) -> Result<Principal, AuthThrottleRejection> {
        let supplied_digest = bearer_digest(headers);
        // This pre-admission path performs at most one SHA-256 over 512 bytes and
        // one immutable lookup in a RandomState-keyed digest index.
        // It reserves availability for known-valid principals even when invalid
        // work fills its queue. SHA-256 does not make weak secrets safe, but a
        // 256-bit randomly generated bearer remains computationally infeasible
        // to recover by searching its digest.
        let invalid_key = InvalidCredentialKey::from_digest(supplied_digest);
        #[cfg(test)]
        if supplied_digest.is_some() {
            self.inner
                .credential_lookups
                .fetch_add(1, Ordering::Relaxed);
        }
        match authorize_digest(supplied_digest, credentials) {
            Ok(principal) => Ok(principal),
            Err(error) => {
                if let Some(rejection) = self.reject_cached_blocked(invalid_key) {
                    return Err(rejection);
                }
                let Some(_admission) = self.inner.verifier_gate.admit().await else {
                    self.inner.metrics.record_authentication_admission_limited();
                    return Err(AuthThrottleRejection::AdmissionLimited);
                };
                self.record_invalid_verification(invalid_key, error)
            }
        }
    }

    fn reject_cached_blocked(&self, source: InvalidCredentialKey) -> Option<AuthThrottleRejection> {
        let mut state = self
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let now = self.inner.clock.now();
        let retry_after_seconds = state.sources.get_mut(&source).and_then(|record| {
            record
                .blocked_until
                .filter(|blocked_until| *blocked_until > now)
                .map(|blocked_until| {
                    record.last_seen = now;
                    retry_after_seconds(blocked_until - now)
                })
        });
        let tracked_sources = state.sources.len();
        if retry_after_seconds.is_some() {
            self.inner
                .metrics
                .record_authentication_failure(true, tracked_sources, 0);
        }
        retry_after_seconds.map(|retry_after_seconds| AuthThrottleRejection::Throttled {
            retry_after_seconds,
            security_warning: None,
        })
    }

    fn record_invalid_verification(
        &self,
        source: InvalidCredentialKey,
        error: ApiError,
    ) -> Result<Principal, AuthThrottleRejection> {
        let mut state = self
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let now = self.inner.clock.now();
        self.prune_expired_locked(&mut state, now);
        let observation = self.record_failure_locked(&mut state, source, now);
        let throttled = matches!(
            observation.disposition,
            AuthFailureDisposition::Throttled { .. }
        );
        self.inner.metrics.record_authentication_failure(
            throttled,
            observation.tracked_sources,
            observation.source_evictions,
        );
        match observation.disposition {
            AuthFailureDisposition::Unauthorized => Err(AuthThrottleRejection::Unauthorized {
                error: Box::new(error),
                security_warning: observation.security_warning,
            }),
            AuthFailureDisposition::Throttled {
                retry_after_seconds,
            } => Err(AuthThrottleRejection::Throttled {
                retry_after_seconds,
                security_warning: observation.security_warning,
            }),
        }
    }

    fn prune_expired_locked(&self, state: &mut AuthThrottleState, now: Duration) -> bool {
        let previous_len = state.sources.len();
        state
            .sources
            .retain(|_, record| now.saturating_sub(record.last_seen) < self.inner.config.entry_ttl);
        state.sources.len() != previous_len
    }

    fn record_failure_locked(
        &self,
        state: &mut AuthThrottleState,
        source: InvalidCredentialKey,
        now: Duration,
    ) -> AuthFailureObservation {
        let config = self.inner.config;

        let mut source_evictions = 0;
        if !state.sources.contains_key(&source) && state.sources.len() >= config.maximum_sources {
            if let Some(oldest) = state
                .sources
                .iter()
                .min_by(|(left_key, left), (right_key, right)| {
                    left.last_seen
                        .cmp(&right.last_seen)
                        .then_with(|| left_key.cmp(right_key))
                })
                .map(|(key, _)| *key)
            {
                state.sources.remove(&oldest);
                source_evictions = 1;
            }
        }

        let (disposition, newly_blocked) = {
            let record = state.sources.entry(source).or_insert(AuthFailureRecord {
                failures: 0,
                window_started: now,
                blocked_until: None,
                last_seen: now,
            });
            record.last_seen = now;

            if let Some(blocked_until) = record.blocked_until {
                if blocked_until > now {
                    (
                        AuthFailureDisposition::Throttled {
                            retry_after_seconds: retry_after_seconds(blocked_until - now),
                        },
                        false,
                    )
                } else {
                    record.failures = 1;
                    record.window_started = now;
                    record.blocked_until = None;
                    (AuthFailureDisposition::Unauthorized, false)
                }
            } else {
                if now.saturating_sub(record.window_started) >= config.failure_window {
                    record.failures = 0;
                    record.window_started = now;
                }
                record.failures = record.failures.saturating_add(1);
                if record.failures >= config.failure_limit {
                    record.blocked_until = Some(now.saturating_add(config.block_duration));
                    (
                        AuthFailureDisposition::Throttled {
                            retry_after_seconds: retry_after_seconds(config.block_duration),
                        },
                        true,
                    )
                } else {
                    (AuthFailureDisposition::Unauthorized, false)
                }
            }
        };
        let tracked_sources = state.sources.len();
        let security_warning = state.warning.observe(
            now,
            config.security_warning_interval,
            source_evictions,
            newly_blocked,
            tracked_sources,
        );
        AuthFailureObservation {
            disposition,
            newly_blocked,
            tracked_sources,
            source_evictions,
            security_warning,
        }
    }
}

impl AuthSecurityWarningState {
    fn observe(
        &mut self,
        now: Duration,
        minimum_interval: Duration,
        source_evictions: u64,
        newly_blocked: bool,
        tracked_sources: usize,
    ) -> Option<AuthSecurityWarning> {
        if source_evictions == 0 && !newly_blocked {
            return None;
        }
        self.pending_evictions = self.pending_evictions.saturating_add(source_evictions);
        self.total_evictions = self.total_evictions.saturating_add(source_evictions);
        if newly_blocked {
            self.pending_throttle_transitions = self.pending_throttle_transitions.saturating_add(1);
            self.total_throttle_transitions = self.total_throttle_transitions.saturating_add(1);
        }
        let emission_due = self
            .last_emitted_at
            .is_none_or(|last| now.saturating_sub(last) >= minimum_interval);
        if !emission_due {
            return None;
        }
        let warning = AuthSecurityWarning {
            evictions_since_warning: self.pending_evictions,
            throttle_transitions_since_warning: self.pending_throttle_transitions,
            total_evictions: self.total_evictions,
            total_throttle_transitions: self.total_throttle_transitions,
            tracked_sources,
        };
        self.pending_evictions = 0;
        self.pending_throttle_transitions = 0;
        self.last_emitted_at = Some(now);
        Some(warning)
    }
}

fn retry_after_seconds(duration: Duration) -> u64 {
    duration
        .as_secs()
        .saturating_add(u64::from(duration.subsec_nanos() > 0))
        .max(1)
}

#[derive(Debug, Error)]
pub enum AuthPolicyError {
    #[error(
        "authentication policy file is unavailable (kind={kind:?}, os_error={raw_os_error:?})"
    )]
    Io {
        #[source]
        source: io::Error,
        kind: io::ErrorKind,
        raw_os_error: Option<i32>,
    },
    #[error("authentication policy JSON is invalid")]
    Json(#[from] serde_json::Error),
    #[error("authentication policy contract is invalid: {0}")]
    Invalid(&'static str),
}

impl From<io::Error> for AuthPolicyError {
    fn from(source: io::Error) -> Self {
        Self::Io {
            kind: source.kind(),
            raw_os_error: source.raw_os_error(),
            source,
        }
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct AuthPolicy {
    schema_version: u32,
    credentials: Vec<PolicyCredential>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct PolicyCredential {
    principal_id: String,
    token_sha256: String,
    token_byte_length: usize,
    scopes: Vec<Scope>,
}

pub fn load_policy(path: &Path) -> Result<CredentialSet, AuthPolicyError> {
    reject_link_components(path)?;
    let file = open_private_policy(path)?;
    let metadata = file.metadata()?;
    if metadata.len() > MAX_POLICY_BYTES {
        return Err(AuthPolicyError::Invalid("policy exceeds 64 KiB"));
    }
    let version = policy_version(&file, &metadata)?;
    let bytes = read_stable_policy_with(&file, version, || {})?;
    let policy: AuthPolicy = serde_json::from_slice(&bytes)?;
    if policy.schema_version != AUTH_POLICY_SCHEMA_VERSION {
        return Err(AuthPolicyError::Invalid(
            "unsupported authentication policy schema_version",
        ));
    }
    if policy.credentials.is_empty() || policy.credentials.len() > MAX_CREDENTIALS {
        return Err(AuthPolicyError::Invalid(
            "credentials must contain between 1 and 64 entries",
        ));
    }
    let mut principal_ids = BTreeSet::new();
    let mut token_digests = BTreeSet::new();
    let mut credentials = Vec::with_capacity(policy.credentials.len());
    for raw in policy.credentials {
        if raw.principal_id.is_empty()
            || raw.principal_id.len() > MAX_PRINCIPAL_ID_BYTES
            || !raw
                .principal_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.'))
        {
            return Err(AuthPolicyError::Invalid("principal_id is invalid"));
        }
        if !principal_ids.insert(raw.principal_id.clone()) {
            return Err(AuthPolicyError::Invalid(
                "principal_id values must be unique",
            ));
        }
        let token_sha256 = parse_digest(&raw.token_sha256)?;
        validate_token_byte_length(raw.token_byte_length)?;
        if !token_digests.insert(token_sha256) {
            return Err(AuthPolicyError::Invalid(
                "token_sha256 values must be unique",
            ));
        }
        if raw.scopes.is_empty() {
            return Err(AuthPolicyError::Invalid(
                "credential scopes must not be empty",
            ));
        }
        let scope_count = raw.scopes.len();
        let scopes = raw.scopes.into_iter().collect::<BTreeSet<_>>();
        if scopes.len() != scope_count {
            return Err(AuthPolicyError::Invalid("credential scopes must be unique"));
        }
        credentials.push(Credential::from_digest(
            token_sha256,
            raw.token_byte_length,
            Principal::new(
                raw.principal_id,
                Arc::from(scopes.into_iter().collect::<Vec<_>>()),
            ),
        ));
    }
    CredentialSet::new(credentials)
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PolicyIdentity {
    first: u64,
    second: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct PolicyVersion {
    identity: PolicyIdentity,
    length: u64,
    modified_seconds: i64,
    modified_nanoseconds: i64,
    changed_seconds: i64,
    changed_nanoseconds: i64,
}

fn read_stable_policy_with(
    file: &File,
    expected: PolicyVersion,
    between_reads: impl FnOnce(),
) -> Result<Vec<u8>, AuthPolicyError> {
    let first = read_policy_bytes(file, expected.length)?;
    verify_policy_version(file, expected)?;
    between_reads();
    let second = read_policy_bytes(file, expected.length)?;
    verify_policy_version(file, expected)?;
    if first != second {
        return Err(AuthPolicyError::Invalid(
            "policy content changed while it was being read",
        ));
    }
    Ok(first)
}

fn read_policy_bytes(file: &File, expected_length: u64) -> Result<Vec<u8>, AuthPolicyError> {
    let capacity = usize::try_from(expected_length)
        .map_err(|_| AuthPolicyError::Invalid("policy length is not representable"))?;
    let mut reader = file.try_clone()?;
    reader.seek(SeekFrom::Start(0))?;
    let mut bytes = Vec::with_capacity(capacity);
    reader.take(MAX_POLICY_BYTES + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > MAX_POLICY_BYTES {
        return Err(AuthPolicyError::Invalid("policy exceeds 64 KiB"));
    }
    if bytes.len() as u64 != expected_length {
        return Err(AuthPolicyError::Invalid(
            "policy length changed while it was being read",
        ));
    }
    Ok(bytes)
}

fn verify_policy_version(file: &File, expected: PolicyVersion) -> Result<(), AuthPolicyError> {
    let metadata = file.metadata()?;
    verify_private_policy_metadata(file, &metadata)?;
    if policy_version(file, &metadata)? != expected {
        return Err(AuthPolicyError::Invalid(
            "policy identity or version changed while it was being read",
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn policy_version(file: &File, metadata: &fs::Metadata) -> Result<PolicyVersion, AuthPolicyError> {
    use std::os::unix::fs::MetadataExt;

    Ok(PolicyVersion {
        identity: policy_identity(file, metadata)?,
        length: metadata.len(),
        modified_seconds: metadata.mtime(),
        modified_nanoseconds: metadata.mtime_nsec(),
        changed_seconds: metadata.ctime(),
        changed_nanoseconds: metadata.ctime_nsec(),
    })
}

#[cfg(windows)]
fn policy_version(file: &File, metadata: &fs::Metadata) -> Result<PolicyVersion, AuthPolicyError> {
    use std::os::windows::fs::MetadataExt;

    let modified = metadata.last_write_time();
    Ok(PolicyVersion {
        identity: policy_identity(file, metadata)?,
        length: metadata.file_size(),
        modified_seconds: i64::try_from(modified / 10_000_000).unwrap_or(i64::MAX),
        modified_nanoseconds: i64::try_from((modified % 10_000_000) * 100).unwrap_or(i64::MAX),
        changed_seconds: 0,
        changed_nanoseconds: 0,
    })
}

#[cfg(not(any(unix, windows)))]
fn policy_version(
    _file: &File,
    _metadata: &fs::Metadata,
) -> Result<PolicyVersion, AuthPolicyError> {
    Err(AuthPolicyError::Invalid(
        "stable policy versioning is unsupported on this platform",
    ))
}

fn open_private_policy(path: &Path) -> Result<File, AuthPolicyError> {
    open_private_policy_with(path, || {})
}

fn open_private_policy_with(
    path: &Path,
    between_handles: impl FnOnce(),
) -> Result<File, AuthPolicyError> {
    let file = open_policy_handle(path)?;
    let metadata = file.metadata()?;
    verify_private_policy_metadata(&file, &metadata)?;

    // Repeat the component walk after acquiring the protected handle, then
    // reopen the path and prove that it still resolves to the same object.
    // The authoritative bytes always come from `file`; this confirmation
    // closes the component-walk-to-open substitution window.
    between_handles();
    reject_link_components(path)?;
    let confirmation = open_policy_handle(path)?;
    let confirmation_metadata = confirmation.metadata()?;
    verify_private_policy_metadata(&confirmation, &confirmation_metadata)?;
    if policy_identity(&file, &metadata)? != policy_identity(&confirmation, &confirmation_metadata)?
    {
        return Err(AuthPolicyError::Invalid(
            "policy path changed while its handle was being acquired",
        ));
    }
    Ok(file)
}

fn open_policy_handle(path: &Path) -> Result<File, AuthPolicyError> {
    let mut options = OpenOptions::new();
    options.read(true);
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::OpenOptionsExt;

        const O_NOFOLLOW: i32 = 0x0002_0000;
        options.custom_flags(O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;

        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options
            .share_mode(FILE_SHARE_READ)
            .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    #[cfg(not(any(target_os = "linux", windows)))]
    return Err(AuthPolicyError::Invalid(
        "private policy verification is unsupported on this platform",
    ));
    options.open(path).map_err(Into::into)
}

fn reject_link_components(path: &Path) -> Result<(), AuthPolicyError> {
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component);
        if current.as_os_str().is_empty() {
            continue;
        }
        let metadata = fs::symlink_metadata(&current)?;
        #[cfg(unix)]
        if metadata.file_type().is_symlink() {
            return Err(AuthPolicyError::Invalid(
                "policy path must not contain symbolic links",
            ));
        }
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;

            const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
            if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
                return Err(AuthPolicyError::Invalid(
                    "policy path must not contain reparse points",
                ));
            }
        }
    }
    Ok(())
}

#[cfg(unix)]
fn verify_private_policy_metadata(
    _file: &File,
    metadata: &fs::Metadata,
) -> Result<(), AuthPolicyError> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    unsafe extern "C" {
        fn geteuid() -> u32;
    }

    // SAFETY: `geteuid` has no arguments and returns the effective process UID.
    let effective_uid = unsafe { geteuid() };
    if !metadata.is_file()
        || metadata.file_type().is_symlink()
        || metadata.nlink() != 1
        || !unix_owner_matches_effective_identity(metadata.uid(), effective_uid)
        || metadata.permissions().mode() & 0o077 != 0
    {
        return Err(AuthPolicyError::Invalid(
            "policy must be an owner-only regular file with one link",
        ));
    }
    Ok(())
}

#[cfg(unix)]
const fn unix_owner_matches_effective_identity(policy_uid: u32, effective_uid: u32) -> bool {
    policy_uid == effective_uid
}

#[cfg(windows)]
fn verify_private_policy_metadata(
    file: &File,
    metadata: &fs::Metadata,
) -> Result<(), AuthPolicyError> {
    use std::os::windows::fs::MetadataExt;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
    if !metadata.is_file() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(AuthPolicyError::Invalid(
            "policy must be a non-reparse regular file",
        ));
    }
    windows_policy_acl::verify_private_handle(file)?;
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn verify_private_policy_metadata(
    _file: &File,
    _metadata: &fs::Metadata,
) -> Result<(), AuthPolicyError> {
    Err(AuthPolicyError::Invalid(
        "private policy verification is unsupported on this platform",
    ))
}

#[cfg(unix)]
fn policy_identity(
    _file: &File,
    metadata: &fs::Metadata,
) -> Result<PolicyIdentity, AuthPolicyError> {
    use std::os::unix::fs::MetadataExt;

    Ok(PolicyIdentity {
        first: metadata.dev(),
        second: metadata.ino(),
    })
}

#[cfg(windows)]
fn policy_identity(
    file: &File,
    _metadata: &fs::Metadata,
) -> Result<PolicyIdentity, AuthPolicyError> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: `file` owns a live handle and `information` is writable storage.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut information) } == 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    if information.nNumberOfLinks != 1 {
        return Err(AuthPolicyError::Invalid(
            "policy must have exactly one filesystem link",
        ));
    }
    Ok(PolicyIdentity {
        first: u64::from(information.dwVolumeSerialNumber),
        second: (u64::from(information.nFileIndexHigh) << 32)
            | u64::from(information.nFileIndexLow),
    })
}

#[cfg(not(any(unix, windows)))]
fn policy_identity(
    _file: &File,
    _metadata: &fs::Metadata,
) -> Result<PolicyIdentity, AuthPolicyError> {
    Err(AuthPolicyError::Invalid(
        "stable policy identity is unsupported on this platform",
    ))
}

#[cfg(windows)]
mod windows_policy_acl {
    use std::{ffi::c_void, fs::File, io, mem::size_of, os::windows::io::AsRawHandle, ptr};

    use windows_sys::Win32::{
        Foundation::{CloseHandle, GetLastError, LocalFree, ERROR_INSUFFICIENT_BUFFER, HANDLE},
        Security::{
            Authorization::{ConvertSidToStringSidW, GetSecurityInfo, SE_FILE_OBJECT},
            GetAce, GetSecurityDescriptorControl, GetTokenInformation, TokenUser,
            ACCESS_ALLOWED_ACE, ACL, DACL_SECURITY_INFORMATION, INHERITED_ACE,
            OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SE_DACL_PROTECTED, TOKEN_QUERY,
            TOKEN_USER,
        },
        Storage::FileSystem::FILE_ALL_ACCESS,
        System::{
            SystemServices::ACCESS_ALLOWED_ACE_TYPE,
            Threading::{GetCurrentProcess, OpenProcessToken},
        },
    };

    pub(super) fn verify_private_handle(file: &File) -> io::Result<()> {
        let descriptor = query_descriptor(file)?;
        let mut control = 0_u16;
        let mut revision = 0_u32;
        // SAFETY: the descriptor owns one valid self-relative security descriptor.
        if unsafe {
            GetSecurityDescriptorControl(
                descriptor.security_descriptor,
                &mut control,
                &mut revision,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        if control & SE_DACL_PROTECTED == 0 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "policy DACL inheritance is not disabled",
            ));
        }
        let acl = descriptor.dacl;
        // SAFETY: descriptor ownership keeps the ACL storage live.
        if acl.is_null() || unsafe { (*acl).AceCount } != 2 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "policy DACL is not limited to owner and SYSTEM",
            ));
        }
        let owner = sid_to_string(descriptor.owner)?;
        let process_identity = current_process_sid()?;
        if !owner_matches_process_identity(&owner, &process_identity) {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "policy owner does not match the current process identity",
            ));
        }
        let mut saw_owner = false;
        let mut saw_system = false;
        for index in 0..2 {
            let mut raw_ace = ptr::null_mut();
            // SAFETY: the ACL has exactly two entries and output storage is valid.
            if unsafe { GetAce(acl, index, &mut raw_ace) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let ace = raw_ace.cast::<ACCESS_ALLOWED_ACE>();
            // SAFETY: `GetAce` returned an ACE within the live ACL.
            let header = unsafe { (*ace).Header };
            if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE
                || u32::from(header.AceFlags) & INHERITED_ACE != 0
            {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "policy DACL contains a non-explicit allow ACE",
                ));
            }
            // SAFETY: the validated allow ACE has ACCESS_ALLOWED_ACE layout.
            let mask = unsafe { (*ace).Mask };
            if mask & FILE_ALL_ACCESS != FILE_ALL_ACCESS {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "policy DACL does not grant full control",
                ));
            }
            // SAFETY: `SidStart` begins the variable-length SID in an allow ACE.
            let sid = unsafe { ptr::addr_of!((*ace).SidStart).cast_mut().cast::<c_void>() };
            let value = sid_to_string(sid)?;
            saw_owner |= value == owner;
            saw_system |= value == "S-1-5-18";
        }
        if !saw_owner || !saw_system {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "policy DACL must contain exactly owner and SYSTEM",
            ));
        }
        Ok(())
    }

    pub(super) fn owner_matches_process_identity(owner: &str, process_identity: &str) -> bool {
        owner == process_identity
    }

    fn current_process_sid() -> io::Result<String> {
        let mut token = ptr::null_mut();
        // SAFETY: `GetCurrentProcess` returns a valid pseudo-handle and `token`
        // is writable storage for the newly opened token handle.
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let token = OwnedHandle(token);
        let mut required = 0_u32;
        // SAFETY: the zero-length query intentionally supplies no destination
        // and asks the API for the required TOKEN_USER buffer size.
        let sizing_result =
            unsafe { GetTokenInformation(token.0, TokenUser, ptr::null_mut(), 0, &mut required) };
        // SAFETY: `GetLastError` reads thread-local error state immediately
        // after the failed sizing query.
        let sizing_error = unsafe { GetLastError() };
        if sizing_result != 0
            || sizing_error != ERROR_INSUFFICIENT_BUFFER
            || required < size_of::<TOKEN_USER>() as u32
        {
            return Err(io::Error::last_os_error());
        }
        let words = (required as usize).div_ceil(size_of::<usize>());
        let mut storage = vec![0_usize; words];
        // SAFETY: `storage` is pointer-aligned and spans at least `required`
        // writable bytes for the TOKEN_USER response and its trailing SID.
        if unsafe {
            GetTokenInformation(
                token.0,
                TokenUser,
                storage.as_mut_ptr().cast::<c_void>(),
                required,
                &mut required,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: the successful query initialized a TOKEN_USER at the start
        // of the aligned storage and keeps its trailing SID live in `storage`.
        let user = unsafe { &*storage.as_ptr().cast::<TOKEN_USER>() };
        sid_to_string(user.User.Sid)
    }

    struct OwnedHandle(HANDLE);

    impl Drop for OwnedHandle {
        fn drop(&mut self) {
            // SAFETY: `OpenProcessToken` returned this owned live handle.
            unsafe { CloseHandle(self.0) };
        }
    }

    struct Descriptor {
        security_descriptor: PSECURITY_DESCRIPTOR,
        owner: PSID,
        dacl: *mut ACL,
    }

    impl Drop for Descriptor {
        fn drop(&mut self) {
            // SAFETY: `GetSecurityInfo` allocates this descriptor with LocalAlloc.
            unsafe { LocalFree(self.security_descriptor.cast::<c_void>()) };
        }
    }

    fn query_descriptor(file: &File) -> io::Result<Descriptor> {
        let mut security_descriptor = ptr::null_mut();
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        // SAFETY: the file handle is live and all requested outputs are valid pointers.
        let status = unsafe {
            GetSecurityInfo(
                file.as_raw_handle() as _,
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                ptr::null_mut(),
                &mut dacl,
                ptr::null_mut(),
                &mut security_descriptor,
            )
        };
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status as i32));
        }
        if security_descriptor.is_null() || owner.is_null() || dacl.is_null() {
            if !security_descriptor.is_null() {
                // SAFETY: the API allocated this non-null descriptor.
                unsafe { LocalFree(security_descriptor.cast::<c_void>()) };
            }
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "policy file has no owner or DACL",
            ));
        }
        Ok(Descriptor {
            security_descriptor,
            owner,
            dacl,
        })
    }

    fn sid_to_string(sid: PSID) -> io::Result<String> {
        let mut string = ptr::null_mut();
        // SAFETY: `sid` points into a live descriptor and `string` is an out pointer.
        if unsafe { ConvertSidToStringSidW(sid, &mut string) } == 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: successful conversion returned NUL-terminated UTF-16.
        let value = unsafe { utf16_ptr_to_string(string) };
        // SAFETY: conversion allocated the string with LocalAlloc.
        unsafe { LocalFree(string.cast::<c_void>()) };
        Ok(value)
    }

    unsafe fn utf16_ptr_to_string(value: *const u16) -> String {
        let mut length = 0;
        // SAFETY: caller provides a valid NUL-terminated UTF-16 allocation.
        while unsafe { *value.add(length) } != 0 {
            length += 1;
        }
        // SAFETY: the scan above established the initialized slice length.
        String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(value, length) })
    }
}

fn parse_digest(value: &str) -> Result<[u8; 32], AuthPolicyError> {
    if value.len() != 64
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(AuthPolicyError::Invalid(
            "token_sha256 must be exactly 64 lowercase hexadecimal characters",
        ));
    }
    let mut digest = [0_u8; 32];
    for (index, pair) in value.as_bytes().chunks_exact(2).enumerate() {
        let text = std::str::from_utf8(pair)
            .map_err(|_| AuthPolicyError::Invalid("token_sha256 contains invalid UTF-8"))?;
        digest[index] = u8::from_str_radix(text, 16)
            .map_err(|_| AuthPolicyError::Invalid("token_sha256 is not hexadecimal"))?;
    }
    Ok(digest)
}

#[derive(Clone, Debug)]
pub struct Principal {
    pub id: Arc<str>,
    scopes: Arc<[Scope]>,
}

impl Principal {
    pub fn new(id: impl Into<Arc<str>>, scopes: impl Into<Arc<[Scope]>>) -> Self {
        Self {
            id: id.into(),
            scopes: scopes.into(),
        }
    }

    pub(crate) fn permits(&self, required: Scope) -> bool {
        self.scopes.contains(&required)
    }
}

#[derive(Clone, Debug)]
pub struct Credential {
    token_sha256: [u8; 32],
    token_byte_length: usize,
    pub principal: Principal,
}

#[derive(Clone, Debug)]
pub struct CredentialSet {
    credentials: Arc<[Credential]>,
    by_digest: Arc<HashMap<[u8; 32], usize>>,
}

impl CredentialSet {
    pub fn new(credentials: impl IntoIterator<Item = Credential>) -> Result<Self, AuthPolicyError> {
        let credentials = credentials.into_iter().collect::<Vec<_>>();
        if credentials.is_empty() || credentials.len() > MAX_CREDENTIALS {
            return Err(AuthPolicyError::Invalid(
                "credentials must contain between 1 and 64 entries",
            ));
        }
        // std::collections::HashMap uses a per-instance randomized RandomState,
        // preventing attacker-selected digest keys from creating deterministic
        // collision chains in the immutable policy index.
        let mut by_digest = HashMap::with_capacity(credentials.len());
        for (index, credential) in credentials.iter().enumerate() {
            if by_digest.insert(credential.token_sha256, index).is_some() {
                return Err(AuthPolicyError::Invalid(
                    "token_sha256 values must be unique",
                ));
            }
        }
        Ok(Self {
            credentials: Arc::from(credentials),
            by_digest: Arc::new(by_digest),
        })
    }

    pub fn len(&self) -> usize {
        self.credentials.len()
    }

    pub fn is_empty(&self) -> bool {
        self.credentials.is_empty()
    }

    fn find(&self, candidate: BearerDigest) -> Option<Principal> {
        let credential = &self.credentials[*self.by_digest.get(&candidate.digest)?];
        (candidate.token_byte_length == credential.token_byte_length)
            .then(|| credential.principal.clone())
    }
}

impl Credential {
    pub fn new(token: impl AsRef<[u8]>, principal: Principal) -> Result<Self, AuthPolicyError> {
        let token = token.as_ref();
        validate_token_byte_length(token.len())?;
        let token_sha256: [u8; 32] = Sha256::digest(token).into();
        Ok(Self {
            token_sha256,
            token_byte_length: token.len(),
            principal,
        })
    }

    fn from_digest(token_sha256: [u8; 32], token_byte_length: usize, principal: Principal) -> Self {
        Self {
            token_sha256,
            token_byte_length,
            principal,
        }
    }

    pub fn owner(token: impl AsRef<[u8]>) -> Result<Self, AuthPolicyError> {
        Self::new(
            token,
            Principal::new(
                "local-supervisor",
                Arc::from([Scope::Inference, Scope::Observability, Scope::Admin]),
            ),
        )
    }
}

fn validate_token_byte_length(token_byte_length: usize) -> Result<(), AuthPolicyError> {
    if !(1..=MAX_BEARER_TOKEN_BYTES).contains(&token_byte_length) {
        return Err(AuthPolicyError::Invalid(
            "token_byte_length must be between 1 and 512 bytes",
        ));
    }
    Ok(())
}

pub fn assert_loopback(host: &str) -> Result<(), ApiError> {
    resolve_loopback(host, 0).map(|_| ())
}

/// Resolves an explicit local host into validated loopback socket addresses.
pub fn resolve_loopback(host: &str, port: u16) -> Result<Vec<SocketAddr>, ApiError> {
    let resolved = if let Ok(ip) = host.parse::<IpAddr>() {
        vec![SocketAddr::new(ip, port)]
    } else if host.eq_ignore_ascii_case("localhost") {
        (host, port)
            .to_socket_addrs()
            .map_err(|_| loopback_error("engine localhost resolution failed"))?
            .collect::<Vec<_>>()
    } else {
        return Err(loopback_error(
            "engine host must be localhost or an IP loopback address",
        ));
    };
    if resolved.is_empty() || resolved.iter().any(|address| !address.ip().is_loopback()) {
        return Err(loopback_error("engine may bind only to loopback"));
    }
    Ok(resolved)
}

fn loopback_error(message: &'static str) -> ApiError {
    ApiError::new(EngineErrorCode::Unauthorized, message)
}

#[derive(Clone, Copy)]
struct BearerDigest {
    digest: [u8; 32],
    token_byte_length: usize,
}

fn bearer_digest(headers: &HeaderMap) -> Option<BearerDigest> {
    headers
        .get(AUTHORIZATION)
        .and_then(|value| value.to_str().ok())
        .and_then(|value| value.strip_prefix("Bearer "))
        .filter(|value| !value.is_empty() && value.len() <= MAX_BEARER_TOKEN_BYTES)
        .map(|value| BearerDigest {
            digest: Sha256::digest(value.as_bytes()).into(),
            token_byte_length: value.len(),
        })
}

fn authorize_digest(
    supplied_digest: Option<BearerDigest>,
    credentials: &CredentialSet,
) -> Result<Principal, ApiError> {
    supplied_digest
        .and_then(|candidate| credentials.find(candidate))
        .ok_or_else(|| {
            ApiError::new(
                EngineErrorCode::Unauthorized,
                "missing or invalid bearer token",
            )
        })
}

pub fn authorize(headers: &HeaderMap, credentials: &CredentialSet) -> Result<Principal, ApiError> {
    authorize_digest(bearer_digest(headers), credentials)
}

pub async fn require_bearer(
    State(state): State<AuthState>,
    mut request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    match state
        .throttle
        .authenticate(request.headers(), &state.credentials)
        .await
    {
        Ok(principal) => {
            request.extensions_mut().insert(principal);
            Ok(next.run(request).await)
        }
        Err(AuthThrottleRejection::Unauthorized {
            error,
            security_warning,
        }) => {
            emit_auth_security_warning(security_warning);
            Err(*error)
        }
        Err(AuthThrottleRejection::AdmissionLimited) => Err(ApiError::new(
            EngineErrorCode::QueueFull,
            "authentication verifier admission is saturated",
        )),
        Err(AuthThrottleRejection::Throttled {
            retry_after_seconds,
            security_warning,
        }) => {
            emit_auth_security_warning(security_warning);
            Err(ApiError::authentication_throttled(retry_after_seconds))
        }
    }
}

fn emit_auth_security_warning(warning: Option<AuthSecurityWarning>) {
    emit_auth_security_warning_with(warning, &TracingAuthSecurityWarningSink);
}

trait AuthSecurityWarningSink {
    fn emit(&self, warning: AuthSecurityWarning);
}

struct TracingAuthSecurityWarningSink;

impl AuthSecurityWarningSink for TracingAuthSecurityWarningSink {
    fn emit(&self, warning: AuthSecurityWarning) {
        tracing::warn!(
            auth_evictions_since_warning = warning.evictions_since_warning,
            auth_throttle_transitions_since_warning = warning.throttle_transitions_since_warning,
            auth_source_evictions_total = warning.total_evictions,
            auth_throttle_transitions_total = warning.total_throttle_transitions,
            auth_tracked_sources = warning.tracked_sources,
            "authentication limiter security events (rate-limited aggregate)"
        );
    }
}

fn emit_auth_security_warning_with(
    warning: Option<AuthSecurityWarning>,
    sink: &impl AuthSecurityWarningSink,
) {
    if let Some(warning) = warning {
        sink.emit(warning);
    }
}

pub async fn require_scope(
    State(required): State<Scope>,
    Extension(principal): Extension<Principal>,
    request: Request,
    next: Next,
) -> Result<Response, ApiError> {
    if !principal.permits(required) {
        return Err(ApiError::forbidden(
            "authenticated principal lacks the required engine scope",
        ));
    }
    Ok(next.run(request).await)
}

#[cfg(test)]
mod tests {
    use std::{
        sync::{
            atomic::{AtomicU64, Ordering},
            Barrier,
        },
        thread,
    };

    use super::*;
    use axum::{
        body::Body,
        http::{header::AUTHORIZATION, HeaderValue, Request as HttpRequest, StatusCode},
        middleware,
        routing::get,
        Router,
    };
    use tower::ServiceExt;

    #[derive(Default)]
    struct CountingWarningSink {
        emitted: AtomicUsize,
        warnings: Mutex<Vec<AuthSecurityWarning>>,
    }

    impl AuthSecurityWarningSink for CountingWarningSink {
        fn emit(&self, warning: AuthSecurityWarning) {
            self.emitted.fetch_add(1, Ordering::SeqCst);
            self.warnings
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .push(warning);
        }
    }

    #[derive(Default)]
    struct ManualClock {
        milliseconds: AtomicU64,
        reads: AtomicU64,
    }

    impl ManualClock {
        fn advance(&self, duration: Duration) {
            self.milliseconds.fetch_add(
                u64::try_from(duration.as_millis()).unwrap(),
                Ordering::SeqCst,
            );
        }
    }

    impl MonotonicClock for ManualClock {
        fn now(&self) -> Duration {
            self.reads.fetch_add(1, Ordering::SeqCst);
            Duration::from_millis(self.milliseconds.load(Ordering::SeqCst))
        }
    }

    fn test_throttle(
        clock: Arc<ManualClock>,
        maximum_sources: usize,
        failure_limit: u32,
    ) -> AuthFailureThrottle {
        test_throttle_with_metrics(clock, maximum_sources, failure_limit, MetricsHub::default())
    }

    fn test_throttle_with_metrics(
        clock: Arc<ManualClock>,
        maximum_sources: usize,
        failure_limit: u32,
        metrics: MetricsHub,
    ) -> AuthFailureThrottle {
        AuthFailureThrottle::new(
            clock,
            AuthThrottleConfig {
                maximum_sources,
                failure_limit,
                failure_window: Duration::from_secs(20),
                block_duration: Duration::from_secs(10),
                entry_ttl: Duration::from_secs(30),
                security_warning_interval: Duration::from_secs(60),
            },
            metrics,
            VerifierGateConfig {
                concurrent_verifiers: 4,
                maximum_admissions: 64,
                admission_interval: Duration::ZERO,
            },
        )
    }

    fn invalid_key(value: u8) -> InvalidCredentialKey {
        InvalidCredentialKey::Digest([value; 32])
    }

    fn numbered_invalid_key(value: u64) -> InvalidCredentialKey {
        InvalidCredentialKey::Digest(Sha256::digest(value.to_le_bytes()).into())
    }

    fn credential_set(credentials: impl IntoIterator<Item = Credential>) -> CredentialSet {
        CredentialSet::new(credentials).unwrap()
    }

    fn write_policy(value: serde_json::Value) -> (tempfile::TempDir, PathBuf) {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("auth-policy.json");
        let mut file = File::create(&path).unwrap();
        serde_json::to_writer(&mut file, &value).unwrap();
        drop(file);
        crate::store::session::secure_and_verify_private_path(&path).unwrap();
        (directory, path)
    }

    fn valid_policy() -> serde_json::Value {
        serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": format!("{:x}", Sha256::digest(b"secret")),
                "token_byte_length": 6,
                "scopes": ["inference"]
            }]
        })
    }

    #[test]
    fn digest_only_policy_authenticates_without_storing_plaintext() {
        let digest = format!("{:x}", Sha256::digest(b"secret"));
        let (_directory, path) = write_policy(serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": digest,
                "token_byte_length": 6,
                "scopes": ["inference"]
            }]
        }));
        let credentials = load_policy(&path).unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(AUTHORIZATION, HeaderValue::from_static("Bearer secret"));

        let principal = authorize(&headers, &credentials).unwrap();
        assert_eq!(principal.id.as_ref(), "worker-1");
        assert!(principal.permits(Scope::Inference));
        assert!(!principal.permits(Scope::Admin));
    }

    #[test]
    fn credential_length_contract_rejects_impossible_and_legacy_policy_records() {
        assert!(Credential::owner([]).is_err());
        assert!(Credential::owner(vec![b'x'; MAX_BEARER_TOKEN_BYTES + 1]).is_err());

        let digest = format!("{:x}", Sha256::digest(b"secret"));
        let (_missing_directory, missing_path) = write_policy(serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": digest,
                "scopes": ["inference"]
            }]
        }));
        assert!(matches!(
            load_policy(&missing_path),
            Err(AuthPolicyError::Json(_))
        ));

        let (_oversized_directory, oversized_path) = write_policy(serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": digest,
                "token_byte_length": MAX_BEARER_TOKEN_BYTES + 1,
                "scopes": ["inference"]
            }]
        }));
        assert!(matches!(
            load_policy(&oversized_path),
            Err(AuthPolicyError::Invalid(
                "token_byte_length must be between 1 and 512 bytes"
            ))
        ));

        let (_legacy_directory, legacy_path) = write_policy(serde_json::json!({
            "schema_version": 1,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": digest,
                "token_byte_length": 6,
                "scopes": ["inference"]
            }]
        }));
        assert!(matches!(
            load_policy(&legacy_path),
            Err(AuthPolicyError::Invalid(
                "unsupported authentication policy schema_version"
            ))
        ));
    }

    #[test]
    fn maximum_length_credential_authenticates_with_exact_length_match() {
        let token = "x".repeat(MAX_BEARER_TOKEN_BYTES);
        let credential = Credential::owner(token.as_bytes()).unwrap();
        let mut headers = HeaderMap::new();
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_str(&format!("Bearer {token}")).unwrap(),
        );

        assert!(authorize(&headers, &credential_set([credential])).is_ok());
    }

    #[test]
    fn immutable_credential_index_rejects_duplicate_digests() {
        let first = Credential::new(
            "same-secret",
            Principal::new("first", Arc::from([Scope::Inference])),
        )
        .unwrap();
        let second = Credential::new(
            "same-secret",
            Principal::new("second", Arc::from([Scope::Admin])),
        )
        .unwrap();

        assert!(matches!(
            CredentialSet::new([first, second]),
            Err(AuthPolicyError::Invalid(
                "token_sha256 values must be unique"
            ))
        ));
    }

    #[test]
    fn policy_rejects_plaintext_unknown_fields_and_noncanonical_digest() {
        let digest = format!("{:X}", Sha256::digest(b"secret"));
        let (_directory, path) = write_policy(serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": digest,
                "token_byte_length": 6,
                "token": "secret",
                "scopes": ["inference"]
            }]
        }));
        assert!(load_policy(&path).is_err());

        let (_directory, path) = write_policy(serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-1",
                "token_sha256": digest,
                "token_byte_length": 6,
                "scopes": ["inference"]
            }]
        }));
        assert!(load_policy(&path).is_err());
    }

    #[tokio::test]
    async fn policy_digest_index_cardinality_is_capped_at_sixty_four() {
        let policy_with = |count: usize| {
            let credentials = (0..count)
                .map(|index| {
                    serde_json::json!({
                        "principal_id": format!("worker-{index}"),
                        "token_sha256": format!("{:x}", Sha256::digest(index.to_le_bytes())),
                        "token_byte_length": std::mem::size_of::<usize>(),
                        "scopes": ["inference"]
                    })
                })
                .collect::<Vec<_>>();
            serde_json::json!({
                "schema_version": AUTH_POLICY_SCHEMA_VERSION,
                "credentials": credentials
            })
        };

        let (_accepted_directory, accepted_path) = write_policy(policy_with(MAX_CREDENTIALS));
        let accepted = load_policy(&accepted_path).unwrap();
        assert_eq!(accepted.len(), MAX_CREDENTIALS);
        let throttle = test_throttle(Arc::new(ManualClock::default()), 8, 5);
        let mut headers = HeaderMap::new();
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_static("Bearer not-present"),
        );
        assert!(throttle.authenticate(&headers, &accepted).await.is_err());
        assert_eq!(throttle.inner.credential_lookups.load(Ordering::Relaxed), 1);
        let (_rejected_directory, rejected_path) = write_policy(policy_with(MAX_CREDENTIALS + 1));
        assert!(matches!(
            load_policy(&rejected_path),
            Err(AuthPolicyError::Invalid(
                "credentials must contain between 1 and 64 entries"
            ))
        ));
    }

    #[test]
    fn policy_rejects_non_private_file_permissions() {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("auth-policy.json");
        fs::write(&path, serde_json::to_vec(&valid_policy()).unwrap()).unwrap();

        assert!(load_policy(&path).is_err());
    }

    #[test]
    fn policy_rejects_additional_hard_links() {
        let (directory, path) = write_policy(valid_policy());
        let linked = directory.path().join("hard-linked-policy.json");
        fs::hard_link(&path, &linked).unwrap();

        assert!(load_policy(&path).is_err());
        assert!(load_policy(&linked).is_err());
    }

    #[cfg(unix)]
    #[test]
    fn policy_rejects_path_substitution_between_opened_handles() {
        let (directory, path) = write_policy(valid_policy());
        let (_replacement_directory, replacement) = write_policy(serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-2",
                "token_sha256": format!("{:x}", Sha256::digest(b"public")),
                "token_byte_length": 6,
                "scopes": ["inference"]
            }]
        }));
        let displaced = directory.path().join("displaced-policy.json");

        let error = open_private_policy_with(&path, || {
            fs::rename(&path, &displaced).unwrap();
            fs::rename(&replacement, &path).unwrap();
        })
        .unwrap_err();

        assert!(matches!(error, AuthPolicyError::Invalid(_)));
    }

    #[cfg(unix)]
    #[test]
    fn unix_policy_owner_must_match_effective_identity() {
        assert!(unix_owner_matches_effective_identity(1_000, 1_000));
        assert!(!unix_owner_matches_effective_identity(1_000, 1_001));
    }

    #[cfg(windows)]
    #[test]
    fn windows_policy_owner_must_match_process_identity() {
        assert!(windows_policy_acl::owner_matches_process_identity(
            "S-1-5-21-1000",
            "S-1-5-21-1000"
        ));
        assert!(!windows_policy_acl::owner_matches_process_identity(
            "S-1-5-18",
            "S-1-5-21-1000"
        ));
    }

    #[test]
    fn stable_policy_read_rejects_same_length_in_place_rewrite() {
        use std::io::Write;

        #[cfg(windows)]
        use std::os::windows::fs::OpenOptionsExt;

        let replacement = serde_json::json!({
            "schema_version": AUTH_POLICY_SCHEMA_VERSION,
            "credentials": [{
                "principal_id": "worker-2",
                "token_sha256": format!("{:x}", Sha256::digest(b"public")),
                "token_byte_length": 6,
                "scopes": ["inference"]
            }]
        });
        let original_bytes = serde_json::to_vec(&valid_policy()).unwrap();
        let replacement_bytes = serde_json::to_vec(&replacement).unwrap();
        assert_eq!(replacement_bytes.len(), original_bytes.len());
        let (_directory, path) = write_policy(valid_policy());
        #[cfg(unix)]
        let file = open_private_policy(&path).unwrap();
        #[cfg(windows)]
        let file = {
            use windows_sys::Win32::Storage::FileSystem::{FILE_SHARE_READ, FILE_SHARE_WRITE};

            const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
            OpenOptions::new()
                .read(true)
                .share_mode(FILE_SHARE_READ | FILE_SHARE_WRITE)
                .custom_flags(FILE_FLAG_OPEN_REPARSE_POINT)
                .open(&path)
                .unwrap()
        };
        let metadata = file.metadata().unwrap();
        let version = policy_version(&file, &metadata).unwrap();

        let error = read_stable_policy_with(&file, version, || {
            let mut writer = OpenOptions::new()
                .write(true)
                .truncate(true)
                .open(&path)
                .unwrap();
            writer.write_all(&replacement_bytes).unwrap();
            writer.sync_all().unwrap();
        })
        .unwrap_err();

        assert!(matches!(error, AuthPolicyError::Invalid(_)));
    }

    #[test]
    fn stable_policy_read_accepts_identical_consecutive_reads() {
        let expected = serde_json::to_vec(&valid_policy()).unwrap();
        let (_directory, path) = write_policy(valid_policy());
        let file = open_private_policy(&path).unwrap();
        let metadata = file.metadata().unwrap();
        let version = policy_version(&file, &metadata).unwrap();

        let actual = read_stable_policy_with(&file, version, || {}).unwrap();

        assert_eq!(actual, expected);
    }

    #[test]
    fn policy_io_diagnostic_is_bounded_and_redacted() {
        let secret_path = r"C:\secret\token-value\auth-policy.json";
        let error =
            AuthPolicyError::from(io::Error::new(io::ErrorKind::PermissionDenied, secret_path));

        let diagnostic = error.to_string();
        assert!(diagnostic.contains("kind=PermissionDenied"));
        assert!(diagnostic.contains("os_error=None"));
        assert!(!diagnostic.contains(secret_path));
        assert!(!diagnostic.contains("token-value"));
    }

    #[cfg(unix)]
    #[test]
    fn policy_rejects_symbolic_paths() {
        use std::os::unix::fs::symlink;

        let (directory, path) = write_policy(valid_policy());
        let linked = directory.path().join("linked-policy.json");
        symlink(&path, &linked).unwrap();

        assert!(load_policy(&linked).is_err());
    }

    #[cfg(windows)]
    #[test]
    fn private_policy_handle_blocks_concurrent_mutation() {
        let (_directory, path) = write_policy(valid_policy());
        let file = open_private_policy(&path).unwrap();

        let error = fs::write(&path, b"replacement").unwrap_err();
        assert_eq!(error.raw_os_error(), Some(32));
        drop(file);
        fs::write(&path, serde_json::to_vec(&valid_policy()).unwrap()).unwrap();
    }

    #[test]
    fn loopback_policy_accepts_ip_literals_and_localhost_only() {
        assert!(assert_loopback("127.0.0.1").is_ok());
        assert!(assert_loopback("::1").is_ok());
        assert!(assert_loopback("localhost").is_ok());
        assert!(resolve_loopback("127.0.0.1", 43121)
            .unwrap()
            .iter()
            .all(|address| address.ip().is_loopback()));
        assert_eq!(
            resolve_loopback("::1", 43121).unwrap()[0].to_string(),
            "[::1]:43121"
        );
        assert!(resolve_loopback("localhost", 43121)
            .unwrap()
            .iter()
            .all(|address| address.ip().is_loopback()));
        assert!(assert_loopback("example.com").is_err());
        assert!(assert_loopback("0.0.0.0").is_err());
        assert!(assert_loopback("127.0.0.1.example.com").is_err());
    }

    #[tokio::test]
    async fn valid_authentication_never_clears_or_inherits_invalid_candidate_throttling() {
        let clock = Arc::new(ManualClock::default());
        let throttle = test_throttle(clock.clone(), 8, 2);
        let invalid = invalid_key(1);
        let credentials = credential_set([Credential::owner("strong-secret").unwrap()]);
        let mut valid_headers = HeaderMap::new();
        valid_headers.insert(
            AUTHORIZATION,
            HeaderValue::from_static("Bearer strong-secret"),
        );

        assert_eq!(
            throttle.record_failure(invalid).disposition,
            AuthFailureDisposition::Unauthorized
        );
        assert!(matches!(
            throttle.record_failure(invalid).disposition,
            AuthFailureDisposition::Throttled { .. }
        ));
        assert!(throttle
            .authenticate(&valid_headers, &credentials)
            .await
            .is_ok());

        let state = throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        assert_eq!(state.sources.len(), 1);
        assert!(state.sources.contains_key(&invalid));
        let snapshot = throttle.inner.metrics.snapshot();
        assert_eq!(snapshot.authentication_failures, 2);
        assert_eq!(snapshot.authentication_throttled_requests, 1);
        assert_eq!(snapshot.authentication_tracked_sources, 1);
    }

    #[tokio::test]
    async fn valid_authentication_does_not_touch_failure_state_or_metrics() {
        let clock = Arc::new(ManualClock::default());
        let metrics = MetricsHub::default();
        let throttle = test_throttle_with_metrics(clock.clone(), 8, 5, metrics.clone());
        throttle.record_failure(invalid_key(7));
        let before = metrics.snapshot();
        clock.advance(Duration::from_secs(31));
        let mut valid_headers = HeaderMap::new();
        valid_headers.insert(
            AUTHORIZATION,
            HeaderValue::from_static("Bearer always-valid"),
        );
        assert!(throttle
            .authenticate(
                &valid_headers,
                &credential_set([Credential::owner("always-valid").unwrap()]),
            )
            .await
            .is_ok());
        assert_eq!(metrics.snapshot(), before);
        let state = throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        assert_eq!(state.sources.len(), 1);
    }

    #[test]
    fn expired_block_and_failure_window_restart_without_real_time() {
        let clock = Arc::new(ManualClock::default());
        let throttle = test_throttle(clock.clone(), 8, 2);
        let source = invalid_key(1);

        assert_eq!(
            throttle.record_failure(source).disposition,
            AuthFailureDisposition::Unauthorized
        );
        assert!(matches!(
            throttle.record_failure(source).disposition,
            AuthFailureDisposition::Throttled { .. }
        ));
        clock.advance(Duration::from_millis(4_500));
        assert_eq!(
            throttle.record_failure(source).disposition,
            AuthFailureDisposition::Throttled {
                retry_after_seconds: 6
            }
        );
        clock.advance(Duration::from_millis(5_500));
        assert_eq!(
            throttle.record_failure(source).disposition,
            AuthFailureDisposition::Unauthorized
        );
        clock.advance(Duration::from_secs(20));
        assert_eq!(
            throttle.record_failure(source).disposition,
            AuthFailureDisposition::Unauthorized
        );
    }

    #[test]
    fn source_cap_evicts_oldest_and_expiry_reclaims_capacity() {
        let clock = Arc::new(ManualClock::default());
        let throttle = test_throttle(clock.clone(), 2, 5);
        let first = invalid_key(1);
        let second = invalid_key(2);
        let third = invalid_key(3);

        assert_eq!(throttle.record_failure(first).tracked_sources, 1);
        clock.advance(Duration::from_millis(1));
        assert_eq!(throttle.record_failure(second).tracked_sources, 2);
        clock.advance(Duration::from_millis(1));
        let eviction = throttle.record_failure(third);
        assert_eq!(eviction.tracked_sources, 2);
        assert_eq!(eviction.source_evictions, 1);
        {
            let state = throttle
                .inner
                .state
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner());
            assert!(!state.sources.contains_key(&first));
            assert!(state.sources.contains_key(&second));
            assert!(state.sources.contains_key(&third));
        }

        clock.advance(Duration::from_secs(31));
        let after_expiry = throttle.record_failure(first);
        assert_eq!(after_expiry.tracked_sources, 1);
        assert_eq!(after_expiry.source_evictions, 0);
    }

    #[test]
    fn unique_invalid_churn_has_exact_eviction_metrics_and_bounded_persistent_warnings() {
        let clock = Arc::new(ManualClock::default());
        let metrics = MetricsHub::default();
        let throttle = AuthFailureThrottle::new(
            clock.clone(),
            AuthThrottleConfig {
                maximum_sources: 2,
                failure_limit: 20_000,
                failure_window: Duration::from_secs(20),
                block_duration: Duration::from_secs(10),
                entry_ttl: Duration::from_secs(30),
                security_warning_interval: Duration::from_secs(10),
            },
            metrics.clone(),
            VerifierGateConfig {
                concurrent_verifiers: 4,
                maximum_admissions: 64,
                admission_interval: Duration::ZERO,
            },
        );
        let sink = CountingWarningSink::default();
        for candidate in 0..10_000 {
            let observation = throttle.record_failure(numbered_invalid_key(candidate));
            emit_auth_security_warning_with(observation.security_warning, &sink);
        }
        assert_eq!(sink.emitted.load(Ordering::SeqCst), 1);
        let first_snapshot = metrics.snapshot();
        assert_eq!(first_snapshot.authentication_failures, 10_000);
        assert_eq!(first_snapshot.authentication_source_evictions, 9_998);
        assert_eq!(first_snapshot.authentication_tracked_sources, 2);

        clock.advance(Duration::from_secs(10));
        let aggregate = throttle.record_failure(numbered_invalid_key(10_000));
        let warning = aggregate.security_warning.unwrap();
        assert_eq!(warning.evictions_since_warning, 9_998);
        assert_eq!(warning.total_evictions, 9_999);
        emit_auth_security_warning_with(Some(warning), &sink);
        assert_eq!(sink.emitted.load(Ordering::SeqCst), 2);
        let warnings = sink
            .warnings
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        assert_eq!(warnings.len(), 2);
        assert_eq!(warnings[0].evictions_since_warning, 1);
        assert_eq!(warnings[1], warning);
        let final_snapshot = metrics.snapshot();
        assert_eq!(final_snapshot.authentication_failures, 10_001);
        assert_eq!(final_snapshot.authentication_source_evictions, 9_999);
        assert_eq!(final_snapshot.authentication_tracked_sources, 2);
    }

    #[test]
    fn concurrent_failures_share_one_atomic_source_budget() {
        let clock = Arc::new(ManualClock::default());
        let metrics = MetricsHub::default();
        let throttle = test_throttle_with_metrics(clock, 8, 4, metrics.clone());
        let barrier = Arc::new(Barrier::new(32));
        let source = invalid_key(1);
        let handles = (0..32)
            .map(|_| {
                let throttle = throttle.clone();
                let barrier = barrier.clone();
                thread::spawn(move || {
                    barrier.wait();
                    throttle.record_failure(source)
                })
            })
            .collect::<Vec<_>>();
        let observations = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .collect::<Vec<_>>();

        assert_eq!(
            observations
                .iter()
                .filter(|observation| matches!(
                    observation.disposition,
                    AuthFailureDisposition::Throttled { .. }
                ))
                .count(),
            29
        );
        assert_eq!(
            observations
                .iter()
                .filter(|observation| observation.newly_blocked)
                .count(),
            1
        );
        let snapshot = metrics.snapshot();
        assert_eq!(snapshot.authentication_failures, 32);
        assert_eq!(snapshot.authentication_throttled_requests, 29);
        assert_eq!(snapshot.authentication_tracked_sources, 1);
    }

    #[test]
    fn concurrent_distinct_sources_publish_the_exact_monotonic_cardinality() {
        let clock = Arc::new(ManualClock::default());
        let metrics = MetricsHub::default();
        let throttle = test_throttle_with_metrics(clock, 64, 100, metrics.clone());
        let barrier = Arc::new(Barrier::new(32));
        let handles = (1..=32)
            .map(|last_octet| {
                let throttle = throttle.clone();
                let barrier = barrier.clone();
                thread::spawn(move || {
                    barrier.wait();
                    throttle.record_failure(invalid_key(last_octet))
                })
            })
            .collect::<Vec<_>>();
        let mut published_cardinalities = handles
            .into_iter()
            .map(|handle| handle.join().unwrap().tracked_sources)
            .collect::<Vec<_>>();
        published_cardinalities.sort_unstable();

        assert_eq!(published_cardinalities, (1..=32).collect::<Vec<_>>());
        let snapshot = metrics.snapshot();
        assert_eq!(snapshot.authentication_failures, 32);
        assert_eq!(snapshot.authentication_tracked_sources, 32);
        assert_eq!(snapshot.authentication_source_evictions, 0);
        let state = throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        assert_eq!(state.sources.len(), 32);
    }

    #[test]
    fn limiter_samples_authoritative_time_only_after_acquiring_state_lock() {
        let clock = Arc::new(ManualClock::default());
        let metrics = MetricsHub::default();
        let throttle = test_throttle_with_metrics(clock.clone(), 8, 4, metrics.clone());
        let state_guard = throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let started = Arc::new(Barrier::new(2));
        let worker = {
            let throttle = throttle.clone();
            let started = started.clone();
            thread::spawn(move || {
                started.wait();
                throttle.record_failure(invalid_key(9))
            })
        };
        started.wait();
        thread::sleep(Duration::from_millis(10));
        assert_eq!(clock.reads.load(Ordering::SeqCst), 0);
        clock.advance(Duration::from_secs(7));
        drop(state_guard);

        assert_eq!(worker.join().unwrap().tracked_sources, 1);
        let state = throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        assert_eq!(
            state.sources[&invalid_key(9)].last_seen,
            Duration::from_secs(7)
        );
        assert_eq!(metrics.snapshot().authentication_tracked_sources, 1);
    }

    #[tokio::test]
    async fn verifier_gate_bounds_work_and_preserves_fifo_valid_admission() {
        let gate = VerifierGate::new(VerifierGateConfig {
            concurrent_verifiers: 1,
            maximum_admissions: 8,
            admission_interval: Duration::ZERO,
        });
        let held = gate.admit().await.unwrap();
        let (completed_tx, mut completed_rx) = tokio::sync::mpsc::channel(7);
        let identities = [1_u8, 2, 3, 99, 4, 5, 6];
        let mut workers = Vec::new();
        for (index, identity) in identities.into_iter().enumerate() {
            let worker_gate = gate.clone();
            let completed_tx = completed_tx.clone();
            workers.push(tokio::spawn(async move {
                let _admission = worker_gate.admit().await.unwrap();
                completed_tx.send(identity).await.unwrap();
            }));
            while gate.inner.in_system.load(Ordering::Acquire) < index + 2 {
                tokio::task::yield_now().await;
            }
            tokio::task::yield_now().await;
        }
        drop(completed_tx);
        assert_eq!(gate.inner.in_system.load(Ordering::Acquire), 8);
        assert!(gate.admit().await.is_none());
        drop(held);

        let mut completion_order = Vec::new();
        while let Some(identity) = completed_rx.recv().await {
            completion_order.push(identity);
        }
        for worker in workers {
            worker.await.unwrap();
        }
        assert_eq!(completion_order, identities);
        assert_eq!(
            completion_order.iter().position(|value| *value == 99),
            Some(3)
        );
        assert_eq!(gate.inner.in_system.load(Ordering::Acquire), 0);
    }

    #[tokio::test]
    async fn valid_fast_path_succeeds_while_sustained_invalid_work_saturates_admission() {
        let metrics = MetricsHub::default();
        let throttle = AuthFailureThrottle::new(
            Arc::new(ManualClock::default()),
            AuthThrottleConfig {
                maximum_sources: 16,
                failure_limit: 5,
                failure_window: Duration::from_secs(20),
                block_duration: Duration::from_secs(10),
                entry_ttl: Duration::from_secs(30),
                security_warning_interval: Duration::from_secs(60),
            },
            metrics.clone(),
            VerifierGateConfig {
                concurrent_verifiers: 1,
                maximum_admissions: 4,
                admission_interval: Duration::ZERO,
            },
        );
        let held = throttle.inner.verifier_gate.admit().await.unwrap();
        let credentials = credential_set([Credential::owner("strong-secret").unwrap()]);
        let mut queued = Vec::new();
        for identity in 0..3 {
            let worker_throttle = throttle.clone();
            let credentials = credentials.clone();
            queued.push(tokio::spawn(async move {
                let mut headers = HeaderMap::new();
                headers.insert(
                    AUTHORIZATION,
                    HeaderValue::from_str(&format!("Bearer queued-invalid-{identity}")).unwrap(),
                );
                worker_throttle.authenticate(&headers, &credentials).await
            }));
            while throttle
                .inner
                .verifier_gate
                .inner
                .in_system
                .load(Ordering::Acquire)
                < identity + 2
            {
                tokio::task::yield_now().await;
            }
        }
        assert_eq!(
            throttle
                .inner
                .verifier_gate
                .inner
                .in_system
                .load(Ordering::Acquire),
            4
        );

        for identity in 0..1_000 {
            let mut headers = HeaderMap::new();
            headers.insert(
                AUTHORIZATION,
                HeaderValue::from_str(&format!("Bearer flood-invalid-{identity}")).unwrap(),
            );
            assert!(matches!(
                throttle.authenticate(&headers, &credentials).await,
                Err(AuthThrottleRejection::AdmissionLimited)
            ));
            assert_eq!(
                throttle
                    .inner
                    .verifier_gate
                    .inner
                    .in_system
                    .load(Ordering::Acquire),
                4
            );
        }
        let after_overflow = metrics.snapshot();
        assert_eq!(after_overflow.authentication_failures, 1_000);
        assert_eq!(after_overflow.authentication_throttled_requests, 1_000);
        assert_eq!(
            after_overflow.authentication_admission_limited_requests,
            1_000
        );
        assert_eq!(after_overflow.authentication_tracked_sources, 0);
        assert!(throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .sources
            .is_empty());

        let mut valid_headers = HeaderMap::new();
        valid_headers.insert(
            AUTHORIZATION,
            HeaderValue::from_static("Bearer strong-secret"),
        );
        let valid = tokio::time::timeout(
            Duration::from_millis(100),
            throttle.authenticate(&valid_headers, &credentials),
        )
        .await
        .expect("valid fast path must not wait for invalid admission capacity");
        assert!(valid.is_ok());
        for _ in 1..1_000 {
            assert!(throttle
                .authenticate(&valid_headers, &credentials)
                .await
                .is_ok());
        }
        assert_eq!(metrics.snapshot(), after_overflow);
        assert!(throttle
            .inner
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .sources
            .is_empty());
        assert_eq!(
            throttle.inner.credential_lookups.load(Ordering::Relaxed),
            2_003
        );
        assert_eq!(
            throttle
                .inner
                .verifier_gate
                .inner
                .in_system
                .load(Ordering::Acquire),
            4
        );

        drop(held);
        for worker in queued {
            assert!(worker.await.unwrap().is_err());
        }
        assert_eq!(
            throttle
                .inner
                .verifier_gate
                .inner
                .in_system
                .load(Ordering::Acquire),
            0
        );
    }

    #[tokio::test]
    async fn verifier_gate_paces_costly_admissions_under_flood() {
        let gate = VerifierGate::new(VerifierGateConfig {
            concurrent_verifiers: 4,
            maximum_admissions: 8,
            admission_interval: Duration::from_millis(10),
        });
        let started = tokio::time::Instant::now();
        let mut workers = (0..4)
            .map(|_| {
                let gate = gate.clone();
                tokio::spawn(async move {
                    let _admission = gate.admit().await.unwrap();
                    tokio::time::Instant::now()
                })
            })
            .collect::<Vec<_>>();
        let mut admitted_at = Vec::new();
        for worker in workers.drain(..) {
            admitted_at.push(worker.await.unwrap());
        }
        assert_eq!(admitted_at.len(), 4);
        assert!(started.elapsed() >= Duration::from_millis(25));
        let next_admission = *gate.inner.next_admission.lock().await;
        assert!(next_admission.duration_since(started) >= Duration::from_millis(30));
        assert_eq!(gate.inner.in_system.load(Ordering::Acquire), 0);
    }

    #[test]
    fn invalid_cache_key_is_fixed_width_candidate_identity_not_claimed_peer_identity() {
        let mut first = HeaderMap::new();
        first.insert(AUTHORIZATION, HeaderValue::from_static("Bearer repeated"));
        first.insert("x-forwarded-for", HeaderValue::from_static("203.0.113.8"));
        let mut second = HeaderMap::new();
        second.insert(AUTHORIZATION, HeaderValue::from_static("Bearer repeated"));
        second.insert("x-forwarded-for", HeaderValue::from_static("198.51.100.4"));

        assert_eq!(
            InvalidCredentialKey::from_digest(bearer_digest(&first)),
            InvalidCredentialKey::from_digest(bearer_digest(&second))
        );
        assert_eq!(
            InvalidCredentialKey::from_digest(None),
            InvalidCredentialKey::MissingOrMalformed
        );
    }

    #[tokio::test]
    async fn oversized_bearer_is_never_hashed_or_indexed() {
        let throttle = test_throttle(Arc::new(ManualClock::default()), 8, 5);
        let credentials = credential_set([Credential::owner("strong-secret").unwrap()]);
        let oversized = "x".repeat(MAX_BEARER_TOKEN_BYTES + 1);
        let mut headers = HeaderMap::new();
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_str(&format!("Bearer {oversized}")).unwrap(),
        );

        assert!(bearer_digest(&headers).is_none());
        assert!(throttle.authenticate(&headers, &credentials).await.is_err());
        assert_eq!(throttle.inner.credential_lookups.load(Ordering::Relaxed), 0);
    }

    #[tokio::test]
    async fn known_blocked_candidate_rejects_before_invalid_gate() {
        let throttle = test_throttle(Arc::new(ManualClock::default()), 8, 1);
        let credentials = credential_set([Credential::owner("strong-secret").unwrap()]);
        let mut headers = HeaderMap::new();
        headers.insert(
            AUTHORIZATION,
            HeaderValue::from_static("Bearer known-invalid"),
        );

        assert!(matches!(
            throttle.authenticate(&headers, &credentials).await,
            Err(AuthThrottleRejection::Throttled { .. })
        ));
        assert_eq!(throttle.inner.credential_lookups.load(Ordering::Relaxed), 1);
        let held_admissions = (0..64)
            .map(|_| throttle.inner.verifier_gate.clone())
            .map(|gate| tokio::spawn(async move { gate.admit().await.unwrap() }))
            .collect::<Vec<_>>();
        while throttle
            .inner
            .verifier_gate
            .inner
            .in_system
            .load(Ordering::Acquire)
            < 64
        {
            tokio::task::yield_now().await;
        }

        assert!(matches!(
            throttle.authenticate(&headers, &credentials).await,
            Err(AuthThrottleRejection::Throttled { .. })
        ));
        assert_eq!(throttle.inner.credential_lookups.load(Ordering::Relaxed), 2);
        for held in held_admissions {
            drop(held.await.unwrap());
        }
    }

    #[tokio::test]
    async fn bearer_middleware_throttles_repeated_invalid_candidate_without_blocking_valid() {
        let clock = Arc::new(ManualClock::default());
        let metrics = MetricsHub::default();
        let throttle = test_throttle_with_metrics(clock, 8, 2, metrics.clone());
        let state = AuthState::with_throttle(
            credential_set([Credential::owner("strong-secret").unwrap()]),
            throttle,
        );
        let router = Router::new()
            .route("/", get(|| async { StatusCode::OK }))
            .layer(middleware::from_fn_with_state(state, require_bearer));
        let request = |token: &'static str| {
            HttpRequest::builder()
                .uri("/")
                .header(AUTHORIZATION, format!("Bearer {token}"))
                .body(Body::empty())
                .unwrap()
        };

        let first = router.clone().oneshot(request("weak")).await.unwrap();
        assert_eq!(first.status(), StatusCode::UNAUTHORIZED);
        let blocked = router.clone().oneshot(request("weak")).await.unwrap();
        assert_eq!(blocked.status(), StatusCode::TOO_MANY_REQUESTS);
        assert_eq!(blocked.headers().get("retry-after").unwrap(), "10");
        let blocked_metrics = metrics.snapshot();
        assert_eq!(blocked_metrics.authentication_failures, 2);
        assert_eq!(blocked_metrics.authentication_throttled_requests, 1);
        assert_eq!(blocked_metrics.authentication_tracked_sources, 1);

        let valid = router
            .clone()
            .oneshot(request("strong-secret"))
            .await
            .unwrap();
        assert_eq!(valid.status(), StatusCode::OK);
        let after_valid_metrics = metrics.snapshot();
        assert_eq!(after_valid_metrics.authentication_failures, 2);
        assert_eq!(after_valid_metrics.authentication_throttled_requests, 1);
        assert_eq!(after_valid_metrics.authentication_tracked_sources, 1);

        let repeated_invalid = router.clone().oneshot(request("weak")).await.unwrap();
        assert_eq!(repeated_invalid.status(), StatusCode::TOO_MANY_REQUESTS);

        let fresh_failure = router.oneshot(request("different-weak")).await.unwrap();
        assert_eq!(fresh_failure.status(), StatusCode::UNAUTHORIZED);
        let final_metrics = metrics.snapshot();
        assert_eq!(final_metrics.authentication_failures, 4);
        assert_eq!(final_metrics.authentication_throttled_requests, 2);
        assert_eq!(final_metrics.authentication_tracked_sources, 2);
    }
}
