use std::{fs, net::TcpListener, process::Command};

use amw_engine::{EXIT_CONFIG_BAD, EXIT_PORT_CONFLICT, EXIT_SUCCESS, EXIT_VERSION_BAD};

#[test]
fn exit_codes_are_pairwise_distinct_and_failures_are_nonzero() {
    let values = [
        EXIT_SUCCESS.get(),
        EXIT_PORT_CONFLICT.get(),
        EXIT_VERSION_BAD.get(),
        EXIT_CONFIG_BAD.get(),
    ];
    for (index, value) in values.iter().enumerate() {
        assert!(!values[..index].contains(value));
    }
    assert_eq!(EXIT_SUCCESS.get(), 0);
    assert!(values[1..].iter().all(|value| *value != 0));
}

#[test]
fn invalid_config_uses_config_bad_exit() {
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("bad.toml");
    fs::write(&path, "unknown = true\n").expect("write fixture");
    let status = Command::new(env!("CARGO_BIN_EXE_amw-engine-server"))
        .args(["--config", path.to_str().expect("UTF-8 temp path")])
        .status()
        .expect("run engine binary");
    assert_eq!(status.code(), Some(EXIT_CONFIG_BAD.get()));
}

#[test]
fn occupied_port_uses_port_conflict_exit() {
    let occupied = TcpListener::bind("127.0.0.1:0").expect("reserve a port");
    let port = occupied.local_addr().expect("local address").port();
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("engine.toml");
    let config =
        include_str!("config_fixture.toml").replace("port = 10933", &format!("port = {port}"));
    fs::write(&path, config).expect("write fixture");
    let status = Command::new(env!("CARGO_BIN_EXE_amw-engine-server"))
        .args(["--config", path.to_str().expect("UTF-8 temp path")])
        .status()
        .expect("run engine binary");
    assert_eq!(status.code(), Some(EXIT_PORT_CONFLICT.get()));
}
