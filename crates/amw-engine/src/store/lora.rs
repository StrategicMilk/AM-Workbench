//! LoRA catalog and hot-swap state retaining real worker/native resource handles.

use std::{any::Any, collections::BTreeMap, path::PathBuf};

use serde::{Deserialize, Serialize};
use thiserror::Error;

use super::loader::LoadedResource;

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct LoraSpec {
    pub id: String,
    pub path: PathBuf,
    pub scale: f32,
}

#[derive(Debug)]
struct LoraEntry {
    spec: LoraSpec,
    resource: Option<LoadedResource>,
}

/// Borrowed applied-adapter view for an owning model worker.
#[derive(Clone, Copy, Debug)]
pub struct ActiveLora<'a> {
    pub spec: &'a LoraSpec,
    pub resource: &'a LoadedResource,
}

#[derive(Debug, Error, Eq, PartialEq)]
pub enum LoraError {
    #[error("LoRA id is invalid")]
    InvalidId,
    #[error("LoRA scale must be finite")]
    InvalidScale,
    #[error("LoRA adapter is not registered: {0}")]
    Missing(String),
    #[error("LoRA adapter is already registered: {0}")]
    Duplicate(String),
    #[error("LoRA adapter file does not exist: {0}")]
    MissingFile(PathBuf),
    #[error("LoRA adapter is not loaded: {0}")]
    NotLoaded(String),
    #[error("LoRA adapter is already loaded: {0}")]
    AlreadyLoaded(String),
    #[error("LoRA adapter is active and cannot be unloaded: {0}")]
    Active(String),
    #[error("LoRA adapter allocation failed: {0}")]
    Allocation(String),
    #[error("LoRA adapter application failed: {0}")]
    Apply(String),
    #[error("multiple active LoRA adapters require explicit opt-in")]
    MultipleAdaptersDisabled,
}

#[derive(Debug, Default)]
pub struct LoraStore {
    known: BTreeMap<String, LoraEntry>,
    active: Vec<String>,
    allow_multiple: bool,
}

impl LoraStore {
    pub fn new(allow_multiple: bool) -> Self {
        Self {
            allow_multiple,
            ..Self::default()
        }
    }

    pub fn register(&mut self, spec: LoraSpec) -> Result<(), LoraError> {
        validate_id(&spec.id)?;
        if !spec.scale.is_finite() {
            return Err(LoraError::InvalidScale);
        }
        if self.known.contains_key(&spec.id) {
            return Err(LoraError::Duplicate(spec.id));
        }
        self.known.insert(
            spec.id.clone(),
            LoraEntry {
                spec,
                resource: None,
            },
        );
        Ok(())
    }

    /// Loads and retains a concrete adapter resource owned by this store.
    pub fn load_with<R: Any + Send + Sync>(
        &mut self,
        id: &str,
        allocate: impl FnOnce(&LoraSpec) -> Result<R, String>,
    ) -> Result<(), LoraError> {
        let entry = self
            .known
            .get_mut(id)
            .ok_or_else(|| LoraError::Missing(id.to_owned()))?;
        if entry.resource.is_some() {
            return Err(LoraError::AlreadyLoaded(id.to_owned()));
        }
        let canonical = entry
            .spec
            .path
            .canonicalize()
            .map_err(|_| LoraError::MissingFile(entry.spec.path.clone()))?;
        if !canonical.is_file() {
            return Err(LoraError::MissingFile(canonical));
        }
        entry.spec.path = canonical;
        let resource = allocate(&entry.spec).map_err(LoraError::Allocation)?;
        entry.resource = Some(LoadedResource::new(resource));
        Ok(())
    }

    /// Commits the active set only after the owning worker applies every retained handle.
    pub fn activate_with(
        &mut self,
        ids: &[String],
        apply: impl FnOnce(&[ActiveLora<'_>]) -> Result<(), String>,
    ) -> Result<Vec<LoraSpec>, LoraError> {
        if ids.len() > 1 && !self.allow_multiple {
            return Err(LoraError::MultipleAdaptersDisabled);
        }
        let selected = ids
            .iter()
            .map(|id| {
                let entry = self
                    .known
                    .get(id)
                    .ok_or_else(|| LoraError::Missing(id.clone()))?;
                if entry.resource.is_none() {
                    return Err(LoraError::NotLoaded(id.clone()));
                }
                Ok(entry.spec.clone())
            })
            .collect::<Result<Vec<_>, _>>()?;
        let resources = self.resources_for(ids)?;
        apply(&resources).map_err(LoraError::Apply)?;
        self.active.clone_from(&ids.to_vec());
        Ok(selected)
    }

    /// Returns the exact retained handles and scales for the owning model worker.
    pub fn active_resources(&self) -> Result<Vec<ActiveLora<'_>>, LoraError> {
        self.resources_for(&self.active)
    }

    fn resources_for(&self, ids: &[String]) -> Result<Vec<ActiveLora<'_>>, LoraError> {
        ids.iter()
            .map(|id| {
                let entry = self
                    .known
                    .get(id)
                    .ok_or_else(|| LoraError::Missing(id.clone()))?;
                let resource = entry
                    .resource
                    .as_ref()
                    .ok_or_else(|| LoraError::NotLoaded(id.clone()))?;
                Ok(ActiveLora {
                    spec: &entry.spec,
                    resource,
                })
            })
            .collect()
    }

    pub fn loaded_resource(&self, id: &str) -> Result<&LoadedResource, LoraError> {
        let entry = self
            .known
            .get(id)
            .ok_or_else(|| LoraError::Missing(id.to_owned()))?;
        entry
            .resource
            .as_ref()
            .ok_or_else(|| LoraError::NotLoaded(id.to_owned()))
    }

    pub fn unload(&mut self, id: &str) -> Result<(), LoraError> {
        if self.active.iter().any(|active| active == id) {
            return Err(LoraError::Active(id.to_owned()));
        }
        let entry = self
            .known
            .get_mut(id)
            .ok_or_else(|| LoraError::Missing(id.to_owned()))?;
        entry
            .resource
            .take()
            .ok_or_else(|| LoraError::NotLoaded(id.to_owned()))?;
        Ok(())
    }

    /// Clears the active set only after the owning worker detaches native adapters.
    pub fn deactivate_all_with(
        &mut self,
        detach: impl FnOnce() -> Result<(), String>,
    ) -> Result<(), LoraError> {
        detach().map_err(LoraError::Apply)?;
        self.active.clear();
        Ok(())
    }

    pub fn active(&self) -> &[String] {
        &self.active
    }
}

fn validate_id(id: &str) -> Result<(), LoraError> {
    if id.is_empty()
        || id.trim() != id
        || id.contains('/')
        || id.contains('\\')
        || id == "."
        || id == ".."
        || id.chars().any(char::is_control)
    {
        Err(LoraError::InvalidId)
    } else {
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use std::{
        fs,
        sync::{
            atomic::{AtomicUsize, Ordering},
            Arc,
        },
    };

    use super::*;

    struct DropProbe(Arc<AtomicUsize>);

    impl Drop for DropProbe {
        fn drop(&mut self) {
            self.0.fetch_add(1, Ordering::SeqCst);
        }
    }

    #[test]
    fn hot_swap_retains_resources_and_enforces_single_adapter_default() {
        let directory = tempfile::tempdir().unwrap();
        let dropped = Arc::new(AtomicUsize::new(0));
        let mut store = LoraStore::new(false);
        for id in ["a", "b"] {
            let path = directory.path().join(format!("{id}.gguf"));
            fs::write(&path, b"adapter").unwrap();
            store
                .register(LoraSpec {
                    id: id.to_owned(),
                    path,
                    scale: 1.0,
                })
                .unwrap();
            store
                .load_with(id, |_| Ok(DropProbe(dropped.clone())))
                .unwrap();
        }
        let applied = Arc::new(AtomicUsize::new(0));
        store
            .activate_with(&["a".to_owned()], {
                let applied = applied.clone();
                move |resources| {
                    assert_eq!(resources.len(), 1);
                    applied.fetch_add(1, Ordering::SeqCst);
                    Ok(())
                }
            })
            .unwrap();
        assert!(store.active_resources().unwrap()[0]
            .resource
            .is::<DropProbe>());
        store.activate_with(&["b".to_owned()], |_| Ok(())).unwrap();
        assert_eq!(store.active(), &["b"]);
        assert_eq!(
            store.activate_with(&["a".to_owned()], |_| Err("native refusal".to_owned())),
            Err(LoraError::Apply("native refusal".to_owned()))
        );
        assert_eq!(store.active(), &["b"]);
        assert_eq!(
            store.activate_with(&["a".to_owned(), "b".to_owned()], |_| Ok(())),
            Err(LoraError::MultipleAdaptersDisabled)
        );
        assert_eq!(applied.load(Ordering::SeqCst), 1);
        assert_eq!(
            store.deactivate_all_with(|| Err("native detach refusal".to_owned())),
            Err(LoraError::Apply("native detach refusal".to_owned()))
        );
        assert_eq!(store.active(), &["b"]);
        store.deactivate_all_with(|| Ok(())).unwrap();
        store.unload("a").unwrap();
        assert_eq!(dropped.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn activation_fails_closed_when_resource_is_not_loaded() {
        let mut store = LoraStore::new(false);
        store
            .register(LoraSpec {
                id: "adapter".to_owned(),
                path: PathBuf::from("missing.gguf"),
                scale: 1.0,
            })
            .unwrap();
        assert_eq!(
            store.activate_with(&["adapter".to_owned()], |_| Ok(())),
            Err(LoraError::NotLoaded("adapter".to_owned()))
        );
    }
}
