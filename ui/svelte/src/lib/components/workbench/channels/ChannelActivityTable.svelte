<script>
  import { formatBlockedReason, formatChannelState, formatRedactionState } from '$lib/utils/displayLabels';

  let { items = [] } = $props();
</script>

<section class="activity-panel" aria-label="Channel activity">
  <header><h2>Activity</h2><span>{items.length} records</span></header>
  <table class="activity-table">
    <thead>
      <tr>
        <th scope="col">channel</th>
        <th scope="col">state</th>
        <th scope="col">redaction</th>
        <th scope="col">reason</th>
      </tr>
    </thead>
    <tbody>
      {#each items as item}
        <tr>
          <td>{item.channel_id}</td>
          <td data-state={item.state}>{formatChannelState(item.state)}</td>
          <td>{formatRedactionState(item.redaction_applied)}</td>
          <td>{formatBlockedReason(item.blocked_reason)}</td>
        </tr>
      {:else}
        <tr>
          <td colspan="4" class="empty">No channel activity records.</td>
        </tr>
      {/each}
    </tbody>
  </table>
</section>

<style>
  .activity-panel { display: flex; flex-direction: column; gap: 12px; }
  header { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
  h2 { margin: 0; font-size: 18px; }
  header span { color: var(--text-muted); font-size: 13px; }
  .activity-table { width: 100%; border-collapse: separate; border-spacing: 0; border: 1px solid var(--border-default); border-radius: 8px; overflow: hidden; }
  th, td { padding: 9px 10px; border-bottom: 1px solid var(--border-subtle, var(--border-default)); text-align: left; }
  th { background: var(--surface-elevated); color: var(--text-muted); font-weight: 700; text-transform: uppercase; font-size: 11px; }
  td { overflow-wrap: anywhere; }
  tbody tr:last-child td { border-bottom: 0; }
  .empty { color: var(--text-muted); text-align: center; }
  [data-state="blocked"], [data-state="approval_required"] { color: #f36b6b; }
  [data-state="delivered"], [data-state="redacted"] { color: #43b37a; }
  @media (max-width: 760px) { .activity-panel { overflow-x: auto; } .activity-table { min-width: 560px; } }
</style>
