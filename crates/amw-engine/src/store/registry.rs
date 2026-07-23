//! Traversal-safe model catalog with bounded GGUF bootstrap and atomic sidecars.

use std::{
    collections::{BTreeMap, BTreeSet},
    ffi::OsString,
    fs,
    io::Write,
    path::{Path, PathBuf},
};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::{
    gguf_meta::{inspect_gguf, GgufMetadata, IntegrityError},
    scan::{discover_gguf_roots, ScanError, ScanLimits},
};

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct DraftPair {
    pub draft_model_id: String,
    pub minimum_context: Option<u32>,
    /// SHA-256 native vocabulary-semantic fingerprint shared by target and draft.
    #[serde(default)]
    pub vocabulary_fingerprint: Option<String>,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct ModelRecord {
    pub id: String,
    pub path: PathBuf,
    #[serde(default)]
    pub aliases: Vec<String>,
    pub draft_pair: Option<DraftPair>,
}

/// One catalog entry presented to API and runtime consumers.
#[derive(Clone, Copy, Debug)]
pub struct CatalogEntry<'a> {
    pub record: &'a ModelRecord,
    pub metadata: &'a GgufMetadata,
}

/// Owned API-facing catalog summary without exposing mutable registry state.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CatalogModel {
    pub id: String,
    pub aliases: Vec<String>,
    pub architecture: Option<String>,
    pub quantization: Option<String>,
    pub context_length: Option<u64>,
    pub embedding_length: Option<u64>,
    pub supports_embeddings: bool,
    pub supports_fim: bool,
}

/// One isolated catalog entry that could not be admitted during bootstrap.
#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct CatalogDiagnostic {
    pub path: PathBuf,
    pub kind: &'static str,
    pub detail: String,
}

#[derive(Debug, Error)]
pub enum RegistryError {
    #[error("model id or alias is invalid: {0}")]
    InvalidId(String),
    #[error("at least one configured model directory is required")]
    EmptyRoots,
    #[error("configured model root is not a directory: {0}")]
    InvalidRoot(PathBuf),
    #[error("model path escapes configured registry roots: {0}")]
    PathEscape(PathBuf),
    #[error("model path is not a regular file: {0}")]
    MissingModel(PathBuf),
    #[error("model id or alias is already registered: {0}")]
    Duplicate(String),
    #[error("model catalog scan failed: {0}")]
    Scan(#[from] ScanError),
    #[error("GGUF model is corrupt or unreadable at {path}: {source}")]
    Integrity {
        path: PathBuf,
        #[source]
        source: IntegrityError,
    },
    #[error("registry sidecar for {model_path} names a different path: {declared_path}")]
    SidecarPathMismatch {
        model_path: PathBuf,
        declared_path: PathBuf,
    },
    #[error("registry I/O failed for {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("registry metadata is invalid for {path}: {source}")]
    Metadata {
        path: PathBuf,
        source: serde_json::Error,
    },
}

#[derive(Debug)]
pub struct ModelRegistry {
    roots: Vec<PathBuf>,
    records: BTreeMap<String, ModelRecord>,
    aliases: BTreeMap<String, String>,
    metadata: BTreeMap<String, GgufMetadata>,
    diagnostics: Vec<CatalogDiagnostic>,
}

impl ModelRegistry {
    /// Creates a writable single-root registry, preserving the original registration API.
    pub fn new(root: impl Into<PathBuf>) -> Result<Self, RegistryError> {
        let root = root.into();
        fs::create_dir_all(&root).map_err(|source| RegistryError::Io {
            path: root.clone(),
            source,
        })?;
        let root = canonical_directory(&root)?;
        Ok(Self::empty(vec![root]))
    }

    /// Bootstraps a bounded catalog from the configured local model directories.
    pub fn bootstrap(
        roots: impl IntoIterator<Item = PathBuf>,
        limits: ScanLimits,
    ) -> Result<Self, RegistryError> {
        let mut unique_roots = BTreeSet::new();
        for root in roots {
            unique_roots.insert(canonical_directory(&root)?);
        }
        if unique_roots.is_empty() {
            return Err(RegistryError::EmptyRoots);
        }
        let roots: Vec<_> = unique_roots.into_iter().collect();
        let models = discover_gguf_roots(roots.clone(), limits)?;
        let mut registry = Self::empty(roots);
        for model_path in models {
            if let Err(error) = registry.bootstrap_model(&model_path) {
                registry.diagnostics.push(CatalogDiagnostic {
                    path: model_path,
                    kind: registry_error_kind(&error),
                    detail: error.to_string(),
                });
            }
        }
        Ok(registry)
    }

    pub fn register(&mut self, mut record: ModelRecord) -> Result<(), RegistryError> {
        let canonical = self.validate_record_path(&record.path)?;
        let metadata = inspect_gguf(&canonical).map_err(|source| RegistryError::Integrity {
            path: canonical.clone(),
            source,
        })?;
        record.path = canonical;
        self.validate_record_names(&record)?;
        self.ensure_available_names(&record)?;
        write_atomic_json(&metadata_sidecar_path(&record.path), &record)?;
        self.insert(record, metadata);
        Ok(())
    }

    /// Resolves a canonical id or alias; malformed path-like names never reach lookup.
    pub fn resolve(&self, id_or_alias: &str) -> Option<&ModelRecord> {
        self.try_resolve(id_or_alias).ok().flatten()
    }

    /// Typed variant of [`Self::resolve`] for API boundaries that must report bad ids.
    pub fn try_resolve(&self, id_or_alias: &str) -> Result<Option<&ModelRecord>, RegistryError> {
        validate_id(id_or_alias)?;
        Ok(self.records.get(id_or_alias).or_else(|| {
            self.aliases
                .get(id_or_alias)
                .and_then(|id| self.records.get(id))
        }))
    }

    pub fn metadata(&self, id_or_alias: &str) -> Result<Option<&GgufMetadata>, RegistryError> {
        let Some(record) = self.try_resolve(id_or_alias)? else {
            return Ok(None);
        };
        Ok(self.metadata.get(&record.id))
    }

    pub fn iter(&self) -> impl ExactSizeIterator<Item = &ModelRecord> {
        self.records.values()
    }

    pub fn entries(&self) -> impl ExactSizeIterator<Item = CatalogEntry<'_>> {
        self.records.iter().map(|(id, record)| CatalogEntry {
            record,
            metadata: self
                .metadata
                .get(id)
                .expect("catalog metadata is inserted atomically with every record"),
        })
    }

    pub fn list(&self) -> Vec<CatalogModel> {
        self.entries()
            .map(|entry| CatalogModel {
                id: entry.record.id.clone(),
                aliases: entry.record.aliases.clone(),
                architecture: entry.metadata.architecture.clone(),
                quantization: entry.metadata.quantization.clone(),
                context_length: entry.metadata.context_length,
                embedding_length: entry.metadata.embedding_length,
                supports_embeddings: entry.metadata.supports_embeddings,
                supports_fim: entry.metadata.supports_fim,
            })
            .collect()
    }

    pub fn len(&self) -> usize {
        self.records.len()
    }

    pub fn is_empty(&self) -> bool {
        self.records.is_empty()
    }

    pub fn roots(&self) -> &[PathBuf] {
        &self.roots
    }

    /// Returns entry-local bootstrap failures without exposing them to selection.
    pub fn diagnostics(&self) -> &[CatalogDiagnostic] {
        &self.diagnostics
    }

    pub fn load_sidecar(&mut self, model_path: &Path) -> Result<(), RegistryError> {
        let canonical = self.validate_record_path(model_path)?;
        let mut record = read_sidecar_for(&canonical)?;
        validate_declared_path(&canonical, &record.path)?;
        record.path = canonical.clone();
        let metadata = inspect_gguf(&canonical).map_err(|source| RegistryError::Integrity {
            path: canonical,
            source,
        })?;
        self.validate_record_names(&record)?;
        self.ensure_available_names(&record)?;
        self.insert(record, metadata);
        Ok(())
    }

    fn empty(roots: Vec<PathBuf>) -> Self {
        Self {
            roots,
            records: BTreeMap::new(),
            aliases: BTreeMap::new(),
            metadata: BTreeMap::new(),
            diagnostics: Vec::new(),
        }
    }

    fn bootstrap_model(&mut self, model_path: &Path) -> Result<(), RegistryError> {
        let canonical = self.validate_record_path(model_path)?;
        let metadata = inspect_gguf(&canonical).map_err(|source| RegistryError::Integrity {
            path: canonical.clone(),
            source,
        })?;
        let mut record = if sidecar_for(&canonical).is_some() {
            let record = read_sidecar_for(&canonical)?;
            validate_declared_path(&canonical, &record.path)?;
            record
        } else {
            inferred_record(&canonical, &metadata)?
        };
        record.path = canonical;
        self.validate_record_names(&record)?;
        self.ensure_available_names(&record)?;
        self.insert(record, metadata);
        Ok(())
    }

    fn validate_record_path(&self, path: &Path) -> Result<PathBuf, RegistryError> {
        let canonical = path
            .canonicalize()
            .map_err(|_| RegistryError::MissingModel(path.to_owned()))?;
        if !canonical.is_file() {
            return Err(RegistryError::MissingModel(canonical));
        }
        if !self.roots.iter().any(|root| canonical.starts_with(root)) {
            return Err(RegistryError::PathEscape(canonical));
        }
        Ok(canonical)
    }

    fn validate_record_names(&self, record: &ModelRecord) -> Result<(), RegistryError> {
        validate_id(&record.id)?;
        for alias in &record.aliases {
            validate_id(alias)?;
            if alias == &record.id {
                return Err(RegistryError::Duplicate(alias.clone()));
            }
        }
        Ok(())
    }

    fn ensure_available_names(&self, record: &ModelRecord) -> Result<(), RegistryError> {
        if self.records.contains_key(&record.id) || self.aliases.contains_key(&record.id) {
            return Err(RegistryError::Duplicate(record.id.clone()));
        }
        let mut local = BTreeSet::new();
        for alias in &record.aliases {
            if !local.insert(alias)
                || self.records.contains_key(alias)
                || self.aliases.contains_key(alias)
            {
                return Err(RegistryError::Duplicate(alias.clone()));
            }
        }
        Ok(())
    }

    fn insert(&mut self, record: ModelRecord, metadata: GgufMetadata) {
        for alias in &record.aliases {
            self.aliases.insert(alias.clone(), record.id.clone());
        }
        self.metadata.insert(record.id.clone(), metadata);
        self.records.insert(record.id.clone(), record);
    }
}

fn registry_error_kind(error: &RegistryError) -> &'static str {
    match error {
        RegistryError::Integrity { .. } => "integrity",
        RegistryError::Metadata { .. } => "sidecar_metadata",
        RegistryError::SidecarPathMismatch { .. } => "sidecar_path",
        RegistryError::Duplicate(_) => "duplicate_name",
        RegistryError::InvalidId(_) => "invalid_id",
        RegistryError::PathEscape(_) => "path_escape",
        RegistryError::MissingModel(_) => "missing_model",
        RegistryError::Io { .. } => "io",
        RegistryError::EmptyRoots | RegistryError::InvalidRoot(_) | RegistryError::Scan(_) => {
            "registry"
        }
    }
}

pub fn metadata_sidecar_path(model_path: &Path) -> PathBuf {
    let mut name = OsString::from(model_path.as_os_str());
    name.push(".meta.json");
    PathBuf::from(name)
}

fn legacy_metadata_sidecar_path(model_path: &Path) -> PathBuf {
    model_path.with_extension("meta.json")
}

fn sidecar_for(model_path: &Path) -> Option<PathBuf> {
    let current = metadata_sidecar_path(model_path);
    if current.is_file() {
        return Some(current);
    }
    let legacy = legacy_metadata_sidecar_path(model_path);
    legacy.is_file().then_some(legacy)
}

fn read_sidecar_for(model_path: &Path) -> Result<ModelRecord, RegistryError> {
    let sidecar = sidecar_for(model_path).unwrap_or_else(|| metadata_sidecar_path(model_path));
    let bytes = fs::read(&sidecar).map_err(|source| RegistryError::Io {
        path: sidecar.clone(),
        source,
    })?;
    serde_json::from_slice(&bytes).map_err(|source| RegistryError::Metadata {
        path: sidecar,
        source,
    })
}

fn inferred_record(path: &Path, metadata: &GgufMetadata) -> Result<ModelRecord, RegistryError> {
    let id = path
        .file_stem()
        .and_then(|value| value.to_str())
        .ok_or_else(|| RegistryError::InvalidId(path.display().to_string()))?
        .to_owned();
    validate_id(&id)?;
    let aliases = metadata
        .model_name
        .as_ref()
        .filter(|alias| alias.as_str() != id.as_str() && validate_id(alias).is_ok())
        .cloned()
        .into_iter()
        .collect();
    Ok(ModelRecord {
        id,
        path: path.to_owned(),
        aliases,
        draft_pair: None,
    })
}

fn validate_declared_path(model_path: &Path, declared_path: &Path) -> Result<(), RegistryError> {
    let declared = declared_path
        .canonicalize()
        .map_err(|_| RegistryError::MissingModel(declared_path.to_owned()))?;
    if declared == model_path {
        Ok(())
    } else {
        Err(RegistryError::SidecarPathMismatch {
            model_path: model_path.to_owned(),
            declared_path: declared,
        })
    }
}

fn canonical_directory(path: &Path) -> Result<PathBuf, RegistryError> {
    let metadata = fs::symlink_metadata(path).map_err(|source| RegistryError::Io {
        path: path.to_owned(),
        source,
    })?;
    if metadata.file_type().is_symlink() {
        return Err(RegistryError::InvalidRoot(path.to_owned()));
    }
    let canonical = path.canonicalize().map_err(|source| RegistryError::Io {
        path: path.to_owned(),
        source,
    })?;
    if !canonical.is_dir() {
        return Err(RegistryError::InvalidRoot(canonical));
    }
    Ok(canonical)
}

fn validate_id(id: &str) -> Result<(), RegistryError> {
    if id.is_empty()
        || id.trim() != id
        || id == "."
        || id == ".."
        || id.contains('/')
        || id.contains('\\')
        || id.chars().any(char::is_control)
    {
        Err(RegistryError::InvalidId(id.to_owned()))
    } else {
        Ok(())
    }
}

fn write_atomic_json(path: &Path, value: &impl Serialize) -> Result<(), RegistryError> {
    let bytes = serde_json::to_vec_pretty(value).map_err(|source| RegistryError::Metadata {
        path: path.to_owned(),
        source,
    })?;
    let temporary = path.with_extension("json.tmp");
    let mut file = fs::File::create(&temporary).map_err(|source| RegistryError::Io {
        path: temporary.clone(),
        source,
    })?;
    file.write_all(&bytes).map_err(|source| RegistryError::Io {
        path: temporary.clone(),
        source,
    })?;
    file.sync_all().map_err(|source| RegistryError::Io {
        path: temporary.clone(),
        source,
    })?;
    fs::rename(&temporary, path).map_err(|source| RegistryError::Io {
        path: path.to_owned(),
        source,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture(name: &str) -> PathBuf {
        Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests")
            .join("fixtures")
            .join(name)
    }

    #[test]
    fn aliases_round_trip_and_traversal_is_refused() {
        let root = tempfile::tempdir().unwrap();
        let model = root.path().join("model.gguf");
        fs::copy(fixture("tiny-cpu.gguf"), &model).unwrap();
        let mut registry = ModelRegistry::new(root.path()).unwrap();
        registry
            .register(ModelRecord {
                id: "model".to_owned(),
                path: model.clone(),
                aliases: vec!["friendly".to_owned()],
                draft_pair: Some(DraftPair {
                    draft_model_id: "draft".to_owned(),
                    minimum_context: Some(4096),
                    vocabulary_fingerprint: Some("a".repeat(64)),
                }),
            })
            .unwrap();
        assert_eq!(registry.resolve("friendly").unwrap().id, "model");
        assert!(metadata_sidecar_path(&model).is_file());
        assert!(matches!(
            registry.try_resolve("../escape"),
            Err(RegistryError::InvalidId(_))
        ));
        assert!(matches!(
            registry.register(ModelRecord {
                id: "outside".to_owned(),
                path: fixture("tiny-cpu.gguf"),
                aliases: Vec::new(),
                draft_pair: None,
            }),
            Err(RegistryError::PathEscape(_))
        ));
    }

    #[test]
    fn bootstrap_discovers_metadata_alias_and_iterates() {
        let root = tempfile::tempdir().unwrap();
        let model = root.path().join("local.gguf");
        fs::copy(fixture("tiny-cpu.gguf"), &model).unwrap();
        let registry =
            ModelRegistry::bootstrap([root.path().to_owned()], ScanLimits::default()).unwrap();
        assert_eq!(registry.len(), 1);
        assert_eq!(registry.resolve("tiny-cpu").unwrap().id, "local");
        let entry = registry.entries().next().unwrap();
        assert_eq!(entry.metadata.architecture.as_deref(), Some("amw-test"));
        assert_eq!(registry.list()[0].id, "local");
        assert_eq!(
            registry.iter().map(|record| &record.id).collect::<Vec<_>>(),
            ["local"]
        );
    }

    #[test]
    fn bootstrap_isolates_corrupt_models_and_keeps_valid_siblings_across_restart() {
        let root = tempfile::tempdir().unwrap();
        let bad = root.path().join("bad.gguf");
        fs::write(&bad, b"not gguf").unwrap();
        fs::copy(fixture("tiny-cpu.gguf"), root.path().join("good.gguf")).unwrap();
        for restart in 0..2 {
            let registry =
                ModelRegistry::bootstrap([root.path().to_owned()], ScanLimits::default()).unwrap();
            assert_eq!(registry.len(), 1, "restart {restart}");
            assert_eq!(registry.resolve("good").unwrap().id, "good");
            assert!(registry.resolve("bad").is_none());
            assert_eq!(registry.diagnostics().len(), 1);
            assert_eq!(
                registry.diagnostics()[0].path,
                bad.canonicalize().unwrap(),
                "diagnostics retain the same canonical path used by admission"
            );
            assert_eq!(registry.diagnostics()[0].kind, "integrity");
        }
    }

    #[test]
    fn bootstrap_refuses_sidecar_path_substitution() {
        let root = tempfile::tempdir().unwrap();
        let model = root.path().join("local.gguf");
        fs::copy(fixture("tiny-cpu.gguf"), &model).unwrap();
        let sidecar = ModelRecord {
            id: "local".to_owned(),
            path: fixture("tiny-cpu.gguf"),
            aliases: Vec::new(),
            draft_pair: None,
        };
        fs::write(
            metadata_sidecar_path(&model),
            serde_json::to_vec(&sidecar).unwrap(),
        )
        .unwrap();
        let registry =
            ModelRegistry::bootstrap([root.path().to_owned()], ScanLimits::default()).unwrap();
        assert!(registry.is_empty());
        assert_eq!(registry.diagnostics().len(), 1);
        assert_eq!(registry.diagnostics()[0].kind, "sidecar_path");
    }
}
