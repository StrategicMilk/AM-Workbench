//! Request-local, resource-bounded GBNF parsing and native activation.

use std::{collections::BTreeSet, time::Duration};

use super::GenError;

/// Maximum accepted GBNF payload (256 KiB).
pub const MAX_GRAMMAR_BYTES: usize = 256 * 1024;
/// Maximum rule definitions in one request-local grammar.
pub const MAX_GRAMMAR_RULES: usize = 1_024;
/// Maximum grouping depth accepted by the bounded parser.
pub const MAX_GRAMMAR_NESTING: usize = 64;
/// Maximum tokens committed to one grammar state.
pub const MAX_GRAMMAR_ACCEPTED_TOKENS: usize = 1_048_576;
/// Deterministic parser-work budget, measured in inspected bytes and operators.
pub const GRAMMAR_COMPILE_BUDGET: usize = MAX_GRAMMAR_BYTES * 4;
/// Post-return wall-clock budget checked around native grammar construction.
///
/// This is not an interruptible deadline: llama.cpp exposes grammar creation as
/// a synchronous call with no cancellation handle. If native creation hangs,
/// the current process cannot safely reclaim it. ENG-P154 therefore owns the
/// enforceable boundary: compile inside the disposable native worker process,
/// let the Axum parent enforce the IPC deadline, and terminate/reap that worker
/// before admitting a replacement. The deterministic Rust parser bounds input
/// before this call; this constant only rejects a native compile that returns
/// after consuming too much wall time.
pub const GRAMMAR_NATIVE_COMPILE_TIMEOUT: Duration = Duration::from_millis(250);

/// Parsed request-local grammar and bounded accepted-token accounting.
#[derive(Clone, Debug)]
pub struct CompiledGrammar {
    source: String,
    rule_count: usize,
    accepted_count: usize,
}

/// A validated grammar attached to the pinned native backend for one request.
#[cfg(any(feature = "cpu", feature = "cuda"))]
pub struct ActiveGrammar {
    backend: crate::ffi::Grammar,
    accepted_count: usize,
}

impl CompiledGrammar {
    /// Parses compiled GBNF text with deterministic byte, rule, work, and nesting caps.
    pub fn compile(source: &str) -> Result<Self, GenError> {
        validate_source_bounds(source)?;
        let rule_count = parse_gbnf(source)?;
        Ok(Self {
            source: source.to_owned(),
            rule_count,
            accepted_count: 0,
        })
    }

    /// Advances only this sequence's bounded grammar accounting state.
    pub fn accept(&mut self, _token: i32) -> Result<(), GenError> {
        self.accepted_count = self
            .accepted_count
            .checked_add(1)
            .filter(|count| *count <= MAX_GRAMMAR_ACCEPTED_TOKENS)
            .ok_or(GenError::GrammarResourceLimit("accepted token history"))?;
        Ok(())
    }

    /// Returns the validated GBNF source for backend compilation.
    pub fn source(&self) -> &str {
        &self.source
    }

    /// Returns the number of parsed rule definitions.
    pub fn rule_count(&self) -> usize {
        self.rule_count
    }

    /// Returns the number of tokens accepted into this request-local state.
    pub fn accepted_count(&self) -> usize {
        self.accepted_count
    }

    /// Attaches validated GBNF to a live model through the RAII-only FFI boundary.
    ///
    /// The deterministic parser removes pathological source shapes before the
    /// native call. The elapsed-time check rejects a late successful return but
    /// cannot interrupt native creation; enforceable timeout and cleanup require
    /// the ENG-P154 worker-process boundary described on
    /// [`GRAMMAR_NATIVE_COMPILE_TIMEOUT`].
    #[cfg(any(feature = "cpu", feature = "cuda"))]
    pub fn activate(&self, model: &crate::ffi::Model) -> Result<ActiveGrammar, GenError> {
        let started = std::time::Instant::now();
        let backend = model
            .grammar(&self.source, "root")
            .map_err(|error| GenError::GrammarInvalid(error.to_string()))?;
        if started.elapsed() > GRAMMAR_NATIVE_COMPILE_TIMEOUT {
            return Err(GenError::GrammarResourceLimit(
                "native grammar compile deadline",
            ));
        }
        Ok(ActiveGrammar {
            backend,
            accepted_count: 0,
        })
    }
}

#[cfg(any(feature = "cpu", feature = "cuda"))]
impl ActiveGrammar {
    /// Commits one sampled token to exactly this request's native grammar state.
    pub fn accept(&mut self, token: i32) -> Result<(), GenError> {
        let next = self
            .accepted_count
            .checked_add(1)
            .filter(|count| *count <= MAX_GRAMMAR_ACCEPTED_TOKENS)
            .ok_or(GenError::GrammarResourceLimit("accepted token history"))?;
        self.backend
            .accept(token)
            .map_err(|error| GenError::Backend(error.to_string()))?;
        self.accepted_count = next;
        Ok(())
    }

    /// Number of tokens committed to this native grammar instance.
    pub fn accepted_count(&self) -> usize {
        self.accepted_count
    }
}

fn validate_source_bounds(source: &str) -> Result<(), GenError> {
    if source.is_empty() {
        return Err(GenError::GrammarInvalid("grammar is empty".to_owned()));
    }
    if source.len() > MAX_GRAMMAR_BYTES {
        return Err(GenError::GrammarInvalid(format!(
            "grammar exceeds {MAX_GRAMMAR_BYTES} bytes"
        )));
    }
    if source.as_bytes().contains(&0) {
        return Err(GenError::GrammarInvalid("grammar contains NUL".to_owned()));
    }
    Ok(())
}

fn parse_gbnf(source: &str) -> Result<usize, GenError> {
    let mut definitions = BTreeSet::new();
    let mut references = BTreeSet::new();
    let mut has_active_rule = false;
    let mut work = 0_usize;
    for raw_line in source.lines() {
        work = charge(work, raw_line.len().saturating_add(1))?;
        let line = strip_comment(raw_line, &mut work)?.trim();
        if line.is_empty() {
            continue;
        }
        if let Some(operator) = find_definition_operator(line, &mut work)? {
            let name = line[..operator].trim();
            validate_rule_name(name)?;
            if !definitions.insert(name.to_owned()) {
                return Err(GenError::GrammarInvalid(format!(
                    "duplicate rule definition: {name}"
                )));
            }
            if definitions.len() > MAX_GRAMMAR_RULES {
                return Err(GenError::GrammarResourceLimit("rule count"));
            }
            validate_expression(line[operator + 3..].trim(), &mut work, &mut references)?;
            has_active_rule = true;
        } else if has_active_rule {
            let continuation = line.strip_prefix('|').map(str::trim_start).unwrap_or(line);
            validate_expression(continuation, &mut work, &mut references)?;
        } else {
            return Err(GenError::GrammarInvalid(
                "content precedes the first rule definition".to_owned(),
            ));
        }
    }
    if !definitions.contains("root") {
        return Err(GenError::GrammarInvalid(
            "grammar has no root rule".to_owned(),
        ));
    }
    if let Some(undefined) = references
        .iter()
        .find(|reference| !definitions.contains(*reference))
    {
        return Err(GenError::GrammarInvalid(format!(
            "undefined rule reference: {undefined}"
        )));
    }
    Ok(definitions.len())
}

fn strip_comment<'a>(line: &'a str, work: &mut usize) -> Result<&'a str, GenError> {
    let mut scanner = LiteralScanner::default();
    for (index, byte) in line.bytes().enumerate() {
        *work = charge(*work, 1)?;
        if !scanner.consume(byte) && scanner.outside() && byte == b'#' {
            return Ok(&line[..index]);
        }
    }
    Ok(line)
}

fn find_definition_operator(line: &str, work: &mut usize) -> Result<Option<usize>, GenError> {
    let bytes = line.as_bytes();
    let mut scanner = LiteralScanner::default();
    let mut index = 0_usize;
    while index < bytes.len() {
        *work = charge(*work, 1)?;
        let byte = bytes[index];
        if !scanner.consume(byte) && scanner.outside() && bytes[index..].starts_with(b"::=") {
            return Ok(Some(index));
        }
        index += 1;
    }
    Ok(None)
}

/// Shared byte scanner for comment and definition detection.
///
/// Escapes are consumed before quote/class delimiters, including paired
/// backslashes and escaped closing brackets. Keeping this state machine shared
/// prevents the two pre-parser passes from disagreeing about where executable
/// grammar text ends.
#[derive(Default)]
struct LiteralScanner {
    quoted: bool,
    class: bool,
    escaped: bool,
}

impl LiteralScanner {
    /// Returns true when `byte` was structural literal state rather than plain text.
    fn consume(&mut self, byte: u8) -> bool {
        if self.escaped {
            self.escaped = false;
            return true;
        }
        if (self.quoted || self.class) && byte == b'\\' {
            self.escaped = true;
            return true;
        }
        if !self.class && byte == b'"' {
            self.quoted = !self.quoted;
            return true;
        }
        if !self.quoted && !self.class && byte == b'[' {
            self.class = true;
            return true;
        }
        if !self.quoted && self.class && byte == b']' {
            self.class = false;
            return true;
        }
        false
    }

    const fn outside(&self) -> bool {
        !self.quoted && !self.class && !self.escaped
    }
}

fn validate_rule_name(name: &str) -> Result<(), GenError> {
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return Err(GenError::GrammarInvalid("empty rule name".to_owned()));
    };
    if !first.is_ascii_alphabetic()
        || !chars.all(|character| {
            character.is_ascii_alphanumeric() || character == '-' || character == '_'
        })
    {
        return Err(GenError::GrammarInvalid(format!(
            "invalid rule name: {name}"
        )));
    }
    Ok(())
}

fn validate_expression(
    expression: &str,
    work: &mut usize,
    references: &mut BTreeSet<String>,
) -> Result<(), GenError> {
    if expression.is_empty() {
        return Err(GenError::GrammarInvalid("empty rule expression".to_owned()));
    }
    if expression.starts_with('|') || expression.ends_with('|') {
        return Err(GenError::GrammarInvalid(
            "empty grammar alternative".to_owned(),
        ));
    }
    let bytes = expression.as_bytes();
    let mut quoted = false;
    let mut class = false;
    let mut escaped = false;
    let mut depth = 0_usize;
    let mut repetition = false;
    let mut index = 0_usize;
    while index < bytes.len() {
        let byte = bytes[index];
        *work = charge(*work, 1)?;
        if escaped {
            escaped = false;
            index += 1;
            continue;
        }
        if (quoted || class) && byte == b'\\' {
            escaped = true;
        } else if !class && byte == b'"' {
            quoted = !quoted;
        } else if !quoted && byte == b'[' {
            if class {
                return Err(GenError::GrammarInvalid(
                    "nested character class".to_owned(),
                ));
            }
            class = true;
        } else if !quoted && byte == b']' {
            if !class {
                return Err(GenError::GrammarInvalid(
                    "unmatched character class close".to_owned(),
                ));
            }
            class = false;
        } else if !quoted && !class && byte == b'(' {
            depth = depth
                .checked_add(1)
                .ok_or(GenError::GrammarResourceLimit("group nesting"))?;
            if depth > MAX_GRAMMAR_NESTING {
                return Err(GenError::GrammarResourceLimit("group nesting"));
            }
        } else if !quoted && !class && byte == b')' {
            depth = depth.checked_sub(1).ok_or_else(|| {
                GenError::GrammarInvalid("unmatched closing parenthesis".to_owned())
            })?;
        } else if !quoted && !class && byte == b'{' {
            if repetition {
                return Err(GenError::GrammarInvalid(
                    "nested repetition range".to_owned(),
                ));
            }
            repetition = true;
        } else if !quoted && !class && byte == b'}' {
            if !repetition {
                return Err(GenError::GrammarInvalid(
                    "unmatched repetition close".to_owned(),
                ));
            }
            repetition = false;
        } else if !quoted && !class && !repetition && (byte as char).is_ascii_alphabetic() {
            let start = index;
            index += 1;
            while index < bytes.len()
                && ((bytes[index] as char).is_ascii_alphanumeric()
                    || bytes[index] == b'-'
                    || bytes[index] == b'_')
            {
                *work = charge(*work, 1)?;
                index += 1;
            }
            references.insert(expression[start..index].to_owned());
            continue;
        } else if !quoted
            && !class
            && !repetition
            && !byte.is_ascii_whitespace()
            && !matches!(byte, b'|' | b'?' | b'*' | b'+')
        {
            return Err(GenError::GrammarInvalid(format!(
                "invalid grammar expression byte: {byte:#x}"
            )));
        } else if repetition
            && !byte.is_ascii_digit()
            && byte != b','
            && !byte.is_ascii_whitespace()
        {
            return Err(GenError::GrammarInvalid(
                "invalid repetition range".to_owned(),
            ));
        }
        index += 1;
    }
    if escaped || quoted || class || repetition || depth != 0 {
        return Err(GenError::GrammarInvalid(
            "unterminated escape, literal, character class, or group".to_owned(),
        ));
    }
    Ok(())
}

fn charge(work: usize, units: usize) -> Result<usize, GenError> {
    work.checked_add(units)
        .filter(|total| *total <= GRAMMAR_COMPILE_BUDGET)
        .ok_or(GenError::GrammarResourceLimit("compile work"))
}
