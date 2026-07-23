//! Async bounded per-sequence event stream with item and retained-byte limits.

use std::sync::{
    atomic::{AtomicUsize, Ordering},
    Arc,
};

use tokio::sync::{
    mpsc::{self, error::TryRecvError, error::TrySendError},
    Notify,
};

use super::{GenError, GenerationControl, GenerationEvent};

/// Maximum queued events per sequence.
pub const OUTPUT_CHANNEL_CAPACITY: usize = 32;
/// Maximum retained payload bytes per sequence.
pub const OUTPUT_CHANNEL_BYTE_CAPACITY: usize = 64 * 1024;
/// Maximum retained bytes in one event.
pub const MAX_GENERATION_EVENT_BYTES: usize = 16 * 1024;

#[derive(Debug)]
struct EventEnvelope {
    event: GenerationEvent,
    retained_bytes: usize,
}

/// Scheduler-side bounded generation event sender.
#[derive(Clone, Debug)]
pub struct GenerationSender {
    sender: mpsc::Sender<EventEnvelope>,
    queued_items: Arc<AtomicUsize>,
    queued_bytes: Arc<AtomicUsize>,
    capacity_available: Arc<Notify>,
}

/// API-side bounded generation event receiver.
#[derive(Debug)]
pub struct GenerationReceiver {
    receiver: mpsc::Receiver<EventEnvelope>,
    queued_items: Arc<AtomicUsize>,
    queued_bytes: Arc<AtomicUsize>,
    capacity_available: Arc<Notify>,
    control: GenerationControl,
}

/// Compatibility alias for existing scheduler call sites.
pub type TokenSender = GenerationSender;
/// Compatibility alias for existing API call sites.
pub type TokenReceiver = GenerationReceiver;

/// Creates an event stream whose item count and retained bytes are both bounded.
pub fn bounded_generation_stream(
    control: GenerationControl,
) -> (GenerationSender, GenerationReceiver) {
    // One physical slot is reserved for the authoritative terminal event. Data
    // events remain bounded by OUTPUT_CHANNEL_CAPACITY and cannot consume it.
    let (sender, receiver) = mpsc::channel(OUTPUT_CHANNEL_CAPACITY + 1);
    let queued_items = Arc::new(AtomicUsize::new(0));
    let queued_bytes = Arc::new(AtomicUsize::new(0));
    let capacity_available = Arc::new(Notify::new());
    (
        GenerationSender {
            sender,
            queued_items: queued_items.clone(),
            queued_bytes: queued_bytes.clone(),
            capacity_available: capacity_available.clone(),
        },
        GenerationReceiver {
            receiver,
            queued_items,
            queued_bytes,
            capacity_available,
            control,
        },
    )
}

/// Creates a stream with a new default control signal.
pub fn bounded_token_stream() -> (GenerationSender, GenerationReceiver) {
    bounded_generation_stream(GenerationControl::default())
}

impl GenerationSender {
    /// Enqueues an event while applying asynchronous item-count backpressure.
    pub async fn send(&self, event: GenerationEvent) -> Result<(), GenError> {
        let terminal = is_terminal(&event);
        self.reserve_items(1, terminal).await?;
        let retained_bytes = match self.reserve_bytes_for(&event, terminal) {
            Ok(retained_bytes) => retained_bytes,
            Err(error) => {
                self.release_items(1);
                return Err(error);
            }
        };
        if self
            .sender
            .send(EventEnvelope {
                event,
                retained_bytes,
            })
            .await
            .is_err()
        {
            self.release_items(1);
            self.queued_bytes
                .fetch_sub(retained_bytes, Ordering::AcqRel);
            return Err(GenError::StreamDisconnected);
        }
        Ok(())
    }

    /// Attempts to enqueue without waiting when item-count backpressure is active.
    pub fn try_send(&self, event: GenerationEvent) -> Result<(), GenError> {
        let terminal = is_terminal(&event);
        self.try_reserve_items(1, terminal)?;
        let retained_bytes = match self.reserve_bytes_for(&event, terminal) {
            Ok(retained_bytes) => retained_bytes,
            Err(error) => {
                self.release_items(1);
                return Err(error);
            }
        };
        let envelope = EventEnvelope {
            event,
            retained_bytes,
        };
        self.sender.try_send(envelope).map_err(|error| {
            self.release_items(1);
            self.queued_bytes
                .fetch_sub(retained_bytes, Ordering::AcqRel);
            match error {
                TrySendError::Full(_) => GenError::Backpressure,
                TrySendError::Closed(_) => GenError::StreamDisconnected,
            }
        })
    }

    /// Atomically reserves item and byte capacity before enqueueing a complete event bundle.
    pub fn try_send_batch(&self, events: Vec<GenerationEvent>) -> Result<(), GenError> {
        if events.is_empty() {
            return Ok(());
        }
        let terminal_count = events.iter().filter(|event| is_terminal(event)).count();
        if terminal_count > 1 {
            return Err(GenError::SpeculationInvalid(
                "generation event bundle contains multiple terminals",
            ));
        }
        let includes_terminal = terminal_count == 1;
        if includes_terminal && !events.last().is_some_and(is_terminal) {
            return Err(GenError::SpeculationInvalid(
                "generation event bundle terminal is not final",
            ));
        }
        self.try_reserve_items(events.len(), includes_terminal)?;
        let permits = self
            .sender
            .try_reserve_many(events.len())
            .map_err(|error| {
                self.release_items(events.len());
                match error {
                    TrySendError::Full(()) => GenError::Backpressure,
                    TrySendError::Closed(()) => GenError::StreamDisconnected,
                }
            })?;
        let retained = events
            .iter()
            .map(|event| {
                let retained = event.retained_bytes();
                if retained > MAX_GENERATION_EVENT_BYTES {
                    Err(GenError::EventTooLarge)
                } else {
                    Ok(retained)
                }
            })
            .collect::<Result<Vec<_>, _>>()
            .inspect_err(|_| {
                self.release_items(events.len());
            })?;
        let total = retained.iter().try_fold(0_usize, |total, retained| {
            total.checked_add(*retained).ok_or(GenError::Backpressure)
        });
        let total = total.inspect_err(|_| {
            self.release_items(events.len());
        })?;
        if let Err(error) = self.reserve_bytes(total, includes_terminal) {
            self.release_items(events.len());
            return Err(error);
        }
        for ((permit, event), retained_bytes) in permits.zip(events).zip(retained) {
            permit.send(EventEnvelope {
                event,
                retained_bytes,
            });
        }
        Ok(())
    }

    /// Current retained payload bytes, exposed for operability and tests.
    pub fn queued_bytes(&self) -> usize {
        self.queued_bytes.load(Ordering::Acquire)
    }

    fn reserve_bytes_for(
        &self,
        event: &GenerationEvent,
        terminal: bool,
    ) -> Result<usize, GenError> {
        let retained_bytes = event.retained_bytes();
        if retained_bytes > MAX_GENERATION_EVENT_BYTES {
            return Err(GenError::EventTooLarge);
        }
        self.reserve_bytes(retained_bytes, terminal)?;
        Ok(retained_bytes)
    }

    async fn reserve_items(&self, count: usize, terminal: bool) -> Result<(), GenError> {
        loop {
            match self.try_reserve_items(count, terminal) {
                Ok(()) => return Ok(()),
                Err(GenError::Backpressure) => {
                    if self.sender.is_closed() {
                        return Err(GenError::StreamDisconnected);
                    }
                    let notified = self.capacity_available.notified();
                    if self.try_reserve_items(count, terminal).is_ok() {
                        return Ok(());
                    }
                    notified.await;
                }
                Err(error) => return Err(error),
            }
        }
    }

    fn try_reserve_items(&self, count: usize, terminal: bool) -> Result<(), GenError> {
        if self.sender.is_closed() {
            return Err(GenError::StreamDisconnected);
        }
        let limit = OUTPUT_CHANNEL_CAPACITY + usize::from(terminal);
        self.queued_items
            .fetch_update(Ordering::AcqRel, Ordering::Acquire, |current| {
                current.checked_add(count).filter(|next| *next <= limit)
            })
            .map(|_| ())
            .map_err(|_| GenError::Backpressure)
    }

    fn release_items(&self, count: usize) {
        self.queued_items.fetch_sub(count, Ordering::AcqRel);
        self.capacity_available.notify_waiters();
    }

    fn reserve_bytes(&self, retained_bytes: usize, terminal: bool) -> Result<(), GenError> {
        let limit = if terminal {
            OUTPUT_CHANNEL_BYTE_CAPACITY
        } else {
            OUTPUT_CHANNEL_BYTE_CAPACITY.saturating_sub(MAX_GENERATION_EVENT_BYTES)
        };
        let mut current = self.queued_bytes.load(Ordering::Acquire);
        loop {
            let Some(next) = current.checked_add(retained_bytes) else {
                return Err(GenError::Backpressure);
            };
            if next > limit {
                return Err(GenError::Backpressure);
            }
            match self.queued_bytes.compare_exchange_weak(
                current,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return Ok(()),
                Err(observed) => current = observed,
            }
        }
    }
}

impl GenerationReceiver {
    /// Receives one event, waiting for the producer while it remains connected.
    pub async fn recv(&mut self) -> Option<GenerationEvent> {
        self.receiver.recv().await.map(|envelope| {
            self.queued_items.fetch_sub(1, Ordering::AcqRel);
            self.queued_bytes
                .fetch_sub(envelope.retained_bytes, Ordering::AcqRel);
            self.capacity_available.notify_waiters();
            envelope.event
        })
    }

    /// Attempts to receive one queued event without waiting.
    pub fn try_recv(&mut self) -> Result<Option<GenerationEvent>, GenError> {
        match self.receiver.try_recv() {
            Ok(envelope) => {
                self.queued_items.fetch_sub(1, Ordering::AcqRel);
                self.queued_bytes
                    .fetch_sub(envelope.retained_bytes, Ordering::AcqRel);
                self.capacity_available.notify_waiters();
                Ok(Some(envelope.event))
            }
            Err(TryRecvError::Empty) => Ok(None),
            Err(TryRecvError::Disconnected) => Err(GenError::StreamDisconnected),
        }
    }
}

fn is_terminal(event: &GenerationEvent) -> bool {
    matches!(
        event,
        GenerationEvent::Finished { .. } | GenerationEvent::Failed(_)
    )
}

impl Drop for GenerationReceiver {
    fn drop(&mut self) {
        self.control.disconnect();
    }
}
