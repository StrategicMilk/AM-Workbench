use std::{
    collections::{BTreeMap, BTreeSet, VecDeque},
    sync::{Arc, RwLock},
};

use serde::{Deserialize, Serialize};
use thiserror::Error;

/// Number of successful requests retained in the bounded throughput window.
pub const THROUGHPUT_WINDOW_REQUESTS: usize = 128;

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct RequestMetrics {
    pub queue_ms: f64,
    pub prefill_ms: f64,
    pub decode_ms: f64,
    pub prompt_tokens: u32,
    pub completion_tokens: u32,
    pub tokens_per_second: f64,
    pub prefix_hit_tokens: u32,
    pub speculation_proposed_tokens: u32,
    pub speculation_accepted_tokens: u32,
    pub speculation_acceptance_rate: Option<f64>,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct RoleAggregate {
    pub requests: u64,
    pub prompt_tokens: u64,
    pub completion_tokens: u64,
    pub total_decode_ms: f64,
    pub speculation_proposed_tokens: u64,
    pub speculation_accepted_tokens: u64,
}

#[derive(Clone, Debug, Default, Deserialize, PartialEq, Serialize)]
pub struct EngineMetrics {
    pub queue_depth: usize,
    pub busy_slots: usize,
    pub kv_occupancy_pct: u8,
    /// Compatibility alias for `window_tokens_per_second`.
    pub tokens_per_second: f64,
    /// Completion tokens divided by decode time across every successful request.
    pub cumulative_tokens_per_second: f64,
    /// Completion tokens divided by decode time in the bounded recent-request window.
    pub window_tokens_per_second: f64,
    pub throughput_window_requests: usize,
    pub admitted_requests: u64,
    pub succeeded_requests: u64,
    pub failed_requests: u64,
    pub cancelled_requests: u64,
    /// Requests admitted but not yet assigned exactly one terminal outcome.
    pub in_flight_requests: u64,
    pub telemetry_emission_failures: u64,
    /// Bearer-token failures observed at the loopback API boundary.
    #[serde(default)]
    pub authentication_failures: u64,
    /// Failed authentication requests rejected by the bounded online limiter.
    #[serde(default)]
    pub authentication_throttled_requests: u64,
    /// Invalid authentication requests rejected because bounded admission was full.
    #[serde(default)]
    pub authentication_admission_limited_requests: u64,
    /// Least-recently-seen sources evicted to preserve the limiter cardinality cap.
    #[serde(default)]
    pub authentication_source_evictions: u64,
    /// Bounded per-source records after the latest admitted invalid-state mutation.
    #[serde(default)]
    pub authentication_tracked_sources: usize,
    pub per_role: BTreeMap<String, RoleAggregate>,
}

impl EngineMetrics {
    fn record_success(&mut self, role: &str, metrics: &RequestMetrics) {
        let aggregate = self.per_role.entry(role.to_owned()).or_default();
        aggregate.requests = aggregate.requests.saturating_add(1);
        aggregate.prompt_tokens = aggregate
            .prompt_tokens
            .saturating_add(u64::from(metrics.prompt_tokens));
        aggregate.completion_tokens = aggregate
            .completion_tokens
            .saturating_add(u64::from(metrics.completion_tokens));
        aggregate.total_decode_ms += metrics.decode_ms;
        aggregate.speculation_proposed_tokens = aggregate
            .speculation_proposed_tokens
            .saturating_add(u64::from(metrics.speculation_proposed_tokens));
        aggregate.speculation_accepted_tokens = aggregate
            .speculation_accepted_tokens
            .saturating_add(u64::from(metrics.speculation_accepted_tokens));
    }

    fn cumulative_throughput(&self) -> f64 {
        let (completion_tokens, decode_ms) =
            self.per_role
                .values()
                .fold((0_u64, 0.0_f64), |(tokens, duration), aggregate| {
                    (
                        tokens.saturating_add(aggregate.completion_tokens),
                        duration + aggregate.total_decode_ms,
                    )
                });
        throughput(completion_tokens, decode_ms)
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TerminalOutcome {
    Succeeded,
    Failed,
    Cancelled,
}

#[derive(Clone, Debug, Error, Eq, PartialEq)]
pub enum MetricsAccountingError {
    #[error("request {request_id} was admitted more than once")]
    DuplicateAdmission { request_id: u64 },
    #[error("request {request_id} reached a terminal outcome without an admission")]
    TerminalWithoutAdmission { request_id: u64 },
}

#[derive(Clone, Copy, Debug)]
struct ThroughputSample {
    completion_tokens: u64,
    decode_ms: f64,
}

#[derive(Debug, Default)]
struct MetricsState {
    metrics: EngineMetrics,
    admitted: BTreeSet<u64>,
    throughput_window: VecDeque<ThroughputSample>,
}

/// Cloneable metrics owner that keeps synchronization out of API route state.
#[derive(Clone, Debug, Default)]
pub struct MetricsHub {
    state: Arc<RwLock<MetricsState>>,
}

impl MetricsHub {
    pub fn snapshot(&self) -> EngineMetrics {
        self.state
            .read()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
            .metrics
            .clone()
    }

    /// Records the first physical scheduler admission for one request.
    pub fn record_admission(&self, request_id: u64) -> Result<(), MetricsAccountingError> {
        let mut state = self
            .state
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if !state.admitted.insert(request_id) {
            return Err(MetricsAccountingError::DuplicateAdmission { request_id });
        }
        state.metrics.admitted_requests = state.metrics.admitted_requests.saturating_add(1);
        state.metrics.in_flight_requests = state.metrics.in_flight_requests.saturating_add(1);
        Ok(())
    }

    /// Records one successful terminal outcome and its request performance.
    pub fn record_success(
        &self,
        request_id: u64,
        role: &str,
        metrics: &RequestMetrics,
    ) -> Result<(), MetricsAccountingError> {
        let mut state = self.terminal_state(request_id, TerminalOutcome::Succeeded)?;
        state.metrics.record_success(role, metrics);
        if state.throughput_window.len() == THROUGHPUT_WINDOW_REQUESTS {
            state.throughput_window.pop_front();
        }
        state.throughput_window.push_back(ThroughputSample {
            completion_tokens: u64::from(metrics.completion_tokens),
            decode_ms: metrics.decode_ms,
        });
        let (window_tokens, window_decode_ms) =
            state
                .throughput_window
                .iter()
                .fold((0_u64, 0.0_f64), |(tokens, duration), sample| {
                    (
                        tokens.saturating_add(sample.completion_tokens),
                        duration + sample.decode_ms,
                    )
                });
        state.metrics.cumulative_tokens_per_second = state.metrics.cumulative_throughput();
        state.metrics.window_tokens_per_second = throughput(window_tokens, window_decode_ms);
        state.metrics.tokens_per_second = state.metrics.window_tokens_per_second;
        state.metrics.throughput_window_requests = state.throughput_window.len();
        Ok(())
    }

    /// Records one failed or cancelled terminal outcome without request performance.
    pub fn record_terminal(
        &self,
        request_id: u64,
        outcome: TerminalOutcome,
    ) -> Result<(), MetricsAccountingError> {
        self.terminal_state(request_id, outcome).map(|_| ())
    }

    /// Increments the public counter for a rejected runtime telemetry event.
    pub fn record_telemetry_emission_failure(&self) {
        let mut state = self
            .state
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.metrics.telemetry_emission_failures =
            state.metrics.telemetry_emission_failures.saturating_add(1);
    }

    /// Records one rejected bearer token and the limiter state after the rejection.
    pub fn record_authentication_failure(
        &self,
        throttled: bool,
        tracked_sources: usize,
        source_evictions: u64,
    ) {
        let mut state = self
            .state
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.metrics.authentication_failures =
            state.metrics.authentication_failures.saturating_add(1);
        if throttled {
            state.metrics.authentication_throttled_requests = state
                .metrics
                .authentication_throttled_requests
                .saturating_add(1);
        }
        state.metrics.authentication_source_evictions = state
            .metrics
            .authentication_source_evictions
            .saturating_add(source_evictions);
        state.metrics.authentication_tracked_sources = tracked_sources;
    }

    /// Records a globally bounded invalid request rejected before per-candidate state work.
    pub fn record_authentication_admission_limited(&self) {
        let mut state = self
            .state
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.metrics.authentication_failures =
            state.metrics.authentication_failures.saturating_add(1);
        state.metrics.authentication_throttled_requests = state
            .metrics
            .authentication_throttled_requests
            .saturating_add(1);
        state.metrics.authentication_admission_limited_requests = state
            .metrics
            .authentication_admission_limited_requests
            .saturating_add(1);
    }

    pub fn update_gauges(&self, queue_depth: usize, busy_slots: usize, kv_occupancy_pct: u8) {
        let mut state = self
            .state
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.metrics.queue_depth = queue_depth;
        state.metrics.busy_slots = busy_slots;
        state.metrics.kv_occupancy_pct = kv_occupancy_pct;
    }

    fn terminal_state(
        &self,
        request_id: u64,
        outcome: TerminalOutcome,
    ) -> Result<std::sync::RwLockWriteGuard<'_, MetricsState>, MetricsAccountingError> {
        let mut state = self
            .state
            .write()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if !state.admitted.remove(&request_id) {
            return Err(MetricsAccountingError::TerminalWithoutAdmission { request_id });
        }
        state.metrics.in_flight_requests = state.metrics.in_flight_requests.saturating_sub(1);
        match outcome {
            TerminalOutcome::Succeeded => {
                state.metrics.succeeded_requests =
                    state.metrics.succeeded_requests.saturating_add(1)
            }
            TerminalOutcome::Failed => {
                state.metrics.failed_requests = state.metrics.failed_requests.saturating_add(1)
            }
            TerminalOutcome::Cancelled => {
                state.metrics.cancelled_requests =
                    state.metrics.cancelled_requests.saturating_add(1)
            }
        }
        debug_assert_eq!(
            state.metrics.admitted_requests,
            state
                .metrics
                .succeeded_requests
                .saturating_add(state.metrics.failed_requests)
                .saturating_add(state.metrics.cancelled_requests)
                .saturating_add(state.metrics.in_flight_requests)
        );
        Ok(state)
    }
}

fn throughput(completion_tokens: u64, decode_ms: f64) -> f64 {
    if completion_tokens == 0 || !decode_ms.is_finite() || decode_ms <= 0.0 {
        0.0
    } else {
        completion_tokens as f64 / (decode_ms / 1_000.0)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn request_metrics(completion_tokens: u32, decode_ms: f64) -> RequestMetrics {
        RequestMetrics {
            decode_ms,
            prompt_tokens: 4,
            completion_tokens,
            speculation_proposed_tokens: 4,
            speculation_accepted_tokens: 3,
            ..RequestMetrics::default()
        }
    }

    #[test]
    fn shared_hub_enforces_terminal_accounting_and_records_exact_values() {
        let hub = MetricsHub::default();
        hub.update_gauges(3, 2, 40);
        hub.record_telemetry_emission_failure();
        hub.record_admission(7).unwrap();
        hub.record_success(7, "worker", &request_metrics(5, 100.0))
            .unwrap();

        let metrics = hub.snapshot();
        assert_eq!(metrics.queue_depth, 3);
        assert_eq!(metrics.busy_slots, 2);
        assert_eq!(metrics.kv_occupancy_pct, 40);
        assert_eq!(metrics.admitted_requests, 1);
        assert_eq!(metrics.succeeded_requests, 1);
        assert_eq!(metrics.in_flight_requests, 0);
        assert_eq!(metrics.telemetry_emission_failures, 1);
        assert_eq!(metrics.per_role["worker"].requests, 1);
        assert_eq!(metrics.per_role["worker"].prompt_tokens, 4);
        assert_eq!(metrics.per_role["worker"].completion_tokens, 5);
        assert_eq!(metrics.per_role["worker"].speculation_proposed_tokens, 4);
        assert_eq!(metrics.per_role["worker"].speculation_accepted_tokens, 3);
        assert_eq!(metrics.cumulative_tokens_per_second, 50.0);
        assert_eq!(metrics.window_tokens_per_second, 50.0);
        assert_eq!(metrics.tokens_per_second, 50.0);
    }

    #[test]
    fn duplicate_terminal_outcome_cannot_corrupt_the_lifecycle_invariant() {
        let hub = MetricsHub::default();
        hub.record_admission(11).unwrap();
        hub.record_terminal(11, TerminalOutcome::Cancelled).unwrap();

        assert_eq!(
            hub.record_terminal(11, TerminalOutcome::Failed),
            Err(MetricsAccountingError::TerminalWithoutAdmission { request_id: 11 })
        );
        let metrics = hub.snapshot();
        assert_eq!(metrics.admitted_requests, 1);
        assert_eq!(metrics.cancelled_requests, 1);
        assert_eq!(metrics.failed_requests, 0);
        assert_eq!(metrics.in_flight_requests, 0);
    }

    #[test]
    fn cumulative_and_recent_window_throughput_have_distinct_defined_denominators() {
        let hub = MetricsHub::default();
        for request_id in 0..=THROUGHPUT_WINDOW_REQUESTS as u64 {
            hub.record_admission(request_id).unwrap();
            let metrics = if request_id == 0 {
                request_metrics(1_000, 1_000.0)
            } else {
                request_metrics(1, 1_000.0)
            };
            hub.record_success(request_id, "worker", &metrics).unwrap();
        }

        let snapshot = hub.snapshot();
        assert_eq!(
            snapshot.throughput_window_requests,
            THROUGHPUT_WINDOW_REQUESTS
        );
        assert_eq!(snapshot.window_tokens_per_second, 1.0);
        assert!(snapshot.cumulative_tokens_per_second > 1.0);
    }

    #[test]
    fn authentication_metrics_preserve_counters_and_current_cardinality() {
        let hub = MetricsHub::default();

        hub.record_authentication_failure(false, 1, 0);
        hub.record_authentication_failure(true, 2, 1);
        hub.record_authentication_admission_limited();

        let snapshot = hub.snapshot();
        assert_eq!(snapshot.authentication_failures, 3);
        assert_eq!(snapshot.authentication_throttled_requests, 2);
        assert_eq!(snapshot.authentication_admission_limited_requests, 1);
        assert_eq!(snapshot.authentication_source_evictions, 1);
        assert_eq!(snapshot.authentication_tracked_sources, 2);
    }
}
