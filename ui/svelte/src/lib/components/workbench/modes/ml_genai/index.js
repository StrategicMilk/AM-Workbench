export const ML_GENAI_MODE_IDS = Object.freeze([
  'ml_genai_eval_design',
  'ml_genai_prompt_iteration',
  'ml_genai_dataset_curation',
  'ml_genai_annotation',
  'ml_genai_training_run_setup',
  'ml_genai_model_selection',
  'ml_genai_deployment_promotion_review',
  'ml_genai_incident_autopsy',
  'ml_genai_runtime_tuning'
]);

export const ML_GENAI_MODE_ROUTES = Object.freeze({
  ml_genai_eval_design: {
    lane: 'evaluation',
    promotionPath: 'eval_suite_review',
    failureFollowUp: 'blocked_missing_eval_or_holdout'
  },
  ml_genai_prompt_iteration: {
    lane: 'prompting',
    promotionPath: 'prompt_candidate_review',
    failureFollowUp: 'blocked_regression_or_unexplained_win'
  },
  ml_genai_dataset_curation: {
    lane: 'data',
    promotionPath: 'dataset_version_review',
    failureFollowUp: 'blocked_license_or_quality_gap'
  },
  ml_genai_annotation: {
    lane: 'labeling',
    promotionPath: 'annotation_batch_review',
    failureFollowUp: 'blocked_label_schema_or_disagreement'
  },
  ml_genai_training_run_setup: {
    lane: 'training',
    promotionPath: 'training_run_approval',
    failureFollowUp: 'blocked_budget_or_reproducibility_gap'
  },
  ml_genai_model_selection: {
    lane: 'selection',
    promotionPath: 'candidate_selection_review',
    failureFollowUp: 'blocked_benchmark_or_risk_gap'
  },
  ml_genai_deployment_promotion_review: {
    lane: 'release',
    promotionPath: 'deployment_gate_review',
    failureFollowUp: 'blocked_missing_gate_or_rollback'
  },
  ml_genai_incident_autopsy: {
    lane: 'incident',
    promotionPath: 'corrective_eval_review',
    failureFollowUp: 'blocked_root_cause_or_repro_gap'
  },
  ml_genai_runtime_tuning: {
    lane: 'runtime',
    promotionPath: 'tuning_change_review',
    failureFollowUp: 'blocked_telemetry_or_rollback_trigger'
  }
});

export function isMlGenaiMode(modeId) {
  return ML_GENAI_MODE_IDS.includes(modeId);
}

export function decorateMlGenaiMode(template) {
  const route = ML_GENAI_MODE_ROUTES[template?.id];
  if (!route) {
    return template;
  }
  return {
    ...template,
    practitionerModeFamily: 'ml_genai',
    route
  };
}

export function summarizeMlGenaiModes(templates) {
  return (templates ?? []).filter((template) => isMlGenaiMode(template.id)).map(decorateMlGenaiMode);
}
