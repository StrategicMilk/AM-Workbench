//! Durable, engine-authored evaluation receipts.
//!
//! The receipt boundary deliberately keeps signing and reservation state in the
//! Rust engine. Consumers may verify a receipt, but cannot mint one or release
//! a consumed evaluation attempt.

pub mod canonical;
pub mod ledger;
pub(crate) mod pkcs11;
pub mod signer;

pub use canonical::{
    absent_sha256, attempt_key, canonical_receipt_bytes, original_messages_sha256,
    system_messages_sha256, AbsentDigestField, AttemptIdentity, CanonicalError, Digest32,
    EvalContext, EvalReceiptClaims, CANONICALIZATION_NAME,
};
pub use ledger::{KeyRotationPredecessor, LedgerError, ReceiptLedger, ReceiptReservation};
pub use signer::{
    verify_receipt_signature, ReceiptSigner, SignedEvalReceipt, SignerError, SignerEvidence,
    SignerIdentity, SignerProvider, SignerTrust, SoftwareTestSigner, SIGNATURE_ALGORITHM,
};
pub(crate) use signer::{
    KeyExportPolicy, PlatformKeyAttestation, ProtectedKeyReference, ProtectedReceiptSigner,
    ProtectedSignerBinding, ProtectedSigningBackend,
};
