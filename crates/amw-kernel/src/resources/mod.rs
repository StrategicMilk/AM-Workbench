use crate::error::KernelError;

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum ResourceSignal<T> {
    Known(T),
    Missing,
    Unknown,
    Stale,
    Unavailable,
}

#[derive(Debug, Clone, Copy, PartialEq)]
pub struct ResourceSnapshot {
    pub available_units: u32,
    pub capacity_units: u32,
    pub telemetry_fresh: bool,
}

#[derive(Debug, Clone, Copy, Eq, PartialEq)]
pub enum ResourceDecision {
    Approved,
    Denied,
}

pub fn evaluate_resource_request(
    signal: ResourceSignal<ResourceSnapshot>,
    requested_units: u32,
) -> Result<ResourceDecision, KernelError> {
    if requested_units == 0 {
        return Err(KernelError::UnknownSignal(
            "requested-resource-units".to_string(),
        ));
    }
    let snapshot = match signal {
        ResourceSignal::Known(snapshot) => snapshot,
        ResourceSignal::Missing => return Err(KernelError::MissingSignal("resource-signal")),
        ResourceSignal::Unknown => {
            return Err(KernelError::UnknownSignal("resource-signal".to_string()))
        }
        ResourceSignal::Stale => {
            return Err(KernelError::UnavailableSignal("stale-resource-signal"))
        }
        ResourceSignal::Unavailable => {
            return Err(KernelError::UnavailableSignal("resource-provider"))
        }
    };
    if snapshot.capacity_units == 0 {
        return Err(KernelError::UnknownSignal("resource-capacity".to_string()));
    }
    if !snapshot.telemetry_fresh {
        return Err(KernelError::MissingSignal("resource-telemetry"));
    }
    if requested_units > snapshot.available_units {
        return Ok(ResourceDecision::Denied);
    }
    Ok(ResourceDecision::Approved)
}

#[cfg(test)]
mod resources_tests {
    use super::*;

    #[test]
    fn resources_approve_only_fresh_known_capacity() {
        let decision = evaluate_resource_request(
            ResourceSignal::Known(ResourceSnapshot {
                available_units: 4,
                capacity_units: 8,
                telemetry_fresh: true,
            }),
            2,
        )
        .expect("fresh capacity");

        assert_eq!(decision, ResourceDecision::Approved);
    }

    #[test]
    fn resources_deny_capacity_exhaustion_without_failing_open() {
        let decision = evaluate_resource_request(
            ResourceSignal::Known(ResourceSnapshot {
                available_units: 1,
                capacity_units: 8,
                telemetry_fresh: true,
            }),
            2,
        )
        .expect("known exhausted capacity is a denial");

        assert_eq!(decision, ResourceDecision::Denied);
    }

    #[test]
    fn resources_fail_closed_for_missing_unknown_stale_and_missing_telemetry() {
        assert!(matches!(
            evaluate_resource_request(ResourceSignal::Missing, 1),
            Err(KernelError::MissingSignal("resource-signal"))
        ));
        assert!(matches!(
            evaluate_resource_request(ResourceSignal::Unknown, 1),
            Err(KernelError::UnknownSignal(signal)) if signal == "resource-signal"
        ));
        assert!(matches!(
            evaluate_resource_request(ResourceSignal::Stale, 1),
            Err(KernelError::UnavailableSignal("stale-resource-signal"))
        ));
        assert!(matches!(
            evaluate_resource_request(
                ResourceSignal::Known(ResourceSnapshot {
                    available_units: 1,
                    capacity_units: 0,
                    telemetry_fresh: true,
                }),
                1,
            ),
            Err(KernelError::UnknownSignal(signal)) if signal == "resource-capacity"
        ));
        assert!(matches!(
            evaluate_resource_request(
                ResourceSignal::Known(ResourceSnapshot {
                    available_units: 1,
                    capacity_units: 1,
                    telemetry_fresh: false,
                }),
                1,
            ),
            Err(KernelError::MissingSignal("resource-telemetry"))
        ));
    }
}
