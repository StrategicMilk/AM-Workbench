//! Bounded named-prefix registry with deterministic content hashes.

use std::collections::BTreeMap;

use super::{
    EventSink, KvManager, KvQuantPolicy, PriorityClass, SchedError, SchedEvent, SeqId,
    SequenceBackend,
};

pub const NAMED_PREFIX_CAPACITY: usize = 128;

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PrefixSnapshot {
    pub name: String,
    pub token_count: usize,
    pub cells: u32,
    pub pins: u32,
    pub hit_count: u64,
    pub kv_seq_id: Option<SeqId>,
}

/// Opaque proof that a prompt matched a currently pinned native prefix.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct PrefixReusePlan {
    pub(crate) request_id: u64,
    pub(crate) name: String,
    pub(crate) source_seq_id: SeqId,
    pub(crate) prefix_hit_tokens: u32,
    pub(crate) cells: u32,
    pub(crate) content_hash: u64,
}

impl PrefixReusePlan {
    pub const fn request_id(&self) -> u64 {
        self.request_id
    }

    pub const fn prefix_hit_tokens(&self) -> u32 {
        self.prefix_hit_tokens
    }
}

#[derive(Clone, Debug)]
struct PrefixEntry {
    tokens: Vec<i32>,
    cells: u32,
    content_hash: u64,
    pins: u32,
    hit_count: u64,
    kv_seq_id: Option<SeqId>,
}

#[derive(Debug)]
pub struct NamedPrefixRegistry {
    capacity: usize,
    entries: BTreeMap<String, PrefixEntry>,
}

impl Default for NamedPrefixRegistry {
    fn default() -> Self {
        Self::new(NAMED_PREFIX_CAPACITY).expect("named prefix capacity is positive")
    }
}

impl NamedPrefixRegistry {
    pub fn new(capacity: usize) -> Result<Self, SchedError> {
        if capacity == 0 {
            return Err(SchedError::InvalidRequest(
                "prefix registry capacity must be positive",
            ));
        }
        Ok(Self {
            capacity,
            entries: BTreeMap::new(),
        })
    }

    pub fn register(
        &mut self,
        name: impl Into<String>,
        tokens: Vec<i32>,
        cells: u32,
        sink: &mut impl EventSink,
    ) -> Result<(), SchedError> {
        let name = name.into();
        if name.is_empty() || tokens.is_empty() || cells == 0 {
            return Err(SchedError::InvalidRequest(
                "prefix name, tokens, and cells must be non-empty",
            ));
        }
        if !self.entries.contains_key(&name) && self.entries.len() >= self.capacity {
            return Err(SchedError::QueueFull);
        }
        if self
            .entries
            .get(&name)
            .is_some_and(|entry| entry.pins > 0 || entry.kv_seq_id.is_some())
        {
            return Err(SchedError::PrefixInUse(name));
        }
        let content_hash = content_hash(&tokens);
        self.entries.insert(
            name.clone(),
            PrefixEntry {
                tokens,
                cells,
                content_hash,
                pins: 0,
                hit_count: 0,
                kv_seq_id: None,
            },
        );
        sink.emit(SchedEvent::PrefixRegistered { name });
        Ok(())
    }

    pub fn pin(&mut self, name: &str) -> Result<(), SchedError> {
        let entry = self
            .entries
            .get_mut(name)
            .ok_or_else(|| SchedError::SessionUnknown(name.to_owned()))?;
        entry.pins = checked_next_pin(entry.pins)?;
        Ok(())
    }

    /// Pins a prefix and reserves its KV cells through the shared ledger on the
    /// first pin. Subsequent pins share the same sequence allocation.
    pub fn pin_with_kv(
        &mut self,
        name: &str,
        kv: &mut KvManager,
        policy: &mut impl KvQuantPolicy,
        sink: &mut impl EventSink,
    ) -> Result<SeqId, SchedError> {
        let entry = self
            .entries
            .get_mut(name)
            .ok_or_else(|| SchedError::SessionUnknown(name.to_owned()))?;
        let next_pins = checked_next_pin(entry.pins)?;
        let seq_id = match entry.kv_seq_id {
            Some(seq_id) => seq_id,
            None => {
                let seq_id = kv.allocate(entry.cells, PriorityClass::Worker, policy, sink)?;
                entry.kv_seq_id = Some(seq_id);
                seq_id
            }
        };
        entry.pins = next_pins;
        Ok(seq_id)
    }

    /// Returns true when the final pin was released and KV ownership may be freed.
    pub fn unpin(&mut self, name: &str) -> Result<bool, SchedError> {
        let entry = self
            .entries
            .get_mut(name)
            .ok_or_else(|| SchedError::SessionUnknown(name.to_owned()))?;
        if entry.pins == 0 {
            return Err(SchedError::InvalidRequest("prefix is not pinned"));
        }
        if entry.kv_seq_id.is_some() {
            return Err(SchedError::InvalidRequest(
                "ledger-backed prefix requires unpin_with_kv",
            ));
        }
        entry.pins -= 1;
        Ok(entry.pins == 0)
    }

    /// Releases the ledger-backed prefix allocation after its final pin.
    pub fn unpin_with_kv(
        &mut self,
        name: &str,
        kv: &mut KvManager,
        backend: &mut impl SequenceBackend,
        sink: &mut impl EventSink,
    ) -> Result<bool, SchedError> {
        let entry = self
            .entries
            .get_mut(name)
            .ok_or_else(|| SchedError::SessionUnknown(name.to_owned()))?;
        if entry.pins == 0 {
            return Err(SchedError::InvalidRequest("prefix is not pinned"));
        }
        if entry.pins > 1 {
            entry.pins -= 1;
            return Ok(false);
        }
        if let Some(seq_id) = entry.kv_seq_id {
            kv.remove(backend, seq_id, sink)?;
        }
        entry.kv_seq_id = None;
        entry.pins = 0;
        Ok(true)
    }

    pub fn match_tokens(
        &mut self,
        name: &str,
        tokens: &[i32],
        sink: &mut impl EventSink,
    ) -> Result<bool, SchedError> {
        let entry = self
            .entries
            .get_mut(name)
            .ok_or_else(|| SchedError::SessionUnknown(name.to_owned()))?;
        if tokens.len() < entry.tokens.len() {
            return Ok(false);
        }
        let candidate = &tokens[..entry.tokens.len()];
        if entry.content_hash != content_hash(candidate) || entry.tokens != candidate {
            return Ok(false);
        }
        entry.hit_count = entry.hit_count.saturating_add(1);
        sink.emit(SchedEvent::PrefixHit {
            name: name.to_owned(),
            prefix_hit_tokens: entry.tokens.len(),
        });
        Ok(true)
    }

    /// Matches a complete prompt prefix and returns a pinned native reuse proof.
    pub fn match_for_reuse(
        &mut self,
        request_id: u64,
        name: &str,
        tokens: &[i32],
        sink: &mut impl EventSink,
    ) -> Result<Option<PrefixReusePlan>, SchedError> {
        if !self.match_tokens(name, tokens, sink)? {
            return Ok(None);
        }
        let entry = self
            .entries
            .get(name)
            .ok_or_else(|| SchedError::SessionUnknown(name.to_owned()))?;
        let Some(source_seq_id) = entry.kv_seq_id.filter(|_| entry.pins > 0) else {
            return Ok(None);
        };
        let prefix_hit_tokens = u32::try_from(entry.tokens.len()).map_err(|_| {
            SchedError::InvalidRequest("prefix token count exceeds scheduler range")
        })?;
        Ok(Some(PrefixReusePlan {
            request_id,
            name: name.to_owned(),
            source_seq_id,
            prefix_hit_tokens,
            cells: entry.cells,
            content_hash: entry.content_hash,
        }))
    }

    pub(crate) fn validates_reuse(&self, plan: &PrefixReusePlan) -> bool {
        self.entries.get(&plan.name).is_some_and(|entry| {
            entry.pins > 0
                && entry.kv_seq_id == Some(plan.source_seq_id)
                && entry.cells == plan.cells
                && entry.content_hash == plan.content_hash
                && u32::try_from(entry.tokens.len()) == Ok(plan.prefix_hit_tokens)
        })
    }

    pub fn pins(&self, name: &str) -> Option<u32> {
        self.entries.get(name).map(|entry| entry.pins)
    }

    pub fn cells(&self, name: &str) -> Option<u32> {
        self.entries.get(name).map(|entry| entry.cells)
    }

    pub fn hit_count(&self, name: &str) -> Option<u64> {
        self.entries.get(name).map(|entry| entry.hit_count)
    }

    pub fn snapshot(&self) -> Vec<PrefixSnapshot> {
        self.entries
            .iter()
            .map(|(name, entry)| PrefixSnapshot {
                name: name.clone(),
                token_count: entry.tokens.len(),
                cells: entry.cells,
                pins: entry.pins,
                hit_count: entry.hit_count,
                kv_seq_id: entry.kv_seq_id,
            })
            .collect()
    }
}

fn checked_next_pin(pins: u32) -> Result<u32, SchedError> {
    pins.checked_add(1)
        .ok_or(SchedError::InvalidRequest("prefix pin count overflow"))
}

fn content_hash(tokens: &[i32]) -> u64 {
    // Stable FNV-1a over explicit little-endian token bytes. Unlike the
    // standard library's map hasher, this value is stable across processes.
    let mut hash = 0xcbf2_9ce4_8422_2325_u64;
    for byte in tokens.iter().flat_map(|token| token.to_le_bytes()) {
        hash ^= u64::from(byte);
        hash = hash.wrapping_mul(0x0000_0100_0000_01b3);
    }
    hash
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::hw::budget::{MemoryAmount, MemoryLedger};
    use crate::sched::StaticKvPolicy;

    #[derive(Default)]
    struct FakeBackend;

    impl SequenceBackend for FakeBackend {
        fn copy_sequence(
            &mut self,
            _source: SeqId,
            _destination: SeqId,
            _cells: u32,
        ) -> Result<(), SchedError> {
            Ok(())
        }

        fn remove_sequence(&mut self, _seq_id: SeqId) -> Result<(), SchedError> {
            Ok(())
        }

        fn export_sequence(&mut self, _seq_id: SeqId) -> Result<Vec<u8>, SchedError> {
            Ok(vec![1])
        }

        fn import_sequence(&mut self, _seq_id: SeqId, _state: &[u8]) -> Result<(), SchedError> {
            Ok(())
        }
    }

    #[test]
    fn sched_prefix_lifecycle_and_hits_are_observable() {
        let mut registry = NamedPrefixRegistry::new(2).unwrap();
        let mut events = Vec::new();
        registry
            .register("system", vec![1, 2, 3], 3, &mut events)
            .unwrap();
        registry.pin("system").unwrap();
        assert!(registry
            .match_tokens("system", &[1, 2, 3, 4, 5], &mut events)
            .unwrap());
        assert!(!registry
            .match_tokens("system", &[1, 2, 4], &mut events)
            .unwrap());
        assert_eq!(registry.hit_count("system"), Some(1));
        assert!(registry.unpin("system").unwrap());
        assert!(matches!(
            events.last(),
            Some(SchedEvent::PrefixHit {
                prefix_hit_tokens: 3,
                ..
            })
        ));
    }

    #[test]
    fn sched_pinned_prefix_survives_sequence_completions() {
        let mut registry = NamedPrefixRegistry::default();
        registry
            .register("stable", vec![7, 8], 2, &mut Vec::new())
            .unwrap();
        registry.pin("stable").unwrap();
        for _ in 0..10 {
            assert_eq!(registry.cells("stable"), Some(2));
        }
        assert_eq!(registry.pins("stable"), Some(1));
    }

    #[test]
    fn sched_prefix_pin_uses_and_releases_kv_ledger() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = KvManager::new(
            10,
            8,
            temp.path().to_owned(),
            MemoryLedger::new(MemoryAmount::ram(100)),
        )
        .unwrap();
        let mut policy = StaticKvPolicy { bytes_per_cell: 4 };
        let mut registry = NamedPrefixRegistry::default();
        let mut backend = FakeBackend;
        registry
            .register("ledger", vec![1, 2], 2, &mut Vec::new())
            .unwrap();
        let seq_id = registry
            .pin_with_kv("ledger", &mut kv, &mut policy, &mut Vec::new())
            .unwrap();
        assert_eq!(kv.sequence_cells(seq_id), Some(2));
        assert!(registry
            .unpin_with_kv("ledger", &mut kv, &mut backend, &mut Vec::new())
            .unwrap());
        assert_eq!(kv.sequence_cells(seq_id), None);
        assert_eq!(kv.ledger_mut().available().ram_bytes, 100);
    }

    #[test]
    fn sched_prefix_rejects_overwrite_while_kv_is_owned() {
        let temp = tempfile::tempdir().unwrap();
        let mut kv = KvManager::new(
            10,
            8,
            temp.path().to_owned(),
            MemoryLedger::new(MemoryAmount::ram(100)),
        )
        .unwrap();
        let mut policy = StaticKvPolicy { bytes_per_cell: 4 };
        let mut registry = NamedPrefixRegistry::default();
        registry
            .register("live", vec![1, 2], 2, &mut Vec::new())
            .unwrap();
        registry
            .pin_with_kv("live", &mut kv, &mut policy, &mut Vec::new())
            .unwrap();
        assert_eq!(
            registry.register("live", vec![9], 1, &mut Vec::new()),
            Err(SchedError::PrefixInUse("live".to_owned()))
        );
        assert_eq!(kv.used_cells(), 2);
        assert!(registry
            .match_tokens("live", &[1, 2, 3], &mut Vec::new())
            .unwrap());
    }
}
