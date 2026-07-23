use std::collections::{BTreeMap, VecDeque};

use crate::extensions::{redact_secret_bearing, ExtensionPermission, SupportEnvelope};

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct McpResource {
    pub uri: String,
    pub title: String,
    pub payload: String,
    pub required_permission: ExtensionPermission,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct McpToolCall {
    pub name: String,
    pub arguments_json: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct McpToolCapability {
    pub name: String,
    pub description: String,
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct McpPromptCapability {
    pub name: String,
    pub description: String,
}

#[derive(Debug, Clone, Default)]
pub struct McpToolRegistry;

impl McpToolRegistry {
    pub fn tools_list(&self) -> Result<Vec<McpToolCapability>, SupportEnvelope> {
        Err(mcp_unimplemented())
    }

    pub fn tools_call(&self, call: McpToolCall) -> Result<String, SupportEnvelope> {
        let _ = call;
        Err(mcp_unimplemented())
    }

    pub fn prompts_list(&self) -> Result<Vec<McpPromptCapability>, SupportEnvelope> {
        Err(mcp_unimplemented())
    }

    pub fn prompts_get(&self, name: &str) -> Result<String, SupportEnvelope> {
        let _ = name;
        Err(mcp_unimplemented())
    }
}

fn mcp_unimplemented() -> SupportEnvelope {
    SupportEnvelope::new(
        "MCP_UNIMPLEMENTED",
        "MCP tools and prompts are not implemented in the Rust kernel yet",
        "return a bounded MCP error instead of invoking an unavailable capability",
    )
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct McpStreamSession {
    pub session_id: String,
    initialized: bool,
    max_events: usize,
    queue: VecDeque<String>,
}

impl McpStreamSession {
    pub fn initialized(
        session_id: impl Into<String>,
        max_events: usize,
    ) -> Result<Self, SupportEnvelope> {
        let session_id = session_id.into();
        if session_id.trim().is_empty() {
            return Err(SupportEnvelope::new(
                "MCP_SESSION_ID",
                "MCP session id is empty",
                "provide a non-empty session id",
            ));
        }
        if max_events == 0 {
            return Err(SupportEnvelope::new(
                "MCP_QUEUE",
                "event queue must be bounded",
                "set a positive queue bound",
            ));
        }
        Ok(Self {
            session_id,
            initialized: true,
            max_events,
            queue: VecDeque::new(),
        })
    }

    pub fn uninitialized(session_id: impl Into<String>) -> Self {
        Self {
            session_id: session_id.into(),
            initialized: false,
            max_events: 1,
            queue: VecDeque::new(),
        }
    }

    pub fn enqueue_event(&mut self, event: &str) -> Result<usize, SupportEnvelope> {
        if !self.initialized {
            return Err(SupportEnvelope::new(
                "MCP_SESSION",
                "MCP session is not initialized",
                "initialize the session first",
            ));
        }
        if event.len() > 4096 {
            return Err(SupportEnvelope::new(
                "MCP_EVENT_SIZE",
                "MCP event exceeds size limit",
                "send a smaller event",
            ));
        }
        if self.queue.len() >= self.max_events {
            return Err(SupportEnvelope::new(
                "MCP_BACKPRESSURE",
                "MCP event queue is full",
                "drain the stream before enqueueing another event",
            ));
        }
        self.queue.push_back(redact_secret_bearing(event));
        Ok(self.queue.len())
    }

    pub fn disconnect_cleanup(&mut self) -> usize {
        let removed = self.queue.len();
        self.queue.clear();
        self.initialized = false;
        removed
    }

    pub fn queued_events(&self) -> Vec<String> {
        self.queue.iter().cloned().collect()
    }
}

#[derive(Debug, Clone, Default)]
pub struct McpResourceRegistry {
    resources: BTreeMap<String, McpResource>,
}

impl McpResourceRegistry {
    pub fn register(&mut self, resource: McpResource) -> Result<(), SupportEnvelope> {
        if resource.uri.trim().is_empty() || !resource.uri.starts_with("resource://") {
            return Err(SupportEnvelope::new(
                "MCP_RESOURCE_URI",
                "resource uri is invalid",
                "use a resource:// uri",
            ));
        }
        if self.resources.contains_key(&resource.uri) {
            return Err(SupportEnvelope::new(
                "MCP_RESOURCE_DUPLICATE",
                "resource uri is already registered",
                "register each resource uri once",
            ));
        }
        self.resources.insert(resource.uri.clone(), resource);
        Ok(())
    }

    pub fn list(
        &self,
        session: &McpStreamSession,
    ) -> Result<Vec<(String, String)>, SupportEnvelope> {
        if !session.initialized {
            return Err(SupportEnvelope::new(
                "MCP_SESSION",
                "MCP session is not initialized",
                "initialize the session first",
            ));
        }
        Ok(self
            .resources
            .values()
            .map(|row| (row.uri.clone(), row.title.clone()))
            .collect())
    }

    pub fn read(
        &self,
        session: &McpStreamSession,
        uri: &str,
        permissions: &[ExtensionPermission],
    ) -> Result<String, SupportEnvelope> {
        if !session.initialized {
            return Err(SupportEnvelope::new(
                "MCP_SESSION",
                "MCP session is not initialized",
                "initialize the session first",
            ));
        }
        let Some(resource) = self.resources.get(uri) else {
            return Err(SupportEnvelope::new(
                "MCP_RESOURCE",
                "MCP resource was not found",
                "refresh resources/list",
            ));
        };
        if !permissions.contains(&resource.required_permission) {
            return Err(SupportEnvelope::new(
                "MCP_RESOURCE_PERMISSION",
                "MCP resource permission is missing",
                "request the declared resource permission",
            ));
        }
        Ok(redact_secret_bearing(&resource.payload))
    }
}

pub fn list_mcp_resources(
    registry: &McpResourceRegistry,
    session: &McpStreamSession,
) -> Result<Vec<(String, String)>, SupportEnvelope> {
    registry.list(session)
}

pub fn read_mcp_resource(
    registry: &McpResourceRegistry,
    session: &McpStreamSession,
    uri: &str,
    permissions: &[ExtensionPermission],
) -> Result<String, SupportEnvelope> {
    registry.read(session, uri, permissions)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uninitialized_session_cannot_list_resources() {
        let registry = McpResourceRegistry::default();
        let session = McpStreamSession::uninitialized("s1");
        assert_eq!(
            registry.list(&session).expect_err("denied").code,
            "MCP_SESSION"
        );
    }

    #[test]
    fn mcp_stream_session_rejects_empty_session_id() {
        assert_eq!(
            McpStreamSession::initialized("", 1)
                .expect_err("empty session id rejected")
                .code,
            "MCP_SESSION_ID"
        );
    }

    #[test]
    fn mcp_stream_session_rejects_whitespace_session_id() {
        assert_eq!(
            McpStreamSession::initialized("   ", 1)
                .expect_err("whitespace session id rejected")
                .code,
            "MCP_SESSION_ID"
        );
    }

    #[test]
    fn mcp_stream_session_accepts_valid_session_id() {
        let session = McpStreamSession::initialized("s1", 1).expect("valid session");
        assert_eq!(session.session_id, "s1");
    }

    #[test]
    fn resource_read_requires_resource_permission_and_redacts_payload() {
        let mut registry = McpResourceRegistry::default();
        registry
            .register(McpResource {
                uri: "resource://workspace/context".to_string(),
                title: "Context".to_string(),
                payload: "secret token abc".to_string(),
                required_permission: ExtensionPermission::Resource("workspace".to_string()),
            })
            .expect("valid resource");
        let session = McpStreamSession::initialized("s1", 2).expect("session");
        assert_eq!(
            registry
                .read(&session, "resource://workspace/context", &[])
                .expect_err("permission denied")
                .code,
            "MCP_RESOURCE_PERMISSION"
        );
        assert_eq!(
            registry
                .read(
                    &session,
                    "resource://workspace/context",
                    &[ExtensionPermission::Resource("workspace".to_string())],
                )
                .expect("authorized"),
            "[redacted]"
        );
    }

    #[test]
    fn duplicate_resource_registration_fails_closed() {
        let mut registry = McpResourceRegistry::default();
        let resource = McpResource {
            uri: "resource://workspace/context".to_string(),
            title: "Context".to_string(),
            payload: "public".to_string(),
            required_permission: ExtensionPermission::Resource("workspace".to_string()),
        };
        registry
            .register(resource.clone())
            .expect("first registration");
        assert_eq!(
            registry
                .register(resource)
                .expect_err("duplicate resource rejected")
                .code,
            "MCP_RESOURCE_DUPLICATE"
        );
    }

    #[test]
    fn queue_is_bounded_and_disconnect_cleans_up() {
        let mut session = McpStreamSession::initialized("s1", 1).expect("bounded");
        session.enqueue_event("first").expect("event");
        assert_eq!(
            session
                .enqueue_event("second")
                .expect_err("backpressure")
                .code,
            "MCP_BACKPRESSURE"
        );
        assert_eq!(session.queued_events(), vec!["first".to_string()]);
        assert_eq!(session.disconnect_cleanup(), 1);
        assert!(session.queued_events().is_empty());
    }

    #[test]
    fn mcp_tools_and_prompts_fail_closed() {
        let registry = McpToolRegistry;
        assert_eq!(
            registry.tools_list().expect_err("tools/list denied").code,
            "MCP_UNIMPLEMENTED"
        );
        assert_eq!(
            registry
                .tools_call(McpToolCall {
                    name: "shell".to_string(),
                    arguments_json: "{}".to_string(),
                })
                .expect_err("tools/call denied")
                .code,
            "MCP_UNIMPLEMENTED"
        );
        assert_eq!(
            registry
                .prompts_list()
                .expect_err("prompts/list denied")
                .code,
            "MCP_UNIMPLEMENTED"
        );
        assert_eq!(
            registry
                .prompts_get("default")
                .expect_err("prompts/get denied")
                .code,
            "MCP_UNIMPLEMENTED"
        );
    }
}
