export function buildTaskInferenceParameterFields() {
  return [
    {
      key: 'temperature',
      label: 'Temperature',
      type: 'number',
      min: 0,
      max: 1,
      step: 0.05,
      defaultValue: null,
    },
    {
      key: 'top_p',
      label: 'Top P',
      type: 'number',
      min: 0.1,
      max: 1,
      step: 0.01,
      defaultValue: null,
    },
    {
      key: 'top_k',
      label: 'Top K',
      type: 'number',
      min: 1,
      max: 100,
      step: 1,
      defaultValue: null,
    },
    {
      key: 'max_tokens',
      label: 'Max Tokens',
      type: 'number',
      min: 128,
      max: 32768,
      step: 128,
      defaultValue: null,
    },
    {
      key: 'prefer_json',
      label: 'Prefer JSON',
      type: 'checkbox',
      defaultValue: false,
    },
  ];
}

export const TASK_INFERENCE_PARAMETER_FIELDS = buildTaskInferenceParameterFields();

export const TASK_INFERENCE_PARAMETER_KEYS = TASK_INFERENCE_PARAMETER_FIELDS.map((field) => field.key);

export const TASK_INFERENCE_PARAMETER_FALLBACKS = TASK_INFERENCE_PARAMETER_FIELDS.reduce((values, field) => {
  values[field.key] = field.defaultValue;
  return values;
}, {});

export function normalizeTaskInferenceParameters(rawPreferences = {}) {
  const normalized = { ...TASK_INFERENCE_PARAMETER_FALLBACKS };
  for (const field of TASK_INFERENCE_PARAMETER_FIELDS) {
    if (field.type === 'checkbox') {
      normalized[field.key] = rawPreferences[field.key] === true;
      continue;
    }
    const rawValue = rawPreferences[field.key];
    if (rawValue == null || rawValue === '') {
      continue;
    }
    const numericValue = Number(rawValue);
    if (!Number.isFinite(numericValue)) {
      continue;
    }
    const boundedValue = Math.max(field.min, Math.min(field.max, numericValue));
    normalized[field.key] = field.step >= 1 ? Math.round(boundedValue) : Number(boundedValue.toFixed(3));
  }
  return normalized;
}
