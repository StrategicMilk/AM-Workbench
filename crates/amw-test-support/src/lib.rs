use amw_kernel::{KernelState, ResourceBudget, WorkerLease};
use amw_protocol::{ProtocolReceipt, ReceiptStatus};

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct MinimalWorkspaceFixture {
    pub crates: Vec<&'static str>,
    pub schemas: Vec<&'static str>,
    pub ci_commands: Vec<&'static str>,
}

pub fn valid_minimal_workspace() -> MinimalWorkspaceFixture {
    MinimalWorkspaceFixture {
        crates: vec!["amw-kernel", "amw-protocol", "amw-test-support"],
        schemas: vec![
            "schemas/amw-foundation-contract.schema.json",
            "schemas/amw-kernel-lifecycle.schema.json",
            "schemas/amw-protocol-receipt.schema.json",
            "schemas/ide_extension.schema.json",
        ],
        ci_commands: vec![
            "cargo test --workspace",
            "cargo fmt --check",
            "cargo clippy --workspace -- -D warnings",
            "${VETINARI_REPO_ROOT}/.venv312/Scripts/python.exe -m pytest tests/operator/test_rust_workspace_contract.py -q",
            "${VETINARI_REPO_ROOT}/.venv312/Scripts/python.exe scripts/check_rust_workspace.py --strict",
        ],
    }
}

pub fn corrupt_receipt_pair() -> Vec<ProtocolReceipt> {
    vec![
        ProtocolReceipt {
            id: "duplicate".to_string(),
            status: ReceiptStatus::Accepted,
            provenance: "fixture".to_string(),
        },
        ProtocolReceipt {
            id: "duplicate".to_string(),
            status: ReceiptStatus::FailedClosed,
            provenance: "fixture".to_string(),
        },
    ]
}

pub fn valid_kernel_handles() -> (KernelState, ResourceBudget, WorkerLease) {
    (
        KernelState::Ready,
        ResourceBudget::new(8),
        WorkerLease::acquire("fixture-worker", None).expect("fixture lease is valid"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use amw_protocol::validate_unique_receipts;

    #[test]
    fn fixture_lists_required_members() {
        let fixture = valid_minimal_workspace();
        assert!(fixture.crates.contains(&"amw-kernel"));
        assert!(fixture.crates.contains(&"amw-protocol"));
        assert!(fixture.crates.contains(&"amw-test-support"));
    }

    #[test]
    fn corrupt_fixture_is_actually_corrupt() {
        assert!(validate_unique_receipts(&corrupt_receipt_pair()).is_err());
    }

    #[test]
    fn ci_commands_are_repo_relative() {
        let fixture = valid_minimal_workspace();
        let forbidden_checkout = ["C:", "dev", "Vetinari"].join("/");
        assert!(fixture
            .ci_commands
            .iter()
            .all(|command| !command.contains(&forbidden_checkout)));
        assert!(fixture
            .ci_commands
            .iter()
            .any(|command| command.contains("${VETINARI_REPO_ROOT}")));
    }
}
