//! Deterministic token-batch scheduling primitives.
//!
//! This module owns model-sequence scheduling. It deliberately does not reuse
//! the kernel task-lease scheduler: model slots, KV cells, and token deadlines
//! have different lifetime and fairness invariants.

use thiserror::Error;

pub mod batch_loop;
pub mod core;
pub mod kv;
pub mod prefix;
pub mod queue;
pub mod slots;

#[cfg(any(feature = "cpu", feature = "cuda"))]
pub use batch_loop::DecodeToken;
pub use batch_loop::{
    ActiveSequence, BatchLoop, BatchPlan, BatchStep, DecodeProgress, SequencePhase,
    SequenceReleaseReceipt, SequenceWork, TerminationReason,
};
pub use core::{
    CoreAdmissionFailure, CoreAdmissionOutcome, CoreAdmissionReceipt, CoreReadmissionReceipt,
    CoreReleaseReceipt, CoreResumeReceipt, CoreSessionRestoreOptions, CoreSessionRestoreReceipt,
    CoreStepReceipt, CoreSuspensionReceipt, SchedulerCore, SchedulerCoreConfig, SchedulerSnapshot,
    ScratchSequenceReceipt,
};
pub use kv::{
    KvCopyReceipt, KvManager, KvQuantPolicy, KvRemoveReceipt, KvSessionRestoreOptions,
    KvSessionRestoreReceipt, ReadmissionReason, ReadmissionReceipt, SeqId, SequenceBackend,
    SessionContinuation, StaticKvPolicy,
};
pub use prefix::{NamedPrefixRegistry, PrefixReusePlan, PrefixSnapshot};
pub use queue::{
    ActiveClassCounts, ActivePrincipalCounts, AdmissionQueue, AdmissionReceipt,
    AdmissionReleaseReceipt, AdmissionRequest, PriorityClass, PriorityPolicy,
};
pub use slots::{Slot, SlotState};

/// A scheduler failure whose variant is stable enough for API-layer mapping.
#[derive(Debug, Error, Eq, PartialEq)]
pub enum SchedError {
    #[error("context overflow: requested {requested} tokens, limit {limit}")]
    ContextOverflow { requested: u32, limit: u32 },
    #[error("KV admission refused for {requested_bytes} bytes")]
    Oom { requested_bytes: u64 },
    #[error("scheduler admission queue is full")]
    QueueFull,
    #[error("active quota is full for {priority:?}")]
    QuotaFull { priority: queue::PriorityClass },
    #[error("slot is draining and cannot accept a sequence")]
    Draining,
    #[error("unknown KV session: {0}")]
    SessionUnknown(String),
    #[error("evaluation deadline expired")]
    EvalTimeout,
    #[error("illegal slot transition from {from:?} to {to:?}")]
    InvalidTransition {
        from: slots::SlotState,
        to: slots::SlotState,
    },
    #[error("unknown sequence {0}")]
    UnknownSequence(u32),
    #[error("invalid scheduler request: {0}")]
    InvalidRequest(&'static str),
    #[error("scheduler I/O failed: {0}")]
    Io(String),
    #[error("llama.cpp decode failed with status {0}")]
    Decode(i32),
    #[error("KV backend operation failed: {0}")]
    Backend(&'static str),
    #[error("scheduler ledger invariant failed: {0}")]
    LedgerInvariant(&'static str),
    #[error("stale batch plan: expected step {expected}, got {actual}")]
    StalePlan { expected: u64, actual: u64 },
    #[error("named prefix is live and cannot be overwritten: {0}")]
    PrefixInUse(String),
}

/// Structured scheduler events consumed by the API telemetry adapter.
#[derive(Clone, Debug, PartialEq)]
pub enum SchedEvent {
    SlotState {
        slot_id: usize,
        from: slots::SlotState,
        to: slots::SlotState,
    },
    Admission {
        request_id: u64,
        queue_ms: u64,
        priority_class: queue::PriorityClass,
    },
    BatchStep {
        eval_slot: Option<usize>,
        slots_busy: usize,
        queue_depth: usize,
    },
    KvOccupancy {
        kv_occupancy_pct: f64,
    },
    PrefixRegistered {
        name: String,
    },
    PrefixHit {
        name: String,
        prefix_hit_tokens: usize,
    },
    BackgroundEvicted {
        seq_id: u32,
    },
}

/// Injection point for telemetry; tests use an in-memory sink.
pub trait EventSink {
    fn emit(&mut self, event: SchedEvent);
}

impl EventSink for Vec<SchedEvent> {
    fn emit(&mut self, event: SchedEvent) {
        self.push(event);
    }
}

/// Explicit step clock used instead of wall-clock sleeps or timing assertions.
pub trait StepClock {
    fn now(&self) -> u64;
}

#[derive(Clone, Copy, Debug, Default)]
pub struct ManualClock {
    step: u64,
}

impl ManualClock {
    pub fn advance(&mut self, steps: u64) {
        self.step = self.step.saturating_add(steps);
    }
}

impl StepClock for ManualClock {
    fn now(&self) -> u64 {
        self.step
    }
}
