use std::collections::{HashSet, VecDeque};
use std::fs::{self, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};

const NONCE_RETENTION_MULTIPLIER: usize = 16;
const MIN_NONCE_RETENTION: usize = 16;
const MAX_NONCE_RETENTION: usize = 4096;

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum IpcError {
    MalformedInput(&'static str),
    ReplayLedger(String),
    Replay(String),
    Disconnected,
    Backpressure { capacity: usize },
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct IpcMessage {
    pub session_id: String,
    pub nonce: String,
    pub kind: String,
    pub payload_bytes: usize,
}

#[derive(Debug, Clone)]
pub struct IpcSession {
    session_id: String,
    capacity: usize,
    in_flight: usize,
    connected: bool,
    seen_nonces: HashSet<String>,
    seen_nonce_order: VecDeque<String>,
    nonce_retention: usize,
    replay_ledger_path: Option<PathBuf>,
}

impl IpcSession {
    #[deprecated(
        note = "use IpcSession::new_with_replay_ledger so nonce replay survives process restarts"
    )]
    pub fn new(session_id: impl Into<String>, capacity: usize) -> Result<Self, IpcError> {
        Self::new_unpersisted(session_id, capacity)
    }

    fn new_unpersisted(session_id: impl Into<String>, capacity: usize) -> Result<Self, IpcError> {
        let session_id = session_id.into();
        if session_id.trim().is_empty() {
            return Err(IpcError::MalformedInput("missing session_id"));
        }
        if capacity == 0 {
            return Err(IpcError::MalformedInput("zero capacity"));
        }
        Ok(Self {
            session_id,
            capacity,
            in_flight: 0,
            connected: true,
            seen_nonces: HashSet::new(),
            seen_nonce_order: VecDeque::new(),
            nonce_retention: nonce_retention_for_capacity(capacity),
            replay_ledger_path: None,
        })
    }

    pub fn new_with_replay_ledger(
        session_id: impl Into<String>,
        capacity: usize,
        replay_ledger_path: impl Into<PathBuf>,
    ) -> Result<Self, IpcError> {
        let replay_ledger_path = replay_ledger_path.into();
        let mut session = Self::new_unpersisted(session_id, capacity)?;
        session.replay_ledger_path = Some(replay_ledger_path.clone());
        session.load_replay_ledger(&replay_ledger_path)?;
        Ok(session)
    }

    pub fn submit(&mut self, message: IpcMessage) -> Result<(), IpcError> {
        if !self.connected {
            return Err(IpcError::Disconnected);
        }
        if message.session_id != self.session_id
            || message.kind.trim().is_empty()
            || message.nonce.trim().is_empty()
        {
            return Err(IpcError::MalformedInput(
                "session, kind, and nonce are required",
            ));
        }
        if message.payload_bytes > 1024 * 1024 {
            return Err(IpcError::MalformedInput(
                "payload exceeds maximum IPC frame size",
            ));
        }
        if message.session_id.contains('\t')
            || message.session_id.contains('\n')
            || message.nonce.contains('\t')
            || message.nonce.contains('\n')
        {
            return Err(IpcError::MalformedInput(
                "session and nonce must be single-line tokens",
            ));
        }
        if self.has_seen_nonce(&message.nonce)? {
            return Err(IpcError::Replay(message.nonce));
        }
        if self.in_flight >= self.capacity {
            return Err(IpcError::Backpressure {
                capacity: self.capacity,
            });
        }
        self.remember_nonce(message.nonce)?;
        self.in_flight += 1;
        Ok(())
    }

    fn remember_nonce(&mut self, nonce: String) -> Result<(), IpcError> {
        if let Some(path) = &self.replay_ledger_path {
            append_replay_ledger(path, &self.session_id, &nonce)?;
        }
        self.remember_nonce_in_memory(nonce);
        Ok(())
    }

    fn has_seen_nonce(&self, nonce: &str) -> Result<bool, IpcError> {
        if self.seen_nonces.contains(nonce) {
            return Ok(true);
        }
        if let Some(path) = &self.replay_ledger_path {
            return replay_ledger_contains(path, &self.session_id, nonce);
        }
        Ok(false)
    }

    fn remember_nonce_in_memory(&mut self, nonce: String) {
        if self.seen_nonces.insert(nonce.clone()) {
            self.seen_nonce_order.push_back(nonce);
        }
        while self.seen_nonces.len() > self.nonce_retention {
            let Some(oldest) = self.seen_nonce_order.pop_front() else {
                break;
            };
            self.seen_nonces.remove(&oldest);
        }
    }

    pub fn ack(&mut self) {
        self.in_flight = self.in_flight.saturating_sub(1);
    }

    pub fn disconnect(&mut self) {
        self.connected = false;
    }

    pub fn retained_nonce_count(&self) -> usize {
        self.seen_nonces.len()
    }

    fn load_replay_ledger(&mut self, path: &Path) -> Result<(), IpcError> {
        if !path.exists() {
            return Ok(());
        }
        let file = fs::File::open(path)
            .map_err(|err| IpcError::ReplayLedger(format!("unreadable replay ledger: {err}")))?;
        for line in BufReader::new(file).lines() {
            let line = line.map_err(|err| {
                IpcError::ReplayLedger(format!("unreadable replay ledger: {err}"))
            })?;
            let Some((session_id, nonce)) = line.split_once('\t') else {
                return Err(IpcError::ReplayLedger(
                    "corrupt replay ledger row".to_string(),
                ));
            };
            if session_id == self.session_id {
                self.remember_nonce_in_memory(nonce.to_string());
            }
        }
        Ok(())
    }
}

fn nonce_retention_for_capacity(capacity: usize) -> usize {
    capacity
        .saturating_mul(NONCE_RETENTION_MULTIPLIER)
        .max(MIN_NONCE_RETENTION)
        .min(MAX_NONCE_RETENTION)
}

fn replay_ledger_contains(path: &Path, session_id: &str, nonce: &str) -> Result<bool, IpcError> {
    if !path.exists() {
        return Ok(false);
    }
    let file = fs::File::open(path)
        .map_err(|err| IpcError::ReplayLedger(format!("unreadable replay ledger: {err}")))?;
    for line in BufReader::new(file).lines() {
        let line =
            line.map_err(|err| IpcError::ReplayLedger(format!("unreadable replay ledger: {err}")))?;
        let Some((ledger_session, ledger_nonce)) = line.split_once('\t') else {
            return Err(IpcError::ReplayLedger(
                "corrupt replay ledger row".to_string(),
            ));
        };
        if ledger_session == session_id && ledger_nonce == nonce {
            return Ok(true);
        }
    }
    Ok(false)
}

fn append_replay_ledger(path: &Path, session_id: &str, nonce: &str) -> Result<(), IpcError> {
    if session_id.contains('\t')
        || session_id.contains('\n')
        || nonce.contains('\t')
        || nonce.contains('\n')
    {
        return Err(IpcError::MalformedInput(
            "session and nonce must be single-line tokens",
        ));
    }
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|err| IpcError::ReplayLedger(format!("cannot create replay ledger: {err}")))?;
    }
    let mut file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|err| IpcError::ReplayLedger(format!("cannot open replay ledger: {err}")))?;
    writeln!(file, "{session_id}\t{nonce}")
        .map_err(|err| IpcError::ReplayLedger(format!("cannot write replay ledger: {err}")))?;
    file.sync_all()
        .map_err(|err| IpcError::ReplayLedger(format!("cannot sync replay ledger: {err}")))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn message(nonce: &str) -> IpcMessage {
        IpcMessage {
            session_id: "session-a".to_string(),
            nonce: nonce.to_string(),
            kind: "sandbox.execute".to_string(),
            payload_bytes: 16,
        }
    }

    #[test]
    fn ipc_malformed_input_fails_closed() {
        let mut session = IpcSession::new_unpersisted("session-a", 2).expect("session builds");
        let malformed = IpcMessage {
            kind: "".to_string(),
            ..message("nonce-a")
        };

        assert!(matches!(
            session.submit(malformed),
            Err(IpcError::MalformedInput(_))
        ));
    }

    #[test]
    fn ipc_replay_session_nonce_fails_closed() {
        let mut session = IpcSession::new_unpersisted("session-a", 2).expect("session builds");
        session
            .submit(message("nonce-a"))
            .expect("first submit succeeds");
        session.ack();

        assert_eq!(
            session.submit(message("nonce-a")),
            Err(IpcError::Replay("nonce-a".to_string()))
        );
    }

    #[test]
    #[expect(deprecated, reason = "acceptance test covers the legacy constructor")]
    fn ipc_session_new_emits_deprecation_via_expected_path() {
        let mut session = IpcSession::new("session-a", 2).expect("session builds");
        session
            .submit(message("nonce-deprecated"))
            .expect("first submit succeeds");
        session.ack();

        assert_eq!(
            session.submit(message("nonce-deprecated")),
            Err(IpcError::Replay("nonce-deprecated".to_string()))
        );
    }

    #[test]
    fn ipc_disconnect_fails_closed() {
        let mut session = IpcSession::new_unpersisted("session-a", 2).expect("session builds");
        session.disconnect();

        assert_eq!(
            session.submit(message("nonce-a")),
            Err(IpcError::Disconnected)
        );
    }

    #[test]
    fn ipc_backpressure_limits_concurrent_callers() {
        let mut session = IpcSession::new_unpersisted("session-a", 1).expect("session builds");
        session
            .submit(message("nonce-a"))
            .expect("first submit succeeds");

        assert_eq!(
            session.submit(message("nonce-b")),
            Err(IpcError::Backpressure { capacity: 1 })
        );
        session.ack();
        session
            .submit(message("nonce-c"))
            .expect("submit after ack succeeds");
    }

    #[test]
    fn ipc_zero_capacity_fails_closed_instead_of_promoting_to_one() {
        assert!(matches!(
            IpcSession::new_unpersisted("session-a", 0),
            Err(IpcError::MalformedInput("zero capacity"))
        ));
    }

    fn temp_path(name: &str) -> PathBuf {
        let stamp = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .expect("clock available")
            .as_nanos();
        std::env::temp_dir().join(format!("amw-ipc-{name}-{stamp}.ledger"))
    }

    #[test]
    fn ipc_kernel_contracts_unpersisted_nonce_cache_is_bounded() {
        let mut session = IpcSession::new_unpersisted("session-a", 1).expect("session builds");
        for index in 0..40 {
            session
                .submit(message(&format!("nonce-{index}")))
                .expect("submit succeeds");
            session.ack();
        }

        assert_eq!(
            session.retained_nonce_count(),
            nonce_retention_for_capacity(1)
        );
        assert_eq!(
            session.submit(message("nonce-39")),
            Err(IpcError::Replay("nonce-39".to_string()))
        );
    }

    #[test]
    fn ipc_kernel_contracts_replay_ledger_survives_restart_and_fails_closed() {
        let path = temp_path("replay");
        let mut session =
            IpcSession::new_with_replay_ledger("session-a", 4, &path).expect("session builds");
        session.submit(message("nonce-a")).expect("first submit");
        session.ack();
        drop(session);

        let mut restarted =
            IpcSession::new_with_replay_ledger("session-a", 4, &path).expect("restarted session");
        assert_eq!(
            restarted.submit(message("nonce-a")),
            Err(IpcError::Replay("nonce-a".to_string()))
        );
        let _ = fs::remove_file(path);

        let corrupt = temp_path("corrupt");
        fs::write(&corrupt, "not-a-ledger-row\n").expect("seed corrupt ledger");
        assert!(matches!(
            IpcSession::new_with_replay_ledger("session-a", 4, &corrupt),
            Err(IpcError::ReplayLedger(message)) if message.contains("corrupt replay ledger")
        ));
        let _ = fs::remove_file(corrupt);
    }

    #[test]
    fn ipc_session_with_ledger_persists_and_reloads_nonces() {
        let path = temp_path("acceptance-replay");
        let mut session =
            IpcSession::new_with_replay_ledger("session-a", 2, &path).expect("session builds");
        session
            .submit(message("nonce-persisted"))
            .expect("first submit");
        session.ack();
        drop(session);

        let mut restarted =
            IpcSession::new_with_replay_ledger("session-a", 2, &path).expect("restarted session");
        assert_eq!(
            restarted.submit(message("nonce-persisted")),
            Err(IpcError::Replay("nonce-persisted".to_string()))
        );

        let _ = fs::remove_file(path);
    }

    #[test]
    fn ipc_replay_ledger_retains_old_nonces_after_restart_without_window_eviction() {
        let path = temp_path("long-replay");
        let mut session =
            IpcSession::new_with_replay_ledger("session-a", 2, &path).expect("session builds");
        for index in 0..25 {
            session
                .submit(message(&format!("nonce-{index}")))
                .expect("submit succeeds");
            session.ack();
        }
        drop(session);

        let mut restarted =
            IpcSession::new_with_replay_ledger("session-a", 2, &path).expect("restarted session");

        assert!(restarted.retained_nonce_count() <= nonce_retention_for_capacity(2));
        assert_eq!(
            restarted.submit(message("nonce-0")),
            Err(IpcError::Replay("nonce-0".to_string()))
        );
        let _ = fs::remove_file(path);
    }

    #[test]
    fn ipc_replay_ledger_checks_evicted_nonces_without_unbounded_memory() {
        let path = temp_path("ledger-bounded");
        let mut session =
            IpcSession::new_with_replay_ledger("session-a", 1, &path).expect("session builds");
        for index in 0..40 {
            session
                .submit(message(&format!("nonce-{index}")))
                .expect("submit succeeds");
            session.ack();
        }

        assert!(session.retained_nonce_count() <= nonce_retention_for_capacity(1));
        assert_eq!(
            session.submit(message("nonce-0")),
            Err(IpcError::Replay("nonce-0".to_string()))
        );
        let _ = fs::remove_file(path);
    }
}
