//! Fail-closed GGUF metadata and tensor-boundary validation.

use std::{
    ffi::OsString,
    fs,
    io::{BufReader, Read, Seek, SeekFrom, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use thiserror::Error;

const GGUF_MAGIC: &[u8; 4] = b"GGUF";
const MAX_METADATA_ENTRIES: u64 = 1_000_000;
const MAX_TENSORS: u64 = 1_000_000;
const MAX_STRING_BYTES: u64 = 16 * 1024 * 1024;
const MAX_HEADER_BYTES: u64 = 256 * 1024 * 1024;
const MAX_ARRAY_NESTING: usize = 16;

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct GgufMetadata {
    pub version: u32,
    pub file_size_bytes: u64,
    pub tensor_count: u64,
    pub metadata_count: u64,
    pub architecture: Option<String>,
    pub model_name: Option<String>,
    pub alignment: u64,
    pub parameter_count: Option<u64>,
    pub quantization: Option<String>,
    pub context_length: Option<u64>,
    pub embedding_length: Option<u64>,
    pub chat_template: Option<String>,
    pub vocabulary_size: Option<u64>,
    pub eog_token_id: Option<u64>,
    pub supports_embeddings: bool,
    pub supports_fim: bool,
}

#[derive(Debug, Error)]
pub enum IntegrityError {
    #[error("model was previously quarantined: {0}")]
    Quarantined(PathBuf),
    #[error("GGUF header is corrupt: {0}")]
    CorruptHeader(&'static str),
    #[error("GGUF structure is truncated while reading {0}")]
    Truncated(&'static str),
    #[error("GGUF contains an unsupported metadata or tensor type: {0}")]
    UnsupportedType(u32),
    #[error("GGUF tensor data is truncated for tensor {0}")]
    TruncatedTensor(String),
    #[error("model file is not a non-link regular file")]
    UnsafeFile,
    #[error("model file identity or content changed during verified loading")]
    IdentityChanged,
    #[error("stable same-file model loading is unsupported on this platform")]
    IdentityGuardUnsupported,
    #[error("GGUF I/O failed for {path}: {source}")]
    Io {
        path: PathBuf,
        source: std::io::Error,
    },
}

#[derive(Serialize)]
struct QuarantineMarker<'a> {
    schema: &'static str,
    quarantined_at: String,
    model_path: &'a Path,
    size_bytes: u64,
    reason: &'static str,
    detail: String,
}

pub fn inspect_gguf(path: &Path) -> Result<GgufMetadata, IntegrityError> {
    let marker = quarantine_sidecar_path(path);
    if marker.exists() {
        return Err(IntegrityError::Quarantined(marker));
    }
    let legacy_marker = legacy_quarantine_sidecar_path(path);
    if legacy_marker.exists() {
        return Err(IntegrityError::Quarantined(legacy_marker));
    }
    let file = fs::File::open(path).map_err(|source| IntegrityError::Io {
        path: path.to_owned(),
        source,
    })?;
    let file_size = opened_file_size(&file, path)?;
    match inspect_opened_gguf(&file, path) {
        Ok(metadata) => Ok(metadata),
        Err(error) => {
            write_quarantine(path, file_size, &error)?;
            Err(error)
        }
    }
}

/// Inspects the exact file referenced by an already-open model handle.
///
/// Unlike [`inspect_gguf`], this entry point does not create a quarantine
/// sidecar. Callers that retain a stable handle may no longer have a trustworthy
/// pathname after a concurrent replacement, so mutating path-adjacent state
/// would reintroduce the substitution race the handle is intended to close.
pub(crate) fn inspect_opened_gguf(
    file: &fs::File,
    display_path: &Path,
) -> Result<GgufMetadata, IntegrityError> {
    let file_size = opened_file_size(file, display_path)?;
    let mut reader = file.try_clone().map_err(|source| IntegrityError::Io {
        path: display_path.to_owned(),
        source,
    })?;
    reader
        .seek(SeekFrom::Start(0))
        .map_err(|source| IntegrityError::Io {
            path: display_path.to_owned(),
            source,
        })?;
    parse_gguf_reader(BufReader::new(reader), file_size)
}

fn opened_file_size(file: &fs::File, path: &Path) -> Result<u64, IntegrityError> {
    file.metadata()
        .map_err(|source| IntegrityError::Io {
            path: path.to_owned(),
            source,
        })
        .map(|metadata| metadata.len())
}

pub fn quarantine_sidecar_path(path: &Path) -> PathBuf {
    let mut name = OsString::from(path.as_os_str());
    name.push(".quarantine.json");
    PathBuf::from(name)
}

fn legacy_quarantine_sidecar_path(path: &Path) -> PathBuf {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or("model");
    path.with_extension(format!("{extension}.amw-quarantine.v1.json"))
}

fn write_quarantine(
    model_path: &Path,
    file_size: u64,
    error: &IntegrityError,
) -> Result<(), IntegrityError> {
    let marker_path = quarantine_sidecar_path(model_path);
    let temporary = marker_path.with_extension("json.tmp");
    let marker = QuarantineMarker {
        schema: "amw-quarantine.v1",
        quarantined_at: utc_now()?,
        model_path,
        size_bytes: file_size,
        reason: integrity_error_kind(error),
        detail: error.to_string(),
    };
    let bytes = serde_json::to_vec_pretty(&marker)
        .map_err(|_| IntegrityError::CorruptHeader("quarantine marker serialization failed"))?;
    let mut file = fs::File::create(&temporary).map_err(|source| IntegrityError::Io {
        path: temporary.clone(),
        source,
    })?;
    file.write_all(&bytes)
        .map_err(|source| IntegrityError::Io {
            path: temporary.clone(),
            source,
        })?;
    file.sync_all().map_err(|source| IntegrityError::Io {
        path: temporary.clone(),
        source,
    })?;
    fs::rename(&temporary, &marker_path).map_err(|source| IntegrityError::Io {
        path: marker_path,
        source,
    })
}

fn integrity_error_kind(error: &IntegrityError) -> &'static str {
    match error {
        IntegrityError::Quarantined(_) => "quarantined",
        IntegrityError::CorruptHeader(_) => "corrupt_header",
        IntegrityError::Truncated(_) => "truncated",
        IntegrityError::UnsupportedType(_) => "unsupported_type",
        IntegrityError::TruncatedTensor(_) => "truncated_tensor",
        IntegrityError::UnsafeFile => "unsafe_file",
        IntegrityError::IdentityChanged => "identity_changed",
        IntegrityError::IdentityGuardUnsupported => "identity_guard_unsupported",
        IntegrityError::Io { .. } => "io",
    }
}

fn utc_now() -> Result<String, IntegrityError> {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| IntegrityError::CorruptHeader("system clock predates Unix epoch"))?
        .as_secs();
    let days = i64::try_from(seconds / 86_400)
        .map_err(|_| IntegrityError::CorruptHeader("system clock is out of range"))?;
    let seconds_of_day = seconds % 86_400;
    let hour = seconds_of_day / 3_600;
    let minute = (seconds_of_day % 3_600) / 60;
    let second = seconds_of_day % 60;
    let (year, month, day) = civil_date_from_unix_days(days);
    Ok(format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}Z"
    ))
}

fn civil_date_from_unix_days(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = shifted.div_euclid(146_097);
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let mut year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_prime = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_prime + 2) / 5 + 1;
    let month = month_prime + if month_prime < 10 { 3 } else { -9 };
    year += i64::from(month <= 2);
    (year, month, day)
}

#[cfg(test)]
fn parse_gguf(bytes: &[u8]) -> Result<GgufMetadata, IntegrityError> {
    parse_gguf_reader(std::io::Cursor::new(bytes), bytes.len() as u64)
}

fn parse_gguf_reader(reader: impl Read, file_size: u64) -> Result<GgufMetadata, IntegrityError> {
    let mut cursor = GgufReader::new(reader);
    if cursor.take(4, "magic")?.as_slice() != GGUF_MAGIC {
        return Err(IntegrityError::CorruptHeader("invalid magic"));
    }
    let version = cursor.u32("version")?;
    if !(2..=3).contains(&version) {
        return Err(IntegrityError::CorruptHeader("unsupported version"));
    }
    let tensor_count = cursor.u64("tensor count")?;
    let metadata_count = cursor.u64("metadata count")?;
    if tensor_count > MAX_TENSORS || metadata_count > MAX_METADATA_ENTRIES {
        return Err(IntegrityError::CorruptHeader("implausible table count"));
    }

    let mut architecture = None;
    let mut model_name = None;
    let mut alignment = None;
    let mut parameter_count = None;
    let mut file_type = None;
    let mut context_length = None;
    let mut embedding_length = None;
    let mut chat_template = None;
    let mut vocabulary_size = None;
    let mut eog_token_id = None;
    let mut model_type = None;
    let mut pooling_type_present = false;
    let mut fim_prefix = false;
    let mut fim_suffix = false;
    let mut fim_middle = false;
    for _ in 0..metadata_count {
        let key = cursor.string("metadata key")?;
        let value_type = cursor.u32("metadata value type")?;
        let value = cursor.value(value_type)?;
        match key.as_str() {
            "general.architecture" => architecture = value.into_string(),
            "general.name" => model_name = value.into_string(),
            "general.alignment" => alignment = value.as_u64(),
            "general.parameter_count" => parameter_count = value.as_u64(),
            "general.file_type" => file_type = value.as_u64(),
            "general.type" => model_type = value.into_string(),
            "tokenizer.chat_template" => chat_template = value.into_string(),
            "tokenizer.ggml.tokens" => {
                vocabulary_size = value.array_len();
                let (prefix, suffix, middle) = value.fim_tokens();
                fim_prefix |= prefix;
                fim_suffix |= suffix;
                fim_middle |= middle;
            }
            "tokenizer.ggml.eog_token_id" => eog_token_id = value.as_u64(),
            _ if key.ends_with(".context_length") => context_length = value.as_u64(),
            _ if key.ends_with(".embedding_length") => embedding_length = value.as_u64(),
            _ if key.ends_with(".pooling_type") => pooling_type_present = true,
            _ if is_fim_key(&key, "prefix") => fim_prefix = value.as_u64().is_some(),
            _ if is_fim_key(&key, "suffix") => fim_suffix = value.as_u64().is_some(),
            _ if is_fim_key(&key, "middle") => fim_middle = value.as_u64().is_some(),
            _ => {}
        }
    }

    let alignment = alignment.unwrap_or(32);
    if alignment == 0 || alignment > 4096 || !alignment.is_power_of_two() {
        return Err(IntegrityError::CorruptHeader(
            "tensor alignment must be a power of two no larger than 4096",
        ));
    }

    let mut tensors = Vec::with_capacity(tensor_count.min(4096) as usize);
    for _ in 0..tensor_count {
        let name = cursor.string("tensor name")?;
        let dimensions = cursor.u32("tensor dimensions")?;
        if dimensions > 4 {
            return Err(IntegrityError::CorruptHeader(
                "tensor has too many dimensions",
            ));
        }
        let mut elements = 1u64;
        let mut first_dimension = None;
        for dimension_index in 0..dimensions {
            let dimension = cursor.u64("tensor dimension")?;
            if dimension_index == 0 {
                first_dimension = Some(dimension);
            }
            elements = elements
                .checked_mul(dimension)
                .ok_or(IntegrityError::CorruptHeader(
                    "tensor element count overflow",
                ))?;
        }
        let ggml_type = cursor.u32("tensor type")?;
        let offset = cursor.u64("tensor offset")?;
        tensors.push((name, elements, first_dimension, ggml_type, offset));
    }

    let data_start = align(cursor.position(), alignment)
        .ok_or(IntegrityError::CorruptHeader("data alignment overflow"))?;
    if data_start > file_size {
        return Err(IntegrityError::Truncated("tensor alignment padding"));
    }
    let data_len = file_size - data_start;
    let mut ranges = Vec::with_capacity(tensors.len());
    for (name, elements, first_dimension, ggml_type, offset) in tensors {
        if offset % alignment != 0 {
            return Err(IntegrityError::CorruptHeader(
                "tensor offset does not satisfy model alignment",
            ));
        }
        let tensor_bytes = tensor_size(elements, first_dimension, ggml_type)?;
        let end = offset
            .checked_add(tensor_bytes)
            .ok_or_else(|| IntegrityError::TruncatedTensor(name.clone()))?;
        if end > data_len {
            return Err(IntegrityError::TruncatedTensor(name));
        }
        ranges.push((offset, end, name));
    }
    ranges.sort_by_key(|(start, _, _)| *start);
    if ranges.windows(2).any(|pair| pair[0].1 > pair[1].0) {
        return Err(IntegrityError::CorruptHeader("tensor ranges overlap"));
    }

    Ok(GgufMetadata {
        version,
        file_size_bytes: file_size,
        tensor_count,
        metadata_count,
        architecture,
        model_name,
        alignment,
        parameter_count,
        quantization: file_type.map(quantization_name),
        context_length,
        embedding_length,
        chat_template,
        vocabulary_size,
        eog_token_id,
        supports_embeddings: embedding_length.is_some()
            || pooling_type_present
            || model_type.as_deref() == Some("embedding"),
        supports_fim: fim_prefix && fim_suffix && fim_middle,
    })
}

fn is_fim_key(key: &str, part: &str) -> bool {
    let abbreviated = match part {
        "prefix" => "pre",
        "suffix" => "suf",
        "middle" => "mid",
        _ => part,
    };
    key == format!("tokenizer.ggml.fim_{part}_token_id")
        || key == format!("tokenizer.ggml.fim_{abbreviated}_token_id")
        || key == format!("tokenizer.ggml.{part}_token_id")
}

fn quantization_name(file_type: u64) -> String {
    // `general.file_type` uses llama.cpp's `llama_ftype` vocabulary, not the
    // numerically different `ggml_type` tensor vocabulary. Keep this table in
    // lockstep with the pinned vendor/llama.cpp/include/llama.h enum.
    const LLAMA_FTYPE_GUESSED: u64 = 1024;
    let base_type = file_type & !LLAMA_FTYPE_GUESSED;
    let name = match base_type {
        0 => "F32",
        1 => "F16",
        2 => "Q4_0",
        3 => "Q4_1",
        7 => "Q8_0",
        8 => "Q5_0",
        9 => "Q5_1",
        10 => "Q2_K",
        11 => "Q3_K_S",
        12 => "Q3_K_M",
        13 => "Q3_K_L",
        14 => "Q4_K_S",
        15 => "Q4_K_M",
        16 => "Q5_K_S",
        17 => "Q5_K_M",
        18 => "Q6_K",
        19 => "IQ2_XXS",
        20 => "IQ2_XS",
        21 => "Q2_K_S",
        22 => "IQ3_XS",
        23 => "IQ3_XXS",
        24 => "IQ1_S",
        25 => "IQ4_NL",
        26 => "IQ3_S",
        27 => "IQ3_M",
        28 => "IQ2_S",
        29 => "IQ2_M",
        30 => "IQ4_XS",
        31 => "IQ1_M",
        32 => "BF16",
        36 => "TQ1_0",
        37 => "TQ2_0",
        38 => "MXFP4_MOE",
        39 => "NVFP4",
        40 => "Q1_0",
        41 => "Q2_0",
        _ => return format!("UNKNOWN_{file_type}"),
    };
    name.to_owned()
}

fn tensor_size(
    elements: u64,
    first_dimension: Option<u64>,
    ggml_type: u32,
) -> Result<u64, IntegrityError> {
    let (block, bytes) = match ggml_type {
        0 => (1, 4),
        1 => (1, 2),
        2 => (32, 18),
        3 => (32, 20),
        6 => (32, 22),
        7 => (32, 24),
        8 => (32, 34),
        9 => (32, 36),
        10 => (256, 84),
        11 => (256, 110),
        12 => (256, 144),
        13 => (256, 176),
        14 => (256, 210),
        15 => (256, 292),
        16 => (256, 66),
        17 => (256, 74),
        18 => (256, 98),
        19 => (256, 50),
        20 => (32, 18),
        21 => (256, 110),
        22 => (256, 82),
        23 => (256, 136),
        24 => (1, 1),
        25 => (1, 2),
        26 => (1, 4),
        27 | 28 => (1, 8),
        29 => (256, 56),
        30 => (1, 2),
        34 => (256, 54),
        35 => (256, 66),
        39 => (32, 17),
        40 => (64, 36),
        41 => (128, 18),
        42 => (64, 18),
        other => return Err(IntegrityError::UnsupportedType(other)),
    };
    if block > 1 && first_dimension.is_none_or(|dimension| dimension % block != 0) {
        return Err(IntegrityError::CorruptHeader(
            "tensor row is not divisible by its quantization block",
        ));
    }
    let blocks = elements / block;
    blocks
        .checked_mul(bytes)
        .ok_or(IntegrityError::CorruptHeader("tensor byte size overflow"))
}

fn align(value: u64, alignment: u64) -> Option<u64> {
    value
        .checked_add(alignment - 1)
        .map(|value| value / alignment * alignment)
}

struct GgufReader<R> {
    reader: R,
    position: u64,
}

impl<R: Read> GgufReader<R> {
    fn new(reader: R) -> Self {
        Self {
            reader,
            position: 0,
        }
    }

    fn position(&self) -> u64 {
        self.position
    }

    fn take(&mut self, count: usize, field: &'static str) -> Result<Vec<u8>, IntegrityError> {
        let count_u64 = u64::try_from(count)
            .map_err(|_| IntegrityError::CorruptHeader("header size overflow"))?;
        let end = self
            .position
            .checked_add(count_u64)
            .ok_or(IntegrityError::Truncated(field))?;
        if end > MAX_HEADER_BYTES {
            return Err(IntegrityError::CorruptHeader(
                "metadata and tensor header exceeds safety limit",
            ));
        }
        let mut value = vec![0_u8; count];
        self.reader
            .read_exact(&mut value)
            .map_err(|_| IntegrityError::Truncated(field))?;
        self.position = end;
        Ok(value)
    }

    fn u32(&mut self, field: &'static str) -> Result<u32, IntegrityError> {
        Ok(u32::from_le_bytes(
            self.take(4, field)?.try_into().expect("fixed slice length"),
        ))
    }

    fn u64(&mut self, field: &'static str) -> Result<u64, IntegrityError> {
        Ok(u64::from_le_bytes(
            self.take(8, field)?.try_into().expect("fixed slice length"),
        ))
    }

    fn string(&mut self, field: &'static str) -> Result<String, IntegrityError> {
        let length = self.u64(field)?;
        if length > MAX_STRING_BYTES {
            return Err(IntegrityError::CorruptHeader("string is too large"));
        }
        let length = usize::try_from(length)
            .map_err(|_| IntegrityError::CorruptHeader("string length overflow"))?;
        let bytes = self.take(length, field)?;
        String::from_utf8(bytes).map_err(|_| IntegrityError::CorruptHeader("string is not UTF-8"))
    }

    fn value(&mut self, value_type: u32) -> Result<MetadataValue, IntegrityError> {
        self.value_with_depth(value_type, 0)
    }

    fn value_with_depth(
        &mut self,
        value_type: u32,
        depth: usize,
    ) -> Result<MetadataValue, IntegrityError> {
        Ok(match value_type {
            0 => MetadataValue::Unsigned(u64::from(self.take(1, "metadata scalar")?[0])),
            1 => MetadataValue::Signed(i64::from(i8::from_le_bytes(
                self.take(1, "metadata scalar")?
                    .try_into()
                    .expect("fixed slice length"),
            ))),
            2 => MetadataValue::Unsigned(u64::from(u16::from_le_bytes(
                self.take(2, "metadata scalar")?
                    .try_into()
                    .expect("fixed slice length"),
            ))),
            3 => MetadataValue::Signed(i64::from(i16::from_le_bytes(
                self.take(2, "metadata scalar")?
                    .try_into()
                    .expect("fixed slice length"),
            ))),
            4 => MetadataValue::Unsigned(u64::from(self.u32("metadata scalar")?)),
            5 => MetadataValue::Signed(i64::from(i32::from_le_bytes(
                self.take(4, "metadata scalar")?
                    .try_into()
                    .expect("fixed slice length"),
            ))),
            6 => {
                self.take(4, "metadata scalar")?;
                MetadataValue::Other
            }
            7 => {
                self.take(1, "metadata scalar")?;
                MetadataValue::Other
            }
            8 => MetadataValue::String(self.string("metadata string")?),
            9 => {
                if depth == MAX_ARRAY_NESTING {
                    return Err(IntegrityError::CorruptHeader(
                        "metadata array nesting exceeds safety limit",
                    ));
                }
                let element_type = self.u32("metadata array type")?;
                let count = self.u64("metadata array length")?;
                if count > MAX_METADATA_ENTRIES {
                    return Err(IntegrityError::CorruptHeader("metadata array is too large"));
                }
                let mut fim_tokens = [false; 3];
                for _ in 0..count {
                    let element = self.value_with_depth(element_type, depth + 1)?;
                    if let MetadataValue::String(token) = &element {
                        if let Some(index) = fim_token_index(token) {
                            fim_tokens[index] = true;
                        }
                    }
                }
                MetadataValue::ArraySummary {
                    len: count,
                    fim_tokens,
                }
            }
            10 => MetadataValue::Unsigned(self.u64("metadata scalar")?),
            11 => MetadataValue::Signed(i64::from_le_bytes(
                self.take(8, "metadata scalar")?
                    .try_into()
                    .expect("fixed slice length"),
            )),
            12 => {
                self.take(8, "metadata scalar")?;
                MetadataValue::Other
            }
            other => return Err(IntegrityError::UnsupportedType(other)),
        })
    }
}

#[derive(Debug)]
enum MetadataValue {
    String(String),
    Unsigned(u64),
    Signed(i64),
    ArraySummary { len: u64, fim_tokens: [bool; 3] },
    Other,
}

impl MetadataValue {
    fn as_u64(&self) -> Option<u64> {
        match self {
            Self::Unsigned(value) => Some(*value),
            Self::Signed(value) => u64::try_from(*value).ok(),
            Self::String(_) | Self::ArraySummary { .. } | Self::Other => None,
        }
    }

    fn into_string(self) -> Option<String> {
        if let Self::String(value) = self {
            Some(value)
        } else {
            None
        }
    }

    fn array_len(&self) -> Option<u64> {
        if let Self::ArraySummary { len, .. } = self {
            Some(*len)
        } else {
            None
        }
    }

    fn fim_tokens(&self) -> (bool, bool, bool) {
        if let Self::ArraySummary { fim_tokens, .. } = self {
            (fim_tokens[0], fim_tokens[1], fim_tokens[2])
        } else {
            (false, false, false)
        }
    }
}

fn fim_token_index(token: &str) -> Option<usize> {
    let normalized = token.to_ascii_lowercase();
    match normalized.as_str() {
        "<|fim_prefix|>" | "<fim_prefix>" | "<pre>" => Some(0),
        "<|fim_suffix|>" | "<fim_suffix>" | "<suf>" => Some(1),
        "<|fim_middle|>" | "<fim_middle>" | "<mid>" => Some(2),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    enum TestValue<'a> {
        String(&'a str),
        U32(u32),
        U64(u64),
        Strings(&'a [&'a str]),
    }

    fn push_string(bytes: &mut Vec<u8>, value: &str) {
        bytes.extend_from_slice(&(value.len() as u64).to_le_bytes());
        bytes.extend_from_slice(value.as_bytes());
    }

    fn metadata_only_gguf(entries: &[(&str, TestValue<'_>)]) -> Vec<u8> {
        let mut bytes = Vec::new();
        bytes.extend_from_slice(GGUF_MAGIC);
        bytes.extend_from_slice(&3_u32.to_le_bytes());
        bytes.extend_from_slice(&0_u64.to_le_bytes());
        bytes.extend_from_slice(&(entries.len() as u64).to_le_bytes());
        for (key, value) in entries {
            push_string(&mut bytes, key);
            match value {
                TestValue::String(value) => {
                    bytes.extend_from_slice(&8_u32.to_le_bytes());
                    push_string(&mut bytes, value);
                }
                TestValue::U32(value) => {
                    bytes.extend_from_slice(&4_u32.to_le_bytes());
                    bytes.extend_from_slice(&value.to_le_bytes());
                }
                TestValue::U64(value) => {
                    bytes.extend_from_slice(&10_u32.to_le_bytes());
                    bytes.extend_from_slice(&value.to_le_bytes());
                }
                TestValue::Strings(values) => {
                    bytes.extend_from_slice(&9_u32.to_le_bytes());
                    bytes.extend_from_slice(&8_u32.to_le_bytes());
                    bytes.extend_from_slice(&(values.len() as u64).to_le_bytes());
                    for value in *values {
                        push_string(&mut bytes, value);
                    }
                }
            }
        }
        bytes.resize(align(bytes.len() as u64, 32).unwrap() as usize, 0);
        bytes
    }

    #[test]
    fn corrupt_magic_is_typed() {
        assert!(matches!(
            parse_gguf(b"NOPE"),
            Err(IntegrityError::CorruptHeader("invalid magic"))
        ));
    }

    #[test]
    fn corrupt_file_writes_schema_versioned_quarantine() {
        let directory = tempfile::tempdir().unwrap();
        let model = directory.path().join("corrupt.gguf");
        fs::write(&model, b"NOPE").unwrap();
        assert!(matches!(
            inspect_gguf(&model),
            Err(IntegrityError::CorruptHeader("invalid magic"))
        ));
        let marker: serde_json::Value =
            serde_json::from_slice(&fs::read(quarantine_sidecar_path(&model)).unwrap()).unwrap();
        assert_eq!(marker["schema"], "amw-quarantine.v1");
        assert_eq!(marker["size_bytes"], 4);
        assert_eq!(marker["reason"], "corrupt_header");
        assert!(marker["quarantined_at"]
            .as_str()
            .is_some_and(|value| value.ends_with('Z')));
        assert_eq!(civil_date_from_unix_days(0), (1970, 1, 1));
        assert_eq!(civil_date_from_unix_days(10_957), (2000, 1, 1));
    }

    #[test]
    fn api_capability_metadata_is_extracted() {
        let bytes = metadata_only_gguf(&[
            ("general.architecture", TestValue::String("llama")),
            ("general.name", TestValue::String("catalog-name")),
            ("general.parameter_count", TestValue::U64(7_000_000_000)),
            ("general.file_type", TestValue::U32(15)),
            ("llama.context_length", TestValue::U32(32_768)),
            ("llama.embedding_length", TestValue::U32(4096)),
            (
                "tokenizer.chat_template",
                TestValue::String("{{ messages }}"),
            ),
            ("tokenizer.ggml.tokens", TestValue::Strings(&["a", "b"])),
            ("tokenizer.ggml.eog_token_id", TestValue::U32(2)),
            ("tokenizer.ggml.fim_prefix_token_id", TestValue::U32(3)),
            ("tokenizer.ggml.fim_suffix_token_id", TestValue::U32(4)),
            ("tokenizer.ggml.fim_middle_token_id", TestValue::U32(5)),
        ]);
        let metadata = parse_gguf(&bytes).unwrap();
        assert_eq!(metadata.quantization.as_deref(), Some("Q4_K_M"));
        assert_eq!(metadata.context_length, Some(32_768));
        assert_eq!(metadata.embedding_length, Some(4096));
        assert_eq!(metadata.vocabulary_size, Some(2));
        assert_eq!(metadata.eog_token_id, Some(2));
        assert!(metadata.supports_embeddings);
        assert!(metadata.supports_fim);
    }

    #[test]
    fn general_file_type_uses_pinned_llama_ftype_vocabulary() {
        let representative = [
            (0, "F32"),
            (7, "Q8_0"),
            (11, "Q3_K_S"),
            (12, "Q3_K_M"),
            (15, "Q4_K_M"),
            (19, "IQ2_XXS"),
            (32, "BF16"),
            (36, "TQ1_0"),
            (38, "MXFP4_MOE"),
            (41, "Q2_0"),
            (1024 + 15, "Q4_K_M"),
        ];
        for (file_type, expected) in representative {
            assert_eq!(quantization_name(file_type), expected);
        }
        assert_eq!(quantization_name(4), "UNKNOWN_4");
    }

    #[test]
    fn current_quantized_tensor_types_are_bounded_exactly() {
        assert_eq!(tensor_size(256, Some(256), 12).unwrap(), 144);
        assert_eq!(tensor_size(256, Some(256), 16).unwrap(), 66);
        assert!(matches!(
            tensor_size(256, Some(128), 12),
            Err(IntegrityError::CorruptHeader(
                "tensor row is not divisible by its quantization block"
            ))
        ));
        assert!(matches!(
            tensor_size(256, Some(256), 31),
            Err(IntegrityError::UnsupportedType(31))
        ));
    }

    #[test]
    fn fim_capability_falls_back_to_well_known_vocabulary_tokens() {
        let bytes = metadata_only_gguf(&[(
            "tokenizer.ggml.tokens",
            TestValue::Strings(&["<PRE>", "<SUF>", "<MID>"]),
        )]);
        assert!(parse_gguf(&bytes).unwrap().supports_fim);
    }
}
