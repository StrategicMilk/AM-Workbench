export const FIRST_RUN_VIEW = 'onboarding';
export const COMPLETED_SETUP_VIEW = 'workbench-shell';

export function resolveDefaultLandingView(setupComplete) {
  return setupComplete === true ? COMPLETED_SETUP_VIEW : FIRST_RUN_VIEW;
}
