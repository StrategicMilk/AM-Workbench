use std::collections::BTreeSet;
use std::str::FromStr;

use amw_kernel::KernelError;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct RequestEnvelope {
    pub id: String,
    pub command: String,
    pub worker: Option<WorkerManifestHandle>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ResponseEnvelope {
    pub request_id: String,
    pub receipt: ProtocolReceipt,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ProtocolReceipt {
    pub id: String,
    pub status: ReceiptStatus,
    pub provenance: String,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum ReceiptStatus {
    Accepted,
    FailedClosed,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct ExtensionManifestHandle {
    pub extension_id: String,
    pub signature: String,
    pub permission_set: Vec<String>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct WorkerManifestHandle {
    pub worker_id: String,
    pub sdk_version: String,
    pub capabilities: Vec<String>,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum WorkerCapability {
    SubmitGoal,
    ReadContext,
    WriteEvidence,
    RunSandbox,
}

impl WorkerCapability {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::SubmitGoal => "tool:submit_goal",
            Self::ReadContext => "tool:read_context",
            Self::WriteEvidence => "tool:write_evidence",
            Self::RunSandbox => "tool:run_sandbox",
        }
    }
}

impl FromStr for WorkerCapability {
    type Err = KernelError;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "tool:submit_goal" => Ok(Self::SubmitGoal),
            "tool:read_context" => Ok(Self::ReadContext),
            "tool:write_evidence" => Ok(Self::WriteEvidence),
            "tool:run_sandbox" => Ok(Self::RunSandbox),
            other => Err(KernelError::UnknownSignal(format!(
                "worker-capability:{other}"
            ))),
        }
    }
}

pub const FOUNDATION_CONTRACT_SCHEMA: &str = "schemas/amw-foundation-contract.schema.json";
pub const KERNEL_LIFECYCLE_SCHEMA: &str = "schemas/amw-kernel-lifecycle.schema.json";
pub const PROTOCOL_RECEIPT_SCHEMA: &str = "schemas/amw-protocol-receipt.schema.json";
pub const IDE_EXTENSION_SCHEMA: &str = "schemas/ide_extension.schema.json";
pub const SIGNAL_REQUEST_ID: &str = "request-id";
pub const SIGNAL_COMMAND: &str = "command";
pub const SIGNAL_WORKER_ID: &str = "worker-id";
pub const SIGNAL_SDK_VERSION: &str = "sdk-version";
pub const SIGNAL_WORKER_CAPABILITIES: &str = "worker-capabilities";

pub fn validate_unique_receipts(receipts: &[ProtocolReceipt]) -> Result<(), KernelError> {
    let mut seen = BTreeSet::new();
    for receipt in receipts {
        if !seen.insert(receipt.id.as_str()) {
            return Err(KernelError::DuplicateReceipt(receipt.id.clone()));
        }
    }
    Ok(())
}

pub fn validate_request_envelope(envelope: &RequestEnvelope) -> Result<(), KernelError> {
    if envelope.id.trim().is_empty() {
        return Err(KernelError::MissingSignal(SIGNAL_REQUEST_ID));
    }
    if envelope.command.trim().is_empty() {
        return Err(KernelError::MissingSignal(SIGNAL_COMMAND));
    }
    if let Some(worker) = &envelope.worker {
        validate_worker_manifest_handle(worker)?;
    }
    Ok(())
}

pub fn protocol_schema_paths() -> [&'static str; 4] {
    [
        FOUNDATION_CONTRACT_SCHEMA,
        KERNEL_LIFECYCLE_SCHEMA,
        PROTOCOL_RECEIPT_SCHEMA,
        IDE_EXTENSION_SCHEMA,
    ]
}

pub fn validate_extension_manifest_handle(
    handle: &ExtensionManifestHandle,
) -> Result<(), KernelError> {
    let manifest = amw_kernel::ExtensionManifest {
        extension_id: handle.extension_id.clone(),
        schema_version: "ide-extension.v1".to_string(),
        signature: handle.signature.clone(),
        permissions: handle.permission_set.clone(),
        retention_days: 7,
    };
    let admission = amw_kernel::admit_extension_manifest(&manifest);
    if admission.admitted {
        Ok(())
    } else {
        let detail = admission
            .support
            .map(|support| {
                format!(
                    "extension_manifest:{}:{}:{}",
                    support.code, support.message, support.recovery
                )
            })
            .unwrap_or_else(|| "extension_manifest:UNKNOWN".to_string());
        Err(KernelError::UnknownSignal(detail))
    }
}

pub fn validate_worker_manifest_handle(handle: &WorkerManifestHandle) -> Result<(), KernelError> {
    if handle.worker_id.trim().is_empty() {
        return Err(KernelError::MissingSignal(SIGNAL_WORKER_ID));
    }
    if handle.sdk_version.trim().is_empty() {
        return Err(KernelError::MissingSignal(SIGNAL_SDK_VERSION));
    }
    if handle.capabilities.is_empty() {
        return Err(KernelError::MissingSignal(SIGNAL_WORKER_CAPABILITIES));
    }
    let mut seen = BTreeSet::new();
    for capability in &handle.capabilities {
        let parsed = capability.parse::<WorkerCapability>()?;
        if !seen.insert(parsed.as_str()) {
            return Err(KernelError::DuplicateReceipt(format!(
                "worker-capability:{}",
                parsed.as_str()
            )));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn duplicate_receipts_fail_closed() {
        let receipts = vec![
            ProtocolReceipt {
                id: "receipt-1".to_string(),
                status: ReceiptStatus::Accepted,
                provenance: "local".to_string(),
            },
            ProtocolReceipt {
                id: "receipt-1".to_string(),
                status: ReceiptStatus::FailedClosed,
                provenance: "local".to_string(),
            },
        ];
        assert!(matches!(
            validate_unique_receipts(&receipts),
            Err(KernelError::DuplicateReceipt(id)) if id == "receipt-1"
        ));
    }

    #[test]
    fn protocol_signal_constants_match_expected_values() {
        assert_eq!(SIGNAL_REQUEST_ID, "request-id");
        assert_eq!(SIGNAL_COMMAND, "command");
        assert_eq!(SIGNAL_WORKER_ID, "worker-id");
        assert_eq!(SIGNAL_SDK_VERSION, "sdk-version");
        assert_eq!(SIGNAL_WORKER_CAPABILITIES, "worker-capabilities");
    }

    #[test]
    fn request_envelope_validates_worker_manifest_at_protocol_boundary() {
        let valid = RequestEnvelope {
            id: "request-1".to_string(),
            command: "run".to_string(),
            worker: Some(WorkerManifestHandle {
                worker_id: "worker-1".to_string(),
                sdk_version: "2026.6".to_string(),
                capabilities: vec![WorkerCapability::ReadContext.as_str().to_string()],
            }),
        };
        assert!(validate_request_envelope(&valid).is_ok());

        let invalid = RequestEnvelope {
            worker: Some(WorkerManifestHandle {
                capabilities: vec!["tool:anything".to_string()],
                ..valid.worker.clone().expect("worker")
            }),
            ..valid
        };
        assert!(matches!(
            validate_request_envelope(&invalid),
            Err(KernelError::UnknownSignal(value)) if value == "worker-capability:tool:anything"
        ));
    }

    #[test]
    fn schema_generation_entry_points_are_stable() {
        assert_eq!(
            protocol_schema_paths(),
            [
                "schemas/amw-foundation-contract.schema.json",
                "schemas/amw-kernel-lifecycle.schema.json",
                "schemas/amw-protocol-receipt.schema.json",
                "schemas/ide_extension.schema.json"
            ]
        );
    }

    #[test]
    fn schema_generation_entry_points_resolve_to_shipped_assets() {
        let crate_root = std::path::Path::new(env!("CARGO_MANIFEST_DIR"));
        for schema_path in protocol_schema_paths() {
            let full_path = crate_root.join(schema_path);
            assert!(
                full_path.is_file(),
                "protocol schema constant {schema_path} must resolve inside amw-protocol"
            );
        }
    }

    #[test]
    fn extension_manifest_handle_validates_through_kernel_admission() {
        let mut handle = ExtensionManifestHandle {
            extension_id: "ide.local".to_string(),
            signature: String::new(),
            permission_set: vec!["tool:submit_goal".to_string()],
        };
        let manifest = amw_kernel::ExtensionManifest {
            extension_id: handle.extension_id.clone(),
            schema_version: "ide-extension.v1".to_string(),
            signature: String::new(),
            permissions: handle.permission_set.clone(),
            retention_days: 7,
        };
        handle.signature = amw_kernel::extensions::trusted_production_signature(&manifest);
        assert!(validate_extension_manifest_handle(&handle).is_ok());

        let bad = ExtensionManifestHandle {
            signature: "unsigned".to_string(),
            ..handle
        };
        assert!(validate_extension_manifest_handle(&bad).is_err());
    }

    #[test]
    fn protocol_kernel_contracts_preserve_extension_admission_details() {
        let missing = ExtensionManifestHandle {
            extension_id: "ide.local".to_string(),
            signature: String::new(),
            permission_set: vec!["tool:submit_goal".to_string()],
        };
        let malformed = ExtensionManifestHandle {
            signature: "unsigned".to_string(),
            ..missing.clone()
        };

        let missing_err = validate_extension_manifest_handle(&missing).expect_err("missing");
        let malformed_err = validate_extension_manifest_handle(&malformed).expect_err("malformed");

        assert!(matches!(
            missing_err,
            KernelError::UnknownSignal(value) if value.contains("EXT_SIGNATURE_MISSING")
        ));
        assert!(matches!(
            malformed_err,
            KernelError::UnknownSignal(value) if value.contains("EXT_SIGNATURE_FORMAT")
        ));
    }

    #[test]
    fn security_extension_manifest_handle_rejects_fixture_signature() {
        let handle = ExtensionManifestHandle {
            extension_id: "ide.local".to_string(),
            signature: "signed:local-fixture".to_string(),
            permission_set: vec!["tool:submit_goal".to_string()],
        };

        assert!(validate_extension_manifest_handle(&handle).is_err());
    }

    #[test]
    fn protocol_kernel_contracts_validate_worker_capability_vocabulary() {
        let valid = WorkerManifestHandle {
            worker_id: "worker-1".to_string(),
            sdk_version: "2026.6".to_string(),
            capabilities: vec![
                WorkerCapability::SubmitGoal.as_str().to_string(),
                WorkerCapability::WriteEvidence.as_str().to_string(),
            ],
        };
        assert!(validate_worker_manifest_handle(&valid).is_ok());

        let invalid = WorkerManifestHandle {
            capabilities: vec!["tool:unbounded_shell".to_string()],
            ..valid.clone()
        };
        assert!(matches!(
            validate_worker_manifest_handle(&invalid),
            Err(KernelError::UnknownSignal(value))
                if value == "worker-capability:tool:unbounded_shell"
        ));

        let duplicate = WorkerManifestHandle {
            capabilities: vec![
                WorkerCapability::SubmitGoal.as_str().to_string(),
                WorkerCapability::SubmitGoal.as_str().to_string(),
            ],
            ..valid
        };
        assert!(matches!(
            validate_worker_manifest_handle(&duplicate),
            Err(KernelError::DuplicateReceipt(value))
                if value == "worker-capability:tool:submit_goal"
        ));
    }
}
