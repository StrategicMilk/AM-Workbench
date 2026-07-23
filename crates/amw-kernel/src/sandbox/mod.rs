use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum SandboxError {
    UnknownPermission(String),
    PermissionDenied(String),
    TraversalDenied(String),
    SymlinkEscapeDenied(String),
    UnreadableRoot(String),
}

#[derive(Debug, Clone, Eq, PartialEq, Hash)]
pub enum SandboxPermission {
    Read,
    Write,
    Delete,
}

impl SandboxPermission {
    fn parse(value: &str) -> Result<Self, SandboxError> {
        match value {
            "read" => Ok(Self::Read),
            "write" => Ok(Self::Write),
            "delete" => Ok(Self::Delete),
            other => Err(SandboxError::UnknownPermission(other.to_string())),
        }
    }
}

#[derive(Debug, Clone, Eq, PartialEq)]
pub struct SandboxGrant {
    pub permission: SandboxPermission,
    pub resolved_path: PathBuf,
}

#[derive(Debug, Clone)]
pub struct SandboxAuthority {
    root: PathBuf,
    allowed: HashSet<SandboxPermission>,
}

impl SandboxAuthority {
    pub fn new(
        root: impl AsRef<Path>,
        allowed: impl IntoIterator<Item = SandboxPermission>,
    ) -> Result<Self, SandboxError> {
        let root = root
            .as_ref()
            .canonicalize()
            .map_err(|exc| SandboxError::UnreadableRoot(exc.to_string()))?;
        Ok(Self {
            root,
            allowed: allowed.into_iter().collect(),
        })
    }

    pub fn check(
        &self,
        permission: &str,
        requested_path: impl AsRef<Path>,
    ) -> Result<SandboxGrant, SandboxError> {
        let permission = SandboxPermission::parse(permission)?;
        if !self.allowed.contains(&permission) {
            return Err(SandboxError::PermissionDenied(format!("{permission:?}")));
        }
        let requested_path = requested_path.as_ref();
        if requested_path
            .components()
            .any(|component| matches!(component, Component::ParentDir))
        {
            return Err(SandboxError::TraversalDenied(
                requested_path.display().to_string(),
            ));
        }

        let candidate = if requested_path.is_absolute() {
            requested_path.to_path_buf()
        } else {
            self.root.join(requested_path)
        };
        let resolved = resolve_existing_or_parent(&candidate)?;
        if !resolved.starts_with(&self.root) {
            if candidate.exists() {
                return Err(SandboxError::SymlinkEscapeDenied(
                    resolved.display().to_string(),
                ));
            }
            return Err(SandboxError::TraversalDenied(
                resolved.display().to_string(),
            ));
        }
        if candidate.exists()
            && candidate
                .symlink_metadata()
                .map(|m| m.file_type().is_symlink())
                .unwrap_or(false)
        {
            let target = candidate
                .canonicalize()
                .map_err(|exc| SandboxError::SymlinkEscapeDenied(exc.to_string()))?;
            if !target.starts_with(&self.root) {
                return Err(SandboxError::SymlinkEscapeDenied(
                    target.display().to_string(),
                ));
            }
        }
        Ok(SandboxGrant {
            permission,
            resolved_path: resolved,
        })
    }
}

fn resolve_existing_or_parent(candidate: &Path) -> Result<PathBuf, SandboxError> {
    if candidate.exists() {
        return candidate
            .canonicalize()
            .map_err(|exc| SandboxError::TraversalDenied(exc.to_string()));
    }
    let parent = candidate
        .parent()
        .ok_or_else(|| SandboxError::TraversalDenied(candidate.display().to_string()))?;
    let parent = parent
        .canonicalize()
        .map_err(|exc| SandboxError::TraversalDenied(exc.to_string()))?;
    Ok(parent.join(candidate.file_name().unwrap_or_default()))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock works")
            .as_nanos();
        let root = std::env::temp_dir().join(format!("amw-sandbox-{name}-{nonce}"));
        fs::create_dir_all(&root).expect("temp root created");
        root
    }

    #[test]
    fn sandbox_unknown_permission_fails_closed() {
        let root = temp_root("unknown-permission");
        let authority =
            SandboxAuthority::new(&root, [SandboxPermission::Read]).expect("authority builds");

        assert_eq!(
            authority.check("execute", "file.txt"),
            Err(SandboxError::UnknownPermission("execute".to_string()))
        );
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn sandbox_permission_denial_branch_executes() {
        let root = temp_root("denied");
        let authority =
            SandboxAuthority::new(&root, [SandboxPermission::Read]).expect("authority builds");

        assert!(matches!(
            authority.check("write", "file.txt"),
            Err(SandboxError::PermissionDenied(_))
        ));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn sandbox_traversal_denial_branch_executes() {
        let root = temp_root("traversal");
        let authority =
            SandboxAuthority::new(&root, [SandboxPermission::Write]).expect("authority builds");

        assert!(matches!(
            authority.check("write", "../escape.txt"),
            Err(SandboxError::TraversalDenied(_))
        ));
        fs::remove_dir_all(root).ok();
    }

    #[test]
    fn sandbox_symlink_escape_denial_branch_executes() {
        let root = temp_root("symlink");
        let outside = temp_root("outside");
        fs::write(outside.join("secret.txt"), "secret").expect("outside secret fixture");
        let link = root.join("linked-secret");
        create_directory_link(&outside, &link).expect("symlink or junction fixture created");
        let authority =
            SandboxAuthority::new(&root, [SandboxPermission::Read]).expect("authority builds");

        assert!(matches!(
            authority.check("read", "linked-secret"),
            Err(SandboxError::SymlinkEscapeDenied(_))
        ));
        fs::remove_dir_all(root).ok();
        fs::remove_dir_all(outside).ok();
    }

    #[cfg(unix)]
    fn create_directory_link(source: &Path, link: &Path) -> std::io::Result<()> {
        std::os::unix::fs::symlink(source, link)
    }

    #[cfg(windows)]
    fn create_directory_link(source: &Path, link: &Path) -> std::io::Result<()> {
        let status = std::process::Command::new("cmd")
            .args(["/C", "mklink", "/J"])
            .arg(link)
            .arg(source)
            .status()?;
        if status.success() {
            Ok(())
        } else {
            Err(std::io::Error::other("mklink /J failed"))
        }
    }
}
