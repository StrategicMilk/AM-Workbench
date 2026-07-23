use std::collections::{BTreeMap, HashMap, VecDeque};

use futures_util::future::{join_all, BoxFuture};
use serde::Serialize;
use thiserror::Error;

use crate::telemetry::{logging::RotatingJsonLog, TraceContext};

/// A decoding sequence with no token progress for this long is considered stuck.
pub const STUCK_DECODE_SECS: u64 = 120;
/// Runtime coordinators should poll often enough to bound one-token recovery latency.
pub const WATCHDOG_POLL_INTERVAL_SECS: u64 = 1;
/// Recovery receives this long after kill/reset before the next escalation step.
pub const SLOT_RESET_SECS: u64 = 30;
/// An idle engine must remain stable this long before one OOM-reduced slot is restored.
pub const SLOT_RECOVERY_SECS: u64 = 300;
/// Bounded post-unload sample window used to distinguish drift from one noisy sample.
pub const LEAK_SAMPLE_WINDOW: usize = 8;
/// Sustained resident growth above this threshold emits a leak observation.
pub const LEAK_GROWTH_THRESHOLD_BYTES: u64 = 64 * 1024 * 1024;
/// Reserved subject for model-load OOMs that are not associated with a request sequence.
pub const ENGINE_LOAD_SEQUENCE_ID: u64 = 0;

pub trait MonotonicClock {
    fn now_secs(&self) -> u64;
}

/// Production clock based on OS uptime, which advances across suspend/hibernate.
#[derive(Clone, Copy, Debug, Default)]
pub struct SystemUptimeClock;

impl MonotonicClock for SystemUptimeClock {
    fn now_secs(&self) -> u64 {
        sysinfo::System::uptime()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WorkClass {
    Foreground,
    Background,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WatchdogAction {
    ResumedFromSleep,
    FailForegroundForRetry,
    ResumeBackground,
    KillSequence,
    ResetSlot,
    ExitProcess,
    EvictLeastRecentlyUsed,
    ReduceActiveSlots,
    RestoreActiveSlots,
    EmitOom,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct WatchdogEvent {
    pub sequence_id: u64,
    pub action: WatchdogAction,
    pub detail: &'static str,
    #[serde(skip_serializing_if = "TraceContext::is_empty")]
    pub trace: TraceContext,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum EscalationStage {
    Healthy,
    KillIssued { at: u64 },
    SlotResetIssued { at: u64 },
    ExitIssued,
}

#[derive(Clone, Debug)]
struct SequenceState {
    last_progress: u64,
    escalation: EscalationStage,
    work_class: WorkClass,
    trace: TraceContext,
}

pub struct Watchdog<C> {
    clock: C,
    last_poll: u64,
    sequences: HashMap<u64, SequenceState>,
    active_slot_target: usize,
    configured_slot_target: usize,
    last_slot_reduction: Option<u64>,
}

impl<C: MonotonicClock> Watchdog<C> {
    pub fn new(clock: C, active_slot_target: usize) -> Self {
        let now = clock.now_secs();
        Self {
            clock,
            last_poll: now,
            sequences: HashMap::new(),
            active_slot_target: active_slot_target.max(1),
            configured_slot_target: active_slot_target.max(1),
            last_slot_reduction: None,
        }
    }

    pub fn register(&mut self, sequence_id: u64, work_class: WorkClass) {
        self.register_traced(sequence_id, work_class, TraceContext::default());
    }

    pub fn register_traced(
        &mut self,
        sequence_id: u64,
        work_class: WorkClass,
        trace: TraceContext,
    ) {
        self.sequences.insert(
            sequence_id,
            SequenceState {
                last_progress: self.clock.now_secs(),
                escalation: EscalationStage::Healthy,
                work_class,
                trace,
            },
        );
    }

    pub fn progress(&mut self, sequence_id: u64) {
        if let Some(state) = self.sequences.get_mut(&sequence_id) {
            state.last_progress = self.clock.now_secs();
            state.escalation = EscalationStage::Healthy;
        }
    }

    pub fn complete(&mut self, sequence_id: u64) {
        self.sequences.remove(&sequence_id);
    }

    /// Compute one-shot recovery actions. Execution is delegated to `WatchdogCoordinator`.
    pub fn poll(&mut self) -> Vec<WatchdogEvent> {
        let now = self.clock.now_secs();
        let host_gap = now.saturating_sub(self.last_poll);
        self.last_poll = now;
        let resumed_from_sleep = host_gap > STUCK_DECODE_SECS;
        let mut events = Vec::new();
        if self.sequences.is_empty()
            && self.active_slot_target < self.configured_slot_target
            && self
                .last_slot_reduction
                .is_some_and(|reduced_at| now.saturating_sub(reduced_at) >= SLOT_RECOVERY_SECS)
        {
            self.active_slot_target = self
                .active_slot_target
                .saturating_add(1)
                .min(self.configured_slot_target);
            self.last_slot_reduction =
                (self.active_slot_target < self.configured_slot_target).then_some(now);
            events.push(event(
                ENGINE_LOAD_SEQUENCE_ID,
                WatchdogAction::RestoreActiveSlots,
                "restore one active slot after an idle OOM recovery cooldown",
                &TraceContext::default(),
            ));
        }
        let mut sequence_ids: Vec<u64> = self.sequences.keys().copied().collect();
        sequence_ids.sort_unstable();
        for sequence_id in sequence_ids {
            let Some(state) = self.sequences.get_mut(&sequence_id) else {
                continue;
            };
            if resumed_from_sleep && now.saturating_sub(state.last_progress) > STUCK_DECODE_SECS {
                events.push(event(
                    sequence_id,
                    WatchdogAction::ResumedFromSleep,
                    "monotonic host gap classified as S3/S4 resume",
                    &state.trace,
                ));
                let (action, detail) = match state.work_class {
                    WorkClass::Foreground => (
                        WatchdogAction::FailForegroundForRetry,
                        "foreground request clean-fails for client retry after sleep",
                    ),
                    WorkClass::Background => (
                        WatchdogAction::ResumeBackground,
                        "background request resumes from preserved slot after sleep",
                    ),
                };
                events.push(event(sequence_id, action, detail, &state.trace));
                state.last_progress = now;
                state.escalation = EscalationStage::Healthy;
                continue;
            }
            if now.saturating_sub(state.last_progress) < STUCK_DECODE_SECS {
                continue;
            }
            let next = match state.escalation {
                EscalationStage::Healthy => Some((
                    WatchdogAction::KillSequence,
                    "decode made no progress for 120 seconds",
                    EscalationStage::KillIssued { at: now },
                )),
                EscalationStage::KillIssued { at } if now.saturating_sub(at) >= SLOT_RESET_SECS => {
                    Some((
                        WatchdogAction::ResetSlot,
                        "sequence kill did not recover slot within 30 seconds",
                        EscalationStage::SlotResetIssued { at: now },
                    ))
                }
                EscalationStage::SlotResetIssued { at }
                    if now.saturating_sub(at) >= SLOT_RESET_SECS =>
                {
                    Some((
                        WatchdogAction::ExitProcess,
                        "slot reset did not recover process within 30 seconds",
                        EscalationStage::ExitIssued,
                    ))
                }
                EscalationStage::KillIssued { .. }
                | EscalationStage::SlotResetIssued { .. }
                | EscalationStage::ExitIssued => None,
            };
            if let Some((action, detail, stage)) = next {
                state.escalation = stage;
                events.push(event(sequence_id, action, detail, &state.trace));
            }
        }
        events
    }

    pub fn handle_oom(&mut self, sequence_id: u64) -> [WatchdogEvent; 3] {
        self.active_slot_target = self.active_slot_target.saturating_sub(1).max(1);
        self.last_slot_reduction = Some(self.clock.now_secs());
        let trace = self
            .sequences
            .get(&sequence_id)
            .map(|state| state.trace.clone())
            .unwrap_or_default();
        [
            event(
                sequence_id,
                WatchdogAction::EvictLeastRecentlyUsed,
                "evict background LRU after CUDA OOM",
                &trace,
            ),
            event(
                sequence_id,
                WatchdogAction::ReduceActiveSlots,
                "reduce active slot target by one with floor one",
                &trace,
            ),
            event(
                sequence_id,
                WatchdogAction::EmitOom,
                "reject the affected request with typed oom",
                &trace,
            ),
        ]
    }

    pub fn handle_load_oom(&mut self) -> [WatchdogEvent; 3] {
        self.handle_oom(ENGINE_LOAD_SEQUENCE_ID)
    }

    pub fn active_slot_target(&self) -> usize {
        self.active_slot_target
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct MemoryLeakObservation {
    pub first_resident_bytes: u64,
    pub latest_resident_bytes: u64,
    pub growth_bytes: u64,
    pub sample_count: usize,
}

/// Bounded detector fed after each completed model unload cycle.
#[derive(Debug, Default)]
pub struct MemoryLeakMonitor {
    post_unload_resident_bytes: VecDeque<u64>,
    last_reported_bytes: Option<u64>,
}

impl MemoryLeakMonitor {
    pub fn record_post_unload(&mut self, resident_bytes: u64) -> Option<MemoryLeakObservation> {
        if self.post_unload_resident_bytes.len() == LEAK_SAMPLE_WINDOW {
            self.post_unload_resident_bytes.pop_front();
        }
        self.post_unload_resident_bytes.push_back(resident_bytes);
        if self.post_unload_resident_bytes.len() < LEAK_SAMPLE_WINDOW
            || !self
                .post_unload_resident_bytes
                .iter()
                .zip(self.post_unload_resident_bytes.iter().skip(1))
                .all(|(left, right)| right >= left)
        {
            return None;
        }
        let (Some(&first), Some(&latest)) = (
            self.post_unload_resident_bytes.front(),
            self.post_unload_resident_bytes.back(),
        ) else {
            return None;
        };
        let growth = latest.saturating_sub(first);
        let new_growth_since_report =
            latest.saturating_sub(self.last_reported_bytes.unwrap_or(first));
        if growth < LEAK_GROWTH_THRESHOLD_BYTES
            || new_growth_since_report < LEAK_GROWTH_THRESHOLD_BYTES
        {
            return None;
        }
        self.last_reported_bytes = Some(latest);
        Some(MemoryLeakObservation {
            first_resident_bytes: first,
            latest_resident_bytes: latest,
            growth_bytes: growth,
            sample_count: self.post_unload_resident_bytes.len(),
        })
    }
}

fn event(
    sequence_id: u64,
    action: WatchdogAction,
    detail: &'static str,
    trace: &TraceContext,
) -> WatchdogEvent {
    WatchdogEvent {
        sequence_id,
        action,
        detail,
        trace: trace.clone(),
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum WatchdogTelemetryPhase {
    Started,
    Succeeded,
    Failed,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct WatchdogTelemetryRecord {
    pub event: WatchdogEvent,
    pub phase: WatchdogTelemetryPhase,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub error: Option<String>,
}

pub trait WatchdogTelemetry: Send + Sync {
    fn record(&self, record: &WatchdogTelemetryRecord) -> Result<(), WatchdogTelemetryError>;
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
#[error("{message}")]
pub struct WatchdogTelemetryError {
    message: String,
}

impl WatchdogTelemetryError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

impl WatchdogTelemetry for RotatingJsonLog {
    fn record(&self, record: &WatchdogTelemetryRecord) -> Result<(), WatchdogTelemetryError> {
        self.append(record)
            .map_err(|error| WatchdogTelemetryError::new(error.to_string()))
    }
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
#[error("{message}")]
pub struct WatchdogCallbackError {
    message: String,
}

impl WatchdogCallbackError {
    pub fn new(message: impl Into<String>) -> Self {
        Self {
            message: message.into(),
        }
    }
}

pub trait WatchdogCallbacks: Send + Sync {
    fn resumed_from_sleep(
        &self,
        event: WatchdogEvent,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn fail_foreground_for_retry(
        &self,
        event: WatchdogEvent,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn resume_background(
        &self,
        event: WatchdogEvent,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn kill_sequence(
        &self,
        event: WatchdogEvent,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn reset_slot(&self, event: WatchdogEvent) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn exit_process(
        &self,
        event: WatchdogEvent,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn evict_least_recently_used(
        &self,
        event: WatchdogEvent,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn reduce_active_slots(
        &self,
        event: WatchdogEvent,
        target: usize,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn restore_active_slots(
        &self,
        event: WatchdogEvent,
        target: usize,
    ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
    fn emit_oom(&self, event: WatchdogEvent) -> BoxFuture<'_, Result<(), WatchdogCallbackError>>;
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct WatchdogExecution {
    pub action: WatchdogAction,
    pub result: Result<(), WatchdogCallbackError>,
    pub telemetry_errors: Vec<WatchdogTelemetryError>,
}

pub struct WatchdogCoordinator<C, H, T> {
    watchdog: Watchdog<C>,
    callbacks: H,
    telemetry: T,
}

impl<C: MonotonicClock, H: WatchdogCallbacks, T: WatchdogTelemetry> WatchdogCoordinator<C, H, T> {
    pub fn new(watchdog: Watchdog<C>, callbacks: H, telemetry: T) -> Self {
        Self {
            watchdog,
            callbacks,
            telemetry,
        }
    }

    pub fn watchdog_mut(&mut self) -> &mut Watchdog<C> {
        &mut self.watchdog
    }

    pub fn register(&mut self, sequence_id: u64, work_class: WorkClass, trace: TraceContext) {
        self.watchdog
            .register_traced(sequence_id, work_class, trace);
    }

    pub fn progress(&mut self, sequence_id: u64) {
        self.watchdog.progress(sequence_id);
    }

    pub fn complete(&mut self, sequence_id: u64) {
        self.watchdog.complete(sequence_id);
    }

    pub fn active_slot_target(&self) -> usize {
        self.watchdog.active_slot_target()
    }

    pub async fn poll(&mut self) -> Vec<WatchdogExecution> {
        let events = self.watchdog.poll();
        let mut by_sequence: BTreeMap<u64, Vec<WatchdogEvent>> = BTreeMap::new();
        for event in events {
            by_sequence
                .entry(event.sequence_id)
                .or_default()
                .push(event);
        }
        let groups = by_sequence.into_values().map(|events| async {
            let mut executions = Vec::with_capacity(events.len());
            for event in events {
                executions.push(self.execute(event).await);
            }
            executions
        });
        join_all(groups).await.into_iter().flatten().collect()
    }

    pub async fn handle_oom(&mut self, sequence_id: u64) -> Vec<WatchdogExecution> {
        let events = self.watchdog.handle_oom(sequence_id);
        let mut executions = Vec::with_capacity(events.len());
        for event in events {
            executions.push(self.execute(event).await);
        }
        executions
    }

    pub async fn handle_load_oom(&mut self) -> Vec<WatchdogExecution> {
        let events = self.watchdog.handle_load_oom();
        let mut executions = Vec::with_capacity(events.len());
        for event in events {
            executions.push(self.execute(event).await);
        }
        executions
    }

    async fn execute(&self, event: WatchdogEvent) -> WatchdogExecution {
        let mut telemetry_errors = Vec::new();
        if let Err(error) = self.telemetry.record(&WatchdogTelemetryRecord {
            event: event.clone(),
            phase: WatchdogTelemetryPhase::Started,
            error: None,
        }) {
            telemetry_errors.push(error);
        }
        let action = event.action;
        let result = match action {
            WatchdogAction::ResumedFromSleep => {
                self.callbacks.resumed_from_sleep(event.clone()).await
            }
            WatchdogAction::FailForegroundForRetry => {
                self.callbacks
                    .fail_foreground_for_retry(event.clone())
                    .await
            }
            WatchdogAction::ResumeBackground => {
                self.callbacks.resume_background(event.clone()).await
            }
            WatchdogAction::KillSequence => self.callbacks.kill_sequence(event.clone()).await,
            WatchdogAction::ResetSlot => self.callbacks.reset_slot(event.clone()).await,
            WatchdogAction::ExitProcess => self.callbacks.exit_process(event.clone()).await,
            WatchdogAction::EvictLeastRecentlyUsed => {
                self.callbacks
                    .evict_least_recently_used(event.clone())
                    .await
            }
            WatchdogAction::ReduceActiveSlots => {
                self.callbacks
                    .reduce_active_slots(event.clone(), self.watchdog.active_slot_target())
                    .await
            }
            WatchdogAction::RestoreActiveSlots => {
                self.callbacks
                    .restore_active_slots(event.clone(), self.watchdog.active_slot_target())
                    .await
            }
            WatchdogAction::EmitOom => self.callbacks.emit_oom(event.clone()).await,
        };
        if let Err(error) = self.telemetry.record(&WatchdogTelemetryRecord {
            event,
            phase: if result.is_ok() {
                WatchdogTelemetryPhase::Succeeded
            } else {
                WatchdogTelemetryPhase::Failed
            },
            error: result.as_ref().err().map(ToString::to_string),
        }) {
            telemetry_errors.push(error);
        }
        WatchdogExecution {
            action,
            result,
            telemetry_errors,
        }
    }
}

#[cfg(test)]
mod tests {
    use std::sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    };

    use super::*;
    use tokio::{
        sync::Notify,
        time::{timeout, Duration},
    };

    #[derive(Clone)]
    struct Clock(Arc<AtomicU64>);

    impl MonotonicClock for Clock {
        fn now_secs(&self) -> u64 {
            self.0.load(Ordering::SeqCst)
        }
    }

    #[derive(Clone, Default)]
    struct Recorder(Arc<Mutex<Vec<WatchdogAction>>>);

    impl Recorder {
        fn invoke(
            &self,
            action: WatchdogAction,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async move {
                self.0
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner())
                    .push(action);
                Ok(())
            })
        }

        fn actions(&self) -> Vec<WatchdogAction> {
            self.0
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .clone()
        }
    }

    impl WatchdogCallbacks for Recorder {
        fn resumed_from_sleep(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::ResumedFromSleep)
        }
        fn fail_foreground_for_retry(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::FailForegroundForRetry)
        }
        fn resume_background(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::ResumeBackground)
        }
        fn kill_sequence(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::KillSequence)
        }
        fn reset_slot(&self, _: WatchdogEvent) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::ResetSlot)
        }
        fn exit_process(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::ExitProcess)
        }
        fn evict_least_recently_used(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::EvictLeastRecentlyUsed)
        }
        fn reduce_active_slots(
            &self,
            _: WatchdogEvent,
            _: usize,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::ReduceActiveSlots)
        }
        fn restore_active_slots(
            &self,
            _: WatchdogEvent,
            _: usize,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::RestoreActiveSlots)
        }
        fn emit_oom(&self, _: WatchdogEvent) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            self.invoke(WatchdogAction::EmitOom)
        }
    }

    #[derive(Clone, Default)]
    struct TelemetryRecorder(Arc<Mutex<Vec<WatchdogTelemetryRecord>>>);

    impl WatchdogTelemetry for TelemetryRecorder {
        fn record(&self, record: &WatchdogTelemetryRecord) -> Result<(), WatchdogTelemetryError> {
            self.0
                .lock()
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .push(record.clone());
            Ok(())
        }
    }

    struct FailingTelemetry;

    impl WatchdogTelemetry for FailingTelemetry {
        fn record(&self, _: &WatchdogTelemetryRecord) -> Result<(), WatchdogTelemetryError> {
            Err(WatchdogTelemetryError::new("log unavailable"))
        }
    }

    struct BlockingCallbacks {
        release_first: Arc<Notify>,
        sibling_called: Arc<Notify>,
    }

    impl WatchdogCallbacks for BlockingCallbacks {
        fn resumed_from_sleep(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn fail_foreground_for_retry(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn resume_background(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn kill_sequence(
            &self,
            event: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async move {
                if event.sequence_id == 1 {
                    self.release_first.notified().await;
                } else {
                    self.sibling_called.notify_one();
                }
                Ok(())
            })
        }
        fn reset_slot(&self, _: WatchdogEvent) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn exit_process(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn evict_least_recently_used(
            &self,
            _: WatchdogEvent,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn reduce_active_slots(
            &self,
            _: WatchdogEvent,
            _: usize,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn restore_active_slots(
            &self,
            _: WatchdogEvent,
            _: usize,
        ) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
        fn emit_oom(&self, _: WatchdogEvent) -> BoxFuture<'_, Result<(), WatchdogCallbackError>> {
            Box::pin(async { Ok(()) })
        }
    }

    #[tokio::test]
    async fn sleep_recovery_executes_callbacks_without_stuck_escalation() {
        let value = Arc::new(AtomicU64::new(0));
        let callbacks = Recorder::default();
        let telemetry = TelemetryRecorder::default();
        let mut watchdog = Watchdog::new(Clock(Arc::clone(&value)), 2);
        watchdog.register_traced(
            1,
            WorkClass::Foreground,
            TraceContext::new("request-1", "trace-1"),
        );
        let mut coordinator =
            WatchdogCoordinator::new(watchdog, callbacks.clone(), telemetry.clone());
        value.store(300, Ordering::SeqCst);

        coordinator.poll().await;

        assert_eq!(
            callbacks.actions(),
            vec![
                WatchdogAction::ResumedFromSleep,
                WatchdogAction::FailForegroundForRetry
            ]
        );
        assert!(!callbacks.actions().contains(&WatchdogAction::KillSequence));
        let records = telemetry.0.lock().unwrap();
        assert!(records
            .iter()
            .all(|record| record.event.trace.trace_id == "trace-1"));
    }

    #[tokio::test]
    async fn full_stuck_ladder_executes_each_callback_once() {
        let value = Arc::new(AtomicU64::new(0));
        let callbacks = Recorder::default();
        let telemetry = TelemetryRecorder::default();
        let mut watchdog = Watchdog::new(Clock(Arc::clone(&value)), 2);
        watchdog.register(7, WorkClass::Foreground);
        let mut coordinator =
            WatchdogCoordinator::new(watchdog, callbacks.clone(), telemetry.clone());

        value.store(STUCK_DECODE_SECS, Ordering::SeqCst);
        coordinator.poll().await;
        value.store(STUCK_DECODE_SECS + SLOT_RESET_SECS - 1, Ordering::SeqCst);
        assert!(coordinator.poll().await.is_empty());
        value.store(STUCK_DECODE_SECS + SLOT_RESET_SECS, Ordering::SeqCst);
        coordinator.poll().await;
        value.store(STUCK_DECODE_SECS + SLOT_RESET_SECS * 2, Ordering::SeqCst);
        coordinator.poll().await;
        value.store(STUCK_DECODE_SECS + SLOT_RESET_SECS * 3, Ordering::SeqCst);
        assert!(coordinator.poll().await.is_empty());

        assert_eq!(
            callbacks.actions(),
            vec![
                WatchdogAction::KillSequence,
                WatchdogAction::ResetSlot,
                WatchdogAction::ExitProcess
            ]
        );
        assert_eq!(telemetry.0.lock().unwrap().len(), 6);
    }

    #[tokio::test]
    async fn oom_ladder_executes_all_mitigation_callbacks_at_slot_floor() {
        let value = Arc::new(AtomicU64::new(0));
        let callbacks = Recorder::default();
        let telemetry = TelemetryRecorder::default();
        let watchdog = Watchdog::new(Clock(value), 1);
        let mut coordinator =
            WatchdogCoordinator::new(watchdog, callbacks.clone(), telemetry.clone());

        let outcomes = coordinator.handle_oom(9).await;

        assert!(outcomes
            .iter()
            .all(|outcome| outcome.result.is_ok() && outcome.telemetry_errors.is_empty()));
        assert_eq!(
            callbacks.actions(),
            vec![
                WatchdogAction::EvictLeastRecentlyUsed,
                WatchdogAction::ReduceActiveSlots,
                WatchdogAction::EmitOom
            ]
        );
        assert_eq!(coordinator.watchdog.active_slot_target(), 1);
        assert_eq!(telemetry.0.lock().unwrap().len(), 6);
    }

    #[tokio::test]
    async fn load_oom_uses_reserved_non_request_subject() {
        let value = Arc::new(AtomicU64::new(0));
        let callbacks = Recorder::default();
        let telemetry = TelemetryRecorder::default();
        let watchdog = Watchdog::new(Clock(value), 2);
        let mut coordinator = WatchdogCoordinator::new(watchdog, callbacks, telemetry.clone());

        let outcomes = coordinator.handle_load_oom().await;

        assert_eq!(outcomes.len(), 3);
        assert_eq!(coordinator.active_slot_target(), 1);
        assert!(telemetry
            .0
            .lock()
            .unwrap()
            .iter()
            .all(|record| record.event.sequence_id == ENGINE_LOAD_SEQUENCE_ID));
    }

    #[tokio::test]
    async fn idle_cooldown_restores_reduced_slots_one_at_a_time() {
        let value = Arc::new(AtomicU64::new(0));
        let callbacks = Recorder::default();
        let watchdog = Watchdog::new(Clock(Arc::clone(&value)), 3);
        let mut coordinator =
            WatchdogCoordinator::new(watchdog, callbacks.clone(), TelemetryRecorder::default());
        coordinator.handle_load_oom().await;
        coordinator.handle_load_oom().await;
        assert_eq!(coordinator.active_slot_target(), 1);

        value.store(SLOT_RECOVERY_SECS - 1, Ordering::SeqCst);
        assert!(coordinator.poll().await.is_empty());
        value.store(SLOT_RECOVERY_SECS, Ordering::SeqCst);
        let first = coordinator.poll().await;
        assert_eq!(coordinator.active_slot_target(), 2);
        assert_eq!(first[0].action, WatchdogAction::RestoreActiveSlots);

        value.store(SLOT_RECOVERY_SECS * 2, Ordering::SeqCst);
        let second = coordinator.poll().await;
        assert_eq!(coordinator.active_slot_target(), 3);
        assert_eq!(second[0].action, WatchdogAction::RestoreActiveSlots);
        assert_eq!(
            callbacks
                .actions()
                .iter()
                .filter(|action| **action == WatchdogAction::RestoreActiveSlots)
                .count(),
            2
        );
    }

    #[tokio::test]
    async fn telemetry_failure_is_observable_without_suppressing_recovery_callback() {
        let value = Arc::new(AtomicU64::new(0));
        let callbacks = Recorder::default();
        let mut watchdog = Watchdog::new(Clock(Arc::clone(&value)), 1);
        watchdog.register(1, WorkClass::Foreground);
        let mut coordinator =
            WatchdogCoordinator::new(watchdog, callbacks.clone(), FailingTelemetry);
        value.store(STUCK_DECODE_SECS, Ordering::SeqCst);

        let outcomes = coordinator.poll().await;

        assert_eq!(callbacks.actions(), vec![WatchdogAction::KillSequence]);
        assert_eq!(outcomes[0].telemetry_errors.len(), 2);
    }

    #[tokio::test]
    async fn blocked_sequence_callback_does_not_stall_sibling_recovery() {
        let value = Arc::new(AtomicU64::new(0));
        let release_first = Arc::new(Notify::new());
        let sibling_called = Arc::new(Notify::new());
        let callbacks = BlockingCallbacks {
            release_first: Arc::clone(&release_first),
            sibling_called: Arc::clone(&sibling_called),
        };
        let mut watchdog = Watchdog::new(Clock(Arc::clone(&value)), 2);
        watchdog.register(1, WorkClass::Foreground);
        watchdog.register(2, WorkClass::Foreground);
        value.store(STUCK_DECODE_SECS, Ordering::SeqCst);
        let mut coordinator =
            WatchdogCoordinator::new(watchdog, callbacks, TelemetryRecorder::default());

        let poll = tokio::spawn(async move { coordinator.poll().await });
        timeout(Duration::from_secs(1), sibling_called.notified())
            .await
            .expect("sibling callback must run while the first sequence is blocked");
        release_first.notify_one();
        assert_eq!(poll.await.unwrap().len(), 2);
    }

    #[test]
    fn progress_in_one_sequence_does_not_mask_stuck_sibling() {
        let value = Arc::new(AtomicU64::new(0));
        let mut watchdog = Watchdog::new(Clock(Arc::clone(&value)), 2);
        watchdog.register(1, WorkClass::Foreground);
        watchdog.register(2, WorkClass::Foreground);
        value.store(STUCK_DECODE_SECS - 1, Ordering::SeqCst);
        watchdog.progress(2);
        value.store(STUCK_DECODE_SECS, Ordering::SeqCst);

        let events = watchdog.poll();

        assert_eq!(events.len(), 1);
        assert_eq!(events[0].sequence_id, 1);
        assert_eq!(events[0].action, WatchdogAction::KillSequence);
    }

    #[test]
    fn leak_monitor_requires_sustained_growth_across_full_window() {
        let mut monitor = MemoryLeakMonitor::default();
        for index in 0..LEAK_SAMPLE_WINDOW - 1 {
            assert!(monitor
                .record_post_unload(index as u64 * 16 * 1024 * 1024)
                .is_none());
        }
        let observation = monitor
            .record_post_unload((LEAK_SAMPLE_WINDOW - 1) as u64 * 16 * 1024 * 1024)
            .expect("sustained growth above threshold should be observed");
        assert_eq!(observation.sample_count, LEAK_SAMPLE_WINDOW);
        assert!(observation.growth_bytes >= LEAK_GROWTH_THRESHOLD_BYTES);

        assert!(monitor.record_post_unload(1).is_none());
    }
}
