use std::{
    fs::{self, File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
    sync::{Arc, Mutex, MutexGuard},
};

use serde::{Deserialize, Serialize};
use serde_json::Value;
use tracing_subscriber::fmt::MakeWriter;

use crate::store::session::secure_and_verify_private_path;

use super::{events::EventEnvelope, TraceContext};

pub const MAX_LOG_BYTES: u64 = 16 * 1024 * 1024;
pub const MAX_LOG_RECORD_BYTES: usize = 256 * 1024;
pub const DEFAULT_LOG_BACKUPS: usize = 3;
pub const MAX_CRASH_REPORT_BYTES: usize = 64 * 1024;
pub const CRASH_REPORT_SCHEMA_VERSION: u32 = 1;
static CRASH_TEMP_SEQUENCE: AtomicU64 = AtomicU64::new(0);
static CRASH_WRITE_LOCK: Mutex<()> = Mutex::new(());

#[derive(Clone)]
pub struct RotatingJsonLog {
    path: PathBuf,
    max_bytes: u64,
    backup_count: usize,
    write_lock: Arc<Mutex<()>>,
}

impl RotatingJsonLog {
    pub fn new(path: impl Into<PathBuf>) -> Self {
        Self::with_limits(path, MAX_LOG_BYTES, DEFAULT_LOG_BACKUPS)
    }

    pub fn with_limits(path: impl Into<PathBuf>, max_bytes: u64, backup_count: usize) -> Self {
        Self {
            path: path.into(),
            max_bytes: max_bytes.max(1),
            backup_count,
            write_lock: Arc::new(Mutex::new(())),
        }
    }

    /// Serialize and append exactly one JSON object, rotating before the configured bound.
    pub fn append<T: Serialize>(&self, record: &T) -> io::Result<()> {
        let mut encoded = serde_json::to_vec(record).map_err(invalid_json)?;
        if encoded.len() > MAX_LOG_RECORD_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "structured log record is {} bytes; maximum is {MAX_LOG_RECORD_BYTES}",
                    encoded.len()
                ),
            ));
        }
        encoded.push(b'\n');
        if encoded.len() as u64 > self.max_bytes {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "structured log record is {} bytes; configured file bound is {}",
                    encoded.len(),
                    self.max_bytes
                ),
            ));
        }

        let _guard = self.lock();
        ensure_parent(&self.path)?;
        let current_bytes = secure_existing_regular_file(&self.path)?
            .map(|metadata| metadata.len())
            .unwrap_or(0);
        if current_bytes > 0 && current_bytes.saturating_add(encoded.len() as u64) > self.max_bytes
        {
            self.rotate()?;
        }
        let mut file = open_private_append(&self.path)?;
        file.write_all(&encoded)?;
        file.flush()?;
        secure_and_verify_private_path(&self.path)
    }

    /// Validate an already encoded object before appending it to the structured log.
    pub fn append_json_line(&self, json_line: &str) -> io::Result<()> {
        let value: Value = serde_json::from_str(json_line.trim_end()).map_err(invalid_json)?;
        if !value.is_object() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "structured log record must be a JSON object",
            ));
        }
        self.append(&value)
    }

    /// Return a `tracing_subscriber` writer that validates each emitted JSON line.
    pub fn make_writer(&self) -> RotatingJsonMakeWriter {
        RotatingJsonMakeWriter { log: self.clone() }
    }

    fn rotate(&self) -> io::Result<()> {
        if self.backup_count == 0 {
            remove_secure_file_if_exists(&self.path)?;
            return Ok(());
        }
        let oldest = backup_path(&self.path, self.backup_count);
        remove_secure_file_if_exists(&oldest)?;
        for index in (1..self.backup_count).rev() {
            let from = backup_path(&self.path, index);
            if secure_existing_regular_file(&from)?.is_some() {
                let destination = backup_path(&self.path, index + 1);
                reject_existing_destination(&destination)?;
                fs::rename(&from, &destination)?;
                secure_and_verify_private_path(&destination)?;
            }
        }
        if secure_existing_regular_file(&self.path)?.is_some() {
            let destination = backup_path(&self.path, 1);
            reject_existing_destination(&destination)?;
            fs::rename(&self.path, &destination)?;
            secure_and_verify_private_path(&destination)?;
        }
        Ok(())
    }

    fn lock(&self) -> MutexGuard<'_, ()> {
        self.write_lock
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }
}

#[derive(Clone)]
pub struct RotatingJsonMakeWriter {
    log: RotatingJsonLog,
}

impl<'writer> MakeWriter<'writer> for RotatingJsonMakeWriter {
    type Writer = StructuredJsonWriter;

    fn make_writer(&'writer self) -> Self::Writer {
        StructuredJsonWriter {
            log: self.log.clone(),
            pending: Vec::new(),
        }
    }
}

pub struct StructuredJsonWriter {
    log: RotatingJsonLog,
    pending: Vec<u8>,
}

impl Write for StructuredJsonWriter {
    fn write(&mut self, buffer: &[u8]) -> io::Result<usize> {
        if self.pending.len().saturating_add(buffer.len()) > MAX_LOG_RECORD_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "tracing JSON record exceeded the structured log record bound",
            ));
        }
        self.pending.extend_from_slice(buffer);
        self.write_complete_lines()?;
        Ok(buffer.len())
    }

    fn flush(&mut self) -> io::Result<()> {
        self.write_complete_lines()?;
        if self.pending.is_empty() {
            return Ok(());
        }
        let line = std::str::from_utf8(&self.pending)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        self.log.append_json_line(line)?;
        self.pending.clear();
        Ok(())
    }
}

impl StructuredJsonWriter {
    fn write_complete_lines(&mut self) -> io::Result<()> {
        while let Some(newline) = self.pending.iter().position(|byte| *byte == b'\n') {
            let line: Vec<u8> = self.pending.drain(..=newline).collect();
            let line = std::str::from_utf8(&line[..line.len().saturating_sub(1)])
                .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
            if !line.trim().is_empty() {
                self.log.append_json_line(line)?;
            }
        }
        Ok(())
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
pub struct CrashReport {
    pub schema_version: u32,
    pub ts: f64,
    pub kind: String,
    pub summary: String,
    #[serde(default, skip_serializing_if = "TraceContext::is_empty")]
    pub trace: TraceContext,
    pub recent_events: Vec<EventEnvelope>,
    pub truncated: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub original_bytes: Option<usize>,
}

impl CrashReport {
    pub fn new(
        ts: f64,
        kind: impl Into<String>,
        summary: impl Into<String>,
        trace: TraceContext,
        recent_events: Vec<EventEnvelope>,
    ) -> Self {
        Self {
            schema_version: CRASH_REPORT_SCHEMA_VERSION,
            ts,
            kind: kind.into(),
            summary: summary.into(),
            trace,
            recent_events,
            truncated: false,
            original_bytes: None,
        }
    }
}

/// Write a valid, bounded JSON crash report through a same-directory temporary file.
pub fn write_crash_report(path: &Path, report: &CrashReport) -> io::Result<CrashReport> {
    let _guard = CRASH_WRITE_LOCK
        .lock()
        .unwrap_or_else(|poisoned| poisoned.into_inner());
    validate_crash_report(report)?;
    let bounded = bound_crash_report(report)?;
    let encoded = serde_json::to_vec(&bounded).map_err(invalid_json)?;
    debug_assert!(encoded.len() < MAX_CRASH_REPORT_BYTES);
    ensure_parent(path)?;
    let temporary = temporary_path(path);
    {
        let mut file = create_private_new(&temporary)?;
        file.write_all(&encoded)?;
        file.write_all(b"\n")?;
        file.sync_all()?;
    }
    let previous = previous_path(path);
    remove_secure_file_if_exists(&previous)?;
    let had_previous = secure_existing_regular_file(path)?.is_some();
    if had_previous {
        fs::rename(path, &previous)?;
        secure_and_verify_private_path(&previous)?;
    }
    let replace_result = fs::rename(&temporary, path);
    if let Err(error) = replace_result {
        let _ = remove_secure_file_if_exists(&temporary);
        if had_previous {
            let _ = fs::rename(&previous, path);
        }
        return Err(error);
    }
    if let Err(error) = secure_and_verify_private_path(path).and_then(|()| sync_parent(path)) {
        let _ = fs::remove_file(path);
        if had_previous {
            let _ = fs::rename(&previous, path);
        }
        return Err(error);
    }
    if had_previous {
        remove_secure_file_if_exists(&previous)?;
        sync_parent(path)?;
    }
    Ok(bounded)
}

fn validate_crash_report(report: &CrashReport) -> io::Result<()> {
    if !report.ts.is_finite() || report.ts < 0.0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "crash report timestamp must be finite and non-negative",
        ));
    }
    if report.kind.trim().is_empty() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "crash report kind must not be empty",
        ));
    }
    for event in &report.recent_events {
        event
            .validate()
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        if event.contains_content() {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "crash report event contains prohibited content",
            ));
        }
    }
    Ok(())
}

fn bound_crash_report(report: &CrashReport) -> io::Result<CrashReport> {
    let original_bytes = serde_json::to_vec(report).map_err(invalid_json)?.len();
    if original_bytes < MAX_CRASH_REPORT_BYTES {
        return Ok(report.clone());
    }

    let mut bounded = report.clone();
    bounded.truncated = true;
    bounded.original_bytes = Some(original_bytes);
    while !bounded.recent_events.is_empty()
        && serde_json::to_vec(&bounded).map_err(invalid_json)?.len() >= MAX_CRASH_REPORT_BYTES
    {
        bounded.recent_events.remove(0);
    }
    while serde_json::to_vec(&bounded).map_err(invalid_json)?.len() >= MAX_CRASH_REPORT_BYTES
        && !bounded.summary.is_empty()
    {
        let new_len = bounded.summary.len().saturating_sub(1024);
        let boundary = floor_char_boundary(&bounded.summary, new_len);
        bounded.summary.truncate(boundary);
    }
    if serde_json::to_vec(&bounded).map_err(invalid_json)?.len() >= MAX_CRASH_REPORT_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "crash report metadata exceeds the bounded report capacity",
        ));
    }
    Ok(bounded)
}

fn ensure_parent(path: &Path) -> io::Result<()> {
    if let Some(parent) = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
    {
        fs::create_dir_all(parent)?;
        secure_and_verify_private_path(parent)?;
    }
    Ok(())
}

fn secure_existing_regular_file(path: &Path) -> io::Result<Option<fs::Metadata>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => {
            verify_regular_file(path, &metadata)?;
            secure_and_verify_private_path(path)?;
            Ok(Some(fs::symlink_metadata(path)?))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn verify_regular_file(path: &Path, metadata: &fs::Metadata) -> io::Result<()> {
    if metadata.file_type().is_symlink() || !metadata.is_file() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "structured telemetry path {} must be a regular file, not a link or reparse point",
                path.display()
            ),
        ));
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;

        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x0000_0400;
        if metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                format!(
                    "structured telemetry path {} must not be a reparse point",
                    path.display()
                ),
            ));
        }
    }
    Ok(())
}

fn reject_existing_destination(path: &Path) -> io::Result<()> {
    if secure_existing_regular_file(path)?.is_some() {
        return Err(io::Error::new(
            io::ErrorKind::AlreadyExists,
            format!(
                "structured telemetry rotation destination {} unexpectedly exists",
                path.display()
            ),
        ));
    }
    Ok(())
}

fn remove_secure_file_if_exists(path: &Path) -> io::Result<()> {
    if secure_existing_regular_file(path)?.is_some() {
        fs::remove_file(path)?;
    }
    Ok(())
}

fn open_private_append(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.create(true).append(true);
    configure_private_open(&mut options);
    let file = options.open(path)?;
    verify_regular_file(path, &file.metadata()?)?;
    secure_and_verify_private_path(path)?;
    Ok(file)
}

fn create_private_new(path: &Path) -> io::Result<File> {
    let mut options = OpenOptions::new();
    options.write(true).create_new(true);
    configure_private_open(&mut options);
    let file = options.open(path)?;
    verify_regular_file(path, &file.metadata()?)?;
    secure_and_verify_private_path(path)?;
    Ok(file)
}

fn configure_private_open(options: &mut OpenOptions) {
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;

        options.mode(0o600);
    }
    #[cfg(target_os = "linux")]
    {
        use std::os::unix::fs::OpenOptionsExt;

        const O_NOFOLLOW: i32 = 0x0002_0000;
        options.custom_flags(O_NOFOLLOW);
    }
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;

        const FILE_FLAG_OPEN_REPARSE_POINT: u32 = 0x0020_0000;
        options.custom_flags(FILE_FLAG_OPEN_REPARSE_POINT);
    }
}

#[cfg(unix)]
fn sync_parent(path: &Path) -> io::Result<()> {
    path.parent()
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "telemetry file has no parent"))
        .and_then(File::open)?
        .sync_all()
}

#[cfg(not(unix))]
fn sync_parent(_path: &Path) -> io::Result<()> {
    Ok(())
}

fn backup_path(path: &Path, index: usize) -> PathBuf {
    let extension = path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| format!("{extension}.{index}"))
        .unwrap_or_else(|| index.to_string());
    path.with_extension(extension)
}

fn temporary_path(path: &Path) -> PathBuf {
    let sequence = CRASH_TEMP_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    let extension = path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| format!("{extension}.{}.{}.tmp", std::process::id(), sequence))
        .unwrap_or_else(|| format!("{}.{}.tmp", std::process::id(), sequence));
    path.with_extension(extension)
}

fn previous_path(path: &Path) -> PathBuf {
    let extension = path
        .extension()
        .and_then(|extension| extension.to_str())
        .map(|extension| format!("{extension}.previous"))
        .unwrap_or_else(|| "previous".to_owned());
    path.with_extension(extension)
}

fn floor_char_boundary(value: &str, requested: usize) -> usize {
    let mut boundary = requested.min(value.len());
    while !value.is_char_boundary(boundary) {
        boundary = boundary.saturating_sub(1);
    }
    boundary
}

fn invalid_json(error: serde_json::Error) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, error)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::telemetry::events::EngineEvent;
    use serde_json::json;
    use tempfile::tempdir;

    #[test]
    fn rotation_preserves_structured_records_and_backup_bound() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("engine.jsonl");
        let log = RotatingJsonLog::with_limits(&path, 32, 2);
        log.append(&json!({"message": "first-record"})).unwrap();
        log.append(&json!({"message": "second-record"})).unwrap();
        log.append(&json!({"message": "third-record"})).unwrap();

        for candidate in [&path, &backup_path(&path, 1), &backup_path(&path, 2)] {
            if candidate.exists() {
                for line in fs::read_to_string(candidate).unwrap().lines() {
                    assert!(serde_json::from_str::<Value>(line).unwrap().is_object());
                }
            }
        }
        assert!(!backup_path(&path, 3).exists());
    }

    #[test]
    fn invalid_raw_json_is_rejected_before_write() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("engine.jsonl");
        let log = RotatingJsonLog::new(&path);

        assert_eq!(
            log.append_json_line("not-json").unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        assert!(!path.exists());
    }

    #[test]
    fn tracing_writer_handles_fragmented_json_without_partial_records() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("engine.jsonl");
        let log = RotatingJsonLog::new(&path);
        let mut writer = log.make_writer().make_writer();
        writer.write_all(b"{\"message\":").unwrap();
        assert!(!path.exists());
        writer.write_all(b"\"ready\"}\n").unwrap();

        let line = fs::read_to_string(path).unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(&line).unwrap()["message"],
            "ready"
        );
    }

    #[test]
    fn oversized_crash_report_remains_valid_json_and_utf8() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("crash.json");
        let event = EventEnvelope::new(
            1.0,
            EngineEvent::Gauges {
                slots_busy: 1,
                queue_depth: 2,
                vram_used_mb: None,
                kv_occupancy_pct: 3,
            },
        );
        let report = CrashReport::new(
            1.0,
            "panic",
            "🙂".repeat(MAX_CRASH_REPORT_BYTES),
            TraceContext::new("request-1", "trace-1"),
            vec![event; 100],
        );

        let bounded = write_crash_report(&path, &report).unwrap();
        let bytes = fs::read(&path).unwrap();
        assert!(bytes.len() <= MAX_CRASH_REPORT_BYTES);
        assert!(serde_json::from_slice::<CrashReport>(&bytes).is_ok());
        assert!(bounded.truncated);
        assert!(bounded.original_bytes.unwrap() > MAX_CRASH_REPORT_BYTES);
    }

    #[test]
    fn crash_report_replacement_keeps_one_complete_current_document() {
        let directory = tempdir().unwrap();
        let path = directory.path().join("crash.json");
        let first = CrashReport::new(1.0, "panic", "first", TraceContext::default(), vec![]);
        let second = CrashReport::new(2.0, "panic", "second", TraceContext::default(), vec![]);
        write_crash_report(&path, &first).unwrap();
        write_crash_report(&path, &second).unwrap();

        let current: CrashReport = serde_json::from_slice(&fs::read(&path).unwrap()).unwrap();
        assert_eq!(current.summary, "second");
        assert!(!previous_path(&path).exists());
    }

    #[cfg(unix)]
    #[test]
    fn log_and_crash_artifacts_have_private_unix_modes() {
        use std::os::unix::fs::PermissionsExt;

        let directory = tempdir().unwrap();
        let private_directory = directory.path().join("private-telemetry");
        let log_path = private_directory.join("engine.jsonl");
        let crash_path = private_directory.join("crash.json");
        RotatingJsonLog::new(&log_path)
            .append(&json!({"message": "private"}))
            .unwrap();
        write_crash_report(
            &crash_path,
            &CrashReport::new(1.0, "panic", "private", TraceContext::default(), vec![]),
        )
        .unwrap();

        assert_eq!(
            fs::metadata(&private_directory)
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        for path in [log_path, crash_path] {
            assert_eq!(
                fs::metadata(path).unwrap().permissions().mode() & 0o777,
                0o600
            );
        }
    }

    #[cfg(unix)]
    #[test]
    fn log_and_crash_writes_reject_symbolic_link_targets() {
        use std::os::unix::fs::symlink;

        let directory = tempdir().unwrap();
        let victim = directory.path().join("victim.txt");
        fs::write(&victim, b"unchanged").unwrap();
        let log_path = directory.path().join("engine.jsonl");
        symlink(&victim, &log_path).unwrap();
        assert_eq!(
            RotatingJsonLog::new(&log_path)
                .append(&json!({"message": "blocked"}))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );

        let crash_path = directory.path().join("crash.json");
        symlink(&victim, &crash_path).unwrap();
        let report = CrashReport::new(1.0, "panic", "blocked", TraceContext::default(), vec![]);
        assert_eq!(
            write_crash_report(&crash_path, &report).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(fs::read(&victim).unwrap(), b"unchanged");
    }

    #[cfg(windows)]
    #[test]
    fn log_and_crash_writes_reject_windows_reparse_targets() {
        use std::os::windows::fs::symlink_file;

        let directory = tempdir().unwrap();
        let victim = directory.path().join("victim.txt");
        fs::write(&victim, b"unchanged").unwrap();
        let log_path = directory.path().join("engine.jsonl");
        if let Err(error) = symlink_file(&victim, &log_path) {
            assert_eq!(
                error.raw_os_error(),
                Some(1314),
                "cannot create reparse-point fixture: {error}"
            );
            return;
        }
        assert_eq!(
            RotatingJsonLog::new(&log_path)
                .append(&json!({"message": "blocked"}))
                .unwrap_err()
                .kind(),
            io::ErrorKind::InvalidData
        );

        let crash_path = directory.path().join("crash.json");
        fs::rename(&log_path, &crash_path).unwrap();
        let report = CrashReport::new(1.0, "panic", "blocked", TraceContext::default(), vec![]);
        assert_eq!(
            write_crash_report(&crash_path, &report).unwrap_err().kind(),
            io::ErrorKind::InvalidData
        );
        assert_eq!(fs::read(&victim).unwrap(), b"unchanged");
    }

    #[cfg(windows)]
    #[test]
    fn log_and_crash_artifacts_receive_private_windows_acls() {
        let directory = tempdir().unwrap();
        let private_directory = directory.path().join("private-telemetry");
        let log_path = private_directory.join("engine.jsonl");
        let crash_path = private_directory.join("crash.json");
        RotatingJsonLog::new(&log_path)
            .append(&json!({"message": "private"}))
            .unwrap();
        write_crash_report(
            &crash_path,
            &CrashReport::new(1.0, "panic", "private", TraceContext::default(), vec![]),
        )
        .unwrap();

        for path in [&private_directory, &log_path, &crash_path] {
            crate::store::session::verify_private_path(path).unwrap();
        }
    }
}
