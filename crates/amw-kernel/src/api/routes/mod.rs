//! Native Axum HTTP route handlers for the AM Workbench kernel.
//!
//! Only substantive Rust-owned handlers are exported here. Broad generated
//! `status: ok` route scaffolds are intentionally not part of the runtime API
//! because they hide incomplete migrations behind false-green responses.

pub mod engine_proxy;
pub mod workbench_domains;

use axum::Router;

pub use workbench_domains::{handle_kernel_request, KernelHttpRequest};

/// Build the native Rust router containing the migrated kernel routes.
pub fn build_router() -> Router {
    workbench_domains::routes().merge(engine_proxy::routes())
}
