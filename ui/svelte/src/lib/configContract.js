export function validateEffectiveConfigEntries(entries = [], schema = {}) {
  const knownKeys = new Set(schema.known_keys ?? schema.knownKeys ?? []);
  const requiredKeys = new Set(schema.required_keys ?? schema.requiredKeys ?? []);
  const seenKeys = new Set();
  const results = [];

  const safeEntries = Array.isArray(entries) ? entries : [];
  for (const entry of safeEntries) {
    const key = entry?.key ?? '';
    seenKeys.add(key);
    if (knownKeys.size && !knownKeys.has(key)) {
      results.push({ key, badge: 'unknown-key', message: 'Key is not declared in the active schema.' });
    }
  }

  for (const key of requiredKeys) {
    if (!seenKeys.has(key)) {
      results.push({ key, badge: 'required-missing', message: 'Required key is absent from the effective config.' });
    }
  }

  return results;
}
