<script>
  import { getAnnouncement } from '$lib/stores/liveAnnouncement.svelte.js';

  let { message = null, politeness = null } = $props();

  let announcement = $derived(getAnnouncement());
  let visibleMessage = $derived(
    typeof message === 'string' ? message.trim() : announcement.message
  );
  let livePoliteness = $derived(
    (politeness ?? announcement.politeness) === 'assertive' ? 'assertive' : 'polite'
  );
  let liveRole = $derived(livePoliteness === 'assertive' ? 'alert' : 'status');
</script>

<div class="sr-only" role={liveRole} aria-live={livePoliteness} aria-atomic="true" aria-relevant="additions text">
  {visibleMessage}
</div>

<style>
  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
