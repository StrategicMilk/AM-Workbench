use std::{collections::HashSet, fs, path::PathBuf};

use amw_engine::{
    config::{CliOverrides, EngineConfig, KvRootPolicy, LogLevel},
    store::session::SessionStore,
};

const GOOD: &str = r#"
[server]
host = "127.0.0.1"
port = 10933
auth_token_path = "auth-token"
auth_policy_path = "auth-policy.json"
[models]
dirs = ["models"]
[budgets]
vram_gb = 12.0
ram_gb = 24.0
margin_pct = 10.0
[slots]
count = 2
default_ctx = 4096
[kv]
cache_type_k = "q8_0"
cache_type_v = "f16"
session_dir = "sessions"
[idle]
keep_alive = "5m"
[scheduler]
preemption = true
batch_token_budget = 2048
[log]
level = "info"
dir = "logs"
"#;

#[test]
fn known_good_config_round_trips() {
    let config = EngineConfig::from_toml_str(GOOD).expect("known-good fixture must parse");
    let encoded = toml::to_string(&config).expect("config must serialize");
    let decoded = EngineConfig::from_toml_str(&encoded).expect("serialized config must parse");
    assert_eq!(decoded, config);
    assert_eq!(EngineConfig::default().server.host, "127.0.0.1");
    assert_eq!(EngineConfig::default().budgets.ram_gb, 30.0);
}

#[test]
fn unknown_key_is_refused_and_named() {
    let bad = GOOD.replace("port = 10933", "port = 10933\nunexpected = true");
    let error = EngineConfig::from_toml_str(&bad).expect_err("unknown key must fail closed");
    assert!(error.to_string().contains("unexpected"), "{error}");
}

#[test]
fn precedence_is_file_then_environment_then_cli() {
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let env = [
        ("VETINARI_ENGINE_SERVER_HOST".into(), "127.0.0.2".into()),
        ("VETINARI_ENGINE_SERVER_PORT".into(), "10934".into()),
        ("VETINARI_ENGINE_LOG_LEVEL".into(), "debug".into()),
    ];
    let cli = CliOverrides {
        host: Some("127.0.0.3".into()),
        port: Some(10935),
        model_dirs: Some(vec![PathBuf::from("cli-models")]),
        log_level: Some(LogLevel::Warn),
    };
    let config = EngineConfig::load_with_env(&path, env, &cli).expect("overlay must apply");
    assert_eq!(config.server.host, "127.0.0.3");
    assert_eq!(config.server.port, 10935);
    assert_eq!(config.models.dirs, [PathBuf::from("cli-models")]);
    assert_eq!(config.log.level, LogLevel::Warn);
    assert_eq!(config.kv.session_dir, temp.path().join("sessions"));
    assert_eq!(config.kv.root_policy, KvRootPolicy::Managed);
}

#[test]
fn missing_external_session_storage_is_rejected_without_creation() {
    let temp = tempfile::tempdir().expect("temp directory");
    let external_parent = tempfile::tempdir().expect("external parent");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let external = external_parent.path().join("missing-external-sessions");
    let env = [(
        "VETINARI_ENGINE_KV_SESSION_DIR".into(),
        external.display().to_string(),
    )];

    let error = EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect_err("missing external session storage must fail closed");

    assert!(error.to_string().contains("exact private"), "{error}");
    assert!(
        !external.exists(),
        "config loading must never create an external root"
    );
}

#[test]
fn exact_private_external_session_storage_is_verify_only() {
    let temp = tempfile::tempdir().expect("temp directory");
    let external_parent = tempfile::tempdir().expect("external parent");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let external = external_parent
        .path()
        .join("preprovisioned-external-sessions");
    drop(SessionStore::open(external.clone()).expect("preprovision exact-private root"));
    let before = fs::metadata(&external).expect("external metadata");
    let env = [(
        "VETINARI_ENGINE_KV_SESSION_DIR".into(),
        external.display().to_string(),
    )];

    let config = EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect("exact-private external root must be supported");
    let after = fs::metadata(&external).expect("external metadata after verification");

    assert_eq!(config.kv.session_dir, external);
    assert_eq!(config.kv.root_policy, KvRootPolicy::ExternalPreprovisioned);
    assert_eq!(before.modified().unwrap(), after.modified().unwrap());
}

#[test]
fn relative_session_storage_cannot_traverse_above_the_config_root() {
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("engine.toml");
    fs::write(
        &path,
        GOOD.replace(
            "session_dir = \"sessions\"",
            "session_dir = \"../sessions\"",
        ),
    )
    .expect("write fixture");

    let error = EngineConfig::load_with_env(&path, [], &CliOverrides::default())
        .expect_err("parent traversal must fail closed");

    assert!(
        error
            .to_string()
            .contains("must stay beneath the config root"),
        "{error}"
    );
}

#[cfg(unix)]
#[test]
fn wrong_external_permissions_fail_without_repair() {
    use std::os::unix::fs::PermissionsExt;

    let temp = tempfile::tempdir().expect("temp directory");
    let external = tempfile::tempdir().expect("external root");
    fs::set_permissions(external.path(), fs::Permissions::from_mode(0o755)).unwrap();
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let env = [(
        "VETINARI_ENGINE_KV_SESSION_DIR".into(),
        external.path().display().to_string(),
    )];

    EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect_err("wrong external permissions must fail closed");

    assert_eq!(
        fs::metadata(external.path()).unwrap().permissions().mode() & 0o777,
        0o755
    );
}

#[cfg(unix)]
#[test]
fn managed_root_rejects_a_symlink_component() {
    use std::os::unix::fs::symlink;

    let temp = tempfile::tempdir().expect("temp directory");
    let target = temp.path().join("target");
    fs::create_dir(&target).unwrap();
    symlink(&target, temp.path().join("linked")).unwrap();
    let path = temp.path().join("engine.toml");
    fs::write(
        &path,
        GOOD.replace("session_dir = \"sessions\"", "session_dir = \"linked/kv\""),
    )
    .expect("write fixture");

    let error = EngineConfig::load_with_env(&path, [], &CliOverrides::default())
        .expect_err("symlink components must fail closed");

    assert!(error.to_string().contains("exact private"), "{error}");
    assert!(!target.join("kv").exists());
}

#[cfg(windows)]
#[test]
fn inherited_external_acl_fails_closed() {
    let temp = tempfile::tempdir().expect("temp directory");
    let external = tempfile::tempdir().expect("inherited-ACL external root");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let env = [(
        "VETINARI_ENGINE_KV_SESSION_DIR".into(),
        external.path().display().to_string(),
    )];

    EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect_err("inherited external ACL must fail exact-private verification");

    assert!(external.path().is_dir());
}

#[cfg(windows)]
#[test]
fn managed_root_rejects_a_reparse_component() {
    use std::os::windows::fs::symlink_dir;

    let temp = tempfile::tempdir().expect("temp directory");
    let target = temp.path().join("target");
    fs::create_dir(&target).unwrap();
    if let Err(error) = symlink_dir(&target, temp.path().join("linked")) {
        assert_eq!(
            error.raw_os_error(),
            Some(1314),
            "cannot create reparse fixture: {error}"
        );
        return;
    }
    let path = temp.path().join("engine.toml");
    fs::write(
        &path,
        GOOD.replace("session_dir = \"sessions\"", "session_dir = \"linked/kv\""),
    )
    .expect("write fixture");

    EngineConfig::load_with_env(&path, [], &CliOverrides::default())
        .expect_err("reparse components must fail closed");

    assert!(!target.join("kv").exists());
}

#[test]
fn exact_shape_has_an_explicit_non_reloadable_policy_for_every_field() {
    let policies = EngineConfig::field_policies();
    assert_eq!(policies.len(), 22);
    assert!(policies.iter().all(|policy| !policy.hot_reloadable));
    assert_eq!(
        policies
            .iter()
            .map(|policy| policy.path)
            .collect::<HashSet<_>>()
            .len(),
        policies.len()
    );
}

#[test]
fn ordinary_inference_config_does_not_require_receipt_provisioning() {
    let config = EngineConfig::from_toml_str(GOOD).expect("ordinary config must remain valid");

    assert!(!config.receipts.is_provisioned());
}

#[test]
fn receipt_provisioning_is_all_or_none() {
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let env = [(
        "VETINARI_ENGINE_RECEIPT_TRUST_ANCHOR".to_owned(),
        temp.path().join("anchor.json").display().to_string(),
    )];

    let error = EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect_err("partial receipt provisioning must fail closed");

    assert!(error.to_string().contains("partial"), "{error}");
}

#[test]
fn receipt_pins_require_canonical_lowercase_sha256() {
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let anchor = temp.path().join("anchor.json");
    let ledger = temp.path().join("receipt.sqlite3");
    let env = [
        (
            "VETINARI_ENGINE_RECEIPT_TRUST_ANCHOR".to_owned(),
            anchor.display().to_string(),
        ),
        (
            "VETINARI_ENGINE_RECEIPT_LEDGER".to_owned(),
            ledger.display().to_string(),
        ),
        (
            "VETINARI_ENGINE_RECEIPT_ANCHOR_SHA256".to_owned(),
            "A".repeat(64),
        ),
        (
            "VETINARI_ENGINE_RECEIPT_AUTHORITY_PIN_SHA256".to_owned(),
            "b".repeat(64),
        ),
    ];

    let error = EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect_err("uppercase digest must fail closed");

    assert!(error.to_string().contains("lowercase"), "{error}");
}

#[test]
fn complete_receipt_env_uses_external_absolute_references() {
    let temp = tempfile::tempdir().expect("temp directory");
    let path = temp.path().join("engine.toml");
    fs::write(&path, GOOD).expect("write fixture");
    let anchor = temp.path().join("anchor.json");
    let ledger = temp.path().join("receipt.sqlite3");
    let env = [
        (
            "VETINARI_ENGINE_RECEIPT_TRUST_ANCHOR".to_owned(),
            anchor.display().to_string(),
        ),
        (
            "VETINARI_ENGINE_RECEIPT_LEDGER".to_owned(),
            ledger.display().to_string(),
        ),
        (
            "VETINARI_ENGINE_RECEIPT_ANCHOR_SHA256".to_owned(),
            "a".repeat(64),
        ),
        (
            "VETINARI_ENGINE_RECEIPT_AUTHORITY_PIN_SHA256".to_owned(),
            "b".repeat(64),
        ),
    ];

    let config = EngineConfig::load_with_env(&path, env, &CliOverrides::default())
        .expect("complete receipt references must load");

    assert!(config.receipts.is_provisioned());
    assert_eq!(
        config.receipts.trust_anchor_path.as_deref(),
        Some(anchor.as_path())
    );
    assert_eq!(
        config.receipts.ledger_path.as_deref(),
        Some(ledger.as_path())
    );
}
