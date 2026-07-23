use std::collections::VecDeque;

use serde::{Deserialize, Serialize};

pub const EVENT_RING_CAPACITY: usize = 4096;

/// Monotonic, process-local position assigned to an event in the telemetry ring.
#[derive(Clone, Copy, Debug, Deserialize, Eq, Ord, PartialEq, PartialOrd, Serialize)]
#[serde(transparent)]
pub struct EventCursor(u64);

impl EventCursor {
    pub const fn new(value: u64) -> Self {
        Self(value)
    }

    pub const fn get(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Sequenced<T> {
    pub cursor: EventCursor,
    pub value: T,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct RingLag {
    /// Number of events no longer retained after the requested cursor.
    pub missed: u64,
    /// Cursor immediately before the oldest event still available for replay.
    pub resume_after: EventCursor,
}

#[derive(Debug)]
pub struct EventRing<T> {
    capacity: usize,
    values: VecDeque<Sequenced<T>>,
    dropped: u64,
    next_cursor: u64,
}

impl<T> EventRing<T> {
    pub fn new(capacity: usize) -> Self {
        let capacity = capacity.max(1);
        Self {
            capacity,
            values: VecDeque::with_capacity(capacity),
            dropped: 0,
            next_cursor: 1,
        }
    }

    pub fn push(&mut self, value: T) -> EventCursor {
        if self.values.len() == self.capacity {
            self.values.pop_front();
            self.dropped = self.dropped.saturating_add(1);
        }
        let cursor = EventCursor::new(self.next_cursor);
        self.next_cursor = self.next_cursor.saturating_add(1);
        self.values.push_back(Sequenced { cursor, value });
        cursor
    }

    pub fn latest_cursor(&self) -> EventCursor {
        EventCursor::new(self.next_cursor.saturating_sub(1))
    }

    pub fn oldest_cursor(&self) -> Option<EventCursor> {
        self.values.front().map(|item| item.cursor)
    }

    pub fn iter(&self) -> impl Iterator<Item = &T> {
        self.values.iter().map(|item| &item.value)
    }

    pub fn len(&self) -> usize {
        self.values.len()
    }

    pub fn is_empty(&self) -> bool {
        self.values.is_empty()
    }

    pub fn dropped(&self) -> u64 {
        self.dropped
    }
}

impl<T: Clone> EventRing<T> {
    /// Return the next retained event after `cursor` without creating a replay queue.
    pub fn next_after(&self, cursor: EventCursor) -> Result<Option<Sequenced<T>>, RingLag> {
        self.check_lag(cursor)?;
        Ok(self
            .values
            .iter()
            .find(|item| item.cursor > cursor)
            .cloned())
    }

    /// Return retained events strictly after `cursor` or an explicit lag boundary.
    pub fn replay_after(&self, cursor: EventCursor) -> Result<Vec<Sequenced<T>>, RingLag> {
        self.check_lag(cursor)?;
        Ok(self
            .values
            .iter()
            .filter(|item| item.cursor > cursor)
            .cloned()
            .collect())
    }

    fn check_lag(&self, cursor: EventCursor) -> Result<(), RingLag> {
        let Some(oldest) = self.oldest_cursor() else {
            return Ok(());
        };
        let minimum_replay_cursor = oldest.get().saturating_sub(1);
        if cursor.get() < minimum_replay_cursor {
            return Err(RingLag {
                missed: minimum_replay_cursor.saturating_sub(cursor.get()),
                resume_after: EventCursor::new(minimum_replay_cursor),
            });
        }
        Ok(())
    }
}

impl<T> Default for EventRing<T> {
    fn default() -> Self {
        Self::new(EVENT_RING_CAPACITY)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn replay_is_exclusive_and_reports_exact_lag() {
        let mut ring = EventRing::new(2);
        assert_eq!(ring.push("one"), EventCursor::new(1));
        ring.push("two");
        ring.push("three");

        assert_eq!(
            ring.replay_after(EventCursor::new(1)).unwrap(),
            vec![
                Sequenced {
                    cursor: EventCursor::new(2),
                    value: "two"
                },
                Sequenced {
                    cursor: EventCursor::new(3),
                    value: "three"
                }
            ]
        );
        assert_eq!(
            ring.replay_after(EventCursor::new(0)),
            Err(RingLag {
                missed: 1,
                resume_after: EventCursor::new(1),
            })
        );
        assert_eq!(ring.dropped(), 1);
    }
}
