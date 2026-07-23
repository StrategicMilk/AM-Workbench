//! Root-confined LoRA import into an engine-owned content-addressed store.

use std::{
    collections::BTreeMap,
    fs,
    io::{BufReader, BufWriter, Read, Seek, SeekFrom, Write},
    path::{Component, Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use thiserror::Error;

/// Default upper bound for one adapter file (8 GiB).
pub const DEFAULT_MAX_ADAPTER_BYTES: u64 = 8 * 1024 * 1024 * 1024;

/// Administrator-approved adapter import declaration.
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct AdapterRegistration {
    pub id: String,
    pub root_id: String,
    pub relative_path: PathBuf,
    pub size_bytes: u64,
    pub sha256: String,
    pub base_model_sha256: String,
    pub scale: f32,
}

/// Immutable CAS record safe to pass to the native loader.
#[derive(Clone, Debug, PartialEq)]
pub struct VerifiedAdapterRecord {
    pub id: String,
    pub path: PathBuf,
    pub size_bytes: u64,
    pub sha256: String,
    pub base_model_sha256: String,
    pub scale: f32,
}

impl VerifiedAdapterRecord {
    /// Returns a domain-separated identity for the complete active adapter binding.
    ///
    /// The identity binds the opaque adapter ID, content and base-model digests,
    /// and the exact IEEE-754 scale bits. Registry validation guarantees both
    /// digest strings are canonical lowercase SHA-256 values before a record can
    /// become active.
    pub fn identity_sha256(&self) -> [u8; 32] {
        let mut hasher = Sha256::new();
        hasher.update(b"AMW\0active-adapter-v1\0");
        update_len_prefixed(&mut hasher, self.id.as_bytes());
        update_len_prefixed(&mut hasher, self.sha256.as_bytes());
        update_len_prefixed(&mut hasher, self.base_model_sha256.as_bytes());
        hasher.update(self.scale.to_bits().to_be_bytes());
        hasher.finalize().into()
    }
}

#[derive(Debug, Error)]
pub enum AdapterRegistryError {
    #[error("at least one approved adapter root is required")]
    EmptyRoots,
    #[error("approved adapter root is invalid or symbolic: {0}")]
    InvalidRoot(PathBuf),
    #[error("adapter content-addressed root is invalid or symbolic: {0}")]
    InvalidStore(PathBuf),
    #[error("adapter id or root id is invalid: {0}")]
    InvalidId(String),
    #[error("adapter id or root id is already registered: {0}")]
    Duplicate(String),
    #[error("adapter is not registered: {0}")]
    Unknown(String),
    #[error("adapter scale must be finite")]
    InvalidScale,
    #[error("adapter size must be positive and within the configured limit")]
    InvalidSize,
    #[error("adapter SHA-256 fields must be exactly 64 lowercase hexadecimal characters")]
    InvalidDigest,
    #[error("adapter relative path contains traversal or a non-normal component: {0}")]
    InvalidRelativePath(PathBuf),
    #[error("adapter path is missing, symbolic, or not a regular file: {0}")]
    InvalidFile(PathBuf),
    #[error("adapter path escapes its approved root: {0}")]
    PathEscape(PathBuf),
    #[error("adapter file must use the .gguf extension: {0}")]
    InvalidExtension(PathBuf),
    #[error("adapter file exceeds the configured {limit}-byte limit: {path}")]
    Oversize { path: PathBuf, limit: u64 },
    #[error("adapter content size does not match its registration: {0}")]
    SizeMismatch(PathBuf),
    #[error("adapter content digest does not match its registered SHA-256: {0}")]
    DigestMismatch(PathBuf),
    #[error("adapter is registered for a different base-model SHA-256: {0}")]
    BaseModelMismatch(String),
    #[error("adapter file identity changed while preparing or loading: {0}")]
    IdentityMismatch(PathBuf),
    #[error("same-file adapter loading is unsupported on this platform")]
    IdentityGuardUnsupported,
    #[error("adapter I/O failed for {path}: {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct FileIdentity {
    first: u64,
    second: u64,
}

/// Exclusive same-file proof retained across native adapter loading.
#[derive(Debug)]
pub struct VerifiedLoadGuard {
    record: VerifiedAdapterRecord,
    file: fs::File,
    identity: FileIdentity,
    native_path: PathBuf,
}

impl VerifiedLoadGuard {
    /// Returns the immutable catalog record bound to this open file.
    pub fn record(&self) -> &VerifiedAdapterRecord {
        &self.record
    }

    /// Returns the only path that may be passed to the native loader.
    ///
    /// Linux uses `/proc/self/fd/<fd>` so a replacement of the CAS pathname
    /// cannot redirect the native open. Windows retains a deny-write/delete
    /// handle across native load and therefore uses the locked CAS pathname.
    pub fn native_path(&self) -> &Path {
        &self.native_path
    }

    fn verify(&self) -> Result<(), AdapterRegistryError> {
        let metadata = self
            .file
            .metadata()
            .map_err(|source| AdapterRegistryError::Io {
                path: self.record.path.clone(),
                source,
            })?;
        if file_identity(&self.file, &metadata, &self.record.path)? != self.identity {
            return Err(AdapterRegistryError::IdentityMismatch(
                self.record.path.clone(),
            ));
        }
        verify_open_file(
            &self.file,
            &self.record.path,
            self.record.size_bytes,
            &self.record.sha256,
        )
    }
}

/// Sole authority mapping opaque API IDs to immutable engine-owned adapter bytes.
#[derive(Debug)]
pub struct AdapterRegistry {
    roots: BTreeMap<String, PathBuf>,
    cas_root: PathBuf,
    records: BTreeMap<String, VerifiedAdapterRecord>,
    max_adapter_bytes: u64,
}

impl AdapterRegistry {
    /// Creates an adapter authority from named import roots and a private CAS root.
    pub fn new(
        roots: impl IntoIterator<Item = (String, PathBuf)>,
        cas_root: PathBuf,
        max_adapter_bytes: u64,
    ) -> Result<Self, AdapterRegistryError> {
        Self::new_with_root_policy(roots, cas_root, max_adapter_bytes, true)
    }

    /// Opens an administrator-preprovisioned CAS root without creating it or rewriting ACLs.
    pub fn new_preprovisioned(
        roots: impl IntoIterator<Item = (String, PathBuf)>,
        cas_root: PathBuf,
        max_adapter_bytes: u64,
    ) -> Result<Self, AdapterRegistryError> {
        Self::new_with_root_policy(roots, cas_root, max_adapter_bytes, false)
    }

    fn new_with_root_policy(
        roots: impl IntoIterator<Item = (String, PathBuf)>,
        cas_root: PathBuf,
        max_adapter_bytes: u64,
        managed: bool,
    ) -> Result<Self, AdapterRegistryError> {
        if max_adapter_bytes == 0 {
            return Err(AdapterRegistryError::InvalidSize);
        }
        let mut approved = BTreeMap::new();
        for (root_id, root) in roots {
            validate_id(&root_id)?;
            let canonical = canonical_directory(&root, false)?;
            if approved.insert(root_id.clone(), canonical).is_some() {
                return Err(AdapterRegistryError::Duplicate(root_id));
            }
        }
        if approved.is_empty() {
            return Err(AdapterRegistryError::EmptyRoots);
        }
        if managed {
            fs::create_dir_all(&cas_root).map_err(|source| AdapterRegistryError::Io {
                path: cas_root.clone(),
                source,
            })?;
        }
        let cas_root = canonical_directory(&cas_root, true)?;
        if managed {
            set_owner_only_directory(&cas_root)?;
        } else {
            super::session::verify_private_directory(&cas_root).map_err(|source| {
                AdapterRegistryError::Io {
                    path: cas_root.clone(),
                    source,
                }
            })?;
        }
        Ok(Self {
            roots: approved,
            cas_root,
            records: BTreeMap::new(),
            max_adapter_bytes,
        })
    }

    /// Verifies source bytes and atomically imports them before recording the ID.
    pub fn register(
        &mut self,
        registration: AdapterRegistration,
    ) -> Result<VerifiedAdapterRecord, AdapterRegistryError> {
        validate_registration(&registration, self.max_adapter_bytes)?;
        if self.records.contains_key(&registration.id) {
            return Err(AdapterRegistryError::Duplicate(registration.id));
        }
        let root = self
            .roots
            .get(&registration.root_id)
            .ok_or_else(|| AdapterRegistryError::Unknown(registration.root_id.clone()))?;
        validate_relative_path(&registration.relative_path)?;
        let source = root.join(&registration.relative_path);
        let source = verify_source_path(root, &source)?;
        verify_file(
            &source,
            registration.size_bytes,
            &registration.sha256,
            self.max_adapter_bytes,
        )?;
        let cas_path = self.cas_root.join(format!("{}.gguf", registration.sha256));
        import_to_cas(&source, &cas_path, &registration)?;
        seal_cas_file(&cas_path)?;
        let record = VerifiedAdapterRecord {
            id: registration.id.clone(),
            path: cas_path,
            size_bytes: registration.size_bytes,
            sha256: registration.sha256,
            base_model_sha256: registration.base_model_sha256,
            scale: registration.scale,
        };
        self.records.insert(record.id.clone(), record.clone());
        Ok(record)
    }

    /// Resolves an ID and re-verifies immutable CAS bytes before native loading.
    pub fn resolve_for_base(
        &self,
        id: &str,
        base_model_sha256: &str,
    ) -> Result<VerifiedLoadGuard, AdapterRegistryError> {
        validate_id(id)?;
        validate_digest(base_model_sha256)?;
        let record = self
            .records
            .get(id)
            .ok_or_else(|| AdapterRegistryError::Unknown(id.to_owned()))?;
        if record.base_model_sha256 != base_model_sha256 {
            return Err(AdapterRegistryError::BaseModelMismatch(record.id.clone()));
        }
        let guard = open_load_guard(record.clone())?;
        guard.verify()?;
        Ok(guard)
    }

    /// Rechecks digest and size after native load and before active-state commit.
    ///
    /// This second check closes the import/load time-of-check/time-of-use window.
    pub fn verify_loaded_for_base(
        &self,
        guard: &VerifiedLoadGuard,
        base_model_sha256: &str,
    ) -> Result<(), AdapterRegistryError> {
        validate_digest(base_model_sha256)?;
        let record = guard.record();
        let authoritative = self
            .records
            .get(&record.id)
            .ok_or_else(|| AdapterRegistryError::Unknown(record.id.clone()))?;
        if authoritative != record || !record.path.starts_with(&self.cas_root) {
            return Err(AdapterRegistryError::Unknown(record.id.clone()));
        }
        if record.base_model_sha256 != base_model_sha256 {
            return Err(AdapterRegistryError::BaseModelMismatch(record.id.clone()));
        }
        guard.verify()
    }
}

fn validate_registration(
    registration: &AdapterRegistration,
    maximum: u64,
) -> Result<(), AdapterRegistryError> {
    validate_id(&registration.id)?;
    validate_id(&registration.root_id)?;
    validate_relative_path(&registration.relative_path)?;
    if !registration.scale.is_finite() {
        return Err(AdapterRegistryError::InvalidScale);
    }
    if registration.size_bytes == 0 || registration.size_bytes > maximum {
        return Err(AdapterRegistryError::InvalidSize);
    }
    validate_digest(&registration.sha256)?;
    validate_digest(&registration.base_model_sha256)
}

fn update_len_prefixed(hasher: &mut Sha256, value: &[u8]) {
    let length = u32::try_from(value.len()).expect("validated adapter identity field fits u32");
    hasher.update(length.to_be_bytes());
    hasher.update(value);
}

fn validate_id(id: &str) -> Result<(), AdapterRegistryError> {
    if id.is_empty()
        || id.trim() != id
        || id == "."
        || id == ".."
        || id.contains('/')
        || id.contains('\\')
        || id.chars().any(char::is_control)
    {
        Err(AdapterRegistryError::InvalidId(id.to_owned()))
    } else {
        Ok(())
    }
}

fn validate_relative_path(path: &Path) -> Result<(), AdapterRegistryError> {
    if path.as_os_str().is_empty()
        || path.is_absolute()
        || path
            .components()
            .any(|component| !matches!(component, Component::Normal(_)))
    {
        return Err(AdapterRegistryError::InvalidRelativePath(path.to_owned()));
    }
    if path
        .extension()
        .and_then(|extension| extension.to_str())
        .is_none_or(|extension| !extension.eq_ignore_ascii_case("gguf"))
    {
        return Err(AdapterRegistryError::InvalidExtension(path.to_owned()));
    }
    Ok(())
}

fn validate_digest(digest: &str) -> Result<(), AdapterRegistryError> {
    if digest.len() == 64
        && digest
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
    {
        Ok(())
    } else {
        Err(AdapterRegistryError::InvalidDigest)
    }
}

fn canonical_directory(path: &Path, store: bool) -> Result<PathBuf, AdapterRegistryError> {
    let metadata = fs::symlink_metadata(path).map_err(|_| {
        if store {
            AdapterRegistryError::InvalidStore(path.to_owned())
        } else {
            AdapterRegistryError::InvalidRoot(path.to_owned())
        }
    })?;
    if metadata.file_type().is_symlink() || !metadata.is_dir() {
        return Err(if store {
            AdapterRegistryError::InvalidStore(path.to_owned())
        } else {
            AdapterRegistryError::InvalidRoot(path.to_owned())
        });
    }
    path.canonicalize().map_err(|_| {
        if store {
            AdapterRegistryError::InvalidStore(path.to_owned())
        } else {
            AdapterRegistryError::InvalidRoot(path.to_owned())
        }
    })
}

fn verify_source_path(root: &Path, source: &Path) -> Result<PathBuf, AdapterRegistryError> {
    let mut current = root.to_owned();
    let relative = source
        .strip_prefix(root)
        .map_err(|_| AdapterRegistryError::PathEscape(source.to_owned()))?;
    for component in relative.components() {
        current.push(component);
        let metadata = fs::symlink_metadata(&current)
            .map_err(|_| AdapterRegistryError::InvalidFile(current.clone()))?;
        if metadata.file_type().is_symlink() {
            return Err(AdapterRegistryError::InvalidFile(current));
        }
    }
    let canonical = source
        .canonicalize()
        .map_err(|_| AdapterRegistryError::InvalidFile(source.to_owned()))?;
    if !canonical.starts_with(root) {
        return Err(AdapterRegistryError::PathEscape(canonical));
    }
    if !canonical.is_file() {
        return Err(AdapterRegistryError::InvalidFile(canonical));
    }
    Ok(canonical)
}

fn verify_file(
    path: &Path,
    expected_size: u64,
    expected_digest: &str,
    maximum: u64,
) -> Result<(), AdapterRegistryError> {
    let metadata = fs::symlink_metadata(path)
        .map_err(|_| AdapterRegistryError::InvalidFile(path.to_owned()))?;
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(AdapterRegistryError::InvalidFile(path.to_owned()));
    }
    if metadata.len() > maximum {
        return Err(AdapterRegistryError::Oversize {
            path: path.to_owned(),
            limit: maximum,
        });
    }
    if metadata.len() != expected_size {
        return Err(AdapterRegistryError::SizeMismatch(path.to_owned()));
    }
    if digest_file(path)? != expected_digest {
        return Err(AdapterRegistryError::DigestMismatch(path.to_owned()));
    }
    Ok(())
}

fn verify_open_file(
    file: &fs::File,
    path: &Path,
    expected_size: u64,
    expected_digest: &str,
) -> Result<(), AdapterRegistryError> {
    let metadata = file.metadata().map_err(|source| AdapterRegistryError::Io {
        path: path.to_owned(),
        source,
    })?;
    if !metadata.is_file() || metadata.len() != expected_size {
        return Err(AdapterRegistryError::SizeMismatch(path.to_owned()));
    }
    let mut reader = file
        .try_clone()
        .map_err(|source| AdapterRegistryError::Io {
            path: path.to_owned(),
            source,
        })?;
    reader
        .seek(SeekFrom::Start(0))
        .map_err(|source| AdapterRegistryError::Io {
            path: path.to_owned(),
            source,
        })?;
    let mut limited = BufReader::new(reader).take(expected_size + 1);
    let mut hasher = Sha256::new();
    let mut total = 0_u64;
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = limited
            .read(&mut buffer)
            .map_err(|source| AdapterRegistryError::Io {
                path: path.to_owned(),
                source,
            })?;
        if read == 0 {
            break;
        }
        total = total.saturating_add(read as u64);
        hasher.update(&buffer[..read]);
    }
    if total != expected_size {
        return Err(AdapterRegistryError::SizeMismatch(path.to_owned()));
    }
    if format!("{:x}", hasher.finalize()) != expected_digest {
        return Err(AdapterRegistryError::DigestMismatch(path.to_owned()));
    }
    Ok(())
}

#[cfg(target_os = "linux")]
fn open_load_guard(
    record: VerifiedAdapterRecord,
) -> Result<VerifiedLoadGuard, AdapterRegistryError> {
    use std::os::{fd::AsRawFd, unix::fs::OpenOptionsExt};

    const O_NOFOLLOW: i32 = 0x0002_0000;
    let file = fs::OpenOptions::new()
        .read(true)
        .custom_flags(O_NOFOLLOW)
        .open(&record.path)
        .map_err(|source| AdapterRegistryError::Io {
            path: record.path.clone(),
            source,
        })?;
    let metadata = file.metadata().map_err(|source| AdapterRegistryError::Io {
        path: record.path.clone(),
        source,
    })?;
    let identity = file_identity(&file, &metadata, &record.path)?;
    let native_path = PathBuf::from(format!("/proc/self/fd/{}", file.as_raw_fd()));
    if !native_path.exists() {
        return Err(AdapterRegistryError::IdentityGuardUnsupported);
    }
    Ok(VerifiedLoadGuard {
        record,
        file,
        identity,
        native_path,
    })
}

#[cfg(windows)]
fn open_load_guard(
    record: VerifiedAdapterRecord,
) -> Result<VerifiedLoadGuard, AdapterRegistryError> {
    use std::os::windows::fs::OpenOptionsExt;
    use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;

    let file = fs::OpenOptions::new()
        .read(true)
        .share_mode(FILE_SHARE_READ)
        .open(&record.path)
        .map_err(|source| AdapterRegistryError::Io {
            path: record.path.clone(),
            source,
        })?;
    let metadata = file.metadata().map_err(|source| AdapterRegistryError::Io {
        path: record.path.clone(),
        source,
    })?;
    let identity = file_identity(&file, &metadata, &record.path)?;
    let native_path = record.path.clone();
    Ok(VerifiedLoadGuard {
        record,
        file,
        identity,
        native_path,
    })
}

#[cfg(not(any(target_os = "linux", windows)))]
fn open_load_guard(
    _record: VerifiedAdapterRecord,
) -> Result<VerifiedLoadGuard, AdapterRegistryError> {
    Err(AdapterRegistryError::IdentityGuardUnsupported)
}

#[cfg(unix)]
fn file_identity(
    _file: &fs::File,
    metadata: &fs::Metadata,
    _path: &Path,
) -> Result<FileIdentity, AdapterRegistryError> {
    use std::os::unix::fs::MetadataExt;
    Ok(FileIdentity {
        first: metadata.dev(),
        second: metadata.ino(),
    })
}

#[cfg(windows)]
fn file_identity(
    file: &fs::File,
    _metadata: &fs::Metadata,
    path: &Path,
) -> Result<FileIdentity, AdapterRegistryError> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{
        GetFileInformationByHandle, BY_HANDLE_FILE_INFORMATION,
    };

    let mut information = BY_HANDLE_FILE_INFORMATION::default();
    // SAFETY: the File owns a live handle and information points to writable storage.
    if unsafe { GetFileInformationByHandle(file.as_raw_handle() as _, &mut information) } == 0 {
        return Err(AdapterRegistryError::Io {
            path: path.to_owned(),
            source: std::io::Error::last_os_error(),
        });
    }
    Ok(FileIdentity {
        first: u64::from(information.dwVolumeSerialNumber),
        second: (u64::from(information.nFileIndexHigh) << 32)
            | u64::from(information.nFileIndexLow),
    })
}

#[cfg(not(any(unix, windows)))]
fn file_identity(
    _file: &fs::File,
    _metadata: &fs::Metadata,
    _path: &Path,
) -> Result<FileIdentity, AdapterRegistryError> {
    Err(AdapterRegistryError::IdentityGuardUnsupported)
}

fn import_to_cas(
    source: &Path,
    destination: &Path,
    registration: &AdapterRegistration,
) -> Result<(), AdapterRegistryError> {
    if destination.exists() {
        return verify_file(
            destination,
            registration.size_bytes,
            &registration.sha256,
            registration.size_bytes,
        );
    }
    let temporary = destination.with_extension(format!("{}.tmp", std::process::id()));
    let source_file = fs::File::open(source).map_err(|source_error| AdapterRegistryError::Io {
        path: source.to_owned(),
        source: source_error,
    })?;
    let destination_file = fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&temporary)
        .map_err(|source_error| AdapterRegistryError::Io {
            path: temporary.clone(),
            source: source_error,
        })?;
    let result = (|| {
        set_owner_only_file(&temporary)?;
        let mut reader = BufReader::new(source_file).take(registration.size_bytes + 1);
        let mut writer = BufWriter::new(destination_file);
        let copied = std::io::copy(&mut reader, &mut writer).map_err(|source_error| {
            AdapterRegistryError::Io {
                path: temporary.clone(),
                source: source_error,
            }
        })?;
        if copied != registration.size_bytes {
            return Err(AdapterRegistryError::SizeMismatch(temporary.clone()));
        }
        writer
            .flush()
            .map_err(|source_error| AdapterRegistryError::Io {
                path: temporary.clone(),
                source: source_error,
            })?;
        writer
            .get_ref()
            .sync_all()
            .map_err(|source_error| AdapterRegistryError::Io {
                path: temporary.clone(),
                source: source_error,
            })?;
        drop(writer);
        verify_file(
            &temporary,
            registration.size_bytes,
            &registration.sha256,
            registration.size_bytes,
        )?;
        fs::rename(&temporary, destination).map_err(|source_error| AdapterRegistryError::Io {
            path: destination.to_owned(),
            source: source_error,
        })
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result
}

fn digest_file(path: &Path) -> Result<String, AdapterRegistryError> {
    let file = fs::File::open(path).map_err(|source| AdapterRegistryError::Io {
        path: path.to_owned(),
        source,
    })?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = reader
            .read(&mut buffer)
            .map_err(|source| AdapterRegistryError::Io {
                path: path.to_owned(),
                source,
            })?;
        if read == 0 {
            break;
        }
        hasher.update(&buffer[..read]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

fn set_owner_only_directory(path: &Path) -> Result<(), AdapterRegistryError> {
    super::session::secure_and_verify_private_path(path).map_err(|source| {
        AdapterRegistryError::Io {
            path: path.to_owned(),
            source,
        }
    })
}

fn set_owner_only_file(path: &Path) -> Result<(), AdapterRegistryError> {
    super::session::secure_and_verify_private_path(path).map_err(|source| {
        AdapterRegistryError::Io {
            path: path.to_owned(),
            source,
        }
    })
}

#[cfg(unix)]
fn seal_cas_file(path: &Path) -> Result<(), AdapterRegistryError> {
    use std::os::unix::fs::PermissionsExt;
    fs::set_permissions(path, fs::Permissions::from_mode(0o400)).map_err(|source| {
        AdapterRegistryError::Io {
            path: path.to_owned(),
            source,
        }
    })?;
    let mode = fs::symlink_metadata(path)
        .map_err(|source| AdapterRegistryError::Io {
            path: path.to_owned(),
            source,
        })?
        .permissions()
        .mode()
        & 0o777;
    if mode != 0o400 {
        return Err(AdapterRegistryError::IdentityMismatch(path.to_owned()));
    }
    Ok(())
}

#[cfg(windows)]
fn seal_cas_file(path: &Path) -> Result<(), AdapterRegistryError> {
    set_owner_only_file(path)
}

#[cfg(not(any(unix, windows)))]
fn seal_cas_file(_path: &Path) -> Result<(), AdapterRegistryError> {
    Err(AdapterRegistryError::IdentityGuardUnsupported)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registration(id: &str, relative_path: &str, bytes: &[u8]) -> AdapterRegistration {
        AdapterRegistration {
            id: id.to_owned(),
            root_id: "imports".to_owned(),
            relative_path: PathBuf::from(relative_path),
            size_bytes: bytes.len() as u64,
            sha256: format!("{:x}", Sha256::digest(bytes)),
            base_model_sha256: "a".repeat(64),
            scale: 0.75,
        }
    }

    #[test]
    fn registration_imports_to_cas_and_resolve_rechecks_owned_bytes() {
        let root = tempfile::tempdir().unwrap();
        let cas = tempfile::tempdir().unwrap();
        let bytes = b"adapter bytes";
        fs::write(root.path().join("adapter.gguf"), bytes).unwrap();
        let mut registry = AdapterRegistry::new(
            [("imports".to_owned(), root.path().to_owned())],
            cas.path().to_owned(),
            DEFAULT_MAX_ADAPTER_BYTES,
        )
        .unwrap();
        let record = registry
            .register(registration("approved", "adapter.gguf", bytes))
            .unwrap();
        assert!(record.path.starts_with(cas.path().canonicalize().unwrap()));
        assert_ne!(record.path, root.path().join("adapter.gguf"));
        let guard = registry
            .resolve_for_base("approved", &"a".repeat(64))
            .unwrap();
        assert_eq!(guard.record(), &record);
        assert!(fs::write(&record.path, b"racing mutation").is_err());
        registry
            .verify_loaded_for_base(&guard, &"a".repeat(64))
            .unwrap();
        assert!(matches!(
            registry.resolve_for_base("../adapter", &"a".repeat(64)),
            Err(AdapterRegistryError::InvalidId(_))
        ));
        assert!(matches!(
            registry.resolve_for_base("approved", &"b".repeat(64)),
            Err(AdapterRegistryError::BaseModelMismatch(_))
        ));
        drop(guard);
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(&record.path, fs::Permissions::from_mode(0o600)).unwrap();
        }
        fs::write(&record.path, b"mutated bytes").unwrap();
        assert!(matches!(
            registry.resolve_for_base("approved", &"a".repeat(64)),
            Err(AdapterRegistryError::SizeMismatch(_))
                | Err(AdapterRegistryError::DigestMismatch(_))
        ));
    }

    #[test]
    fn traversal_extension_size_digest_and_base_digest_fail_before_import() {
        let root = tempfile::tempdir().unwrap();
        let cas = tempfile::tempdir().unwrap();
        fs::write(root.path().join("valid.gguf"), b"original").unwrap();
        fs::write(root.path().join("wrong.bin"), b"original").unwrap();
        let mut registry = AdapterRegistry::new(
            [("imports".to_owned(), root.path().to_owned())],
            cas.path().to_owned(),
            8,
        )
        .unwrap();
        let mut traversal = registration("traversal", "../valid.gguf", b"original");
        assert!(matches!(
            registry.register(traversal.clone()),
            Err(AdapterRegistryError::InvalidRelativePath(_))
        ));
        traversal.id = "extension".to_owned();
        traversal.relative_path = PathBuf::from("wrong.bin");
        assert!(matches!(
            registry.register(traversal.clone()),
            Err(AdapterRegistryError::InvalidExtension(_))
        ));
        traversal.id = "size".to_owned();
        traversal.relative_path = PathBuf::from("valid.gguf");
        traversal.size_bytes = 7;
        assert!(matches!(
            registry.register(traversal.clone()),
            Err(AdapterRegistryError::SizeMismatch(_))
        ));
        traversal.id = "digest".to_owned();
        traversal.size_bytes = 8;
        traversal.sha256 = "b".repeat(64);
        assert!(matches!(
            registry.register(traversal.clone()),
            Err(AdapterRegistryError::DigestMismatch(_))
        ));
        traversal.id = "base".to_owned();
        traversal.base_model_sha256 = "invalid".to_owned();
        assert!(matches!(
            registry.register(traversal),
            Err(AdapterRegistryError::InvalidDigest)
        ));
    }

    #[test]
    fn adapter_identity_binds_scale_and_base_model() {
        let record = VerifiedAdapterRecord {
            id: "adapter-a".to_owned(),
            path: PathBuf::from("unused.gguf"),
            size_bytes: 4,
            sha256: "a".repeat(64),
            base_model_sha256: "b".repeat(64),
            scale: 0.75,
        };
        let mut changed_scale = record.clone();
        changed_scale.scale = 0.5;
        let mut changed_base = record.clone();
        changed_base.base_model_sha256 = "c".repeat(64);

        assert_ne!(record.identity_sha256(), changed_scale.identity_sha256());
        assert_ne!(record.identity_sha256(), changed_base.identity_sha256());
    }

    #[test]
    fn post_verification_source_growth_is_bounded_and_cleans_temporary_bytes() {
        let root = tempfile::tempdir().unwrap();
        let cas = tempfile::tempdir().unwrap();
        let source = root.path().join("growing.gguf");
        fs::write(&source, vec![b'x'; 1024 * 1024]).unwrap();
        let expected = b"x";
        let registration = registration("bounded", "growing.gguf", expected);
        let destination = cas.path().join(format!("{}.gguf", registration.sha256));

        assert!(matches!(
            import_to_cas(&source, &destination, &registration),
            Err(AdapterRegistryError::SizeMismatch(_))
        ));
        assert!(!destination.exists());
        assert_eq!(fs::read_dir(cas.path()).unwrap().count(), 0);
    }
}
