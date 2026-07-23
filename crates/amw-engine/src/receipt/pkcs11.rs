//! Linux PKCS#11 receipt signer resolution and live key attestation.

#[cfg(target_os = "linux")]
use std::{
    fs::{File, OpenOptions},
    io::{self, Read},
    os::unix::fs::{MetadataExt as _, OpenOptionsExt as _},
    path::Component,
};
#[cfg(any(target_os = "linux", test))]
use std::{
    path::{Path, PathBuf},
    sync::{Arc, Mutex},
};

#[cfg(any(target_os = "linux", test))]
use cryptoki::{
    context::{CInitializeArgs, CInitializeFlags, Pkcs11},
    mechanism::{Mechanism, MechanismType},
    object::{Attribute, AttributeType, KeyType, ObjectClass, ObjectHandle},
    session::{Session, UserType},
    types::AuthPin,
};
#[cfg(any(target_os = "linux", test))]
use p256::{
    ecdsa::VerifyingKey,
    pkcs8::{DecodePublicKey as _, EncodePublicKey as _},
};
#[cfg(target_os = "linux")]
use serde::Deserialize;
#[cfg(any(target_os = "linux", test))]
use sha2::{Digest as _, Sha256};

#[cfg(any(target_os = "linux", test))]
use super::{
    Digest32, KeyExportPolicy, PlatformKeyAttestation, ProtectedKeyReference,
    ProtectedReceiptSigner, ProtectedSignerBinding, ProtectedSigningBackend, ReceiptSigner,
    SignerError, SignerProvider,
};

#[cfg(target_os = "linux")]
const PROVIDER_CONFIG_SCHEMA_VERSION: u16 = 1;
#[cfg(target_os = "linux")]
const MAX_PROVIDER_CONFIG_BYTES: u64 = 64 * 1024;
#[cfg(target_os = "linux")]
const MAX_PIN_BYTES: u64 = 4 * 1024;
#[cfg(any(target_os = "linux", test))]
const P256_EC_PARAMS_DER: &[u8] = &[0x06, 0x08, 0x2a, 0x86, 0x48, 0xce, 0x3d, 0x03, 0x01, 0x07];
#[cfg(any(target_os = "linux", test))]
const PKCS11_POP_DOMAIN: &[u8] = b"AMW\0linux-pkcs11-receipt-key-pop-v1\0";

/// Resolved Linux signer and service identity after live PKCS#11 proof of possession.
#[cfg(any(target_os = "linux", test))]
pub(crate) struct LinuxPkcs11ResolvedSigner {
    pub(crate) signer: Arc<dyn ReceiptSigner>,
    pub(crate) service_identity: String,
}

#[derive(Clone, Debug, Eq, PartialEq)]
#[cfg(any(target_os = "linux", test))]
struct ExpectedAnchorBinding {
    installation_id: String,
    provider: SignerProvider,
    key_epoch: u64,
    public_key_spki_der: Vec<u8>,
    service_identity: String,
    anchor_sha256: Digest32,
    authority_pin_sha256: Digest32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
#[cfg(any(target_os = "linux", test))]
struct ValidatedProviderConfig {
    installation_id: String,
    key_id: Digest32,
    module_path: PathBuf,
    module_sha256: Digest32,
    token_label: String,
    token_serial: String,
    key_object_id: Vec<u8>,
    key_label: String,
    user_pin_path: PathBuf,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
#[cfg(any(target_os = "linux", test))]
struct PrivateFileFacts {
    is_regular: bool,
    owner_uid: u32,
    mode: u32,
    link_count: u64,
    length: u64,
}

#[derive(Debug, Deserialize)]
#[cfg(target_os = "linux")]
#[serde(deny_unknown_fields)]
struct ProviderConfigRecord {
    schema_version: u16,
    installation_id: String,
    key_id: String,
    module_path: PathBuf,
    module_sha256: String,
    token_label: String,
    token_serial: String,
    key_object_id: String,
    key_label: String,
    user_pin_path: PathBuf,
}

#[derive(Clone, Debug, Eq, PartialEq)]
#[cfg(any(target_os = "linux", test))]
struct Pkcs11KeyAttestation {
    key_reference: String,
    module_sha256: Digest32,
    token_label: String,
    token_serial: String,
    private_object_class: bool,
    private_key_object_id: Vec<u8>,
    private_key_label: Vec<u8>,
    private_token: bool,
    private_private: bool,
    private_sensitive: bool,
    private_always_sensitive: bool,
    private_extractable: bool,
    private_never_extractable: bool,
    private_sign: bool,
    private_key_type_ec: bool,
    private_ec_params_der: Vec<u8>,
    public_token: bool,
    public_private: bool,
    public_verify: bool,
    public_object_class: bool,
    public_key_object_id: Vec<u8>,
    public_key_label: Vec<u8>,
    public_key_type_ec: bool,
    public_ec_params_der: Vec<u8>,
    public_ec_point_der: Vec<u8>,
    public_key_spki_der: Vec<u8>,
    ecdsa_sign_supported: bool,
    service_identity: String,
}

#[cfg(any(target_os = "linux", test))]
trait Pkcs11Provider: Send + Sync {
    fn attest_key(&self) -> Result<Pkcs11KeyAttestation, SignerError>;

    fn sign_sha256_digest(
        &self,
        key_reference: &str,
        digest: &[u8; 32],
    ) -> Result<[u8; 64], SignerError>;
}

#[cfg(any(target_os = "linux", test))]
struct Pkcs11SigningBackend<P> {
    provider: P,
    platform_attestation: PlatformKeyAttestation,
}

#[cfg(any(target_os = "linux", test))]
impl<P> ProtectedSigningBackend for Pkcs11SigningBackend<P>
where
    P: Pkcs11Provider,
{
    fn attest_key(&self) -> Result<PlatformKeyAttestation, SignerError> {
        Ok(self.platform_attestation.clone())
    }

    fn sign_p256_sha256_p1363(
        &self,
        key: &ProtectedKeyReference,
        message: &[u8],
    ) -> Result<[u8; 64], SignerError> {
        let digest: [u8; 32] = Sha256::digest(message).into();
        self.provider
            .sign_sha256_digest(key.opaque_key_reference(), &digest)
    }
}

/// Resolves the exact Linux PKCS#11 key bound by a verified receipt trust anchor.
///
/// The provider configuration path is deterministically derived from `anchor_path`
/// by appending `.pkcs11.toml`. No environment variable or request data may select
/// a module, token, or key. Construction fails unless the config and PIN are private
/// service-owned files, every selector and key attribute matches, and the token signs
/// an anchor-bound proof-of-possession challenge.
#[cfg(target_os = "linux")]
pub(crate) fn resolve_linux_pkcs11_signer(
    anchor_path: &Path,
    installation_id: &str,
    expected_provider: SignerProvider,
    key_epoch: u64,
    public_key_spki_der: &[u8],
    service_identity: &str,
    anchor_sha256: Digest32,
    authority_pin_sha256: Digest32,
) -> Result<LinuxPkcs11ResolvedSigner, SignerError> {
    if !anchor_path.is_absolute() {
        return Err(provider_error(
            "protected trust anchor path must be absolute for PKCS#11 resolution",
        ));
    }
    require_absolute_normal_path(anchor_path, "trust_anchor_path")?;
    let effective_uid = effective_uid();
    if effective_uid == 0 {
        return Err(provider_error(
            "PKCS#11 receipt signing requires a dedicated non-root service UID",
        ));
    }
    if !matches!(
        expected_provider,
        SignerProvider::Pkcs11 | SignerProvider::Hsm
    ) {
        return Err(provider_error(
            "Linux PKCS#11 resolution requires a pkcs11 or hsm trust-anchor provider",
        ));
    }
    let expected_service_identity = format!("uid:{effective_uid}");
    if service_identity != expected_service_identity {
        return Err(provider_error(
            "anchor service identity does not match the engine effective UID",
        ));
    }
    let expected = ExpectedAnchorBinding {
        installation_id: installation_id.to_owned(),
        provider: expected_provider,
        key_epoch,
        public_key_spki_der: public_key_spki_der.to_vec(),
        service_identity: service_identity.to_owned(),
        anchor_sha256,
        authority_pin_sha256,
    };
    let config_path = provider_config_path(anchor_path)?;
    let config = load_provider_config(&config_path, effective_uid)?;
    let provider = CryptokiProvider::open(&config, effective_uid, &expected.service_identity)?;
    resolve_with_provider(expected, config, provider)
}

#[cfg(any(target_os = "linux", test))]
fn resolve_with_provider<P>(
    expected: ExpectedAnchorBinding,
    config: ValidatedProviderConfig,
    provider: P,
) -> Result<LinuxPkcs11ResolvedSigner, SignerError>
where
    P: Pkcs11Provider + 'static,
{
    validate_config_binding(&expected, &config)?;
    let attestation = provider.attest_key()?;
    validate_pkcs11_attestation(&expected, &config, &attestation)?;
    let binding = ProtectedSignerBinding {
        provider: expected.provider,
        key_epoch: expected.key_epoch,
        public_key_spki_der: expected.public_key_spki_der.clone(),
        key_handle_reference: attestation.key_reference.clone(),
        service_identity: expected.service_identity.clone(),
        anchor_sha256: expected.anchor_sha256,
        authority_pin_sha256: expected.authority_pin_sha256,
    };
    let backend = Pkcs11SigningBackend {
        provider,
        platform_attestation: PlatformKeyAttestation {
            provider: expected.provider,
            key_handle_reference: attestation.key_reference,
            public_key_spki_der: attestation.public_key_spki_der,
            export_policy: KeyExportPolicy::NonExportable,
            service_identity: expected.service_identity.clone(),
            service_acl_identities: vec![expected.service_identity.clone()],
        },
    };
    let signer = ProtectedReceiptSigner::from_attested_backend(binding, backend)?;
    signer.sign_canonical(&proof_of_possession_challenge(&expected))?;
    Ok(LinuxPkcs11ResolvedSigner {
        signer: Arc::new(signer),
        service_identity: expected.service_identity,
    })
}

#[cfg(any(target_os = "linux", test))]
fn validate_config_binding(
    expected: &ExpectedAnchorBinding,
    config: &ValidatedProviderConfig,
) -> Result<(), SignerError> {
    if !matches!(
        expected.provider,
        SignerProvider::Pkcs11 | SignerProvider::Hsm
    ) || expected.key_epoch == 0
        || config.installation_id != expected.installation_id
        || config.key_id != Digest32::sha256(&expected.public_key_spki_der)
    {
        return Err(provider_error(
            "PKCS#11 provider config is not bound to the verified trust anchor",
        ));
    }
    validate_nonempty_text("installation_id", &config.installation_id)?;
    validate_nonempty_text("service_identity", &expected.service_identity)?;
    validate_nonempty_text("token_label", &config.token_label)?;
    validate_nonempty_text("token_serial", &config.token_serial)?;
    validate_nonempty_text("key_label", &config.key_label)?;
    if config.key_object_id.is_empty() || config.key_object_id.len() > 128 {
        return Err(provider_error(
            "PKCS#11 CKA_ID must contain between 1 and 128 bytes",
        ));
    }
    let verifying_key = VerifyingKey::from_public_key_der(&expected.public_key_spki_der)
        .map_err(|_| SignerError::InvalidPublicKey)?;
    let canonical_spki = verifying_key
        .to_public_key_der()
        .map_err(|_| SignerError::InvalidPublicKey)?;
    if canonical_spki.as_bytes() != expected.public_key_spki_der {
        return Err(provider_error(
            "anchor P-256 SubjectPublicKeyInfo is not canonical DER",
        ));
    }
    Ok(())
}

#[cfg(any(target_os = "linux", test))]
fn validate_pkcs11_attestation(
    expected: &ExpectedAnchorBinding,
    config: &ValidatedProviderConfig,
    attestation: &Pkcs11KeyAttestation,
) -> Result<(), SignerError> {
    if attestation.module_sha256 != config.module_sha256
        || attestation.token_label != config.token_label
        || attestation.token_serial != config.token_serial
        || attestation.service_identity != expected.service_identity
    {
        return Err(provider_error(
            "live PKCS#11 module, token, key, or service identity differs from its protected pin",
        ));
    }
    if !attestation.private_object_class
        || attestation.private_key_object_id != config.key_object_id
        || attestation.private_key_label != config.key_label.as_bytes()
        || !attestation.private_token
        || !attestation.private_private
        || !attestation.private_sensitive
        || !attestation.private_always_sensitive
        || attestation.private_extractable
        || !attestation.private_never_extractable
        || !attestation.private_sign
        || !attestation.private_key_type_ec
        || attestation.private_ec_params_der != P256_EC_PARAMS_DER
    {
        return Err(provider_error(
            "PKCS#11 private key protection or P-256 signing attributes are invalid",
        ));
    }
    if !attestation.public_object_class
        || attestation.public_key_object_id != config.key_object_id
        || attestation.public_key_label != config.key_label.as_bytes()
        || !attestation.public_token
        || attestation.public_private
        || !attestation.public_verify
        || !attestation.public_key_type_ec
        || attestation.public_ec_params_der != P256_EC_PARAMS_DER
        || !attestation.ecdsa_sign_supported
    {
        return Err(provider_error(
            "PKCS#11 public key or CKM_ECDSA mechanism attributes are invalid",
        ));
    }
    let expected_point = expected_ec_point_der(&expected.public_key_spki_der)?;
    if attestation.public_ec_point_der != expected_point
        || attestation.public_key_spki_der != expected.public_key_spki_der
    {
        return Err(provider_error(
            "PKCS#11 public EC point does not exactly match the anchor SPKI",
        ));
    }
    Ok(())
}

#[cfg(any(target_os = "linux", test))]
fn expected_ec_point_der(public_key_spki_der: &[u8]) -> Result<Vec<u8>, SignerError> {
    let verifying_key = VerifyingKey::from_public_key_der(public_key_spki_der)
        .map_err(|_| SignerError::InvalidPublicKey)?;
    let point = verifying_key.to_encoded_point(false);
    let point_bytes = point.as_bytes();
    let point_length = u8::try_from(point_bytes.len())
        .map_err(|_| provider_error("P-256 EC point length exceeds DER short form"))?;
    let mut encoded = Vec::with_capacity(point_bytes.len() + 2);
    encoded.extend_from_slice(&[0x04, point_length]);
    encoded.extend_from_slice(point_bytes);
    Ok(encoded)
}

#[cfg(any(target_os = "linux", test))]
fn proof_of_possession_challenge(expected: &ExpectedAnchorBinding) -> Vec<u8> {
    let installation = expected.installation_id.as_bytes();
    let mut challenge = Vec::with_capacity(PKCS11_POP_DOMAIN.len() + 68 + installation.len());
    challenge.extend_from_slice(PKCS11_POP_DOMAIN);
    challenge.extend_from_slice(expected.anchor_sha256.as_bytes());
    challenge.extend_from_slice(Digest32::sha256(&expected.public_key_spki_der).as_bytes());
    challenge.extend_from_slice(
        &u32::try_from(installation.len())
            .unwrap_or(u32::MAX)
            .to_be_bytes(),
    );
    challenge.extend_from_slice(installation);
    challenge
}

#[cfg(target_os = "linux")]
fn provider_config_path(anchor_path: &Path) -> Result<PathBuf, SignerError> {
    let file_name = anchor_path
        .file_name()
        .and_then(|name| name.to_str())
        .ok_or_else(|| provider_error("protected trust anchor path has no UTF-8 file name"))?;
    Ok(anchor_path.with_file_name(format!("{file_name}.pkcs11.toml")))
}

#[cfg(target_os = "linux")]
fn load_provider_config(
    config_path: &Path,
    effective_uid: u32,
) -> Result<ValidatedProviderConfig, SignerError> {
    let bytes = read_private_service_file(
        config_path,
        effective_uid,
        MAX_PROVIDER_CONFIG_BYTES,
        "provider config",
    )?;
    let text = std::str::from_utf8(&bytes)
        .map_err(|_| provider_error("PKCS#11 provider config is not UTF-8"))?;
    let record: ProviderConfigRecord = toml::from_str(text)
        .map_err(|error| provider_error(format!("PKCS#11 provider config is invalid: {error}")))?;
    if record.schema_version != PROVIDER_CONFIG_SCHEMA_VERSION {
        return Err(provider_error(
            "PKCS#11 provider config schema version is unsupported",
        ));
    }
    require_absolute_normal_path(&record.module_path, "module_path")?;
    require_absolute_normal_path(&record.user_pin_path, "user_pin_path")?;
    let key_id = Digest32::from_lower_hex(&record.key_id)
        .map_err(|error| provider_error(format!("PKCS#11 key_id is invalid: {error}")))?;
    let module_sha256 = Digest32::from_lower_hex(&record.module_sha256)
        .map_err(|error| provider_error(format!("PKCS#11 module_sha256 is invalid: {error}")))?;
    let key_object_id = decode_lower_hex(&record.key_object_id, "key_object_id")?;
    Ok(ValidatedProviderConfig {
        installation_id: record.installation_id,
        key_id,
        module_path: record.module_path,
        module_sha256,
        token_label: record.token_label,
        token_serial: record.token_serial,
        key_object_id,
        key_label: record.key_label,
        user_pin_path: record.user_pin_path,
    })
}

#[cfg(target_os = "linux")]
fn require_absolute_normal_path(path: &Path, field: &'static str) -> Result<(), SignerError> {
    if !path.is_absolute()
        || path
            .components()
            .any(|component| matches!(component, Component::CurDir | Component::ParentDir))
    {
        return Err(provider_error(format!(
            "PKCS#11 {field} must be an absolute normalized path"
        )));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn read_private_service_file(
    path: &Path,
    effective_uid: u32,
    max_bytes: u64,
    description: &'static str,
) -> Result<Vec<u8>, SignerError> {
    validate_private_parent(path, effective_uid, description)?;
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|error| provider_io_error(description, error))?;
    let metadata = file
        .metadata()
        .map_err(|error| provider_io_error(description, error))?;
    let facts = PrivateFileFacts {
        is_regular: metadata.file_type().is_file(),
        owner_uid: metadata.uid(),
        mode: metadata.mode(),
        link_count: metadata.nlink(),
        length: metadata.len(),
    };
    if !private_file_facts_are_secure(facts, effective_uid, max_bytes) {
        return Err(provider_error(format!(
            "PKCS#11 {description} must be a non-empty service-owned 0600 regular file"
        )));
    }
    let capacity = usize::try_from(metadata.len())
        .map_err(|_| provider_error(format!("PKCS#11 {description} is too large")))?;
    let mut bytes = Vec::with_capacity(capacity);
    file.read_to_end(&mut bytes)
        .map_err(|error| provider_io_error(description, error))?;
    if bytes.len() != capacity {
        return Err(provider_error(format!(
            "PKCS#11 {description} changed while being read"
        )));
    }
    Ok(bytes)
}

#[cfg(target_os = "linux")]
fn validate_module_file(path: &Path, expected_digest: Digest32) -> Result<Digest32, SignerError> {
    validate_root_owned_ancestors(path)?;
    let mut file = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_CLOEXEC | libc::O_NOFOLLOW)
        .open(path)
        .map_err(|error| provider_io_error("module", error))?;
    let metadata = file
        .metadata()
        .map_err(|error| provider_io_error("module", error))?;
    if !metadata.file_type().is_file()
        || metadata.uid() != 0
        || metadata.mode() & 0o022 != 0
        || metadata.len() == 0
    {
        return Err(provider_error(
            "PKCS#11 module must be a non-writable root-owned regular file",
        ));
    }
    let actual = sha256_reader(&mut file, "module")?;
    if actual != expected_digest {
        return Err(provider_error(
            "PKCS#11 module does not match the protected SHA-256 pin",
        ));
    }
    Ok(actual)
}

#[cfg(target_os = "linux")]
fn validate_private_parent(
    path: &Path,
    effective_uid: u32,
    description: &'static str,
) -> Result<(), SignerError> {
    let parent = path
        .parent()
        .ok_or_else(|| provider_error(format!("PKCS#11 {description} has no parent directory")))?;
    let mut current = Some(parent);
    let mut immediate = true;
    while let Some(directory) = current {
        let metadata = directory
            .symlink_metadata()
            .map_err(|error| provider_io_error(description, error))?;
        let exact_private_parent =
            metadata.uid() == effective_uid && metadata.mode() & 0o7777 == 0o700;
        let protected_ancestor = (metadata.uid() == 0 || metadata.uid() == effective_uid)
            && metadata.mode() & 0o022 == 0;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || (immediate && !exact_private_parent)
            || (!immediate && !protected_ancestor)
        {
            return Err(provider_error(format!(
                "PKCS#11 {description} path must stay beneath a service-owned 0700 directory without mutable or symlinked ancestors"
            )));
        }
        immediate = false;
        current = directory.parent();
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn validate_root_owned_ancestors(path: &Path) -> Result<(), SignerError> {
    let mut current = path.parent();
    while let Some(directory) = current {
        let metadata = directory
            .symlink_metadata()
            .map_err(|error| provider_io_error("module parent", error))?;
        if metadata.file_type().is_symlink()
            || !metadata.is_dir()
            || metadata.uid() != 0
            || metadata.mode() & 0o022 != 0
        {
            return Err(provider_error(
                "PKCS#11 module ancestors must be non-writable root-owned directories",
            ));
        }
        current = directory.parent();
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn sha256_reader(file: &mut File, description: &'static str) -> Result<Digest32, SignerError> {
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|error| provider_io_error(description, error))?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(Digest32::from_bytes(hasher.finalize().into()))
}

#[cfg(target_os = "linux")]
fn read_user_pin(path: &Path, effective_uid: u32) -> Result<AuthPin, SignerError> {
    let bytes = read_private_service_file(path, effective_uid, MAX_PIN_BYTES, "user PIN")?;
    let mut pin =
        String::from_utf8(bytes).map_err(|_| provider_error("PKCS#11 user PIN must be UTF-8"))?;
    if pin.ends_with('\n') {
        pin.pop();
        if pin.ends_with('\r') {
            pin.pop();
        }
    }
    if pin.is_empty()
        || pin
            .bytes()
            .any(|byte| matches!(byte, b'\0' | b'\r' | b'\n'))
    {
        return Err(provider_error(
            "PKCS#11 user PIN file contains an invalid value",
        ));
    }
    Ok(AuthPin::new(pin.into()))
}

#[cfg(all(test, not(target_os = "linux")))]
fn validate_module_file(_path: &Path, _expected_digest: Digest32) -> Result<Digest32, SignerError> {
    Err(provider_error("live PKCS#11 module loading is Linux-only"))
}

#[cfg(all(test, not(target_os = "linux")))]
fn read_user_pin(_path: &Path, _effective_uid: u32) -> Result<AuthPin, SignerError> {
    Err(provider_error("live PKCS#11 PIN loading is Linux-only"))
}

#[cfg(any(target_os = "linux", test))]
struct CryptokiProvider {
    session: Mutex<Session>,
    private_key: ObjectHandle,
    attestation: Pkcs11KeyAttestation,
}

#[cfg(any(target_os = "linux", test))]
impl CryptokiProvider {
    fn open(
        config: &ValidatedProviderConfig,
        effective_uid: u32,
        service_identity: &str,
    ) -> Result<Self, SignerError> {
        let module_sha256 = validate_module_file(&config.module_path, config.module_sha256)?;
        let client = Pkcs11::new(&config.module_path)
            .map_err(|error| provider_error(format!("PKCS#11 module load failed: {error}")))?;
        client
            .initialize(CInitializeArgs::new(CInitializeFlags::OS_LOCKING_OK))
            .map_err(|error| provider_error(format!("PKCS#11 initialization failed: {error}")))?;
        let (slot, token_label, token_serial) =
            select_exact_slot(&client, &config.token_label, &config.token_serial)?;
        let mechanisms = client.get_mechanism_list(slot).map_err(|error| {
            provider_error(format!("PKCS#11 mechanism enumeration failed: {error}"))
        })?;
        let ecdsa_sign_supported = mechanisms.contains(&MechanismType::ECDSA)
            && client
                .get_mechanism_info(slot, MechanismType::ECDSA)
                .map_err(|error| {
                    provider_error(format!("PKCS#11 CKM_ECDSA query failed: {error}"))
                })?
                .sign();
        let session = client.open_ro_session(slot).map_err(|error| {
            provider_error(format!("PKCS#11 read-only session failed: {error}"))
        })?;
        let pin = read_user_pin(&config.user_pin_path, effective_uid)?;
        session
            .login(UserType::User, Some(&pin))
            .map_err(|error| provider_error(format!("PKCS#11 user login failed: {error}")))?;
        let private_key = find_exact_key(
            &session,
            ObjectClass::PRIVATE_KEY,
            &config.key_object_id,
            &config.key_label,
        )?;
        let public_key = find_exact_key(
            &session,
            ObjectClass::PUBLIC_KEY,
            &config.key_object_id,
            &config.key_label,
        )?;
        let private_attributes = session
            .get_attributes(private_key, &private_attribute_types())
            .map_err(|error| {
                provider_error(format!("PKCS#11 private-key attestation failed: {error}"))
            })?;
        let public_attributes = session
            .get_attributes(public_key, &public_attribute_types())
            .map_err(|error| {
                provider_error(format!("PKCS#11 public-key attestation failed: {error}"))
            })?;
        let public_ec_point_der = attribute_bytes(&public_attributes, AttributeType::EcPoint)?;
        let public_key_spki_der = spki_from_ec_point_der(&public_ec_point_der)?;
        let key_reference = format!(
            "pkcs11:module={};token={};id={};label={}",
            config.module_sha256,
            config.token_serial,
            hex::encode(&config.key_object_id),
            config.key_label
        );
        let attestation = Pkcs11KeyAttestation {
            key_reference,
            module_sha256,
            token_label,
            token_serial,
            private_object_class: attribute_object_class(&private_attributes)?
                == ObjectClass::PRIVATE_KEY,
            private_key_object_id: attribute_bytes(&private_attributes, AttributeType::Id)?,
            private_key_label: attribute_bytes(&private_attributes, AttributeType::Label)?,
            private_token: attribute_bool(&private_attributes, AttributeType::Token)?,
            private_private: attribute_bool(&private_attributes, AttributeType::Private)?,
            private_sensitive: attribute_bool(&private_attributes, AttributeType::Sensitive)?,
            private_always_sensitive: attribute_bool(
                &private_attributes,
                AttributeType::AlwaysSensitive,
            )?,
            private_extractable: attribute_bool(&private_attributes, AttributeType::Extractable)?,
            private_never_extractable: attribute_bool(
                &private_attributes,
                AttributeType::NeverExtractable,
            )?,
            private_sign: attribute_bool(&private_attributes, AttributeType::Sign)?,
            private_key_type_ec: attribute_key_type(&private_attributes)? == KeyType::EC,
            private_ec_params_der: attribute_bytes(&private_attributes, AttributeType::EcParams)?,
            public_token: attribute_bool(&public_attributes, AttributeType::Token)?,
            public_private: attribute_bool(&public_attributes, AttributeType::Private)?,
            public_verify: attribute_bool(&public_attributes, AttributeType::Verify)?,
            public_object_class: attribute_object_class(&public_attributes)?
                == ObjectClass::PUBLIC_KEY,
            public_key_object_id: attribute_bytes(&public_attributes, AttributeType::Id)?,
            public_key_label: attribute_bytes(&public_attributes, AttributeType::Label)?,
            public_key_type_ec: attribute_key_type(&public_attributes)? == KeyType::EC,
            public_ec_params_der: attribute_bytes(&public_attributes, AttributeType::EcParams)?,
            public_ec_point_der,
            public_key_spki_der,
            ecdsa_sign_supported,
            service_identity: service_identity.to_owned(),
        };
        Ok(Self {
            session: Mutex::new(session),
            private_key,
            attestation,
        })
    }
}

#[cfg(any(target_os = "linux", test))]
impl Pkcs11Provider for CryptokiProvider {
    fn attest_key(&self) -> Result<Pkcs11KeyAttestation, SignerError> {
        Ok(self.attestation.clone())
    }

    fn sign_sha256_digest(
        &self,
        key_reference: &str,
        digest: &[u8; 32],
    ) -> Result<[u8; 64], SignerError> {
        if key_reference != self.attestation.key_reference {
            return Err(provider_error(
                "PKCS#11 signing request used an unverified key reference",
            ));
        }
        let session = self
            .session
            .lock()
            .map_err(|_| provider_error("PKCS#11 signing session lock is poisoned"))?;
        let signature = session
            .sign(&Mechanism::Ecdsa, self.private_key, digest)
            .map_err(|error| provider_error(format!("PKCS#11 CKM_ECDSA failed: {error}")))?;
        signature.try_into().map_err(|signature: Vec<u8>| {
            provider_error(format!(
                "PKCS#11 CKM_ECDSA returned {} bytes instead of P-256 P1363",
                signature.len()
            ))
        })
    }
}

#[cfg(any(target_os = "linux", test))]
fn select_exact_slot(
    client: &Pkcs11,
    token_label: &str,
    token_serial: &str,
) -> Result<(cryptoki::slot::Slot, String, String), SignerError> {
    let mut matches = Vec::new();
    for slot in client
        .get_slots_with_token()
        .map_err(|error| provider_error(format!("PKCS#11 slot enumeration failed: {error}")))?
    {
        let info = client
            .get_token_info(slot)
            .map_err(|error| provider_error(format!("PKCS#11 token query failed: {error}")))?;
        if info.label() == token_label && info.serial_number() == token_serial {
            matches.push((
                slot,
                info.label().to_owned(),
                info.serial_number().to_owned(),
            ));
        }
    }
    if matches.len() != 1 {
        return Err(provider_error(
            "PKCS#11 token label and serial must select exactly one token",
        ));
    }
    Ok(matches.remove(0))
}

#[cfg(any(target_os = "linux", test))]
fn find_exact_key(
    session: &Session,
    class: ObjectClass,
    key_object_id: &[u8],
    key_label: &str,
) -> Result<ObjectHandle, SignerError> {
    let template = [
        Attribute::Class(class),
        Attribute::KeyType(KeyType::EC),
        Attribute::Id(key_object_id.to_vec()),
        Attribute::Label(key_label.as_bytes().to_vec()),
        Attribute::Token(true),
    ];
    let handles = session
        .find_objects(&template)
        .map_err(|error| provider_error(format!("PKCS#11 key selection failed: {error}")))?;
    if handles.len() != 1 {
        return Err(provider_error(
            "PKCS#11 pinned key selectors must identify exactly one object",
        ));
    }
    Ok(handles[0])
}

#[cfg(any(target_os = "linux", test))]
fn private_attribute_types() -> [AttributeType; 12] {
    [
        AttributeType::Class,
        AttributeType::KeyType,
        AttributeType::Id,
        AttributeType::Label,
        AttributeType::Token,
        AttributeType::Private,
        AttributeType::Sensitive,
        AttributeType::AlwaysSensitive,
        AttributeType::Extractable,
        AttributeType::NeverExtractable,
        AttributeType::Sign,
        AttributeType::EcParams,
    ]
}

#[cfg(any(target_os = "linux", test))]
fn public_attribute_types() -> [AttributeType; 9] {
    [
        AttributeType::Class,
        AttributeType::KeyType,
        AttributeType::Id,
        AttributeType::Label,
        AttributeType::Token,
        AttributeType::Private,
        AttributeType::Verify,
        AttributeType::EcParams,
        AttributeType::EcPoint,
    ]
}

#[cfg(any(target_os = "linux", test))]
fn attribute_bool(
    attributes: &[Attribute],
    expected_type: AttributeType,
) -> Result<bool, SignerError> {
    attributes
        .iter()
        .find(|attribute| attribute.attribute_type() == expected_type)
        .and_then(|attribute| match attribute {
            Attribute::Token(value)
            | Attribute::Private(value)
            | Attribute::Sensitive(value)
            | Attribute::AlwaysSensitive(value)
            | Attribute::Extractable(value)
            | Attribute::NeverExtractable(value)
            | Attribute::Sign(value)
            | Attribute::Verify(value) => Some(*value),
            _ => None,
        })
        .ok_or_else(|| provider_error("PKCS#11 required boolean key attribute is unavailable"))
}

#[cfg(any(target_os = "linux", test))]
fn attribute_bytes(
    attributes: &[Attribute],
    expected_type: AttributeType,
) -> Result<Vec<u8>, SignerError> {
    attributes
        .iter()
        .find(|attribute| attribute.attribute_type() == expected_type)
        .and_then(|attribute| match attribute {
            Attribute::Id(value)
            | Attribute::Label(value)
            | Attribute::EcParams(value)
            | Attribute::EcPoint(value) => Some(value.clone()),
            _ => None,
        })
        .ok_or_else(|| provider_error("PKCS#11 required byte-string key attribute is unavailable"))
}

#[cfg(any(target_os = "linux", test))]
fn attribute_key_type(attributes: &[Attribute]) -> Result<KeyType, SignerError> {
    attributes
        .iter()
        .find_map(|attribute| match attribute {
            Attribute::KeyType(value) => Some(*value),
            _ => None,
        })
        .ok_or_else(|| provider_error("PKCS#11 CKA_KEY_TYPE is unavailable"))
}

#[cfg(any(target_os = "linux", test))]
fn attribute_object_class(attributes: &[Attribute]) -> Result<ObjectClass, SignerError> {
    attributes
        .iter()
        .find_map(|attribute| match attribute {
            Attribute::Class(value) => Some(*value),
            _ => None,
        })
        .ok_or_else(|| provider_error("PKCS#11 CKA_CLASS is unavailable"))
}

#[cfg(any(target_os = "linux", test))]
fn spki_from_ec_point_der(point_der: &[u8]) -> Result<Vec<u8>, SignerError> {
    if point_der.len() != 67 || point_der[0..2] != [0x04, 65] {
        return Err(provider_error(
            "PKCS#11 CKA_EC_POINT is not a canonical DER P-256 point",
        ));
    }
    let verifying_key = VerifyingKey::from_sec1_bytes(&point_der[2..])
        .map_err(|_| provider_error("PKCS#11 CKA_EC_POINT is not a valid P-256 point"))?;
    verifying_key
        .to_public_key_der()
        .map(|document| document.as_bytes().to_vec())
        .map_err(|_| SignerError::InvalidPublicKey)
}

#[cfg(any(target_os = "linux", test))]
fn validate_nonempty_text(field: &'static str, value: &str) -> Result<(), SignerError> {
    if value.is_empty()
        || value.len() > 4_096
        || value
            .chars()
            .any(|character| character.is_control() || character == '\0')
    {
        return Err(provider_error(format!("PKCS#11 {field} is invalid")));
    }
    Ok(())
}

#[cfg(any(target_os = "linux", test))]
fn private_file_facts_are_secure(
    facts: PrivateFileFacts,
    effective_uid: u32,
    max_bytes: u64,
) -> bool {
    facts.is_regular
        && facts.owner_uid == effective_uid
        && facts.mode & 0o7777 == 0o600
        && facts.link_count == 1
        && facts.length > 0
        && facts.length <= max_bytes
}

#[cfg(target_os = "linux")]
fn decode_lower_hex(value: &str, field: &'static str) -> Result<Vec<u8>, SignerError> {
    if value.is_empty()
        || value.len() % 2 != 0
        || !value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        return Err(provider_error(format!(
            "PKCS#11 {field} must be canonical lowercase hexadecimal"
        )));
    }
    hex::decode(value)
        .map_err(|error| provider_error(format!("PKCS#11 {field} is invalid: {error}")))
}

#[cfg(target_os = "linux")]
fn effective_uid() -> u32 {
    // SAFETY: geteuid has no preconditions, takes no pointers, and returns the process identity.
    unsafe { libc::geteuid() }
}

#[cfg(target_os = "linux")]
fn provider_io_error(description: &'static str, error: io::Error) -> SignerError {
    provider_error(format!("PKCS#11 {description} is unavailable: {error}"))
}

#[cfg(any(target_os = "linux", test))]
fn provider_error(message: impl Into<String>) -> SignerError {
    SignerError::ProtectedProvider(message.into())
}

#[cfg(test)]
mod tests {
    use p256::{
        ecdsa::{signature::hazmat::PrehashSigner as _, Signature, SigningKey},
        pkcs8::EncodePublicKey as _,
    };

    use super::*;

    #[test]
    fn pinned_cryptoki_surface_type_checks_without_opening_a_token() {
        let _open = CryptokiProvider::open;
        let _sign = <CryptokiProvider as Pkcs11Provider>::sign_sha256_digest;
    }

    struct FakeProvider {
        signing_key: SigningKey,
        attestation: Pkcs11KeyAttestation,
        fail_signing: bool,
    }

    impl Pkcs11Provider for FakeProvider {
        fn attest_key(&self) -> Result<Pkcs11KeyAttestation, SignerError> {
            Ok(self.attestation.clone())
        }

        fn sign_sha256_digest(
            &self,
            key_reference: &str,
            digest: &[u8; 32],
        ) -> Result<[u8; 64], SignerError> {
            if self.fail_signing || key_reference != self.attestation.key_reference {
                return Err(provider_error("fake PKCS#11 signing failed"));
            }
            let signature: Signature = self
                .signing_key
                .sign_prehash(digest)
                .map_err(|_| provider_error("fake PKCS#11 prehash signing failed"))?;
            Ok(signature.to_bytes().into())
        }
    }

    fn fixture() -> (ExpectedAnchorBinding, ValidatedProviderConfig, FakeProvider) {
        let signing_key =
            SigningKey::from_bytes((&[29_u8; 32]).into()).expect("fixture key is valid");
        let spki = signing_key
            .verifying_key()
            .to_public_key_der()
            .expect("fixture SPKI encodes")
            .as_bytes()
            .to_vec();
        let expected = ExpectedAnchorBinding {
            installation_id: "installation-a".to_owned(),
            provider: SignerProvider::Pkcs11,
            key_epoch: 4,
            public_key_spki_der: spki.clone(),
            service_identity: "uid:1001".to_owned(),
            anchor_sha256: Digest32::sha256(b"anchor"),
            authority_pin_sha256: Digest32::sha256(b"authority"),
        };
        let config = ValidatedProviderConfig {
            installation_id: expected.installation_id.clone(),
            key_id: Digest32::sha256(&spki),
            module_path: PathBuf::from("provider.so"),
            module_sha256: Digest32::sha256(b"provider"),
            token_label: "receipt-token".to_owned(),
            token_serial: "12345678".to_owned(),
            key_object_id: vec![0xa1, 0xb2],
            key_label: "amw-receipt-key".to_owned(),
            user_pin_path: PathBuf::from("pin"),
        };
        let attestation = Pkcs11KeyAttestation {
            key_reference: "pkcs11:pinned-fixture".to_owned(),
            module_sha256: config.module_sha256,
            token_label: config.token_label.clone(),
            token_serial: config.token_serial.clone(),
            private_object_class: true,
            private_key_object_id: config.key_object_id.clone(),
            private_key_label: config.key_label.as_bytes().to_vec(),
            private_token: true,
            private_private: true,
            private_sensitive: true,
            private_always_sensitive: true,
            private_extractable: false,
            private_never_extractable: true,
            private_sign: true,
            private_key_type_ec: true,
            private_ec_params_der: P256_EC_PARAMS_DER.to_vec(),
            public_token: true,
            public_private: false,
            public_verify: true,
            public_object_class: true,
            public_key_object_id: config.key_object_id.clone(),
            public_key_label: config.key_label.as_bytes().to_vec(),
            public_key_type_ec: true,
            public_ec_params_der: P256_EC_PARAMS_DER.to_vec(),
            public_ec_point_der: expected_ec_point_der(&spki).expect("fixture point encodes"),
            public_key_spki_der: spki,
            ecdsa_sign_supported: true,
            service_identity: expected.service_identity.clone(),
        };
        (
            expected,
            config,
            FakeProvider {
                signing_key,
                attestation,
                fail_signing: false,
            },
        )
    }

    #[test]
    fn exact_attestation_and_proof_of_possession_create_protected_signer() {
        let (expected, config, provider) = fixture();
        let resolved = resolve_with_provider(expected.clone(), config, provider)
            .expect("exact token policy and live proof create a signer");
        assert_eq!(resolved.signer.identity().provider, SignerProvider::Pkcs11);
        assert_eq!(resolved.service_identity, expected.service_identity);
        assert!(resolved.signer.sign_canonical(b"receipt").is_ok());
    }

    #[test]
    fn hsm_anchor_can_use_the_same_exact_pkcs11_attestation_path() {
        let (mut expected, config, provider) = fixture();
        expected.provider = SignerProvider::Hsm;
        let resolved = resolve_with_provider(expected, config, provider)
            .expect("HSM anchors may use the pinned PKCS#11 provider path");
        assert_eq!(resolved.signer.identity().provider, SignerProvider::Hsm);
    }

    #[test]
    fn protected_key_attribute_regressions_fail_closed() {
        for mutate in [
            |facts: &mut Pkcs11KeyAttestation| facts.private_private = false,
            |facts: &mut Pkcs11KeyAttestation| facts.private_sensitive = false,
            |facts: &mut Pkcs11KeyAttestation| facts.private_always_sensitive = false,
            |facts: &mut Pkcs11KeyAttestation| facts.private_extractable = true,
            |facts: &mut Pkcs11KeyAttestation| facts.private_never_extractable = false,
            |facts: &mut Pkcs11KeyAttestation| facts.private_sign = false,
            |facts: &mut Pkcs11KeyAttestation| facts.ecdsa_sign_supported = false,
        ] {
            let (expected, config, mut provider) = fixture();
            mutate(&mut provider.attestation);
            assert!(resolve_with_provider(expected, config, provider).is_err());
        }
    }

    #[test]
    fn selector_spki_and_service_identity_drift_fail_closed() {
        let (expected, config, mut provider) = fixture();
        provider.attestation.token_serial = "other-token".to_owned();
        assert!(resolve_with_provider(expected, config, provider).is_err());

        let (expected, config, mut provider) = fixture();
        provider.attestation.public_ec_point_der[5] ^= 1;
        assert!(resolve_with_provider(expected, config, provider).is_err());

        let (expected, config, mut provider) = fixture();
        provider.attestation.service_identity = "uid:0".to_owned();
        assert!(resolve_with_provider(expected, config, provider).is_err());
    }

    #[test]
    fn unavailable_proof_of_possession_has_no_software_fallback() {
        let (expected, config, mut provider) = fixture();
        provider.fail_signing = true;
        assert!(resolve_with_provider(expected, config, provider).is_err());
    }

    #[test]
    fn provider_config_and_pin_policy_rejects_mutable_or_linked_files() {
        let secure = PrivateFileFacts {
            is_regular: true,
            owner_uid: 1001,
            mode: 0o100600,
            link_count: 1,
            length: 128,
        };
        assert!(private_file_facts_are_secure(secure, 1001, 1024));
        for insecure in [
            PrivateFileFacts {
                mode: 0o100640,
                ..secure
            },
            PrivateFileFacts {
                owner_uid: 0,
                ..secure
            },
            PrivateFileFacts {
                link_count: 2,
                ..secure
            },
            PrivateFileFacts {
                is_regular: false,
                ..secure
            },
            PrivateFileFacts {
                length: 0,
                ..secure
            },
        ] {
            assert!(!private_file_facts_are_secure(insecure, 1001, 1024));
        }
    }
}
