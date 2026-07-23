//! Per-sequence stop evaluation with bounded prefix withholding.

use std::collections::VecDeque;

use super::GenError;

/// Maximum bytes in one configured stop string.
pub const MAX_STOP_STRING_BYTES: usize = 4 * 1024;
/// Maximum aggregate bytes retained by configured stop strings.
pub const MAX_STOP_CONFIG_BYTES: usize = 64 * 1024;
/// Maximum bytes accepted from one decoded token piece.
pub const MAX_TOKEN_PIECE_BYTES: usize = 16 * 1024;

/// Terminal reason for one sequence.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StopReason {
    /// A configured byte sequence completed.
    StopString(String),
    /// Native EOS/EOG token.
    EndToken(i32),
    /// Requested generation length reached.
    MaxTokens,
    /// Caller explicitly cancelled.
    Cancelled,
    /// Transport consumer disappeared.
    Disconnected,
    /// Monotonic request deadline elapsed.
    DeadlineExceeded,
}

/// Result of evaluating one decoded token.
#[derive(Clone, Debug, Eq, PartialEq)]
pub enum StopDecision {
    /// Scheduler may execute another decode boundary.
    Continue,
    /// Sequence terminates for the supplied reason.
    Stop(StopReason),
}

/// Bytes safe to emit plus the decision for the sampled token.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct StopObservation {
    /// Bytes that cannot be a prefix of any configured stop string.
    pub emit: Vec<u8>,
    /// Whether generation may continue.
    pub decision: StopDecision,
}

/// Request-local stop matcher with bounded pending-prefix storage.
#[derive(Clone, Debug)]
pub struct StopEvaluator {
    stops: Vec<Vec<u8>>,
    end_tokens: Vec<i32>,
    max_tokens: usize,
    generated: usize,
    pending: VecDeque<u8>,
    recent_capacity: usize,
}

impl StopEvaluator {
    /// Constructs a validated matcher for textual, end-token, and length boundaries.
    pub fn new(
        stops: Vec<String>,
        end_tokens: Vec<i32>,
        max_tokens: usize,
    ) -> Result<Self, GenError> {
        if max_tokens == 0 || end_tokens.iter().any(|token| *token < 0) {
            return Err(GenError::InvalidStop(
                "max_tokens must be positive and end tokens non-negative",
            ));
        }
        let stops: Vec<_> = stops.into_iter().map(String::into_bytes).collect();
        let total_bytes = stops
            .iter()
            .try_fold(0_usize, |total, stop| total.checked_add(stop.len()));
        if stops
            .iter()
            .any(|stop| stop.is_empty() || stop.len() > MAX_STOP_STRING_BYTES)
            || total_bytes.is_none_or(|total| total > MAX_STOP_CONFIG_BYTES)
        {
            return Err(GenError::InvalidStop(
                "stop strings must be non-empty and fit configured byte bounds",
            ));
        }
        let recent_capacity = stops.iter().map(Vec::len).max().unwrap_or(0);
        Ok(Self {
            stops,
            end_tokens,
            max_tokens,
            generated: 0,
            pending: VecDeque::with_capacity(recent_capacity),
            recent_capacity,
        })
    }

    /// Evaluates end token, count, and boundary-spanning strings.
    pub fn observe_bytes(
        &mut self,
        token_id: i32,
        token_bytes: &[u8],
    ) -> Result<StopObservation, GenError> {
        if token_id < 0 || token_bytes.len() > MAX_TOKEN_PIECE_BYTES {
            return Err(GenError::InvalidStop(
                "decoded token identifier or byte piece is invalid",
            ));
        }
        if self.end_tokens.contains(&token_id) {
            return Ok(StopObservation {
                emit: self.pending.drain(..).collect(),
                decision: StopDecision::Stop(StopReason::EndToken(token_id)),
            });
        }
        self.generated = self.generated.saturating_add(1);
        self.pending.extend(token_bytes);

        let bytes: Vec<_> = self.pending.iter().copied().collect();
        let matched = self
            .stops
            .iter()
            .filter_map(|stop| find_subslice(&bytes, stop).map(|index| (index, stop)))
            .min_by_key(|(index, _)| *index);
        if let Some((index, stop)) = matched {
            let emit = self.pending.drain(..index).collect();
            self.pending.clear();
            return Ok(StopObservation {
                emit,
                decision: StopDecision::Stop(StopReason::StopString(
                    String::from_utf8_lossy(stop).into_owned(),
                )),
            });
        }

        if self.generated >= self.max_tokens {
            return Ok(StopObservation {
                emit: self.pending.drain(..).collect(),
                decision: StopDecision::Stop(StopReason::MaxTokens),
            });
        }

        let retained = longest_stop_prefix_suffix(&bytes, &self.stops).min(self.recent_capacity);
        let emit_count = self.pending.len().saturating_sub(retained);
        Ok(StopObservation {
            emit: self.pending.drain(..emit_count).collect(),
            decision: StopDecision::Continue,
        })
    }
}

fn find_subslice(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack
        .windows(needle.len())
        .position(|window| window == needle)
}

fn longest_stop_prefix_suffix(bytes: &[u8], stops: &[Vec<u8>]) -> usize {
    stops
        .iter()
        .flat_map(|stop| 1..stop.len())
        .filter(|length| {
            *length <= bytes.len()
                && stops.iter().any(|stop| {
                    *length < stop.len() && bytes[bytes.len() - *length..] == stop[..*length]
                })
        })
        .max()
        .unwrap_or(0)
}
