//! P-256 receipt signing and strict signature verification.

use base64::{engine::general_purpose::URL_SAFE_NO_PAD, Engine as _};
use p256::{
    ecdsa::{
        signature::{Signer as _, Verifier as _},
        Signature, SigningKey, VerifyingKey,
    },
    pkcs8::{DecodePublicKey, EncodePublicKey},
};
use serde::{Deserialize, Serialize};
use thiserror::Error;
use zeroize::Zeroize;

use super::canonical::{canonical_receipt_bytes, CanonicalError, Digest32, EvalReceiptClaims};
use super::CANONICALIZATION_NAME;

/// Signature algorithm identifier required by ADR-0174.
pub const SIGNATURE_ALGORITHM: &str = "ecdsa-p256-sha256-p1363";

/// The provider that owns a receipt-signing key.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SignerProvider {
    WindowsCngMachine,
    Tpm,
    Pkcs11,
    Hsm,
    SoftwareTest,
}

/// Whether the signer is eligible to contribute production evidence.
#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum SignerTrust {
    ProductionProtected,
    UntrustedSoftwareTest,
}

/// Immutable public identity of a receipt signer.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SignerIdentity {
    pub key_id: Digest32,
    pub key_epoch: u64,
    pub provider: SignerProvider,
    pub trust: SignerTrust,
    pub public_key_spki_der: Vec<u8>,
    pub anchor_sha256: Option<Digest32>,
    pub authority_pin_sha256: Option<Digest32>,
}

/// Non-secret signer evidence carried on the strict receipt wire envelope.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SignerEvidence {
    pub provider: SignerProvider,
    pub trust: SignerTrust,
    pub public_key_spki_der: String,
}

impl SignerEvidence {
    fn from_identity(identity: &SignerIdentity) -> Self {
        Self {
            provider: identity.provider,
            trust: identity.trust,
            public_key_spki_der: URL_SAFE_NO_PAD.encode(&identity.public_key_spki_der),
        }
    }

    fn decoded_spki(&self) -> Result<Vec<u8>, SignerError> {
        decode_base64url_no_pad(&self.public_key_spki_der)
    }
}

/// Strict, self-verifiable signed receipt envelope.
#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct SignedEvalReceipt {
    pub canonicalization: String,
    pub algorithm: String,
    pub claims: EvalReceiptClaims,
    pub receipt_id: Digest32,
    pub signature: String,
    pub signer: SignerEvidence,
}

impl SignedEvalReceipt {
    /// Constructs and validates an envelope from a signer's raw P1363 output.
    pub fn from_signature(
        claims: EvalReceiptClaims,
        identity: &SignerIdentity,
        signature: [u8; 64],
    ) -> Result<Self, SignerError> {
        validate_identity(identity)?;
        validate_claim_signer_bindings(&claims, identity)?;
        let canonical = canonical_receipt_bytes(&claims)?;
        let normalized = normalize_signature(&signature)?;
        let signature = URL_SAFE_NO_PAD.encode(normalized.to_bytes());
        let receipt = Self {
            canonicalization: CANONICALIZATION_NAME.to_owned(),
            algorithm: SIGNATURE_ALGORITHM.to_owned(),
            claims,
            receipt_id: Digest32::sha256(&canonical),
            signature,
            signer: SignerEvidence::from_identity(identity),
        };
        receipt.verify()?;
        Ok(receipt)
    }

    /// Validates envelope constants, claim bindings, receipt hash, and signature.
    pub fn verify(&self) -> Result<(), SignerError> {
        if self.canonicalization != CANONICALIZATION_NAME {
            return Err(SignerError::UnsupportedCanonicalization);
        }
        if self.algorithm != SIGNATURE_ALGORITHM {
            return Err(SignerError::UnsupportedAlgorithm);
        }
        validate_provider_trust(self.signer.provider, self.signer.trust)?;
        let canonical = canonical_receipt_bytes(&self.claims)?;
        if self.receipt_id != Digest32::sha256(&canonical) {
            return Err(SignerError::ReceiptIdMismatch);
        }
        let spki = self.signer.decoded_spki()?;
        if self.claims.key_id != Digest32::sha256(&spki) {
            return Err(SignerError::KeyIdMismatch);
        }
        verify_receipt_signature(&spki, &canonical, &self.signature)
    }
}

/// Abstraction over protected production and explicit test-only receipt signers.
pub trait ReceiptSigner: Send + Sync {
    /// Returns the signer's stable public identity and trust classification.
    fn identity(&self) -> &SignerIdentity;

    /// Signs canonical bytes as a 64-byte, low-S IEEE-P1363 signature.
    fn sign_canonical(&self, canonical: &[u8]) -> Result<[u8; 64], SignerError>;
}

/// Export policy reported by a live platform provider query.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum KeyExportPolicy {
    NonExportable,
    // A conforming platform resolver normally rejects this before signer construction, but the
    // value remains representable so negative attestations fail at this shared boundary too.
    #[allow(dead_code)]
    Exportable,
}

/// Live key facts reported by the platform provider, never by request data.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct PlatformKeyAttestation {
    pub provider: SignerProvider,
    pub key_handle_reference: String,
    pub public_key_spki_der: Vec<u8>,
    pub export_policy: KeyExportPolicy,
    pub service_identity: String,
    pub service_acl_identities: Vec<String>,
}

/// Exact independently verified anchor bindings expected from the live key.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ProtectedSignerBinding {
    pub provider: SignerProvider,
    pub key_epoch: u64,
    pub public_key_spki_der: Vec<u8>,
    pub key_handle_reference: String,
    pub service_identity: String,
    pub anchor_sha256: Digest32,
    pub authority_pin_sha256: Digest32,
}

/// Opaque reference to an attested non-exportable platform key.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ProtectedKeyReference {
    identity: SignerIdentity,
    opaque_key_reference: String,
    service_identity: String,
}

impl ProtectedKeyReference {
    /// Returns the provider-specific opaque reference without exposing key material.
    #[must_use]
    pub fn opaque_key_reference(&self) -> &str {
        &self.opaque_key_reference
    }

    /// Returns the dedicated service identity authorized to use the key.
    #[must_use]
    pub fn service_identity(&self) -> &str {
        &self.service_identity
    }
}

/// Crate-internal bridge implemented by a live OS, HSM, TPM, or PKCS#11 query.
pub(crate) trait ProtectedSigningBackend: Send + Sync {
    /// Queries the live provider's handle, SPKI, export policy, and service ACL.
    fn attest_key(&self) -> Result<PlatformKeyAttestation, SignerError>;

    /// Signs the message using the referenced non-exportable P-256 key.
    fn sign_p256_sha256_p1363(
        &self,
        key: &ProtectedKeyReference,
        message: &[u8],
    ) -> Result<[u8; 64], SignerError>;
}

/// Production-trusted signer backed only by a protected key-service reference.
pub(crate) struct ProtectedReceiptSigner<B> {
    key: ProtectedKeyReference,
    backend: B,
}

impl<B> ProtectedReceiptSigner<B>
where
    B: ProtectedSigningBackend,
{
    /// Creates production trust only after live attestation matches exact anchor bindings.
    pub(crate) fn from_attested_backend(
        binding: ProtectedSignerBinding,
        backend: B,
    ) -> Result<Self, SignerError> {
        let attestation = backend.attest_key()?;
        validate_platform_attestation(&binding, &attestation)?;
        let identity = SignerIdentity {
            key_id: Digest32::sha256(&binding.public_key_spki_der),
            key_epoch: binding.key_epoch,
            provider: binding.provider,
            trust: SignerTrust::ProductionProtected,
            public_key_spki_der: binding.public_key_spki_der,
            anchor_sha256: Some(binding.anchor_sha256),
            authority_pin_sha256: Some(binding.authority_pin_sha256),
        };
        validate_identity(&identity)?;
        Ok(Self {
            key: ProtectedKeyReference {
                identity,
                opaque_key_reference: binding.key_handle_reference,
                service_identity: attestation.service_identity,
            },
            backend,
        })
    }
}

impl<B> ReceiptSigner for ProtectedReceiptSigner<B>
where
    B: ProtectedSigningBackend,
{
    fn identity(&self) -> &SignerIdentity {
        &self.key.identity
    }

    fn sign_canonical(&self, canonical: &[u8]) -> Result<[u8; 64], SignerError> {
        let raw = self.backend.sign_p256_sha256_p1363(&self.key, canonical)?;
        let normalized = normalize_signature(&raw)?;
        let encoded = URL_SAFE_NO_PAD.encode(normalized.to_bytes());
        verify_receipt_signature(&self.key.identity.public_key_spki_der, canonical, &encoded)?;
        Ok(normalized.to_bytes().into())
    }
}

/// Explicitly untrusted deterministic software signer for contract tests only.
pub struct SoftwareTestSigner {
    signing_key: SigningKey,
    identity: SignerIdentity,
}

impl SoftwareTestSigner {
    /// Creates an untrusted test signer from exactly 32 secret scalar bytes.
    pub fn from_secret_bytes(mut secret: [u8; 32], key_epoch: u64) -> Result<Self, SignerError> {
        let signing_key = SigningKey::from_bytes((&secret).into())
            .map_err(|_| SignerError::InvalidSoftwareTestKey);
        secret.zeroize();
        let signing_key = signing_key?;
        let public_key_spki_der = signing_key
            .verifying_key()
            .to_public_key_der()
            .map_err(|_| SignerError::InvalidPublicKey)?
            .as_bytes()
            .to_vec();
        let identity = SignerIdentity {
            key_id: Digest32::sha256(&public_key_spki_der),
            key_epoch,
            provider: SignerProvider::SoftwareTest,
            trust: SignerTrust::UntrustedSoftwareTest,
            public_key_spki_der,
            anchor_sha256: None,
            authority_pin_sha256: None,
        };
        Ok(Self {
            signing_key,
            identity,
        })
    }
}

impl ReceiptSigner for SoftwareTestSigner {
    fn identity(&self) -> &SignerIdentity {
        &self.identity
    }

    fn sign_canonical(&self, canonical: &[u8]) -> Result<[u8; 64], SignerError> {
        let signature: Signature = self.signing_key.sign(canonical);
        let normalized = signature.normalize_s().unwrap_or(signature);
        Ok(normalized.to_bytes().into())
    }
}

/// Strictly verifies a no-padding base64url P1363 signature and requires low-S.
pub fn verify_receipt_signature(
    public_key_spki_der: &[u8],
    canonical: &[u8],
    encoded_signature: &str,
) -> Result<(), SignerError> {
    let raw = decode_base64url_no_pad(encoded_signature)?;
    if raw.len() != 64 {
        return Err(SignerError::InvalidSignatureEncoding);
    }
    let signature = Signature::from_slice(&raw).map_err(|_| SignerError::InvalidSignature)?;
    if signature.normalize_s().is_some() {
        return Err(SignerError::HighSSignature);
    }
    let verifying_key = VerifyingKey::from_public_key_der(public_key_spki_der)
        .map_err(|_| SignerError::InvalidPublicKey)?;
    verifying_key
        .verify(canonical, &signature)
        .map_err(|_| SignerError::SignatureVerificationFailed)
}

/// Errors raised by receipt signer construction, signing, or verification.
#[derive(Debug, Error)]
pub enum SignerError {
    #[error(transparent)]
    Canonical(#[from] CanonicalError),
    #[error("receipt canonicalization is unsupported")]
    UnsupportedCanonicalization,
    #[error("receipt signature algorithm is unsupported")]
    UnsupportedAlgorithm,
    #[error("receipt signature must be unpadded base64url")]
    InvalidBase64Url,
    #[error("receipt signature must contain exactly 64 P1363 bytes")]
    InvalidSignatureEncoding,
    #[error("receipt signature is not a valid P-256 scalar pair")]
    InvalidSignature,
    #[error("receipt signature uses a malleable high-S value")]
    HighSSignature,
    #[error("receipt signature verification failed")]
    SignatureVerificationFailed,
    #[error("receipt public key is not valid P-256 SPKI DER")]
    InvalidPublicKey,
    #[error("receipt key_id does not match signer SPKI")]
    KeyIdMismatch,
    #[error("receipt key epoch does not match signer identity")]
    KeyEpochMismatch,
    #[error("receipt anchor does not match protected signer identity")]
    AnchorMismatch,
    #[error("receipt_id does not match canonical claims")]
    ReceiptIdMismatch,
    #[error("signer provider and trust classification are inconsistent")]
    InvalidTrustClassification,
    #[error("protected key reference must be non-exportable, non-software, and service-bound")]
    InvalidProtectedKeyReference,
    #[error("live protected-key attestation does not match the pinned anchor binding")]
    ProtectedAttestationMismatch,
    #[error("live protected-key provider reports exportable private key material")]
    ProtectedKeyExportable,
    #[error("live protected-key service ACL is not restricted to the service and SYSTEM")]
    ProtectedServiceAclMismatch,
    #[error("software test secret is not a valid P-256 scalar")]
    InvalidSoftwareTestKey,
    #[error("protected signing provider failed: {0}")]
    ProtectedProvider(String),
}

fn normalize_signature(raw: &[u8; 64]) -> Result<Signature, SignerError> {
    let signature = Signature::from_slice(raw).map_err(|_| SignerError::InvalidSignature)?;
    Ok(signature.normalize_s().unwrap_or(signature))
}

fn validate_identity(identity: &SignerIdentity) -> Result<(), SignerError> {
    validate_provider_trust(identity.provider, identity.trust)?;
    VerifyingKey::from_public_key_der(&identity.public_key_spki_der)
        .map_err(|_| SignerError::InvalidPublicKey)?;
    if identity.key_id != Digest32::sha256(&identity.public_key_spki_der) {
        return Err(SignerError::KeyIdMismatch);
    }
    match identity.trust {
        SignerTrust::ProductionProtected
            if identity.anchor_sha256.is_none() || identity.authority_pin_sha256.is_none() =>
        {
            Err(SignerError::InvalidProtectedKeyReference)
        }
        SignerTrust::UntrustedSoftwareTest
            if identity.anchor_sha256.is_some() || identity.authority_pin_sha256.is_some() =>
        {
            Err(SignerError::InvalidTrustClassification)
        }
        _ => Ok(()),
    }
}

fn validate_platform_attestation(
    binding: &ProtectedSignerBinding,
    attestation: &PlatformKeyAttestation,
) -> Result<(), SignerError> {
    if binding.provider == SignerProvider::SoftwareTest
        || binding.key_handle_reference.is_empty()
        || binding.service_identity.is_empty()
    {
        return Err(SignerError::InvalidProtectedKeyReference);
    }
    VerifyingKey::from_public_key_der(&binding.public_key_spki_der)
        .map_err(|_| SignerError::InvalidPublicKey)?;
    if attestation.export_policy != KeyExportPolicy::NonExportable {
        return Err(SignerError::ProtectedKeyExportable);
    }
    if attestation.provider != binding.provider
        || attestation.key_handle_reference != binding.key_handle_reference
        || attestation.public_key_spki_der != binding.public_key_spki_der
        || attestation.service_identity != binding.service_identity
    {
        return Err(SignerError::ProtectedAttestationMismatch);
    }
    let mut identities = attestation.service_acl_identities.clone();
    identities.sort();
    identities.dedup();
    let mut expected = vec![binding.service_identity.clone()];
    if binding.provider == SignerProvider::WindowsCngMachine {
        expected.push("S-1-5-18".to_owned());
    }
    expected.sort();
    if identities != expected {
        return Err(SignerError::ProtectedServiceAclMismatch);
    }
    Ok(())
}

fn validate_claim_signer_bindings(
    claims: &EvalReceiptClaims,
    identity: &SignerIdentity,
) -> Result<(), SignerError> {
    if claims.key_id != identity.key_id {
        return Err(SignerError::KeyIdMismatch);
    }
    if claims.key_epoch != identity.key_epoch {
        return Err(SignerError::KeyEpochMismatch);
    }
    if let Some(anchor_sha256) = identity.anchor_sha256 {
        if claims.anchor_sha256 != anchor_sha256 {
            return Err(SignerError::AnchorMismatch);
        }
    }
    Ok(())
}

fn validate_provider_trust(
    provider: SignerProvider,
    trust: SignerTrust,
) -> Result<(), SignerError> {
    match (provider, trust) {
        (SignerProvider::SoftwareTest, SignerTrust::UntrustedSoftwareTest)
        | (
            SignerProvider::WindowsCngMachine
            | SignerProvider::Tpm
            | SignerProvider::Pkcs11
            | SignerProvider::Hsm,
            SignerTrust::ProductionProtected,
        ) => Ok(()),
        _ => Err(SignerError::InvalidTrustClassification),
    }
}

fn decode_base64url_no_pad(value: &str) -> Result<Vec<u8>, SignerError> {
    if value.is_empty()
        || value.contains('=')
        || value.len() % 4 == 1
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_'))
    {
        return Err(SignerError::InvalidBase64Url);
    }
    URL_SAFE_NO_PAD
        .decode(value)
        .map_err(|_| SignerError::InvalidBase64Url)
}

#[cfg(test)]
mod tests {
    use super::*;

    struct FakeAttestedBackend {
        attestation: PlatformKeyAttestation,
        signer: SoftwareTestSigner,
    }

    impl ProtectedSigningBackend for FakeAttestedBackend {
        fn attest_key(&self) -> Result<PlatformKeyAttestation, SignerError> {
            Ok(self.attestation.clone())
        }

        fn sign_p256_sha256_p1363(
            &self,
            _key: &ProtectedKeyReference,
            message: &[u8],
        ) -> Result<[u8; 64], SignerError> {
            self.signer.sign_canonical(message)
        }
    }

    fn fake_binding_and_backend(
        export_policy: KeyExportPolicy,
        acl: Vec<String>,
    ) -> (ProtectedSignerBinding, FakeAttestedBackend) {
        let signer = SoftwareTestSigner::from_secret_bytes([9_u8; 32], 8)
            .expect("fixed fake provider scalar is valid");
        let spki = signer.identity().public_key_spki_der.clone();
        let service_identity = "S-1-5-80-12345".to_owned();
        let binding = ProtectedSignerBinding {
            provider: SignerProvider::WindowsCngMachine,
            key_epoch: 8,
            public_key_spki_der: spki.clone(),
            key_handle_reference: "cng://machine/amw-engine-eval".to_owned(),
            service_identity: service_identity.clone(),
            anchor_sha256: Digest32::sha256(b"anchor-v2"),
            authority_pin_sha256: Digest32::sha256(b"authority-pin"),
        };
        let backend = FakeAttestedBackend {
            attestation: PlatformKeyAttestation {
                provider: SignerProvider::WindowsCngMachine,
                key_handle_reference: binding.key_handle_reference.clone(),
                public_key_spki_der: spki,
                export_policy,
                service_identity,
                service_acl_identities: acl,
            },
            signer,
        };
        (binding, backend)
    }

    #[test]
    fn protected_signer_requires_live_non_exportable_exact_acl_attestation() {
        let service = "S-1-5-80-12345".to_owned();
        let (binding, backend) = fake_binding_and_backend(
            KeyExportPolicy::NonExportable,
            vec![service.clone(), "S-1-5-18".to_owned()],
        );
        let signer = ProtectedReceiptSigner::from_attested_backend(binding, backend)
            .expect("exact live attestation creates protected signer");
        assert_eq!(signer.identity().trust, SignerTrust::ProductionProtected);

        let (binding, backend) = fake_binding_and_backend(
            KeyExportPolicy::Exportable,
            vec![service.clone(), "S-1-5-18".to_owned()],
        );
        assert!(matches!(
            ProtectedReceiptSigner::from_attested_backend(binding, backend),
            Err(SignerError::ProtectedKeyExportable)
        ));

        let (binding, backend) = fake_binding_and_backend(
            KeyExportPolicy::NonExportable,
            vec![service, "S-1-5-18".to_owned(), "S-1-5-32-545".to_owned()],
        );
        assert!(matches!(
            ProtectedReceiptSigner::from_attested_backend(binding, backend),
            Err(SignerError::ProtectedServiceAclMismatch)
        ));
    }
}
