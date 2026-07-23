use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use rusqlite::Connection;
use std::{
    fs,
    sync::{Arc, Barrier},
};

use amw_engine::receipt::{
    absent_sha256, attempt_key, canonical_receipt_bytes, original_messages_sha256,
    system_messages_sha256, AbsentDigestField, AttemptIdentity, Digest32, EvalReceiptClaims,
    KeyRotationPredecessor, LedgerError, ReceiptLedger, ReceiptSigner, SignedEvalReceipt,
    SignerError, SignerIdentity, SignerProvider, SignerTrust, SoftwareTestSigner,
};

const TEST_SERVICE_SID: &str = "S-1-5-80-1-2-3-4-5";

fn digest(label: &str) -> Digest32 {
    Digest32::sha256(label.as_bytes())
}

fn test_signer() -> SoftwareTestSigner {
    SoftwareTestSigner::from_secret_bytes([7_u8; 32], 3).expect("fixed test scalar is valid")
}

struct ProductionTestSigner {
    software: SoftwareTestSigner,
    identity: SignerIdentity,
}

impl ProductionTestSigner {
    fn new(
        secret: [u8; 32],
        key_epoch: u64,
        anchor_sha256: Digest32,
        authority_pin_sha256: Digest32,
    ) -> Self {
        let software = SoftwareTestSigner::from_secret_bytes(secret, key_epoch)
            .expect("fixed production-test scalar is valid");
        let software_identity = software.identity();
        let identity = SignerIdentity {
            key_id: software_identity.key_id,
            key_epoch,
            provider: SignerProvider::WindowsCngMachine,
            trust: SignerTrust::ProductionProtected,
            public_key_spki_der: software_identity.public_key_spki_der.clone(),
            anchor_sha256: Some(anchor_sha256),
            authority_pin_sha256: Some(authority_pin_sha256),
        };
        Self { software, identity }
    }
}

impl ReceiptSigner for ProductionTestSigner {
    fn identity(&self) -> &SignerIdentity {
        &self.identity
    }

    fn sign_canonical(&self, canonical: &[u8]) -> Result<[u8; 64], SignerError> {
        self.software.sign_canonical(canonical)
    }
}

fn fixture_claims(signer: &dyn ReceiptSigner) -> EvalReceiptClaims {
    let identity = AttemptIdentity {
        installation_id: "install-001".to_owned(),
        run_id: "run-001".to_owned(),
        suite_id: "suite-001".to_owned(),
        case_id: "case-001".to_owned(),
        ordinal: 7,
    };
    let messages = vec![
        ("system".to_owned(), "Be exact.\nNo flourish.".to_owned()),
        ("user".to_owned(), "What is 2 + 2?".to_owned()),
    ];
    EvalReceiptClaims {
        schema_version: 1,
        installation_id: identity.installation_id.clone(),
        anchor_sha256: digest("anchor"),
        key_id: signer.identity().key_id,
        key_epoch: signer.identity().key_epoch,
        engine_release: "0.1.0".to_owned(),
        source_commit: "0123456789abcdef0123456789abcdef01234567".to_owned(),
        libllama_revision: "86a9c79f866799eb0e7e89c03578ccfbcc5d808e".to_owned(),
        release_manifest_sha256: digest("release-manifest"),
        engine_binary_sha256: digest("engine-binary"),
        engine_instance_id: "engine-instance-001".to_owned(),
        principal_id: "local-supervisor".to_owned(),
        request_id: "request-001".to_owned(),
        trace_id: "trace-001".to_owned(),
        endpoint: "/v1/chat/completions".to_owned(),
        run_id: identity.run_id.clone(),
        suite_id: identity.suite_id.clone(),
        suite_revision_sha256: digest("suite-revision"),
        case_id: identity.case_id.clone(),
        ordinal: identity.ordinal,
        attempt_key: attempt_key(&identity).expect("fixture attempt identity is valid"),
        eval_slot: 2,
        seed: 4_242,
        case_spec_sha256: digest("case-spec"),
        model_id: "model/example-7b".to_owned(),
        model_sha256: digest("model"),
        adapter_set_sha256: absent_sha256(AbsentDigestField::AdapterSet),
        template_sha256: digest("template"),
        system_messages_sha256: system_messages_sha256(&messages)
            .expect("fixture messages are valid"),
        grammar_sha256: absent_sha256(AbsentDigestField::Grammar),
        sampler_sha256: digest("sampler-f32-bits"),
        generation_control_sha256: digest("generation-control"),
        original_messages_sha256: original_messages_sha256(&messages)
            .expect("fixture messages are valid"),
        rendered_prompt_sha256: digest("rendered-prompt-utf8"),
        output_sha256: digest("4"),
        prompt_tokens: 18,
        completion_tokens: 1,
        finish_reason: "stop".to_owned(),
    }
}

fn signed_fixture() -> SignedEvalReceipt {
    let signer = test_signer();
    let claims = fixture_claims(&signer);
    let canonical = canonical_receipt_bytes(&claims).expect("fixture claims are canonical");
    let signature = signer
        .sign_canonical(&canonical)
        .expect("software test signer signs fixture");
    SignedEvalReceipt::from_signature(claims, signer.identity(), signature)
        .expect("fixture receipt verifies")
}

#[test]
fn golden_vector_is_exact_and_cross_language_stable() {
    let signer = test_signer();
    let claims = fixture_claims(&signer);
    let canonical = canonical_receipt_bytes(&claims).expect("fixture claims are canonical");
    assert_eq!(
        &canonical[..41],
        b"AMW\0engine-eval-terminal-receipt\0\0\x01\0\0\0\x02\0\x01"
    );
    assert_eq!(
        claims.attempt_key.to_lower_hex(),
        "c66750787fc387acc661f98c3720bdb416e95007b533c04ff6428fd96aa5dccc"
    );
    assert_eq!(
        absent_sha256(AbsentDigestField::AdapterSet).to_lower_hex(),
        "bc31fc8227bdbb94530fe66419897e1f741f87a1957a2b25ff3820215538bf92"
    );
    assert_eq!(
        Digest32::sha256(&canonical).to_lower_hex(),
        "c413adadd9215e092b1532c966a4795b4ec80948236865a88a2edef69831edaa"
    );

    let receipt = signed_fixture();
    assert_eq!(
        receipt.signature,
        "_QER8PT63d5nGBt7xWwwpn-Xd6UR_eQPlsQtlkgJgItDlq5L3jnjTY5kxDmdofir56jAwJxRK2rN0go0nGfNPA"
    );
    receipt.verify().expect("golden receipt verifies");

    let wire = serde_json::to_string(&receipt).expect("receipt serializes");
    let decoded: SignedEvalReceipt = serde_json::from_str(&wire).expect("receipt deserializes");
    assert_eq!(decoded, receipt);
    assert!(!wire.contains('=') && !receipt.signature.contains('='));
}

#[test]
fn verifier_rejects_tampering_padding_high_s_and_unknown_wire_fields() {
    let receipt = signed_fixture();

    let mut tampered = receipt.clone();
    tampered.claims.output_sha256 = digest("tampered-output");
    assert!(matches!(
        tampered.verify(),
        Err(SignerError::ReceiptIdMismatch)
    ));

    let mut padded = receipt.clone();
    padded.signature.push('=');
    assert!(matches!(
        padded.verify(),
        Err(SignerError::InvalidBase64Url)
    ));

    let mut high_s = receipt.clone();
    let raw = URL_SAFE_NO_PAD
        .decode(&high_s.signature)
        .expect("fixture signature is base64url");
    high_s.signature = URL_SAFE_NO_PAD.encode(to_high_s(&raw));
    assert!(matches!(high_s.verify(), Err(SignerError::HighSSignature)));

    let mut value = serde_json::to_value(&receipt).expect("receipt serializes");
    value
        .as_object_mut()
        .expect("receipt is an object")
        .insert("unexpected".to_owned(), serde_json::Value::Bool(true));
    assert!(serde_json::from_value::<SignedEvalReceipt>(value).is_err());
}

#[test]
fn software_signer_is_untrusted_and_cannot_claim_production_trust() {
    let signer = test_signer();
    assert_eq!(signer.identity().provider, SignerProvider::SoftwareTest);
    assert_eq!(signer.identity().trust, SignerTrust::UntrustedSoftwareTest);
    let mut receipt = signed_fixture();
    receipt.signer.trust = SignerTrust::ProductionProtected;
    assert!(matches!(
        receipt.verify(),
        Err(SignerError::InvalidTrustClassification)
    ));
}

#[test]
fn ledger_reservations_are_durable_unique_and_immutable() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("eval-receipts.sqlite3");
    let signer = test_signer();
    let claims = fixture_claims(&signer);
    let identity = claims.attempt_identity();
    let ledger = ReceiptLedger::open_for_test(&path).expect("ledger opens with WAL/FULL");
    let reservation = ledger
        .reserve_attempt(&claims.request_id, &identity)
        .expect("first attempt reservation succeeds");
    assert!(matches!(
        ledger.reserve_attempt(
            &claims.request_id,
            &AttemptIdentity {
                ordinal: identity.ordinal + 1,
                ..identity.clone()
            }
        ),
        Err(LedgerError::ReservationConflict)
    ));
    assert!(matches!(
        ledger.reserve_attempt("request-002", &identity),
        Err(LedgerError::ReservationConflict)
    ));

    let receipt = ledger
        .commit_terminal_receipt(&reservation, &claims, &signer)
        .expect("signed receipt commits atomically");
    assert_eq!(
        ledger
            .receipt_for_request(&claims.request_id)
            .expect("receipt lookup succeeds"),
        Some(receipt)
    );
    assert!(matches!(
        ledger.commit_terminal_receipt(&reservation, &claims, &signer),
        Err(LedgerError::ReservationUnavailable)
    ));
    ledger.readiness_check().expect("committed ledger is ready");
    drop(ledger);

    let reopened = ReceiptLedger::open_for_test(&path).expect("committed ledger survives restart");
    assert!(matches!(
        reopened.reserve_attempt("request-003", &identity),
        Err(LedgerError::ReservationConflict)
    ));
    drop(reopened);

    let raw = Connection::open(&path).expect("test can inspect ledger directly");
    assert!(raw
        .execute(
            "DELETE FROM eval_receipt_attempts WHERE request_id = ?1",
            [&claims.request_id]
        )
        .is_err());
}

#[test]
fn signer_failure_consumes_tombstone_and_malformed_schema_fails_readiness() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("eval-receipts.sqlite3");
    let signer = test_signer();
    let claims = fixture_claims(&signer);
    let identity = claims.attempt_identity();
    let ledger = ReceiptLedger::open_for_test(&path).expect("ledger opens");
    let reservation = ledger
        .reserve_attempt(&claims.request_id, &identity)
        .expect("attempt is reserved");
    let failing = FailingSigner {
        identity: signer.identity().clone(),
    };
    assert!(matches!(
        ledger.commit_terminal_receipt(&reservation, &claims, &failing),
        Err(LedgerError::Signer(SignerError::ProtectedProvider(_)))
    ));
    assert_eq!(
        ledger
            .receipt_for_request(&claims.request_id)
            .expect("reserved tombstone is readable"),
        None
    );
    drop(ledger);

    let reopened =
        ReceiptLedger::open_for_test(&path).expect("reserved tombstone survives restart");
    assert!(matches!(
        reopened.reserve_attempt("request-004", &identity),
        Err(LedgerError::ReservationConflict)
    ));
    drop(reopened);

    let raw = Connection::open(&path).expect("test can corrupt schema directly");
    raw.execute_batch("DROP TRIGGER eval_receipt_no_delete")
        .expect("test removes required trigger");
    drop(raw);
    assert!(matches!(
        ReceiptLedger::open_for_test(&path),
        Err(LedgerError::MalformedLedger("required schema object"))
    ));
}

#[test]
fn readiness_rejects_same_named_invalid_unique_indexes() {
    let non_unique_directory = tempfile::tempdir().expect("temporary directory is created");
    let non_unique_path = non_unique_directory.path().join("non-unique.sqlite3");
    drop(ReceiptLedger::open_for_test(&non_unique_path).expect("clean ledger opens"));
    let non_unique = Connection::open(&non_unique_path).expect("test opens ledger directly");
    non_unique
        .execute_batch(
            "DROP INDEX uq_eval_receipt_request_id;
             CREATE INDEX uq_eval_receipt_request_id
                 ON eval_receipt_attempts(request_id);",
        )
        .expect("test replaces unique index with same-named non-unique index");
    drop(non_unique);
    assert!(matches!(
        ReceiptLedger::open_for_test(&non_unique_path),
        Err(LedgerError::MalformedLedger(
            "schema definition fingerprint"
        ))
    ));

    let wrong_order_directory = tempfile::tempdir().expect("temporary directory is created");
    let wrong_order_path = wrong_order_directory.path().join("wrong-order.sqlite3");
    drop(ReceiptLedger::open_for_test(&wrong_order_path).expect("clean ledger opens"));
    let wrong_order = Connection::open(&wrong_order_path).expect("test opens ledger directly");
    wrong_order
        .execute_batch(
            "DROP INDEX uq_eval_receipt_attempt_identity;
             CREATE UNIQUE INDEX uq_eval_receipt_attempt_identity
                 ON eval_receipt_attempts(
                     installation_id, run_id, suite_id, ordinal, case_id
                 );",
        )
        .expect("test replaces unique index with wrong ordered columns");
    drop(wrong_order);
    assert!(matches!(
        ReceiptLedger::open_for_test(&wrong_order_path),
        Err(LedgerError::MalformedLedger(
            "schema definition fingerprint"
        ))
    ));
}

#[test]
fn repeated_readiness_detects_post_start_schema_replacement() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("post-start-replacement.sqlite3");
    let ledger = ReceiptLedger::open_for_test(&path).expect("clean ledger opens");
    ledger.readiness_check().expect("initial readiness passes");
    let raw = Connection::open(&path).expect("test opens live ledger directly");
    raw.execute_batch(
        "DROP INDEX uq_eval_receipt_attempt_key;
         CREATE INDEX uq_eval_receipt_attempt_key ON eval_receipt_attempts(attempt_key);",
    )
    .expect("test replaces unique index after startup");
    drop(raw);
    assert!(matches!(
        ledger.readiness_check(),
        Err(LedgerError::MalformedLedger(
            "schema definition fingerprint"
        ))
    ));
}

#[test]
fn readiness_rejects_same_named_no_op_triggers() {
    let replacements = [
        (
            "eval_receipt_no_delete",
            "CREATE TRIGGER eval_receipt_no_delete
             BEFORE DELETE ON eval_receipt_attempts BEGIN SELECT 1; END;",
        ),
        (
            "eval_receipt_committed_immutable",
            "CREATE TRIGGER eval_receipt_committed_immutable
             BEFORE UPDATE ON eval_receipt_attempts
             WHEN OLD.state = 'committed' BEGIN SELECT 1; END;",
        ),
        (
            "eval_receipt_reservation_identity_immutable",
            "CREATE TRIGGER eval_receipt_reservation_identity_immutable
             BEFORE UPDATE ON eval_receipt_attempts
             WHEN OLD.state = 'reserved' BEGIN SELECT 1; END;",
        ),
        (
            "eval_receipt_authority_no_delete",
            "CREATE TRIGGER eval_receipt_authority_no_delete
             BEFORE DELETE ON eval_receipt_authority BEGIN SELECT 1; END;",
        ),
        (
            "eval_receipt_authority_immutable",
            "CREATE TRIGGER eval_receipt_authority_immutable
             BEFORE UPDATE ON eval_receipt_authority BEGIN SELECT 1; END;",
        ),
        (
            "eval_receipt_key_history_no_delete",
            "CREATE TRIGGER eval_receipt_key_history_no_delete
             BEFORE DELETE ON eval_receipt_key_history BEGIN SELECT 1; END;",
        ),
        (
            "eval_receipt_key_history_immutable",
            "CREATE TRIGGER eval_receipt_key_history_immutable
             BEFORE UPDATE ON eval_receipt_key_history BEGIN SELECT 1; END;",
        ),
    ];
    for (trigger_name, replacement) in replacements {
        let directory = tempfile::tempdir().expect("temporary directory is created");
        let path = directory.path().join(format!("{trigger_name}.sqlite3"));
        drop(ReceiptLedger::open_for_test(&path).expect("clean ledger opens"));
        let raw = Connection::open(&path).expect("test opens ledger directly");
        raw.execute_batch(&format!("DROP TRIGGER {trigger_name}; {replacement}"))
            .expect("test replaces enforcing trigger with same-named no-op");
        drop(raw);
        assert!(matches!(
            ReceiptLedger::open_for_test(&path),
            Err(LedgerError::MalformedLedger(
                "schema definition fingerprint"
            ))
        ));
    }
}

#[test]
fn readiness_rejects_trigger_that_only_blocks_legacy_probe_sentinels() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("selective-trigger.sqlite3");
    drop(ReceiptLedger::open_for_test(&path).expect("clean ledger opens"));
    let raw = Connection::open(&path).expect("test opens ledger directly");
    raw.execute_batch(
        "DROP TRIGGER eval_receipt_no_delete;
         CREATE TRIGGER eval_receipt_no_delete
         BEFORE DELETE ON eval_receipt_attempts
         WHEN OLD.request_id LIKE 'readiness-probe-%'
         BEGIN
             SELECT RAISE(ABORT, 'selective fake immutability');
         END;",
    )
    .expect("test installs sentinel-aware malicious trigger");
    drop(raw);
    assert!(matches!(
        ReceiptLedger::open_for_test(&path),
        Err(LedgerError::MalformedLedger(
            "schema definition fingerprint"
        ))
    ));
}

#[test]
fn readiness_probe_does_not_collide_with_legacy_sentinel_reservation() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("sentinel-collision.sqlite3");
    let ledger = ReceiptLedger::open_for_test(&path).expect("clean ledger opens");
    let identity = AttemptIdentity {
        installation_id: "readiness-install".to_owned(),
        run_id: "readiness-run".to_owned(),
        suite_id: "readiness-suite".to_owned(),
        case_id: "readiness-case".to_owned(),
        ordinal: u32::MAX,
    };
    ledger
        .reserve_attempt("readiness-probe-reserved", &identity)
        .expect("legacy sentinel-shaped reservation is legitimate data");
    ledger
        .readiness_check()
        .expect("randomized probe cannot collide with legitimate sentinel-shaped data");
}

#[test]
fn ledger_authority_binding_is_singleton_and_exact() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("authority-binding.sqlite3");
    let ledger = ReceiptLedger::open_for_test(&path).expect("test ledger opens");
    let anchor = digest("anchor");
    let authority_pin = digest("authority-pin");
    let key_id = digest("key-epoch-3");
    ledger
        .bind_authority("install-001", authority_pin)
        .expect("first authority binding commits");
    ledger
        .bind_authority("install-001", authority_pin)
        .expect("exact binding is idempotent");
    assert!(matches!(
        ledger.bind_authority("install-002", authority_pin),
        Err(LedgerError::AuthorityBindingMismatch)
    ));
    assert!(matches!(
        ledger.bind_authority("install-001", digest("other-authority")),
        Err(LedgerError::AuthorityBindingMismatch)
    ));
    ledger
        .register_key_anchor(3, key_id, anchor, None)
        .expect("first key anchor is registered");
    ledger
        .register_key_anchor(3, key_id, anchor, None)
        .expect("exact key anchor is idempotent");
    assert!(matches!(
        ledger.register_key_anchor(4, digest("key-epoch-4"), digest("anchor-4"), None),
        Err(LedgerError::RotationPredecessorMismatch)
    ));
    let rotated_key = digest("key-epoch-4");
    let rotated_anchor = digest("anchor-4");
    let predecessor = KeyRotationPredecessor {
        key_epoch: 3,
        key_id,
        anchor_sha256: anchor,
    };
    ledger
        .register_key_anchor(4, rotated_key, rotated_anchor, Some(predecessor))
        .expect("rotation appends with exact predecessor");
    assert!(matches!(
        ledger.register_key_anchor(
            5,
            digest("key-epoch-5"),
            digest("anchor-5"),
            Some(KeyRotationPredecessor {
                key_epoch: 3,
                key_id: rotated_key,
                anchor_sha256: rotated_anchor,
            })
        ),
        Err(LedgerError::RotationPredecessorMismatch)
    ));
    assert!(matches!(
        ledger.register_key_anchor(
            5,
            digest("key-epoch-5"),
            digest("anchor-5"),
            Some(KeyRotationPredecessor {
                key_epoch: 4,
                key_id: rotated_key,
                anchor_sha256: digest("wrong-predecessor-anchor"),
            })
        ),
        Err(LedgerError::RotationPredecessorMismatch)
    ));
    ledger
        .register_key_anchor(
            5,
            digest("key-epoch-5"),
            digest("anchor-5"),
            Some(KeyRotationPredecessor {
                key_epoch: 4,
                key_id: rotated_key,
                anchor_sha256: rotated_anchor,
            }),
        )
        .expect("rotation persists the full authenticated predecessor tuple");
    ledger
        .readiness_check()
        .expect("bound ledger remains ready");
}

#[test]
fn retired_key_cannot_bootstrap_or_commit_after_rotation() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("retired-key-rollback.sqlite3");
    let authority_pin = digest("rollback-authority-pin");
    let retired_anchor = digest("anchor");
    let latest_anchor = digest("anchor-epoch-2");
    let retired_signer = ProductionTestSigner::new([11_u8; 32], 1, retired_anchor, authority_pin);
    let latest_signer = ProductionTestSigner::new([12_u8; 32], 2, latest_anchor, authority_pin);
    let predecessor = KeyRotationPredecessor {
        key_epoch: 1,
        key_id: retired_signer.identity().key_id,
        anchor_sha256: retired_anchor,
    };

    let retired_authority = ReceiptLedger::open_for_test(&path).expect("test ledger opens");
    retired_authority
        .bind_authority("install-001", authority_pin)
        .expect("authority binding commits");
    retired_authority
        .register_key_anchor(1, retired_signer.identity().key_id, retired_anchor, None)
        .expect("initial anchor registers");
    let rotator = ReceiptLedger::open_for_test(&path).expect("rotation connection opens");
    rotator
        .bind_authority("install-001", authority_pin)
        .expect("rotation connection authenticates authority");
    rotator
        .register_key_anchor(
            2,
            latest_signer.identity().key_id,
            latest_anchor,
            Some(predecessor),
        )
        .expect("rotation registers");

    let retired_claims = fixture_claims(&retired_signer);
    let retired_reservation = retired_authority
        .reserve_attempt(
            &retired_claims.request_id,
            &retired_claims.attempt_identity(),
        )
        .expect("retired attempt reserves before authority rejection");
    assert!(matches!(
        retired_authority.commit_terminal_receipt(
            &retired_reservation,
            &retired_claims,
            &retired_signer,
        ),
        Err(LedgerError::AuthorityBindingMismatch)
    ));
    drop(rotator);
    drop(retired_authority);

    let reopened = ReceiptLedger::open_for_test(&path).expect("rotated ledger reopens");
    reopened
        .bind_authority("install-001", authority_pin)
        .expect("exact authority remains idempotent");
    assert!(matches!(
        reopened.register_key_anchor(1, retired_signer.identity().key_id, retired_anchor, None,),
        Err(LedgerError::AuthorityBindingMismatch)
    ));
    reopened
        .register_key_anchor(
            2,
            latest_signer.identity().key_id,
            latest_anchor,
            Some(predecessor),
        )
        .expect("latest anchor remains idempotent");

    let mut latest_claims = fixture_claims(&latest_signer);
    latest_claims.anchor_sha256 = latest_anchor;
    latest_claims.request_id = "request-epoch-2".to_owned();
    latest_claims.ordinal += 1;
    latest_claims.attempt_key =
        attempt_key(&latest_claims.attempt_identity()).expect("latest attempt identity is valid");
    let latest_reservation = reopened
        .reserve_attempt(&latest_claims.request_id, &latest_claims.attempt_identity())
        .expect("latest attempt reserves");
    reopened
        .commit_terminal_receipt(&latest_reservation, &latest_claims, &latest_signer)
        .expect("latest key remains authorized to commit");
    reopened
        .readiness_check()
        .expect("historical key remains valid for verification only");
}

#[test]
fn readiness_rejects_self_valid_receipt_from_foreign_key_history() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("foreign-key.sqlite3");
    let ledger = ReceiptLedger::open_for_test(&path).expect("test ledger opens");
    let software = test_signer();
    let authority_pin = digest("authority-pin");
    let foreign_anchor = digest("foreign-anchor");
    let forged_identity = SignerIdentity {
        key_id: software.identity().key_id,
        key_epoch: software.identity().key_epoch,
        provider: SignerProvider::WindowsCngMachine,
        trust: SignerTrust::ProductionProtected,
        public_key_spki_der: software.identity().public_key_spki_der.clone(),
        anchor_sha256: Some(foreign_anchor),
        authority_pin_sha256: Some(authority_pin),
    };
    let mut claims = fixture_claims(&software);
    claims.anchor_sha256 = foreign_anchor;
    let canonical = canonical_receipt_bytes(&claims).expect("forged claims remain canonical");
    let signature = software
        .sign_canonical(&canonical)
        .expect("foreign software key signs self-valid receipt");
    let forged = SignedEvalReceipt::from_signature(claims.clone(), &forged_identity, signature)
        .expect("foreign receipt is cryptographically self-valid");
    let reservation = ledger
        .reserve_attempt(&claims.request_id, &claims.attempt_identity())
        .expect("attempt is reserved");
    ledger
        .bind_authority(&claims.installation_id, authority_pin)
        .expect("stable authority binds");
    ledger
        .register_key_anchor(
            claims.key_epoch,
            digest("legitimate-key"),
            digest("legitimate-anchor"),
            None,
        )
        .expect("legitimate key history binds");

    let raw = Connection::open(&path).expect("test opens ledger directly");
    raw.execute(
        "UPDATE eval_receipt_attempts
         SET state = 'committed', receipt_id = ?1, canonical_receipt = ?2,
             signed_receipt_json = ?3
         WHERE request_id = ?4",
        rusqlite::params![
            forged.receipt_id.as_bytes().as_slice(),
            canonical,
            serde_json::to_string(&forged).expect("forged receipt serializes"),
            reservation.request_id,
        ],
    )
    .expect("malicious state injects self-valid foreign receipt");
    drop(raw);
    assert!(matches!(
        ledger.readiness_check(),
        Err(LedgerError::MalformedLedger("committed receipt authority"))
    ));
}

#[test]
fn production_open_rejects_relative_and_insecure_precreated_paths() {
    assert!(matches!(
        ReceiptLedger::open("relative-ledger.sqlite3", TEST_SERVICE_SID),
        Err(LedgerError::UnsafeLedgerPath(_))
    ));

    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("insecure-precreated.sqlite3");
    fs::write(&path, []).expect("test precreates empty ledger state");
    let mut permissions = fs::metadata(&path)
        .expect("test ledger metadata is readable")
        .permissions();
    permissions.set_readonly(true);
    fs::set_permissions(&path, permissions).expect("test makes ledger state non-service writable");
    assert!(matches!(
        ReceiptLedger::open(&path, TEST_SERVICE_SID),
        Err(LedgerError::UnsafeLedgerPath(_))
    ));
}

#[test]
fn production_open_rejects_symlink_or_reparse_traversal() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let target = directory.path().join("target.sqlite3");
    fs::write(&target, []).expect("test target is created");
    let link = directory.path().join("linked.sqlite3");
    if let Err(error) = create_file_symlink(&target, &link) {
        assert!(
            error.kind() == std::io::ErrorKind::PermissionDenied
                || error.raw_os_error() == Some(1_314),
            "unexpected symlink creation failure: {error}"
        );
        return;
    }
    assert!(matches!(
        ReceiptLedger::open(&link, TEST_SERVICE_SID),
        Err(LedgerError::UnsafeLedgerPath(_))
    ));
}

#[cfg(target_os = "linux")]
#[test]
fn linux_protected_open_rejects_writable_ancestor_chain() {
    use std::os::unix::fs::PermissionsExt as _;

    let root = linux_protected_test_root();
    let writable_ancestor = root.path().join("writable-ancestor");
    fs::create_dir(&writable_ancestor).expect("writable ancestor is created");
    fs::set_permissions(&writable_ancestor, fs::Permissions::from_mode(0o777))
        .expect("ancestor is made attacker-writable");
    let protected_parent = writable_ancestor.join("protected");
    fs::create_dir(&protected_parent).expect("protected child is created");
    fs::set_permissions(&protected_parent, fs::Permissions::from_mode(0o700))
        .expect("protected child uses mode 0700");
    let path = protected_parent.join("ledger.sqlite3");
    precreate_linux_ledger(&path);

    let message = linux_unsafe_path_message(ReceiptLedger::open(&path, TEST_SERVICE_SID))
        .expect("unsafe ancestor must produce the expected path-security rejection");
    assert!(
        message.contains("ancestor directory is group- or other-writable"),
        "unexpected rejection: {message}"
    );
}

#[cfg(target_os = "linux")]
#[test]
fn linux_readiness_rejects_file_inode_rollback_swap() {
    let root = linux_protected_test_root();
    let active_path = root.path().join("active.sqlite3");
    let rollback_path = root.path().join("rollback.sqlite3");
    let displaced_path = root.path().join("displaced.sqlite3");
    precreate_linux_ledger(&active_path);
    precreate_linux_ledger(&rollback_path);
    drop(
        ReceiptLedger::open(&rollback_path, TEST_SERVICE_SID)
            .expect("rollback ledger is initialized"),
    );
    let ledger =
        ReceiptLedger::open(&active_path, TEST_SERVICE_SID).expect("active ledger opens securely");

    fs::rename(&active_path, &displaced_path).expect("active inode is displaced");
    fs::rename(&rollback_path, &active_path).expect("old ledger inode replaces active path");
    assert!(matches!(
        ledger.readiness_check(),
        Err(LedgerError::UnsafeLedgerPath(_))
    ));
}

#[cfg(target_os = "linux")]
#[test]
fn linux_readiness_rejects_protected_directory_swap() {
    use std::os::unix::fs::PermissionsExt as _;

    let root = linux_protected_test_root();
    let active_parent = root.path().join("active");
    let displaced_parent = root.path().join("displaced");
    fs::create_dir(&active_parent).expect("active parent is created");
    fs::set_permissions(&active_parent, fs::Permissions::from_mode(0o700))
        .expect("active parent uses mode 0700");
    let active_path = active_parent.join("ledger.sqlite3");
    precreate_linux_ledger(&active_path);
    let ledger =
        ReceiptLedger::open(&active_path, TEST_SERVICE_SID).expect("active ledger opens securely");

    fs::rename(&active_parent, &displaced_parent).expect("protected directory is displaced");
    fs::create_dir(&active_parent).expect("replacement parent is created");
    fs::set_permissions(&active_parent, fs::Permissions::from_mode(0o700))
        .expect("replacement parent uses mode 0700");
    precreate_linux_ledger(&active_path);
    assert!(matches!(
        ledger.readiness_check(),
        Err(LedgerError::UnsafeLedgerPath(_))
    ));
}

#[test]
fn concurrent_reservations_have_exactly_one_winner() {
    let directory = tempfile::tempdir().expect("temporary directory is created");
    let path = directory.path().join("eval-receipts.sqlite3");
    let ledger = Arc::new(ReceiptLedger::open_for_test(&path).expect("ledger opens"));
    let identity = Arc::new(AttemptIdentity {
        installation_id: "install-race".to_owned(),
        run_id: "run-race".to_owned(),
        suite_id: "suite-race".to_owned(),
        case_id: "case-race".to_owned(),
        ordinal: 0,
    });
    let barrier = Arc::new(Barrier::new(8));
    let handles = (0..8)
        .map(|index| {
            let ledger = Arc::clone(&ledger);
            let identity = Arc::clone(&identity);
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                barrier.wait();
                ledger.reserve_attempt(&format!("request-race-{index}"), &identity)
            })
        })
        .collect::<Vec<_>>();
    let results = handles
        .into_iter()
        .map(|handle| handle.join().expect("reservation thread does not panic"))
        .collect::<Vec<_>>();
    assert_eq!(results.iter().filter(|result| result.is_ok()).count(), 1);
    assert_eq!(
        results
            .iter()
            .filter(|result| matches!(result, Err(LedgerError::ReservationConflict)))
            .count(),
        7
    );
}

#[cfg(target_os = "linux")]
fn linux_protected_test_root() -> tempfile::TempDir {
    use std::os::unix::fs::PermissionsExt as _;

    let safe_base = std::env::var_os("XDG_RUNTIME_DIR")
        .or_else(|| std::env::var_os("HOME"))
        .map(std::path::PathBuf::from)
        .filter(|path| path.is_absolute() && path.is_dir())
        .unwrap_or_else(|| std::env::current_dir().expect("test current directory is available"));
    let root = tempfile::Builder::new()
        .prefix("amw-ledger-security-")
        .tempdir_in(safe_base)
        .expect("protected test root is created below a private user directory");
    fs::set_permissions(root.path(), fs::Permissions::from_mode(0o700))
        .expect("protected test root uses mode 0700");
    root
}

#[cfg(target_os = "linux")]
fn precreate_linux_ledger(path: &std::path::Path) {
    use std::{fs::OpenOptions, os::unix::fs::PermissionsExt as _};

    OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .expect("ledger file is pre-created");
    fs::set_permissions(path, fs::Permissions::from_mode(0o600))
        .expect("ledger file uses mode 0600");
}

#[cfg(target_os = "linux")]
fn linux_unsafe_path_message(result: Result<ReceiptLedger, LedgerError>) -> Result<String, String> {
    match result {
        Err(LedgerError::UnsafeLedgerPath(message)) => Ok(message),
        Err(error) => Err(format!("expected unsafe-path rejection, got {error}")),
        Ok(_) => Err("unsafe Linux ledger path was accepted".to_owned()),
    }
}

struct FailingSigner {
    identity: amw_engine::receipt::SignerIdentity,
}

impl ReceiptSigner for FailingSigner {
    fn identity(&self) -> &amw_engine::receipt::SignerIdentity {
        &self.identity
    }

    fn sign_canonical(&self, _canonical: &[u8]) -> Result<[u8; 64], SignerError> {
        Err(SignerError::ProtectedProvider(
            "injected provider outage".to_owned(),
        ))
    }
}

fn to_high_s(signature: &[u8]) -> [u8; 64] {
    assert_eq!(signature.len(), 64);
    const ORDER: [u8; 32] = [
        0xff, 0xff, 0xff, 0xff, 0x00, 0x00, 0x00, 0x00, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
        0xff, 0xbc, 0xe6, 0xfa, 0xad, 0xa7, 0x17, 0x9e, 0x84, 0xf3, 0xb9, 0xca, 0xc2, 0xfc, 0x63,
        0x25, 0x51,
    ];
    let mut output = [0_u8; 64];
    output[..32].copy_from_slice(&signature[..32]);
    let mut borrow = 0_i16;
    for index in (0..32).rev() {
        let difference = i16::from(ORDER[index]) - i16::from(signature[index + 32]) - borrow;
        if difference < 0 {
            output[index + 32] = u8::try_from(difference + 256).expect("byte difference fits");
            borrow = 1;
        } else {
            output[index + 32] = u8::try_from(difference).expect("byte difference fits");
            borrow = 0;
        }
    }
    assert_eq!(borrow, 0);
    output
}

#[cfg(unix)]
fn create_file_symlink(target: &std::path::Path, link: &std::path::Path) -> std::io::Result<()> {
    std::os::unix::fs::symlink(target, link)
}

#[cfg(windows)]
fn create_file_symlink(target: &std::path::Path, link: &std::path::Path) -> std::io::Result<()> {
    std::os::windows::fs::symlink_file(target, link)
}
