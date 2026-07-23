use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant, SystemTime};

#[derive(Debug, Clone, Eq, PartialEq)]
pub enum StorageError {
    Io(String),
    Corrupt(String),
    LockTimeout(PathBuf),
}

impl From<io::Error> for StorageError {
    fn from(value: io::Error) -> Self {
        Self::Io(value.to_string())
    }
}

pub struct AppendLogStore {
    path: PathBuf,
    lock_path: PathBuf,
    cached_lines: Mutex<Option<CachedLines>>,
}

#[derive(Clone)]
struct CachedLines {
    signature: LogSignature,
    lines: Vec<String>,
}

#[derive(Clone, Eq, PartialEq)]
struct LogSignature {
    len: u64,
    modified: Option<SystemTime>,
}

impl AppendLogStore {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        let path = path.into();
        let lock_path = path.with_extension("lock");
        Self {
            path,
            lock_path,
            cached_lines: Mutex::new(None),
        }
    }

    pub fn path(&self) -> &Path {
        &self.path
    }

    pub fn append_line(&self, line: &str) -> Result<(), StorageError> {
        self.append_line_transaction(line, |_| Ok::<(), StorageError>(()))
    }

    pub fn append_line_transaction<E, F>(&self, line: &str, validate: F) -> Result<(), E>
    where
        E: From<StorageError>,
        F: FnOnce(Vec<String>) -> Result<(), E>,
    {
        if !line.ends_with('\n') {
            return Err(E::from(StorageError::Corrupt(
                "append-log writes must be newline terminated".to_string(),
            )));
        }
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)
                .map_err(StorageError::from)
                .map_err(E::from)?;
        }
        let _guard =
            FileLock::acquire(&self.lock_path, Duration::from_secs(10)).map_err(E::from)?;
        let mut cached = self
            .cached_lines
            .lock()
            .unwrap_or_else(|err| err.into_inner());
        let mut lines = self
            .read_lines_cached_locked(&mut cached)
            .map_err(E::from)?;
        validate(lines.clone())?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .map_err(StorageError::from)
            .map_err(E::from)?;
        file.write_all(line.as_bytes())
            .map_err(StorageError::from)
            .map_err(E::from)?;
        file.sync_all()
            .map_err(StorageError::from)
            .map_err(E::from)?;
        lines.push(
            line.trim_end_matches('\n')
                .trim_end_matches('\r')
                .to_string(),
        );
        *cached = Some(CachedLines {
            signature: self.log_signature().map_err(E::from)?,
            lines,
        });
        Ok(())
    }

    pub fn read_lines(&self) -> Result<Vec<String>, StorageError> {
        self.read_lines_unlocked()
    }

    fn read_lines_unlocked(&self) -> Result<Vec<String>, StorageError> {
        if !self.path.exists() {
            return Ok(Vec::new());
        }
        let mut file = File::open(&self.path)?;
        let mut bytes = Vec::new();
        file.read_to_end(&mut bytes)?;
        if !bytes.is_empty() && !bytes.ends_with(b"\n") {
            return Err(StorageError::Corrupt(
                "append-log truncated; last line incomplete".to_string(),
            ));
        }
        let text =
            String::from_utf8(bytes).map_err(|err| StorageError::Corrupt(err.to_string()))?;
        Ok(text
            .lines()
            .filter(|line| !line.trim().is_empty())
            .map(str::to_string)
            .collect())
    }

    fn read_lines_cached_locked(
        &self,
        cached: &mut Option<CachedLines>,
    ) -> Result<Vec<String>, StorageError> {
        let signature = self.log_signature()?;
        if let Some(existing) = cached {
            if existing.signature == signature {
                return Ok(existing.lines.clone());
            }
        }
        let lines = self.read_lines_unlocked()?;
        *cached = Some(CachedLines {
            signature: signature.clone(),
            lines: lines.clone(),
        });
        Ok(lines)
    }

    fn log_signature(&self) -> Result<LogSignature, StorageError> {
        match fs::metadata(&self.path) {
            Ok(metadata) => Ok(LogSignature {
                len: metadata.len(),
                modified: metadata.modified().ok(),
            }),
            Err(err) if err.kind() == io::ErrorKind::NotFound => Ok(LogSignature {
                len: 0,
                modified: None,
            }),
            Err(err) => Err(StorageError::Io(err.to_string())),
        }
    }

    pub fn write_atomic(&self, contents: &str) -> Result<(), StorageError> {
        if let Some(parent) = self.path.parent() {
            fs::create_dir_all(parent)?;
        }
        let _guard = FileLock::acquire(&self.lock_path, Duration::from_secs(10))?;
        let tmp = self.path.with_extension("tmp");
        {
            let mut file = File::create(&tmp)?;
            file.write_all(contents.as_bytes())?;
            file.sync_all()?;
        }
        fs::rename(tmp, &self.path)?;
        *self
            .cached_lines
            .lock()
            .unwrap_or_else(|err| err.into_inner()) = None;
        Ok(())
    }
}

struct FileLock {
    path: PathBuf,
}

impl FileLock {
    fn acquire(path: &Path, timeout: Duration) -> Result<Self, StorageError> {
        Self::acquire_with_stale_after(path, timeout, Duration::from_secs(30), SystemTime::now())
    }

    fn acquire_with_stale_after(
        path: &Path,
        timeout: Duration,
        stale_after: Duration,
        now: SystemTime,
    ) -> Result<Self, StorageError> {
        if let Some(parent) = path.parent() {
            fs::create_dir_all(parent)?;
        }
        let deadline = Instant::now() + timeout;
        loop {
            match OpenOptions::new().write(true).create_new(true).open(path) {
                Ok(mut file) => {
                    file.write_all(b"locked")?;
                    file.sync_all()?;
                    return Ok(Self {
                        path: path.to_path_buf(),
                    });
                }
                Err(err) if err.kind() == io::ErrorKind::AlreadyExists => {
                    if lock_is_stale_at(path, stale_after, now)? {
                        fs::remove_file(path)?;
                        continue;
                    }
                    if Instant::now() >= deadline {
                        return Err(StorageError::LockTimeout(path.to_path_buf()));
                    }
                    thread::sleep(Duration::from_millis(25));
                }
                Err(err) => return Err(StorageError::Io(err.to_string())),
            }
        }
    }
}

pub fn lock_is_stale(path: &Path, stale_after: Duration) -> Result<bool, StorageError> {
    lock_is_stale_at(path, stale_after, SystemTime::now())
}

pub fn lock_is_stale_at(
    path: &Path,
    stale_after: Duration,
    now: SystemTime,
) -> Result<bool, StorageError> {
    let metadata = match fs::metadata(path) {
        Ok(metadata) => metadata,
        Err(err) if err.kind() == io::ErrorKind::NotFound => return Ok(false),
        Err(err) => return Err(StorageError::Io(err.to_string())),
    };
    let modified = metadata
        .modified()
        .map_err(|err| StorageError::Io(err.to_string()))?;
    Ok(now.duration_since(modified).unwrap_or_default() >= stale_after)
}

impl Drop for FileLock {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn temp_path(name: &str) -> PathBuf {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock available")
            .as_nanos();
        std::env::temp_dir().join(format!("amw-kernel-{name}-{stamp}.jsonl"))
    }

    #[test]
    fn append_log_round_trips_newline_records() {
        let path = temp_path("round-trip");
        let store = AppendLogStore::new(&path);

        store
            .append_line("{\"kind\":\"asset\"}\n")
            .expect("append succeeds");

        assert_eq!(
            store.read_lines().expect("read succeeds"),
            vec!["{\"kind\":\"asset\"}"]
        );
        let _ = fs::remove_file(path);
    }

    #[test]
    fn append_log_rejects_truncated_lines() {
        let path = temp_path("truncated");
        fs::write(&path, b"{\"kind\":\"asset\"}").expect("seed corrupt log");
        let store = AppendLogStore::new(&path);

        assert_eq!(
            store.read_lines().expect_err("truncated tail fails"),
            StorageError::Corrupt("append-log truncated; last line incomplete".to_string())
        );
        let _ = fs::remove_file(path);
    }

    #[test]
    fn file_lock_recovers_stale_crash_lock_but_denies_active_lock() {
        let path = temp_path("stale-lock");
        let lock_path = path.with_extension("lock");
        fs::write(&lock_path, b"locked").expect("seed stale lock");

        let guard = FileLock::acquire_with_stale_after(
            &lock_path,
            Duration::from_millis(50),
            Duration::from_millis(0),
            SystemTime::now(),
        )
        .expect("stale lock is recovered");
        assert!(lock_path.exists());

        let active = FileLock::acquire_with_stale_after(
            &lock_path,
            Duration::from_millis(10),
            Duration::from_secs(60),
            SystemTime::now(),
        );
        assert!(matches!(active, Err(StorageError::LockTimeout(_))));

        drop(guard);
        let _ = fs::remove_file(path);
    }

    #[test]
    fn lock_is_stale_clock_injection_past_threshold() {
        let path = temp_path("stale-clock-past");
        fs::write(&path, b"locked").expect("seed lock");
        let now = SystemTime::now() + Duration::from_secs(120);

        assert_eq!(
            lock_is_stale_at(&path, Duration::from_secs(60), now),
            Ok(true)
        );

        let _ = fs::remove_file(path);
    }

    #[test]
    fn lock_is_stale_clock_injection_before_threshold() {
        let path = temp_path("stale-clock-before");
        fs::write(&path, b"locked").expect("seed lock");

        assert_eq!(
            lock_is_stale_at(&path, Duration::from_secs(60), UNIX_EPOCH),
            Ok(false)
        );

        let _ = fs::remove_file(path);
    }
}
