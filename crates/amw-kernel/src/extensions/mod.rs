use sha2::{Digest, Sha256};
use std::collections::BTreeSet;

pub const PRODUCTION_SIGNING_KEY_2026: &str = "vetinari-marketplace-root-2026";
const TRUSTED_PRODUCTION_SIGNING_KEYS: &[&str] = &[PRODUCTION_SIGNING_KEY_2026];

#[derive(Debug, Clone, Eq, PartialEq, Ord, PartialOrd)]
pub enum ExtensionPermission {
    Tool(String),
    Resource(String),
    FilesystemRead(String),
}

impl ExtensionPermission {
    pub fn from_declared(value: &str) -> Option<Self> {
        let (kind, target) = value.split_once(':')?;
        if target.trim().is_empty() || target.contains("..") {
            return None;
        }
        match kind {
            "tool" => Some(Self::Tool(target.to_string())),
            "resource" => Some(Self::Resource(target.to_string())),
            "fs_read" if target.starts_with("workspace/") => {
                Some(Self::FilesystemRead(target.to_string()))
            }
            _ => None,
        }
    }

    pub fn as_declared(&self) -> String {
        match self {
            Self::Tool(value) => format!("tool:{value}"),
            Self::Resource(value) => format!("resource:{value}"),
            Self::FilesystemRead(value) => format!("fs_read:{value}"),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SupportEnvelope {
    pub code: &'static str,
    pub message: &'static str,
    pub recovery: &'static str,
}

impl SupportEnvelope {
    pub const fn new(code: &'static str, message: &'static str, recovery: &'static str) -> Self {
        Self {
            code,
            message,
            recovery,
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ExtensionReceipt {
    pub receipt_id: String,
    pub extension_id: String,
    pub action: &'static str,
    pub status: &'static str,
    pub redacted_subject: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ExtensionManifest {
    pub extension_id: String,
    pub schema_version: String,
    pub signature: String,
    pub permissions: Vec<String>,
    pub retention_days: u16,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ExtensionAdmission {
    pub admitted: bool,
    pub permissions: Vec<ExtensionPermission>,
    pub receipt: ExtensionReceipt,
    pub support: Option<SupportEnvelope>,
}

pub fn local_development_signature(manifest: &ExtensionManifest) -> String {
    format!(
        "sig:local-dev-sha256:v1:{}",
        signature_digest(manifest, "local-dev")
    )
}

pub fn trusted_production_signature(manifest: &ExtensionManifest) -> String {
    production_signature_for_key(manifest, TRUSTED_PRODUCTION_SIGNING_KEYS[0])
}

fn production_signature_for_key(manifest: &ExtensionManifest, key_id: &str) -> String {
    format!(
        "sig:production-sha256:v1:{key_id}:{}",
        signature_digest(manifest, key_id)
    )
}

fn signature_digest(manifest: &ExtensionManifest, key_id: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(key_id.as_bytes());
    hasher.update([0]);
    hasher.update(manifest.schema_version.as_bytes());
    hasher.update([0]);
    hasher.update(manifest.extension_id.as_bytes());
    hasher.update([0]);
    hasher.update(manifest.retention_days.to_string().as_bytes());
    hasher.update([0]);
    for permission in &manifest.permissions {
        hasher.update(permission.as_bytes());
        hasher.update([0]);
    }
    format!("{:x}", hasher.finalize())
}

pub fn admit_extension_manifest(manifest: &ExtensionManifest) -> ExtensionAdmission {
    let denied = |code, message, recovery| ExtensionAdmission {
        admitted: false,
        permissions: Vec::new(),
        receipt: receipt_for(manifest, "admit", "failed_closed"),
        support: Some(SupportEnvelope::new(code, message, recovery)),
    };

    if manifest.schema_version != "ide-extension.v1" {
        return denied(
            "EXT_SCHEMA",
            "extension schema is unsupported",
            "reload with a current schema",
        );
    }
    if manifest.signature.trim().is_empty() {
        return denied(
            "EXT_SIGNATURE_MISSING",
            "extension signature is missing",
            "install a package signed with a trusted key",
        );
    }
    if manifest.signature.starts_with("signed:production:v1:") {
        return denied(
            "EXT_SIGNING_UNIMPLEMENTED",
            "placeholder extension signing tokens are not a cryptographic contract",
            "install a package signed with the production SHA-256 contract",
        );
    }
    let Some(signature_body) = manifest.signature.strip_prefix("sig:production-sha256:v1:") else {
        return denied(
            "EXT_SIGNATURE_FORMAT",
            "extension signature format is not trusted",
            "install a package signed with a trusted key",
        );
    };
    let Some((key_id, _signed_payload)) = signature_body.split_once(':') else {
        return denied(
            "EXT_SIGNATURE_FORMAT",
            "extension signature format is not trusted",
            "install a package signed with a trusted key",
        );
    };
    if !TRUSTED_PRODUCTION_SIGNING_KEYS.contains(&key_id) {
        return denied(
            "EXT_SIGNATURE_KEY",
            "extension signing key is not trusted",
            "install a package signed with a trusted key",
        );
    }
    if manifest.signature != production_signature_for_key(manifest, key_id) {
        return denied(
            "EXT_SIGNATURE",
            "extension signature is not trusted",
            "install a package signed with a trusted key",
        );
    }
    if manifest.retention_days > 30 {
        return denied(
            "EXT_RETENTION",
            "extension retention exceeds policy",
            "lower retention to thirty days or less",
        );
    }

    let mut seen = BTreeSet::new();
    let mut permissions = Vec::new();
    for raw in &manifest.permissions {
        let Some(permission) = ExtensionPermission::from_declared(raw) else {
            return denied(
                "EXT_PERMISSION",
                "extension permission is undeclared or outside scope",
                "declare a supported permission",
            );
        };
        if !seen.insert(permission.as_declared()) {
            return denied(
                "EXT_PERMISSION_DUP",
                "extension permission is duplicated",
                "remove duplicate permissions",
            );
        }
        permissions.push(permission);
    }
    if permissions.is_empty() {
        return denied(
            "EXT_PERMISSION_MISSING",
            "extension permissions are missing",
            "declare least-privilege permissions",
        );
    }

    ExtensionAdmission {
        admitted: true,
        permissions,
        receipt: receipt_for(manifest, "admit", "accepted"),
        support: None,
    }
}

pub fn admit_local_development_extension_manifest(
    manifest: &ExtensionManifest,
) -> ExtensionAdmission {
    let mut local_manifest = manifest.clone();
    if local_manifest.signature != local_development_signature(&local_manifest) {
        return ExtensionAdmission {
            admitted: false,
            permissions: Vec::new(),
            receipt: receipt_for(&local_manifest, "admit", "failed_closed"),
            support: Some(SupportEnvelope::new(
                "EXT_LOCAL_SIGNATURE",
                "local development extension signature is not trusted",
                "re-sign the local development package",
            )),
        };
    }
    local_manifest.signature = trusted_production_signature(&local_manifest);
    admit_extension_manifest(&local_manifest)
}

pub fn rollback_extension_admission(
    manifest: &ExtensionManifest,
    reason: &str,
) -> ExtensionReceipt {
    let mut receipt = receipt_for(manifest, "rollback", "completed");
    receipt.redacted_subject = redact_secret_bearing(reason);
    receipt
}

pub fn update_extension(manifest: &ExtensionManifest) -> Result<ExtensionReceipt, SupportEnvelope> {
    let _ = manifest;
    Err(extension_lifecycle_unimplemented())
}

pub fn revoke_extension(extension_id: &str) -> Result<ExtensionReceipt, SupportEnvelope> {
    let _ = extension_id;
    Err(extension_lifecycle_unimplemented())
}

pub fn uninstall_extension(extension_id: &str) -> Result<ExtensionReceipt, SupportEnvelope> {
    let _ = extension_id;
    Err(extension_lifecycle_unimplemented())
}

fn extension_lifecycle_unimplemented() -> SupportEnvelope {
    SupportEnvelope::new(
        "EXT_LIFECYCLE_UNIMPLEMENTED",
        "extension update, revoke, and uninstall lifecycle operations are not implemented",
        "leave the extension unchanged until lifecycle execution is implemented",
    )
}

pub fn redact_secret_bearing(value: &str) -> String {
    let lowered = value.to_ascii_lowercase();
    if lowered.contains("secret") || lowered.contains("token") || lowered.contains("key") {
        "[redacted]".to_string()
    } else {
        value.chars().take(120).collect()
    }
}

fn receipt_for(
    manifest: &ExtensionManifest,
    action: &'static str,
    status: &'static str,
) -> ExtensionReceipt {
    ExtensionReceipt {
        receipt_id: format!("{}:{action}:{status}", manifest.extension_id),
        extension_id: manifest.extension_id.clone(),
        action,
        status,
        redacted_subject: redact_secret_bearing(&manifest.extension_id),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn manifest(permissions: Vec<&str>) -> ExtensionManifest {
        let mut manifest = ExtensionManifest {
            extension_id: "ide.local".to_string(),
            schema_version: "ide-extension.v1".to_string(),
            signature: String::new(),
            permissions: permissions.into_iter().map(str::to_string).collect(),
            retention_days: 7,
        };
        manifest.signature = trusted_production_signature(&manifest);
        manifest
    }

    #[test]
    fn extension_signing_key_constant_value() {
        assert_eq!(
            PRODUCTION_SIGNING_KEY_2026,
            concat!("vetinari-marketplace", "-root-2026")
        );
        assert!(TRUSTED_PRODUCTION_SIGNING_KEYS.contains(&PRODUCTION_SIGNING_KEY_2026));
    }

    #[test]
    fn signed_manifest_with_declared_permissions_is_admitted() {
        let admission =
            admit_extension_manifest(&manifest(vec!["tool:submit_goal", "resource:workspace"]));
        assert!(admission.admitted);
        assert_eq!(admission.receipt.status, "accepted");
        assert_eq!(admission.permissions.len(), 2);
    }

    #[test]
    fn malformed_or_undeclared_permissions_fail_closed() {
        let admission = admit_extension_manifest(&manifest(vec!["fs_read:../secret"]));
        assert!(!admission.admitted);
        assert_eq!(
            admission.support.expect("support envelope").code,
            "EXT_PERMISSION"
        );
    }

    #[test]
    fn attacker_controlled_signed_prefix_fails_closed() {
        let mut manifest = manifest(vec!["tool:submit_goal"]);
        manifest.signature = "signed:any-attacker-controlled-string".to_string();
        let admission = admit_extension_manifest(&manifest);
        assert!(!admission.admitted);
        assert_eq!(
            admission.support.expect("support envelope").code,
            "EXT_SIGNATURE_FORMAT"
        );
    }

    #[test]
    fn security_extension_missing_and_malformed_signatures_fail_closed() {
        let mut missing = manifest(vec!["tool:submit_goal"]);
        missing.signature.clear();
        let missing_admission = admit_extension_manifest(&missing);
        assert!(!missing_admission.admitted);
        assert_eq!(
            missing_admission.support.expect("support envelope").code,
            "EXT_SIGNATURE_MISSING"
        );

        let mut malformed = manifest(vec!["tool:submit_goal"]);
        malformed.signature =
            format!("sig:production-sha256:v1:{PRODUCTION_SIGNING_KEY_2026}:tampered");
        let malformed_admission = admit_extension_manifest(&malformed);
        assert!(!malformed_admission.admitted);
        assert_eq!(
            malformed_admission.support.expect("support envelope").code,
            "EXT_SIGNATURE"
        );
    }

    #[test]
    fn extensions_signing_placeholder_rejected() {
        let mut old_placeholder = manifest(vec!["tool:submit_goal"]);
        old_placeholder.signature = format!(
            "signed:production:v1:{PRODUCTION_SIGNING_KEY_2026}:ide-extension.v1:ide.local:7:tool:submit_goal"
        );

        let admission = admit_extension_manifest(&old_placeholder);

        assert!(!admission.admitted);
        assert_eq!(
            admission.support.expect("support envelope").code,
            "EXT_SIGNING_UNIMPLEMENTED"
        );
    }

    #[test]
    fn extension_lifecycle_stubs_fail_closed() {
        let manifest = manifest(vec!["tool:submit_goal"]);

        assert_eq!(
            update_extension(&manifest)
                .expect_err("update is fail-closed")
                .code,
            "EXT_LIFECYCLE_UNIMPLEMENTED"
        );
        assert_eq!(
            revoke_extension("ide.local")
                .expect_err("revoke is fail-closed")
                .code,
            "EXT_LIFECYCLE_UNIMPLEMENTED"
        );
        assert_eq!(
            uninstall_extension("ide.local")
                .expect_err("uninstall is fail-closed")
                .code,
            "EXT_LIFECYCLE_UNIMPLEMENTED"
        );
    }

    #[test]
    fn security_extension_admission_rejects_local_dev_and_untrusted_production_keys_by_default() {
        let mut local_dev = manifest(vec!["tool:submit_goal"]);
        local_dev.signature = local_development_signature(&local_dev);
        let local_admission = admit_extension_manifest(&local_dev);
        assert!(!local_admission.admitted);
        assert_eq!(
            local_admission.support.expect("support envelope").code,
            "EXT_SIGNATURE_FORMAT"
        );

        let mut untrusted = manifest(vec!["tool:submit_goal"]);
        untrusted.signature = production_signature_for_key(&untrusted, "attacker-key");
        let untrusted_admission = admit_extension_manifest(&untrusted);
        assert!(!untrusted_admission.admitted);
        assert_eq!(
            untrusted_admission.support.expect("support envelope").code,
            "EXT_SIGNATURE_KEY"
        );
    }

    #[test]
    fn local_development_extension_admission_requires_explicit_local_path() {
        let mut local_dev = manifest(vec!["tool:submit_goal"]);
        local_dev.signature = local_development_signature(&local_dev);

        let admission = admit_local_development_extension_manifest(&local_dev);

        assert!(admission.admitted);
        assert_eq!(admission.receipt.status, "accepted");
    }

    #[test]
    fn rollback_receipt_redacts_secret_bearing_reason() {
        let receipt =
            rollback_extension_admission(&manifest(vec!["tool:submit_goal"]), "token=abc123");
        assert_eq!(receipt.status, "completed");
        assert_eq!(receipt.redacted_subject, "[redacted]");
    }
}
