<script>
  import JourneyLink from './JourneyLink.svelte';

  let {
    projectId = 'default',
    runId = '',
    traceId = '',
    assetId = '',
    proposalId = '',
    label = 'Trace',
  } = $props();

  let canOpen = $derived(Boolean(runId && traceId && assetId && proposalId));
</script>

<JourneyLink
  target="console"
  {projectId}
  disabled={!canOpen}
  {label}
  ariaLabel="Open promotion evidence trace in Workbench Console"
  params={{
    runId,
    traceId,
    evidenceTraceId: traceId,
    assetId,
    proposalId,
  }}
/>
