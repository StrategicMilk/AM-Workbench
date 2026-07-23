use std::sync::{Arc, Mutex, MutexGuard};

use events::EventEnvelope;
use ring::{EventCursor, EventRing, Sequenced};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use tokio::sync::watch;

pub mod events;
pub mod logging;
pub mod metrics;
pub mod ring;

#[derive(Clone, Debug, Default, Deserialize, Eq, PartialEq, Serialize)]
pub struct TraceContext {
    pub request_id: String,
    pub trace_id: String,
}

impl TraceContext {
    pub fn new(request_id: impl Into<String>, trace_id: impl Into<String>) -> Self {
        Self {
            request_id: request_id.into(),
            trace_id: trace_id.into(),
        }
    }

    pub fn is_empty(&self) -> bool {
        self.request_id.is_empty() && self.trace_id.is_empty()
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SubscriptionStart {
    /// Replay every event still retained by the bounded ring.
    Earliest,
    /// Deliver only events emitted after subscription creation.
    Latest,
    /// Replay retained events strictly after the supplied cursor.
    After(EventCursor),
}

#[derive(Clone, Debug, PartialEq)]
pub enum SubscriptionItem {
    Event(Sequenced<EventEnvelope>),
    Lagged {
        missed: u64,
        resume_after: EventCursor,
    },
    Closed,
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum TelemetryError {
    #[error("telemetry event contains prohibited prompt or completion content")]
    SensitiveContent,
    #[error("invalid telemetry event: {0}")]
    InvalidEvent(&'static str),
    #[error("event cursor {requested} is ahead of latest cursor {latest}")]
    FutureCursor { requested: u64, latest: u64 },
}

#[derive(Clone)]
pub struct TelemetryHub {
    ring: Arc<Mutex<EventRing<EventEnvelope>>>,
    changes: watch::Sender<EventCursor>,
}

impl Default for TelemetryHub {
    fn default() -> Self {
        Self::with_capacity(ring::EVENT_RING_CAPACITY)
    }
}

impl TelemetryHub {
    pub fn with_capacity(capacity: usize) -> Self {
        let (changes, _) = watch::channel(EventCursor::new(0));
        Self {
            ring: Arc::new(Mutex::new(EventRing::new(capacity))),
            changes,
        }
    }

    /// Add one schema-v1 event and notify live subscribers without buffering per subscriber.
    pub fn emit(&self, event: EventEnvelope) -> Result<EventCursor, TelemetryError> {
        if event.contains_content() {
            return Err(TelemetryError::SensitiveContent);
        }
        event.validate().map_err(TelemetryError::InvalidEvent)?;
        let cursor = self.lock_ring().push(event);
        self.changes.send_replace(cursor);
        Ok(cursor)
    }

    pub fn subscribe(
        &self,
        start: SubscriptionStart,
    ) -> Result<TelemetrySubscription, TelemetryError> {
        let changes = self.changes.subscribe();
        let ring = self.lock_ring();
        let latest = ring.latest_cursor();
        let cursor = match start {
            SubscriptionStart::Earliest => ring
                .oldest_cursor()
                .map(|cursor| EventCursor::new(cursor.get().saturating_sub(1)))
                .unwrap_or(latest),
            SubscriptionStart::Latest => latest,
            SubscriptionStart::After(cursor) => {
                if cursor > latest {
                    return Err(TelemetryError::FutureCursor {
                        requested: cursor.get(),
                        latest: latest.get(),
                    });
                }
                cursor
            }
        };
        drop(ring);
        Ok(TelemetrySubscription {
            ring: Arc::clone(&self.ring),
            changes,
            cursor,
        })
    }

    pub fn ndjson_snapshot(&self) -> String {
        self.lock_ring()
            .iter()
            .filter_map(|event| event.to_ndjson().ok())
            .collect()
    }

    pub fn len(&self) -> usize {
        self.lock_ring().len()
    }

    /// Returns whether the telemetry ring currently retains no events.
    pub fn is_empty(&self) -> bool {
        self.lock_ring().is_empty()
    }

    pub fn dropped(&self) -> u64 {
        self.lock_ring().dropped()
    }

    /// Clones retained events without waiting when called from a crash-report hook.
    pub fn try_recent_events(&self) -> Vec<EventEnvelope> {
        self.ring
            .try_lock()
            .map(|ring| ring.iter().cloned().collect())
            .unwrap_or_default()
    }

    fn lock_ring(&self) -> MutexGuard<'_, EventRing<EventEnvelope>> {
        self.ring
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

pub struct TelemetrySubscription {
    ring: Arc<Mutex<EventRing<EventEnvelope>>>,
    changes: watch::Receiver<EventCursor>,
    cursor: EventCursor,
}

impl TelemetrySubscription {
    pub fn cursor(&self) -> EventCursor {
        self.cursor
    }

    /// Wait for one replayed or future event, reporting overwritten history explicitly.
    pub async fn recv(&mut self) -> SubscriptionItem {
        loop {
            let replay = {
                let ring = self
                    .ring
                    .lock()
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
                ring.next_after(self.cursor)
            };
            match replay {
                Ok(Some(item)) => {
                    self.cursor = item.cursor;
                    return SubscriptionItem::Event(item);
                }
                Ok(None) => {}
                Err(lag) => {
                    self.cursor = lag.resume_after;
                    return SubscriptionItem::Lagged {
                        missed: lag.missed,
                        resume_after: lag.resume_after,
                    };
                }
            }

            // A changed notification is only a wake-up hint. Replay from the ring is the
            // authority, which closes the subscribe/replay race without per-client queues.
            if *self.changes.borrow_and_update() > self.cursor {
                continue;
            }
            if self.changes.changed().await.is_err() {
                return SubscriptionItem::Closed;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use events::EngineEvent;

    fn gauges(ts: f64) -> EventEnvelope {
        EventEnvelope::new(
            ts,
            EngineEvent::Gauges {
                slots_busy: 0,
                queue_depth: 0,
                vram_used_mb: None,
                kv_occupancy_pct: 0,
            },
        )
    }

    #[tokio::test]
    async fn live_subscription_receives_future_events() {
        let hub = TelemetryHub::with_capacity(2);
        let mut subscription = hub.subscribe(SubscriptionStart::Latest).unwrap();
        hub.emit(gauges(1.0)).unwrap();

        let item = match subscription.recv().await {
            SubscriptionItem::Event(item) => Some(item),
            SubscriptionItem::Lagged { .. } | SubscriptionItem::Closed => None,
        };
        assert!(item.is_some(), "future event should not be reported as lag");
        let item = item.unwrap();
        assert_eq!(item.cursor, EventCursor::new(1));
        assert_eq!(item.value.ts, 1.0);
    }

    #[tokio::test]
    async fn slow_subscription_reports_lag_then_replays_retained_events() {
        let hub = TelemetryHub::with_capacity(2);
        let mut subscription = hub
            .subscribe(SubscriptionStart::After(EventCursor::new(0)))
            .unwrap();
        for timestamp in 1..=4 {
            hub.emit(gauges(f64::from(timestamp))).unwrap();
        }

        assert_eq!(
            subscription.recv().await,
            SubscriptionItem::Lagged {
                missed: 2,
                resume_after: EventCursor::new(2),
            }
        );
        let item = match subscription.recv().await {
            SubscriptionItem::Event(item) => Some(item),
            SubscriptionItem::Lagged { .. } | SubscriptionItem::Closed => None,
        };
        assert!(
            item.is_some(),
            "oldest retained event should follow lag notification"
        );
        let item = item.unwrap();
        assert_eq!(item.cursor, EventCursor::new(3));
        assert_eq!(hub.len(), 2);
        assert_eq!(hub.dropped(), 2);
    }

    #[test]
    fn future_cursor_is_rejected_instead_of_silently_hanging() {
        let hub = TelemetryHub::default();
        assert_eq!(
            hub.subscribe(SubscriptionStart::After(EventCursor::new(1)))
                .err(),
            Some(TelemetryError::FutureCursor {
                requested: 1,
                latest: 0,
            })
        );
    }

    #[test]
    fn malformed_metrics_fail_before_entering_replay_history() {
        let hub = TelemetryHub::default();
        let event = EventEnvelope::new(
            1.0,
            EngineEvent::RequestComplete {
                request_id: "r".to_owned(),
                trace_id: "t".to_owned(),
                model_id: "m".to_owned(),
                queue_ms: 0.0,
                prefill_ms: 0.0,
                decode_ms: f64::NAN,
                input_tokens: 1,
                output_tokens: 1,
                tok_per_s: 1.0,
                prefix_hit_tokens: 0,
                speculation_proposed_tokens: 0,
                speculation_accepted_tokens: 0,
                spec_accept_rate: None,
                priority_class: "interactive".to_owned(),
                eval_slot: 0,
            },
        );

        assert_eq!(
            hub.emit(event),
            Err(TelemetryError::InvalidEvent(
                "request metrics must be finite and non-negative"
            ))
        );
        assert_eq!(hub.len(), 0);
    }

    #[tokio::test]
    async fn subscription_closes_after_hub_drop_and_retained_replay_is_drained() {
        let hub = TelemetryHub::default();
        hub.emit(gauges(1.0)).unwrap();
        let mut subscription = hub.subscribe(SubscriptionStart::Earliest).unwrap();
        drop(hub);

        assert!(matches!(
            subscription.recv().await,
            SubscriptionItem::Event(_)
        ));
        assert_eq!(subscription.recv().await, SubscriptionItem::Closed);
    }
}
