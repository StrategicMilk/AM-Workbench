use std::{
    env, io,
    net::{IpAddr, SocketAddr},
};

use amw_kernel::build_router;
use tokio::net::TcpListener;

fn arg_value(args: &[String], name: &str, default: &str) -> String {
    args.windows(2)
        .find_map(|window| (window[0] == name).then(|| window[1].clone()))
        .unwrap_or_else(|| default.to_string())
}

fn allow_insecure_remote_bind() -> bool {
    env::var("AMW_KERNEL_ALLOW_INSECURE_REMOTE_BIND")
        .is_ok_and(|value| value == "1" || value.eq_ignore_ascii_case("true"))
}

fn validate_bind_security(host: &str, insecure_remote_override: bool) -> Result<(), io::Error> {
    let ip: IpAddr = host.parse().map_err(|err| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("invalid kernel bind host {host}: {err}"),
        )
    })?;
    if ip.is_loopback() || insecure_remote_override {
        return Ok(());
    }
    Err(io::Error::new(
        io::ErrorKind::PermissionDenied,
        "amw-kernel refuses non-loopback HTTP bind without explicit insecure override",
    ))
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = env::args().collect();
    let host = arg_value(&args, "--host", "127.0.0.1");
    let port = arg_value(&args, "--port", "5000");
    validate_bind_security(&host, allow_insecure_remote_bind())?;
    let addr: SocketAddr = format!("{host}:{port}").parse()?;
    let listener = TcpListener::bind(addr).await?;
    println!("amw-kernel server listening on http://{addr}");
    axum::serve(listener, build_router()).await?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn security_remote_http_bind_requires_explicit_insecure_override() {
        let denied = validate_bind_security("0.0.0.0", false)
            .expect_err("remote HTTP bind fails closed by default");
        assert_eq!(denied.kind(), io::ErrorKind::PermissionDenied);

        validate_bind_security("127.0.0.1", false).expect("loopback bind allowed");
        validate_bind_security("0.0.0.0", true).expect("explicit insecure override allowed");
    }
}
