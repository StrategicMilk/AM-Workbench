const PATH_PATTERNS = [
  /\b[A-Za-z]:[\\/][^\s"'<>]+/g,
  /\/(?:home|Users|tmp|var|opt)\/[^\s"'<>]+/g,
  /\\\.venv[^\s"'<>]*/g,
  /\.venv[\\/][^\s"'<>]+/g,
  /\bweights[\\/][^\s"'<>]+/g,
  /Path\((['"])[^'"]+\1\)/g,
];

export function sanitizeTrace(text = '') {
  return PATH_PATTERNS.reduce((value, pattern) => value.replace(pattern, '[redacted-path]'), String(text));
}

export function isTraceSanitized(text = '') {
  return !PATH_PATTERNS.some((pattern) => {
    pattern.lastIndex = 0;
    return pattern.test(String(text));
  });
}
