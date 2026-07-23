<script>
  import {
    classifyWorkbenchCommandSafety,
    decideWorkbenchCommandSafety,
    fetchWorkbenchCommandSafetyProfiles,
  } from '$lib/api.js';
  import { ToolSurfaceKind } from '$lib/contracts/enums.js';

  let { projectId = 'default', nowProvider = () => new Date() } = $props();
  let profiles = $state([]);
  let profileId = $state('readonly-local');
  let command = $state('git status');
  let cwd = $state('<project-workspace>');
  let decision = $state(null);
  let loading = $state(false);
  let error = $state('');
  let reasons = $derived(decision?.reasons ?? decision?.classification?.reasons ?? []);
  let verdict = $derived(decision?.verdict ?? decision?.classification?.verdict ?? 'not evaluated');
  let receiptRef = $derived(decision?.receipt_ref || decision?.receipt_payload?.receipt_id || '');

  $effect(() => {
    let cancelled = false;
    fetchWorkbenchCommandSafetyProfiles()
      .then((payload) => { if (!cancelled) profiles = payload.profiles ?? []; })
      .catch((err) => { if (!cancelled) error = err instanceof Error ? err.message : String(err); });
    return () => { cancelled = true; };
  });

  function nowIsoString() {
    const value = nowProvider();
    return typeof value === 'string' ? value : value.toISOString();
  }

  function requestPayload() {
    const observedSurface = {
      surface_id: 'shell.local',
      surface_kind: ToolSurfaceKind.SHELL_COMMAND,
      command: 'powershell',
      host: 'localhost',
      transport: 'local_process',
      permissions: ['command:read'],
      owner: 'workbench',
      policy_mode: 'strict',
      version: '1.0.0',
      authority_refs: ['authority:command-safety'],
      provenance_refs: ['receipt:tool-pin'],
      captured_at_utc: nowIsoString(),
      trust_boundary: 'local-worktree-read',
      max_staleness_hours: 24,
    };
    return { project_id: projectId, run_id: 'ui-preview-run', session_id: 'ui-preview-session', surface_id: 'shell.local', surface: 'shell', profile_id: profileId, actor_id: 'operator', cwd, command, approval_sources: ['human'], pinned_surfaces: { 'shell.local': observedSurface }, observed_surface: observedSurface };
  }

  async function preview() {
    loading = true; error = '';
    try { decision = await classifyWorkbenchCommandSafety(requestPayload()); }
    catch (err) { error = err instanceof Error ? err.message : String(err); }
    finally { loading = false; }
  }

  async function evaluate() {
    loading = true; error = '';
    try { decision = await decideWorkbenchCommandSafety(requestPayload()); }
    catch (err) { error = err instanceof Error ? err.message : String(err); }
    finally { loading = false; }
  }
</script>

<section class="command-safety-panel" aria-label="Command safety">
  <header class="panel-header">
    <h1>Command Safety</h1>
    <div class="actions">
      <button type="button" onclick={preview} disabled={loading}>Classify</button>
      <button type="button" onclick={evaluate} disabled={loading}>Evaluate</button>
    </div>
  </header>
  <div class="form-grid">
    <label><span>Profile</span><select bind:value={profileId}>{#each profiles as profile}<option value={profile.profile_id ?? profile.id}>{profile.profile_id ?? profile.id}</option>{/each}{#if profiles.length === 0}<option value="readonly-local">readonly-local</option>{/if}</select></label>
    <label><span>CWD</span><input bind:value={cwd} /></label>
    <label class="wide"><span>Command</span><input bind:value={command} /></label>
  </div>
  <div class="decision-grid">
    <div><span class="label">Verdict</span><strong>{verdict}</strong></div>
    <div><span class="label">Approval</span><strong>{decision?.human_approval_required ? 'required' : 'not required'}</strong></div>
    <div><span class="label">Tool Surface</span><strong>{decision?.tool_surface?.status ?? 'not checked'}</strong></div>
    <div><span class="label">Receipt</span><strong>{receiptRef || 'none'}</strong></div>
  </div>
  {#if reasons.length > 0}<ul class="reason-list" aria-label="Safety reasons">{#each reasons as reason}<li>{reason}</li>{/each}</ul>{/if}
  {#if decision?.cwd_state}<div class="cwd-state"><span class="label">CWD State</span><code>{decision.cwd_state.cwd || decision.cwd_state.status}</code></div>{/if}
  {#if error}<p class="error">{error}</p>{/if}
</section>

<style>
  .command-safety-panel { display: grid; gap: 16px; padding: 24px; color: var(--text-primary); }
  .panel-header { display: flex; align-items: center; justify-content: space-between; gap: 16px; }
  h1 { margin: 0; font-size: 24px; }
  .actions { display: flex; gap: 8px; }
  button, input, select { min-height: 36px; border: 1px solid var(--border-default); border-radius: 6px; background: var(--surface-elevated, #1a202d); color: var(--text-primary); font: inherit; }
  button { padding: 0 12px; cursor: pointer; }
  .form-grid, .decision-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
  label { display: grid; gap: 6px; }
  .wide { grid-column: 1 / -1; }
  input, select { width: 100%; padding: 0 10px; }
  .decision-grid > div, .cwd-state { display: grid; gap: 4px; padding: 12px; border: 1px solid var(--border-default); border-radius: 6px; }
  .label { color: var(--text-muted); font-size: 12px; text-transform: uppercase; }
  .reason-list { display: flex; flex-wrap: wrap; gap: 8px; padding: 0; list-style: none; }
  .reason-list li { padding: 6px 8px; border-radius: 6px; background: var(--glass-bg, rgba(255, 255, 255, 0.05)); font-size: 13px; }
  .error { color: var(--danger, #ef4444); }
</style>
