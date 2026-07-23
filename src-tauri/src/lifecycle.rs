use serde::{Deserialize, Serialize};
use std::str::FromStr;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum CommandOrigin {
    Tauri,
    CompatibilityApi,
    BrowserFallback,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleDecisionAction {
    OpenUi,
    KeepBackground,
    BundleSupport,
    StopGraceful,
    Restart,
    QuitComplete,
    ForceQuit,
    RecoverCrash,
    BlockAwaitingConsent,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleHostState {
    Running,
    Background,
    Stopping,
    Restarting,
    Quit,
    Recovering,
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LifecycleCommandRequest {
    pub action: LifecycleAction,
    #[serde(default = "default_origin")]
    pub origin: CommandOrigin,
    #[serde(default)]
    pub admin_equivalent: bool,
    #[serde(default)]
    pub force: bool,
    #[serde(default)]
    pub mode: Option<String>,
}

impl LifecycleCommandRequest {
    pub fn with_origin(mut self, origin: CommandOrigin) -> Self {
        self.origin = origin;
        self
    }

    pub fn without_renderer_admin_equivalence(mut self) -> Self {
        self.admin_equivalent = false;
        self
    }
}

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct LifecycleCommandDecision {
    pub accepted: bool,
    pub action: LifecycleAction,
    pub decision_action: LifecycleDecisionAction,
    pub state_after: LifecycleHostState,
    pub denial_reason: Option<String>,
}

#[derive(Clone, Debug)]
pub struct LifecycleShellHost {
    state: LifecycleHostState,
}

impl Default for LifecycleShellHost {
    fn default() -> Self {
        Self {
            state: LifecycleHostState::Running,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum LifecycleAction {
    Open,
    CloseWindow,
    KeepInBackground,
    Stop,
    Restart,
    QuitCompletely,
    ForceQuit,
    CrashRecover,
    SupportBundle,
}

impl LifecycleAction {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Open => "open",
            Self::CloseWindow => "close_window",
            Self::KeepInBackground => "keep_in_background",
            Self::Stop => "stop",
            Self::Restart => "restart",
            Self::QuitCompletely => "quit_completely",
            Self::ForceQuit => "force_quit",
            Self::CrashRecover => "crash_recover",
            Self::SupportBundle => "support_bundle",
        }
    }

    fn admin_equivalent(self) -> bool {
        matches!(
            self,
            Self::Stop
                | Self::Restart
                | Self::QuitCompletely
                | Self::ForceQuit
                | Self::SupportBundle
        )
    }
}

impl FromStr for LifecycleAction {
    type Err = &'static str;

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "open" => Ok(Self::Open),
            "close_window" => Ok(Self::CloseWindow),
            "keep_in_background" => Ok(Self::KeepInBackground),
            "stop" => Ok(Self::Stop),
            "restart" => Ok(Self::Restart),
            "quit_completely" => Ok(Self::QuitCompletely),
            "force_quit" => Ok(Self::ForceQuit),
            "crash_recover" => Ok(Self::CrashRecover),
            "support_bundle" => Ok(Self::SupportBundle),
            _ => Err("unknown lifecycle command"),
        }
    }
}

impl LifecycleShellHost {
    pub fn execute(&mut self, request: LifecycleCommandRequest) -> LifecycleCommandDecision {
        let action = request.action;
        if let Some(reason) = self.denial_reason(&request, action) {
            return self.denied(action, reason);
        }

        let decision_action = match action {
            LifecycleAction::Open => LifecycleDecisionAction::OpenUi,
            LifecycleAction::CloseWindow | LifecycleAction::KeepInBackground => {
                self.state = LifecycleHostState::Background;
                LifecycleDecisionAction::KeepBackground
            }
            LifecycleAction::Stop => {
                self.state = LifecycleHostState::Stopping;
                LifecycleDecisionAction::StopGraceful
            }
            LifecycleAction::Restart => {
                self.state = LifecycleHostState::Restarting;
                LifecycleDecisionAction::Restart
            }
            LifecycleAction::QuitCompletely => {
                self.state = LifecycleHostState::Quit;
                LifecycleDecisionAction::QuitComplete
            }
            LifecycleAction::ForceQuit => {
                self.state = LifecycleHostState::Quit;
                LifecycleDecisionAction::ForceQuit
            }
            LifecycleAction::CrashRecover => {
                self.state = LifecycleHostState::Recovering;
                LifecycleDecisionAction::RecoverCrash
            }
            LifecycleAction::SupportBundle => LifecycleDecisionAction::BundleSupport,
        };
        LifecycleCommandDecision {
            accepted: true,
            action,
            decision_action,
            state_after: self.state.clone(),
            denial_reason: None,
        }
    }

    fn denial_reason(
        &self,
        request: &LifecycleCommandRequest,
        action: LifecycleAction,
    ) -> Option<&'static str> {
        let admin_action = action.admin_equivalent();
        match request.origin {
            CommandOrigin::Tauri | CommandOrigin::CompatibilityApi => {
                if admin_action && !request.admin_equivalent {
                    Some("admin-equivalent confirmation required")
                } else {
                    None
                }
            }
            CommandOrigin::BrowserFallback => {
                if admin_action {
                    Some("browser fallback cannot run administrative lifecycle commands")
                } else {
                    None
                }
            }
        }
    }

    fn denied(&self, action: LifecycleAction, reason: &'static str) -> LifecycleCommandDecision {
        LifecycleCommandDecision {
            accepted: false,
            action,
            decision_action: LifecycleDecisionAction::BlockAwaitingConsent,
            state_after: self.state.clone(),
            denial_reason: Some(reason.to_string()),
        }
    }
}

fn default_origin() -> CommandOrigin {
    CommandOrigin::BrowserFallback
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn unknown_command_fails_closed() {
        let result = serde_json::from_value::<LifecycleCommandRequest>(
            serde_json::json!({"action": "self_destruct", "origin": "tauri"}),
        );

        assert!(result.is_err());
    }

    #[test]
    fn security_unknown_lifecycle_command_fails_closed() {
        let result = crate::commands::workbench_lifecycle_command(serde_json::json!({
            "action": "launch_unbounded_shell",
            "admin_equivalent": true,
        }));

        assert!(result
            .expect_err("unknown command fails at command entry")
            .contains("invalid lifecycle payload"));
    }

    #[test]
    fn browser_fallback_cannot_stop_backend() {
        let mut host = LifecycleShellHost::default();
        let result = host.execute(LifecycleCommandRequest {
            action: LifecycleAction::Stop,
            origin: CommandOrigin::BrowserFallback,
            admin_equivalent: false,
            force: false,
            mode: None,
        });

        assert!(!result.accepted);
        assert_eq!(result.state_after, LifecycleHostState::Running);
    }

    #[test]
    fn close_window_keeps_background_state() {
        let mut host = LifecycleShellHost::default();
        let result = host.execute(LifecycleCommandRequest {
            action: LifecycleAction::CloseWindow,
            origin: CommandOrigin::Tauri,
            admin_equivalent: true,
            force: false,
            mode: None,
        });

        assert!(result.accepted);
        assert_eq!(
            result.decision_action,
            LifecycleDecisionAction::KeepBackground
        );
        assert_eq!(result.state_after, LifecycleHostState::Background);
    }

    #[test]
    fn support_bundle_admin_branch_is_reachable_from_tauri() {
        let mut host = LifecycleShellHost::default();
        let denied = host.execute(LifecycleCommandRequest {
            action: LifecycleAction::SupportBundle,
            origin: CommandOrigin::Tauri,
            admin_equivalent: false,
            force: false,
            mode: None,
        });
        assert!(!denied.accepted);
        assert_eq!(
            denied.denial_reason.as_deref(),
            Some("admin-equivalent confirmation required")
        );

        let accepted = host.execute(LifecycleCommandRequest {
            action: LifecycleAction::SupportBundle,
            origin: CommandOrigin::Tauri,
            admin_equivalent: true,
            force: false,
            mode: None,
        });
        assert!(accepted.accepted);
        assert_eq!(accepted.action, LifecycleAction::SupportBundle);
    }

    #[test]
    fn support_bundle_action_has_distinct_decision() {
        let mut host = LifecycleShellHost::default();
        let accepted = host.execute(LifecycleCommandRequest {
            action: LifecycleAction::SupportBundle,
            origin: CommandOrigin::Tauri,
            admin_equivalent: true,
            force: false,
            mode: None,
        });

        assert!(accepted.accepted);
        assert_eq!(
            accepted.decision_action,
            LifecycleDecisionAction::BundleSupport
        );
    }
}
