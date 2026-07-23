//! KV-cell accounting and backend state lifecycle.

use std::{
    collections::BTreeMap,
    fs::{self, File},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    time::{SystemTime, UNIX_EPOCH},
};

use sha2::{Digest, Sha256};

use crate::hw::budget::{MemoryAmount, MemoryLedger, MemoryPurpose, ReservationId};
use crate::store::session::{
    create_private_file, ensure_private_directory, open_private_file_nofollow,
    secure_file_metadata, verify_private_file, verify_secure_directory,
};

use super::{EventSink, PriorityClass, SchedError, SchedEvent};

pub type SeqId = u32;

const SESSION_MAGIC: &[u8; 8] = b"AMWKV4\0\0";
const MODEL_FINGERPRINT_BYTES: usize = 32;
const OWNER_FINGERPRINT_BYTES: usize = 32;
const SESSION_DIGEST_BYTES: usize = 32;
const MAX_SESSION_ID_BYTES: usize = 128;
const MAX_SESSIONS_PER_MODEL: u64 = 1_024;
const MAX_SESSION_BYTES: u64 = 512 * 1024 * 1024;
const MAX_SESSION_STORAGE_PER_MODEL: u64 = 2 * 1024 * 1024 * 1024;
const MAX_SESSIONS_GLOBAL: u64 = 4_096;
const MAX_SESSION_STORAGE_GLOBAL: u64 = 8 * 1024 * 1024 * 1024;
const SESSION_KIND_EMPTY: u8 = 0;
const SESSION_KIND_MATERIALIZED: u8 = 1;
const SESSION_HEADER_BYTES: usize =
    SESSION_MAGIC.len() + 1 + 4 + 1 + 4 + 4 + MODEL_FINGERPRINT_BYTES + OWNER_FINGERPRINT_BYTES + 8;
static SESSION_TEMP_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Copy)]
struct SessionQuota {
    per_session_bytes: u64,
    per_model_files: u64,
    per_model_bytes: u64,
    global_files: u64,
    global_bytes: u64,
}

impl SessionQuota {
    const DEFAULT: Self = Self {
        per_session_bytes: MAX_SESSION_BYTES,
        per_model_files: MAX_SESSIONS_PER_MODEL,
        per_model_bytes: MAX_SESSION_STORAGE_PER_MODEL,
        global_files: MAX_SESSIONS_GLOBAL,
        global_bytes: MAX_SESSION_STORAGE_GLOBAL,
    };
}

pub trait KvQuantPolicy {
    fn bytes_per_cell(&mut self, priority: PriorityClass) -> u64;
}

#[derive(Clone, Copy, Debug)]
pub struct StaticKvPolicy {
    pub bytes_per_cell: u64,
}

impl KvQuantPolicy for StaticKvPolicy {
    fn bytes_per_cell(&mut self, _priority: PriorityClass) -> u64 {
        self.bytes_per_cell
    }
}

/// Backend half of a scheduler-owned KV transaction.
pub trait SequenceBackend {
    fn copy_sequence(
        &mut self,
        source: SeqId,
        destination: SeqId,
        cells: u32,
    ) -> Result<(), SchedError>;

    fn remove_sequence(&mut self, seq_id: SeqId) -> Result<(), SchedError>;

    fn export_sequence(&mut self, seq_id: SeqId) -> Result<Vec<u8>, SchedError>;

    fn sequence_state_size(&mut self, seq_id: SeqId) -> Result<usize, SchedError> {
        self.export_sequence(seq_id).map(|state| state.len())
    }

    fn import_sequence(&mut self, seq_id: SeqId, state: &[u8]) -> Result<(), SchedError>;

    /// Returns the greatest materialized position for a sequence.
    fn sequence_position_max(&mut self, _seq_id: SeqId) -> Result<i32, SchedError> {
        Err(SchedError::Backend(
            "backend does not expose sequence positions",
        ))
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl SequenceBackend for crate::ffi::Context {
    fn copy_sequence(
        &mut self,
        source: SeqId,
        destination: SeqId,
        cells: u32,
    ) -> Result<(), SchedError> {
        if cells == 0 {
            return Err(SchedError::InvalidRequest(
                "prefix copy must contain at least one cell",
            ));
        }
        let source = i32::try_from(source)
            .map_err(|_| SchedError::InvalidRequest("source sequence exceeds native range"))?;
        let destination = i32::try_from(destination)
            .map_err(|_| SchedError::InvalidRequest("destination sequence exceeds native range"))?;
        let end = i32::try_from(cells)
            .map_err(|_| SchedError::InvalidRequest("prefix position exceeds native range"))?;
        let source_position = self
            .memory_seq_pos_max(source)
            .map_err(|_| SchedError::Backend("native source position query failed"))?;
        if source_position != end - 1 {
            return Err(SchedError::Backend(
                "native sequence copy source extent does not match requested cells",
            ));
        }
        // The pinned llama.cpp revision only supports full cross-stream KV
        // copies. Prefix sources are exact, immutable sequences, so proving
        // their extent above makes a full copy equivalent to the requested
        // prefix range without entering the native assertion path.
        self.memory_seq_cp(source, destination, -1, -1)
            .map_err(|_| SchedError::Backend("native prefix copy failed"))?;
        let position = self
            .memory_seq_pos_max(destination)
            .map_err(|_| SchedError::Backend("native prefix position query failed"))?;
        if position >= end - 1 {
            Ok(())
        } else {
            Err(SchedError::Backend(
                "native prefix copy produced no destination state",
            ))
        }
    }

    fn remove_sequence(&mut self, seq_id: SeqId) -> Result<(), SchedError> {
        let seq_id = i32::try_from(seq_id)
            .map_err(|_| SchedError::InvalidRequest("sequence exceeds native range"))?;
        if self
            .memory_seq_rm(seq_id, -1, -1)
            .map_err(|_| SchedError::Backend("native sequence removal failed"))?
        {
            Ok(())
        } else {
            Err(SchedError::Backend("native sequence removal was refused"))
        }
    }

    fn export_sequence(&mut self, seq_id: SeqId) -> Result<Vec<u8>, SchedError> {
        let seq_id = i32::try_from(seq_id)
            .map_err(|_| SchedError::InvalidRequest("sequence exceeds native range"))?;
        self.sequence_state(seq_id)
            .map_err(|_| SchedError::Backend("native sequence snapshot failed"))
    }

    fn sequence_state_size(&mut self, seq_id: SeqId) -> Result<usize, SchedError> {
        let seq_id = i32::try_from(seq_id)
            .map_err(|_| SchedError::InvalidRequest("sequence exceeds native range"))?;
        crate::ffi::Context::sequence_state_size(self, seq_id)
            .map_err(|_| SchedError::Backend("native sequence snapshot sizing failed"))
    }

    fn import_sequence(&mut self, seq_id: SeqId, state: &[u8]) -> Result<(), SchedError> {
        let seq_id = i32::try_from(seq_id)
            .map_err(|_| SchedError::InvalidRequest("sequence exceeds native range"))?;
        self.restore_sequence_state(seq_id, state)
            .map_err(|_| SchedError::Backend("native sequence restore failed"))
    }

    fn sequence_position_max(&mut self, seq_id: SeqId) -> Result<i32, SchedError> {
        let seq_id = i32::try_from(seq_id)
            .map_err(|_| SchedError::InvalidRequest("sequence exceeds native range"))?;
        self.memory_seq_pos_max(seq_id)
            .map_err(|_| SchedError::Backend("native sequence position query failed"))
    }
}

#[derive(Clone, Debug)]
struct Allocation {
    cells: u32,
    priority: PriorityClass,
    reservation: ReservationId,
    order: u64,
    evictable: bool,
    suspended: bool,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KvCopyReceipt {
    pub source_seq_id: SeqId,
    pub destination_seq_id: SeqId,
    pub copied_cells: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct KvRemoveReceipt {
    pub seq_id: SeqId,
    pub released_cells: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReadmissionReceipt {
    pub seq_id: SeqId,
    pub evicted_cells: u32,
    pub reason: ReadmissionReason,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ReadmissionReason {
    KvPressure,
}

/// Stable metadata for one sampled token that has not yet been decoded into KV state.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct SessionContinuation {
    model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES],
    last_token: i32,
    next_position: u32,
}

impl SessionContinuation {
    pub fn new(
        model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES],
        last_token: i32,
        next_position: u32,
    ) -> Result<Self, SchedError> {
        if model_fingerprint == [0; MODEL_FINGERPRINT_BYTES] {
            return Err(SchedError::InvalidRequest(
                "session model fingerprint must not be empty",
            ));
        }
        if last_token < 0 {
            return Err(SchedError::InvalidRequest(
                "session continuation token must be non-negative",
            ));
        }
        if next_position < 2 {
            return Err(SchedError::InvalidRequest(
                "session continuation requires materialized state before its pending token",
            ));
        }
        Ok(Self {
            model_fingerprint,
            last_token,
            next_position,
        })
    }

    pub fn model_fingerprint(self) -> [u8; MODEL_FINGERPRINT_BYTES] {
        self.model_fingerprint
    }

    pub fn last_token(self) -> i32 {
        self.last_token
    }

    /// Returns the next free position after the pending token is decoded.
    pub fn next_position(self) -> u32 {
        self.next_position
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KvSessionRestoreReceipt {
    pub seq_id: SeqId,
    pub cells: u32,
    pub priority: PriorityClass,
    pub continuation: SessionContinuation,
}

/// Validated identity and capacity inputs for restoring one KV session.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KvSessionRestoreOptions {
    pub expected_model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES],
    pub expected_owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    pub required_cells: u32,
}

#[derive(Debug)]
enum SessionSnapshot {
    Empty {
        model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES],
        owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    },
    Materialized {
        cells: u32,
        priority: PriorityClass,
        continuation: SessionContinuation,
        owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
        state: Vec<u8>,
    },
}

impl SessionSnapshot {
    fn model_fingerprint(&self) -> [u8; MODEL_FINGERPRINT_BYTES] {
        match self {
            Self::Empty {
                model_fingerprint,
                owner_fingerprint: _,
            } => *model_fingerprint,
            Self::Materialized { continuation, .. } => continuation.model_fingerprint(),
        }
    }

    fn owner_fingerprint(&self) -> [u8; OWNER_FINGERPRINT_BYTES] {
        match self {
            Self::Empty {
                owner_fingerprint, ..
            }
            | Self::Materialized {
                owner_fingerprint, ..
            } => *owner_fingerprint,
        }
    }
}

#[derive(Debug)]
pub struct KvManager {
    capacity_cells: u32,
    sequence_capacity: u32,
    session_dir: PathBuf,
    ledger: MemoryLedger,
    allocations: BTreeMap<SeqId, Allocation>,
    next_order: u64,
    background_evicted: u64,
}

impl KvManager {
    pub fn new(
        capacity_cells: u32,
        sequence_capacity: u32,
        session_dir: PathBuf,
        ledger: MemoryLedger,
    ) -> Result<Self, SchedError> {
        if capacity_cells == 0 {
            return Err(SchedError::InvalidRequest("KV capacity must be positive"));
        }
        if sequence_capacity == 0 || sequence_capacity > i32::MAX as u32 {
            return Err(SchedError::InvalidRequest(
                "native sequence capacity must fit the backend range",
            ));
        }
        Ok(Self {
            capacity_cells,
            sequence_capacity,
            session_dir,
            ledger,
            allocations: BTreeMap::new(),
            next_order: 0,
            background_evicted: 0,
        })
    }

    pub fn ledger_mut(&mut self) -> &mut MemoryLedger {
        &mut self.ledger
    }

    pub fn used_cells(&self) -> u32 {
        self.allocations
            .values()
            .map(|allocation| allocation.cells)
            .sum()
    }

    pub fn sequence_cells(&self, seq_id: SeqId) -> Option<u32> {
        self.allocations
            .get(&seq_id)
            .map(|allocation| allocation.cells)
    }

    pub fn sequence_priority(&self, seq_id: SeqId) -> Option<PriorityClass> {
        self.allocations
            .get(&seq_id)
            .map(|allocation| allocation.priority)
    }

    pub const fn background_evicted(&self) -> u64 {
        self.background_evicted
    }

    pub fn mark_preempted(&mut self, seq_id: SeqId, preempted: bool) -> Result<(), SchedError> {
        let allocation = self
            .allocations
            .get_mut(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if allocation.priority != PriorityClass::Background && preempted {
            return Err(SchedError::InvalidRequest(
                "only Background KV may be suspended",
            ));
        }
        allocation.suspended = preempted;
        allocation.evictable = false;
        Ok(())
    }

    /// Makes one suspended Background allocation eligible for explicit pressure eviction.
    ///
    /// Suspension alone deliberately retains native KV state. Callers must opt in at the
    /// admission boundary where an actual allocation failure can be converted into a typed
    /// readmission receipt.
    pub(crate) fn mark_pressure_evictable(&mut self, seq_id: SeqId) -> Result<(), SchedError> {
        let allocation = self
            .allocations
            .get_mut(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if allocation.priority != PriorityClass::Background || !allocation.suspended {
            return Err(SchedError::InvalidRequest(
                "only suspended Background KV may become pressure-evictable",
            ));
        }
        allocation.evictable = true;
        Ok(())
    }

    pub(crate) fn suspended_background_candidates(&self) -> Vec<SeqId> {
        let mut candidates: Vec<_> = self
            .allocations
            .iter()
            .filter(|(_, allocation)| {
                allocation.priority == PriorityClass::Background && allocation.suspended
            })
            .map(|(&seq_id, allocation)| (allocation.order, seq_id))
            .collect();
        candidates.sort_unstable();
        candidates.into_iter().map(|(_, seq_id)| seq_id).collect()
    }

    pub(crate) fn is_suspended(&self, seq_id: SeqId) -> bool {
        self.allocations
            .get(&seq_id)
            .is_some_and(|allocation| allocation.suspended)
    }

    pub fn allocate(
        &mut self,
        cells: u32,
        priority: PriorityClass,
        policy: &mut impl KvQuantPolicy,
        sink: &mut impl EventSink,
    ) -> Result<SeqId, SchedError> {
        let seq_id = self.reserve_allocation(cells, priority, policy)?;
        self.emit_occupancy(sink);
        Ok(seq_id)
    }

    /// Removes native state first, then releases the exact tracked reservation.
    pub fn remove(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<KvRemoveReceipt, SchedError> {
        let allocation = self
            .allocations
            .get(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?
            .clone();
        backend.remove_sequence(seq_id)?;
        self.release_allocation(seq_id, allocation.reservation)?;
        self.emit_occupancy(sink);
        Ok(KvRemoveReceipt {
            seq_id,
            released_cells: allocation.cells,
        })
    }

    /// Releases accounting before any backend sequence state was materialized.
    pub(crate) fn discard_allocation(
        &mut self,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<KvRemoveReceipt, SchedError> {
        let allocation = self
            .allocations
            .get(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?
            .clone();
        self.release_allocation(seq_id, allocation.reservation)?;
        self.emit_occupancy(sink);
        Ok(KvRemoveReceipt {
            seq_id,
            released_cells: allocation.cells,
        })
    }

    /// Copies backend prefix state and ledger ownership as one operation.
    pub fn copy_prefix(
        &mut self,
        backend: &mut impl SequenceBackend,
        source: SeqId,
        cells: u32,
        priority: PriorityClass,
        policy: &mut impl KvQuantPolicy,
        sink: &mut impl EventSink,
    ) -> Result<KvCopyReceipt, SchedError> {
        let source_cells = self
            .allocations
            .get(&source)
            .ok_or(SchedError::UnknownSequence(source))?
            .cells;
        if cells > source_cells {
            return Err(SchedError::InvalidRequest(
                "shared prefix exceeds source sequence",
            ));
        }
        let destination = self.reserve_allocation(cells, priority, policy)?;
        if let Err(error) = backend.copy_sequence(source, destination, cells) {
            let reservation = self.allocations[&destination].reservation;
            self.release_allocation(destination, reservation)?;
            return Err(error);
        }
        self.emit_occupancy(sink);
        Ok(KvCopyReceipt {
            source_seq_id: source,
            destination_seq_id: destination,
            copied_cells: cells,
        })
    }

    /// Copies a resident prefix into an already-accounted destination.
    pub fn copy_into(
        &mut self,
        backend: &mut impl SequenceBackend,
        source: SeqId,
        destination: SeqId,
        cells: u32,
    ) -> Result<KvCopyReceipt, SchedError> {
        if source == destination {
            return Err(SchedError::InvalidRequest(
                "prefix source and destination must differ",
            ));
        }
        let source_cells = self
            .allocations
            .get(&source)
            .ok_or(SchedError::UnknownSequence(source))?
            .cells;
        let destination_cells = self
            .allocations
            .get(&destination)
            .ok_or(SchedError::UnknownSequence(destination))?
            .cells;
        if cells == 0 || cells > source_cells || cells > destination_cells {
            return Err(SchedError::InvalidRequest(
                "shared prefix exceeds tracked sequence capacity",
            ));
        }
        backend.copy_sequence(source, destination, cells)?;
        Ok(KvCopyReceipt {
            source_seq_id: source,
            destination_seq_id: destination,
            copied_cells: cells,
        })
    }

    pub(crate) fn evict_pressure_candidate(
        &mut self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        sink: &mut impl EventSink,
    ) -> Result<ReadmissionReceipt, SchedError> {
        let allocation = self
            .allocations
            .get(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?
            .clone();
        if allocation.priority != PriorityClass::Background
            || !allocation.suspended
            || !allocation.evictable
        {
            return Err(SchedError::InvalidRequest(
                "pressure eviction requires an explicitly eligible suspended Background",
            ));
        }
        if !self.ledger.is_committed(allocation.reservation) {
            return Err(SchedError::LedgerInvariant(
                "pressure-evicted KV reservation was not committed",
            ));
        }
        if let Err(error) = backend.remove_sequence(seq_id) {
            if let Some(allocation) = self.allocations.get_mut(&seq_id) {
                allocation.evictable = false;
            }
            return Err(error);
        }
        self.release_allocation(seq_id, allocation.reservation)?;
        self.emit_occupancy(sink);
        self.background_evicted = self.background_evicted.saturating_add(1);
        sink.emit(SchedEvent::BackgroundEvicted { seq_id });
        Ok(ReadmissionReceipt {
            seq_id,
            evicted_cells: allocation.cells,
            reason: ReadmissionReason::KvPressure,
        })
    }

    /// Saves actual native sequence state, not scheduler metadata alone.
    pub fn save_session(
        &self,
        backend: &mut impl SequenceBackend,
        session_id: &str,
        seq_id: SeqId,
        continuation: SessionContinuation,
        owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    ) -> Result<PathBuf, SchedError> {
        let path = self.session_path(session_id)?;
        validate_owner_fingerprint(owner_fingerprint)?;
        let existing = self.read_session_snapshot(session_id)?;
        if existing.owner_fingerprint() != owner_fingerprint {
            return Err(SchedError::SessionUnknown(session_id.to_owned()));
        }
        let payload =
            self.export_session_payload(backend, seq_id, continuation, owner_fingerprint)?;
        self.enforce_session_quota(&path, payload.len())?;
        write_snapshot_atomic(&path, &payload)?;
        Ok(path)
    }

    /// Encodes native sequence state without choosing a durable storage location.
    pub fn export_session_payload(
        &self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
        continuation: SessionContinuation,
        owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    ) -> Result<Vec<u8>, SchedError> {
        validate_owner_fingerprint(owner_fingerprint)?;
        let allocation = self
            .allocations
            .get(&seq_id)
            .ok_or(SchedError::UnknownSequence(seq_id))?;
        if continuation.next_position() > allocation.cells {
            return Err(SchedError::InvalidRequest(
                "session continuation exceeds its KV allocation",
            ));
        }
        let position_max = backend.sequence_position_max(seq_id)?;
        let expected_position_max = i32::try_from(continuation.next_position() - 2)
            .map_err(|_| SchedError::InvalidRequest("session position exceeds native range"))?;
        if position_max != expected_position_max {
            return Err(SchedError::InvalidRequest(
                "session continuation position does not match native state",
            ));
        }
        let projected_size = self.session_payload_size(backend, seq_id)?;
        let state = backend.export_sequence(seq_id)?;
        if state.is_empty() {
            return Err(SchedError::Backend(
                "backend returned an empty sequence snapshot",
            ));
        }
        let payload = encode_snapshot(&SessionSnapshot::Materialized {
            cells: allocation.cells,
            priority: allocation.priority,
            continuation,
            owner_fingerprint,
            state,
        })?;
        if payload.len() as u64 != projected_size {
            return Err(SchedError::Io(
                "native session state size changed during export".to_owned(),
            ));
        }
        Ok(payload)
    }

    /// Returns the exact encoded payload size before exporting native bytes.
    pub fn session_payload_size(
        &self,
        backend: &mut impl SequenceBackend,
        seq_id: SeqId,
    ) -> Result<u64, SchedError> {
        if !self.allocations.contains_key(&seq_id) {
            return Err(SchedError::UnknownSequence(seq_id));
        }
        let state_size = backend.sequence_state_size(seq_id)?;
        let projected_size = SESSION_HEADER_BYTES
            .checked_add(state_size)
            .and_then(|size| size.checked_add(SESSION_DIGEST_BYTES))
            .ok_or_else(|| SchedError::Io("sequence snapshot size overflow".to_owned()))?;
        let projected_size = u64::try_from(projected_size)
            .map_err(|_| SchedError::Io("sequence snapshot exceeds platform size".to_owned()))?;
        if projected_size > MAX_SESSION_BYTES {
            return Err(SchedError::Io(
                "session snapshot exceeds the per-session byte quota".to_owned(),
            ));
        }
        Ok(projected_size)
    }

    /// Persists an idempotent empty session before any generation state exists.
    pub fn create_session(
        &self,
        session_id: &str,
        model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES],
        owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    ) -> Result<PathBuf, SchedError> {
        if model_fingerprint == [0; MODEL_FINGERPRINT_BYTES] {
            return Err(SchedError::InvalidRequest(
                "session model fingerprint must not be empty",
            ));
        }
        validate_owner_fingerprint(owner_fingerprint)?;
        let path = self.session_path(session_id)?;
        match read_snapshot_nofollow(&path) {
            Ok(payload) => {
                let snapshot = decode_snapshot(&payload)?;
                if snapshot.owner_fingerprint() != owner_fingerprint {
                    return Err(SchedError::SessionUnknown(session_id.to_owned()));
                }
                if snapshot.model_fingerprint() != model_fingerprint {
                    return Err(SchedError::InvalidRequest(
                        "session model fingerprint does not match the loaded model",
                    ));
                }
                return Ok(path);
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(SchedError::Io(error.to_string())),
        }
        ensure_private_directory(&self.session_dir)
            .map_err(|error| SchedError::Io(error.to_string()))?;
        let payload = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint,
            owner_fingerprint,
        })?;
        self.enforce_session_quota(&path, payload.len())?;
        match write_snapshot_create_atomic(&path, &payload) {
            Ok(()) => Ok(path),
            Err(SchedError::Io(_)) if path.is_file() => {
                let snapshot = self.read_session_snapshot(session_id)?;
                if snapshot.owner_fingerprint() != owner_fingerprint {
                    return Err(SchedError::SessionUnknown(session_id.to_owned()));
                }
                if snapshot.model_fingerprint() == model_fingerprint {
                    Ok(path)
                } else {
                    Err(SchedError::InvalidRequest(
                        "session model fingerprint does not match the loaded model",
                    ))
                }
            }
            Err(error) => Err(error),
        }
    }

    pub fn session_has_state(&self, session_id: &str) -> Result<bool, SchedError> {
        self.read_session_snapshot(session_id)
            .map(|snapshot| matches!(snapshot, SessionSnapshot::Materialized { .. }))
    }

    pub fn session_owner_fingerprint(
        &self,
        session_id: &str,
    ) -> Result<[u8; OWNER_FINGERPRINT_BYTES], SchedError> {
        self.read_session_snapshot(session_id)
            .map(|snapshot| snapshot.owner_fingerprint())
    }

    pub fn delete_session(
        &self,
        session_id: &str,
        owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    ) -> Result<(), SchedError> {
        validate_owner_fingerprint(owner_fingerprint)?;
        let snapshot = self.read_session_snapshot(session_id)?;
        if snapshot.owner_fingerprint() != owner_fingerprint {
            return Err(SchedError::SessionUnknown("session".to_owned()));
        }
        let path = self.session_path(session_id)?;
        let metadata = secure_file_metadata(&path)
            .map_err(|_| SchedError::SessionUnknown(session_id.to_owned()))?;
        debug_assert!(metadata.is_file());
        fs::remove_file(&path).map_err(|error| SchedError::Io(error.to_string()))?;
        sync_snapshot_directory(&path)
    }

    /// Restores backend state and rolls back its ledger allocation on failure.
    pub fn restore_session(
        &mut self,
        backend: &mut impl SequenceBackend,
        session_id: &str,
        options: KvSessionRestoreOptions,
        policy: &mut impl KvQuantPolicy,
        sink: &mut impl EventSink,
    ) -> Result<KvSessionRestoreReceipt, SchedError> {
        let path = self.session_path(session_id)?;
        let payload = read_snapshot_nofollow(&path)
            .map_err(|_| SchedError::SessionUnknown(session_id.to_owned()))?;
        self.restore_session_payload(backend, &payload, options, policy, sink)
    }

    /// Restores an opaque snapshot supplied by the process-wide session store.
    pub fn restore_session_payload(
        &mut self,
        backend: &mut impl SequenceBackend,
        payload: &[u8],
        options: KvSessionRestoreOptions,
        policy: &mut impl KvQuantPolicy,
        sink: &mut impl EventSink,
    ) -> Result<KvSessionRestoreReceipt, SchedError> {
        let snapshot = decode_snapshot(payload)?;
        let SessionSnapshot::Materialized {
            cells: saved_cells,
            priority,
            continuation,
            owner_fingerprint,
            state,
        } = snapshot
        else {
            return Err(SchedError::SessionUnknown("session".to_owned()));
        };
        if continuation.model_fingerprint() != options.expected_model_fingerprint {
            return Err(SchedError::InvalidRequest(
                "session model fingerprint does not match the loaded model",
            ));
        }
        if owner_fingerprint != options.expected_owner_fingerprint {
            return Err(SchedError::SessionUnknown("session".to_owned()));
        }
        if options.required_cells < continuation.next_position() {
            return Err(SchedError::InvalidRequest(
                "session restore allocation cannot contain saved positions",
            ));
        }
        let cells = saved_cells.max(options.required_cells);
        let seq_id = self.reserve_allocation(cells, priority, policy)?;
        if let Err(error) = backend.import_sequence(seq_id, &state) {
            let reservation = self.allocations[&seq_id].reservation;
            let _ = backend.remove_sequence(seq_id);
            self.release_allocation(seq_id, reservation)?;
            return Err(error);
        }
        let expected_position_max = i32::try_from(continuation.next_position() - 2)
            .map_err(|_| SchedError::InvalidRequest("session position exceeds native range"))?;
        let restored_position_max = match backend.sequence_position_max(seq_id) {
            Ok(position) => position,
            Err(error) => {
                let reservation = self.allocations[&seq_id].reservation;
                let cleanup_result = backend.remove_sequence(seq_id);
                self.release_allocation(seq_id, reservation)?;
                cleanup_result?;
                return Err(error);
            }
        };
        if restored_position_max != expected_position_max {
            let reservation = self.allocations[&seq_id].reservation;
            let cleanup_result = backend.remove_sequence(seq_id);
            self.release_allocation(seq_id, reservation)?;
            cleanup_result?;
            return Err(SchedError::Backend(
                "restored sequence position does not match session continuation",
            ));
        }
        self.emit_occupancy(sink);
        Ok(KvSessionRestoreReceipt {
            seq_id,
            cells,
            priority,
            continuation,
        })
    }

    /// Reads continuation metadata from an opaque materialized snapshot.
    pub fn session_payload_continuation(
        payload: &[u8],
        expected_model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES],
        expected_owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
    ) -> Result<(PriorityClass, SessionContinuation), SchedError> {
        let snapshot = decode_snapshot(payload)?;
        let SessionSnapshot::Materialized {
            priority,
            continuation,
            owner_fingerprint,
            ..
        } = snapshot
        else {
            return Err(SchedError::SessionUnknown("session".to_owned()));
        };
        if continuation.model_fingerprint() != expected_model_fingerprint {
            return Err(SchedError::InvalidRequest(
                "session model fingerprint does not match the loaded model",
            ));
        }
        if owner_fingerprint != expected_owner_fingerprint {
            return Err(SchedError::SessionUnknown("session".to_owned()));
        }
        Ok((priority, continuation))
    }

    pub fn session_priority(&self, session_id: &str) -> Result<PriorityClass, SchedError> {
        match self.read_session_snapshot(session_id)? {
            SessionSnapshot::Materialized { priority, .. } => Ok(priority),
            SessionSnapshot::Empty { .. } => Err(SchedError::SessionUnknown(session_id.to_owned())),
        }
    }

    pub fn session_continuation(
        &self,
        session_id: &str,
    ) -> Result<SessionContinuation, SchedError> {
        match self.read_session_snapshot(session_id)? {
            SessionSnapshot::Materialized { continuation, .. } => Ok(continuation),
            SessionSnapshot::Empty { .. } => Err(SchedError::SessionUnknown(session_id.to_owned())),
        }
    }

    pub fn session_ids(&self) -> Result<Vec<String>, SchedError> {
        match fs::symlink_metadata(&self.session_dir) {
            Ok(_) => verify_secure_directory(&self.session_dir)
                .map_err(|error| SchedError::Io(error.to_string()))?,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
            Err(error) => return Err(SchedError::Io(error.to_string())),
        }
        let entries =
            fs::read_dir(&self.session_dir).map_err(|error| SchedError::Io(error.to_string()))?;
        let mut ids = Vec::new();
        for entry in entries {
            let entry = entry.map_err(|error| SchedError::Io(error.to_string()))?;
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) != Some("amwkv") {
                continue;
            }
            let file_type = entry
                .file_type()
                .map_err(|error| SchedError::Io(error.to_string()))?;
            if file_type.is_symlink() {
                return Err(SchedError::Io(
                    "session directory contains a symbolic-link snapshot".to_owned(),
                ));
            }
            if !file_type.is_file() {
                continue;
            }
            secure_file_metadata(&path).map_err(|error| SchedError::Io(error.to_string()))?;
            let id = path
                .file_stem()
                .and_then(|value| value.to_str())
                .ok_or_else(|| SchedError::Io("session filename is not UTF-8".to_owned()))?;
            self.session_path(id)?;
            ids.push(id.to_owned());
        }
        ids.sort();
        Ok(ids)
    }

    fn reserve_allocation(
        &mut self,
        cells: u32,
        priority: PriorityClass,
        policy: &mut impl KvQuantPolicy,
    ) -> Result<SeqId, SchedError> {
        if cells == 0 || cells > self.capacity_cells.saturating_sub(self.used_cells()) {
            return Err(SchedError::Oom {
                requested_bytes: u64::from(cells),
            });
        }
        let seq_id = (0..self.sequence_capacity)
            .find(|seq_id| !self.allocations.contains_key(seq_id))
            .ok_or(SchedError::QuotaFull { priority })?;
        let bytes_per_cell = policy.bytes_per_cell(priority);
        if bytes_per_cell == 0 {
            return Err(SchedError::InvalidRequest(
                "KV policy bytes per cell must be positive",
            ));
        }
        let requested_bytes =
            u64::from(cells)
                .checked_mul(bytes_per_cell)
                .ok_or(SchedError::Oom {
                    requested_bytes: u64::MAX,
                })?;
        let reservation = self
            .ledger
            .reserve(MemoryPurpose::KvCache, MemoryAmount::ram(requested_bytes))
            .map_err(|_| SchedError::Oom { requested_bytes })?;
        if self.ledger.commit(reservation).is_err() {
            let _ = self.ledger.release(reservation);
            return Err(SchedError::Oom { requested_bytes });
        }
        let order = self.next_order;
        self.next_order = self.next_order.saturating_add(1);
        self.allocations.insert(
            seq_id,
            Allocation {
                cells,
                priority,
                reservation,
                order,
                evictable: false,
                suspended: false,
            },
        );
        Ok(seq_id)
    }

    fn release_allocation(
        &mut self,
        seq_id: SeqId,
        reservation: ReservationId,
    ) -> Result<(), SchedError> {
        self.ledger
            .release(reservation)
            .map_err(|_| SchedError::LedgerInvariant("KV reservation was not live"))?;
        self.allocations.remove(&seq_id);
        Ok(())
    }

    fn session_path(&self, session_id: &str) -> Result<PathBuf, SchedError> {
        if session_id.is_empty()
            || session_id.len() > MAX_SESSION_ID_BYTES
            || !session_id
                .bytes()
                .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
        {
            return Err(SchedError::SessionUnknown(session_id.to_owned()));
        }
        Ok(self.session_dir.join(format!("{session_id}.amwkv")))
    }

    fn read_session_snapshot(&self, session_id: &str) -> Result<SessionSnapshot, SchedError> {
        let path = self.session_path(session_id)?;
        let payload = read_snapshot_nofollow(&path)
            .map_err(|_| SchedError::SessionUnknown(session_id.to_owned()))?;
        decode_snapshot(&payload).map_err(|_| SchedError::SessionUnknown(session_id.to_owned()))
    }

    fn enforce_session_quota(&self, path: &Path, payload_len: usize) -> Result<(), SchedError> {
        self.enforce_session_quota_with(path, payload_len, SessionQuota::DEFAULT)
    }

    fn enforce_session_quota_with(
        &self,
        path: &Path,
        payload_len: usize,
        quota: SessionQuota,
    ) -> Result<(), SchedError> {
        let payload_len = u64::try_from(payload_len)
            .map_err(|_| SchedError::Io("session snapshot exceeds platform size".to_owned()))?;
        if payload_len > quota.per_session_bytes {
            return Err(SchedError::Io(
                "session snapshot exceeds the per-session byte quota".to_owned(),
            ));
        }
        let existing_len = match secure_file_metadata(path) {
            Ok(metadata) => metadata.len(),
            Err(error) if error.kind() == io::ErrorKind::NotFound => 0,
            Err(error) => return Err(SchedError::Io(error.to_string())),
        };
        let per_model = session_storage_usage(&self.session_dir, false)?;
        let global_root = self
            .session_dir
            .parent()
            .unwrap_or(self.session_dir.as_path());
        let global = session_storage_usage(global_root, true)?;
        let new_file = u64::from(existing_len == 0);
        let per_model_count = per_model.files.saturating_add(new_file);
        let global_count = global.files.saturating_add(new_file);
        if per_model_count > quota.per_model_files || global_count > quota.global_files {
            return Err(SchedError::Io(
                "session count quota is exhausted".to_owned(),
            ));
        }
        let per_model_bytes = per_model
            .bytes
            .saturating_sub(existing_len)
            .saturating_add(payload_len);
        let global_bytes = global
            .bytes
            .saturating_sub(existing_len)
            .saturating_add(payload_len);
        if per_model_bytes > quota.per_model_bytes || global_bytes > quota.global_bytes {
            return Err(SchedError::Io(
                "session storage byte quota is exhausted".to_owned(),
            ));
        }
        Ok(())
    }

    fn emit_occupancy(&self, sink: &mut impl EventSink) {
        sink.emit(SchedEvent::KvOccupancy {
            kv_occupancy_pct: f64::from(self.used_cells()) / f64::from(self.capacity_cells),
        });
    }
}

#[derive(Clone, Copy, Debug, Default)]
struct SessionStorageUsage {
    files: u64,
    bytes: u64,
}

fn session_storage_usage(root: &Path, recursive: bool) -> Result<SessionStorageUsage, SchedError> {
    match fs::symlink_metadata(root) {
        Ok(_) => {
            verify_secure_directory(root).map_err(|error| SchedError::Io(error.to_string()))?
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(SessionStorageUsage::default())
        }
        Err(error) => return Err(SchedError::Io(error.to_string())),
    }
    let mut usage = SessionStorageUsage::default();
    let entries = fs::read_dir(root).map_err(|error| SchedError::Io(error.to_string()))?;
    for entry in entries {
        let entry = entry.map_err(|error| SchedError::Io(error.to_string()))?;
        let path = entry.path();
        let file_type = entry
            .file_type()
            .map_err(|error| SchedError::Io(error.to_string()))?;
        if file_type.is_symlink() {
            if path.extension().and_then(|value| value.to_str()) == Some("amwkv")
                || (recursive && path.extension().is_none())
            {
                return Err(SchedError::Io(
                    "session storage must not contain symbolic links".to_owned(),
                ));
            }
            continue;
        }
        if recursive && file_type.is_dir() {
            if !is_model_session_namespace(&entry.file_name()) {
                continue;
            }
            let nested = session_storage_usage(&path, false)?;
            usage.files = usage.files.saturating_add(nested.files);
            usage.bytes = usage.bytes.saturating_add(nested.bytes);
            continue;
        }
        if file_type.is_file() && path.extension().and_then(|value| value.to_str()) == Some("amwkv")
        {
            let metadata =
                secure_file_metadata(&path).map_err(|error| SchedError::Io(error.to_string()))?;
            usage.files = usage.files.saturating_add(1);
            usage.bytes = usage.bytes.saturating_add(metadata.len());
        }
    }
    Ok(usage)
}

fn is_model_session_namespace(name: &std::ffi::OsStr) -> bool {
    name.to_str().is_some_and(|value| {
        value.len() == 64
            && value
                .bytes()
                .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    })
}

fn read_snapshot_nofollow(path: &Path) -> io::Result<Vec<u8>> {
    read_snapshot_nofollow_with(path, MAX_SESSION_BYTES, || Ok(()))
}

fn read_snapshot_nofollow_with(
    path: &Path,
    max_bytes: u64,
    after_open: impl FnOnce() -> io::Result<()>,
) -> io::Result<Vec<u8>> {
    let file = open_private_file_nofollow(path)?;
    let opened = file.metadata()?;
    if !opened.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "session snapshot changed before it could be opened",
        ));
    }
    let capacity = usize::try_from(opened.len()).map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            "session snapshot exceeds platform size",
        )
    })?;
    if opened.len() > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "session snapshot exceeds the per-session byte quota",
        ));
    }
    after_open()?;
    let mut payload = Vec::with_capacity(capacity);
    file.take(max_bytes.saturating_add(1))
        .read_to_end(&mut payload)?;
    if payload.len() as u64 > max_bytes {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "session snapshot exceeded the per-session byte quota while reading",
        ));
    }
    Ok(payload)
}

fn encode_snapshot(snapshot: &SessionSnapshot) -> Result<Vec<u8>, SchedError> {
    let (
        kind,
        cells,
        priority,
        last_token,
        next_position,
        model_fingerprint,
        owner_fingerprint,
        state,
    ) = match snapshot {
        SessionSnapshot::Empty {
            model_fingerprint,
            owner_fingerprint,
        } => (
            SESSION_KIND_EMPTY,
            0,
            PriorityClass::Interactive,
            -1,
            0,
            *model_fingerprint,
            *owner_fingerprint,
            &[][..],
        ),
        SessionSnapshot::Materialized {
            cells,
            priority,
            continuation,
            owner_fingerprint,
            state,
        } => (
            SESSION_KIND_MATERIALIZED,
            *cells,
            *priority,
            continuation.last_token(),
            continuation.next_position(),
            continuation.model_fingerprint(),
            *owner_fingerprint,
            state.as_slice(),
        ),
    };
    let state_len = u64::try_from(state.len())
        .map_err(|_| SchedError::Io("sequence snapshot exceeds file format".to_owned()))?;
    let mut payload = Vec::with_capacity(
        SESSION_HEADER_BYTES
            .checked_add(state.len())
            .and_then(|size| size.checked_add(SESSION_DIGEST_BYTES))
            .ok_or_else(|| SchedError::Io("sequence snapshot size overflow".to_owned()))?,
    );
    payload.extend_from_slice(SESSION_MAGIC);
    payload.push(kind);
    payload.extend_from_slice(&cells.to_le_bytes());
    payload.push(priority_tag(priority));
    payload.extend_from_slice(&last_token.to_le_bytes());
    payload.extend_from_slice(&next_position.to_le_bytes());
    payload.extend_from_slice(&model_fingerprint);
    payload.extend_from_slice(&owner_fingerprint);
    payload.extend_from_slice(&state_len.to_le_bytes());
    payload.extend_from_slice(state);
    let digest = Sha256::digest(&payload);
    payload.extend_from_slice(&digest);
    Ok(payload)
}

fn decode_snapshot(payload: &[u8]) -> Result<SessionSnapshot, SchedError> {
    if payload.len() < SESSION_HEADER_BYTES + SESSION_DIGEST_BYTES
        || &payload[..SESSION_MAGIC.len()] != SESSION_MAGIC
    {
        return Err(SchedError::Io("invalid KV session header".to_owned()));
    }
    let content_len = payload.len() - SESSION_DIGEST_BYTES;
    let expected_digest = Sha256::digest(&payload[..content_len]);
    if &payload[content_len..] != expected_digest.as_slice() {
        return Err(SchedError::Io(
            "KV session digest does not match file contents".to_owned(),
        ));
    }
    let mut offset = SESSION_MAGIC.len();
    let kind = payload[offset];
    offset += 1;
    let cells = u32::from_le_bytes(
        payload[offset..offset + 4]
            .try_into()
            .map_err(|_| SchedError::Io("invalid KV session cells".to_owned()))?,
    );
    offset += 4;
    let priority = priority_from_tag(payload[offset])?;
    offset += 1;
    let last_token = i32::from_le_bytes(
        payload[offset..offset + 4]
            .try_into()
            .map_err(|_| SchedError::Io("invalid KV session continuation token".to_owned()))?,
    );
    offset += 4;
    let next_position = u32::from_le_bytes(
        payload[offset..offset + 4]
            .try_into()
            .map_err(|_| SchedError::Io("invalid KV session continuation position".to_owned()))?,
    );
    offset += 4;
    let model_fingerprint: [u8; MODEL_FINGERPRINT_BYTES] = payload
        [offset..offset + MODEL_FINGERPRINT_BYTES]
        .try_into()
        .map_err(|_| SchedError::Io("invalid KV session model fingerprint".to_owned()))?;
    offset += MODEL_FINGERPRINT_BYTES;
    let owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES] = payload
        [offset..offset + OWNER_FINGERPRINT_BYTES]
        .try_into()
        .map_err(|_| SchedError::Io("invalid KV session owner fingerprint".to_owned()))?;
    offset += OWNER_FINGERPRINT_BYTES;
    validate_owner_fingerprint(owner_fingerprint)
        .map_err(|error| SchedError::Io(error.to_string()))?;
    let state_len = u64::from_le_bytes(
        payload[offset..offset + 8]
            .try_into()
            .map_err(|_| SchedError::Io("invalid KV session length".to_owned()))?,
    );
    offset += 8;
    let state_len = usize::try_from(state_len)
        .map_err(|_| SchedError::Io("KV session length exceeds platform".to_owned()))?;
    if content_len.checked_sub(offset) != Some(state_len) {
        return Err(SchedError::Io(
            "KV session state length does not match file".to_owned(),
        ));
    }
    match kind {
        SESSION_KIND_EMPTY => {
            if cells != 0 || last_token != -1 || next_position != 0 || state_len != 0 {
                return Err(SchedError::Io(
                    "empty KV session has materialized state fields".to_owned(),
                ));
            }
            if model_fingerprint == [0; MODEL_FINGERPRINT_BYTES] {
                return Err(SchedError::Io(
                    "session model fingerprint is empty".to_owned(),
                ));
            }
            Ok(SessionSnapshot::Empty {
                model_fingerprint,
                owner_fingerprint,
            })
        }
        SESSION_KIND_MATERIALIZED => {
            if cells == 0 || state_len == 0 {
                return Err(SchedError::Io("KV session payload is empty".to_owned()));
            }
            let continuation =
                SessionContinuation::new(model_fingerprint, last_token, next_position)
                    .map_err(|error| SchedError::Io(error.to_string()))?;
            if continuation.next_position() > cells {
                return Err(SchedError::Io(
                    "KV session continuation exceeds saved allocation".to_owned(),
                ));
            }
            Ok(SessionSnapshot::Materialized {
                cells,
                priority,
                continuation,
                owner_fingerprint,
                state: payload[offset..content_len].to_vec(),
            })
        }
        _ => Err(SchedError::Io(
            "KV session representation tag is invalid".to_owned(),
        )),
    }
}

fn validate_owner_fingerprint(
    owner_fingerprint: [u8; OWNER_FINGERPRINT_BYTES],
) -> Result<(), SchedError> {
    if owner_fingerprint == [0; OWNER_FINGERPRINT_BYTES] {
        Err(SchedError::InvalidRequest(
            "session owner fingerprint must not be empty",
        ))
    } else {
        Ok(())
    }
}

fn write_snapshot_atomic(path: &Path, payload: &[u8]) -> Result<(), SchedError> {
    write_snapshot_atomic_with(path, payload, || Ok(()))
}

fn write_snapshot_atomic_with(
    path: &Path,
    payload: &[u8],
    before_replace: impl FnOnce() -> Result<(), SchedError>,
) -> Result<(), SchedError> {
    write_snapshot_atomic_with_verifier(path, payload, before_replace, |installed| {
        verify_private_file(installed).map_err(|error| SchedError::Io(error.to_string()))
    })
}

fn write_snapshot_atomic_with_verifier(
    path: &Path,
    payload: &[u8],
    before_replace: impl FnOnce() -> Result<(), SchedError>,
    verify_installed: impl FnOnce(&Path) -> Result<(), SchedError>,
) -> Result<(), SchedError> {
    let (temporary_path, mut temporary) = create_snapshot_temp(path)?;
    let backup_path = snapshot_transaction_path(path, "backup");
    let had_prior = match fs::symlink_metadata(path) {
        Ok(_) => true,
        Err(error) if error.kind() == io::ErrorKind::NotFound => false,
        Err(error) => return Err(SchedError::Io(error.to_string())),
    };
    let result = (|| {
        temporary
            .write_all(payload)
            .and_then(|()| temporary.flush())
            .and_then(|()| temporary.sync_all())
            .map_err(|error| SchedError::Io(error.to_string()))?;
        before_replace()?;
        drop(temporary);
        if had_prior {
            fs::rename(path, &backup_path).map_err(|error| SchedError::Io(error.to_string()))?;
        }
        if let Err(error) = replace_snapshot(&temporary_path, path) {
            if had_prior {
                let _ = fs::rename(&backup_path, path);
            }
            return Err(error);
        }
        if let Err(error) = verify_installed(path) {
            if !rollback_snapshot_install(path, had_prior.then_some(backup_path.as_path())) {
                return Err(SchedError::Io(format!(
                    "{error}; installed snapshot rollback failed"
                )));
            }
            return Err(error);
        }
        if let Err(error) = sync_snapshot_directory(path) {
            if !rollback_snapshot_install(path, had_prior.then_some(backup_path.as_path())) {
                return Err(SchedError::Io(format!(
                    "{error}; installed snapshot rollback failed"
                )));
            }
            return Err(error);
        }
        if had_prior {
            fs::remove_file(&backup_path).map_err(|error| SchedError::Io(error.to_string()))?;
            sync_snapshot_directory(path)?;
        }
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary_path);
    }
    result
}

fn write_snapshot_create_atomic(path: &Path, payload: &[u8]) -> Result<(), SchedError> {
    write_snapshot_create_atomic_with_verifier(path, payload, |installed| {
        verify_private_file(installed).map_err(|error| SchedError::Io(error.to_string()))
    })
}

fn write_snapshot_create_atomic_with_verifier(
    path: &Path,
    payload: &[u8],
    verify_installed: impl FnOnce(&Path) -> Result<(), SchedError>,
) -> Result<(), SchedError> {
    let (temporary_path, mut temporary) = create_snapshot_temp(path)?;
    let result = (|| {
        temporary
            .write_all(payload)
            .and_then(|()| temporary.flush())
            .and_then(|()| temporary.sync_all())
            .map_err(|error| SchedError::Io(error.to_string()))?;
        drop(temporary);
        install_new_snapshot(&temporary_path, path)?;
        if let Err(error) = verify_installed(path) {
            if !rollback_snapshot_install(path, None) {
                return Err(SchedError::Io(format!(
                    "{error}; installed snapshot cleanup failed"
                )));
            }
            return Err(error);
        }
        sync_snapshot_directory(path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary_path);
    }
    result
}

fn snapshot_transaction_path(path: &Path, role: &str) -> PathBuf {
    let sequence = SESSION_TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let stem = path
        .file_name()
        .and_then(|value| value.to_str())
        .unwrap_or("session.amwkv");
    path.with_file_name(format!(
        ".{stem}.{}.{}.{}",
        std::process::id(),
        sequence,
        role
    ))
}

fn rollback_snapshot_install(path: &Path, backup: Option<&Path>) -> bool {
    let quarantine = snapshot_transaction_path(path, "unsafe");
    if fs::rename(path, &quarantine).is_err() {
        return false;
    }
    if let Some(backup) = backup {
        if fs::rename(backup, path).is_err() {
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

#[cfg(windows)]
fn install_new_snapshot(temporary_path: &Path, path: &Path) -> Result<(), SchedError> {
    fs::rename(temporary_path, path).map_err(|error| SchedError::Io(error.to_string()))
}

#[cfg(not(windows))]
fn install_new_snapshot(temporary_path: &Path, path: &Path) -> Result<(), SchedError> {
    fs::hard_link(temporary_path, path).map_err(|error| SchedError::Io(error.to_string()))?;
    fs::remove_file(temporary_path).map_err(|error| SchedError::Io(error.to_string()))
}

fn create_snapshot_temp(path: &Path) -> Result<(PathBuf, File), SchedError> {
    let directory = path
        .parent()
        .ok_or_else(|| SchedError::Io("session path has no parent directory".to_owned()))?;
    let stem = path
        .file_name()
        .and_then(|value| value.to_str())
        .ok_or_else(|| SchedError::Io("session filename is not UTF-8".to_owned()))?;
    for _ in 0..128 {
        let sequence = SESSION_TEMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let nonce = Sha256::digest(
            format!("{}:{sequence}:{timestamp}:{stem}", std::process::id()).as_bytes(),
        );
        let suffix = nonce[..16]
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect::<String>();
        let temporary_path = directory.join(format!(".{stem}.{suffix}.tmp"));
        match create_private_file(&temporary_path) {
            Ok(file) => return Ok((temporary_path, file)),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(SchedError::Io(error.to_string())),
        }
    }
    Err(SchedError::Io(
        "could not allocate a collision-free session temporary file".to_owned(),
    ))
}

#[cfg(not(windows))]
fn replace_snapshot(temporary_path: &Path, path: &Path) -> Result<(), SchedError> {
    fs::rename(temporary_path, path).map_err(|error| SchedError::Io(error.to_string()))
}

#[cfg(windows)]
fn replace_snapshot(temporary_path: &Path, path: &Path) -> Result<(), SchedError> {
    use std::{ffi::OsStr, os::windows::ffi::OsStrExt};

    const REPLACEFILE_WRITE_THROUGH: u32 = 0x0000_0001;
    #[link(name = "kernel32")]
    unsafe extern "system" {
        fn ReplaceFileW(
            replaced_file_name: *const u16,
            replacement_file_name: *const u16,
            backup_file_name: *const u16,
            replace_flags: u32,
            exclude: *mut std::ffi::c_void,
            reserved: *mut std::ffi::c_void,
        ) -> i32;
    }

    if !path.exists() {
        return fs::rename(temporary_path, path).map_err(|error| SchedError::Io(error.to_string()));
    }
    fn wide(path: &Path) -> Result<Vec<u16>, SchedError> {
        let mut value = OsStr::new(path.as_os_str())
            .encode_wide()
            .collect::<Vec<_>>();
        if value.contains(&0) {
            return Err(SchedError::Io(
                "session path contains an interior NUL".to_owned(),
            ));
        }
        value.push(0);
        Ok(value)
    }
    let replaced = wide(path)?;
    let replacement = wide(temporary_path)?;
    // SAFETY: both path buffers are NUL-terminated and live for the call. Null optional
    // arguments request an atomic replacement without a separately named backup file.
    let result = unsafe {
        ReplaceFileW(
            replaced.as_ptr(),
            replacement.as_ptr(),
            std::ptr::null(),
            REPLACEFILE_WRITE_THROUGH,
            std::ptr::null_mut(),
            std::ptr::null_mut(),
        )
    };
    if result == 0 {
        Err(SchedError::Io(io::Error::last_os_error().to_string()))
    } else {
        Ok(())
    }
}

#[cfg(not(windows))]
fn sync_snapshot_directory(path: &Path) -> Result<(), SchedError> {
    let directory = path
        .parent()
        .ok_or_else(|| SchedError::Io("session path has no parent directory".to_owned()))?;
    File::open(directory)
        .and_then(|file| file.sync_all())
        .map_err(|error| SchedError::Io(error.to_string()))
}

#[cfg(windows)]
fn sync_snapshot_directory(_path: &Path) -> Result<(), SchedError> {
    // ReplaceFileW with REPLACEFILE_WRITE_THROUGH provides the available Windows durability
    // boundary; standard Rust cannot open a directory handle for FlushFileBuffers.
    Ok(())
}

const fn priority_tag(priority: PriorityClass) -> u8 {
    match priority {
        PriorityClass::InteractiveBlocking => 0,
        PriorityClass::Interactive => 1,
        PriorityClass::Worker => 2,
        PriorityClass::Eval => 3,
        PriorityClass::Background => 4,
    }
}

fn priority_from_tag(tag: u8) -> Result<PriorityClass, SchedError> {
    match tag {
        0 => Ok(PriorityClass::InteractiveBlocking),
        1 => Ok(PriorityClass::Interactive),
        2 => Ok(PriorityClass::Worker),
        3 => Ok(PriorityClass::Eval),
        4 => Ok(PriorityClass::Background),
        _ => Err(SchedError::Io(
            "KV session priority tag is invalid".to_owned(),
        )),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[derive(Default)]
    struct FakeBackend {
        state: BTreeMap<SeqId, Vec<u8>>,
        position_max: BTreeMap<SeqId, i32>,
        fail_copy: bool,
        fail_import: bool,
        fail_remove: bool,
        reported_state_size: Option<usize>,
        export_calls: usize,
    }

    impl SequenceBackend for FakeBackend {
        fn copy_sequence(
            &mut self,
            source: SeqId,
            destination: SeqId,
            _cells: u32,
        ) -> Result<(), SchedError> {
            if self.fail_copy {
                return Err(SchedError::Backend("injected copy failure"));
            }
            let state = self
                .state
                .get(&source)
                .cloned()
                .unwrap_or_else(|| vec![source as u8]);
            self.state.insert(destination, state);
            Ok(())
        }

        fn remove_sequence(&mut self, seq_id: SeqId) -> Result<(), SchedError> {
            if self.fail_remove {
                return Err(SchedError::Backend("injected remove failure"));
            }
            self.state.remove(&seq_id);
            self.position_max.remove(&seq_id);
            Ok(())
        }

        fn export_sequence(&mut self, seq_id: SeqId) -> Result<Vec<u8>, SchedError> {
            self.export_calls += 1;
            Ok(self
                .state
                .get(&seq_id)
                .cloned()
                .unwrap_or_else(|| vec![seq_id as u8]))
        }

        fn sequence_state_size(&mut self, seq_id: SeqId) -> Result<usize, SchedError> {
            Ok(self
                .reported_state_size
                .unwrap_or_else(|| self.state.get(&seq_id).map_or(1, |state| state.len())))
        }

        fn import_sequence(&mut self, seq_id: SeqId, state: &[u8]) -> Result<(), SchedError> {
            if self.fail_import {
                return Err(SchedError::Backend("injected import failure"));
            }
            self.state.insert(seq_id, state.to_vec());
            self.position_max.insert(
                seq_id,
                i32::try_from(state.len())
                    .unwrap_or(i32::MAX)
                    .saturating_sub(1),
            );
            Ok(())
        }

        fn sequence_position_max(&mut self, seq_id: SeqId) -> Result<i32, SchedError> {
            self.position_max
                .get(&seq_id)
                .copied()
                .ok_or(SchedError::UnknownSequence(seq_id))
        }
    }

    fn continuation(last_token: i32, next_position: u32) -> SessionContinuation {
        SessionContinuation::new([7; MODEL_FINGERPRINT_BYTES], last_token, next_position).unwrap()
    }

    const fn owner() -> [u8; OWNER_FINGERPRINT_BYTES] {
        [9; OWNER_FINGERPRINT_BYTES]
    }

    fn manager(capacity: u32, ram: u64, dir: PathBuf) -> KvManager {
        KvManager::new(capacity, 20, dir, MemoryLedger::new(MemoryAmount::ram(ram))).unwrap()
    }

    fn private_model_dir(temp: &tempfile::TempDir) -> PathBuf {
        let scheduler = temp.path().join("scheduler");
        ensure_private_directory(&scheduler).unwrap();
        scheduler.join("model")
    }

    #[test]
    fn sched_kv_copy_and_remove_are_backend_ledger_transactions() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(20, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend::default();
        let source = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        backend.state.insert(source, vec![1, 2, 3]);
        let copy = kv
            .copy_prefix(
                &mut backend,
                source,
                3,
                PriorityClass::Worker,
                &mut policy,
                &mut Vec::new(),
            )
            .unwrap();
        assert_eq!(backend.state[&copy.destination_seq_id], vec![1, 2, 3]);
        kv.remove(&mut backend, source, &mut Vec::new()).unwrap();
        assert_eq!(kv.sequence_cells(copy.destination_seq_id), Some(3));
    }

    #[test]
    fn sched_kv_sequence_ids_are_compact_bounded_and_reused_after_release() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = KvManager::new(
            20,
            2,
            private_model_dir(&temp),
            MemoryLedger::new(MemoryAmount::ram(200)),
        )
        .unwrap();
        let mut policy = StaticKvPolicy { bytes_per_cell: 1 };
        let first = kv
            .allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        let second = kv
            .allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        assert_eq!((first, second), (0, 1));
        assert_eq!(
            kv.allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new()),
            Err(SchedError::QuotaFull {
                priority: PriorityClass::Worker,
            })
        );
        kv.discard_allocation(first, &mut Vec::new()).unwrap();
        let reused = kv
            .allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        assert_eq!(reused, first);
    }

    #[test]
    fn sched_kv_fragmented_sequence_ids_recover_every_released_hole() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = KvManager::new(
            20,
            4,
            private_model_dir(&temp),
            MemoryLedger::new(MemoryAmount::ram(200)),
        )
        .unwrap();
        let mut policy = StaticKvPolicy { bytes_per_cell: 1 };
        let ids = (0..4)
            .map(|_| {
                kv.allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new())
                    .unwrap()
            })
            .collect::<Vec<_>>();
        kv.discard_allocation(ids[1], &mut Vec::new()).unwrap();
        kv.discard_allocation(ids[3], &mut Vec::new()).unwrap();

        let first_hole = kv
            .allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        let second_hole = kv
            .allocate(2, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();

        assert_eq!((first_hole, second_hole), (ids[1], ids[3]));
        assert_eq!(kv.used_cells(), 8);
        assert_eq!(kv.ledger_mut().available().ram_bytes, 192);
    }

    #[test]
    fn sched_kv_failed_backend_copy_rolls_back_ledger_and_allocation() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(20, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend {
            fail_copy: true,
            ..FakeBackend::default()
        };
        let source = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        assert!(matches!(
            kv.copy_prefix(
                &mut backend,
                source,
                3,
                PriorityClass::Worker,
                &mut policy,
                &mut Vec::new(),
            ),
            Err(SchedError::Backend(_))
        ));
        assert_eq!(kv.used_cells(), 5);
        assert_eq!(kv.ledger_mut().available().ram_bytes, 190);
    }

    #[test]
    fn sched_kv_session_round_trip_restores_backend_state() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(20, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend::default();
        let seq = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        backend.state.insert(seq, vec![9, 8]);
        backend.position_max.insert(seq, 1);
        kv.create_session("known", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        kv.save_session(&mut backend, "known", seq, continuation(7, 3), owner())
            .unwrap();
        let restored = kv
            .restore_session(
                &mut backend,
                "known",
                KvSessionRestoreOptions {
                    expected_model_fingerprint: [7; MODEL_FINGERPRINT_BYTES],
                    expected_owner_fingerprint: owner(),
                    required_cells: 7,
                },
                &mut policy,
                &mut Vec::new(),
            )
            .unwrap();
        assert_eq!(backend.state[&restored.seq_id], vec![9, 8]);
        assert_eq!(backend.position_max[&restored.seq_id], 1);
        assert_eq!(restored.continuation, continuation(7, 3));
        assert_eq!(kv.sequence_cells(restored.seq_id), Some(7));
    }

    #[test]
    fn sched_kv_empty_session_is_durable_and_distinct_after_restart() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let fingerprint = [7; MODEL_FINGERPRINT_BYTES];
        {
            let kv = manager(20, 200, session_dir.clone());
            let path = kv
                .create_session("empty", fingerprint, owner())
                .expect("empty session creation must persist");
            assert!(path.is_file());
            assert!(!kv.session_has_state("empty").unwrap());
        }

        let restarted = manager(20, 200, session_dir);
        assert_eq!(restarted.session_ids().unwrap(), vec!["empty".to_owned()]);
        assert!(!restarted.session_has_state("empty").unwrap());
        restarted
            .create_session("empty", fingerprint, owner())
            .expect("same-model creation must be idempotent");
        assert!(matches!(
            restarted.create_session("empty", [8; MODEL_FINGERPRINT_BYTES], owner()),
            Err(SchedError::InvalidRequest(_))
        ));
    }

    #[test]
    fn sched_kv_concurrent_same_session_creation_is_idempotent_and_atomic() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let kv = manager(20, 200, session_dir.clone());
        let barrier = std::sync::Arc::new(std::sync::Barrier::new(4));

        std::thread::scope(|scope| {
            let handles = (0..4)
                .map(|_| {
                    let barrier = std::sync::Arc::clone(&barrier);
                    let kv = &kv;
                    scope.spawn(move || {
                        barrier.wait();
                        kv.create_session("shared", [7; MODEL_FINGERPRINT_BYTES], owner())
                    })
                })
                .collect::<Vec<_>>();
            for handle in handles {
                assert_eq!(
                    handle.join().unwrap().unwrap(),
                    session_dir.join("shared.amwkv")
                );
            }
        });

        assert_eq!(kv.session_ids().unwrap(), vec!["shared".to_owned()]);
        assert!(!kv.session_has_state("shared").unwrap());
        let files = fs::read_dir(&session_dir)
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(files.len(), 1);
        assert_eq!(files[0].file_name(), "shared.amwkv");
    }

    #[test]
    fn sched_kv_materialized_save_replaces_empty_session() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(20, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend::default();
        kv.create_session("replace", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        let seq = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        backend.state.insert(seq, vec![3, 4]);
        backend.position_max.insert(seq, 1);
        kv.create_session("oversized", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        kv.save_session(&mut backend, "replace", seq, continuation(4, 3), owner())
            .unwrap();
        assert!(kv.session_has_state("replace").unwrap());
    }

    #[test]
    fn sched_kv_failed_atomic_replacement_preserves_previous_snapshot() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("replace.amwkv");
        let original = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [7; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();
        write_snapshot_create_atomic(&path, &original).unwrap();
        let replacement = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [8; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();

        let error = write_snapshot_atomic_with(&path, &replacement, || {
            Err(SchedError::Io(
                "injected failure before replacement".to_owned(),
            ))
        })
        .expect_err("injected pre-replacement failure must surface");

        assert!(matches!(error, SchedError::Io(message) if message.contains("injected")));
        assert_eq!(fs::read(&path).unwrap(), original);
        assert_eq!(
            fs::read_dir(temp.path())
                .unwrap()
                .filter_map(Result::ok)
                .filter(
                    |entry| entry.path().extension().and_then(|value| value.to_str())
                        == Some("tmp")
                )
                .count(),
            0
        );
    }

    #[test]
    fn sched_kv_post_install_verification_failure_restores_prior_across_restart() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        ensure_private_directory(&session_dir).unwrap();
        let path = session_dir.join("rollback.amwkv");
        let original = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [7; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();
        let replacement = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [8; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();
        write_snapshot_create_atomic(&path, &original).unwrap();

        write_snapshot_atomic_with_verifier(
            &path,
            &replacement,
            || Ok(()),
            |_| Err(SchedError::Io("injected final ACL failure".to_owned())),
        )
        .expect_err("final verification failure must roll back");

        let restarted = manager(20, 200, session_dir);
        assert!(matches!(
            restarted.read_session_snapshot("rollback").unwrap(),
            SessionSnapshot::Empty { model_fingerprint, .. }
                if model_fingerprint == [7; MODEL_FINGERPRINT_BYTES]
        ));
    }

    #[test]
    fn sched_kv_failed_new_install_verification_leaves_no_readable_snapshot() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        ensure_private_directory(&session_dir).unwrap();
        let path = session_dir.join("unsafe.amwkv");
        let payload = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [7; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();

        write_snapshot_create_atomic_with_verifier(&path, &payload, |_| {
            Err(SchedError::Io("injected final ACL failure".to_owned()))
        })
        .expect_err("final verification failure must remove the install");

        assert!(!path.exists());
        let restarted = manager(20, 200, session_dir);
        assert!(matches!(
            restarted.read_session_snapshot("unsafe"),
            Err(SchedError::SessionUnknown(_))
        ));
    }

    #[test]
    fn sched_kv_read_rejects_exact_private_directory_substitution() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let kv = manager(20, 200, session_dir.clone());
        let path = kv
            .create_session("substituted", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        fs::remove_file(&path).unwrap();
        ensure_private_directory(&path).unwrap();

        assert!(matches!(
            kv.read_session_snapshot("substituted"),
            Err(SchedError::SessionUnknown(_))
        ));
    }

    #[test]
    fn sched_kv_read_rejects_post_open_growth_at_actual_byte_bound() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        ensure_private_directory(&session_dir).unwrap();
        let path = session_dir.join("growing.amwkv");
        let mut snapshot = create_private_file(&path).unwrap();
        snapshot.write_all(&[0x41; 32]).unwrap();
        snapshot.sync_all().unwrap();
        drop(snapshot);

        let error = read_snapshot_nofollow_with(&path, 64, || {
            let mut writer = fs::OpenOptions::new().append(true).open(&path)?;
            writer.write_all(&[0x42; 64])?;
            writer.sync_all()
        })
        .expect_err("growth after open must be bounded by actual consumed bytes");

        assert_eq!(error.kind(), io::ErrorKind::InvalidData);
        assert!(error.to_string().contains("while reading"));
        assert_eq!(fs::metadata(path).unwrap().len(), 96);
    }

    #[cfg(unix)]
    #[test]
    fn sched_kv_read_rejects_non_private_snapshot_mode() {
        use std::os::unix::fs::PermissionsExt;

        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let kv = manager(20, 200, session_dir);
        let path = kv
            .create_session("public", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        fs::set_permissions(&path, fs::Permissions::from_mode(0o644)).unwrap();

        assert!(matches!(
            kv.read_session_snapshot("public"),
            Err(SchedError::SessionUnknown(_))
        ));
    }

    #[cfg(windows)]
    #[test]
    fn sched_kv_install_and_replace_handle_verify_the_final_target() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        ensure_private_directory(&session_dir).unwrap();
        let path = session_dir.join("final.amwkv");
        let first = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [7; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();
        let replacement = encode_snapshot(&SessionSnapshot::Empty {
            model_fingerprint: [8; MODEL_FINGERPRINT_BYTES],
            owner_fingerprint: owner(),
        })
        .unwrap();

        write_snapshot_create_atomic(&path, &first).unwrap();
        verify_private_file(&path).unwrap();
        write_snapshot_atomic_with(&path, &replacement, || Ok(())).unwrap();
        verify_private_file(&path).unwrap();

        assert_eq!(fs::read(path).unwrap(), replacement);
    }

    #[test]
    fn sched_kv_same_length_mutation_fails_digest_closed() {
        let mut payload = encode_snapshot(&SessionSnapshot::Materialized {
            cells: 5,
            priority: PriorityClass::Worker,
            continuation: continuation(4, 3),
            owner_fingerprint: owner(),
            state: vec![3, 4],
        })
        .unwrap();
        payload[SESSION_HEADER_BYTES] ^= 0x01;

        assert!(matches!(
            decode_snapshot(&payload),
            Err(SchedError::Io(message)) if message.contains("digest")
        ));
    }

    #[test]
    fn sched_kv_rejects_oversized_session_id() {
        let temp = tempfile::tempdir().unwrap();
        let kv = manager(20, 200, private_model_dir(&temp));
        let oversized = "a".repeat(MAX_SESSION_ID_BYTES + 1);

        assert!(matches!(
            kv.create_session(&oversized, [7; MODEL_FINGERPRINT_BYTES], owner()),
            Err(SchedError::SessionUnknown(id)) if id == oversized
        ));
    }

    #[test]
    fn sched_kv_restart_accounting_enforces_count_and_byte_quotas() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let first = manager(20, 200, session_dir.clone());
        first
            .create_session("first", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        drop(first);

        let restarted = manager(20, 200, session_dir.clone());
        let second_path = session_dir.join("second.amwkv");
        let one_file_only = SessionQuota {
            per_session_bytes: u64::MAX,
            per_model_files: 1,
            per_model_bytes: u64::MAX,
            global_files: u64::MAX,
            global_bytes: u64::MAX,
        };
        assert!(matches!(
            restarted.enforce_session_quota_with(&second_path, 1, one_file_only),
            Err(SchedError::Io(message)) if message.contains("count")
        ));

        let existing_bytes = fs::metadata(session_dir.join("first.amwkv")).unwrap().len();
        let byte_limited = SessionQuota {
            per_session_bytes: u64::MAX,
            per_model_files: u64::MAX,
            per_model_bytes: existing_bytes,
            global_files: u64::MAX,
            global_bytes: u64::MAX,
        };
        assert!(matches!(
            restarted.enforce_session_quota_with(&second_path, 1, byte_limited),
            Err(SchedError::Io(message)) if message.contains("byte")
        ));
    }

    #[test]
    fn sched_kv_oversized_native_state_is_rejected_before_export_clone() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(20, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend {
            reported_state_size: Some(MAX_SESSION_BYTES as usize),
            ..FakeBackend::default()
        };
        let seq = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        backend.position_max.insert(seq, 1);
        kv.create_session("oversized", [4; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();

        assert!(matches!(
            kv.save_session(
                &mut backend,
                "oversized",
                seq,
                continuation(4, 3),
                owner(),
            ),
            Err(SchedError::Io(message)) if message.contains("quota")
        ));
        assert_eq!(backend.export_calls, 0);
    }

    #[test]
    fn sched_kv_creates_exact_private_namespace_and_snapshot() {
        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let kv = manager(20, 200, session_dir.clone());

        let snapshot = kv
            .create_session("private", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();

        verify_secure_directory(&session_dir).unwrap();
        verify_private_file(&snapshot).unwrap();
    }

    #[cfg(unix)]
    #[test]
    fn sched_kv_private_mode_and_symlink_snapshot_fail_closed() {
        use std::os::unix::fs::{symlink, PermissionsExt};

        let temp = tempfile::tempdir().unwrap();
        let session_dir = private_model_dir(&temp);
        let kv = manager(20, 200, session_dir.clone());
        let private = kv
            .create_session("private", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        assert_eq!(
            fs::metadata(&private).unwrap().permissions().mode() & 0o777,
            0o600
        );
        symlink(&private, session_dir.join("linked.amwkv")).unwrap();
        assert!(matches!(kv.session_ids(), Err(SchedError::Io(_))));
        assert!(matches!(
            kv.session_has_state("linked"),
            Err(SchedError::SessionUnknown(_))
        ));
    }

    #[test]
    fn sched_kv_failed_restore_rolls_back_ledger() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(20, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend::default();
        let seq = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        backend.state.insert(seq, vec![1]);
        backend.position_max.insert(seq, 0);
        kv.create_session("known", [7; MODEL_FINGERPRINT_BYTES], owner())
            .unwrap();
        kv.save_session(&mut backend, "known", seq, continuation(1, 2), owner())
            .unwrap();
        backend.fail_import = true;
        assert!(matches!(
            kv.restore_session(
                &mut backend,
                "known",
                KvSessionRestoreOptions {
                    expected_model_fingerprint: [7; MODEL_FINGERPRINT_BYTES],
                    expected_owner_fingerprint: owner(),
                    required_cells: 5,
                },
                &mut policy,
                &mut Vec::new(),
            ),
            Err(SchedError::Backend(_))
        ));
        assert_eq!(kv.used_cells(), 5);
        assert_eq!(kv.ledger_mut().available().ram_bytes, 190);
    }

    #[test]
    fn sched_kv_suspension_retains_background_native_state() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(10, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend::default();
        let worker = kv
            .allocate(5, PriorityClass::Worker, &mut policy, &mut Vec::new())
            .unwrap();
        let background = kv
            .allocate(5, PriorityClass::Background, &mut policy, &mut Vec::new())
            .unwrap();
        kv.mark_preempted(background, true).unwrap();
        assert!(matches!(
            kv.evict_pressure_candidate(&mut backend, background, &mut Vec::new()),
            Err(SchedError::InvalidRequest(_))
        ));
        assert!(kv.is_suspended(background));
        assert_eq!(kv.sequence_cells(background), Some(5));
        assert_eq!(kv.sequence_cells(worker), Some(5));
        assert_eq!(kv.background_evicted(), 0);
    }

    #[test]
    fn sched_kv_pressure_never_evicts_active_background() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(10, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let background = kv
            .allocate(10, PriorityClass::Background, &mut policy, &mut Vec::new())
            .unwrap();
        let before = kv.ledger_mut().available().ram_bytes;
        assert!(matches!(
            kv.mark_pressure_evictable(background),
            Err(SchedError::InvalidRequest(_))
        ));
        assert_eq!(kv.sequence_cells(background), Some(10));
        assert_eq!(kv.ledger_mut().available().ram_bytes, before);
    }

    #[test]
    fn sched_kv_failed_pressure_eviction_requires_fresh_authorization() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = manager(10, 200, private_model_dir(&temp));
        let mut policy = StaticKvPolicy { bytes_per_cell: 2 };
        let mut backend = FakeBackend::default();
        let background = kv
            .allocate(10, PriorityClass::Background, &mut policy, &mut Vec::new())
            .unwrap();
        kv.mark_preempted(background, true).unwrap();
        kv.mark_pressure_evictable(background).unwrap();

        backend.fail_remove = true;
        assert_eq!(
            kv.evict_pressure_candidate(&mut backend, background, &mut Vec::new()),
            Err(SchedError::Backend("injected remove failure"))
        );
        assert!(kv.is_suspended(background));
        assert_eq!(kv.sequence_cells(background), Some(10));
        assert_eq!(kv.background_evicted(), 0);
        assert!(matches!(
            kv.evict_pressure_candidate(&mut backend, background, &mut Vec::new()),
            Err(SchedError::InvalidRequest(_))
        ));

        backend.fail_remove = false;
        kv.mark_pressure_evictable(background).unwrap();
        let receipt = kv
            .evict_pressure_candidate(&mut backend, background, &mut Vec::new())
            .unwrap();
        assert_eq!(receipt.seq_id, background);
        assert_eq!(receipt.reason, ReadmissionReason::KvPressure);
        assert_eq!(kv.background_evicted(), 1);
    }
}
