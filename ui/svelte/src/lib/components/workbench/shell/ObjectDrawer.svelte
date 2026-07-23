<script>
  import { RuntimeUxCards } from '../runtime_ux';

  let { objects = [], selectedObjectId = null, onSelect = () => {}, runtimeUx = null } = $props();

  let objectRows = $derived(Array.isArray(objects) ? objects : []);

  function objectKey(object) {
    return `${object?.object_kind ?? 'unknown'}:${object?.object_id ?? object?.title ?? 'unidentified'}`;
  }

  function objectLabel(value, fallback = 'unknown') {
    return value === null || value === undefined || value === '' ? fallback : String(value);
  }
</script>

<section class="object-drawer" aria-label="Workbench objects" data-testid="workbench-object-drawer">
  <header>
    <h2>Objects</h2>
    <span role="status" aria-label={`${objectRows.length} workbench objects`}>{objectRows.length}</span>
  </header>

  <div class="object-list">
    {#if objectRows.length === 0}
      <p class="empty-state" role="status">No workbench objects are available.</p>
    {/if}
    {#each objectRows as object (objectKey(object))}
      <button
        type="button"
        class:active={selectedObjectId === object.object_id}
        data-testid={`workbench-object-${objectLabel(object.object_kind)}-${objectLabel(object.object_id)}`}
        title={objectLabel(object.why, 'No provenance note available')}
        onclick={() => onSelect(object)}
        aria-pressed={selectedObjectId === object.object_id}
        aria-label={`Select ${objectLabel(object.object_kind)} ${objectLabel(object.title, 'Untitled object')}`}
      >
        <span class="kind">{objectLabel(object.object_kind)}</span>
        <strong>{objectLabel(object.title, 'Untitled object')}</strong>
        <span>{objectLabel(object.status)} / {objectLabel(object.provenance_state)} / {objectLabel(object.risk_level)}</span>
      </button>
    {/each}
  </div>

  <RuntimeUxCards {runtimeUx} />
</section>

<style>
  .object-drawer {
    border: 1px solid var(--border-default, #334155);
    border-radius: 8px;
    background: var(--surface-elevated, #111827);
    min-width: 0;
  }

  header {
    display: flex;
    justify-content: space-between;
    gap: 10px;
    border-bottom: 1px solid var(--border-default, #334155);
    padding: 10px 12px;
  }

  h2 {
    margin: 0;
    font-size: 0.92rem;
  }

  header span,
  button span {
    color: var(--text-muted, #94a3b8);
    font-size: 0.78rem;
  }

  .object-list {
    display: grid;
    max-height: 520px;
    overflow: auto;
    padding: 8px;
  }

  button {
    display: grid;
    gap: 3px;
    border: 0;
    border-radius: 6px;
    background: transparent;
    color: var(--text-primary, #e5e7eb);
    padding: 9px 10px;
    text-align: left;
    cursor: pointer;
  }

  button.active,
  button:hover {
    background: rgba(56, 189, 248, 0.12);
  }

  button:focus-visible {
    outline: 2px solid #38bdf8;
    outline-offset: 2px;
  }

  .empty-state {
    margin: 0;
    padding: 10px;
    color: var(--text-muted, #94a3b8);
    font-size: 0.82rem;
  }

  strong {
    overflow-wrap: anywhere;
  }

  .kind {
    text-transform: uppercase;
  }
</style>
