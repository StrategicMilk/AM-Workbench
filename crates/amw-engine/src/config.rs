use std::{
    collections::HashMap,
    fs,
    path::{Component, Path, PathBuf},
    str::FromStr,
    time::Duration,
};

use serde::{de, Deserialize, Deserializer, Serialize, Serializer};
use thiserror::Error;

#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("cannot read engine config {path}: {source}")]
    Read {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("cannot resolve engine config root for {path}: {source}")]
    ResolveRoot {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("engine config path has no parent directory: {path}")]
    MissingRoot { path: PathBuf },
    #[error("engine session storage must stay beneath the config root {root}: {path}")]
    UnsafeSessionRoot { root: PathBuf, path: PathBuf },
    #[error("engine session storage root {path} is not an exact private, current-owner directory: {source}")]
    SessionRootPrivacy {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("invalid engine config: {0}")]
    Parse(#[from] toml::de::Error),
    #[error("invalid value {value:?} for {key}: expected {expected}")]
    Override {
        key: String,
        value: String,
        expected: &'static str,
    },
    #[error("evaluation receipt provisioning is partial; anchor path, ledger path, anchor SHA-256, and authority-pin SHA-256 must be supplied together")]
    IncompleteReceiptProvisioning,
    #[error(
        "invalid evaluation receipt digest for {key}; expected 64 lowercase hexadecimal characters"
    )]
    InvalidReceiptDigest { key: &'static str },
    #[error("evaluation receipt {key} must be an absolute normalized external path: {path}")]
    InvalidReceiptPath { key: &'static str, path: PathBuf },
    #[error("evaluation receipt trust anchor and ledger must use distinct paths")]
    CollidingReceiptPaths,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct EngineConfig {
    pub server: ServerConfig,
    pub models: ModelsConfig,
    pub budgets: BudgetsConfig,
    pub slots: SlotsConfig,
    pub kv: KvConfig,
    pub idle: IdleConfig,
    pub scheduler: SchedulerConfig,
    pub log: LogConfig,
    #[serde(default)]
    pub receipts: ReceiptConfig,
}

/// External protected-state references required for engine-authored eval receipts.
#[derive(Clone, Debug, Default, Eq, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReceiptConfig {
    pub trust_anchor_path: Option<PathBuf>,
    pub ledger_path: Option<PathBuf>,
    pub anchor_sha256: Option<String>,
    pub authority_pin_sha256: Option<String>,
}

impl ReceiptConfig {
    /// Returns whether the complete external receipt authority is configured.
    ///
    /// Missing provisioning is legal for ordinary inference; partial
    /// provisioning is rejected by [`EngineConfig`] loading.
    pub fn is_provisioned(&self) -> bool {
        self.trust_anchor_path.is_some()
            && self.ledger_path.is_some()
            && self.anchor_sha256.is_some()
            && self.authority_pin_sha256.is_some()
    }

    fn validate(&self) -> Result<(), ConfigError> {
        let supplied = [
            self.trust_anchor_path.is_some(),
            self.ledger_path.is_some(),
            self.anchor_sha256.is_some(),
            self.authority_pin_sha256.is_some(),
        ];
        if !supplied.iter().any(|value| *value) {
            return Ok(());
        }
        if !supplied.iter().all(|value| *value) {
            return Err(ConfigError::IncompleteReceiptProvisioning);
        }
        let anchor = self
            .trust_anchor_path
            .as_deref()
            .expect("complete receipt config has an anchor path");
        let ledger = self
            .ledger_path
            .as_deref()
            .expect("complete receipt config has a ledger path");
        validate_external_receipt_path("trust_anchor_path", anchor)?;
        validate_external_receipt_path("ledger_path", ledger)?;
        if anchor == ledger {
            return Err(ConfigError::CollidingReceiptPaths);
        }
        for (key, digest) in [
            (
                "anchor_sha256",
                self.anchor_sha256
                    .as_deref()
                    .expect("complete receipt config has an anchor digest"),
            ),
            (
                "authority_pin_sha256",
                self.authority_pin_sha256
                    .as_deref()
                    .expect("complete receipt config has an authority pin"),
            ),
        ] {
            if digest.len() != 64
                || !digest
                    .bytes()
                    .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
            {
                return Err(ConfigError::InvalidReceiptDigest { key });
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    pub host: String,
    pub port: u16,
    pub auth_token_path: PathBuf,
    pub auth_policy_path: PathBuf,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ModelsConfig {
    pub dirs: Vec<PathBuf>,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct BudgetsConfig {
    pub vram_gb: f64,
    pub ram_gb: f64,
    pub margin_pct: f64,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SlotsConfig {
    pub count: usize,
    pub default_ctx: u32,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct KvConfig {
    pub cache_type_k: KvCacheType,
    pub cache_type_v: KvCacheType,
    pub session_dir: PathBuf,
    #[serde(skip)]
    pub root_policy: KvRootPolicy,
}

/// Whether the engine may create the configured KV storage root.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum KvRootPolicy {
    /// A relative path, or an absolute path beneath the config directory.
    #[default]
    Managed,
    /// An absolute path outside the config directory that must already be private.
    ExternalPreprovisioned,
}

impl KvConfig {
    pub(crate) fn durable_dir(&self) -> PathBuf {
        self.session_dir.join("durable")
    }

    pub(crate) fn scheduler_dir(&self) -> PathBuf {
        self.session_dir.join("scheduler")
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum KvCacheType {
    F16,
    Q8_0,
    Q4_0,
}

impl FromStr for KvCacheType {
    type Err = ();
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.to_ascii_lowercase().as_str() {
            "f16" => Ok(Self::F16),
            "q8_0" => Ok(Self::Q8_0),
            "q4_0" => Ok(Self::Q4_0),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct IdleConfig {
    pub keep_alive: KeepAlive,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum KeepAlive {
    Immediate,
    Forever,
    Duration(Duration),
}

impl FromStr for KeepAlive {
    type Err = ();
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        let value = value.trim();
        match value {
            "0" => return Ok(Self::Immediate),
            "-1" => return Ok(Self::Forever),
            _ => {}
        }
        let (digits, multiplier) = if let Some(v) = value.strip_suffix("ms") {
            (v, 1_u64)
        } else if let Some(v) = value.strip_suffix('s') {
            (v, 1_000)
        } else if let Some(v) = value.strip_suffix('m') {
            (v, 60_000)
        } else if let Some(v) = value.strip_suffix('h') {
            (v, 3_600_000)
        } else {
            (value, 1_000)
        };
        let amount = digits.parse::<u64>().map_err(|_| ())?;
        let millis = amount.checked_mul(multiplier).ok_or(())?;
        if millis == 0 {
            Ok(Self::Immediate)
        } else {
            Ok(Self::Duration(Duration::from_millis(millis)))
        }
    }
}

impl Serialize for KeepAlive {
    fn serialize<S: Serializer>(&self, serializer: S) -> Result<S::Ok, S::Error> {
        match self {
            Self::Immediate => serializer.serialize_i64(0),
            Self::Forever => serializer.serialize_i64(-1),
            Self::Duration(duration) => {
                serializer.serialize_str(&format!("{}ms", duration.as_millis()))
            }
        }
    }
}

impl<'de> Deserialize<'de> for KeepAlive {
    fn deserialize<D: Deserializer<'de>>(deserializer: D) -> Result<Self, D::Error> {
        #[derive(Deserialize)]
        #[serde(untagged)]
        enum Raw {
            Integer(i64),
            Text(String),
        }
        let raw = Raw::deserialize(deserializer)?;
        let text = match raw {
            Raw::Integer(v) => v.to_string(),
            Raw::Text(v) => v,
        };
        text.parse().map_err(|()| {
            de::Error::custom("keep_alive must be -1, 0, or a non-negative duration such as 5m")
        })
    }
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SchedulerConfig {
    pub preemption: bool,
    pub batch_token_budget: u32,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct LogConfig {
    pub level: LogLevel,
    pub dir: PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum LogLevel {
    Trace,
    Debug,
    Info,
    Warn,
    Error,
}

impl LogLevel {
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Trace => "trace",
            Self::Debug => "debug",
            Self::Info => "info",
            Self::Warn => "warn",
            Self::Error => "error",
        }
    }
}

impl FromStr for LogLevel {
    type Err = ();
    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value.to_ascii_lowercase().as_str() {
            "trace" => Ok(Self::Trace),
            "debug" => Ok(Self::Debug),
            "info" => Ok(Self::Info),
            "warn" => Ok(Self::Warn),
            "error" => Ok(Self::Error),
            _ => Err(()),
        }
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct CliOverrides {
    pub host: Option<String>,
    pub port: Option<u16>,
    pub model_dirs: Option<Vec<PathBuf>>,
    pub log_level: Option<LogLevel>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct FieldPolicy {
    pub path: &'static str,
    pub hot_reloadable: bool,
}

impl EngineConfig {
    pub fn from_toml_str(input: &str) -> Result<Self, ConfigError> {
        let config: Self = toml::from_str(input)?;
        config.receipts.validate()?;
        Ok(config)
    }

    pub fn load(path: &Path, cli: &CliOverrides) -> Result<Self, ConfigError> {
        let env = std::env::vars().filter(|(key, _)| key.starts_with("VETINARI_ENGINE_"));
        Self::load_with_env(path, env, cli)
    }

    pub fn load_with_env<I>(path: &Path, env: I, cli: &CliOverrides) -> Result<Self, ConfigError>
    where
        I: IntoIterator<Item = (String, String)>,
    {
        let input = fs::read_to_string(path).map_err(|source| ConfigError::Read {
            path: path.to_owned(),
            source,
        })?;
        let resolved_path = if path.is_absolute() {
            path.to_owned()
        } else {
            fs::canonicalize(path).map_err(|source| ConfigError::ResolveRoot {
                path: path.to_owned(),
                source,
            })?
        };
        let config_root = resolved_path
            .parent()
            .filter(|parent| !parent.as_os_str().is_empty())
            .ok_or_else(|| ConfigError::MissingRoot {
                path: resolved_path.clone(),
            })?;
        let mut config: Self = toml::from_str(&input)?;
        config.apply_env(env.into_iter().collect())?;
        if let Some(value) = &cli.host {
            config.server.host.clone_from(value);
        }
        if let Some(value) = cli.port {
            config.server.port = value;
        }
        if let Some(value) = &cli.model_dirs {
            config.models.dirs.clone_from(value);
        }
        if let Some(value) = cli.log_level {
            config.log.level = value;
        }
        config.receipts.validate()?;
        let (session_dir, root_policy) = resolve_session_root(config_root, &config.kv.session_dir)?;
        config.kv.session_dir = session_dir;
        config.kv.root_policy = root_policy;
        Ok(config)
    }

    pub fn field_policies() -> &'static [FieldPolicy] {
        const PATHS: &[FieldPolicy] = &[
            FieldPolicy {
                path: "server.host",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "server.port",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "server.auth_token_path",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "server.auth_policy_path",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "models.dirs",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "budgets.vram_gb",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "budgets.ram_gb",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "budgets.margin_pct",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "slots.count",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "slots.default_ctx",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "kv.cache_type_k",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "kv.cache_type_v",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "kv.session_dir",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "idle.keep_alive",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "scheduler.preemption",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "scheduler.batch_token_budget",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "log.level",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "log.dir",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "receipts.trust_anchor_path",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "receipts.ledger_path",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "receipts.anchor_sha256",
                hot_reloadable: false,
            },
            FieldPolicy {
                path: "receipts.authority_pin_sha256",
                hot_reloadable: false,
            },
        ];
        PATHS
    }

    fn apply_env(&mut self, env: HashMap<String, String>) -> Result<(), ConfigError> {
        macro_rules! parsed {
            ($key:literal, $field:expr, $ty:ty, $expected:literal) => {
                if let Some(value) = env.get($key) {
                    $field = value.parse::<$ty>().map_err(|_| ConfigError::Override {
                        key: $key.into(),
                        value: value.clone(),
                        expected: $expected,
                    })?;
                }
            };
        }
        if let Some(v) = env.get("VETINARI_ENGINE_SERVER_HOST") {
            self.server.host.clone_from(v);
        }
        parsed!(
            "VETINARI_ENGINE_SERVER_PORT",
            self.server.port,
            u16,
            "a TCP port (0-65535)"
        );
        if let Some(v) = env.get("VETINARI_ENGINE_SERVER_AUTH_TOKEN_PATH") {
            self.server.auth_token_path = PathBuf::from(v);
        }
        if let Some(v) = env.get("VETINARI_ENGINE_SERVER_AUTH_POLICY_PATH") {
            self.server.auth_policy_path = PathBuf::from(v);
        }
        if let Some(v) = env.get("VETINARI_ENGINE_MODELS_DIRS") {
            self.models.dirs = v
                .split(';')
                .filter(|x| !x.is_empty())
                .map(PathBuf::from)
                .collect();
        }
        parsed!(
            "VETINARI_ENGINE_BUDGETS_VRAM_GB",
            self.budgets.vram_gb,
            f64,
            "a number"
        );
        parsed!(
            "VETINARI_ENGINE_BUDGETS_RAM_GB",
            self.budgets.ram_gb,
            f64,
            "a number"
        );
        parsed!(
            "VETINARI_ENGINE_BUDGETS_MARGIN_PCT",
            self.budgets.margin_pct,
            f64,
            "a number"
        );
        parsed!(
            "VETINARI_ENGINE_SLOTS_COUNT",
            self.slots.count,
            usize,
            "a non-negative integer"
        );
        parsed!(
            "VETINARI_ENGINE_SLOTS_DEFAULT_CTX",
            self.slots.default_ctx,
            u32,
            "a non-negative integer"
        );
        parsed!(
            "VETINARI_ENGINE_KV_CACHE_TYPE_K",
            self.kv.cache_type_k,
            KvCacheType,
            "f16, q8_0, or q4_0"
        );
        parsed!(
            "VETINARI_ENGINE_KV_CACHE_TYPE_V",
            self.kv.cache_type_v,
            KvCacheType,
            "f16, q8_0, or q4_0"
        );
        if let Some(v) = env.get("VETINARI_ENGINE_KV_SESSION_DIR") {
            self.kv.session_dir = PathBuf::from(v);
        }
        parsed!(
            "VETINARI_ENGINE_IDLE_KEEP_ALIVE",
            self.idle.keep_alive,
            KeepAlive,
            "-1, 0, or a duration"
        );
        parsed!(
            "VETINARI_ENGINE_SCHEDULER_PREEMPTION",
            self.scheduler.preemption,
            bool,
            "true or false"
        );
        parsed!(
            "VETINARI_ENGINE_SCHEDULER_BATCH_TOKEN_BUDGET",
            self.scheduler.batch_token_budget,
            u32,
            "a non-negative integer"
        );
        parsed!(
            "VETINARI_ENGINE_LOG_LEVEL",
            self.log.level,
            LogLevel,
            "trace, debug, info, warn, or error"
        );
        if let Some(v) = env.get("VETINARI_ENGINE_LOG_DIR") {
            self.log.dir = PathBuf::from(v);
        }
        if let Some(v) = env.get("VETINARI_ENGINE_RECEIPT_TRUST_ANCHOR") {
            self.receipts.trust_anchor_path = Some(PathBuf::from(v));
        }
        if let Some(v) = env.get("VETINARI_ENGINE_RECEIPT_LEDGER") {
            self.receipts.ledger_path = Some(PathBuf::from(v));
        }
        if let Some(v) = env.get("VETINARI_ENGINE_RECEIPT_ANCHOR_SHA256") {
            self.receipts.anchor_sha256 = Some(v.clone());
        }
        if let Some(v) = env.get("VETINARI_ENGINE_RECEIPT_AUTHORITY_PIN_SHA256") {
            self.receipts.authority_pin_sha256 = Some(v.clone());
        }
        Ok(())
    }
}

fn validate_external_receipt_path(key: &'static str, path: &Path) -> Result<(), ConfigError> {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::ParentDir | Component::CurDir))
    {
        return Err(ConfigError::InvalidReceiptPath {
            key,
            path: path.to_owned(),
        });
    }
    Ok(())
}

fn resolve_session_root(
    config_root: &Path,
    configured: &Path,
) -> Result<(PathBuf, KvRootPolicy), ConfigError> {
    let canonical_root =
        fs::canonicalize(config_root).map_err(|source| ConfigError::ResolveRoot {
            path: config_root.to_owned(),
            source,
        })?;
    if configured
        .components()
        .any(|component| matches!(component, Component::ParentDir))
    {
        return Err(ConfigError::UnsafeSessionRoot {
            root: canonical_root,
            path: configured.to_owned(),
        });
    }
    let candidate = if configured.is_absolute() {
        configured.to_owned()
    } else {
        config_root.join(configured)
    };
    crate::store::session::verify_no_reparse_components(&candidate).map_err(|source| {
        ConfigError::SessionRootPrivacy {
            path: candidate.clone(),
            source,
        }
    })?;

    let policy = if candidate.starts_with(config_root) || candidate.starts_with(&canonical_root) {
        KvRootPolicy::Managed
    } else if configured.is_absolute() {
        KvRootPolicy::ExternalPreprovisioned
    } else {
        return Err(ConfigError::UnsafeSessionRoot {
            root: canonical_root,
            path: candidate,
        });
    };
    match fs::symlink_metadata(&candidate) {
        Ok(_) => crate::store::session::verify_secure_directory(&candidate).map_err(|source| {
            ConfigError::SessionRootPrivacy {
                path: candidate.clone(),
                source,
            }
        })?,
        Err(error)
            if error.kind() == std::io::ErrorKind::NotFound && policy == KvRootPolicy::Managed => {}
        Err(source) => {
            return Err(ConfigError::SessionRootPrivacy {
                path: candidate,
                source,
            })
        }
    }
    Ok((candidate, policy))
}

impl Default for EngineConfig {
    fn default() -> Self {
        Self {
            server: ServerConfig {
                host: "127.0.0.1".into(),
                port: 10_933,
                auth_token_path: "auth-token".into(),
                auth_policy_path: "auth-policy.json".into(),
            },
            models: ModelsConfig {
                dirs: vec!["models".into()],
            },
            budgets: BudgetsConfig {
                vram_gb: 0.0,
                ram_gb: 30.0,
                margin_pct: 10.0,
            },
            slots: SlotsConfig {
                count: 1,
                default_ctx: 4_096,
            },
            kv: KvConfig {
                cache_type_k: KvCacheType::F16,
                cache_type_v: KvCacheType::F16,
                session_dir: "sessions".into(),
                root_policy: KvRootPolicy::Managed,
            },
            idle: IdleConfig {
                keep_alive: KeepAlive::Duration(Duration::from_secs(300)),
            },
            scheduler: SchedulerConfig {
                preemption: true,
                batch_token_budget: 2_048,
            },
            log: LogConfig {
                level: LogLevel::Info,
                dir: "logs".into(),
            },
            receipts: ReceiptConfig::default(),
        }
    }
}
