pub mod api;
pub mod config;
pub mod gen;
pub mod hw;
pub mod receipt;
pub mod runtime;
pub mod sched;
pub mod store;
pub mod telemetry;
pub mod watchdog;

#[cfg(any(feature = "cpu", feature = "cuda"))]
pub mod ffi;

/// Process exit code with a named engine lifecycle meaning.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct EngineExitCode(i32);

impl EngineExitCode {
    pub const fn get(self) -> i32 {
        self.0
    }
}

pub const EXIT_SUCCESS: EngineExitCode = EngineExitCode(0);
pub const EXIT_PORT_CONFLICT: EngineExitCode = EngineExitCode(20);
/// Reserved for the S5.2 supervisor `/version` handshake mismatch path.
pub const EXIT_VERSION_BAD: EngineExitCode = EngineExitCode(21);
pub const EXIT_CONFIG_BAD: EngineExitCode = EngineExitCode(22);
