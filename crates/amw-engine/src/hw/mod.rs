//! Hardware inventory and joint memory admission.
//!
//! Hardware discovery treats a missing GPU as a valid CPU-only state. Every
//! model allocation must reserve both RAM and per-device VRAM through
//! [`budget::MemoryLedger`] before the allocation callback is invoked.

pub mod budget;
pub mod devices;
