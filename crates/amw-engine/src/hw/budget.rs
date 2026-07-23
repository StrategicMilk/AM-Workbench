//! Joint RAM/VRAM accounting with admission-before-allocation semantics.

use std::collections::BTreeMap;

use serde::{Deserialize, Serialize};
use thiserror::Error;

#[derive(Clone, Copy, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub enum MemoryPurpose {
    BaseModel,
    DraftModel,
    KvCache,
    LoraAdapter,
    SafetyMargin,
}

#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
pub struct MemoryAmount {
    pub ram_bytes: u64,
    pub vram_bytes: BTreeMap<u32, u64>,
}

impl MemoryAmount {
    pub fn ram(ram_bytes: u64) -> Self {
        Self {
            ram_bytes,
            vram_bytes: BTreeMap::new(),
        }
    }

    pub fn with_vram(mut self, device: u32, bytes: u64) -> Self {
        self.vram_bytes.insert(device, bytes);
        self
    }

    fn checked_add_assign(&mut self, other: &Self) -> Result<(), BudgetError> {
        self.ram_bytes = self
            .ram_bytes
            .checked_add(other.ram_bytes)
            .ok_or(BudgetError::AccountingOverflow)?;
        for (&device, &bytes) in &other.vram_bytes {
            let entry = self.vram_bytes.entry(device).or_default();
            *entry = entry
                .checked_add(bytes)
                .ok_or(BudgetError::AccountingOverflow)?;
        }
        Ok(())
    }

    fn checked_sub_assign(&mut self, other: &Self) -> Result<(), BudgetError> {
        self.ram_bytes = self
            .ram_bytes
            .checked_sub(other.ram_bytes)
            .ok_or(BudgetError::AccountingUnderflow)?;
        for (&device, &bytes) in &other.vram_bytes {
            let entry = self
                .vram_bytes
                .get_mut(&device)
                .ok_or(BudgetError::AccountingUnderflow)?;
            *entry = entry
                .checked_sub(bytes)
                .ok_or(BudgetError::AccountingUnderflow)?;
        }
        Ok(())
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq, Serialize, Deserialize)]
pub struct ReservationId(u64);

#[derive(Clone, Debug, Eq, PartialEq)]
struct Reservation {
    purpose: MemoryPurpose,
    amount: MemoryAmount,
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum BudgetError {
    #[error("RAM admission refused: requested {requested} bytes, available {available} bytes")]
    RamRefused { requested: u64, available: u64 },
    #[error(
        "VRAM admission refused on device {device}: requested {requested} bytes, available {available} bytes"
    )]
    VramRefused {
        device: u32,
        requested: u64,
        available: u64,
    },
    #[error("unknown memory reservation")]
    UnknownReservation,
    #[error("memory accounting overflow")]
    AccountingOverflow,
    #[error("memory accounting underflow")]
    AccountingUnderflow,
}

#[derive(Debug)]
pub struct MemoryLedger {
    capacity: MemoryAmount,
    reserved_total: MemoryAmount,
    committed_total: MemoryAmount,
    pending: BTreeMap<u64, Reservation>,
    committed: BTreeMap<u64, Reservation>,
    next_id: u64,
}

impl MemoryLedger {
    pub fn new(capacity: MemoryAmount) -> Self {
        Self {
            capacity,
            reserved_total: MemoryAmount::default(),
            committed_total: MemoryAmount::default(),
            pending: BTreeMap::new(),
            committed: BTreeMap::new(),
            next_id: 1,
        }
    }

    pub fn reserve(
        &mut self,
        purpose: MemoryPurpose,
        amount: MemoryAmount,
    ) -> Result<ReservationId, BudgetError> {
        self.ensure_available(&amount)?;
        let id = self.next_id;
        self.next_id = self
            .next_id
            .checked_add(1)
            .ok_or(BudgetError::AccountingOverflow)?;
        self.reserved_total.checked_add_assign(&amount)?;
        self.pending.insert(id, Reservation { purpose, amount });
        Ok(ReservationId(id))
    }

    pub fn commit(&mut self, id: ReservationId) -> Result<(), BudgetError> {
        let reservation = self
            .pending
            .remove(&id.0)
            .ok_or(BudgetError::UnknownReservation)?;
        self.reserved_total
            .checked_sub_assign(&reservation.amount)?;
        self.committed_total
            .checked_add_assign(&reservation.amount)?;
        self.committed.insert(id.0, reservation);
        Ok(())
    }

    pub fn release(&mut self, id: ReservationId) -> Result<(), BudgetError> {
        if let Some(reservation) = self.pending.remove(&id.0) {
            return self.reserved_total.checked_sub_assign(&reservation.amount);
        }
        let reservation = self
            .committed
            .remove(&id.0)
            .ok_or(BudgetError::UnknownReservation)?;
        self.committed_total.checked_sub_assign(&reservation.amount)
    }

    pub fn available(&self) -> MemoryAmount {
        let ram_used = self
            .reserved_total
            .ram_bytes
            .saturating_add(self.committed_total.ram_bytes);
        let mut available = MemoryAmount::ram(self.capacity.ram_bytes.saturating_sub(ram_used));
        for (&device, &capacity) in &self.capacity.vram_bytes {
            let used = self
                .reserved_total
                .vram_bytes
                .get(&device)
                .copied()
                .unwrap_or_default()
                .saturating_add(
                    self.committed_total
                        .vram_bytes
                        .get(&device)
                        .copied()
                        .unwrap_or_default(),
                );
            available
                .vram_bytes
                .insert(device, capacity.saturating_sub(used));
        }
        available
    }

    pub(crate) fn is_committed(&self, id: ReservationId) -> bool {
        self.committed.contains_key(&id.0)
    }

    pub fn committed_for(&self, purpose: MemoryPurpose) -> MemoryAmount {
        let mut total = MemoryAmount::default();
        for reservation in self.committed.values().filter(|r| r.purpose == purpose) {
            total
                .checked_add_assign(&reservation.amount)
                .expect("validated ledger entries cannot overflow");
        }
        total
    }

    fn ensure_available(&self, amount: &MemoryAmount) -> Result<(), BudgetError> {
        let available = self.available();
        if amount.ram_bytes > available.ram_bytes {
            return Err(BudgetError::RamRefused {
                requested: amount.ram_bytes,
                available: available.ram_bytes,
            });
        }
        for (&device, &requested) in &amount.vram_bytes {
            let device_available = available
                .vram_bytes
                .get(&device)
                .copied()
                .unwrap_or_default();
            if requested > device_available {
                return Err(BudgetError::VramRefused {
                    device,
                    requested,
                    available: device_available,
                });
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn budget_admission_refusal_has_no_side_effect() {
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(100).with_vram(0, 50));
        let before = ledger.available();
        let error = ledger
            .reserve(
                MemoryPurpose::BaseModel,
                MemoryAmount::ram(101).with_vram(0, 1),
            )
            .unwrap_err();
        assert!(matches!(error, BudgetError::RamRefused { .. }));
        assert_eq!(ledger.available(), before);
    }

    #[test]
    fn release_restores_joint_capacity() {
        let capacity = MemoryAmount::ram(1_000).with_vram(0, 800).with_vram(1, 400);
        let mut ledger = MemoryLedger::new(capacity.clone());
        let id = ledger
            .reserve(
                MemoryPurpose::BaseModel,
                MemoryAmount::ram(300).with_vram(0, 200).with_vram(1, 100),
            )
            .unwrap();
        ledger.commit(id).unwrap();
        assert_eq!(ledger.available().ram_bytes, 700);
        assert_eq!(ledger.available().vram_bytes[&0], 600);
        assert_eq!(ledger.available().vram_bytes[&1], 300);
        ledger.release(id).unwrap();
        assert_eq!(ledger.available(), capacity);
    }

    #[test]
    fn floor_tier_joint_accounting() {
        const GIB: u64 = 1 << 30;
        let mut ledger = MemoryLedger::new(MemoryAmount::ram(16 * GIB).with_vram(0, 8 * GIB));
        let requests = [
            (MemoryPurpose::BaseModel, 4 * GIB),
            (MemoryPurpose::DraftModel, GIB),
            (MemoryPurpose::KvCache, GIB),
            (MemoryPurpose::SafetyMargin, GIB),
        ];
        for (purpose, vram) in requests {
            let id = ledger
                .reserve(purpose, MemoryAmount::ram(vram / 2).with_vram(0, vram))
                .unwrap();
            ledger.commit(id).unwrap();
        }
        assert_eq!(ledger.available().vram_bytes[&0], GIB);
        assert_eq!(
            ledger.committed_for(MemoryPurpose::BaseModel).vram_bytes[&0],
            4 * GIB
        );
    }
}
