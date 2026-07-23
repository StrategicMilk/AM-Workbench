use crate::error::KernelError;

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct LiveActionSignal {
    pub target_ref: String,
    pub evidence_id: String,
    pub safety_signal_present: bool,
    pub approval_ref: Option<String>,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct LiveActionReceipt {
    pub receipt_id: String,
    pub target_ref: String,
    pub status: String,
    pub rollback_ref: Option<String>,
}

pub fn execute_live_action(
    action_id: &str,
    signal: LiveActionSignal,
) -> Result<LiveActionReceipt, KernelError> {
    if action_id.trim().is_empty() {
        return Err(KernelError::MissingSignal("action-id"));
    }
    if signal.target_ref.trim().is_empty() {
        return Err(KernelError::MissingSignal("target-ref"));
    }
    if signal.evidence_id.trim().is_empty() {
        return Err(KernelError::MissingSignal("action-evidence"));
    }
    if !signal.safety_signal_present {
        return Err(KernelError::MissingSignal("live-action-safety-signal"));
    }
    if matches!(action_id, "cancel" | "adjust_interactive_reserve") && signal.approval_ref.is_none()
    {
        return Err(KernelError::UnavailableSignal("approval-required"));
    }
    Ok(LiveActionReceipt {
        receipt_id: format!("resource-action-{action_id}-{}", signal.evidence_id),
        target_ref: signal.target_ref,
        status: "accepted".to_string(),
        rollback_ref: Some(format!("rollback:{action_id}")),
    })
}

#[cfg(test)]
mod resource_cockpit_tests {
    use super::*;

    #[test]
    fn resource_cockpit_live_action_requires_safety_signal_and_approval() {
        let missing_signal = execute_live_action(
            "cancel",
            LiveActionSignal {
                target_ref: "lease-1".to_string(),
                evidence_id: "evidence-1".to_string(),
                safety_signal_present: false,
                approval_ref: Some("approval-1".to_string()),
            },
        );
        assert!(matches!(
            missing_signal,
            Err(KernelError::MissingSignal("live-action-safety-signal"))
        ));

        let missing_approval = execute_live_action(
            "cancel",
            LiveActionSignal {
                target_ref: "lease-1".to_string(),
                evidence_id: "evidence-1".to_string(),
                safety_signal_present: true,
                approval_ref: None,
            },
        );
        assert!(matches!(
            missing_approval,
            Err(KernelError::UnavailableSignal("approval-required"))
        ));
    }

    #[test]
    fn resource_cockpit_live_action_returns_rollback_receipt() {
        let receipt = execute_live_action(
            "pause",
            LiveActionSignal {
                target_ref: "lease-1".to_string(),
                evidence_id: "evidence-1".to_string(),
                safety_signal_present: true,
                approval_ref: None,
            },
        )
        .expect("safe action");

        assert_eq!(receipt.status, "accepted");
        assert_eq!(receipt.rollback_ref, Some("rollback:pause".to_string()));
    }
}
