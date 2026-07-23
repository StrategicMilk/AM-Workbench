//! Principal-isolated, quota-accounted durable session snapshots.

use std::{
    collections::{HashMap, HashSet},
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex, Weak,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use sha2::{Digest, Sha256};
use thiserror::Error;

const SNAPSHOT_MAGIC: &[u8; 8] = b"AMWSES1\0";
const HASH_BYTES: usize = 32;
const DIGEST_BYTES: usize = 32;
const HEADER_BYTES: usize = SNAPSHOT_MAGIC.len() + 8 + 8 + HASH_BYTES + HASH_BYTES + DIGEST_BYTES;
const SNAPSHOT_EXTENSION: &str = "amws";
const PRINCIPAL_PREFIX: &str = "p-";
const MODEL_PREFIX: &str = "m-";
const HASH_HEX_BYTES: usize = HASH_BYTES * 2;
const MAX_ID_BYTES: usize = 128;
const MAX_SNAPSHOT_BYTES: u64 = 512 * 1024 * 1024;
const MAX_SESSIONS_PER_PRINCIPAL: u64 = 128;
const MAX_BYTES_PER_PRINCIPAL: u64 = 4 * 1024 * 1024 * 1024;
const MAX_SESSIONS_PER_HOST: u64 = 1_024;
const MAX_BYTES_PER_HOST: u64 = 8 * 1024 * 1024 * 1024;
const DEFAULT_SCAN_ENTRY_LIMIT: u64 = 8_192;
const RETENTION: Duration = Duration::from_secs(30 * 24 * 60 * 60);
const RETENTION_SWEEP_INTERVAL: Duration = Duration::from_secs(24 * 60 * 60);
const RETENTION_SWEEP_RETRY_INTERVAL: Duration = Duration::from_secs(5 * 60);
static TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

/// One authority for all durable-session capacity and retention limits.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SessionStoreLimits {
    pub max_snapshot_bytes: u64,
    pub max_sessions_per_principal: u64,
    pub max_bytes_per_principal: u64,
    pub max_sessions_per_host: u64,
    pub max_bytes_per_host: u64,
    pub scan_entry_limit: u64,
    pub retention: Duration,
}

impl Default for SessionStoreLimits {
    fn default() -> Self {
        Self {
            max_snapshot_bytes: MAX_SNAPSHOT_BYTES,
            max_sessions_per_principal: MAX_SESSIONS_PER_PRINCIPAL,
            max_bytes_per_principal: MAX_BYTES_PER_PRINCIPAL,
            max_sessions_per_host: MAX_SESSIONS_PER_HOST,
            max_bytes_per_host: MAX_BYTES_PER_HOST,
            scan_entry_limit: DEFAULT_SCAN_ENTRY_LIMIT,
            retention: RETENTION,
        }
    }
}

impl SessionStoreLimits {
    fn validate(self) -> Result<Self, SessionStoreError> {
        let header = u64::try_from(HEADER_BYTES).expect("session header fits u64");
        if self.max_snapshot_bytes <= header
            || self.max_sessions_per_principal == 0
            || self.max_bytes_per_principal < self.max_snapshot_bytes
            || self.max_sessions_per_host < self.max_sessions_per_principal
            || self.max_bytes_per_host < self.max_bytes_per_principal
            || self.scan_entry_limit < self.max_sessions_per_host
            || self.retention.is_zero()
        {
            return Err(SessionStoreError::InvalidInput(
                "session store limits are internally inconsistent".to_owned(),
            ));
        }
        Ok(self)
    }
}

/// A stable tenant/model/session key. Raw tenant identifiers are never used as paths.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub struct SessionKey {
    principal_hash: [u8; HASH_BYTES],
    model_hash: [u8; HASH_BYTES],
    session_id: String,
}

impl SessionKey {
    /// Validates and hashes a principal/model/session identity for storage.
    pub fn new(
        principal_id: &str,
        model_fingerprint: [u8; HASH_BYTES],
        session_id: &str,
    ) -> Result<Self, SessionStoreError> {
        validate_id("principal_id", principal_id)?;
        validate_id("session_id", session_id)?;
        if model_fingerprint == [0; HASH_BYTES] {
            return Err(SessionStoreError::InvalidInput(
                "model_fingerprint must not be all zeroes".to_owned(),
            ));
        }
        Ok(Self {
            principal_hash: Sha256::digest(principal_id.as_bytes()).into(),
            model_hash: Sha256::digest(model_fingerprint).into(),
            session_id: session_id.to_owned(),
        })
    }

    pub fn session_id(&self) -> &str {
        &self.session_id
    }

    fn namespace(&self) -> NamespaceKey {
        NamespaceKey {
            principal_hash: self.principal_hash,
            model_hash: self.model_hash,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct NamespaceKey {
    principal_hash: [u8; HASH_BYTES],
    model_hash: [u8; HASH_BYTES],
}

/// Why new persistence writes are currently disabled.
#[derive(Clone, Debug, Eq, Hash, PartialEq)]
pub enum PersistenceDisableReason {
    Privacy(String),
    Malformed(String),
    OverQuota(String),
    ScanLimit,
}

impl PersistenceDisableReason {
    fn blocks_reads(&self) -> bool {
        matches!(self, Self::Privacy(_))
    }
}

/// Committed and in-flight resource use for one quota scope.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SessionUsage {
    pub sessions: u64,
    pub bytes: u64,
    pub reserved_sessions: u64,
    pub reserved_bytes: u64,
}

/// Current process-wide persistence posture and capacity accounting.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SessionQuotaStatus {
    pub saves_enabled: bool,
    pub host: SessionUsage,
    pub principal: Option<SessionUsage>,
    pub disable_reasons: Vec<PersistenceDisableReason>,
}

/// Metadata returned without loading a potentially large snapshot.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StoredSession {
    pub session_id: String,
    pub stored_bytes: u64,
    pub updated_at: Option<SystemTime>,
    pub corrupt: bool,
}

/// Result of deleting expired snapshots. Unexpired data is never quota-evicted.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct SweepReceipt {
    pub sessions_deleted: u64,
    pub bytes_deleted: u64,
}

#[derive(Debug, Error)]
pub enum SessionStoreError {
    #[error("invalid session store input: {0}")]
    InvalidInput(String),
    #[error("session snapshot is unknown")]
    Unknown,
    #[error("session persistence quota is exhausted for {scope}: {limit}")]
    Quota {
        scope: &'static str,
        limit: &'static str,
    },
    #[error("session persistence is disabled: {0}")]
    Disabled(String),
    #[error("session snapshot is corrupt: {0}")]
    Corrupt(String),
    #[error("session snapshot already has an in-flight write")]
    Conflict,
    #[error("session store I/O failed during {operation} for {path}: {source}")]
    Io {
        operation: &'static str,
        path: PathBuf,
        #[source]
        source: io::Error,
    },
}

#[derive(Clone, Copy, Debug, Default)]
struct Usage {
    sessions: u64,
    bytes: u64,
    reserved_sessions: u64,
    reserved_bytes: u64,
}

impl From<Usage> for SessionUsage {
    fn from(value: Usage) -> Self {
        Self {
            sessions: value.sessions,
            bytes: value.bytes,
            reserved_sessions: value.reserved_sessions,
            reserved_bytes: value.reserved_bytes,
        }
    }
}

#[derive(Clone, Debug)]
struct EntryState {
    stored_bytes: u64,
    updated_at: Option<SystemTime>,
    corrupt: bool,
}

#[derive(Debug)]
struct ReservationState {
    key: SessionKey,
    new_stored_bytes: u64,
    charged_sessions: u64,
    charged_bytes: u64,
}

#[derive(Debug, Default)]
struct Accounting {
    entries: HashMap<SessionKey, EntryState>,
    principals: HashMap<[u8; HASH_BYTES], Usage>,
    host: Usage,
    reservations: HashMap<u64, ReservationState>,
    reserved_keys: HashSet<SessionKey>,
    disable_reasons: Vec<PersistenceDisableReason>,
}

impl Accounting {
    fn add_disable_reason(&mut self, reason: PersistenceDisableReason) {
        if !self.disable_reasons.contains(&reason) {
            self.disable_reasons.push(reason);
        }
    }

    fn read_blocked_reason(&self) -> Option<&PersistenceDisableReason> {
        self.disable_reasons
            .iter()
            .find(|reason| reason.blocks_reads())
    }

    fn add_entry(&mut self, key: SessionKey, entry: EntryState) {
        let usage = self.principals.entry(key.principal_hash).or_default();
        usage.sessions = usage.sessions.saturating_add(1);
        usage.bytes = usage.bytes.saturating_add(entry.stored_bytes);
        self.host.sessions = self.host.sessions.saturating_add(1);
        self.host.bytes = self.host.bytes.saturating_add(entry.stored_bytes);
        self.entries.insert(key, entry);
    }

    fn remove_entry(&mut self, key: &SessionKey) -> Option<EntryState> {
        let entry = self.entries.remove(key)?;
        if let Some(usage) = self.principals.get_mut(&key.principal_hash) {
            usage.sessions = usage.sessions.saturating_sub(1);
            usage.bytes = usage.bytes.saturating_sub(entry.stored_bytes);
        }
        self.host.sessions = self.host.sessions.saturating_sub(1);
        self.host.bytes = self.host.bytes.saturating_sub(entry.stored_bytes);
        Some(entry)
    }
}

#[derive(Debug)]
struct StoreInner {
    root: PathBuf,
    limits: SessionStoreLimits,
    accounting: Mutex<Accounting>,
    next_reservation: AtomicU64,
    next_retention_sweep: AtomicU64,
}

/// Process-wide durable snapshot store. The runtime should own one `Arc<SessionStore>`.
#[derive(Clone, Debug)]
pub struct SessionStore {
    inner: Arc<StoreInner>,
}

/// A quota reservation consumed by [`SessionStore::write`]. Dropping it rolls back capacity.
#[derive(Debug)]
pub struct SessionWriteReservation {
    store: Weak<StoreInner>,
    id: u64,
    payload_bytes: u64,
    active: bool,
}

impl Drop for SessionWriteReservation {
    fn drop(&mut self) {
        if !self.active {
            return;
        }
        let Some(store) = self.store.upgrade() else {
            return;
        };
        let mut accounting = lock_accounting(&store.accounting);
        release_reservation(&mut accounting, self.id);
    }
}

impl SessionStore {
    /// Opens a store, performs one bounded accounting scan, and applies privacy controls.
    pub fn open(root: impl Into<PathBuf>) -> Result<Self, SessionStoreError> {
        Self::open_with_limits(root, SessionStoreLimits::default())
    }

    /// Opens a store with explicit limits, primarily for bounded deployments and tests.
    pub fn open_with_limits(
        root: impl Into<PathBuf>,
        limits: SessionStoreLimits,
    ) -> Result<Self, SessionStoreError> {
        let limits = limits.validate()?;
        let root = explicit_root(root.into())?;
        let inner = Arc::new(StoreInner {
            root,
            limits,
            accounting: Mutex::new(Accounting::default()),
            next_reservation: AtomicU64::new(1),
            next_retention_sweep: AtomicU64::new(u64::MAX),
        });
        let store = Self { inner };
        store.reconcile()?;
        store.run_retention_sweep(SystemTime::now(), "startup")?;
        store.schedule_retention_sweep(SystemTime::now(), RETENTION_SWEEP_INTERVAL);
        Ok(store)
    }

    /// Reserves count and byte capacity before a caller materializes or copies a snapshot.
    pub fn reserve(
        &self,
        key: &SessionKey,
        payload_bytes: u64,
    ) -> Result<SessionWriteReservation, SessionStoreError> {
        self.sweep_expired_if_due(SystemTime::now());
        let stored_bytes = encoded_size(payload_bytes, self.inner.limits.max_snapshot_bytes)?;
        let mut accounting = lock_accounting(&self.inner.accounting);
        if !accounting.disable_reasons.is_empty() {
            return Err(SessionStoreError::Disabled(format_disable_reasons(
                &accounting.disable_reasons,
            )));
        }
        if accounting.reserved_keys.contains(key) {
            return Err(SessionStoreError::Conflict);
        }
        let old_bytes = accounting
            .entries
            .get(key)
            .map_or(0, |entry| entry.stored_bytes);
        let charged_sessions = u64::from(!accounting.entries.contains_key(key));
        let charged_bytes = stored_bytes.saturating_sub(old_bytes);
        let principal = accounting
            .principals
            .get(&key.principal_hash)
            .copied()
            .unwrap_or_default();
        check_usage(
            principal,
            charged_sessions,
            charged_bytes,
            self.inner.limits.max_sessions_per_principal,
            self.inner.limits.max_bytes_per_principal,
            "principal",
        )?;
        check_usage(
            accounting.host,
            charged_sessions,
            charged_bytes,
            self.inner.limits.max_sessions_per_host,
            self.inner.limits.max_bytes_per_host,
            "host",
        )?;
        let id = self.inner.next_reservation.fetch_add(1, Ordering::Relaxed);
        let principal = accounting.principals.entry(key.principal_hash).or_default();
        principal.reserved_sessions = principal.reserved_sessions.saturating_add(charged_sessions);
        principal.reserved_bytes = principal.reserved_bytes.saturating_add(charged_bytes);
        accounting.host.reserved_sessions = accounting
            .host
            .reserved_sessions
            .saturating_add(charged_sessions);
        accounting.host.reserved_bytes =
            accounting.host.reserved_bytes.saturating_add(charged_bytes);
        accounting.reserved_keys.insert(key.clone());
        accounting.reservations.insert(
            id,
            ReservationState {
                key: key.clone(),
                new_stored_bytes: stored_bytes,
                charged_sessions,
                charged_bytes,
            },
        );
        Ok(SessionWriteReservation {
            store: Arc::downgrade(&self.inner),
            id,
            payload_bytes,
            active: true,
        })
    }

    /// Atomically installs a reserved snapshot and commits its accounting delta.
    pub fn write(
        &self,
        mut reservation: SessionWriteReservation,
        payload: &[u8],
    ) -> Result<StoredSession, SessionStoreError> {
        self.write_reserved_with(&mut reservation, payload, || Ok(()))
    }

    fn write_reserved_with(
        &self,
        reservation: &mut SessionWriteReservation,
        payload: &[u8],
        before_replace: impl FnOnce() -> io::Result<()>,
    ) -> Result<StoredSession, SessionStoreError> {
        self.write_reserved_with_verifier(reservation, payload, before_replace, verify_private_file)
    }

    fn write_reserved_with_verifier(
        &self,
        reservation: &mut SessionWriteReservation,
        payload: &[u8],
        before_replace: impl FnOnce() -> io::Result<()>,
        verify_installed: impl FnOnce(&Path) -> io::Result<()>,
    ) -> Result<StoredSession, SessionStoreError> {
        if reservation.payload_bytes != payload.len() as u64 {
            return Err(SessionStoreError::InvalidInput(
                "reserved payload length does not match write length".to_owned(),
            ));
        }
        let Some(owner) = reservation.store.upgrade() else {
            return Err(SessionStoreError::InvalidInput(
                "reservation owner no longer exists".to_owned(),
            ));
        };
        if !Arc::ptr_eq(&owner, &self.inner) {
            return Err(SessionStoreError::InvalidInput(
                "reservation belongs to a different session store".to_owned(),
            ));
        }
        let mut accounting = lock_accounting(&self.inner.accounting);
        let state = accounting
            .reservations
            .get(&reservation.id)
            .ok_or_else(|| {
                SessionStoreError::InvalidInput("reservation is no longer active".to_owned())
            })?;
        let key = state.key.clone();
        let expected_bytes = state.new_stored_bytes;
        let prior = accounting.entries.get(&key).cloned();
        if let Err(error) = preflight_target(&self.inner.root, &key, prior.as_ref()) {
            if let SessionStoreError::Corrupt(message) = &error {
                accounting.add_disable_reason(PersistenceDisableReason::Malformed(message.clone()));
            }
            return Err(error);
        }
        let now = SystemTime::now();
        let encoded = encode_snapshot(&key, now, payload)?;
        debug_assert_eq!(encoded.len() as u64, expected_bytes);
        let path = snapshot_path(&self.inner.root, &key);
        let result = write_atomic_with_verifier(
            &self.inner.root,
            &path,
            &encoded,
            before_replace,
            verify_installed,
        );
        let installed = match result {
            Ok(()) => true,
            Err(failure) => {
                if failure.privacy_failure {
                    accounting.add_disable_reason(PersistenceDisableReason::Privacy(format!(
                        "cannot verify installed snapshot {}: {}",
                        path.display(),
                        failure.source
                    )));
                    release_reservation(&mut accounting, reservation.id);
                    reservation.active = false;
                } else if failure.installed {
                    commit_reservation(
                        &mut accounting,
                        reservation.id,
                        EntryState {
                            stored_bytes: expected_bytes,
                            updated_at: Some(now),
                            corrupt: false,
                        },
                    );
                    reservation.active = false;
                } else if matches!(
                    failure.source.kind(),
                    io::ErrorKind::PermissionDenied | io::ErrorKind::Unsupported
                ) {
                    accounting.add_disable_reason(PersistenceDisableReason::Privacy(format!(
                        "cannot secure snapshot {}: {}",
                        path.display(),
                        failure.source
                    )));
                }
                return Err(SessionStoreError::Io {
                    operation: if failure.privacy_failure {
                        "verify replacement privacy"
                    } else if failure.installed {
                        "sync replacement"
                    } else {
                        "atomic write"
                    },
                    path,
                    source: failure.source,
                });
            }
        };
        debug_assert!(installed);
        commit_reservation(
            &mut accounting,
            reservation.id,
            EntryState {
                stored_bytes: expected_bytes,
                updated_at: Some(now),
                corrupt: false,
            },
        );
        reservation.active = false;
        Ok(StoredSession {
            session_id: key.session_id,
            stored_bytes: expected_bytes,
            updated_at: Some(now),
            corrupt: false,
        })
    }

    /// Reads and authenticates one snapshot using an explicit `MAX + 1` bound.
    pub fn read(&self, key: &SessionKey) -> Result<Vec<u8>, SessionStoreError> {
        self.sweep_expired_if_due(SystemTime::now());
        let mut accounting = lock_accounting(&self.inner.accounting);
        if let Some(reason) = accounting.read_blocked_reason() {
            return Err(SessionStoreError::Disabled(format!("{reason:?}")));
        }
        if accounting.reserved_keys.contains(key) {
            return Err(SessionStoreError::Conflict);
        }
        let path = snapshot_path(&self.inner.root, key);
        let encoded = match read_bounded_nofollow(&path, self.inner.limits.max_snapshot_bytes) {
            Ok(encoded) => encoded,
            Err(source)
                if matches!(
                    source.kind(),
                    io::ErrorKind::PermissionDenied | io::ErrorKind::Unsupported
                ) =>
            {
                let reason = PersistenceDisableReason::Privacy(format!(
                    "snapshot privacy verification failed for {}: {source}",
                    path.display()
                ));
                accounting.add_disable_reason(reason.clone());
                return Err(SessionStoreError::Disabled(format!("{reason:?}")));
            }
            Err(source) => return Err(map_read_error(&path, source)),
        };
        let decoded = decode_snapshot(key, &encoded).map_err(|message| {
            if let Some(entry) = accounting.entries.get_mut(key) {
                entry.corrupt = true;
            }
            accounting.add_disable_reason(PersistenceDisableReason::Malformed(message.clone()));
            SessionStoreError::Corrupt(message)
        })?;
        Ok(decoded)
    }

    /// Lists one principal/model namespace without loading snapshot payloads.
    pub fn list(
        &self,
        principal_id: &str,
        model_fingerprint: [u8; HASH_BYTES],
    ) -> Result<Vec<StoredSession>, SessionStoreError> {
        self.sweep_expired_if_due(SystemTime::now());
        let namespace = namespace_key(principal_id, model_fingerprint)?;
        let accounting = lock_accounting(&self.inner.accounting);
        let mut result = accounting
            .entries
            .iter()
            .filter(|(key, _)| key.namespace() == namespace)
            .map(|(key, entry)| StoredSession {
                session_id: key.session_id.clone(),
                stored_bytes: entry.stored_bytes,
                updated_at: entry.updated_at,
                corrupt: entry.corrupt,
            })
            .collect::<Vec<_>>();
        result.sort_by(|left, right| left.session_id.cmp(&right.session_id));
        Ok(result)
    }

    /// Deletes a snapshot under the accounting lock, including a corrupt snapshot.
    pub fn delete(&self, key: &SessionKey) -> Result<(), SessionStoreError> {
        self.sweep_expired_if_due(SystemTime::now());
        let mut accounting = lock_accounting(&self.inner.accounting);
        if accounting.reserved_keys.contains(key) {
            return Err(SessionStoreError::Conflict);
        }
        let path = snapshot_path(&self.inner.root, key);
        let metadata = secure_file_metadata(&path).map_err(|source| {
            if source.kind() == io::ErrorKind::NotFound {
                SessionStoreError::Unknown
            } else {
                map_read_error(&path, source)
            }
        })?;
        fs::remove_file(&path).map_err(|source| SessionStoreError::Io {
            operation: "delete",
            path: path.clone(),
            source,
        })?;
        sync_parent(&path).map_err(|source| SessionStoreError::Io {
            operation: "sync deletion",
            path: path.clone(),
            source,
        })?;
        if accounting.remove_entry(key).is_none() {
            accounting.add_disable_reason(PersistenceDisableReason::Malformed(format!(
                "deleted unaccounted snapshot {} ({} bytes); reconcile required",
                path.display(),
                metadata.len()
            )));
        }
        Ok(())
    }

    /// Deletes only snapshots older than the configured retention period.
    pub fn sweep_expired(&self, now: SystemTime) -> Result<SweepReceipt, SessionStoreError> {
        self.run_retention_sweep(now, "manual")
    }

    fn run_retention_sweep(
        &self,
        now: SystemTime,
        trigger: &'static str,
    ) -> Result<SweepReceipt, SessionStoreError> {
        let cutoff = now
            .checked_sub(self.inner.limits.retention)
            .unwrap_or(UNIX_EPOCH);
        let result = self.sweep_expired_unobserved(now);
        match &result {
            Ok(receipt) => tracing::info!(
                trigger,
                retention_seconds = self.inner.limits.retention.as_secs(),
                cutoff_unix_seconds = unix_seconds_saturating(cutoff),
                sessions_deleted = receipt.sessions_deleted,
                bytes_deleted = receipt.bytes_deleted,
                "session retention sweep receipt"
            ),
            Err(error) => tracing::error!(
                trigger,
                retention_seconds = self.inner.limits.retention.as_secs(),
                cutoff_unix_seconds = unix_seconds_saturating(cutoff),
                error = %error,
                "session retention sweep failed; expected expired snapshots to be removable; inspect session directory permissions and retry"
            ),
        }
        result
    }

    fn sweep_expired_unobserved(&self, now: SystemTime) -> Result<SweepReceipt, SessionStoreError> {
        let cutoff = now
            .checked_sub(self.inner.limits.retention)
            .unwrap_or(UNIX_EPOCH);
        let mut accounting = lock_accounting(&self.inner.accounting);
        if !accounting.reservations.is_empty() {
            return Err(SessionStoreError::Conflict);
        }
        let expired = accounting
            .entries
            .iter()
            .filter_map(|(key, entry)| {
                entry
                    .updated_at
                    .filter(|updated| *updated < cutoff)
                    .map(|_| (key.clone(), entry.stored_bytes))
            })
            .collect::<Vec<_>>();
        let mut receipt = SweepReceipt::default();
        for (key, bytes) in expired {
            let path = snapshot_path(&self.inner.root, &key);
            secure_file_metadata(&path).map_err(|source| map_read_error(&path, source))?;
            fs::remove_file(&path).map_err(|source| SessionStoreError::Io {
                operation: "sweep expired snapshot",
                path: path.clone(),
                source,
            })?;
            sync_parent(&path).map_err(|source| SessionStoreError::Io {
                operation: "sync expiration sweep",
                path: path.clone(),
                source,
            })?;
            accounting.remove_entry(&key);
            receipt.sessions_deleted = receipt.sessions_deleted.saturating_add(1);
            receipt.bytes_deleted = receipt.bytes_deleted.saturating_add(bytes);
        }
        Ok(receipt)
    }

    fn sweep_expired_if_due(&self, now: SystemTime) {
        let now_seconds = unix_seconds_saturating(now);
        let scheduled = self.inner.next_retention_sweep.load(Ordering::Acquire);
        if now_seconds < scheduled {
            return;
        }
        let next = now_seconds.saturating_add(RETENTION_SWEEP_INTERVAL.as_secs());
        if self
            .inner
            .next_retention_sweep
            .compare_exchange(scheduled, next, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return;
        }
        if self.run_retention_sweep(now, "periodic").is_err() {
            self.schedule_retention_sweep(now, RETENTION_SWEEP_RETRY_INTERVAL);
        }
    }

    fn schedule_retention_sweep(&self, now: SystemTime, interval: Duration) {
        self.inner.next_retention_sweep.store(
            unix_seconds_saturating(now).saturating_add(interval.as_secs()),
            Ordering::Release,
        );
    }

    /// Rebuilds committed accounting from one bounded, non-traversing disk scan.
    pub fn reconcile(&self) -> Result<SessionQuotaStatus, SessionStoreError> {
        let mut replacement = Accounting::default();
        match scan_store(&self.inner.root, self.inner.limits, &mut replacement) {
            Ok(()) => {}
            Err(ScanFailure::Privacy(message)) => {
                replacement.add_disable_reason(PersistenceDisableReason::Privacy(message));
            }
            Err(ScanFailure::Io { path, source }) => {
                return Err(SessionStoreError::Io {
                    operation: "reconcile",
                    path,
                    source,
                });
            }
        }
        mark_over_quota(&mut replacement, self.inner.limits);
        let mut accounting = lock_accounting(&self.inner.accounting);
        if !accounting.reservations.is_empty() {
            return Err(SessionStoreError::Conflict);
        }
        *accounting = replacement;
        Ok(status_for(&accounting, None))
    }

    /// Returns host and optional principal quota state without touching disk.
    pub fn quota_status(
        &self,
        principal_id: Option<&str>,
    ) -> Result<SessionQuotaStatus, SessionStoreError> {
        self.sweep_expired_if_due(SystemTime::now());
        let principal_hash = principal_id
            .map(|id| {
                validate_id("principal_id", id)?;
                Ok::<_, SessionStoreError>(Sha256::digest(id.as_bytes()).into())
            })
            .transpose()?;
        let accounting = lock_accounting(&self.inner.accounting);
        Ok(status_for(&accounting, principal_hash))
    }
}

fn lock_accounting(mutex: &Mutex<Accounting>) -> std::sync::MutexGuard<'_, Accounting> {
    mutex
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

fn unix_seconds_saturating(time: SystemTime) -> u64 {
    time.duration_since(UNIX_EPOCH)
        .map_or(0, |duration| duration.as_secs())
}

fn validate_id(field: &str, value: &str) -> Result<(), SessionStoreError> {
    if value.is_empty()
        || value.len() > MAX_ID_BYTES
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'-'))
    {
        return Err(SessionStoreError::InvalidInput(format!(
            "{field} must be 1-{MAX_ID_BYTES} ASCII alphanumeric, underscore, or hyphen bytes"
        )));
    }
    Ok(())
}

fn namespace_key(
    principal_id: &str,
    model_fingerprint: [u8; HASH_BYTES],
) -> Result<NamespaceKey, SessionStoreError> {
    let sentinel = SessionKey::new(principal_id, model_fingerprint, "namespace")?;
    Ok(sentinel.namespace())
}

fn explicit_root(path: PathBuf) -> Result<PathBuf, SessionStoreError> {
    if path.as_os_str().is_empty() {
        return Err(SessionStoreError::InvalidInput(
            "session store root must not be empty".to_owned(),
        ));
    }
    if path.is_relative() {
        return Err(SessionStoreError::InvalidInput(
            "session store root must be absolute; resolve it against the engine config directory"
                .to_owned(),
        ));
    }
    Ok(path)
}

fn encoded_size(payload_bytes: u64, max_bytes: u64) -> Result<u64, SessionStoreError> {
    let stored = payload_bytes
        .checked_add(u64::try_from(HEADER_BYTES).expect("session header fits u64"))
        .ok_or_else(|| SessionStoreError::InvalidInput("snapshot size overflow".to_owned()))?;
    if stored > max_bytes {
        return Err(SessionStoreError::Quota {
            scope: "snapshot",
            limit: "bytes",
        });
    }
    Ok(stored)
}

fn check_usage(
    usage: Usage,
    sessions: u64,
    bytes: u64,
    max_sessions: u64,
    max_bytes: u64,
    scope: &'static str,
) -> Result<(), SessionStoreError> {
    if usage
        .sessions
        .saturating_add(usage.reserved_sessions)
        .saturating_add(sessions)
        > max_sessions
    {
        return Err(SessionStoreError::Quota {
            scope,
            limit: "session count",
        });
    }
    if usage
        .bytes
        .saturating_add(usage.reserved_bytes)
        .saturating_add(bytes)
        > max_bytes
    {
        return Err(SessionStoreError::Quota {
            scope,
            limit: "stored bytes",
        });
    }
    Ok(())
}

fn release_reservation(accounting: &mut Accounting, id: u64) {
    let Some(reservation) = accounting.reservations.remove(&id) else {
        return;
    };
    accounting.reserved_keys.remove(&reservation.key);
    if let Some(usage) = accounting
        .principals
        .get_mut(&reservation.key.principal_hash)
    {
        usage.reserved_sessions = usage
            .reserved_sessions
            .saturating_sub(reservation.charged_sessions);
        usage.reserved_bytes = usage
            .reserved_bytes
            .saturating_sub(reservation.charged_bytes);
    }
    accounting.host.reserved_sessions = accounting
        .host
        .reserved_sessions
        .saturating_sub(reservation.charged_sessions);
    accounting.host.reserved_bytes = accounting
        .host
        .reserved_bytes
        .saturating_sub(reservation.charged_bytes);
}

fn commit_reservation(accounting: &mut Accounting, id: u64, entry: EntryState) {
    let reservation = accounting
        .reservations
        .remove(&id)
        .expect("an installed snapshot retains its quota reservation");
    accounting.reserved_keys.remove(&reservation.key);
    if let Some(usage) = accounting
        .principals
        .get_mut(&reservation.key.principal_hash)
    {
        usage.reserved_sessions = usage
            .reserved_sessions
            .saturating_sub(reservation.charged_sessions);
        usage.reserved_bytes = usage
            .reserved_bytes
            .saturating_sub(reservation.charged_bytes);
    }
    accounting.host.reserved_sessions = accounting
        .host
        .reserved_sessions
        .saturating_sub(reservation.charged_sessions);
    accounting.host.reserved_bytes = accounting
        .host
        .reserved_bytes
        .saturating_sub(reservation.charged_bytes);
    accounting.remove_entry(&reservation.key);
    accounting.add_entry(reservation.key, entry);
}

fn status_for(
    accounting: &Accounting,
    principal_hash: Option<[u8; HASH_BYTES]>,
) -> SessionQuotaStatus {
    SessionQuotaStatus {
        saves_enabled: accounting.disable_reasons.is_empty(),
        host: accounting.host.into(),
        principal: principal_hash.map(|hash| {
            accounting
                .principals
                .get(&hash)
                .copied()
                .unwrap_or_default()
                .into()
        }),
        disable_reasons: accounting.disable_reasons.clone(),
    }
}

fn format_disable_reasons(reasons: &[PersistenceDisableReason]) -> String {
    reasons
        .iter()
        .map(|reason| format!("{reason:?}"))
        .collect::<Vec<_>>()
        .join("; ")
}

fn principal_dir(root: &Path, principal_hash: &[u8; HASH_BYTES]) -> PathBuf {
    root.join(format!("{PRINCIPAL_PREFIX}{}", encode_hex(principal_hash)))
}

fn model_dir(root: &Path, key: &SessionKey) -> PathBuf {
    principal_dir(root, &key.principal_hash)
        .join(format!("{MODEL_PREFIX}{}", encode_hex(&key.model_hash)))
}

fn snapshot_path(root: &Path, key: &SessionKey) -> PathBuf {
    model_dir(root, key).join(format!("{}.{}", key.session_id, SNAPSHOT_EXTENSION))
}

fn encode_hex(bytes: &[u8]) -> String {
    const HEX: &[u8; 16] = b"0123456789abcdef";
    let mut output = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        output.push(char::from(HEX[usize::from(byte >> 4)]));
        output.push(char::from(HEX[usize::from(byte & 0x0f)]));
    }
    output
}

fn decode_hash_namespace(name: &str, prefix: &str) -> Option<[u8; HASH_BYTES]> {
    let hex = name.strip_prefix(prefix)?;
    if hex.len() != HASH_HEX_BYTES || !hex.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return None;
    }
    let mut output = [0u8; HASH_BYTES];
    for (index, slot) in output.iter_mut().enumerate() {
        let offset = index * 2;
        *slot = u8::from_str_radix(&hex[offset..offset + 2], 16).ok()?;
    }
    Some(output)
}

fn encode_snapshot(
    key: &SessionKey,
    updated_at: SystemTime,
    payload: &[u8],
) -> Result<Vec<u8>, SessionStoreError> {
    let updated = updated_at
        .duration_since(UNIX_EPOCH)
        .map_err(|_| SessionStoreError::InvalidInput("system time precedes epoch".to_owned()))?
        .as_secs();
    let payload_len = payload.len() as u64;
    let mut prefix = Vec::with_capacity(HEADER_BYTES - DIGEST_BYTES);
    prefix.extend_from_slice(SNAPSHOT_MAGIC);
    prefix.extend_from_slice(&updated.to_le_bytes());
    prefix.extend_from_slice(&payload_len.to_le_bytes());
    prefix.extend_from_slice(&key.principal_hash);
    prefix.extend_from_slice(&key.model_hash);
    let mut hasher = Sha256::new();
    hasher.update(&prefix);
    hasher.update(payload);
    let digest = hasher.finalize();
    let mut encoded = Vec::with_capacity(HEADER_BYTES + payload.len());
    encoded.extend_from_slice(&prefix);
    encoded.extend_from_slice(&digest);
    encoded.extend_from_slice(payload);
    Ok(encoded)
}

fn decode_snapshot(key: &SessionKey, encoded: &[u8]) -> Result<Vec<u8>, String> {
    let header = parse_header(encoded, key.principal_hash, key.model_hash)?;
    let expected_len = HEADER_BYTES
        .checked_add(usize::try_from(header.payload_bytes).map_err(|_| "payload size overflow")?)
        .ok_or("snapshot size overflow")?;
    if encoded.len() != expected_len {
        return Err("snapshot length does not match its header".to_owned());
    }
    let mut hasher = Sha256::new();
    hasher.update(&encoded[..HEADER_BYTES - DIGEST_BYTES]);
    hasher.update(&encoded[HEADER_BYTES..]);
    if hasher.finalize().as_slice() != &encoded[HEADER_BYTES - DIGEST_BYTES..HEADER_BYTES] {
        return Err("snapshot digest mismatch".to_owned());
    }
    Ok(encoded[HEADER_BYTES..].to_vec())
}

#[derive(Clone, Copy, Debug)]
struct ParsedHeader {
    updated_at: SystemTime,
    payload_bytes: u64,
}

fn parse_header(
    header: &[u8],
    principal_hash: [u8; HASH_BYTES],
    model_hash: [u8; HASH_BYTES],
) -> Result<ParsedHeader, String> {
    if header.len() < HEADER_BYTES || &header[..SNAPSHOT_MAGIC.len()] != SNAPSHOT_MAGIC {
        return Err("snapshot header or magic is invalid".to_owned());
    }
    let mut offset = SNAPSHOT_MAGIC.len();
    let updated = read_u64(header, &mut offset)?;
    let payload_bytes = read_u64(header, &mut offset)?;
    if header.get(offset..offset + HASH_BYTES) != Some(principal_hash.as_slice()) {
        return Err("snapshot principal namespace mismatch".to_owned());
    }
    offset += HASH_BYTES;
    if header.get(offset..offset + HASH_BYTES) != Some(model_hash.as_slice()) {
        return Err("snapshot model namespace mismatch".to_owned());
    }
    Ok(ParsedHeader {
        updated_at: UNIX_EPOCH
            .checked_add(Duration::from_secs(updated))
            .ok_or_else(|| "snapshot timestamp exceeds platform range".to_owned())?,
        payload_bytes,
    })
}

fn read_u64(payload: &[u8], offset: &mut usize) -> Result<u64, String> {
    let bytes: [u8; 8] = payload
        .get(*offset..*offset + 8)
        .ok_or("snapshot header is truncated")?
        .try_into()
        .map_err(|_| "snapshot integer is malformed")?;
    *offset += 8;
    Ok(u64::from_le_bytes(bytes))
}

fn preflight_target(
    root: &Path,
    key: &SessionKey,
    expected: Option<&EntryState>,
) -> Result<(), SessionStoreError> {
    let path = snapshot_path(root, key);
    match secure_file_metadata(&path) {
        Ok(_metadata) if expected.is_none() => Err(SessionStoreError::Corrupt(
            "an unaccounted target appeared before write".to_owned(),
        )),
        Ok(metadata) if expected.is_some_and(|entry| entry.stored_bytes != metadata.len()) => Err(
            SessionStoreError::Corrupt("snapshot size changed outside the store".to_owned()),
        ),
        Ok(_) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound && expected.is_none() => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Err(SessionStoreError::Corrupt(
            "accounted snapshot disappeared before write".to_owned(),
        )),
        Err(source) => Err(map_read_error(&path, source)),
    }
}

#[derive(Debug)]
struct AtomicWriteFailure {
    source: io::Error,
    installed: bool,
    privacy_failure: bool,
}

impl From<io::Error> for AtomicWriteFailure {
    fn from(source: io::Error) -> Self {
        Self {
            source,
            installed: false,
            privacy_failure: false,
        }
    }
}

fn write_atomic_with_verifier(
    root: &Path,
    target: &Path,
    payload: &[u8],
    before_replace: impl FnOnce() -> io::Result<()>,
    verify_installed: impl FnOnce(&Path) -> io::Result<()>,
) -> Result<(), AtomicWriteFailure> {
    let directory = target
        .parent()
        .expect("snapshot path always has a model directory");
    let before_install = |source| AtomicWriteFailure {
        source,
        installed: false,
        privacy_failure: false,
    };
    ensure_private_directory(root).map_err(before_install)?;
    ensure_private_directory(&principal_parent(root, target)).map_err(before_install)?;
    ensure_private_directory(directory).map_err(before_install)?;
    let temp = temp_path(target);
    let backup = transaction_path(target, "backup");
    let had_prior = match fs::symlink_metadata(target) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(source) => return Err(before_install(source)),
    };
    let write_result = (|| -> Result<(), AtomicWriteFailure> {
        let mut file = create_private_file(&temp)?;
        file.write_all(payload)?;
        file.sync_all()?;
        verify_private_file(&temp)?;
        drop(file);
        before_replace()?;
        if had_prior {
            fs::rename(target, &backup)?;
        }
        if let Err(source) = replace_file(&temp, target) {
            if had_prior {
                let _ = fs::rename(&backup, target);
            }
            return Err(AtomicWriteFailure {
                source,
                installed: false,
                privacy_failure: false,
            });
        }
        if let Err(source) = verify_installed(target) {
            let restored = rollback_installed_snapshot(target, had_prior.then_some(&backup));
            return Err(AtomicWriteFailure {
                source,
                installed: !restored,
                privacy_failure: true,
            });
        }
        if had_prior {
            fs::remove_file(&backup).map_err(|source| AtomicWriteFailure {
                source,
                installed: true,
                privacy_failure: false,
            })?;
        }
        Ok(())
    })();
    if let Err(error) = write_result {
        if temp.exists() {
            let _ = fs::remove_file(&temp);
        }
        return Err(error);
    }
    if let Err(error) = sync_parent(target) {
        return Err(AtomicWriteFailure {
            source: error,
            installed: true,
            privacy_failure: false,
        });
    }
    Ok(())
}

fn transaction_path(target: &Path, role: &str) -> PathBuf {
    let sequence = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let name = target
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("snapshot.amws");
    target.with_file_name(format!(
        ".{name}.{}.{}.{}",
        std::process::id(),
        sequence,
        role
    ))
}

/// Moves an untrusted installed name out of the read path before restoring the prior snapshot.
/// A failed cleanup may leave a quarantined object, but never intentionally re-exposes it as the
/// canonical snapshot name.
fn rollback_installed_snapshot(target: &Path, backup: Option<&Path>) -> bool {
    let quarantine = transaction_path(target, "unsafe");
    if fs::rename(target, &quarantine).is_err() {
        return false;
    }
    if let Some(backup) = backup {
        if fs::rename(backup, target).is_err() {
            return false;
        }
    }
    match fs::symlink_metadata(&quarantine) {
        Ok(metadata) if metadata.is_dir() => {
            let _ = fs::remove_dir(&quarantine);
        }
        Ok(_) => {
            let _ = fs::remove_file(&quarantine);
        }
        Err(_) => {}
    }
    true
}

fn principal_parent(root: &Path, target: &Path) -> PathBuf {
    target
        .parent()
        .and_then(Path::parent)
        .map(Path::to_owned)
        .unwrap_or_else(|| root.to_owned())
}

fn temp_path(target: &Path) -> PathBuf {
    let sequence = TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let name = target
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("snapshot.amws");
    target.with_file_name(format!(".{name}.{}.{}.tmp", std::process::id(), sequence))
}

fn read_bounded_nofollow(path: &Path, max_bytes: u64) -> io::Result<Vec<u8>> {
    let file = open_private_file_nofollow(path)?;
    let opened = file.metadata()?;
    verify_regular_metadata(&opened)?;
    if opened.len() > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "snapshot exceeds configured maximum",
        ));
    }
    let bound = max_bytes.saturating_add(1);
    let mut encoded = Vec::with_capacity(usize::try_from(opened.len()).map_err(|_| {
        io::Error::new(io::ErrorKind::InvalidData, "snapshot size exceeds platform")
    })?);
    file.take(bound).read_to_end(&mut encoded)?;
    if encoded.len() as u64 > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "snapshot exceeded configured maximum while reading",
        ));
    }
    Ok(encoded)
}

fn map_read_error(path: &Path, source: io::Error) -> SessionStoreError {
    match source.kind() {
        io::ErrorKind::NotFound => SessionStoreError::Unknown,
        io::ErrorKind::InvalidData | io::ErrorKind::InvalidInput => {
            SessionStoreError::Corrupt(source.to_string())
        }
        _ => SessionStoreError::Io {
            operation: "read",
            path: path.to_owned(),
            source,
        },
    }
}

#[derive(Debug)]
enum ScanFailure {
    Privacy(String),
    Io { path: PathBuf, source: io::Error },
}

fn scan_store(
    root: &Path,
    limits: SessionStoreLimits,
    accounting: &mut Accounting,
) -> Result<(), ScanFailure> {
    if let Err(error) = ensure_private_directory(root).and_then(|_| verify_private_directory(root))
    {
        return Err(ScanFailure::Privacy(format!(
            "cannot secure session root {}: {error}",
            root.display()
        )));
    }
    let mut visited = 0u64;
    for principal_entry in read_dir(root)? {
        let principal_entry = principal_entry.map_err(|source| ScanFailure::Io {
            path: root.to_owned(),
            source,
        })?;
        visited = visited.saturating_add(1);
        if visited > limits.scan_entry_limit {
            accounting.add_disable_reason(PersistenceDisableReason::ScanLimit);
            break;
        }
        let principal_path = principal_entry.path();
        let principal_name = principal_entry.file_name().to_string_lossy().into_owned();
        let Some(principal_hash) = decode_hash_namespace(&principal_name, PRINCIPAL_PREFIX) else {
            accounting.add_disable_reason(PersistenceDisableReason::Malformed(format!(
                "unexpected principal namespace {}",
                principal_path.display()
            )));
            continue;
        };
        if let Err(error) = verify_secure_directory(&principal_path) {
            accounting.add_disable_reason(PersistenceDisableReason::Privacy(format!(
                "unsafe principal namespace {}: {error}",
                principal_path.display()
            )));
            continue;
        }
        for model_entry in read_dir(&principal_path)? {
            let model_entry = model_entry.map_err(|source| ScanFailure::Io {
                path: principal_path.clone(),
                source,
            })?;
            visited = visited.saturating_add(1);
            if visited > limits.scan_entry_limit {
                accounting.add_disable_reason(PersistenceDisableReason::ScanLimit);
                break;
            }
            let model_path = model_entry.path();
            let model_name = model_entry.file_name().to_string_lossy().into_owned();
            let Some(model_hash) = decode_hash_namespace(&model_name, MODEL_PREFIX) else {
                accounting.add_disable_reason(PersistenceDisableReason::Malformed(format!(
                    "unexpected model namespace {}",
                    model_path.display()
                )));
                continue;
            };
            if let Err(error) = verify_secure_directory(&model_path) {
                accounting.add_disable_reason(PersistenceDisableReason::Privacy(format!(
                    "unsafe model namespace {}: {error}",
                    model_path.display()
                )));
                continue;
            }
            for snapshot_entry in read_dir(&model_path)? {
                let snapshot_entry = snapshot_entry.map_err(|source| ScanFailure::Io {
                    path: model_path.clone(),
                    source,
                })?;
                visited = visited.saturating_add(1);
                if visited > limits.scan_entry_limit {
                    accounting.add_disable_reason(PersistenceDisableReason::ScanLimit);
                    break;
                }
                scan_snapshot(
                    snapshot_entry.path(),
                    principal_hash,
                    model_hash,
                    limits,
                    accounting,
                );
            }
        }
    }
    Ok(())
}

fn read_dir(path: &Path) -> Result<fs::ReadDir, ScanFailure> {
    fs::read_dir(path).map_err(|source| ScanFailure::Io {
        path: path.to_owned(),
        source,
    })
}

fn scan_snapshot(
    path: PathBuf,
    principal_hash: [u8; HASH_BYTES],
    model_hash: [u8; HASH_BYTES],
    limits: SessionStoreLimits,
    accounting: &mut Accounting,
) {
    let session_id = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_owned();
    if path.extension().and_then(|value| value.to_str()) != Some(SNAPSHOT_EXTENSION)
        || validate_id("session_id", &session_id).is_err()
    {
        accounting.add_disable_reason(PersistenceDisableReason::Malformed(format!(
            "unexpected session entry {}",
            path.display()
        )));
        return;
    }
    let key = SessionKey {
        principal_hash,
        model_hash,
        session_id,
    };
    if let Err(error) =
        fs::symlink_metadata(&path).and_then(|metadata| verify_regular_metadata(&metadata))
    {
        accounting.add_disable_reason(PersistenceDisableReason::Privacy(format!(
            "unsafe snapshot {}: {error}",
            path.display()
        )));
        return;
    }
    let metadata = match secure_file_metadata(&path) {
        Ok(metadata) => metadata,
        Err(error) => {
            accounting.add_disable_reason(PersistenceDisableReason::Privacy(format!(
                "unsafe snapshot {}: {error}",
                path.display()
            )));
            return;
        }
    };
    let mut state = EntryState {
        stored_bytes: metadata.len(),
        updated_at: None,
        corrupt: false,
    };
    match read_header_nofollow(&path, limits.max_snapshot_bytes).and_then(|header| {
        parse_scanned_header(&header, principal_hash, model_hash, metadata.len())
    }) {
        Ok(header) => state.updated_at = Some(header.updated_at),
        Err(error) => {
            state.corrupt = true;
            accounting.add_disable_reason(PersistenceDisableReason::Malformed(format!(
                "malformed snapshot {}: {error}",
                path.display()
            )));
        }
    }
    accounting.add_entry(key, state);
}

fn read_header_nofollow(path: &Path, max_bytes: u64) -> io::Result<Vec<u8>> {
    let metadata = secure_file_metadata(path)?;
    if metadata.len() > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "snapshot exceeds configured maximum",
        ));
    }
    let mut file = open_nofollow(path)?;
    let mut header = vec![0u8; HEADER_BYTES];
    file.read_exact(&mut header)?;
    Ok(header)
}

fn parse_scanned_header(
    header: &[u8],
    principal_hash: [u8; HASH_BYTES],
    model_hash: [u8; HASH_BYTES],
    stored_bytes: u64,
) -> io::Result<ParsedHeader> {
    let parsed = parse_header(header, principal_hash, model_hash)
        .map_err(|message| io::Error::new(io::ErrorKind::InvalidData, message))?;
    let expected = encoded_size(parsed.payload_bytes, u64::MAX)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error.to_string()))?;
    if expected != stored_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "snapshot length does not match header",
        ));
    }
    Ok(parsed)
}

fn mark_over_quota(accounting: &mut Accounting, limits: SessionStoreLimits) {
    if accounting.host.sessions > limits.max_sessions_per_host {
        accounting.add_disable_reason(PersistenceDisableReason::OverQuota(
            "host session count".to_owned(),
        ));
    }
    if accounting.host.bytes > limits.max_bytes_per_host {
        accounting.add_disable_reason(PersistenceDisableReason::OverQuota(
            "host stored bytes".to_owned(),
        ));
    }
    let over_principal = accounting.principals.values().any(|usage| {
        usage.sessions > limits.max_sessions_per_principal
            || usage.bytes > limits.max_bytes_per_principal
    });
    if over_principal {
        accounting.add_disable_reason(PersistenceDisableReason::OverQuota(
            "principal quota".to_owned(),
        ));
    }
}

pub(crate) fn verify_secure_directory(path: &Path) -> io::Result<()> {
    verify_private_directory(path)
}

pub(crate) fn ensure_private_directory(path: &Path) -> io::Result<()> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            verify_not_reparse(&metadata)?;
            if !metadata.is_dir() {
                return Err(io::Error::new(
                    io::ErrorKind::AlreadyExists,
                    "session path exists but is not a directory",
                ));
            }
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            if let Err(create_error) = create_private_directory(path) {
                if create_error.kind() != io::ErrorKind::AlreadyExists {
                    return Err(create_error);
                }
            }
        }
        Err(error) => return Err(error),
    }
    verify_private_directory(path)
}

/// Creates a missing managed root component-by-component, while never rewriting an existing root.
pub(crate) fn prepare_managed_private_root(path: &Path) -> io::Result<()> {
    let path = explicit_root(path.to_owned())
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidInput, error.to_string()))?;
    verify_no_reparse_components(&path)?;
    let mut current = PathBuf::new();
    for component in path.components() {
        current.push(component.as_os_str());
        if current.as_os_str().is_empty()
            || current == Path::new(component.as_os_str()) && !current.has_root()
        {
            continue;
        }
        match fs::symlink_metadata(&current) {
            Ok(metadata) => {
                verify_not_reparse(&metadata)?;
                if !metadata.is_dir() {
                    return Err(io::Error::new(
                        io::ErrorKind::NotADirectory,
                        format!(
                            "storage root component is not a directory: {}",
                            current.display()
                        ),
                    ));
                }
                if current == path {
                    verify_private_directory(&current)?;
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {
                create_private_directory(&current)?;
                verify_private_directory(&current)?;
            }
            Err(error) => return Err(error),
        }
    }
    verify_secure_directory(&path)
}

/// Rejects symbolic links and Windows reparse points in every existing path component.
pub(crate) fn verify_no_reparse_components(path: &Path) -> io::Result<()> {
    let mut current = Some(path);
    while let Some(component_path) = current {
        match fs::symlink_metadata(component_path) {
            Ok(metadata) => {
                verify_not_reparse(&metadata)?;
                if component_path != path && !metadata.is_dir() {
                    return Err(io::Error::new(
                        io::ErrorKind::NotADirectory,
                        format!(
                            "storage path component is not a directory: {}",
                            component_path.display()
                        ),
                    ));
                }
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        current = component_path.parent();
    }
    Ok(())
}

/// Applies and verifies the platform privacy contract without traversing a reparse point.
pub(crate) fn secure_and_verify_private_path(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    verify_not_reparse(&metadata)?;
    secure_path(path)?;
    verify_private_path(path)
}

#[cfg(unix)]
fn create_private_directory(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::DirBuilderExt;

    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700);
    builder.create(path)
}

#[cfg(windows)]
fn create_private_directory(path: &Path) -> io::Result<()> {
    windows_acl::create_private_directory(path)
}

#[cfg(not(any(unix, windows)))]
fn create_private_directory(path: &Path) -> io::Result<()> {
    fs::create_dir(path)
}

#[cfg(unix)]
pub(crate) fn create_private_file(path: &Path) -> io::Result<File> {
    use std::os::unix::fs::OpenOptionsExt;

    OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
}

#[cfg(windows)]
pub(crate) fn create_private_file(path: &Path) -> io::Result<File> {
    windows_acl::create_private_file(path)
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn create_private_file(path: &Path) -> io::Result<File> {
    OpenOptions::new().write(true).create_new(true).open(path)
}

pub(crate) fn secure_file_metadata(path: &Path) -> io::Result<fs::Metadata> {
    let metadata = fs::symlink_metadata(path)?;
    verify_regular_metadata(&metadata)?;
    verify_private_file(path)?;
    Ok(metadata)
}

pub(crate) fn verify_regular_metadata(metadata: &fs::Metadata) -> io::Result<()> {
    verify_not_reparse(metadata)?;
    if !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "snapshot is not a regular file",
        ));
    }
    Ok(())
}

#[cfg(unix)]
pub(crate) fn verify_not_reparse(metadata: &fs::Metadata) -> io::Result<()> {
    if metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "symbolic links are forbidden in session storage",
        ));
    }
    Ok(())
}

#[cfg(windows)]
pub(crate) fn verify_not_reparse(metadata: &fs::Metadata) -> io::Result<()> {
    use std::os::windows::fs::MetadataExt;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
    if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "reparse points are forbidden in session storage",
        ));
    }
    Ok(())
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn verify_not_reparse(metadata: &fs::Metadata) -> io::Result<()> {
    if metadata.file_type().is_symlink() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "symbolic links are forbidden in session storage",
        ));
    }
    Ok(())
}

pub(crate) fn open_nofollow(path: &Path) -> io::Result<File> {
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
        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
    let file = options.open(path)?;
    verify_regular_metadata(&file.metadata()?)?;
    Ok(file)
}

/// Opens one exact-private regular file without following a reparse point and keeps the
/// verified object handle stable for the caller's subsequent read.
#[cfg(windows)]
pub(crate) fn open_private_file_nofollow(path: &Path) -> io::Result<File> {
    windows_acl::open_private_file(path)
}

#[cfg(unix)]
pub(crate) fn open_private_file_nofollow(path: &Path) -> io::Result<File> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    unsafe extern "C" {
        fn geteuid() -> u32;
    }

    let file = open_nofollow(path)?;
    let metadata = file.metadata()?;
    if metadata.permissions().mode() & 0o777 != 0o600 {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "session file permissions are not private",
        ));
    }
    // SAFETY: `geteuid` has no arguments and returns the effective process UID.
    if metadata.uid() != unsafe { geteuid() } {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "session file is not owned by the current process identity",
        ));
    }
    Ok(file)
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn open_private_file_nofollow(_path: &Path) -> io::Result<File> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "private session permissions are unsupported on this platform",
    ))
}

#[cfg(unix)]
fn secure_path(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::PermissionsExt;

    let metadata = fs::symlink_metadata(path)?;
    verify_not_reparse(&metadata)?;
    let mode = if metadata.is_dir() { 0o700 } else { 0o600 };
    fs::set_permissions(path, fs::Permissions::from_mode(mode))
}

#[cfg(unix)]
pub(crate) fn verify_private_path(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::{MetadataExt, PermissionsExt};

    unsafe extern "C" {
        fn geteuid() -> u32;
    }

    let metadata = fs::symlink_metadata(path)?;
    verify_not_reparse(&metadata)?;
    let expected = if metadata.is_dir() { 0o700 } else { 0o600 };
    if metadata.permissions().mode() & 0o777 != expected {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "session path permissions are not private",
        ));
    }
    // SAFETY: `geteuid` has no arguments and returns the effective process UID.
    if metadata.uid() != unsafe { geteuid() } {
        return Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "session path is not owned by the current process identity",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn secure_path(path: &Path) -> io::Result<()> {
    windows_acl::apply_owner_system_acl(path, false)
}

#[cfg(windows)]
pub(crate) fn verify_private_path(path: &Path) -> io::Result<()> {
    windows_acl::verify_private_object(path)
}

#[cfg(not(any(unix, windows)))]
fn secure_path(_path: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "private session permissions are unsupported on this platform",
    ))
}

#[cfg(not(any(unix, windows)))]
pub(crate) fn verify_private_path(_path: &Path) -> io::Result<()> {
    Err(io::Error::new(
        io::ErrorKind::Unsupported,
        "private session permissions are unsupported on this platform",
    ))
}

#[cfg(windows)]
pub(crate) fn verify_private_file(path: &Path) -> io::Result<()> {
    open_private_file_nofollow(path).map(drop)
}

#[cfg(not(windows))]
pub(crate) fn verify_private_file(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    verify_regular_metadata(&metadata)?;
    verify_private_path(path)
}

#[cfg(windows)]
pub(crate) fn verify_private_directory(path: &Path) -> io::Result<()> {
    windows_acl::verify_private_path(path, windows_acl::PrivatePathKind::Directory)
}

#[cfg(not(windows))]
pub(crate) fn verify_private_directory(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    verify_not_reparse(&metadata)?;
    if !metadata.is_dir() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "session namespace is not a directory",
        ));
    }
    verify_private_path(path)
}

#[cfg(unix)]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    fs::rename(source, target)
}

#[cfg(windows)]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    use std::os::windows::ffi::OsStrExt;
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    // `MoveFileExW` does not perform Rust's path normalization. In particular,
    // configuration files commonly supply an absolute root with `/` separators,
    // while the hashed namespace appended by `Path::join` uses `\`. Resolve the
    // already-verified parent once so both native paths use one canonical Windows
    // representation and extended-length paths remain supported.
    let directory = source.parent().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "temporary snapshot has no parent",
        )
    })?;
    if target.parent() != Some(directory) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "atomic snapshot replacement must remain within one directory",
        ));
    }
    let canonical_directory = fs::canonicalize(directory)?;
    let source = canonical_directory.join(source.file_name().ok_or_else(|| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "temporary snapshot has no filename",
        )
    })?);
    let target =
        canonical_directory.join(target.file_name().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "snapshot has no filename")
        })?);
    let source_wide = source
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    let target_wide = target
        .as_os_str()
        .encode_wide()
        .chain(Some(0))
        .collect::<Vec<_>>();
    // SAFETY: both arguments are stable, NUL-terminated UTF-16 paths for the duration of the call.
    let result = unsafe {
        MoveFileExW(
            source_wide.as_ptr(),
            target_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if result == 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(())
    }
}

#[cfg(not(any(unix, windows)))]
fn replace_file(source: &Path, target: &Path) -> io::Result<()> {
    fs::rename(source, target)
}

#[cfg(unix)]
fn sync_parent(path: &Path) -> io::Result<()> {
    let parent = path
        .parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "snapshot has no parent"))?;
    File::open(parent)?.sync_all()
}

#[cfg(not(unix))]
fn sync_parent(_path: &Path) -> io::Result<()> {
    Ok(())
}

#[cfg(windows)]
mod windows_acl {
    use std::{
        ffi::c_void,
        fs::{self, File},
        io,
        mem::size_of,
        os::windows::{
            ffi::OsStrExt,
            io::{AsRawHandle, FromRawHandle},
        },
        path::Path,
        ptr,
    };

    use windows_sys::Win32::{
        Foundation::{
            CloseHandle, GetLastError, LocalFree, ERROR_INSUFFICIENT_BUFFER, HANDLE,
            INVALID_HANDLE_VALUE,
        },
        Security::{
            Authorization::{
                ConvertSidToStringSidW, ConvertStringSecurityDescriptorToSecurityDescriptorW,
                GetNamedSecurityInfoW, GetSecurityInfo, SE_FILE_OBJECT,
            },
            GetAce, GetSecurityDescriptorControl, GetSecurityDescriptorDacl,
            GetSecurityDescriptorOwner, GetTokenInformation, TokenUser, ACCESS_ALLOWED_ACE, ACL,
            DACL_SECURITY_INFORMATION, INHERITED_ACE, OWNER_SECURITY_INFORMATION,
            PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID, SECURITY_ATTRIBUTES,
            SE_DACL_PROTECTED, TOKEN_QUERY, TOKEN_USER,
        },
        Storage::FileSystem::{
            CreateDirectoryW, CreateFileW, FileAttributeTagInfo, GetFileInformationByHandleEx,
            CREATE_NEW, FILE_ALL_ACCESS, FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_NORMAL,
            FILE_ATTRIBUTE_REPARSE_POINT, FILE_ATTRIBUTE_TAG_INFO, FILE_FLAG_BACKUP_SEMANTICS,
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_GENERIC_READ, FILE_GENERIC_WRITE,
            FILE_READ_ATTRIBUTES, FILE_SHARE_DELETE, FILE_SHARE_READ, FILE_SHARE_WRITE,
            OPEN_EXISTING,
        },
        System::{
            SystemServices::ACCESS_ALLOWED_ACE_TYPE,
            Threading::{GetCurrentProcess, OpenProcessToken},
        },
    };

    #[cfg(test)]
    use windows_sys::Win32::Security::GetSecurityDescriptorLength;

    const SDDL_REVISION_1: u32 = 1;
    const READ_CONTROL_ACCESS: u32 = 0x0002_0000;

    #[derive(Clone, Copy)]
    pub(super) enum PrivatePathKind {
        File,
        Directory,
    }

    pub(super) fn create_private_directory(path: &Path) -> io::Result<()> {
        let wide = new_path_wide(path)?;
        let (descriptor, attributes) = private_security_attributes()?;
        // SAFETY: the path is NUL-terminated and the descriptor outlives this call.
        if unsafe { CreateDirectoryW(wide.as_ptr(), &attributes) } == 0 {
            return Err(io::Error::last_os_error());
        }
        drop(descriptor);
        // SAFETY: `wide` is NUL-terminated, the access/share flags are valid, and the returned
        // handle is checked before ownership is transferred to `File` below.
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                FILE_READ_ATTRIBUTES | READ_CONTROL_ACCESS,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            let error = io::Error::last_os_error();
            let _ = fs::remove_dir(path);
            return Err(error);
        }
        // SAFETY: CreateFileW returned a newly owned live handle.
        let directory = unsafe { File::from_raw_handle(handle as _) };
        let result = verify_private_handle(&directory, PrivatePathKind::Directory);
        if result.is_err() {
            drop(directory);
            let _ = fs::remove_dir(path);
        }
        result
    }

    pub(super) fn create_private_file(path: &Path) -> io::Result<File> {
        let wide = new_path_wide(path)?;
        let (_descriptor, attributes) = private_security_attributes()?;
        // SAFETY: the path and security descriptor remain live for the call.
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                FILE_GENERIC_WRITE,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                &attributes,
                CREATE_NEW,
                FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OPEN_REPARSE_POINT,
                ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: CreateFileW returned a newly owned live handle.
        let file = unsafe { File::from_raw_handle(handle as _) };
        if let Err(error) = verify_private_handle(&file, PrivatePathKind::File) {
            drop(file);
            let _ = fs::remove_file(path);
            return Err(error);
        }
        Ok(file)
    }

    fn private_security_attributes() -> io::Result<(PrivateDescriptor, SECURITY_ATTRIBUTES)> {
        let owner = current_process_sid()?;
        let sddl = format!("O:{owner}D:P(A;;FA;;;{owner})(A;;FA;;;SY)");
        let sddl = sddl.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        let mut raw = ptr::null_mut();
        // SAFETY: sddl is NUL-terminated and raw is writable output storage.
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl.as_ptr(),
                SDDL_REVISION_1,
                &mut raw,
                ptr::null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let descriptor = PrivateDescriptor(raw);
        let attributes = SECURITY_ATTRIBUTES {
            nLength: size_of::<SECURITY_ATTRIBUTES>() as u32,
            lpSecurityDescriptor: descriptor.0,
            bInheritHandle: 0,
        };
        Ok((descriptor, attributes))
    }

    struct PrivateDescriptor(PSECURITY_DESCRIPTOR);

    impl Drop for PrivateDescriptor {
        fn drop(&mut self) {
            // SAFETY: the conversion API allocated this descriptor with LocalAlloc.
            unsafe { LocalFree(self.0.cast::<c_void>()) };
        }
    }

    pub(super) fn apply_owner_system_acl(path: &Path, allow_owner_change: bool) -> io::Result<()> {
        let wide = wide(path)?;
        let owner_text = current_process_sid()?;
        let existing_descriptor = query_descriptor(&wide, OWNER_SECURITY_INFORMATION)?;
        let existing_owner_text = sid_to_string(existing_descriptor.owner)?;
        if !allow_owner_change {
            require_current_owner(&existing_owner_text, &owner_text)?;
        }
        let sddl = format!("O:{owner_text}D:P(A;;FA;;;{owner_text})(A;;FA;;;SY)");
        let sddl_wide = sddl.encode_utf16().chain(Some(0)).collect::<Vec<_>>();
        let mut acl_descriptor: PSECURITY_DESCRIPTOR = ptr::null_mut();
        // SAFETY: sddl_wide is NUL-terminated and acl_descriptor is an out pointer.
        if unsafe {
            ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl_wide.as_ptr(),
                SDDL_REVISION_1,
                &mut acl_descriptor,
                ptr::null_mut(),
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        let mut present = 0;
        let mut defaulted = 0;
        let mut acl: *mut ACL = ptr::null_mut();
        let mut owner: PSID = ptr::null_mut();
        let mut owner_defaulted = 0;
        // SAFETY: acl_descriptor remains live and all outputs point to valid stack variables.
        let got_acl = unsafe {
            GetSecurityDescriptorDacl(acl_descriptor, &mut present, &mut acl, &mut defaulted)
        };
        if got_acl == 0 || present == 0 || acl.is_null() {
            // SAFETY: descriptor was allocated by the conversion API.
            unsafe { LocalFree(acl_descriptor.cast::<c_void>()) };
            return Err(io::Error::last_os_error());
        }
        // SAFETY: acl_descriptor remains live and owns the returned owner SID.
        if unsafe { GetSecurityDescriptorOwner(acl_descriptor, &mut owner, &mut owner_defaulted) }
            == 0
            || owner.is_null()
        {
            // SAFETY: `acl_descriptor` was allocated by the security-descriptor conversion API.
            unsafe { LocalFree(acl_descriptor.cast::<c_void>()) };
            return Err(io::Error::last_os_error());
        }
        // SAFETY: path and ACL pointers remain live for this call.
        let security_information = DACL_SECURITY_INFORMATION
            | PROTECTED_DACL_SECURITY_INFORMATION
            | if allow_owner_change {
                OWNER_SECURITY_INFORMATION
            } else {
                0
            };
        // SAFETY: `wide`, `owner`, and `acl` remain live for the call and describe a filesystem
        // object security update with no borrowed outputs.
        let status = unsafe {
            windows_sys::Win32::Security::Authorization::SetNamedSecurityInfoW(
                wide.as_ptr(),
                SE_FILE_OBJECT,
                security_information,
                if allow_owner_change {
                    owner
                } else {
                    ptr::null_mut()
                },
                ptr::null_mut(),
                acl,
                ptr::null_mut(),
            )
        };
        // SAFETY: both descriptors were allocated by LocalAlloc-returning Win32 APIs.
        unsafe { LocalFree(acl_descriptor.cast::<c_void>()) };
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status as i32));
        }
        verify_private_object(path)
    }

    fn require_current_owner(actual_owner: &str, current_owner: &str) -> io::Result<()> {
        if actual_owner != current_owner {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "session path is not owned by the current process identity",
            ));
        }
        Ok(())
    }

    fn current_process_sid() -> io::Result<String> {
        let mut token = ptr::null_mut();
        // SAFETY: the process pseudo-handle is valid and `token` is writable output storage.
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let token = OwnedHandle(token);
        let mut required = 0_u32;
        // SAFETY: this is the documented sizing call with a null buffer and writable size output.
        let sizing_result =
            unsafe { GetTokenInformation(token.0, TokenUser, ptr::null_mut(), 0, &mut required) };
        // SAFETY: this reads the calling thread's last-error value immediately after the failed
        // sizing call above.
        let sizing_error = unsafe { GetLastError() };
        if sizing_result != 0
            || sizing_error != ERROR_INSUFFICIENT_BUFFER
            || required < size_of::<TOKEN_USER>() as u32
        {
            return Err(io::Error::last_os_error());
        }
        let words = (required as usize).div_ceil(size_of::<usize>());
        let mut storage = vec![0_usize; words];
        // SAFETY: `storage` has at least `required` writable bytes and the live token is queryable.
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
        // SAFETY: the successful call above initialized at least one complete `TOKEN_USER` in
        // aligned `usize` storage, which remains live for this borrow.
        let user = unsafe { &*storage.as_ptr().cast::<TOKEN_USER>() };
        sid_to_string(user.User.Sid)
    }

    struct OwnedHandle(HANDLE);

    impl Drop for OwnedHandle {
        fn drop(&mut self) {
            // SAFETY: `OwnedHandle` is constructed only from a successfully opened, uniquely
            // owned Win32 handle and closes it exactly once here.
            unsafe { CloseHandle(self.0) };
        }
    }

    pub(super) fn verify_private_path(path: &Path, expected: PrivatePathKind) -> io::Result<()> {
        let file = open_existing_nofollow(path, FILE_READ_ATTRIBUTES | READ_CONTROL_ACCESS)?;
        verify_private_handle(&file, expected)
    }

    pub(super) fn verify_private_object(path: &Path) -> io::Result<()> {
        let file = open_existing_nofollow(path, FILE_READ_ATTRIBUTES | READ_CONTROL_ACCESS)?;
        verify_private_handle_security(&file)
    }

    pub(super) fn open_private_file(path: &Path) -> io::Result<File> {
        let file = open_existing_nofollow(path, FILE_GENERIC_READ | READ_CONTROL_ACCESS)?;
        verify_private_handle(&file, PrivatePathKind::File)?;
        Ok(file)
    }

    fn open_existing_nofollow(path: &Path, desired_access: u32) -> io::Result<File> {
        let wide = new_path_wide(path)?;
        // OPEN_REPARSE_POINT guarantees the handle identifies the named object rather than
        // traversing a symbolic link or mount-point target.
        // SAFETY: `wide` is NUL-terminated, all flags are valid, and the result is checked before
        // being converted into an owned `File`.
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                desired_access,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: CreateFileW returned a newly owned live handle.
        Ok(unsafe { File::from_raw_handle(handle as _) })
    }

    fn verify_private_handle(file: &File, expected: PrivatePathKind) -> io::Result<()> {
        let mut attributes = FILE_ATTRIBUTE_TAG_INFO::default();
        // SAFETY: file owns a live handle and attributes is writable storage of the declared size.
        if unsafe {
            GetFileInformationByHandleEx(
                file.as_raw_handle() as _,
                FileAttributeTagInfo,
                ptr::addr_of_mut!(attributes).cast::<c_void>(),
                size_of::<FILE_ATTRIBUTE_TAG_INFO>() as u32,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        if attributes.FileAttributes & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "reparse points are forbidden in session storage",
            ));
        }
        let is_directory = attributes.FileAttributes & FILE_ATTRIBUTE_DIRECTORY != 0;
        if is_directory != matches!(expected, PrivatePathKind::Directory) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "session path kind does not match the expected private object kind",
            ));
        }
        verify_private_handle_security(file)
    }

    fn verify_private_handle_security(file: &File) -> io::Result<()> {
        let descriptor =
            query_handle_descriptor(file, OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION)?;
        verify_descriptor(descriptor)
    }

    #[cfg(test)]
    pub(super) fn security_descriptor_bytes(path: &Path) -> io::Result<Vec<u8>> {
        let file = open_existing_nofollow(path, FILE_READ_ATTRIBUTES | READ_CONTROL_ACCESS)?;
        let descriptor = query_handle_descriptor(
            &file,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        )?;
        // SAFETY: the descriptor owns a live security descriptor.
        let length = unsafe { GetSecurityDescriptorLength(descriptor.security_descriptor) };
        if length == 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: GetSecurityDescriptorLength returned the allocation's exact byte length.
        Ok(unsafe {
            std::slice::from_raw_parts(descriptor.security_descriptor.cast::<u8>(), length as usize)
                .to_vec()
        })
    }

    #[cfg(test)]
    pub(super) fn create_exact_private_junction(path: &Path, target: &Path) -> io::Result<()> {
        const FSCTL_SET_REPARSE_POINT: u32 = 0x0009_00A4;
        const IO_REPARSE_TAG_MOUNT_POINT: u32 = 0xA000_0003;
        const GENERIC_WRITE_ACCESS: u32 = 0x4000_0000;
        #[link(name = "kernel32")]
        unsafe extern "system" {
            fn DeviceIoControl(
                device: HANDLE,
                control_code: u32,
                input: *mut c_void,
                input_bytes: u32,
                output: *mut c_void,
                output_bytes: u32,
                returned_bytes: *mut u32,
                overlapped: *mut c_void,
            ) -> i32;
        }

        create_private_directory(path)?;
        let wide = new_path_wide(path)?;
        // SAFETY: the path is NUL-terminated and the directory was just created by this process.
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                GENERIC_WRITE_ACCESS | READ_CONTROL_ACCESS,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT,
                ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            let error = io::Error::last_os_error();
            let _ = fs::remove_dir(path);
            return Err(error);
        }
        // SAFETY: CreateFileW returned a newly owned live handle.
        let directory = unsafe { File::from_raw_handle(handle as _) };
        let canonical_target = fs::canonicalize(target)?;
        let target_text = canonical_target.to_string_lossy();
        let substitute = target_text.strip_prefix(r"\\?\").map_or_else(
            || format!(r"\??\{target_text}"),
            |value| format!(r"\??\{value}"),
        );
        let print = target.display().to_string();
        let substitute = substitute.encode_utf16().collect::<Vec<_>>();
        let print = print.encode_utf16().collect::<Vec<_>>();
        let substitute_bytes = u16::try_from(substitute.len().saturating_mul(2)).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "junction target is too long")
        })?;
        let print_bytes = u16::try_from(print.len().saturating_mul(2)).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "junction target is too long")
        })?;
        let print_offset = substitute_bytes.saturating_add(2);
        let path_bytes = usize::from(print_offset)
            .saturating_add(usize::from(print_bytes))
            .saturating_add(2);
        let data_length = u16::try_from(8usize.saturating_add(path_bytes)).map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "junction data is too long")
        })?;
        let mut buffer = Vec::with_capacity(8 + usize::from(data_length));
        buffer.extend_from_slice(&IO_REPARSE_TAG_MOUNT_POINT.to_le_bytes());
        buffer.extend_from_slice(&data_length.to_le_bytes());
        buffer.extend_from_slice(&0u16.to_le_bytes());
        buffer.extend_from_slice(&0u16.to_le_bytes());
        buffer.extend_from_slice(&substitute_bytes.to_le_bytes());
        buffer.extend_from_slice(&print_offset.to_le_bytes());
        buffer.extend_from_slice(&print_bytes.to_le_bytes());
        for word in substitute
            .iter()
            .chain(std::iter::once(&0))
            .chain(print.iter())
            .chain(std::iter::once(&0))
        {
            buffer.extend_from_slice(&word.to_le_bytes());
        }
        let mut returned = 0;
        // SAFETY: directory is a live no-follow directory handle and buffer is a complete
        // mount-point REPARSE_DATA_BUFFER for the duration of the call.
        if unsafe {
            DeviceIoControl(
                directory.as_raw_handle() as _,
                FSCTL_SET_REPARSE_POINT,
                buffer.as_mut_ptr().cast::<c_void>(),
                buffer.len() as u32,
                ptr::null_mut(),
                0,
                &mut returned,
                ptr::null_mut(),
            )
        } == 0
        {
            let error = io::Error::last_os_error();
            drop(directory);
            let _ = fs::remove_dir(path);
            return Err(error);
        }
        let descriptor = query_handle_descriptor(
            &directory,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
        )?;
        verify_descriptor(descriptor)
    }

    fn verify_descriptor(descriptor: Descriptor) -> io::Result<()> {
        let mut control = 0u16;
        let mut revision = 0u32;
        // SAFETY: descriptor.security_descriptor is a valid self-relative security descriptor.
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
                "session DACL inheritance is not disabled",
            ));
        }
        let owner_text = sid_to_string(descriptor.owner)?;
        require_current_owner(&owner_text, &current_process_sid()?)?;
        let acl = descriptor.dacl;
        // SAFETY: descriptor owns a live ACL and the null guard prevents dereferencing null.
        if acl.is_null() || unsafe { (*acl).AceCount } != 2 {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "session DACL is not limited to owner and SYSTEM",
            ));
        }
        let mut saw_owner = false;
        let mut saw_system = false;
        for index in 0..2 {
            let mut raw_ace = ptr::null_mut();
            // SAFETY: ACL has exactly two ACEs and raw_ace is an out pointer.
            if unsafe { GetAce(acl, index, &mut raw_ace) } == 0 {
                return Err(io::Error::last_os_error());
            }
            let ace = raw_ace.cast::<ACCESS_ALLOWED_ACE>();
            // SAFETY: GetAce returned a valid ACE header within the live ACL.
            let header = unsafe { (*ace).Header };
            if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE
                || u32::from(header.AceFlags) & INHERITED_ACE != 0
            {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "session DACL contains a non-explicit allow ACE",
                ));
            }
            // SAFETY: the checked ACE type guarantees ACCESS_ALLOWED_ACE layout.
            let sid = unsafe { ptr::addr_of!((*ace).SidStart).cast_mut().cast::<c_void>() };
            let mask = unsafe { (*ace).Mask };
            if mask & FILE_ALL_ACCESS != FILE_ALL_ACCESS {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "session DACL does not grant full control",
                ));
            }
            // Compare text forms to avoid caller-sized well-known SID buffers.
            let sid_text = sid_to_string(sid)?;
            saw_owner |= sid_text == owner_text;
            saw_system |= sid_text == "S-1-5-18";
        }
        if !saw_owner || !saw_system {
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "session DACL does not contain exactly owner and SYSTEM",
            ));
        }
        Ok(())
    }

    struct Descriptor {
        security_descriptor: PSECURITY_DESCRIPTOR,
        owner: PSID,
        dacl: *mut ACL,
    }

    impl Drop for Descriptor {
        fn drop(&mut self) {
            // SAFETY: GetNamedSecurityInfoW allocates this descriptor with LocalAlloc.
            unsafe { LocalFree(self.security_descriptor.cast::<c_void>()) };
        }
    }

    fn query_descriptor(wide: &[u16], information: u32) -> io::Result<Descriptor> {
        let mut security_descriptor = ptr::null_mut();
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        // SAFETY: wide is NUL-terminated and all requested outputs are valid stack pointers.
        let status = unsafe {
            GetNamedSecurityInfoW(
                wide.as_ptr(),
                SE_FILE_OBJECT,
                information,
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
        if security_descriptor.is_null() || owner.is_null() {
            if !security_descriptor.is_null() {
                // SAFETY: GetNamedSecurityInfoW allocated the non-null descriptor.
                unsafe { LocalFree(security_descriptor.cast::<c_void>()) };
            }
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "session path has no security owner",
            ));
        }
        Ok(Descriptor {
            security_descriptor,
            owner,
            dacl,
        })
    }

    fn query_handle_descriptor(file: &File, information: u32) -> io::Result<Descriptor> {
        let mut security_descriptor = ptr::null_mut();
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        // SAFETY: the file owns a live handle and all requested outputs are valid pointers.
        let status = unsafe {
            GetSecurityInfo(
                file.as_raw_handle() as _,
                SE_FILE_OBJECT,
                information,
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
        if security_descriptor.is_null() || owner.is_null() {
            if !security_descriptor.is_null() {
                // SAFETY: non-null `security_descriptor` was allocated by GetNamedSecurityInfoW.
                unsafe { LocalFree(security_descriptor.cast::<c_void>()) };
            }
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "session handle has no security owner",
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
        // SAFETY: sid points into a live descriptor and string is an out pointer.
        if unsafe { ConvertSidToStringSidW(sid, &mut string) } == 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: successful conversion returned a live NUL-terminated UTF-16 allocation.
        let value = unsafe { utf16_ptr_to_string(string) };
        // SAFETY: conversion allocated the string using LocalAlloc.
        unsafe { LocalFree(string.cast::<c_void>()) };
        Ok(value)
    }

    fn wide(path: &Path) -> io::Result<Vec<u16>> {
        // The named-security APIs do not normalize mixed separators or add the
        // extended-length prefix. Canonicalizing an already-opened store path
        // preserves the no-reparse verification while making hashed namespace
        // paths beyond MAX_PATH valid Win32 inputs.
        Ok(fs::canonicalize(path)?
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect())
    }

    fn new_path_wide(path: &Path) -> io::Result<Vec<u16>> {
        let parent = path.parent().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "session path has no parent")
        })?;
        let name = path.file_name().ok_or_else(|| {
            io::Error::new(io::ErrorKind::InvalidInput, "session path has no filename")
        })?;
        let normalized = fs::canonicalize(parent)?.join(name);
        let value = normalized
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        if value[..value.len().saturating_sub(1)].contains(&0) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "session path contains an interior NUL",
            ));
        }
        Ok(value)
    }

    unsafe fn utf16_ptr_to_string(pointer: *const u16) -> String {
        let mut length = 0usize;
        // SAFETY: Win32 returned a NUL-terminated string pointer.
        while unsafe { *pointer.add(length) } != 0 {
            length += 1;
        }
        // SAFETY: length was derived by scanning the same allocation to its NUL terminator.
        String::from_utf16_lossy(unsafe { std::slice::from_raw_parts(pointer, length) })
    }

    #[cfg(test)]
    mod ownership_tests {
        use super::require_current_owner;

        #[test]
        fn existing_foreign_owner_is_rejected_before_acl_rewrite() {
            let error = require_current_owner("S-1-5-32-544", "S-1-5-21-1000").unwrap_err();
            assert_eq!(error.kind(), std::io::ErrorKind::PermissionDenied);
        }

        #[test]
        fn current_owner_is_accepted() {
            require_current_owner("S-1-5-21-1000", "S-1-5-21-1000").unwrap();
        }
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        sync::{Arc, Barrier},
        thread,
    };

    use super::*;

    fn limits() -> SessionStoreLimits {
        SessionStoreLimits {
            max_snapshot_bytes: 1_024,
            max_sessions_per_principal: 2,
            max_bytes_per_principal: 2_048,
            max_sessions_per_host: 3,
            max_bytes_per_host: 3_072,
            scan_entry_limit: 32,
            retention: Duration::from_secs(60),
        }
    }

    fn key(principal: &str, model: u8, session: &str) -> SessionKey {
        SessionKey::new(principal, [model; HASH_BYTES], session).unwrap()
    }

    fn save(store: &SessionStore, key: &SessionKey, payload: &[u8]) {
        let reservation = store.reserve(key, payload.len() as u64).unwrap();
        store.write(reservation, payload).unwrap();
    }

    #[test]
    fn ids_are_strict_ascii_and_bounded() {
        for invalid in ["", "space here", "../escape", "é", &"x".repeat(129)] {
            assert!(matches!(
                SessionKey::new(invalid, [1; HASH_BYTES], "valid"),
                Err(SessionStoreError::InvalidInput(_))
            ));
            assert!(matches!(
                SessionKey::new("valid", [1; HASH_BYTES], invalid),
                Err(SessionStoreError::InvalidInput(_))
            ));
        }
        assert!(SessionKey::new("Principal_1", [1; HASH_BYTES], "session-1").is_ok());
    }

    #[test]
    fn store_root_must_be_explicitly_anchored() {
        assert!(matches!(
            SessionStore::open("sessions"),
            Err(SessionStoreError::InvalidInput(message))
                if message.contains("must be absolute")
        ));
    }

    #[test]
    fn principal_and_host_count_quotas_include_reservations() {
        let temp = tempfile::tempdir().unwrap();
        let store = SessionStore::open_with_limits(temp.path().join("sessions"), limits()).unwrap();
        save(&store, &key("p1", 1, "one"), b"1");
        save(&store, &key("p1", 1, "two"), b"2");
        assert!(matches!(
            store.reserve(&key("p1", 1, "three"), 1),
            Err(SessionStoreError::Quota {
                scope: "principal",
                limit: "session count"
            })
        ));
        save(&store, &key("p2", 1, "three"), b"3");
        assert!(matches!(
            store.reserve(&key("p3", 1, "four"), 1),
            Err(SessionStoreError::Quota {
                scope: "host",
                limit: "session count"
            })
        ));
    }

    #[test]
    fn restart_reconstructs_accounting_once() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let first = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        save(&first, &key("p1", 1, "one"), b"durable");
        drop(first);
        let restarted = SessionStore::open_with_limits(root, limits()).unwrap();
        let status = restarted.quota_status(Some("p1")).unwrap();
        assert_eq!(status.host.sessions, 1);
        assert_eq!(status.principal.unwrap().sessions, 1);
        assert_eq!(restarted.read(&key("p1", 1, "one")).unwrap(), b"durable");
    }

    #[test]
    fn managed_root_creation_is_private_and_scheduler_namespace_is_scan_isolated() {
        let temp = tempfile::tempdir().unwrap();
        let storage_root = temp.path().join("managed").join("kv");
        prepare_managed_private_root(&storage_root).unwrap();
        verify_secure_directory(&storage_root).unwrap();
        let scheduler_root = storage_root.join("scheduler");
        ensure_private_directory(&scheduler_root).unwrap();
        let scheduler_model = scheduler_root.join("a".repeat(HASH_HEX_BYTES));
        ensure_private_directory(&scheduler_model).unwrap();
        let mut scheduler_snapshot =
            create_private_file(&scheduler_model.join("live.amwkv")).unwrap();
        scheduler_snapshot.write_all(b"scheduler-owned").unwrap();
        drop(scheduler_snapshot);

        let durable_root = storage_root.join("durable");
        let first = SessionStore::open_with_limits(durable_root.clone(), limits()).unwrap();
        let target = key("p1", 1, "one");
        save(&first, &target, b"durable");
        drop(first);

        let restarted = SessionStore::open_with_limits(durable_root, limits()).unwrap();
        assert_eq!(restarted.quota_status(None).unwrap().host.sessions, 1);
        assert_eq!(restarted.read(&target).unwrap(), b"durable");
        verify_secure_directory(&scheduler_root).unwrap();
        verify_secure_directory(&scheduler_model).unwrap();
        verify_private_file(&scheduler_model.join("live.amwkv")).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn existing_managed_root_with_wrong_mode_is_not_repaired() {
        use std::os::unix::fs::PermissionsExt;

        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("existing");
        fs::create_dir(&root).unwrap();
        fs::set_permissions(&root, fs::Permissions::from_mode(0o755)).unwrap();

        prepare_managed_private_root(&root).expect_err("existing root must be verify-only");

        assert_eq!(
            fs::metadata(root).unwrap().permissions().mode() & 0o777,
            0o755
        );
    }

    #[cfg(windows)]
    #[test]
    fn atomic_replace_accepts_forward_slash_config_roots_on_windows() {
        let temp = tempfile::tempdir().unwrap();
        let root = PathBuf::from(format!(
            "{}/sessions",
            temp.path().display().to_string().replace('\\', "/")
        ));
        let store = SessionStore::open_with_limits(root, limits()).unwrap();
        let target = key("p1", 1, "mixed-separators");
        save(&store, &target, b"durable");
        assert_eq!(store.read(&target).unwrap(), b"durable");
    }

    #[cfg(windows)]
    #[test]
    fn atomic_write_accepts_extended_mixed_separator_paths_on_windows() {
        let temp = tempfile::tempdir().unwrap();
        let long_parent = temp.path().join("x".repeat(120));
        fs::create_dir(&long_parent).unwrap();
        let root = PathBuf::from(format!(
            "{}/sessions",
            long_parent.display().to_string().replace('\\', "/")
        ));
        let store = SessionStore::open_with_limits(root, limits()).unwrap();
        let target = key("contract-owner", 1, "empty-restart-session");
        save(&store, &target, b"durable");
        assert_eq!(store.read(&target).unwrap(), b"durable");
    }

    #[test]
    fn concurrent_reservations_do_not_overcommit() {
        let temp = tempfile::tempdir().unwrap();
        let mut constrained = limits();
        constrained.max_sessions_per_principal = 1;
        let store = Arc::new(
            SessionStore::open_with_limits(temp.path().join("sessions"), constrained).unwrap(),
        );
        let barrier = Arc::new(Barrier::new(3));
        let handles = ["one", "two"].map(|session| {
            let store = Arc::clone(&store);
            let barrier = Arc::clone(&barrier);
            thread::spawn(move || {
                barrier.wait();
                let result = store.reserve(&key("p1", 1, session), 1);
                barrier.wait();
                result.is_ok()
            })
        });
        barrier.wait();
        barrier.wait();
        let successes = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .filter(|success| *success)
            .count();
        assert_eq!(successes, 1);
    }

    #[test]
    fn replacement_accounts_only_committed_delta() {
        let temp = tempfile::tempdir().unwrap();
        let store = SessionStore::open_with_limits(temp.path().join("sessions"), limits()).unwrap();
        let target = key("p1", 1, "same");
        save(&store, &target, b"short");
        let before = store.quota_status(Some("p1")).unwrap().host.bytes;
        save(&store, &target, b"a considerably longer replacement");
        let after = store.quota_status(Some("p1")).unwrap().host.bytes;
        assert_eq!(after - before, 28);
        assert_eq!(store.quota_status(None).unwrap().host.sessions, 1);
    }

    #[test]
    fn principal_and_host_byte_quotas_account_for_file_overhead() {
        let temp = tempfile::tempdir().unwrap();
        let mut constrained = limits();
        constrained.max_snapshot_bytes = 200;
        constrained.max_bytes_per_principal = 240;
        constrained.max_bytes_per_host = 362;
        let store =
            SessionStore::open_with_limits(temp.path().join("sessions"), constrained).unwrap();
        save(&store, &key("p1", 1, "one"), b"x");
        assert!(matches!(
            store.reserve(&key("p1", 1, "two"), 1),
            Err(SessionStoreError::Quota {
                scope: "principal",
                limit: "stored bytes"
            })
        ));
        save(&store, &key("p2", 1, "two"), b"y");
        assert!(matches!(
            store.reserve(&key("p3", 1, "three"), 1),
            Err(SessionStoreError::Quota {
                scope: "host",
                limit: "stored bytes"
            })
        ));
    }

    #[test]
    fn injected_pre_replace_failure_preserves_prior_snapshot_and_releases_quota() {
        let temp = tempfile::tempdir().unwrap();
        let store = SessionStore::open_with_limits(temp.path().join("sessions"), limits()).unwrap();
        let target = key("p1", 1, "same");
        save(&store, &target, b"prior");
        let mut reservation = store.reserve(&target, 11).unwrap();
        let error = store
            .write_reserved_with(&mut reservation, b"replacement", || {
                Err(io::Error::new(
                    io::ErrorKind::StorageFull,
                    "injected disk full",
                ))
            })
            .unwrap_err();
        assert!(matches!(error, SessionStoreError::Io { .. }));
        drop(reservation);
        assert_eq!(store.read(&target).unwrap(), b"prior");
        assert_eq!(store.quota_status(None).unwrap().host.reserved_bytes, 0);
    }

    #[test]
    fn injected_post_install_privacy_failure_restores_prior_and_disables_saves() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let store = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let target = key("p1", 1, "same");
        save(&store, &target, b"prior");
        let mut reservation = store.reserve(&target, 11).unwrap();

        let error = store
            .write_reserved_with_verifier(
                &mut reservation,
                b"replacement",
                || Ok(()),
                |_| {
                    Err(io::Error::new(
                        io::ErrorKind::PermissionDenied,
                        "injected final ACL verification failure",
                    ))
                },
            )
            .unwrap_err();

        assert!(matches!(error, SessionStoreError::Io { .. }));
        let status = store.quota_status(None).unwrap();
        assert_eq!(status.host.reserved_bytes, 0);
        assert!(!status.saves_enabled);
        assert!(status
            .disable_reasons
            .iter()
            .any(|reason| matches!(reason, PersistenceDisableReason::Privacy(_))));
        assert!(matches!(
            store.read(&target),
            Err(SessionStoreError::Disabled(_))
        ));
        drop(store);

        let restarted = SessionStore::open_with_limits(root, limits()).unwrap();
        assert_eq!(restarted.read(&target).unwrap(), b"prior");
        assert_eq!(restarted.quota_status(None).unwrap().host.sessions, 1);
    }

    #[test]
    fn sweep_deletes_only_expired_sessions() {
        let temp = tempfile::tempdir().unwrap();
        let store = SessionStore::open_with_limits(temp.path().join("sessions"), limits()).unwrap();
        let target = key("p1", 1, "old");
        save(&store, &target, b"payload");
        let early = store.sweep_expired(SystemTime::now()).unwrap();
        assert_eq!(early.sessions_deleted, 0);
        let late = store
            .sweep_expired(SystemTime::now() + Duration::from_secs(61))
            .unwrap();
        assert_eq!(late.sessions_deleted, 1);
        assert!(matches!(
            store.read(&target),
            Err(SessionStoreError::Unknown)
        ));
    }

    #[test]
    fn startup_sweep_deletes_expired_sessions_before_store_is_returned() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let target = key("p1", 1, "expired-at-startup");
        let first = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        save(&first, &target, b"payload");
        let path = snapshot_path(&root, &target);
        fs::write(
            &path,
            encode_snapshot(&target, UNIX_EPOCH + Duration::from_secs(1), b"payload").unwrap(),
        )
        .unwrap();
        drop(first);

        let restarted = SessionStore::open_with_limits(root, limits()).unwrap();

        assert_eq!(restarted.quota_status(None).unwrap().host.sessions, 0);
        assert!(!path.exists());
    }

    #[test]
    fn due_maintenance_sweep_runs_from_normal_store_operations() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let store = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let target = key("p1", 1, "expired-periodically");
        save(&store, &target, b"payload");
        let expired_at = UNIX_EPOCH + Duration::from_secs(1);
        let path = snapshot_path(&root, &target);
        fs::write(
            &path,
            encode_snapshot(&target, expired_at, b"payload").unwrap(),
        )
        .unwrap();
        lock_accounting(&store.inner.accounting)
            .entries
            .get_mut(&target)
            .unwrap()
            .updated_at = Some(expired_at);
        store.inner.next_retention_sweep.store(0, Ordering::Release);

        let status = store.quota_status(None).unwrap();

        assert_eq!(status.host.sessions, 0);
        assert!(!path.exists());
        assert!(
            store.inner.next_retention_sweep.load(Ordering::Acquire)
                > unix_seconds_saturating(SystemTime::now())
        );
    }

    #[test]
    fn malformed_scan_disables_saves_but_list_delete_and_reconcile_recover() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let first = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let target = key("p1", 1, "broken");
        save(&first, &target, b"payload");
        let path = snapshot_path(&root, &target);
        fs::write(&path, b"broken").unwrap();
        drop(first);
        let store = SessionStore::open_with_limits(root, limits()).unwrap();
        assert!(!store.quota_status(None).unwrap().saves_enabled);
        let listed = store.list("p1", [1; HASH_BYTES]).unwrap();
        assert_eq!(listed.len(), 1);
        assert!(listed[0].corrupt);
        store.delete(&target).unwrap();
        assert!(store.reconcile().unwrap().saves_enabled);
        save(&store, &key("p1", 1, "recovered"), b"ok");
    }

    #[test]
    fn startup_overquota_disables_new_saves_without_hiding_recovery_apis() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let first = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let one = key("p1", 1, "one");
        let two = key("p1", 1, "two");
        save(&first, &one, b"one");
        save(&first, &two, b"two");
        drop(first);

        let mut tightened = limits();
        tightened.max_sessions_per_principal = 1;
        let store = SessionStore::open_with_limits(root, tightened).unwrap();
        assert!(!store.quota_status(Some("p1")).unwrap().saves_enabled);
        assert_eq!(store.list("p1", [1; HASH_BYTES]).unwrap().len(), 2);
        assert_eq!(store.read(&one).unwrap(), b"one");
        store.delete(&two).unwrap();
        assert!(store.reconcile().unwrap().saves_enabled);
    }

    #[test]
    fn privacy_setup_failure_returns_a_disabled_recovery_handle() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        fs::write(&root, b"not a private directory").unwrap();
        let store = SessionStore::open_with_limits(root, limits()).unwrap();
        let status = store.quota_status(None).unwrap();
        assert!(!status.saves_enabled);
        assert!(status
            .disable_reasons
            .iter()
            .any(|reason| matches!(reason, PersistenceDisableReason::Privacy(_))));
        assert!(matches!(
            store.reserve(&key("p1", 1, "blocked"), 1),
            Err(SessionStoreError::Disabled(_))
        ));
    }

    #[cfg(unix)]
    #[test]
    fn unix_modes_are_private_and_symlinks_fail_closed() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let store = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let target = key("p1", 1, "private");
        save(&store, &target, b"payload");
        let path = snapshot_path(&root, &target);
        assert_eq!(
            fs::metadata(&root).unwrap().permissions().mode() & 0o777,
            0o700
        );
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        let linked = key("p1", 1, "linked");
        symlink(&path, snapshot_path(&root, &linked)).unwrap();
        assert!(matches!(
            store.read(&linked),
            Err(SessionStoreError::Corrupt(_))
        ));
    }

    #[cfg(windows)]
    #[test]
    fn windows_acl_is_private_and_reparse_points_fail_closed() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let store = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let target = key("p1", 1, "private");
        save(&store, &target, b"payload");
        let path = snapshot_path(&root, &target);
        verify_private_directory(&root).unwrap();
        verify_private_file(&path).unwrap();
        let junction_target = root.join("junction-target");
        ensure_private_directory(&junction_target).unwrap();
        let junction = root.join("exact-private-junction");
        windows_acl::create_exact_private_junction(&junction, &junction_target).unwrap();
        assert!(!windows_acl::security_descriptor_bytes(&junction)
            .unwrap()
            .is_empty());
        let error = verify_private_directory(&junction)
            .expect_err("an exact-private reparse point must still fail closed");
        assert!(error.to_string().contains("reparse"), "{error}");
    }

    #[cfg(windows)]
    #[test]
    fn rejected_existing_root_preserves_the_actual_security_descriptor() {
        let root = tempfile::tempdir().unwrap();
        let before = windows_acl::security_descriptor_bytes(root.path()).unwrap();

        prepare_managed_private_root(root.path())
            .expect_err("an inherited-ACL existing root must be verify-only");

        let after = windows_acl::security_descriptor_bytes(root.path()).unwrap();
        assert_eq!(after, before);
    }

    #[cfg(windows)]
    #[test]
    fn final_session_target_is_handle_verified_after_replacement() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("sessions");
        let store = SessionStore::open_with_limits(root.clone(), limits()).unwrap();
        let target = key("p1", 1, "replace-final");
        save(&store, &target, b"first");
        save(&store, &target, b"replacement");
        let path = snapshot_path(&root, &target);

        verify_private_file(&path).unwrap();
        assert_eq!(store.read(&target).unwrap(), b"replacement");
    }

    #[cfg(windows)]
    #[test]
    fn fixed_kind_verifiers_reject_exact_private_object_substitution() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("private-root");
        ensure_private_directory(&root).unwrap();

        let directory = root.join("directory-at-snapshot-path");
        ensure_private_directory(&directory).unwrap();
        let directory_error = verify_private_file(&directory)
            .expect_err("a private directory must not satisfy the snapshot-file contract");
        assert!(
            directory_error.to_string().contains("kind"),
            "{directory_error}"
        );

        let file = root.join("file-at-namespace-path");
        drop(create_private_file(&file).unwrap());
        let file_error = verify_private_directory(&file)
            .expect_err("a private file must not satisfy the namespace-directory contract");
        assert!(file_error.to_string().contains("kind"), "{file_error}");
    }
}
