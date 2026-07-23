//! Fail-closed SQLite reservation and immutable receipt ledger.

#[cfg(not(target_os = "linux"))]
use std::fs;
use std::{
    path::{Path, PathBuf},
    sync::{Mutex, MutexGuard},
    time::Duration,
};

use rusqlite::{params, Connection, ErrorCode, OpenFlags, OptionalExtension, TransactionBehavior};
use thiserror::Error;

use super::{
    attempt_key, canonical_receipt_bytes, AttemptIdentity, CanonicalError, Digest32,
    EvalReceiptClaims, ReceiptSigner, SignedEvalReceipt, SignerError, SignerIdentity, SignerTrust,
};

const LEDGER_SCHEMA_VERSION: i64 = 1;
const REQUIRED_INDEXES: [(&str, &[&str]); 3] = [
    ("uq_eval_receipt_request_id", &["request_id"]),
    (
        "uq_eval_receipt_attempt_identity",
        &[
            "installation_id",
            "run_id",
            "suite_id",
            "case_id",
            "ordinal",
        ],
    ),
    ("uq_eval_receipt_attempt_key", &["attempt_key"]),
];
const REQUIRED_TRIGGERS: [&str; 7] = [
    "eval_receipt_no_delete",
    "eval_receipt_committed_immutable",
    "eval_receipt_reservation_identity_immutable",
    "eval_receipt_authority_no_delete",
    "eval_receipt_authority_immutable",
    "eval_receipt_key_history_no_delete",
    "eval_receipt_key_history_immutable",
];

const CREATE_SCHEMA: &str = r#"
BEGIN IMMEDIATE;
CREATE TABLE eval_receipt_attempts (
    id INTEGER PRIMARY KEY,
    request_id TEXT NOT NULL CHECK(length(request_id) BETWEEN 1 AND 128),
    installation_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    suite_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 4294967295),
    attempt_key BLOB NOT NULL CHECK(length(attempt_key) = 32),
    state TEXT NOT NULL CHECK(state IN ('reserved', 'committed')),
    receipt_id BLOB CHECK(receipt_id IS NULL OR length(receipt_id) = 32),
    canonical_receipt BLOB,
    signed_receipt_json TEXT,
    CHECK (
        (state = 'reserved' AND receipt_id IS NULL AND canonical_receipt IS NULL AND signed_receipt_json IS NULL)
        OR
        (state = 'committed' AND receipt_id IS NOT NULL AND canonical_receipt IS NOT NULL AND signed_receipt_json IS NOT NULL)
    )
);
CREATE UNIQUE INDEX uq_eval_receipt_request_id
    ON eval_receipt_attempts(request_id);
CREATE UNIQUE INDEX uq_eval_receipt_attempt_identity
    ON eval_receipt_attempts(installation_id, run_id, suite_id, case_id, ordinal);
CREATE UNIQUE INDEX uq_eval_receipt_attempt_key
    ON eval_receipt_attempts(attempt_key);
CREATE TABLE eval_receipt_authority (
    singleton_id INTEGER PRIMARY KEY CHECK(singleton_id = 1),
    installation_id TEXT NOT NULL CHECK(length(installation_id) BETWEEN 1 AND 128),
    authority_pin_sha256 BLOB NOT NULL CHECK(length(authority_pin_sha256) = 32)
);
CREATE TABLE eval_receipt_key_history (
    key_epoch INTEGER PRIMARY KEY CHECK(key_epoch >= 0),
    key_id BLOB NOT NULL UNIQUE CHECK(length(key_id) = 32),
    anchor_sha256 BLOB NOT NULL UNIQUE CHECK(length(anchor_sha256) = 32),
    predecessor_key_epoch INTEGER,
    predecessor_key_id BLOB,
    predecessor_anchor_sha256 BLOB,
    CHECK (
        (predecessor_key_epoch IS NULL AND predecessor_key_id IS NULL
            AND predecessor_anchor_sha256 IS NULL)
        OR
        (predecessor_key_epoch >= 0 AND length(predecessor_key_id) = 32
            AND length(predecessor_anchor_sha256) = 32)
    )
);
CREATE TRIGGER eval_receipt_no_delete
BEFORE DELETE ON eval_receipt_attempts
BEGIN
    SELECT RAISE(ABORT, 'evaluation attempt tombstones are immutable');
END;
CREATE TRIGGER eval_receipt_committed_immutable
BEFORE UPDATE ON eval_receipt_attempts
WHEN OLD.state = 'committed'
BEGIN
    SELECT RAISE(ABORT, 'committed evaluation receipts are immutable');
END;
CREATE TRIGGER eval_receipt_reservation_identity_immutable
BEFORE UPDATE ON eval_receipt_attempts
WHEN OLD.request_id != NEW.request_id
    OR OLD.installation_id != NEW.installation_id
    OR OLD.run_id != NEW.run_id
    OR OLD.suite_id != NEW.suite_id
    OR OLD.case_id != NEW.case_id
    OR OLD.ordinal != NEW.ordinal
    OR OLD.attempt_key != NEW.attempt_key
BEGIN
    SELECT RAISE(ABORT, 'evaluation reservation identity is immutable');
END;
CREATE TRIGGER eval_receipt_authority_no_delete
BEFORE DELETE ON eval_receipt_authority
BEGIN
    SELECT RAISE(ABORT, 'evaluation ledger authority is immutable');
END;
CREATE TRIGGER eval_receipt_authority_immutable
BEFORE UPDATE ON eval_receipt_authority
BEGIN
    SELECT RAISE(ABORT, 'evaluation ledger authority is immutable');
END;
CREATE TRIGGER eval_receipt_key_history_no_delete
BEFORE DELETE ON eval_receipt_key_history
BEGIN
    SELECT RAISE(ABORT, 'evaluation ledger key history is append-only');
END;
CREATE TRIGGER eval_receipt_key_history_immutable
BEFORE UPDATE ON eval_receipt_key_history
BEGIN
    SELECT RAISE(ABORT, 'evaluation ledger key history is append-only');
END;
PRAGMA user_version = 1;
COMMIT;
"#;

/// Durable proof that an attempt and request identifier have been consumed.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ReceiptReservation {
    pub request_id: String,
    pub identity: AttemptIdentity,
    pub attempt_key: Digest32,
}

/// Exact prior key-anchor tuple authenticated by a rotated trust anchor.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct KeyRotationPredecessor {
    pub key_epoch: u64,
    pub key_id: Digest32,
    pub anchor_sha256: Digest32,
}

/// SQLite-backed durable reservation and receipt store.
pub struct ReceiptLedger {
    connection: Mutex<Connection>,
    path: PathBuf,
    protected_path: bool,
    protected_service_identity: Option<String>,
    #[cfg(target_os = "linux")]
    linux_path_binding: Option<linux_path_security::ProtectedLedgerPath>,
    #[cfg(windows)]
    windows_path_binding: Option<windows_path_security::ProtectedLedgerPath>,
}

impl ReceiptLedger {
    /// Opens a pre-provisioned protected WAL/FULL production ledger.
    ///
    /// The absolute ledger path and its parent must already exist, be owned by
    /// the engine service identity, deny mutation by other identities, and
    /// contain no symlink or reparse traversal.
    pub fn open(
        path: impl AsRef<Path>,
        anchored_service_identity: &str,
    ) -> Result<Self, LedgerError> {
        let path = path.as_ref();
        #[cfg(target_os = "linux")]
        {
            let binding = linux_path_security::ProtectedLedgerPath::open(path)?;
            let mut ledger = Self::open_internal_with_options(
                binding.sqlite_path(),
                Some(anchored_service_identity),
                true,
            )?;
            binding.validate_current_identity(path)?;
            ledger.path = path.to_path_buf();
            ledger.linux_path_binding = Some(binding);
            Ok(ledger)
        }
        #[cfg(windows)]
        {
            validate_protected_ledger_path(path, anchored_service_identity)?;
            let binding =
                windows_path_security::ProtectedLedgerPath::open(path, anchored_service_identity)?;
            let mut ledger = Self::open_internal(path, Some(anchored_service_identity))?;
            binding.validate_current_identity(path, anchored_service_identity)?;
            ledger.windows_path_binding = Some(binding);
            Ok(ledger)
        }
        #[cfg(all(not(target_os = "linux"), not(windows)))]
        {
            validate_protected_ledger_path(path, anchored_service_identity)?;
            let ledger = Self::open_internal(path, Some(anchored_service_identity))?;
            validate_protected_ledger_path(path, anchored_service_identity)?;
            Ok(ledger)
        }
    }

    /// Opens a temporary ledger without production ownership checks.
    ///
    /// This constructor is compiled only for tests and debug builds. It still
    /// uses SQLite's no-follow flag for the final path.
    #[cfg(any(test, debug_assertions))]
    pub fn open_for_test(path: impl AsRef<Path>) -> Result<Self, LedgerError> {
        Self::open_internal(path.as_ref(), None)
    }

    fn open_internal(
        path: &Path,
        protected_service_identity: Option<&str>,
    ) -> Result<Self, LedgerError> {
        Self::open_internal_with_options(path, protected_service_identity, false)
    }

    fn open_internal_with_options(
        path: &Path,
        protected_service_identity: Option<&str>,
        retained_linux_alias: bool,
    ) -> Result<Self, LedgerError> {
        let protected_path = protected_service_identity.is_some();
        let mut flags = OpenFlags::SQLITE_OPEN_READ_WRITE;
        if !retained_linux_alias {
            flags |= OpenFlags::SQLITE_OPEN_NOFOLLOW;
        }
        if !protected_path {
            flags |= OpenFlags::SQLITE_OPEN_CREATE;
        }
        let connection = Connection::open_with_flags(path, flags)?;
        connection.busy_timeout(Duration::from_secs(5))?;
        configure_durability(&connection)?;
        initialize_or_validate_schema(&connection)?;
        validate_ledger(&connection)?;
        Ok(Self {
            connection: Mutex::new(connection),
            path: path.to_path_buf(),
            protected_path,
            protected_service_identity: protected_service_identity.map(str::to_owned),
            #[cfg(target_os = "linux")]
            linux_path_binding: None,
            #[cfg(windows)]
            windows_path_binding: None,
        })
    }

    /// Permanently consumes request and attempt uniqueness before generation.
    pub fn reserve_attempt(
        &self,
        request_id: &str,
        identity: &AttemptIdentity,
    ) -> Result<ReceiptReservation, LedgerError> {
        validate_request_id(request_id)?;
        identity.validate()?;
        let derived_attempt_key = attempt_key(identity)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let result = transaction.execute(
            "INSERT INTO eval_receipt_attempts (
                request_id, installation_id, run_id, suite_id, case_id, ordinal,
                attempt_key, state
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'reserved')",
            params![
                request_id,
                identity.installation_id,
                identity.run_id,
                identity.suite_id,
                identity.case_id,
                i64::from(identity.ordinal),
                derived_attempt_key.as_bytes().as_slice(),
            ],
        );
        match result {
            Ok(1) => transaction.commit()?,
            Ok(_) => return Err(LedgerError::MalformedLedger("reservation insert count")),
            Err(error) if is_constraint_violation(&error) => {
                return Err(LedgerError::ReservationConflict)
            }
            Err(error) => return Err(error.into()),
        }
        Ok(ReceiptReservation {
            request_id: request_id.to_owned(),
            identity: identity.clone(),
            attempt_key: derived_attempt_key,
        })
    }

    /// Atomically binds this ledger to one stable installation and authority pin.
    pub fn bind_authority(
        &self,
        installation_id: &str,
        authority_pin_sha256: Digest32,
    ) -> Result<(), LedgerError> {
        validate_authority_installation_id(installation_id)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing = transaction
            .query_row(
                "SELECT installation_id, authority_pin_sha256
                 FROM eval_receipt_authority WHERE singleton_id = 1",
                [],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?)),
            )
            .optional()?;
        match existing {
            None => {
                transaction.execute(
                    "INSERT INTO eval_receipt_authority (
                        singleton_id, installation_id, authority_pin_sha256
                     ) VALUES (1, ?1, ?2)",
                    params![installation_id, authority_pin_sha256.as_bytes().as_slice(),],
                )?;
            }
            Some((stored_installation, stored_authority)) => {
                if stored_installation != installation_id
                    || digest_from_blob(stored_authority, "authority pin")? != authority_pin_sha256
                {
                    return Err(LedgerError::AuthorityBindingMismatch);
                }
            }
        }
        transaction.commit()?;
        Ok(())
    }

    /// Appends one authenticated installation-key anchor to the rotation history.
    ///
    /// Re-registering the exact latest tuple is idempotent. A matching older
    /// tuple remains available for receipt verification but cannot bootstrap a
    /// live signing authority.
    pub fn register_key_anchor(
        &self,
        key_epoch: u64,
        key_id: Digest32,
        anchor_sha256: Digest32,
        predecessor: Option<KeyRotationPredecessor>,
    ) -> Result<(), LedgerError> {
        let key_epoch = i64::try_from(key_epoch).map_err(|_| LedgerError::KeyEpochOutOfRange)?;
        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let authority_exists: bool = transaction.query_row(
            "SELECT EXISTS(SELECT 1 FROM eval_receipt_authority WHERE singleton_id = 1)",
            [],
            |row| row.get(0),
        )?;
        if !authority_exists {
            return Err(LedgerError::AuthorityUnbound);
        }
        let latest = transaction
            .query_row(
                "SELECT key_epoch, key_id, anchor_sha256 FROM eval_receipt_key_history
                 ORDER BY key_epoch DESC LIMIT 1",
                [],
                |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, Vec<u8>>(1)?,
                        row.get::<_, Vec<u8>>(2)?,
                    ))
                },
            )
            .optional()?;
        let existing = transaction
            .query_row(
                "SELECT key_id, anchor_sha256, predecessor_key_epoch,
                        predecessor_key_id, predecessor_anchor_sha256
                 FROM eval_receipt_key_history WHERE key_epoch = ?1",
                [key_epoch],
                |row| {
                    Ok((
                        row.get::<_, Vec<u8>>(0)?,
                        row.get::<_, Vec<u8>>(1)?,
                        row.get::<_, Option<i64>>(2)?,
                        row.get::<_, Option<Vec<u8>>>(3)?,
                        row.get::<_, Option<Vec<u8>>>(4)?,
                    ))
                },
            )
            .optional()?;
        if let Some((
            stored_key,
            stored_anchor,
            predecessor_epoch,
            predecessor_key,
            predecessor_anchor,
        )) = existing
        {
            let stored_predecessor =
                decode_predecessor(predecessor_epoch, predecessor_key, predecessor_anchor)?;
            if digest_from_blob(stored_key, "key history id")? != key_id
                || digest_from_blob(stored_anchor, "key history anchor")? != anchor_sha256
                || stored_predecessor != predecessor
            {
                return Err(LedgerError::AuthorityBindingMismatch);
            }
            let Some((latest_epoch, latest_key, latest_anchor)) = latest else {
                return Err(LedgerError::MalformedLedger("missing latest key history"));
            };
            if latest_epoch != key_epoch
                || digest_from_blob(latest_key, "latest key id")? != key_id
                || digest_from_blob(latest_anchor, "latest anchor")? != anchor_sha256
            {
                return Err(LedgerError::AuthorityBindingMismatch);
            }
            transaction.commit()?;
            return Ok(());
        }
        match latest {
            None if predecessor.is_some() => return Err(LedgerError::RotationPredecessorMismatch),
            Some((latest_epoch, latest_key, latest_anchor)) => {
                let latest_key = digest_from_blob(latest_key, "latest key id")?;
                let expected = KeyRotationPredecessor {
                    key_epoch: u64::try_from(latest_epoch)
                        .map_err(|_| LedgerError::MalformedLedger("latest key epoch"))?,
                    key_id: latest_key,
                    anchor_sha256: digest_from_blob(latest_anchor, "latest anchor")?,
                };
                if key_epoch <= latest_epoch || predecessor != Some(expected) {
                    return Err(LedgerError::RotationPredecessorMismatch);
                }
            }
            _ => {}
        }
        let reused_identity: bool = transaction.query_row(
            "SELECT EXISTS(
                SELECT 1 FROM eval_receipt_key_history
                WHERE key_id = ?1 OR anchor_sha256 = ?2
             )",
            params![
                key_id.as_bytes().as_slice(),
                anchor_sha256.as_bytes().as_slice(),
            ],
            |row| row.get(0),
        )?;
        if reused_identity {
            return Err(LedgerError::AuthorityBindingMismatch);
        }
        let predecessor_epoch = predecessor
            .map(|value| i64::try_from(value.key_epoch))
            .transpose()
            .map_err(|_| LedgerError::KeyEpochOutOfRange)?;
        let predecessor_key = predecessor.map(|value| *value.key_id.as_bytes());
        let predecessor_anchor = predecessor.map(|value| *value.anchor_sha256.as_bytes());
        transaction
            .execute(
                "INSERT INTO eval_receipt_key_history (
                    key_epoch, key_id, anchor_sha256, predecessor_key_epoch,
                    predecessor_key_id, predecessor_anchor_sha256
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                params![
                    key_epoch,
                    key_id.as_bytes().as_slice(),
                    anchor_sha256.as_bytes().as_slice(),
                    predecessor_epoch,
                    predecessor_key.as_ref().map(<[u8; 32]>::as_slice),
                    predecessor_anchor.as_ref().map(<[u8; 32]>::as_slice),
                ],
            )
            .map_err(|error| {
                if is_constraint_violation(&error) {
                    LedgerError::AuthorityBindingMismatch
                } else {
                    error.into()
                }
            })?;
        transaction.commit()?;
        Ok(())
    }

    /// Signs first, then atomically commits the receipt and reservation state.
    pub fn commit_terminal_receipt(
        &self,
        reservation: &ReceiptReservation,
        claims: &EvalReceiptClaims,
        signer: &dyn ReceiptSigner,
    ) -> Result<SignedEvalReceipt, LedgerError> {
        validate_reservation_claim_bindings(reservation, claims)?;
        let canonical = canonical_receipt_bytes(claims)?;
        let raw_signature = signer.sign_canonical(&canonical)?;
        let receipt =
            SignedEvalReceipt::from_signature(claims.clone(), signer.identity(), raw_signature)?;
        let receipt_json = serde_json::to_string(&receipt)?;

        let mut connection = self.lock_connection()?;
        let transaction = connection.transaction_with_behavior(TransactionBehavior::Immediate)?;
        validate_committing_authority(&transaction, claims, signer.identity())?;
        let updated = transaction.execute(
            "UPDATE eval_receipt_attempts
             SET state = 'committed', receipt_id = ?1, canonical_receipt = ?2,
                 signed_receipt_json = ?3
             WHERE request_id = ?4 AND installation_id = ?5 AND run_id = ?6
                 AND suite_id = ?7 AND case_id = ?8 AND ordinal = ?9
                 AND attempt_key = ?10 AND state = 'reserved'",
            params![
                receipt.receipt_id.as_bytes().as_slice(),
                canonical,
                receipt_json,
                reservation.request_id,
                reservation.identity.installation_id,
                reservation.identity.run_id,
                reservation.identity.suite_id,
                reservation.identity.case_id,
                i64::from(reservation.identity.ordinal),
                reservation.attempt_key.as_bytes().as_slice(),
            ],
        )?;
        if updated != 1 {
            return Err(LedgerError::ReservationUnavailable);
        }
        transaction.commit()?;
        Ok(receipt)
    }

    /// Loads and fully verifies a committed receipt for one request.
    pub fn receipt_for_request(
        &self,
        request_id: &str,
    ) -> Result<Option<SignedEvalReceipt>, LedgerError> {
        validate_request_id(request_id)?;
        let connection = self.lock_connection()?;
        let row = connection
            .query_row(
                "SELECT state, signed_receipt_json FROM eval_receipt_attempts WHERE request_id = ?1",
                [request_id],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, Option<String>>(1)?)),
            )
            .optional()?;
        match row {
            None | Some((_, None)) => Ok(None),
            Some((state, Some(receipt_json))) if state == "committed" => {
                let receipt: SignedEvalReceipt = serde_json::from_str(&receipt_json)?;
                receipt.verify()?;
                let authority = load_authority_state(&connection)?;
                validate_committed_receipt_authority(authority.as_ref(), &receipt)?;
                Ok(Some(receipt))
            }
            Some(_) => Err(LedgerError::MalformedLedger("receipt state")),
        }
    }

    /// Re-runs schema, durability, integrity, and every-row receipt validation.
    pub fn readiness_check(&self) -> Result<(), LedgerError> {
        if self.protected_path {
            let service_identity = self.protected_service_identity.as_deref().ok_or_else(|| {
                LedgerError::UnsafeLedgerPath(
                    "protected ledger has no anchored service identity".to_owned(),
                )
            })?;
            #[cfg(target_os = "linux")]
            {
                let _ = service_identity;
                self.linux_path_binding
                    .as_ref()
                    .ok_or_else(|| {
                        LedgerError::UnsafeLedgerPath(
                            "protected Linux ledger has no retained path binding".to_owned(),
                        )
                    })?
                    .validate_current_identity(&self.path)?;
            }
            #[cfg(windows)]
            self.windows_path_binding
                .as_ref()
                .ok_or_else(|| {
                    LedgerError::UnsafeLedgerPath(
                        "protected Windows ledger has no retained path binding".to_owned(),
                    )
                })?
                .validate_current_identity(&self.path, service_identity)?;
            #[cfg(all(not(target_os = "linux"), not(windows)))]
            validate_protected_ledger_path(&self.path, service_identity)?;
        }
        let connection = self.lock_connection()?;
        configure_durability(&connection)?;
        validate_ledger(&connection)
    }

    fn lock_connection(&self) -> Result<MutexGuard<'_, Connection>, LedgerError> {
        self.connection
            .lock()
            .map_err(|_| LedgerError::ConnectionPoisoned)
    }
}

/// Fail-closed ledger errors.
#[derive(Debug, Error)]
pub enum LedgerError {
    #[error(transparent)]
    Sqlite(#[from] rusqlite::Error),
    #[error(transparent)]
    Canonical(#[from] CanonicalError),
    #[error(transparent)]
    Signer(#[from] SignerError),
    #[error(transparent)]
    Serialization(#[from] serde_json::Error),
    #[error("ledger connection mutex is poisoned")]
    ConnectionPoisoned,
    #[error("protected ledger path is unsafe: {0}")]
    UnsafeLedgerPath(String),
    #[error("protected ledger path inspection failed: {0}")]
    PathIo(#[from] std::io::Error),
    #[error("ledger must use SQLite WAL journal mode")]
    JournalModeNotWal,
    #[error("ledger must use SQLite synchronous=FULL")]
    SynchronousNotFull,
    #[error("ledger schema version {0} is unsupported")]
    UnsupportedSchemaVersion(i64),
    #[error("ledger integrity check failed: {0}")]
    IntegrityCheckFailed(String),
    #[error("ledger schema or row is malformed: {0}")]
    MalformedLedger(&'static str),
    #[error("request_id must be a non-empty bounded ASCII identifier")]
    InvalidRequestId,
    #[error("request_id or evaluation attempt was already consumed")]
    ReservationConflict,
    #[error("reservation is missing, already committed, or does not match")]
    ReservationUnavailable,
    #[error("terminal claims do not match the durable reservation")]
    ReservationBindingMismatch,
    #[error("ledger authority binding does not match installation, anchor, or authority pin")]
    AuthorityBindingMismatch,
    #[error("production receipt signer cannot commit through an unbound ledger")]
    AuthorityUnbound,
    #[error("authority installation_id must be a non-empty bounded ASCII identifier")]
    InvalidAuthorityBinding,
    #[error("receipt key epoch exceeds SQLite's signed integer range")]
    KeyEpochOutOfRange,
    #[error("receipt key rotation predecessor does not match the latest ledger key")]
    RotationPredecessorMismatch,
}

#[cfg(not(target_os = "linux"))]
fn validate_protected_ledger_path(
    path: &Path,
    anchored_service_identity: &str,
) -> Result<(), LedgerError> {
    if !path.is_absolute() {
        return Err(LedgerError::UnsafeLedgerPath(
            "path must be absolute".to_owned(),
        ));
    }
    let parent = path.parent().ok_or_else(|| {
        LedgerError::UnsafeLedgerPath("path must have a protected parent".to_owned())
    })?;
    let parent_metadata = fs::symlink_metadata(parent)?;
    if !parent_metadata.is_dir() {
        return Err(LedgerError::UnsafeLedgerPath(
            "parent is not a directory".to_owned(),
        ));
    }
    let file_metadata = fs::symlink_metadata(path)?;
    if !file_metadata.is_file() {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger is not a regular file".to_owned(),
        ));
    }
    for component in path.ancestors() {
        let metadata = fs::symlink_metadata(component)?;
        if metadata.file_type().is_symlink() || is_reparse_point(&metadata) {
            return Err(LedgerError::UnsafeLedgerPath(
                "symlink or reparse traversal is forbidden".to_owned(),
            ));
        }
    }
    validate_service_owned_objects(
        parent,
        &parent_metadata,
        path,
        &file_metadata,
        anchored_service_identity,
    )
}

#[cfg(windows)]
fn is_reparse_point(metadata: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt as _;

    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
    metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(all(not(windows), not(target_os = "linux")))]
const fn is_reparse_point(_metadata: &fs::Metadata) -> bool {
    false
}

#[cfg(any(windows, test))]
const WINDOWS_SYSTEM_SID: &str = "S-1-5-18";
#[cfg(any(windows, test))]
const WINDOWS_BUILTIN_ADMINISTRATORS_SID: &str = "S-1-5-32-544";
#[cfg(any(windows, test))]
const WINDOWS_TRUSTED_INSTALLER_SID: &str =
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464";
#[cfg(any(windows, test))]
const WINDOWS_OBJECT_INHERIT_ACE: u8 = 0x01;
#[cfg(any(windows, test))]
const WINDOWS_CONTAINER_INHERIT_ACE: u8 = 0x02;
#[cfg(any(windows, test))]
const WINDOWS_INHERITED_ACE: u8 = 0x10;
#[cfg(any(windows, test))]
const WINDOWS_FILE_ALL_ACCESS: u32 = 0x001f_01ff;
#[cfg(any(windows, test))]
const WINDOWS_ANCESTOR_MUTATION_MASK: u32 =
    0x0001_0000 | 0x0004_0000 | 0x0008_0000 | 0x0000_0040 | 0x0000_0002 | 0x1000_0000 | 0x4000_0000;
#[cfg(any(windows, test))]
pub(crate) const TOKEN_GROUP_ENABLED_ATTRIBUTE: u32 = 0x0000_0004;
#[cfg(any(windows, test))]
pub(crate) const TOKEN_GROUP_DENY_ONLY_ATTRIBUTE: u32 = 0x0000_0010;

#[cfg(any(windows, test))]
pub(crate) fn validate_dedicated_service_sid(service_identity: &str) -> Result<(), LedgerError> {
    let components = service_identity
        .strip_prefix("S-1-5-80-")
        .map(|suffix| suffix.split('-').collect::<Vec<_>>())
        .filter(|components| components.len() == 5)
        .ok_or_else(|| {
            LedgerError::UnsafeLedgerPath(
                "anchored identity is not a canonical dedicated Windows service SID".to_owned(),
            )
        })?;
    if components.iter().any(|component| {
        component.is_empty()
            || component
                .parse::<u32>()
                .map_or(true, |value| value.to_string() != *component)
    }) {
        return Err(LedgerError::UnsafeLedgerPath(
            "anchored identity is not a canonical dedicated Windows service SID".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(any(windows, test))]
pub(crate) fn validate_service_token_snapshot(
    service_identity: &str,
    token_user: &str,
    token_groups: &[(String, u32)],
) -> Result<(), LedgerError> {
    validate_dedicated_service_sid(service_identity)?;
    if token_user == service_identity {
        return Err(LedgerError::UnsafeLedgerPath(
            "anchored service SID must be an enabled group, not only TokenUser".to_owned(),
        ));
    }
    let matches = token_groups
        .iter()
        .filter(|(sid, _)| sid == service_identity)
        .collect::<Vec<_>>();
    if matches.len() != 1
        || matches[0].1 & TOKEN_GROUP_ENABLED_ATTRIBUTE == 0
        || matches[0].1 & TOKEN_GROUP_DENY_ONLY_ATTRIBUTE != 0
    {
        return Err(LedgerError::UnsafeLedgerPath(
            "anchored service SID is not one enabled non-deny-only token group".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn validate_service_acl_snapshot(
    service_identity: &str,
    owner_identity: &str,
    allow_identities: &[String],
) -> Result<(), LedgerError> {
    validate_dedicated_service_sid(service_identity)?;
    if owner_identity != WINDOWS_SYSTEM_SID && owner_identity != service_identity {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger owner must be the dedicated service SID or SYSTEM".to_owned(),
        ));
    }
    if allow_identities.len() != 2 {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger DACL must bind exactly distinct service SID and SYSTEM".to_owned(),
        ));
    }
    let mut actual = allow_identities.to_vec();
    actual.sort();
    actual.dedup();
    let mut expected = vec![service_identity.to_owned(), WINDOWS_SYSTEM_SID.to_owned()];
    expected.sort();
    if actual.len() != 2 || actual != expected {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger DACL must bind exactly distinct service SID and SYSTEM".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn validate_service_parent_acl_snapshot(
    service_identity: &str,
    owner_identity: &str,
    allow_entries: &[(String, u8)],
) -> Result<(), LedgerError> {
    let identities = allow_entries
        .iter()
        .map(|(identity, _)| identity.clone())
        .collect::<Vec<_>>();
    validate_service_acl_snapshot(service_identity, owner_identity, &identities)?;
    if allow_entries
        .iter()
        .any(|(_, flags)| *flags != WINDOWS_OBJECT_INHERIT_ACE | WINDOWS_CONTAINER_INHERIT_ACE)
    {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger parent ACEs must explicitly apply full control to child files".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn validate_service_sidecar_acl_snapshot(
    service_identity: &str,
    owner_identity: &str,
    allow_entries: &[(String, u32, u8)],
) -> Result<(), LedgerError> {
    let identities = allow_entries
        .iter()
        .map(|(identity, _, _)| identity.clone())
        .collect::<Vec<_>>();
    validate_service_acl_snapshot(service_identity, owner_identity, &identities)?;
    if allow_entries
        .iter()
        .any(|(_, mask, flags)| *mask != WINDOWS_FILE_ALL_ACCESS || *flags != WINDOWS_INHERITED_ACE)
    {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger sidecars must inherit exact service and SYSTEM full control".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(any(windows, test))]
fn validate_windows_ancestor_acl_snapshot(
    service_identity: &str,
    owner_identity: &str,
    allow_entries: &[(String, u32, bool)],
) -> Result<(), LedgerError> {
    validate_dedicated_service_sid(service_identity)?;
    let trusted = |identity: &str| {
        matches!(
            identity,
            WINDOWS_SYSTEM_SID | WINDOWS_BUILTIN_ADMINISTRATORS_SID | WINDOWS_TRUSTED_INSTALLER_SID
        ) || identity == service_identity
    };
    if !trusted(owner_identity) {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger ancestor owner is not a privileged system or service identity".to_owned(),
        ));
    }
    if allow_entries.iter().any(|(identity, mask, inherit_only)| {
        !inherit_only && !trusted(identity) && mask & WINDOWS_ANCESTOR_MUTATION_MASK != 0
    }) {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger ancestor grants namespace or ACL mutation to an untrusted identity".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(all(unix, not(target_os = "linux")))]
fn validate_service_owned_objects(
    _parent: &Path,
    parent_metadata: &fs::Metadata,
    _path: &Path,
    file_metadata: &fs::Metadata,
    _anchored_service_identity: &str,
) -> Result<(), LedgerError> {
    use std::os::unix::fs::MetadataExt as _;

    // SAFETY: geteuid has no preconditions and does not borrow caller memory.
    let effective_uid = unsafe { libc::geteuid() };
    let parent_mode = parent_metadata.mode();
    let file_mode = file_metadata.mode();
    if parent_metadata.uid() != effective_uid
        || file_metadata.uid() != effective_uid
        || parent_mode & 0o077 != 0
        || parent_mode & 0o700 != 0o700
        || file_mode & 0o077 != 0
        || file_mode & 0o600 != 0o600
        || file_metadata.nlink() != 1
    {
        return Err(LedgerError::UnsafeLedgerPath(
            "ledger and parent must be service-owned with private permissions".to_owned(),
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn validate_service_owned_objects(
    parent: &Path,
    _parent_metadata: &fs::Metadata,
    path: &Path,
    _file_metadata: &fs::Metadata,
    anchored_service_identity: &str,
) -> Result<(), LedgerError> {
    windows_path_security::verify_enabled_service_group(anchored_service_identity)?;
    windows_path_security::verify_service_parent(parent, anchored_service_identity)?;
    windows_path_security::verify_service_owned(path, anchored_service_identity)?;
    Ok(())
}

#[cfg(not(any(unix, windows)))]
fn validate_service_owned_objects(
    _parent: &Path,
    _parent_metadata: &fs::Metadata,
    _path: &Path,
    _file_metadata: &fs::Metadata,
    _anchored_service_identity: &str,
) -> Result<(), LedgerError> {
    Err(LedgerError::UnsafeLedgerPath(
        "protected ledger paths are unsupported on this platform".to_owned(),
    ))
}

fn configure_durability(connection: &Connection) -> Result<(), LedgerError> {
    connection.pragma_update(None, "journal_mode", "WAL")?;
    let journal_mode: String =
        connection.pragma_query_value(None, "journal_mode", |row| row.get(0))?;
    if !journal_mode.eq_ignore_ascii_case("wal") {
        return Err(LedgerError::JournalModeNotWal);
    }
    connection.pragma_update(None, "synchronous", "FULL")?;
    let synchronous: i64 = connection.pragma_query_value(None, "synchronous", |row| row.get(0))?;
    if synchronous != 2 {
        return Err(LedgerError::SynchronousNotFull);
    }
    connection.pragma_update(None, "foreign_keys", true)?;
    Ok(())
}

fn initialize_or_validate_schema(connection: &Connection) -> Result<(), LedgerError> {
    let version: i64 = connection.pragma_query_value(None, "user_version", |row| row.get(0))?;
    match version {
        0 => connection.execute_batch(CREATE_SCHEMA)?,
        LEDGER_SCHEMA_VERSION => {}
        other => return Err(LedgerError::UnsupportedSchemaVersion(other)),
    }
    validate_schema_objects(connection)
}

fn validate_schema_objects(connection: &Connection) -> Result<(), LedgerError> {
    let mut statement = connection.prepare("PRAGMA table_info(eval_receipt_attempts)")?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    let expected_columns = [
        "id",
        "request_id",
        "installation_id",
        "run_id",
        "suite_id",
        "case_id",
        "ordinal",
        "attempt_key",
        "state",
        "receipt_id",
        "canonical_receipt",
        "signed_receipt_json",
    ];
    if columns != expected_columns {
        return Err(LedgerError::MalformedLedger("table columns"));
    }
    let mut authority_statement =
        connection.prepare("PRAGMA table_info(eval_receipt_authority)")?;
    let authority_columns = authority_statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    if authority_columns != ["singleton_id", "installation_id", "authority_pin_sha256"] {
        return Err(LedgerError::MalformedLedger("authority table columns"));
    }
    let mut history_statement =
        connection.prepare("PRAGMA table_info(eval_receipt_key_history)")?;
    let history_columns = history_statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    if history_columns
        != [
            "key_epoch",
            "key_id",
            "anchor_sha256",
            "predecessor_key_epoch",
            "predecessor_key_id",
            "predecessor_anchor_sha256",
        ]
    {
        return Err(LedgerError::MalformedLedger("key history table columns"));
    }
    for table in [
        "eval_receipt_attempts",
        "eval_receipt_authority",
        "eval_receipt_key_history",
    ] {
        validate_schema_fingerprint(connection, "table", table)?;
    }
    for (index, expected_columns) in REQUIRED_INDEXES {
        validate_schema_fingerprint(connection, "index", index)?;
        validate_unique_index(connection, index, expected_columns)?;
    }
    for trigger in REQUIRED_TRIGGERS {
        validate_schema_fingerprint(connection, "trigger", trigger)?;
    }
    validate_trigger_semantics(connection)
}

fn validate_schema_fingerprint(
    connection: &Connection,
    object_type: &'static str,
    name: &'static str,
) -> Result<(), LedgerError> {
    let actual = connection
        .query_row(
            "SELECT sql FROM sqlite_schema WHERE type = ?1 AND name = ?2",
            [object_type, name],
            |row| row.get::<_, String>(0),
        )
        .optional()?
        .ok_or(LedgerError::MalformedLedger("required schema object"))?;
    let marker = match object_type {
        "table" => format!("CREATE TABLE {name}"),
        "index" => format!("CREATE UNIQUE INDEX {name}"),
        "trigger" => format!("CREATE TRIGGER {name}"),
        _ => return Err(LedgerError::MalformedLedger("schema object type")),
    };
    let start = CREATE_SCHEMA
        .find(&marker)
        .ok_or(LedgerError::MalformedLedger("authoritative schema source"))?;
    let source = &CREATE_SCHEMA[start..];
    let end_marker = if object_type == "trigger" {
        "END;"
    } else {
        ";"
    };
    let end = source
        .find(end_marker)
        .ok_or(LedgerError::MalformedLedger("authoritative schema source"))?;
    let expected = &source[..end + end_marker.len()];
    if normalize_schema_sql(&actual) != normalize_schema_sql(expected) {
        return Err(LedgerError::MalformedLedger(
            "schema definition fingerprint",
        ));
    }
    Ok(())
}

fn normalize_schema_sql(sql: &str) -> String {
    sql.chars()
        .filter(|character| !character.is_ascii_whitespace() && *character != ';')
        .flat_map(char::to_lowercase)
        .collect()
}

fn validate_unique_index(
    connection: &Connection,
    index_name: &'static str,
    expected_columns: &[&str],
) -> Result<(), LedgerError> {
    let (is_unique, is_partial): (i64, i64) = connection
        .query_row(
            "SELECT \"unique\", partial
             FROM pragma_index_list('eval_receipt_attempts')
             WHERE name = ?1",
            [index_name],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()?
        .ok_or(LedgerError::MalformedLedger("required unique index"))?;
    if is_unique != 1 || is_partial != 0 {
        return Err(LedgerError::MalformedLedger("required unique index"));
    }
    let mut statement =
        connection.prepare("SELECT name FROM pragma_index_info(?1) ORDER BY seqno")?;
    let columns = statement
        .query_map([index_name], |row| row.get::<_, String>(0))?
        .collect::<Result<Vec<_>, _>>()?;
    if columns != expected_columns {
        return Err(LedgerError::MalformedLedger("unique index columns"));
    }
    Ok(())
}

fn validate_trigger_semantics(connection: &Connection) -> Result<(), LedgerError> {
    connection.execute_batch("SAVEPOINT eval_receipt_readiness_probe")?;
    let result = (|| {
        let token: String =
            connection.query_row("SELECT lower(hex(randomblob(16)))", [], |row| row.get(0))?;
        let reserved_request = format!("probe-r-{token}");
        let committed_request = format!("probe-c-{token}");
        let installation_id = format!("probe-i-{token}");
        let run_id = format!("probe-rn-{token}");
        let suite_id = format!("probe-s-{token}");
        let case_id = format!("probe-cs-{token}");
        connection.execute(
            "INSERT INTO eval_receipt_attempts (
                request_id, installation_id, run_id, suite_id, case_id, ordinal,
                attempt_key, state
             ) VALUES (?1, ?2, ?3, ?4, ?5, 4294967295, randomblob(32), 'reserved')",
            params![reserved_request, installation_id, run_id, suite_id, case_id,],
        )?;
        require_constraint_result(connection.execute(
            "DELETE FROM eval_receipt_attempts WHERE request_id = ?1",
            [&reserved_request],
        ))?;
        let mutated_request = format!("probe-m-{token}");
        require_constraint_result(connection.execute(
            "UPDATE eval_receipt_attempts SET request_id = ?1 WHERE request_id = ?2",
            [&mutated_request, &reserved_request],
        ))?;
        connection.execute(
            "INSERT INTO eval_receipt_attempts (
                request_id, installation_id, run_id, suite_id, case_id, ordinal,
                attempt_key, state, receipt_id, canonical_receipt, signed_receipt_json
             ) VALUES (?1, ?2, ?3, ?4, ?5, 4294967294, randomblob(32),
                 'committed', zeroblob(32), x'00', '{}')",
            params![
                committed_request,
                installation_id,
                run_id,
                suite_id,
                case_id,
            ],
        )?;
        require_constraint_result(connection.execute(
            "UPDATE eval_receipt_attempts SET canonical_receipt = x'01'
             WHERE request_id = ?1",
            [&committed_request],
        ))?;
        connection.execute(
            "INSERT OR IGNORE INTO eval_receipt_authority (
                singleton_id, installation_id, authority_pin_sha256
             ) VALUES (1, 'readiness-install', randomblob(32))",
            [],
        )?;
        require_constraint_rejection(
            connection,
            "UPDATE eval_receipt_authority SET installation_id = 'readiness-mutated'
             WHERE singleton_id = 1",
        )?;
        require_constraint_rejection(
            connection,
            "DELETE FROM eval_receipt_authority WHERE singleton_id = 1",
        )?;
        connection.execute(
            "INSERT OR IGNORE INTO eval_receipt_key_history (
                key_epoch, key_id, anchor_sha256, predecessor_key_epoch,
                predecessor_key_id, predecessor_anchor_sha256
             ) VALUES (
                9223372036854775807, randomblob(32), randomblob(32), NULL, NULL, NULL
             )",
            [],
        )?;
        require_constraint_rejection(
            connection,
            "UPDATE eval_receipt_key_history SET anchor_sha256 = randomblob(32)
             WHERE key_epoch = 9223372036854775807",
        )?;
        require_constraint_rejection(
            connection,
            "DELETE FROM eval_receipt_key_history
             WHERE key_epoch = 9223372036854775807",
        )
    })();
    let cleanup = connection.execute_batch(
        "ROLLBACK TO eval_receipt_readiness_probe; RELEASE eval_receipt_readiness_probe",
    );
    cleanup?;
    result
}

fn require_constraint_rejection(
    connection: &Connection,
    statement: &'static str,
) -> Result<(), LedgerError> {
    match connection.execute(statement, []) {
        Err(error) if is_constraint_violation(&error) => Ok(()),
        Err(error) => Err(error.into()),
        Ok(_) => Err(LedgerError::MalformedLedger("trigger semantics")),
    }
}

fn require_constraint_result(result: Result<usize, rusqlite::Error>) -> Result<(), LedgerError> {
    match result {
        Err(error) if is_constraint_violation(&error) => Ok(()),
        Err(error) => Err(error.into()),
        Ok(_) => Err(LedgerError::MalformedLedger("trigger semantics")),
    }
}

fn validate_ledger(connection: &Connection) -> Result<(), LedgerError> {
    validate_schema_objects(connection)?;
    let integrity: String = connection.query_row("PRAGMA quick_check", [], |row| row.get(0))?;
    if integrity != "ok" {
        return Err(LedgerError::IntegrityCheckFailed(integrity));
    }
    let authority = load_authority_state(connection)?;
    let mut statement = connection.prepare(
        "SELECT request_id, installation_id, run_id, suite_id, case_id, ordinal,
                attempt_key, state, receipt_id, canonical_receipt, signed_receipt_json
         FROM eval_receipt_attempts ORDER BY id",
    )?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let request_id: String = row.get(0)?;
        validate_request_id(&request_id)?;
        let ordinal_value: i64 = row.get(5)?;
        let ordinal =
            u32::try_from(ordinal_value).map_err(|_| LedgerError::MalformedLedger("ordinal"))?;
        let identity = AttemptIdentity {
            installation_id: row.get(1)?,
            run_id: row.get(2)?,
            suite_id: row.get(3)?,
            case_id: row.get(4)?,
            ordinal,
        };
        identity.validate()?;
        let stored_attempt = digest_from_blob(row.get(6)?, "attempt_key")?;
        if stored_attempt != attempt_key(&identity)? {
            return Err(LedgerError::MalformedLedger("attempt_key"));
        }
        let state: String = row.get(7)?;
        let receipt_id_blob: Option<Vec<u8>> = row.get(8)?;
        let canonical_blob: Option<Vec<u8>> = row.get(9)?;
        let receipt_json: Option<String> = row.get(10)?;
        match (
            state.as_str(),
            receipt_id_blob,
            canonical_blob,
            receipt_json,
        ) {
            ("reserved", None, None, None) => {}
            ("committed", Some(receipt_id), Some(canonical), Some(receipt_json)) => {
                let receipt: SignedEvalReceipt = serde_json::from_str(&receipt_json)?;
                receipt.verify()?;
                if digest_from_blob(receipt_id, "receipt_id")? != receipt.receipt_id
                    || canonical_receipt_bytes(&receipt.claims)? != canonical
                    || receipt.claims.request_id != request_id
                    || receipt.claims.attempt_identity() != identity
                    || receipt.claims.attempt_key != stored_attempt
                {
                    return Err(LedgerError::MalformedLedger("committed receipt binding"));
                }
                validate_committed_receipt_authority(authority.as_ref(), &receipt)?;
            }
            _ => return Err(LedgerError::MalformedLedger("reservation state")),
        }
    }
    Ok(())
}

#[derive(Clone, Debug)]
struct LedgerAuthorityState {
    installation_id: String,
    _authority_pin_sha256: Digest32,
    key_history: Vec<KeyHistoryBinding>,
}

#[derive(Clone, Copy, Debug)]
struct KeyHistoryBinding {
    key_epoch: u64,
    key_id: Digest32,
    anchor_sha256: Digest32,
}

fn load_authority_state(
    connection: &Connection,
) -> Result<Option<LedgerAuthorityState>, LedgerError> {
    let mut statement = connection.prepare(
        "SELECT singleton_id, installation_id, authority_pin_sha256
         FROM eval_receipt_authority ORDER BY singleton_id",
    )?;
    let mut rows = statement.query([])?;
    let mut count = 0_u8;
    let mut authority = None;
    while let Some(row) = rows.next()? {
        count = count.saturating_add(1);
        let singleton_id: i64 = row.get(0)?;
        let installation_id: String = row.get(1)?;
        if count != 1 || singleton_id != 1 {
            return Err(LedgerError::MalformedLedger("authority singleton"));
        }
        validate_authority_installation_id(&installation_id)?;
        authority = Some((
            installation_id,
            digest_from_blob(row.get(2)?, "authority pin")?,
        ));
    }
    let key_history = load_key_history(connection, count == 1)?;
    Ok(authority.map(
        |(installation_id, authority_pin_sha256)| LedgerAuthorityState {
            installation_id,
            _authority_pin_sha256: authority_pin_sha256,
            key_history,
        },
    ))
}

fn load_key_history(
    connection: &Connection,
    authority_exists: bool,
) -> Result<Vec<KeyHistoryBinding>, LedgerError> {
    let mut statement = connection.prepare(
        "SELECT key_epoch, key_id, anchor_sha256, predecessor_key_epoch,
                predecessor_key_id, predecessor_anchor_sha256
         FROM eval_receipt_key_history ORDER BY key_epoch",
    )?;
    let mut rows = statement.query([])?;
    let mut previous_epoch = None;
    let mut previous = None;
    let mut seen_keys = Vec::new();
    let mut seen_anchors = Vec::new();
    let mut history = Vec::new();
    while let Some(row) = rows.next()? {
        if !authority_exists {
            return Err(LedgerError::MalformedLedger(
                "key history without authority",
            ));
        }
        let epoch: i64 = row.get(0)?;
        let key_id = digest_from_blob(row.get(1)?, "key history id")?;
        let anchor = digest_from_blob(row.get(2)?, "key history anchor")?;
        let predecessor = decode_predecessor(row.get(3)?, row.get(4)?, row.get(5)?)?;
        if epoch < 0
            || previous_epoch.is_some_and(|previous| epoch <= previous)
            || predecessor != previous
            || seen_keys.contains(&key_id)
            || seen_anchors.contains(&anchor)
        {
            return Err(LedgerError::MalformedLedger("key rotation history"));
        }
        previous_epoch = Some(epoch);
        previous = Some(KeyRotationPredecessor {
            key_epoch: u64::try_from(epoch)
                .map_err(|_| LedgerError::MalformedLedger("key rotation epoch"))?,
            key_id,
            anchor_sha256: anchor,
        });
        seen_keys.push(key_id);
        seen_anchors.push(anchor);
        history.push(KeyHistoryBinding {
            key_epoch: u64::try_from(epoch)
                .map_err(|_| LedgerError::MalformedLedger("key rotation epoch"))?,
            key_id,
            anchor_sha256: anchor,
        });
    }
    Ok(history)
}

fn validate_committed_receipt_authority(
    authority: Option<&LedgerAuthorityState>,
    receipt: &SignedEvalReceipt,
) -> Result<(), LedgerError> {
    let Some(authority) = authority else {
        return if receipt.signer.trust == SignerTrust::ProductionProtected {
            Err(LedgerError::MalformedLedger(
                "production receipt in unbound ledger",
            ))
        } else {
            Ok(())
        };
    };
    let key_binding = authority
        .key_history
        .iter()
        .find(|binding| binding.key_epoch == receipt.claims.key_epoch);
    if receipt.signer.trust != SignerTrust::ProductionProtected
        || receipt.claims.installation_id != authority.installation_id
        || key_binding.is_none_or(|binding| {
            binding.key_id != receipt.claims.key_id
                || binding.anchor_sha256 != receipt.claims.anchor_sha256
        })
    {
        return Err(LedgerError::MalformedLedger("committed receipt authority"));
    }
    Ok(())
}

fn validate_committing_authority(
    connection: &Connection,
    claims: &EvalReceiptClaims,
    signer: &SignerIdentity,
) -> Result<(), LedgerError> {
    let binding = connection
        .query_row(
            "SELECT installation_id, authority_pin_sha256
             FROM eval_receipt_authority WHERE singleton_id = 1",
            [],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, Vec<u8>>(1)?)),
        )
        .optional()?;
    let Some((installation_id, authority_pin)) = binding else {
        return if signer.trust == SignerTrust::ProductionProtected {
            Err(LedgerError::AuthorityUnbound)
        } else {
            Ok(())
        };
    };
    let key_epoch = i64::try_from(signer.key_epoch).map_err(|_| LedgerError::KeyEpochOutOfRange)?;
    let key_binding = connection
        .query_row(
            "SELECT key_id, anchor_sha256 FROM eval_receipt_key_history
             WHERE key_epoch = ?1
               AND key_epoch = (SELECT MAX(key_epoch) FROM eval_receipt_key_history)",
            [key_epoch],
            |row| Ok((row.get::<_, Vec<u8>>(0)?, row.get::<_, Vec<u8>>(1)?)),
        )
        .optional()?;
    let Some((key_id, anchor)) = key_binding else {
        return Err(LedgerError::AuthorityBindingMismatch);
    };
    if installation_id != claims.installation_id
        || digest_from_blob(key_id, "key history id")? != claims.key_id
        || digest_from_blob(anchor, "key history anchor")? != claims.anchor_sha256
        || signer.anchor_sha256 != Some(claims.anchor_sha256)
        || signer.authority_pin_sha256 != Some(digest_from_blob(authority_pin, "authority pin")?)
    {
        return Err(LedgerError::AuthorityBindingMismatch);
    }
    Ok(())
}

fn validate_reservation_claim_bindings(
    reservation: &ReceiptReservation,
    claims: &EvalReceiptClaims,
) -> Result<(), LedgerError> {
    claims.validate()?;
    if claims.request_id != reservation.request_id
        || claims.attempt_identity() != reservation.identity
        || claims.attempt_key != reservation.attempt_key
    {
        return Err(LedgerError::ReservationBindingMismatch);
    }
    Ok(())
}

fn digest_from_blob(blob: Vec<u8>, field: &'static str) -> Result<Digest32, LedgerError> {
    let bytes: [u8; 32] = blob
        .try_into()
        .map_err(|_| LedgerError::MalformedLedger(field))?;
    Ok(Digest32::from_bytes(bytes))
}

fn decode_predecessor(
    key_epoch: Option<i64>,
    key_id: Option<Vec<u8>>,
    anchor_sha256: Option<Vec<u8>>,
) -> Result<Option<KeyRotationPredecessor>, LedgerError> {
    match (key_epoch, key_id, anchor_sha256) {
        (None, None, None) => Ok(None),
        (Some(key_epoch), Some(key_id), Some(anchor_sha256)) => Ok(Some(KeyRotationPredecessor {
            key_epoch: u64::try_from(key_epoch)
                .map_err(|_| LedgerError::MalformedLedger("predecessor key epoch"))?,
            key_id: digest_from_blob(key_id, "predecessor key id")?,
            anchor_sha256: digest_from_blob(anchor_sha256, "predecessor anchor")?,
        })),
        _ => Err(LedgerError::MalformedLedger("partial rotation predecessor")),
    }
}

fn validate_request_id(request_id: &str) -> Result<(), LedgerError> {
    let mut bytes = request_id.bytes();
    let Some(first) = bytes.next() else {
        return Err(LedgerError::InvalidRequestId);
    };
    if request_id.len() > 128
        || !first.is_ascii_alphanumeric()
        || !bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(LedgerError::InvalidRequestId);
    }
    Ok(())
}

fn validate_authority_installation_id(installation_id: &str) -> Result<(), LedgerError> {
    let mut bytes = installation_id.bytes();
    let Some(first) = bytes.next() else {
        return Err(LedgerError::InvalidAuthorityBinding);
    };
    if installation_id.len() > 128
        || !first.is_ascii_alphanumeric()
        || !bytes
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b':'))
    {
        return Err(LedgerError::InvalidAuthorityBinding);
    }
    Ok(())
}

fn is_constraint_violation(error: &rusqlite::Error) -> bool {
    matches!(
        error,
        rusqlite::Error::SqliteFailure(inner, _)
            if inner.code == ErrorCode::ConstraintViolation
    )
}

#[cfg(target_os = "linux")]
mod linux_path_security {
    use std::{
        ffi::{CString, OsStr, OsString},
        fs::{self, File},
        io,
        os::{
            fd::{AsRawFd as _, FromRawFd as _, RawFd},
            unix::{ffi::OsStrExt as _, fs::MetadataExt as _},
        },
        path::{Component, Path, PathBuf},
    };

    use super::LedgerError;

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct FileIdentity {
        device: u64,
        inode: u64,
    }

    impl FileIdentity {
        fn from_stat(stat: &libc::stat) -> Self {
            Self {
                device: stat.st_dev,
                inode: stat.st_ino,
            }
        }

        fn from_metadata(metadata: &fs::Metadata) -> Self {
            Self {
                device: metadata.dev(),
                inode: metadata.ino(),
            }
        }
    }

    pub(super) struct ProtectedLedgerPath {
        parent: File,
        file: File,
        parent_identity: FileIdentity,
        file_identity: FileIdentity,
        file_name: OsString,
        sqlite_path: PathBuf,
    }

    impl ProtectedLedgerPath {
        pub(super) fn open(path: &Path) -> Result<Self, LedgerError> {
            let (parent, parent_identity, file_name) = securely_open_parent(path)?;
            let file = open_at(
                parent.as_raw_fd(),
                &file_name,
                libc::O_RDWR | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )?;
            let file_stat = fstat(&file)?;
            validate_ledger_file(&file_stat)?;
            let file_identity = FileIdentity::from_stat(&file_stat);
            let sqlite_path =
                PathBuf::from(format!("/proc/self/fd/{}", parent.as_raw_fd())).join(&file_name);
            validate_proc_aliases(
                parent.as_raw_fd(),
                &sqlite_path,
                parent_identity,
                file_identity,
            )?;
            Ok(Self {
                parent,
                file,
                parent_identity,
                file_identity,
                file_name,
                sqlite_path,
            })
        }

        pub(super) fn sqlite_path(&self) -> &Path {
            &self.sqlite_path
        }

        pub(super) fn validate_current_identity(
            &self,
            original_path: &Path,
        ) -> Result<(), LedgerError> {
            let retained_parent_stat = fstat(&self.parent)?;
            validate_ancestor_directory(&retained_parent_stat, true)?;
            let retained_file_stat = fstat(&self.file)?;
            validate_ledger_file(&retained_file_stat)?;
            if FileIdentity::from_stat(&retained_parent_stat) != self.parent_identity
                || FileIdentity::from_stat(&retained_file_stat) != self.file_identity
            {
                return Err(identity_mismatch());
            }
            validate_proc_aliases(
                self.parent.as_raw_fd(),
                &self.sqlite_path,
                self.parent_identity,
                self.file_identity,
            )?;

            let (current_parent, current_parent_identity, current_file_name) =
                securely_open_parent(original_path)?;
            if current_parent_identity != self.parent_identity
                || current_file_name != self.file_name
            {
                return Err(identity_mismatch());
            }
            let current_file = open_at(
                current_parent.as_raw_fd(),
                &current_file_name,
                libc::O_RDONLY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )?;
            let current_file_stat = fstat(&current_file)?;
            validate_ledger_file(&current_file_stat)?;
            if FileIdentity::from_stat(&current_file_stat) != self.file_identity {
                return Err(identity_mismatch());
            }
            Ok(())
        }
    }

    fn securely_open_parent(path: &Path) -> Result<(File, FileIdentity, OsString), LedgerError> {
        if !path.is_absolute() {
            return Err(LedgerError::UnsafeLedgerPath(
                "path must be absolute".to_owned(),
            ));
        }
        let file_name = path
            .file_name()
            .filter(|name| !name.is_empty())
            .ok_or_else(|| {
                LedgerError::UnsafeLedgerPath("path must name a ledger file".to_owned())
            })?
            .to_os_string();
        let parent = path.parent().ok_or_else(|| {
            LedgerError::UnsafeLedgerPath("path must have a protected parent".to_owned())
        })?;
        let mut components = parent.components();
        if !matches!(components.next(), Some(Component::RootDir)) {
            return Err(LedgerError::UnsafeLedgerPath(
                "path must be absolute and normalized".to_owned(),
            ));
        }
        let directory_names = components
            .map(|component| match component {
                Component::Normal(name) => Ok(name.to_os_string()),
                _ => Err(LedgerError::UnsafeLedgerPath(
                    "path must be absolute and normalized".to_owned(),
                )),
            })
            .collect::<Result<Vec<_>, _>>()?;

        let root = open_root_directory()?;
        let root_stat = fstat(&root)?;
        validate_ancestor_directory(&root_stat, directory_names.is_empty())?;
        let mut current = root;
        for (index, name) in directory_names.iter().enumerate() {
            let next = open_at(
                current.as_raw_fd(),
                name,
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )?;
            let stat = fstat(&next)?;
            validate_ancestor_directory(&stat, index + 1 == directory_names.len())?;
            current = next;
        }
        let identity = FileIdentity::from_stat(&fstat(&current)?);
        Ok((current, identity, file_name))
    }

    fn open_root_directory() -> Result<File, LedgerError> {
        let root = CString::new("/").expect("root path contains no NUL");
        // SAFETY: root is NUL-terminated and the flags require no variadic mode argument.
        let descriptor = unsafe {
            libc::open(
                root.as_ptr(),
                libc::O_RDONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC,
            )
        };
        file_from_descriptor(descriptor)
    }

    fn open_at(parent: RawFd, name: &OsStr, flags: i32) -> Result<File, LedgerError> {
        let name = CString::new(name.as_bytes()).map_err(|_| {
            LedgerError::UnsafeLedgerPath("ledger path contains an embedded NUL".to_owned())
        })?;
        // SAFETY: parent is a retained live directory descriptor, name is NUL-terminated,
        // and callers provide flags that require no variadic mode argument.
        let descriptor = unsafe { libc::openat(parent, name.as_ptr(), flags) };
        file_from_descriptor(descriptor)
    }

    fn file_from_descriptor(descriptor: RawFd) -> Result<File, LedgerError> {
        if descriptor < 0 {
            return Err(io::Error::last_os_error().into());
        }
        // SAFETY: a successful open/openat returns one uniquely owned descriptor.
        Ok(unsafe { File::from_raw_fd(descriptor) })
    }

    fn fstat(file: &File) -> Result<libc::stat, LedgerError> {
        // SAFETY: zeroed is a valid initial state for the fstat output structure.
        let mut stat = unsafe { std::mem::zeroed::<libc::stat>() };
        // SAFETY: file owns a live descriptor and stat is writable output storage.
        if unsafe { libc::fstat(file.as_raw_fd(), &mut stat) } != 0 {
            return Err(io::Error::last_os_error().into());
        }
        Ok(stat)
    }

    fn validate_ancestor_directory(
        stat: &libc::stat,
        immediate_parent: bool,
    ) -> Result<(), LedgerError> {
        let mode = stat.st_mode;
        if mode & libc::S_IFMT != libc::S_IFDIR {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger ancestor is not a directory".to_owned(),
            ));
        }
        // SAFETY: geteuid has no preconditions and does not borrow caller memory.
        let effective_uid = unsafe { libc::geteuid() };
        if stat.st_uid != 0 && stat.st_uid != effective_uid {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger ancestors must be owned by root or the engine service".to_owned(),
            ));
        }
        if mode & 0o022 != 0 {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger ancestor directory is group- or other-writable".to_owned(),
            ));
        }
        if immediate_parent
            && (stat.st_uid != effective_uid || mode & 0o077 != 0 || mode & 0o700 != 0o700)
        {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger parent must be service-owned with mode 0700".to_owned(),
            ));
        }
        Ok(())
    }

    fn validate_ledger_file(stat: &libc::stat) -> Result<(), LedgerError> {
        // SAFETY: geteuid has no preconditions and does not borrow caller memory.
        let effective_uid = unsafe { libc::geteuid() };
        let mode = stat.st_mode;
        if mode & libc::S_IFMT != libc::S_IFREG
            || stat.st_uid != effective_uid
            || mode & 0o077 != 0
            || mode & 0o600 != 0o600
            || stat.st_nlink != 1
        {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger must be a service-owned mode-0600 single-link regular file".to_owned(),
            ));
        }
        Ok(())
    }

    fn validate_proc_aliases(
        parent_descriptor: RawFd,
        sqlite_path: &Path,
        parent_identity: FileIdentity,
        file_identity: FileIdentity,
    ) -> Result<(), LedgerError> {
        let proc_parent = PathBuf::from(format!("/proc/self/fd/{parent_descriptor}"));
        let proc_parent_metadata = fs::metadata(&proc_parent)?;
        let proc_file_metadata = fs::metadata(sqlite_path)?;
        if FileIdentity::from_metadata(&proc_parent_metadata) != parent_identity
            || FileIdentity::from_metadata(&proc_file_metadata) != file_identity
        {
            return Err(identity_mismatch());
        }
        Ok(())
    }

    fn identity_mismatch() -> LedgerError {
        LedgerError::UnsafeLedgerPath(
            "ledger path no longer resolves to the retained parent and file identity".to_owned(),
        )
    }
}

#[cfg(windows)]
mod windows_path_security {
    use std::{
        ffi::c_void,
        fs, io,
        mem::size_of,
        os::windows::ffi::OsStrExt as _,
        path::{Component, Path, PathBuf, Prefix},
        ptr,
    };

    use windows_sys::Win32::{
        Foundation::{
            CloseHandle, GetLastError, LocalFree, ERROR_INSUFFICIENT_BUFFER, HANDLE,
            INVALID_HANDLE_VALUE,
        },
        Security::{
            Authorization::{
                ConvertSidToStringSidW, GetNamedSecurityInfoW, GetSecurityInfo, SE_FILE_OBJECT,
            },
            GetAce, GetSecurityDescriptorControl, GetTokenInformation, TokenGroups, TokenUser,
            ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, DACL_SECURITY_INFORMATION, INHERITED_ACE,
            INHERIT_ONLY_ACE, OWNER_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR, PSID,
            SE_DACL_PROTECTED, SID_AND_ATTRIBUTES, TOKEN_GROUPS, TOKEN_INFORMATION_CLASS,
            TOKEN_QUERY, TOKEN_USER,
        },
        Storage::FileSystem::{
            CreateFileW, GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION, FILE_ALL_ACCESS,
            FILE_ATTRIBUTE_DIRECTORY, FILE_ATTRIBUTE_REPARSE_POINT, FILE_FLAG_BACKUP_SEMANTICS,
            FILE_FLAG_OPEN_REPARSE_POINT, FILE_READ_ATTRIBUTES, FILE_READ_DATA, FILE_SHARE_READ,
            FILE_SHARE_WRITE, OPEN_EXISTING, READ_CONTROL,
        },
        System::{
            SystemServices::{ACCESS_ALLOWED_ACE_TYPE, ACCESS_DENIED_ACE_TYPE},
            Threading::{GetCurrentProcess, OpenProcessToken},
        },
    };

    use super::{
        validate_dedicated_service_sid, validate_service_acl_snapshot,
        validate_service_parent_acl_snapshot, validate_service_sidecar_acl_snapshot,
        validate_service_token_snapshot, validate_windows_ancestor_acl_snapshot, LedgerError,
    };

    #[derive(Clone, Copy, Debug, Eq, PartialEq)]
    struct FileIdentity {
        volume_serial_number: u32,
        file_index: u64,
    }

    struct RetainedPathHandle {
        path: PathBuf,
        handle: OwnedHandle,
        identity: FileIdentity,
        immediate_parent: bool,
    }

    pub(super) struct ProtectedLedgerPath {
        ancestors: Vec<RetainedPathHandle>,
        file_path: PathBuf,
        file_handle: OwnedHandle,
        file_identity: FileIdentity,
    }

    impl ProtectedLedgerPath {
        pub(super) fn open(path: &Path, service_identity: &str) -> Result<Self, LedgerError> {
            verify_enabled_service_group(service_identity)?;
            validate_local_normalized_path(path)?;
            let parent = path.parent().ok_or_else(|| {
                LedgerError::UnsafeLedgerPath("path must have a protected parent".to_owned())
            })?;
            let mut ancestor_paths = parent
                .ancestors()
                .map(Path::to_path_buf)
                .collect::<Vec<_>>();
            ancestor_paths.reverse();
            let ancestor_count = ancestor_paths.len();
            let mut ancestors = Vec::with_capacity(ancestor_count);
            for (index, ancestor_path) in ancestor_paths.into_iter().enumerate() {
                let handle = open_no_share_delete(&ancestor_path, true)?;
                let identity = handle_identity(&handle, true)?;
                let immediate_parent = index + 1 == ancestor_count;
                if immediate_parent {
                    verify_service_owned_handle(&handle, service_identity, true)?;
                } else {
                    verify_mutation_safe_ancestor_handle(&handle, service_identity)?;
                }
                ancestors.push(RetainedPathHandle {
                    path: ancestor_path,
                    handle,
                    identity,
                    immediate_parent,
                });
            }
            let file_handle = open_no_share_delete(path, false)?;
            let file_identity = handle_identity(&file_handle, false)?;
            verify_service_owned_handle(&file_handle, service_identity, false)?;
            verify_existing_sidecars(path, service_identity)?;
            Ok(Self {
                ancestors,
                file_path: path.to_path_buf(),
                file_handle,
                file_identity,
            })
        }

        pub(super) fn validate_current_identity(
            &self,
            original_path: &Path,
            service_identity: &str,
        ) -> Result<(), LedgerError> {
            verify_enabled_service_group(service_identity)?;
            validate_local_normalized_path(original_path)?;
            if original_path != self.file_path {
                return Err(identity_mismatch());
            }
            for retained in &self.ancestors {
                if handle_identity(&retained.handle, true)? != retained.identity {
                    return Err(identity_mismatch());
                }
                if retained.immediate_parent {
                    verify_service_owned_handle(&retained.handle, service_identity, true)?;
                } else {
                    verify_mutation_safe_ancestor_handle(&retained.handle, service_identity)?;
                }
                let current = open_no_share_delete(&retained.path, true)?;
                if handle_identity(&current, true)? != retained.identity {
                    return Err(identity_mismatch());
                }
            }
            if handle_identity(&self.file_handle, false)? != self.file_identity {
                return Err(identity_mismatch());
            }
            verify_service_owned_handle(&self.file_handle, service_identity, false)?;
            let current_file = open_no_share_delete(&self.file_path, false)?;
            if handle_identity(&current_file, false)? != self.file_identity {
                return Err(identity_mismatch());
            }
            verify_existing_sidecars(&self.file_path, service_identity)?;
            Ok(())
        }
    }

    pub(super) fn verify_enabled_service_group(service_identity: &str) -> Result<(), LedgerError> {
        validate_dedicated_service_sid(service_identity)?;
        let (token_user, token_groups) = current_process_token_snapshot()?;
        validate_service_token_snapshot(service_identity, &token_user, &token_groups)
    }

    pub(super) fn verify_service_owned(
        path: &Path,
        service_identity: &str,
    ) -> Result<(), LedgerError> {
        validate_dedicated_service_sid(service_identity)?;
        let descriptor = query_descriptor(path)?;
        verify_service_owned_descriptor(&descriptor, service_identity, false)
    }

    pub(super) fn verify_service_parent(
        path: &Path,
        service_identity: &str,
    ) -> Result<(), LedgerError> {
        validate_dedicated_service_sid(service_identity)?;
        let descriptor = query_descriptor(path)?;
        verify_service_owned_descriptor(&descriptor, service_identity, true)
    }

    fn verify_service_owned_handle(
        handle: &OwnedHandle,
        service_identity: &str,
        parent: bool,
    ) -> Result<(), LedgerError> {
        validate_dedicated_service_sid(service_identity)?;
        let descriptor = query_handle_descriptor(handle.0)?;
        verify_service_owned_descriptor(&descriptor, service_identity, parent)
    }

    fn verify_service_owned_descriptor(
        descriptor: &Descriptor,
        service_identity: &str,
        parent: bool,
    ) -> Result<(), LedgerError> {
        let mut control = 0_u16;
        let mut revision = 0_u32;
        // SAFETY: descriptor owns a valid security descriptor for the duration of this call.
        if unsafe {
            GetSecurityDescriptorControl(
                descriptor.security_descriptor,
                &mut control,
                &mut revision,
            )
        } == 0
        {
            return Err(io::Error::last_os_error().into());
        }
        if control & SE_DACL_PROTECTED == 0 {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger DACL inheritance must be disabled".to_owned(),
            ));
        }
        let owner = sid_to_string(descriptor.owner)?;
        let acl = descriptor.dacl;
        // SAFETY: descriptor owns the ACL and the null check precedes dereference.
        if acl.is_null() || unsafe { (*acl).AceCount } != 2 {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger DACL must contain exactly service-owner and SYSTEM".to_owned(),
            ));
        }
        let mut allow_entries = Vec::with_capacity(2);
        for index in 0..2 {
            let mut raw_ace = ptr::null_mut();
            // SAFETY: the ACL has exactly two entries and raw_ace is writable output storage.
            if unsafe { GetAce(acl, index, &mut raw_ace) } == 0 {
                return Err(io::Error::last_os_error().into());
            }
            let ace = raw_ace.cast::<ACCESS_ALLOWED_ACE>();
            // SAFETY: GetAce returned a live ACE header within the descriptor-owned ACL.
            let header = unsafe { (*ace).Header };
            if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE
                || u32::from(header.AceFlags) & INHERITED_ACE != 0
            {
                return Err(LedgerError::UnsafeLedgerPath(
                    "ledger DACL contains a non-explicit allow entry".to_owned(),
                ));
            }
            // SAFETY: the checked ACE type guarantees ACCESS_ALLOWED_ACE layout.
            if unsafe { (*ace).Mask } & FILE_ALL_ACCESS != FILE_ALL_ACCESS {
                return Err(LedgerError::UnsafeLedgerPath(
                    "ledger DACL entries must grant full control".to_owned(),
                ));
            }
            // SAFETY: the checked ACE layout places a variable-length SID at SidStart.
            let sid = unsafe { ptr::addr_of!((*ace).SidStart).cast_mut().cast::<c_void>() };
            let text = sid_to_string(sid)?;
            allow_entries.push((text, header.AceFlags));
        }
        if parent {
            validate_service_parent_acl_snapshot(service_identity, &owner, &allow_entries)
        } else {
            let allow_identities = allow_entries
                .into_iter()
                .map(|(identity, _)| identity)
                .collect::<Vec<_>>();
            validate_service_acl_snapshot(service_identity, &owner, &allow_identities)
        }
    }

    fn validate_local_normalized_path(path: &Path) -> Result<(), LedgerError> {
        if path.as_os_str().encode_wide().any(|unit| unit == 0) {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger path contains an embedded NUL".to_owned(),
            ));
        }
        let mut components = path.components();
        let local_disk = matches!(
            components.next(),
            Some(Component::Prefix(prefix))
                if matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))
        );
        if !local_disk
            || !matches!(components.next(), Some(Component::RootDir))
            || components.any(|component| !matches!(component, Component::Normal(_)))
        {
            return Err(LedgerError::UnsafeLedgerPath(
                "Windows ledger path must be a normalized absolute local-disk path".to_owned(),
            ));
        }
        Ok(())
    }

    fn open_no_share_delete(path: &Path, directory: bool) -> Result<OwnedHandle, LedgerError> {
        let wide = path
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        // SAFETY: wide is NUL-terminated, security attributes and template are null,
        // and OPEN_EXISTING requires no creation security descriptor.
        let handle = unsafe {
            CreateFileW(
                wide.as_ptr(),
                FILE_READ_DATA | FILE_READ_ATTRIBUTES | READ_CONTROL,
                FILE_SHARE_READ | FILE_SHARE_WRITE,
                ptr::null(),
                OPEN_EXISTING,
                FILE_FLAG_OPEN_REPARSE_POINT
                    | if directory {
                        FILE_FLAG_BACKUP_SEMANTICS
                    } else {
                        0
                    },
                ptr::null_mut(),
            )
        };
        if handle == INVALID_HANDLE_VALUE {
            return Err(io::Error::last_os_error().into());
        }
        Ok(OwnedHandle(handle))
    }

    fn handle_identity(handle: &OwnedHandle, directory: bool) -> Result<FileIdentity, LedgerError> {
        // SAFETY: zeroed is a valid initialization for the Win32 output structure.
        let mut information = unsafe { std::mem::zeroed::<BY_HANDLE_FILE_INFORMATION>() };
        // SAFETY: handle is live and information is writable output storage.
        if unsafe { GetFileInformationByHandle(handle.0, &mut information) } == 0 {
            return Err(io::Error::last_os_error().into());
        }
        let attributes = information.dwFileAttributes;
        if attributes & FILE_ATTRIBUTE_REPARSE_POINT != 0
            || (attributes & FILE_ATTRIBUTE_DIRECTORY != 0) != directory
        {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger path handle is a reparse point or wrong object type".to_owned(),
            ));
        }
        Ok(FileIdentity {
            volume_serial_number: information.dwVolumeSerialNumber,
            file_index: (u64::from(information.nFileIndexHigh) << 32)
                | u64::from(information.nFileIndexLow),
        })
    }

    fn verify_mutation_safe_ancestor_handle(
        handle: &OwnedHandle,
        service_identity: &str,
    ) -> Result<(), LedgerError> {
        let descriptor = query_handle_descriptor(handle.0)?;
        let owner = sid_to_string(descriptor.owner)?;
        let acl = descriptor.dacl;
        if acl.is_null() {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger ancestor has a null DACL".to_owned(),
            ));
        }
        // SAFETY: descriptor owns the non-null ACL for this function's duration.
        let ace_count = unsafe { (*acl).AceCount };
        let mut allow_entries = Vec::new();
        for index in 0..u32::from(ace_count) {
            let mut raw_ace = ptr::null_mut();
            // SAFETY: index is bounded by AceCount and raw_ace is writable output storage.
            if unsafe { GetAce(acl, index, &mut raw_ace) } == 0 {
                return Err(io::Error::last_os_error().into());
            }
            // SAFETY: GetAce returned a live ACE header within the descriptor-owned ACL.
            let header = unsafe { &*raw_ace.cast::<ACE_HEADER>() };
            if u32::from(header.AceType) == ACCESS_DENIED_ACE_TYPE {
                continue;
            }
            if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE {
                return Err(LedgerError::UnsafeLedgerPath(
                    "ledger ancestor DACL contains an unsupported allow ACE".to_owned(),
                ));
            }
            let ace = raw_ace.cast::<ACCESS_ALLOWED_ACE>();
            // SAFETY: the checked ACE type guarantees ACCESS_ALLOWED_ACE layout.
            let mask = unsafe { (*ace).Mask };
            // SAFETY: the checked ACE layout places a variable-length SID at SidStart.
            let sid = unsafe { ptr::addr_of!((*ace).SidStart).cast_mut().cast::<c_void>() };
            allow_entries.push((
                sid_to_string(sid)?,
                mask,
                u32::from(header.AceFlags) & INHERIT_ONLY_ACE != 0,
            ));
        }
        validate_windows_ancestor_acl_snapshot(service_identity, &owner, &allow_entries)
    }

    fn verify_existing_sidecars(
        ledger_path: &Path,
        service_identity: &str,
    ) -> Result<(), LedgerError> {
        for suffix in ["-wal", "-shm"] {
            let mut sidecar_name = ledger_path.as_os_str().to_os_string();
            sidecar_name.push(suffix);
            let sidecar_path = PathBuf::from(sidecar_name);
            match fs::symlink_metadata(&sidecar_path) {
                Ok(_) => {
                    let handle = open_no_share_delete(&sidecar_path, false)?;
                    handle_identity(&handle, false)?;
                    verify_service_sidecar_handle(&handle, service_identity)?;
                }
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error.into()),
            }
        }
        Ok(())
    }

    fn verify_service_sidecar_handle(
        handle: &OwnedHandle,
        service_identity: &str,
    ) -> Result<(), LedgerError> {
        let descriptor = query_handle_descriptor(handle.0)?;
        let owner = sid_to_string(descriptor.owner)?;
        let acl = descriptor.dacl;
        // SAFETY: descriptor owns the ACL and the null check precedes dereference.
        if acl.is_null() || unsafe { (*acl).AceCount } != 2 {
            return Err(LedgerError::UnsafeLedgerPath(
                "ledger sidecar DACL must contain exactly inherited service and SYSTEM".to_owned(),
            ));
        }
        let mut allow_entries = Vec::with_capacity(2);
        for index in 0..2 {
            let mut raw_ace = ptr::null_mut();
            // SAFETY: the ACL has exactly two entries and raw_ace is writable output storage.
            if unsafe { GetAce(acl, index, &mut raw_ace) } == 0 {
                return Err(io::Error::last_os_error().into());
            }
            let ace = raw_ace.cast::<ACCESS_ALLOWED_ACE>();
            // SAFETY: GetAce returned a live ACE header within the descriptor-owned ACL.
            let header = unsafe { (*ace).Header };
            if u32::from(header.AceType) != ACCESS_ALLOWED_ACE_TYPE {
                return Err(LedgerError::UnsafeLedgerPath(
                    "ledger sidecar DACL contains a non-allow entry".to_owned(),
                ));
            }
            // SAFETY: the checked ACE type guarantees ACCESS_ALLOWED_ACE layout.
            let mask = unsafe { (*ace).Mask };
            // SAFETY: the checked ACE layout places a variable-length SID at SidStart.
            let sid = unsafe { ptr::addr_of!((*ace).SidStart).cast_mut().cast::<c_void>() };
            allow_entries.push((sid_to_string(sid)?, mask, header.AceFlags));
        }
        validate_service_sidecar_acl_snapshot(service_identity, &owner, &allow_entries)
    }

    fn identity_mismatch() -> LedgerError {
        LedgerError::UnsafeLedgerPath(
            "Windows ledger path no longer resolves to retained object identities".to_owned(),
        )
    }

    fn query_descriptor(path: &Path) -> io::Result<Descriptor> {
        let wide = path
            .as_os_str()
            .encode_wide()
            .chain(Some(0))
            .collect::<Vec<_>>();
        let mut security_descriptor = ptr::null_mut();
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        // SAFETY: wide is NUL-terminated and all requested outputs are valid stack pointers.
        let status = unsafe {
            GetNamedSecurityInfoW(
                wide.as_ptr(),
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                ptr::null_mut(),
                &mut dacl,
                ptr::null_mut(),
                &mut security_descriptor,
            )
        };
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status as i32));
        }
        if security_descriptor.is_null() || owner.is_null() {
            if !security_descriptor.is_null() {
                // SAFETY: the Win32 security API allocated this descriptor with LocalAlloc.
                unsafe { LocalFree(security_descriptor.cast::<c_void>()) };
            }
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "ledger path has no security owner",
            ));
        }
        Ok(Descriptor {
            security_descriptor,
            owner,
            dacl,
        })
    }

    fn query_handle_descriptor(handle: HANDLE) -> io::Result<Descriptor> {
        let mut security_descriptor = ptr::null_mut();
        let mut owner = ptr::null_mut();
        let mut dacl = ptr::null_mut();
        // SAFETY: handle is live and all requested outputs are valid stack pointers.
        let status = unsafe {
            GetSecurityInfo(
                handle,
                SE_FILE_OBJECT,
                OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
                &mut owner,
                ptr::null_mut(),
                &mut dacl,
                ptr::null_mut(),
                &mut security_descriptor,
            )
        };
        if status != 0 {
            return Err(io::Error::from_raw_os_error(status as i32));
        }
        if security_descriptor.is_null() || owner.is_null() {
            if !security_descriptor.is_null() {
                // SAFETY: the Win32 security API allocated this descriptor with LocalAlloc.
                unsafe { LocalFree(security_descriptor.cast::<c_void>()) };
            }
            return Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "ledger path handle has no security owner",
            ));
        }
        Ok(Descriptor {
            security_descriptor,
            owner,
            dacl,
        })
    }

    fn current_process_token_snapshot() -> Result<(String, Vec<(String, u32)>), LedgerError> {
        let mut token = ptr::null_mut();
        // SAFETY: the current process pseudo-handle is valid and token is writable output.
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) } == 0 {
            return Err(io::Error::last_os_error().into());
        }
        let token = OwnedHandle(token);
        let user_storage = information_buffer(token.0, TokenUser)?;
        if user_storage.len() * size_of::<usize>() < size_of::<TOKEN_USER>() {
            return Err(LedgerError::UnsafeLedgerPath(
                "current process TokenUser record is truncated".to_owned(),
            ));
        }
        // SAFETY: the buffer is aligned and was populated for TokenUser.
        let user = unsafe { &*user_storage.as_ptr().cast::<TOKEN_USER>() };
        let user_sid = sid_to_string(user.User.Sid)?;

        let group_storage = information_buffer(token.0, TokenGroups)?;
        let group_bytes = group_storage.len() * size_of::<usize>();
        let group_offset = std::mem::offset_of!(TOKEN_GROUPS, Groups);
        if group_bytes < group_offset {
            return Err(LedgerError::UnsafeLedgerPath(
                "current process TokenGroups record is truncated".to_owned(),
            ));
        }
        // SAFETY: the buffer is aligned and was populated for TokenGroups.
        let token_groups = unsafe { &*group_storage.as_ptr().cast::<TOKEN_GROUPS>() };
        let group_count = usize::try_from(token_groups.GroupCount).map_err(|_| {
            LedgerError::UnsafeLedgerPath("process token group count overflowed".to_owned())
        })?;
        let available_groups = (group_bytes - group_offset) / size_of::<SID_AND_ATTRIBUTES>();
        if group_count > available_groups {
            return Err(LedgerError::UnsafeLedgerPath(
                "current process TokenGroups record is truncated".to_owned(),
            ));
        }
        // SAFETY: group_count is bounded by the variable-length array bytes above.
        let groups = unsafe {
            std::slice::from_raw_parts(token_groups.Groups.as_ptr(), group_count)
                .iter()
                .map(|group| Ok((sid_to_string(group.Sid)?, group.Attributes)))
                .collect::<Result<Vec<_>, io::Error>>()?
        };
        Ok((user_sid, groups))
    }

    fn information_buffer(
        token: HANDLE,
        information_class: TOKEN_INFORMATION_CLASS,
    ) -> io::Result<Vec<usize>> {
        let mut required = 0_u32;
        // SAFETY: documented sizing call with null buffer and writable byte count.
        let result = unsafe {
            GetTokenInformation(token, information_class, ptr::null_mut(), 0, &mut required)
        };
        // SAFETY: GetLastError is read immediately after the sizing call.
        let error = unsafe { GetLastError() };
        if result != 0 || error != ERROR_INSUFFICIENT_BUFFER || required == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut storage = vec![0_usize; (required as usize).div_ceil(size_of::<usize>())];
        // SAFETY: storage is aligned and contains at least required writable bytes.
        if unsafe {
            GetTokenInformation(
                token,
                information_class,
                storage.as_mut_ptr().cast::<c_void>(),
                required,
                &mut required,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(storage)
    }

    fn sid_to_string(sid: PSID) -> io::Result<String> {
        let mut text = ptr::null_mut();
        // SAFETY: sid is owned by a live token or descriptor and text is writable output.
        if unsafe { ConvertSidToStringSidW(sid, &mut text) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let mut length = 0;
        // SAFETY: the conversion API returned a NUL-terminated UTF-16 allocation.
        while unsafe { *text.add(length) } != 0 {
            length += 1;
        }
        // SAFETY: length was measured inside the live allocation.
        let result = String::from_utf16(unsafe { std::slice::from_raw_parts(text, length) })
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error));
        // SAFETY: ConvertSidToStringSidW allocated text with LocalAlloc.
        unsafe { LocalFree(text.cast::<c_void>()) };
        result
    }

    struct Descriptor {
        security_descriptor: PSECURITY_DESCRIPTOR,
        owner: PSID,
        dacl: *mut ACL,
    }

    impl Drop for Descriptor {
        fn drop(&mut self) {
            // SAFETY: GetNamedSecurityInfoW allocated this descriptor with LocalAlloc.
            unsafe { LocalFree(self.security_descriptor.cast::<c_void>()) };
        }
    }

    struct OwnedHandle(HANDLE);

    // SAFETY: Windows kernel handles are process-wide, may be queried from any thread, and this
    // wrapper closes its uniquely owned handle exactly once during Drop.
    unsafe impl Send for OwnedHandle {}
    // SAFETY: the retained handles are used only by immutable Win32 query APIs; CloseHandle runs
    // only after all shared owners of the enclosing ledger have been dropped.
    unsafe impl Sync for OwnedHandle {}

    impl Drop for OwnedHandle {
        fn drop(&mut self) {
            // SAFETY: Win32 returned this uniquely owned live token or file handle.
            unsafe { CloseHandle(self.0) };
        }
    }

    #[cfg(test)]
    mod tests {
        use std::fs;

        use super::{handle_identity, open_no_share_delete};

        #[test]
        fn retained_handles_block_file_and_directory_namespace_swaps() {
            let root = tempfile::tempdir().expect("temporary directory is created");
            let file_path = root.path().join("ledger.sqlite3");
            let moved_file_path = root.path().join("moved.sqlite3");
            fs::write(&file_path, []).expect("test file is created");
            let file_handle =
                open_no_share_delete(&file_path, false).expect("file handle is retained");
            handle_identity(&file_handle, false).expect("file identity is available");
            assert!(
                fs::rename(&file_path, &moved_file_path).is_err(),
                "retained file handle must deny rename/delete sharing"
            );

            let directory_path = root.path().join("protected");
            let moved_directory_path = root.path().join("moved-protected");
            fs::create_dir(&directory_path).expect("test directory is created");
            let directory_handle =
                open_no_share_delete(&directory_path, true).expect("directory handle is retained");
            handle_identity(&directory_handle, true).expect("directory identity is available");
            assert!(
                fs::rename(&directory_path, &moved_directory_path).is_err(),
                "retained directory handle must deny rename/delete sharing"
            );
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{
        validate_dedicated_service_sid, validate_service_acl_snapshot,
        validate_service_parent_acl_snapshot, validate_service_sidecar_acl_snapshot,
        validate_service_token_snapshot, validate_windows_ancestor_acl_snapshot, LedgerError,
        TOKEN_GROUP_DENY_ONLY_ATTRIBUTE, TOKEN_GROUP_ENABLED_ATTRIBUTE, WINDOWS_FILE_ALL_ACCESS,
        WINDOWS_SYSTEM_SID, WINDOWS_TRUSTED_INSTALLER_SID,
    };

    const SERVICE_SID: &str = "S-1-5-80-1-2-3-4-5";
    const INTERACTIVE_USER_SID: &str = "S-1-5-21-100-200-300-400";

    #[test]
    fn dedicated_service_sid_rejects_malformed_and_non_service_identities() {
        for identity in [
            "S-1-5-80-1-2-3-4",
            "S-1-5-80-01-2-3-4-5",
            WINDOWS_SYSTEM_SID,
            INTERACTIVE_USER_SID,
        ] {
            assert!(matches!(
                validate_dedicated_service_sid(identity),
                Err(LedgerError::UnsafeLedgerPath(_))
            ));
        }
    }

    #[test]
    fn token_snapshot_requires_one_enabled_non_deny_only_service_group() {
        let valid_group = [(SERVICE_SID.to_owned(), TOKEN_GROUP_ENABLED_ATTRIBUTE)];
        assert!(
            validate_service_token_snapshot(SERVICE_SID, INTERACTIVE_USER_SID, &valid_group)
                .is_ok()
        );

        let negative_groups = [
            Vec::new(),
            vec![(SERVICE_SID.to_owned(), 0)],
            vec![(
                SERVICE_SID.to_owned(),
                TOKEN_GROUP_ENABLED_ATTRIBUTE | TOKEN_GROUP_DENY_ONLY_ATTRIBUTE,
            )],
            vec![
                (SERVICE_SID.to_owned(), TOKEN_GROUP_ENABLED_ATTRIBUTE),
                (SERVICE_SID.to_owned(), TOKEN_GROUP_ENABLED_ATTRIBUTE),
            ],
        ];
        for groups in negative_groups {
            assert!(matches!(
                validate_service_token_snapshot(SERVICE_SID, INTERACTIVE_USER_SID, &groups),
                Err(LedgerError::UnsafeLedgerPath(_))
            ));
        }
    }

    #[test]
    fn token_snapshot_rejects_service_sid_as_token_user() {
        assert!(matches!(
            validate_service_token_snapshot(
                SERVICE_SID,
                SERVICE_SID,
                &[(SERVICE_SID.to_owned(), TOKEN_GROUP_ENABLED_ATTRIBUTE)],
            ),
            Err(LedgerError::UnsafeLedgerPath(_))
        ));
    }

    #[test]
    fn acl_snapshot_accepts_only_distinct_service_and_system_entries() {
        let exact_acl = [SERVICE_SID.to_owned(), WINDOWS_SYSTEM_SID.to_owned()];
        assert!(validate_service_acl_snapshot(SERVICE_SID, SERVICE_SID, &exact_acl).is_ok());
        assert!(validate_service_acl_snapshot(SERVICE_SID, WINDOWS_SYSTEM_SID, &exact_acl).is_ok());
    }

    #[test]
    fn acl_snapshot_rejects_token_user_duplicates_missing_and_extra_entries() {
        let negative_acls = [
            vec![
                INTERACTIVE_USER_SID.to_owned(),
                WINDOWS_SYSTEM_SID.to_owned(),
            ],
            vec![WINDOWS_SYSTEM_SID.to_owned(), WINDOWS_SYSTEM_SID.to_owned()],
            vec![WINDOWS_SYSTEM_SID.to_owned()],
            vec![
                SERVICE_SID.to_owned(),
                WINDOWS_SYSTEM_SID.to_owned(),
                INTERACTIVE_USER_SID.to_owned(),
            ],
        ];
        for acl in negative_acls {
            assert!(matches!(
                validate_service_acl_snapshot(SERVICE_SID, WINDOWS_SYSTEM_SID, &acl),
                Err(LedgerError::UnsafeLedgerPath(_))
            ));
        }
    }

    #[test]
    fn acl_snapshot_rejects_interactive_owner() {
        assert!(matches!(
            validate_service_acl_snapshot(
                SERVICE_SID,
                INTERACTIVE_USER_SID,
                &[SERVICE_SID.to_owned(), WINDOWS_SYSTEM_SID.to_owned()],
            ),
            Err(LedgerError::UnsafeLedgerPath(_))
        ));
    }

    #[test]
    fn parent_acl_requires_explicit_object_and_container_inheritance() {
        let valid_flags = 0x01 | 0x02;
        let valid = [
            (SERVICE_SID.to_owned(), valid_flags),
            (WINDOWS_SYSTEM_SID.to_owned(), valid_flags),
        ];
        assert!(
            validate_service_parent_acl_snapshot(SERVICE_SID, WINDOWS_SYSTEM_SID, &valid).is_ok()
        );

        for invalid_flags in [
            0,
            0x01,
            0x02,
            valid_flags | 0x04,
            valid_flags | 0x08,
            valid_flags | 0x10,
            valid_flags | 0x40,
            valid_flags | 0x80,
        ] {
            let invalid = [
                (SERVICE_SID.to_owned(), invalid_flags),
                (WINDOWS_SYSTEM_SID.to_owned(), valid_flags),
            ];
            assert!(matches!(
                validate_service_parent_acl_snapshot(SERVICE_SID, WINDOWS_SYSTEM_SID, &invalid,),
                Err(LedgerError::UnsafeLedgerPath(_))
            ));
        }
    }

    #[test]
    fn sidecar_acl_requires_inherited_exact_full_control_identities() {
        let inherited = 0x10;
        let valid = [
            (SERVICE_SID.to_owned(), WINDOWS_FILE_ALL_ACCESS, inherited),
            (
                WINDOWS_SYSTEM_SID.to_owned(),
                WINDOWS_FILE_ALL_ACCESS,
                inherited,
            ),
        ];
        assert!(
            validate_service_sidecar_acl_snapshot(SERVICE_SID, WINDOWS_SYSTEM_SID, &valid).is_ok()
        );

        for invalid in [
            [
                (SERVICE_SID.to_owned(), WINDOWS_FILE_ALL_ACCESS, 0),
                (
                    WINDOWS_SYSTEM_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited,
                ),
            ],
            [
                (
                    SERVICE_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited | 0x08,
                ),
                (
                    WINDOWS_SYSTEM_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited,
                ),
            ],
            [
                (
                    SERVICE_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited | 0x01,
                ),
                (
                    WINDOWS_SYSTEM_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited,
                ),
            ],
            [
                (
                    SERVICE_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS - 1,
                    inherited,
                ),
                (
                    WINDOWS_SYSTEM_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited,
                ),
            ],
            [
                (
                    SERVICE_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS | 0x8000_0000,
                    inherited,
                ),
                (
                    WINDOWS_SYSTEM_SID.to_owned(),
                    WINDOWS_FILE_ALL_ACCESS,
                    inherited,
                ),
            ],
        ] {
            assert!(matches!(
                validate_service_sidecar_acl_snapshot(SERVICE_SID, WINDOWS_SYSTEM_SID, &invalid,),
                Err(LedgerError::UnsafeLedgerPath(_))
            ));
        }
    }

    #[test]
    fn windows_ancestor_acl_rejects_untrusted_mutation_rights() {
        for mask in [
            0x0001_0000,
            0x0004_0000,
            0x0008_0000,
            0x0000_0040,
            0x0000_0002,
            0x4000_0000,
        ] {
            assert!(matches!(
                validate_windows_ancestor_acl_snapshot(
                    SERVICE_SID,
                    WINDOWS_SYSTEM_SID,
                    &[(INTERACTIVE_USER_SID.to_owned(), mask, false)],
                ),
                Err(LedgerError::UnsafeLedgerPath(_))
            ));
        }
    }

    #[test]
    fn windows_ancestor_acl_rejects_untrusted_owner() {
        assert!(matches!(
            validate_windows_ancestor_acl_snapshot(
                SERVICE_SID,
                INTERACTIVE_USER_SID,
                &[(WINDOWS_SYSTEM_SID.to_owned(), u32::MAX, false)],
            ),
            Err(LedgerError::UnsafeLedgerPath(_))
        ));
    }

    #[test]
    fn windows_ancestor_acl_accepts_default_root_shaped_nonreplacement_rights() {
        assert!(validate_windows_ancestor_acl_snapshot(
            SERVICE_SID,
            WINDOWS_TRUSTED_INSTALLER_SID,
            &[
                (INTERACTIVE_USER_SID.to_owned(), 0x0000_0004, false),
                (INTERACTIVE_USER_SID.to_owned(), 0x000d_013f, true),
                (WINDOWS_SYSTEM_SID.to_owned(), u32::MAX, false),
                (SERVICE_SID.to_owned(), u32::MAX, false),
            ],
        )
        .is_ok());
    }
}
