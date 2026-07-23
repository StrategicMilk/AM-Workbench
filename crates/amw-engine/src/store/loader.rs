//! Ledger-governed model lifecycle with owned resources and per-model locks.

use std::{
    any::Any,
    collections::BTreeMap,
    fmt,
    fs::{self, File, OpenOptions},
    io::{BufReader, Read, Seek, SeekFrom},
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
    time::Instant,
};

#[cfg(target_os = "linux")]
use std::{
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

use sha2::{Digest, Sha256};
use thiserror::Error;

use crate::hw::budget::{BudgetError, MemoryAmount, MemoryLedger, MemoryPurpose, ReservationId};

use super::{
    gguf_meta::{inspect_opened_gguf, GgufMetadata, IntegrityError},
    registry::ModelRecord,
};

pub const DEFAULT_KEEP_ALIVE_MS: u64 = 30 * 60 * 1_000;
#[cfg(target_os = "linux")]
static SNAPSHOT_SEQUENCE: AtomicU64 = AtomicU64::new(0);

pub trait Clock {
    fn now_ms(&self) -> u64;
}

/// Process-monotonic production clock for keep-alive and LRU decisions.
#[derive(Clone, Copy, Debug)]
pub struct MonotonicClock {
    origin: Instant,
}

impl MonotonicClock {
    pub fn new() -> Self {
        Self {
            origin: Instant::now(),
        }
    }
}

impl Default for MonotonicClock {
    fn default() -> Self {
        Self::new()
    }
}

impl Clock for MonotonicClock {
    fn now_ms(&self) -> u64 {
        u64::try_from(self.origin.elapsed().as_millis()).unwrap_or(u64::MAX)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KeepAlive {
    Default,
    DurationMs(u64),
    Immediate,
    Never,
}

impl KeepAlive {
    fn deadline(self, now_ms: u64) -> Option<u64> {
        match self {
            Self::Default => Some(now_ms.saturating_add(DEFAULT_KEEP_ALIVE_MS)),
            Self::DurationMs(duration) => Some(now_ms.saturating_add(duration)),
            Self::Immediate => Some(now_ms),
            Self::Never => None,
        }
    }
}

#[derive(Debug, Error)]
pub enum LoaderError {
    #[error(transparent)]
    Budget(#[from] BudgetError),
    #[error("model file is missing for {model_id}: {path}")]
    MissingModel { model_id: String, path: PathBuf },
    #[error("model integrity check failed for {model_id} at {path}: {source}")]
    CorruptModel {
        model_id: String,
        path: PathBuf,
        #[source]
        source: IntegrityError,
    },
    #[error("model allocation failed: {0}")]
    Allocation(String),
    #[error("model release failed: {0}")]
    Release(String),
    #[error("model is not loaded: {0}")]
    NotLoaded(String),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct FileIdentity {
    first: u64,
    second: u64,
}

struct ImmutableSnapshot {
    file: Option<File>,
    #[cfg_attr(not(any(feature = "cpu", feature = "cuda")), allow(dead_code))]
    native_path: PathBuf,
    cleanup_path: Option<PathBuf>,
    cleanup_directory: Option<PathBuf>,
}

#[cfg(target_os = "linux")]
struct PendingSnapshot {
    file: Option<File>,
    path: Option<PathBuf>,
    directory: Option<PathBuf>,
}

#[cfg(target_os = "linux")]
impl PendingSnapshot {
    fn file_mut(&mut self) -> &mut File {
        self.file
            .as_mut()
            .expect("pending model snapshot file remains live")
    }

    fn into_parts(mut self) -> (File, PathBuf, PathBuf) {
        let file = self
            .file
            .take()
            .expect("pending model snapshot file remains live");
        let path = self
            .path
            .take()
            .expect("pending model snapshot path remains live");
        let directory = self
            .directory
            .take()
            .expect("pending model snapshot directory remains live");
        (file, directory, path)
    }
}

#[cfg(target_os = "linux")]
impl Drop for PendingSnapshot {
    fn drop(&mut self) {
        drop(self.file.take());
        if let Some(path) = self.path.take() {
            let _ = fs::remove_file(path);
        }
        if let Some(path) = self.directory.take() {
            let _ = fs::remove_dir(path);
        }
    }
}

impl ImmutableSnapshot {
    #[cfg(target_os = "linux")]
    fn copy_from(
        source: &File,
        expected_size: u64,
        display_path: &Path,
    ) -> Result<Self, IntegrityError> {
        let mut pending = create_private_snapshot(display_path)?;
        let mut reader = source.try_clone().map_err(|source| IntegrityError::Io {
            path: display_path.to_owned(),
            source,
        })?;
        reader
            .seek(SeekFrom::Start(0))
            .map_err(|source| IntegrityError::Io {
                path: display_path.to_owned(),
                source,
            })?;
        let copied = std::io::copy(
            &mut reader.take(expected_size.saturating_add(1)),
            pending.file_mut(),
        )
        .map_err(|source| IntegrityError::Io {
            path: display_path.to_owned(),
            source,
        })?;
        if copied != expected_size {
            return Err(IntegrityError::IdentityChanged);
        }
        pending
            .file_mut()
            .sync_all()
            .map_err(|source| IntegrityError::Io {
                path: display_path.to_owned(),
                source,
            })?;
        pending
            .file_mut()
            .seek(SeekFrom::Start(0))
            .map_err(|source| IntegrityError::Io {
                path: display_path.to_owned(),
                source,
            })?;
        let path = pending
            .path
            .as_ref()
            .expect("pending model snapshot path remains live");
        let directory = pending
            .directory
            .as_ref()
            .expect("pending model snapshot directory remains live");
        super::session::secure_and_verify_private_path(directory).map_err(|source| {
            IntegrityError::Io {
                path: display_path.to_owned(),
                source,
            }
        })?;
        super::session::secure_and_verify_private_path(path).map_err(|source| {
            IntegrityError::Io {
                path: display_path.to_owned(),
                source,
            }
        })?;
        let (file, directory, path) = pending.into_parts();
        finalize_snapshot(file, directory, path, display_path)
    }

    #[cfg(not(any(target_os = "linux", windows)))]
    fn copy_from(
        _source: &File,
        _expected_size: u64,
        _display_path: &Path,
    ) -> Result<Self, IntegrityError> {
        Err(IntegrityError::IdentityGuardUnsupported)
    }

    #[cfg(windows)]
    fn copy_from(
        source: &File,
        expected_size: u64,
        display_path: &Path,
    ) -> Result<Self, IntegrityError> {
        let file = source.try_clone().map_err(|source| IntegrityError::Io {
            path: display_path.to_owned(),
            source,
        })?;
        let metadata = file.metadata().map_err(|source| IntegrityError::Io {
            path: display_path.to_owned(),
            source,
        })?;
        if metadata.len() != expected_size {
            return Err(IntegrityError::IdentityChanged);
        }
        Ok(Self {
            file: Some(file),
            native_path: display_path.to_owned(),
            cleanup_path: None,
            cleanup_directory: None,
        })
    }

    fn file(&self) -> &File {
        self.file
            .as_ref()
            .expect("verified model snapshot file remains live")
    }

    #[cfg_attr(not(any(feature = "cpu", feature = "cuda")), allow(dead_code))]
    fn native_path(&self) -> &Path {
        &self.native_path
    }
}

impl Drop for ImmutableSnapshot {
    fn drop(&mut self) {
        drop(self.file.take());
        if let Some(path) = self.cleanup_path.take() {
            if remove_snapshot_file(&path).is_err() {
                tracing::warn!(
                    "verified model snapshot cleanup was deferred by the operating system"
                );
            }
        }
        if let Some(path) = self.cleanup_directory.take() {
            let _ = fs::remove_dir(path);
        }
    }
}

#[cfg(windows)]
fn remove_snapshot_file(path: &Path) -> std::io::Result<()> {
    let mut last_error = None;
    for _ in 0..20 {
        match fs::remove_file(path) {
            Ok(()) => return Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
            Err(error) => {
                last_error = Some(error);
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
        }
    }
    Err(last_error
        .unwrap_or_else(|| std::io::Error::other("verified model snapshot cleanup failed")))
}

#[cfg(not(windows))]
fn remove_snapshot_file(path: &Path) -> std::io::Result<()> {
    match fs::remove_file(path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}

/// Stable opened-file proof shared by inspection, fingerprinting, and native loading.
pub struct VerifiedModelFile {
    snapshot: ImmutableSnapshot,
    source_path: PathBuf,
    identity: FileIdentity,
    file_size: u64,
    metadata: GgufMetadata,
    sha256: [u8; 32],
}

struct OpenedModelFile {
    file: File,
    source_path: PathBuf,
    identity: FileIdentity,
    file_size: u64,
}

impl OpenedModelFile {
    fn open(path: &Path) -> Result<Self, IntegrityError> {
        let file = open_model_nofollow(path)?;
        let metadata = file.metadata().map_err(|source| IntegrityError::Io {
            path: path.to_owned(),
            source,
        })?;
        verify_regular_model_file(&metadata, path)?;
        Ok(Self {
            identity: file_identity(&file, &metadata, path)?,
            file_size: metadata.len(),
            file,
            source_path: path.to_owned(),
        })
    }
}

impl VerifiedModelFile {
    /// Opens, inspects, and fingerprints one non-link regular model file.
    pub fn open(path: &Path) -> Result<Self, IntegrityError> {
        Self::from_opened(OpenedModelFile::open(path)?)
    }

    fn from_opened(source: OpenedModelFile) -> Result<Self, IntegrityError> {
        let snapshot =
            ImmutableSnapshot::copy_from(&source.file, source.file_size, &source.source_path)?;
        let source_after = source.file.metadata().map_err(|error| IntegrityError::Io {
            path: source.source_path.clone(),
            source: error,
        })?;
        if source_after.len() != source.file_size
            || file_identity(&source.file, &source_after, &source.source_path)? != source.identity
        {
            return Err(IntegrityError::IdentityChanged);
        }
        let file = snapshot.file();
        let file_metadata = file.metadata().map_err(|error| IntegrityError::Io {
            path: source.source_path.clone(),
            source: error,
        })?;
        verify_regular_model_file(&file_metadata, &source.source_path)?;
        let identity = file_identity(file, &file_metadata, &source.source_path)?;
        let file_size = file_metadata.len();
        let metadata = inspect_opened_gguf(file, &source.source_path)?;
        let sha256 = hash_open_file(file, &source.source_path)?;
        if hash_open_file(&source.file, &source.source_path)? != sha256 {
            return Err(IntegrityError::IdentityChanged);
        }
        let verified = Self {
            snapshot,
            source_path: source.source_path,
            identity,
            file_size,
            metadata,
            sha256,
        };
        verified.verify_snapshot(false)?;
        Ok(verified)
    }

    /// Returns metadata parsed from this exact opened file.
    pub fn metadata(&self) -> &GgufMetadata {
        &self.metadata
    }

    /// Returns the SHA-256 digest read from this exact opened file.
    pub fn sha256(&self) -> [u8; 32] {
        self.sha256
    }

    /// Returns the source path retained for internal diagnostics only.
    pub fn source_path(&self) -> &Path {
        &self.source_path
    }

    /// Returns the stable path that the native loader must open.
    #[cfg_attr(not(any(feature = "cpu", feature = "cuda")), allow(dead_code))]
    pub(crate) fn native_path(&self) -> &Path {
        self.snapshot.native_path()
    }

    /// Re-hashes the opened file after native loading to detect in-place substitution.
    #[cfg_attr(not(any(feature = "cpu", feature = "cuda")), allow(dead_code))]
    pub(crate) fn verify_unchanged(&self) -> Result<(), IntegrityError> {
        self.verify_snapshot(true)
    }

    fn verify_snapshot(&self, verify_digest: bool) -> Result<(), IntegrityError> {
        let file = self.snapshot.file();
        let metadata = file.metadata().map_err(|source| IntegrityError::Io {
            path: self.source_path.clone(),
            source,
        })?;
        verify_regular_model_file(&metadata, &self.source_path)?;
        if metadata.len() != self.file_size
            || file_identity(file, &metadata, &self.source_path)? != self.identity
        {
            return Err(IntegrityError::IdentityChanged);
        }
        if verify_digest && hash_open_file(file, &self.source_path)? != self.sha256 {
            return Err(IntegrityError::IdentityChanged);
        }
        Ok(())
    }
}

impl fmt::Debug for VerifiedModelFile {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("VerifiedModelFile")
            .field("source_path", &self.source_path)
            .field("identity", &self.identity)
            .field("file_size", &self.file_size)
            .field("sha256", &"<verified>")
            .finish_non_exhaustive()
    }
}

/// Type-erased native resource or model-worker handle owned by one resident entry.
pub struct LoadedResource {
    inner: Box<dyn Any + Send + Sync>,
}

impl LoadedResource {
    pub fn new<R: Any + Send + Sync>(resource: R) -> Self {
        Self {
            inner: Box::new(resource),
        }
    }

    pub fn downcast_ref<R: Any + Send + Sync>(&self) -> Option<&R> {
        self.inner.downcast_ref()
    }

    pub fn is<R: Any + Send + Sync>(&self) -> bool {
        self.inner.is::<R>()
    }
}

impl fmt::Debug for LoadedResource {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str("LoadedResource(<opaque>)")
    }
}

#[derive(Debug)]
struct Resident {
    reservation: ReservationId,
    amount: MemoryAmount,
    last_access_ms: u64,
    expires_at_ms: Option<u64>,
    resource: LoadedResource,
}

#[derive(Debug, Default)]
pub struct ModelLocks {
    locks: Mutex<BTreeMap<String, Arc<Mutex<()>>>>,
}

impl ModelLocks {
    pub fn lock_for(&self, model_id: &str) -> Arc<Mutex<()>> {
        let mut locks = self
            .locks
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        locks
            .entry(model_id.to_owned())
            .or_insert_with(|| Arc::new(Mutex::new(())))
            .clone()
    }
}

#[derive(Debug)]
pub struct ModelLoader<C> {
    clock: C,
    ledger: MemoryLedger,
    residents: BTreeMap<String, Resident>,
    locks: ModelLocks,
    eviction_enabled: bool,
}

impl<C: Clock> ModelLoader<C> {
    pub fn new(clock: C, ledger: MemoryLedger, eviction_enabled: bool) -> Self {
        Self {
            clock,
            ledger,
            residents: BTreeMap::new(),
            locks: ModelLocks::default(),
            eviction_enabled,
        }
    }

    pub fn locks(&self) -> &ModelLocks {
        &self.locks
    }

    /// Compatibility entry point for accounting-only callers.
    pub fn load_with(
        &mut self,
        model_id: &str,
        amount: MemoryAmount,
        keep_alive: KeepAlive,
        allocate: impl FnOnce() -> Result<(), String>,
    ) -> Result<(), LoaderError> {
        self.load_resource_with(model_id, amount, keep_alive, allocate)
    }

    /// Admits and owns the actual loaded resource only after allocation succeeds.
    pub fn load_resource_with<R: Any + Send + Sync>(
        &mut self,
        model_id: &str,
        amount: MemoryAmount,
        keep_alive: KeepAlive,
        allocate: impl FnOnce() -> Result<R, String>,
    ) -> Result<(), LoaderError> {
        let model_lock = self.locks.lock_for(model_id);
        let _guard = model_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        self.load_resource_locked(model_id, amount, keep_alive, allocate)
    }

    /// Admits, snapshots, integrity-checks, and then allocates a registry record.
    pub fn load_record_with<R: Any + Send + Sync>(
        &mut self,
        record: &ModelRecord,
        amount: MemoryAmount,
        keep_alive: KeepAlive,
        allocate: impl FnOnce(&ModelRecord, VerifiedModelFile) -> Result<R, String>,
    ) -> Result<(), LoaderError> {
        let model_lock = self.locks.lock_for(&record.id);
        let _guard = model_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let now = self.clock.now_ms();
        if let Some(resident) = self.residents.get_mut(&record.id) {
            resident.last_access_ms = now;
            resident.expires_at_ms = keep_alive.deadline(now);
            return Ok(());
        }

        // Reserve the caller's declared native footprint before opening or
        // inspecting the source. The opened handle is then retained while the
        // reservation is expanded for platforms that require an immutable
        // full-file copy; Windows instead retains a handle that denies mutation.
        let preliminary = self.reserve_locked(MemoryPurpose::BaseModel, amount.clone())?;
        let source = match OpenedModelFile::open(&record.path) {
            Ok(source) => source,
            Err(source) => {
                self.ledger.release(preliminary)?;
                return Err(record_integrity_error(record, source));
            }
        };
        let mut accounted_amount = amount;
        accounted_amount.ram_bytes = match accounted_amount
            .ram_bytes
            .checked_add(snapshot_accounting_bytes(source.file_size))
        {
            Some(bytes) => bytes,
            None => {
                self.ledger.release(preliminary)?;
                return Err(BudgetError::AccountingOverflow.into());
            }
        };
        self.ledger.release(preliminary)?;
        let reservation =
            self.reserve_locked(MemoryPurpose::BaseModel, accounted_amount.clone())?;

        let verified = match VerifiedModelFile::from_opened(source) {
            Ok(verified) => verified,
            Err(source) => {
                self.ledger.release(reservation)?;
                return Err(record_integrity_error(record, source));
            }
        };
        let resource = match allocate(record, verified) {
            Ok(resource) => LoadedResource::new(resource),
            Err(error) => {
                self.ledger.release(reservation)?;
                return Err(LoaderError::Allocation(error));
            }
        };
        if let Err(error) = self.ledger.commit(reservation) {
            let _ = self.ledger.release(reservation);
            return Err(error.into());
        }
        self.residents.insert(
            record.id.clone(),
            Resident {
                reservation,
                amount: accounted_amount,
                last_access_ms: now,
                expires_at_ms: keep_alive.deadline(now),
                resource,
            },
        );
        Ok(())
    }

    pub fn unload(&mut self, model_id: &str) -> Result<(), LoaderError> {
        self.unload_with(model_id, |_| Ok(()))
    }

    /// Runs a fallible native/worker shutdown before releasing ledger capacity.
    pub fn unload_with(
        &mut self,
        model_id: &str,
        release: impl FnOnce(&LoadedResource) -> Result<(), String>,
    ) -> Result<(), LoaderError> {
        let model_lock = self.locks.lock_for(model_id);
        let _guard = model_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let resident = self
            .residents
            .get(model_id)
            .ok_or_else(|| LoaderError::NotLoaded(model_id.to_owned()))?;
        release(&resident.resource).map_err(LoaderError::Release)?;
        self.ledger.release(resident.reservation)?;
        self.residents
            .remove(model_id)
            .expect("resident cannot disappear during an exclusive unload");
        Ok(())
    }

    pub fn purge_expired(&mut self) -> Result<Vec<String>, LoaderError> {
        let now = self.clock.now_ms();
        let expired: Vec<_> = self
            .residents
            .iter()
            .filter_map(|(id, resident)| {
                resident
                    .expires_at_ms
                    .filter(|deadline| *deadline <= now)
                    .map(|_| id.clone())
            })
            .collect();
        for id in &expired {
            self.unload(id)?;
        }
        Ok(expired)
    }

    pub fn touch(&mut self, model_id: &str, keep_alive: KeepAlive) -> Result<(), LoaderError> {
        let model_lock = self.locks.lock_for(model_id);
        let _guard = model_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        let now = self.clock.now_ms();
        let resident = self
            .residents
            .get_mut(model_id)
            .ok_or_else(|| LoaderError::NotLoaded(model_id.to_owned()))?;
        resident.last_access_ms = now;
        resident.expires_at_ms = keep_alive.deadline(now);
        Ok(())
    }

    pub fn is_loaded(&self, model_id: &str) -> bool {
        self.residents.contains_key(model_id)
    }

    pub fn resident_resource(&self, model_id: &str) -> Option<&LoadedResource> {
        self.residents
            .get(model_id)
            .map(|resident| &resident.resource)
    }

    pub fn resident_ids(&self) -> impl ExactSizeIterator<Item = &str> {
        self.residents.keys().map(String::as_str)
    }

    pub fn resident_amount(&self, model_id: &str) -> Option<&MemoryAmount> {
        self.residents
            .get(model_id)
            .map(|resident| &resident.amount)
    }

    pub fn available_memory(&self) -> MemoryAmount {
        self.ledger.available()
    }

    fn load_resource_locked<R: Any + Send + Sync>(
        &mut self,
        model_id: &str,
        amount: MemoryAmount,
        keep_alive: KeepAlive,
        allocate: impl FnOnce() -> Result<R, String>,
    ) -> Result<(), LoaderError> {
        let now = self.clock.now_ms();
        if let Some(resident) = self.residents.get_mut(model_id) {
            resident.last_access_ms = now;
            resident.expires_at_ms = keep_alive.deadline(now);
            return Ok(());
        }

        let reservation = self.reserve_locked(MemoryPurpose::BaseModel, amount.clone())?;

        let resource = match allocate() {
            Ok(resource) => LoadedResource::new(resource),
            Err(error) => {
                self.ledger.release(reservation)?;
                return Err(LoaderError::Allocation(error));
            }
        };
        if let Err(error) = self.ledger.commit(reservation) {
            let _ = self.ledger.release(reservation);
            return Err(error.into());
        }
        self.residents.insert(
            model_id.to_owned(),
            Resident {
                reservation,
                amount,
                last_access_ms: now,
                expires_at_ms: keep_alive.deadline(now),
                resource,
            },
        );
        Ok(())
    }

    fn reserve_locked(
        &mut self,
        purpose: MemoryPurpose,
        amount: MemoryAmount,
    ) -> Result<ReservationId, LoaderError> {
        loop {
            match self.ledger.reserve(purpose, amount.clone()) {
                Ok(reservation) => return Ok(reservation),
                Err(error) if self.eviction_enabled => {
                    let Some(candidate) = self.lru_candidate() else {
                        return Err(error.into());
                    };
                    self.unload(&candidate)?;
                }
                Err(error) => return Err(error.into()),
            }
        }
    }

    fn lru_candidate(&self) -> Option<String> {
        self.residents
            .iter()
            .min_by_key(|(id, resident)| (resident.last_access_ms, id.as_str()))
            .map(|(id, _)| id.clone())
    }
}

fn record_integrity_error(record: &ModelRecord, source: IntegrityError) -> LoaderError {
    if matches!(&source, IntegrityError::Io { source, .. } if source.kind() == std::io::ErrorKind::NotFound)
    {
        LoaderError::MissingModel {
            model_id: record.id.clone(),
            path: record.path.clone(),
        }
    } else {
        LoaderError::CorruptModel {
            model_id: record.id.clone(),
            path: record.path.clone(),
            source,
        }
    }
}

#[cfg(windows)]
const fn snapshot_accounting_bytes(_file_size: u64) -> u64 {
    // The retained Windows handle denies write/delete sharing, so the native
    // loader safely reopens the original path without a second full-file copy.
    0
}

#[cfg(not(windows))]
const fn snapshot_accounting_bytes(file_size: u64) -> u64 {
    file_size
}

#[cfg(target_os = "linux")]
fn create_private_snapshot(display_path: &Path) -> Result<PendingSnapshot, IntegrityError> {
    let base = std::env::temp_dir();
    let timestamp = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    for attempt in 0..64_u64 {
        let sequence = SNAPSHOT_SEQUENCE.fetch_add(1, Ordering::Relaxed);
        let directory = base.join(format!(
            "amw-model-snapshot-{}-{timestamp:x}-{sequence:x}-{attempt:x}",
            std::process::id()
        ));
        match create_snapshot_directory(&directory) {
            Ok(()) => {}
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(source) => {
                return Err(IntegrityError::Io {
                    path: display_path.to_owned(),
                    source,
                });
            }
        }
        if let Err(source) = super::session::secure_and_verify_private_path(&directory) {
            let _ = fs::remove_dir(&directory);
            return Err(IntegrityError::Io {
                path: display_path.to_owned(),
                source,
            });
        }
        let path = directory.join("model.gguf");
        match create_snapshot_file(&path) {
            Ok(file) => {
                return Ok(PendingSnapshot {
                    file: Some(file),
                    path: Some(path),
                    directory: Some(directory),
                });
            }
            Err(source) => {
                let _ = fs::remove_dir(&directory);
                return Err(IntegrityError::Io {
                    path: display_path.to_owned(),
                    source,
                });
            }
        }
    }
    Err(IntegrityError::IdentityGuardUnsupported)
}

#[cfg(target_os = "linux")]
fn create_snapshot_directory(path: &Path) -> std::io::Result<()> {
    use std::os::unix::fs::DirBuilderExt;

    let mut builder = fs::DirBuilder::new();
    builder.mode(0o700).create(path)
}

#[cfg(target_os = "linux")]
fn create_snapshot_file(path: &Path) -> std::io::Result<File> {
    use std::os::unix::fs::OpenOptionsExt;

    OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)
}

fn open_model_nofollow(path: &Path) -> Result<File, IntegrityError> {
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
    {
        let _ = path;
        return Err(IntegrityError::IdentityGuardUnsupported);
    }
    options.open(path).map_err(|source| IntegrityError::Io {
        path: path.to_owned(),
        source,
    })
}

fn verify_regular_model_file(metadata: &fs::Metadata, _path: &Path) -> Result<(), IntegrityError> {
    #[cfg(unix)]
    if metadata.file_type().is_symlink() {
        return Err(IntegrityError::UnsafeFile);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;

        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(IntegrityError::UnsafeFile);
        }
    }
    if !metadata.is_file() {
        return Err(IntegrityError::UnsafeFile);
    }
    Ok(())
}

fn hash_open_file(file: &File, path: &Path) -> Result<[u8; 32], IntegrityError> {
    let mut reader = file.try_clone().map_err(|source| IntegrityError::Io {
        path: path.to_owned(),
        source,
    })?;
    reader
        .seek(SeekFrom::Start(0))
        .map_err(|source| IntegrityError::Io {
            path: path.to_owned(),
            source,
        })?;
    let mut reader = BufReader::new(reader);
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 1024 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|source| IntegrityError::Io {
                path: path.to_owned(),
                source,
            })?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(hasher.finalize().into())
}

#[cfg(target_os = "linux")]
fn finalize_snapshot(
    file: File,
    directory: PathBuf,
    path: PathBuf,
    display_path: &Path,
) -> Result<ImmutableSnapshot, IntegrityError> {
    use std::os::fd::AsRawFd;

    let native_path = PathBuf::from(format!("/proc/self/fd/{}", file.as_raw_fd()));
    fs::remove_file(&path).map_err(|source| IntegrityError::Io {
        path: display_path.to_owned(),
        source,
    })?;
    fs::remove_dir(&directory).map_err(|source| IntegrityError::Io {
        path: display_path.to_owned(),
        source,
    })?;
    if !native_path.exists() {
        return Err(IntegrityError::IdentityGuardUnsupported);
    }
    Ok(ImmutableSnapshot {
        file: Some(file),
        native_path,
        cleanup_path: None,
        cleanup_directory: None,
    })
}

#[cfg(unix)]
fn file_identity(
    _file: &File,
    metadata: &fs::Metadata,
    _path: &Path,
) -> Result<FileIdentity, IntegrityError> {
    use std::os::unix::fs::MetadataExt;

    Ok(FileIdentity {
        first: metadata.dev(),
        second: metadata.ino(),
    })
}

#[cfg(windows)]
fn file_identity(
    file: &File,
    _metadata: &fs::Metadata,
    path: &Path,
) -> Result<FileIdentity, IntegrityError> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: `file` owns a live handle and `information` is valid writable storage.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut information) } == 0 {
        return Err(IntegrityError::Io {
            path: path.to_owned(),
            source: std::io::Error::last_os_error(),
        });
    }
    Ok(FileIdentity {
        first: u64::from(information.dwVolumeSerialNumber),
        second: (u64::from(information.nFileIndexHigh) << 32)
            | u64::from(information.nFileIndexLow),
    })
}

#[cfg(not(any(unix, windows)))]
fn file_identity(
    _file: &File,
    _metadata: &fs::Metadata,
    _path: &Path,
) -> Result<FileIdentity, IntegrityError> {
    Err(IntegrityError::IdentityGuardUnsupported)
}

#[cfg(test)]
mod tests {
    use std::{
        cell::Cell,
        rc::Rc,
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
    };

    use super::*;

    #[cfg(target_os = "linux")]
    use std::io::Write;

    fn fixture(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join(name)
    }

    #[derive(Clone)]
    struct ManualClock(Rc<Cell<u64>>);

    impl Clock for ManualClock {
        fn now_ms(&self) -> u64 {
            self.0.get()
        }
    }

    impl ManualClock {
        fn new(now: u64) -> Self {
            Self(Rc::new(Cell::new(now)))
        }

        fn set(&self, now: u64) {
            self.0.set(now);
        }
    }

    struct DropProbe(Arc<AtomicUsize>);

    impl Drop for DropProbe {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn verified_snapshot_is_unchanged_by_source_path_replacement() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("model.gguf");
        fs::copy(fixture("tiny-cpu.gguf"), &source).unwrap();
        let verified = VerifiedModelFile::open(&source).unwrap();
        let fingerprint = verified.sha256();
        let native_path = verified.native_path().to_owned();

        fs::rename(&source, directory.path().join("displaced.gguf")).unwrap();
        fs::write(&source, b"replacement").unwrap();

        verified.verify_unchanged().unwrap();
        assert_eq!(verified.sha256(), fingerprint);
        assert_eq!(
            verified.metadata().architecture.as_deref(),
            Some("amw-test")
        );
        drop(verified);
        assert!(!native_path.exists());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn verified_snapshot_is_unchanged_by_in_place_source_mutation() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("model.gguf");
        fs::copy(fixture("tiny-cpu.gguf"), &source).unwrap();
        let verified = VerifiedModelFile::open(&source).unwrap();
        let fingerprint = verified.sha256();
        let mut source_file = OpenOptions::new().write(true).open(&source).unwrap();

        source_file.write_all(b"NOPE").unwrap();
        source_file.sync_all().unwrap();

        verified.verify_unchanged().unwrap();
        assert_eq!(verified.sha256(), fingerprint);
        assert_eq!(
            verified.metadata().architecture.as_deref(),
            Some("amw-test")
        );
    }

    #[cfg(windows)]
    #[test]
    fn verified_source_handle_blocks_mutation_without_a_temp_copy() {
        let directory = tempfile::tempdir().unwrap();
        let source = directory.path().join("model.gguf");
        fs::copy(fixture("tiny-cpu.gguf"), &source).unwrap();
        let verified = VerifiedModelFile::open(&source).unwrap();

        assert_eq!(verified.native_path(), source);
        let error = fs::write(&source, b"replacement").unwrap_err();
        assert_eq!(error.raw_os_error(), Some(32));
        verified.verify_unchanged().unwrap();
    }

    #[test]
    fn allocation_is_not_called_before_admission() {
        let clock = ManualClock::new(0);
        let ledger = MemoryLedger::new(MemoryAmount::ram(10));
        let mut loader = ModelLoader::new(clock, ledger, false);
        let called = Cell::new(false);
        assert!(matches!(
            loader.load_with("large", MemoryAmount::ram(11), KeepAlive::Default, || {
                called.set(true);
                Ok(())
            }),
            Err(LoaderError::Budget(BudgetError::RamRefused { .. }))
        ));
        assert!(!called.get());
    }

    #[test]
    fn record_is_not_opened_before_admission() {
        let directory = tempfile::tempdir().unwrap();
        let missing = directory.path().join("missing.gguf");
        let record = ModelRecord {
            id: "large".to_owned(),
            path: missing,
            aliases: Vec::new(),
            draft_pair: None,
        };
        let mut loader = ModelLoader::new(
            ManualClock::new(0),
            MemoryLedger::new(MemoryAmount::ram(10)),
            false,
        );

        let error = loader
            .load_record_with(
                &record,
                MemoryAmount::ram(11),
                KeepAlive::Default,
                |_, _| Ok(()),
            )
            .unwrap_err();

        assert!(matches!(
            error,
            LoaderError::Budget(BudgetError::RamRefused { .. })
        ));
    }

    #[cfg(not(windows))]
    #[test]
    fn record_snapshot_bytes_are_admitted_before_copy_and_allocation() {
        let path = fixture("tiny-cpu.gguf");
        let snapshot_bytes = snapshot_accounting_bytes(fs::metadata(&path).unwrap().len());
        let record = ModelRecord {
            id: "model".to_owned(),
            path,
            aliases: Vec::new(),
            draft_pair: None,
        };
        let mut loader = ModelLoader::new(
            ManualClock::new(0),
            MemoryLedger::new(MemoryAmount::ram(snapshot_bytes + 3)),
            false,
        );
        let called = Cell::new(false);

        let error = loader
            .load_record_with(&record, MemoryAmount::ram(4), KeepAlive::Never, |_, _| {
                called.set(true);
                Ok(())
            })
            .unwrap_err();

        assert!(matches!(
            error,
            LoaderError::Budget(BudgetError::RamRefused { .. })
        ));
        assert!(!called.get());
        assert_eq!(loader.available_memory().ram_bytes, snapshot_bytes + 3);
    }

    #[test]
    fn owned_resource_drops_on_transactional_unload() {
        let dropped = Arc::new(AtomicUsize::new(0));
        let mut loader = ModelLoader::new(
            ManualClock::new(0),
            MemoryLedger::new(MemoryAmount::ram(10)),
            false,
        );
        loader
            .load_resource_with("model", MemoryAmount::ram(4), KeepAlive::Never, || {
                Ok(DropProbe(dropped.clone()))
            })
            .unwrap();
        assert!(loader.resident_resource("model").unwrap().is::<DropProbe>());
        loader.unload("model").unwrap();
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
        assert_eq!(loader.available_memory().ram_bytes, 10);
    }

    #[test]
    fn failed_allocation_rolls_back_pending_ledger_reservation() {
        let mut loader = ModelLoader::new(
            ManualClock::new(0),
            MemoryLedger::new(MemoryAmount::ram(10)),
            false,
        );
        assert!(matches!(
            loader.load_resource_with::<u32>(
                "model",
                MemoryAmount::ram(4),
                KeepAlive::Never,
                || Err("native load failed".to_owned())
            ),
            Err(LoaderError::Allocation(_))
        ));
        assert!(!loader.is_loaded("model"));
        assert_eq!(loader.available_memory().ram_bytes, 10);
    }

    #[test]
    fn failed_release_keeps_resource_and_ledger_resident() {
        let mut loader = ModelLoader::new(
            ManualClock::new(0),
            MemoryLedger::new(MemoryAmount::ram(10)),
            false,
        );
        loader
            .load_resource_with("model", MemoryAmount::ram(4), KeepAlive::Never, || {
                Ok(42_u32)
            })
            .unwrap();
        assert!(matches!(
            loader.unload_with("model", |_| Err("worker refused shutdown".to_owned())),
            Err(LoaderError::Release(_))
        ));
        assert!(loader.is_loaded("model"));
        assert_eq!(loader.available_memory().ram_bytes, 6);
    }

    #[test]
    fn lru_eviction_within_budget() {
        let clock = ManualClock::new(1);
        let ledger = MemoryLedger::new(MemoryAmount::ram(10));
        let mut loader = ModelLoader::new(clock.clone(), ledger, true);
        loader
            .load_with("old", MemoryAmount::ram(6), KeepAlive::Default, || Ok(()))
            .unwrap();
        clock.set(2);
        loader
            .load_with("new", MemoryAmount::ram(6), KeepAlive::Default, || Ok(()))
            .unwrap();
        assert!(!loader.is_loaded("old"));
        assert!(loader.is_loaded("new"));
        assert_eq!(loader.available_memory().ram_bytes, 4);
    }

    #[test]
    fn keep_alive_semantics_use_injected_clock() {
        let clock = ManualClock::new(100);
        let ledger = MemoryLedger::new(MemoryAmount::ram(40));
        let mut loader = ModelLoader::new(clock.clone(), ledger, false);
        loader
            .load_with("zero", MemoryAmount::ram(10), KeepAlive::Immediate, || {
                Ok(())
            })
            .unwrap();
        loader
            .load_with(
                "duration",
                MemoryAmount::ram(10),
                KeepAlive::DurationMs(5),
                || Ok(()),
            )
            .unwrap();
        loader
            .load_with("never", MemoryAmount::ram(10), KeepAlive::Never, || Ok(()))
            .unwrap();
        assert_eq!(loader.purge_expired().unwrap(), vec!["zero"]);
        clock.set(105);
        assert_eq!(loader.purge_expired().unwrap(), vec!["duration"]);
        clock.set(u64::MAX);
        assert!(loader.purge_expired().unwrap().is_empty());
        assert!(loader.is_loaded("never"));
    }
}
