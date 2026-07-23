//! Bounded, symlink-safe GGUF discovery.

use std::{
    collections::{BTreeSet, VecDeque},
    fs,
    path::{Path, PathBuf},
};

use thiserror::Error;

pub const DEFAULT_MAX_DEPTH: usize = 8;
pub const DEFAULT_MAX_MODELS: usize = 10_000;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct ScanLimits {
    pub max_depth: usize,
    pub max_models: usize,
}

impl Default for ScanLimits {
    fn default() -> Self {
        Self {
            max_depth: DEFAULT_MAX_DEPTH,
            max_models: DEFAULT_MAX_MODELS,
        }
    }
}

#[derive(Debug, Error)]
pub enum ScanError {
    #[error("model root cannot be read: {path}: {source}")]
    Root {
        path: PathBuf,
        source: std::io::Error,
    },
    #[error("GGUF discovery exceeded the configured model count limit of {limit}")]
    ModelLimit { limit: usize },
    #[error("model root must not be a symbolic link: {0}")]
    SymlinkRoot(PathBuf),
}

pub fn discover_gguf(
    root: impl Into<PathBuf>,
    limits: ScanLimits,
) -> Result<Vec<PathBuf>, ScanError> {
    discover_gguf_roots([root.into()], limits)
}

/// Discovers GGUF files across configured roots under one aggregate model cap.
pub fn discover_gguf_roots(
    roots: impl IntoIterator<Item = PathBuf>,
    limits: ScanLimits,
) -> Result<Vec<PathBuf>, ScanError> {
    let mut canonical_roots = BTreeSet::new();
    for root in roots {
        let metadata = fs::symlink_metadata(&root).map_err(|source| ScanError::Root {
            path: root.clone(),
            source,
        })?;
        if metadata.file_type().is_symlink() {
            return Err(ScanError::SymlinkRoot(root));
        }
        let canonical = root.canonicalize().map_err(|source| ScanError::Root {
            path: root.clone(),
            source,
        })?;
        canonical_roots.insert((canonical, metadata.is_dir()));
    }

    let mut queue = VecDeque::new();
    let mut models = BTreeSet::new();
    for (root, is_directory) in canonical_roots {
        if is_directory {
            queue.push_back((root, 0usize));
        } else if is_gguf(&root) {
            push_model(&mut models, root, limits.max_models)?;
        }
    }
    while let Some((directory, depth)) = queue.pop_front() {
        let entries = fs::read_dir(&directory).map_err(|source| ScanError::Root {
            path: directory.clone(),
            source,
        })?;
        for entry in entries {
            let entry = entry.map_err(|source| ScanError::Root {
                path: directory.clone(),
                source,
            })?;
            let file_type = entry.file_type().map_err(|source| ScanError::Root {
                path: entry.path(),
                source,
            })?;
            if file_type.is_symlink() {
                continue;
            }
            let path = entry.path();
            if file_type.is_dir() {
                if depth < limits.max_depth {
                    queue.push_back((path, depth + 1));
                }
            } else if file_type.is_file() && is_gguf(&path) {
                push_model(&mut models, path, limits.max_models)?;
            }
        }
    }
    Ok(models.into_iter().collect())
}

fn push_model(
    models: &mut BTreeSet<PathBuf>,
    path: PathBuf,
    limit: usize,
) -> Result<(), ScanError> {
    if !models.contains(&path) && models.len() == limit {
        return Err(ScanError::ModelLimit { limit });
    }
    models.insert(path);
    Ok(())
}

fn is_gguf(path: &Path) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| extension.eq_ignore_ascii_case("gguf"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discovery_honors_depth_and_count_bounds() {
        let root = tempfile::tempdir().unwrap();
        fs::write(root.path().join("a.gguf"), b"GGUF").unwrap();
        let deep = root.path().join("one").join("two");
        fs::create_dir_all(&deep).unwrap();
        fs::write(deep.join("deep.gguf"), b"GGUF").unwrap();
        let found = discover_gguf(
            root.path(),
            ScanLimits {
                max_depth: 1,
                max_models: 10,
            },
        )
        .unwrap();
        assert_eq!(found.len(), 1);
        assert!(matches!(
            discover_gguf(
                root.path(),
                ScanLimits {
                    max_depth: 8,
                    max_models: 1,
                }
            ),
            Err(ScanError::ModelLimit { limit: 1 })
        ));
    }

    #[test]
    fn multiple_roots_share_one_limit_and_overlaps_are_deduplicated() {
        let first = tempfile::tempdir().unwrap();
        let nested = first.path().join("nested");
        fs::create_dir(&nested).unwrap();
        fs::write(nested.join("a.gguf"), b"GGUF").unwrap();
        let found = discover_gguf_roots(
            [first.path().to_owned(), nested.clone()],
            ScanLimits {
                max_depth: 8,
                max_models: 1,
            },
        )
        .unwrap();
        assert_eq!(found.len(), 1);

        let second = tempfile::tempdir().unwrap();
        fs::write(second.path().join("b.gguf"), b"GGUF").unwrap();
        assert!(matches!(
            discover_gguf_roots(
                [first.path().to_owned(), second.path().to_owned()],
                ScanLimits {
                    max_depth: 8,
                    max_models: 1,
                }
            ),
            Err(ScanError::ModelLimit { limit: 1 })
        ));
    }
}
