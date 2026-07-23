# Route Auth Matrix

The primary HTTP API boundary is the native Rust kernel route surface in
`crates/amw-kernel/src/api/routes/workbench_domains.rs`.

Legacy `VETINARI_ADMIN_TOKEN` and `RemoteSensitiveReadGuard` documentation now
belongs only to historical Python-web evidence. Native Rust route authorization
must be documented against the kernel policy source below.

The retired Python Litestar app factory and primary route registry have been
removed. Do not add route-auth rows for deleted Litestar route modules or
deleted Python API wrapper modules.

## Native Boundary

| Surface | Runtime owner | Auth policy source |
|---|---|---|
| `/health`, `/ready` | Rust kernel | `crates/amw-kernel/src/api/routes/workbench_domains.rs` |
| `/api/*` | Rust kernel | `crates/amw-kernel/src/api/routes/workbench_domains.rs` |
| `/api/v1/*` | Rust kernel | `crates/amw-kernel/src/api/routes/workbench_domains.rs` |
| `/api/workbench/*` | Rust kernel | `crates/amw-kernel/src/api/routes/workbench_domains.rs` |
| `/api/v1/projects/{project_id}/workbench/*` | Rust kernel | `crates/amw-kernel/src/api/routes/workbench_domains.rs` |

## Retained Python Support

`vetinari/web/shared.py` and SSE helpers remain shared Python support modules
because non-web runtime code imports them. They are not route registries.

The protected Python sibling surfaces are intentionally separate from the
primary Workbench API boundary and must not be used as the default host for new
Workbench routes.
