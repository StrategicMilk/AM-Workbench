use std::collections::BTreeSet;
use std::fs;
use std::path::{Path, PathBuf};

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum FileIndexError {
    UnreadableRoot(String),
    NotDirectory(String),
    StaleIndex,
    CacheDeletionFailed(String),
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct FileIndexEntry {
    pub path: String,
    pub is_dir: bool,
    pub redacted: bool,
}

#[derive(Debug, Clone)]
pub struct FileIndex {
    root: PathBuf,
    entries: Vec<FileIndexEntry>,
}

impl FileIndex {
    pub fn build(root: impl AsRef<Path>) -> Result<Self, FileIndexError> {
        let root = root.as_ref().to_path_buf();
        if !root.exists() {
            return Err(FileIndexError::UnreadableRoot(root.display().to_string()));
        }
        if !root.is_dir() {
            return Err(FileIndexError::NotDirectory(root.display().to_string()));
        }

        let mut entries = Vec::new();
        collect_entries(&root, &root, &mut entries)?;
        entries.sort_by(|a, b| a.path.cmp(&b.path));
        Ok(Self { root, entries })
    }

    pub fn entries(&self) -> &[FileIndexEntry] {
        &self.entries
    }

    pub fn validate_fresh(&self) -> Result<(), FileIndexError> {
        let current = Self::build(&self.root)?;
        let old_paths: BTreeSet<&str> = self
            .entries
            .iter()
            .map(|entry| entry.path.as_str())
            .collect();
        let current_paths: BTreeSet<&str> = current
            .entries
            .iter()
            .map(|entry| entry.path.as_str())
            .collect();
        if old_paths == current_paths {
            Ok(())
        } else {
            Err(FileIndexError::StaleIndex)
        }
    }

    pub fn cache_dir(&self) -> PathBuf {
        self.root.join(".amw-index-cache")
    }

    pub fn delete_cache(&self) -> Result<(), FileIndexError> {
        let cache_dir = self.cache_dir();
        if cache_dir.exists() {
            fs::remove_dir_all(&cache_dir)
                .map_err(|exc| FileIndexError::CacheDeletionFailed(exc.to_string()))?;
        }
        Ok(())
    }
}

fn collect_entries(
    root: &Path,
    current: &Path,
    entries: &mut Vec<FileIndexEntry>,
) -> Result<(), FileIndexError> {
    let reader = fs::read_dir(current)
        .map_err(|exc| FileIndexError::UnreadableRoot(format!("{}: {exc}", current.display())))?;
    for item in reader {
        let item = item.map_err(|exc| FileIndexError::UnreadableRoot(exc.to_string()))?;
        let path = item.path();
        let name = item.file_name().to_string_lossy().to_string();
        if default_ignored(&name) {
            continue;
        }
        let metadata = fs::symlink_metadata(&path)
            .map_err(|exc| FileIndexError::UnreadableRoot(format!("{}: {exc}", path.display())))?;
        let relative = path
            .strip_prefix(root)
            .map(|value| value.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| "[outside-root]".to_string());
        let redacted = secret_like(&relative);
        entries.push(FileIndexEntry {
            path: if redacted {
                "[REDACTED]".to_string()
            } else {
                relative
            },
            is_dir: metadata.is_dir(),
            redacted,
        });
        if metadata.is_dir() {
            collect_entries(root, &path, entries)?;
        }
    }
    Ok(())
}

fn default_ignored(name: &str) -> bool {
    matches!(name, ".git" | "target" | "__pycache__" | ".amw-index-cache")
}

fn secret_like(value: &str) -> bool {
    let lower = value.to_ascii_lowercase();
    [
        "secret",
        "token",
        "password",
        "api_key",
        "apikey",
        "credential",
        "credentials",
        "private_key",
        "private-key",
        "private key",
        "bearer",
        "cert",
        ".pem",
    ]
    .iter()
    .any(|marker| lower.contains(marker))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock works")
            .as_nanos();
        let root = std::env::temp_dir().join(format!("amw-files-{name}-{nonce}"));
        fs::create_dir_all(&root).expect("temp root created");
        root
    }

    #[test]
    fn files_indexes_visible_entries_and_ignores_default_cache_dirs() {
        let root = temp_root("index");
        fs::write(root.join("visible.txt"), "ok").expect("write visible");
        fs::create_dir(root.join(".git")).expect("create ignored dir");
        fs::write(root.join(".git").join("config"), "ignored").expect("write ignored");

        let index = FileIndex::build(&root).expect("index builds");

        assert!(index
            .entries()
            .iter()
            .any(|entry| entry.path == "visible.txt"));
        assert!(!index
            .entries()
            .iter()
            .any(|entry| entry.path.contains(".git")));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn files_stale_index_invalidation_fails_closed_after_root_changes() {
        let root = temp_root("stale");
        fs::write(root.join("before.txt"), "ok").expect("write before");
        let index = FileIndex::build(&root).expect("index builds");
        fs::write(root.join("after.txt"), "changed").expect("write after");

        assert_eq!(index.validate_fresh(), Err(FileIndexError::StaleIndex));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn files_unreadable_root_and_non_directory_roots_fail_closed() {
        let root = temp_root("file-root");
        let file_root = root.join("not-a-dir.txt");
        fs::write(&file_root, "not a directory").expect("write file root");

        assert!(matches!(
            FileIndex::build(&file_root),
            Err(FileIndexError::NotDirectory(_))
        ));
        assert!(matches!(
            FileIndex::build(root.join("missing")),
            Err(FileIndexError::UnreadableRoot(_))
        ));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn files_privacy_redacts_secret_like_paths_and_deletes_cache() {
        let root = temp_root("privacy");
        fs::write(root.join("api_token.txt"), "sk-test-secret").expect("write secret fixture");
        let index = FileIndex::build(&root).expect("index builds");
        fs::create_dir_all(index.cache_dir()).expect("create cache");

        assert!(index
            .entries()
            .iter()
            .any(|entry| entry.redacted && entry.path == "[REDACTED]"));
        index.delete_cache().expect("cache deletion succeeds");
        assert!(!index.cache_dir().exists());
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn files_privacy_redacts_credential_private_key_bearer_cert_and_pem_paths() {
        let root = temp_root("expanded-secret-vocabulary");
        fs::write(root.join("visible.txt"), "ok").expect("write visible fixture");
        fs::write(root.join("credentials.json"), "{}").expect("write credentials fixture");
        fs::write(root.join("private_key.pem"), "key").expect("write private key fixture");
        fs::write(root.join("bearer-session.txt"), "bearer").expect("write bearer fixture");
        fs::write(root.join("client-cert.der"), "cert").expect("write cert fixture");

        let index = FileIndex::build(&root).expect("index builds");
        let serialized = format!("{:?}", index.entries());

        assert!(index
            .entries()
            .iter()
            .any(|entry| !entry.redacted && entry.path == "visible.txt"));
        assert_eq!(
            index
                .entries()
                .iter()
                .filter(|entry| entry.redacted && entry.path == "[REDACTED]")
                .count(),
            4
        );
        for leaked in [
            "credentials.json",
            "private_key.pem",
            "bearer-session.txt",
            "client-cert.der",
        ] {
            assert!(
                !serialized.contains(leaked),
                "redacted path leaked: {leaked}"
            );
        }
        fs::remove_dir_all(root).ok();
    }
}
