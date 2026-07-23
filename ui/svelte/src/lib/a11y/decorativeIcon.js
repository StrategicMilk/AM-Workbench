export function decorativeIcon() {
  return {
    'aria-hidden': 'true',
    focusable: 'false',
    role: 'presentation',
  };
}

export function labeledIcon(label, fallback = 'Icon') {
  const text = typeof label === 'string' ? label.trim() : '';
  return {
    'aria-label': text || fallback,
  };
}
