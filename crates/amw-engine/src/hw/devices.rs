//! Deterministic hardware inventory with injectable descriptors.

use serde::{Deserialize, Serialize};
use sysinfo::System;

const MAX_ENGINE_THREADS: usize = 64;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct CpuDescriptor {
    pub physical_cores: usize,
    pub logical_cores: usize,
    pub avx: bool,
    pub avx2: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct GpuDescriptor {
    pub index: u32,
    pub name: String,
    pub total_vram_bytes: u64,
    pub free_vram_bytes: Option<u64>,
    pub driver_version: Option<String>,
    pub cuda_driver_version: Option<i32>,
    pub cuda_compute_capability: Option<(u32, u32)>,
    pub flash_attention_capable: bool,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SystemDescriptor {
    pub cpu: CpuDescriptor,
    pub total_ram_bytes: u64,
    pub available_ram_bytes: u64,
    pub gpus: Vec<GpuDescriptor>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ThreadPlan {
    pub inference_threads: usize,
    pub batch_threads: usize,
}

pub fn thread_plan(physical_cores: usize, logical_cores: usize) -> ThreadPlan {
    ThreadPlan {
        inference_threads: physical_cores.clamp(1, MAX_ENGINE_THREADS),
        batch_threads: logical_cores.clamp(1, MAX_ENGINE_THREADS),
    }
}

impl SystemDescriptor {
    pub fn detect() -> Self {
        let mut system = System::new();
        system.refresh_memory();
        let physical_cores = num_cpus::get_physical();
        let logical_cores = num_cpus::get();
        Self {
            cpu: CpuDescriptor {
                physical_cores,
                logical_cores,
                avx: cfg!(target_feature = "avx"),
                avx2: cfg!(target_feature = "avx2"),
            },
            total_ram_bytes: system.total_memory(),
            available_ram_bytes: system.available_memory(),
            gpus: detect_nvml_gpus(),
        }
    }

    pub fn threads(&self) -> ThreadPlan {
        thread_plan(self.cpu.physical_cores, self.cpu.logical_cores)
    }

    pub fn is_cpu_only(&self) -> bool {
        self.gpus.is_empty()
    }
}

#[cfg(feature = "nvml")]
fn detect_nvml_gpus() -> Vec<GpuDescriptor> {
    use nvml_wrapper::Nvml;

    let Ok(nvml) = Nvml::init() else {
        return Vec::new();
    };
    let Ok(count) = nvml.device_count() else {
        return Vec::new();
    };
    let driver_version = nvml.sys_driver_version().ok();
    let cuda_driver_version = nvml.sys_cuda_driver_version().ok();
    (0..count)
        .filter_map(|index| {
            let device = nvml.device_by_index(index).ok()?;
            let memory = device.memory_info().ok()?;
            let compute_capability = device
                .cuda_compute_capability()
                .ok()
                .and_then(|capability| {
                    Some((
                        u32::try_from(capability.major).ok()?,
                        u32::try_from(capability.minor).ok()?,
                    ))
                });
            Some(GpuDescriptor {
                index,
                name: device
                    .name()
                    .unwrap_or_else(|_| format!("CUDA device {index}")),
                total_vram_bytes: memory.total,
                free_vram_bytes: Some(memory.free),
                driver_version: driver_version.clone(),
                cuda_driver_version,
                cuda_compute_capability: compute_capability,
                flash_attention_capable: compute_capability.is_some_and(|(major, _)| major >= 8),
            })
        })
        .collect()
}

#[cfg(not(feature = "nvml"))]
fn detect_nvml_gpus() -> Vec<GpuDescriptor> {
    Vec::new()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn thread_heuristics_clamped() {
        assert_eq!(
            thread_plan(0, 512),
            ThreadPlan {
                inference_threads: 1,
                batch_threads: 64,
            }
        );
        assert_eq!(thread_plan(8, 16).inference_threads, 8);
        assert_eq!(thread_plan(8, 16).batch_threads, 16);
    }

    #[test]
    fn cpu_only_enumeration() {
        let descriptor = SystemDescriptor {
            cpu: CpuDescriptor {
                physical_cores: 4,
                logical_cores: 8,
                avx: true,
                avx2: true,
            },
            total_ram_bytes: 16 << 30,
            available_ram_bytes: 12 << 30,
            gpus: Vec::new(),
        };
        assert!(descriptor.is_cpu_only());
        assert_eq!(descriptor.threads().inference_threads, 4);
    }
}
