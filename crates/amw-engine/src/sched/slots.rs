//! Slot lifecycle and transition validation.

use super::{EventSink, SchedError, SchedEvent};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum SlotState {
    Idle,
    Prefill,
    Decode,
    Draining,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Slot {
    id: usize,
    state: SlotState,
}

impl Slot {
    pub const fn new(id: usize) -> Self {
        Self {
            id,
            state: SlotState::Idle,
        }
    }

    pub const fn id(&self) -> usize {
        self.id
    }

    pub const fn state(&self) -> SlotState {
        self.state
    }

    pub fn transition(
        &mut self,
        to: SlotState,
        sink: &mut impl EventSink,
    ) -> Result<(), SchedError> {
        let from = self.state;
        let legal = matches!(
            (from, to),
            (SlotState::Idle, SlotState::Prefill)
                | (SlotState::Prefill, SlotState::Decode)
                | (SlotState::Prefill, SlotState::Draining)
                | (SlotState::Decode, SlotState::Draining)
                | (SlotState::Draining, SlotState::Idle)
        );
        if !legal {
            return Err(if from == SlotState::Draining && to == SlotState::Prefill {
                SchedError::Draining
            } else {
                SchedError::InvalidTransition { from, to }
            });
        }
        self.state = to;
        sink.emit(SchedEvent::SlotState {
            slot_id: self.id,
            from,
            to,
        });
        Ok(())
    }

    pub(crate) fn suspend(&mut self, sink: &mut impl EventSink) -> Result<(), SchedError> {
        let from = self.state;
        if !matches!(from, SlotState::Prefill | SlotState::Decode) {
            return Err(SchedError::InvalidTransition {
                from,
                to: SlotState::Idle,
            });
        }
        self.state = SlotState::Idle;
        sink.emit(SchedEvent::SlotState {
            slot_id: self.id,
            from,
            to: SlotState::Idle,
        });
        Ok(())
    }

    pub(crate) fn reactivate(
        &mut self,
        to: SlotState,
        sink: &mut impl EventSink,
    ) -> Result<(), SchedError> {
        let from = self.state;
        if from != SlotState::Idle || !matches!(to, SlotState::Prefill | SlotState::Decode) {
            return Err(SchedError::InvalidTransition { from, to });
        }
        self.state = to;
        sink.emit(SchedEvent::SlotState {
            slot_id: self.id,
            from,
            to,
        });
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sched_slot_legal_lifecycle_emits_once_per_transition() {
        let mut slot = Slot::new(3);
        let mut events = Vec::new();
        for state in [
            SlotState::Prefill,
            SlotState::Decode,
            SlotState::Draining,
            SlotState::Idle,
        ] {
            slot.transition(state, &mut events).unwrap();
        }
        assert_eq!(events.len(), 4);
        assert_eq!(slot.state(), SlotState::Idle);
    }

    #[test]
    fn sched_slot_illegal_transition_does_not_mutate() {
        let mut slot = Slot::new(0);
        let mut events = Vec::new();
        let error = slot.transition(SlotState::Decode, &mut events).unwrap_err();
        assert_eq!(
            error,
            SchedError::InvalidTransition {
                from: SlotState::Idle,
                to: SlotState::Decode
            }
        );
        assert_eq!(slot.state(), SlotState::Idle);
        assert!(events.is_empty());
    }

    #[test]
    fn sched_draining_rejects_prefill() {
        let mut slot = Slot::new(0);
        let mut events = Vec::new();
        slot.transition(SlotState::Prefill, &mut events).unwrap();
        slot.transition(SlotState::Draining, &mut events).unwrap();
        assert_eq!(
            slot.transition(SlotState::Prefill, &mut events),
            Err(SchedError::Draining)
        );
        assert_eq!(slot.state(), SlotState::Draining);
    }
}
