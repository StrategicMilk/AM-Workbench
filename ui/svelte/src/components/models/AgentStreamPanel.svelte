<script>
  import * as engineStream from '$lib/stores/engineStream.svelte.js';

  const SCROLLBACK_CHARS = 65536;
  let text = $state('');
  let terminalState = $state('streaming');
  let cancelling = $state(false);

  function append(payload) {
    const token = payload?.delta?.content ?? payload?.delta ?? payload?.token ?? payload?.content ?? '';
    text = `${text}${token}`.slice(-SCROLLBACK_CHARS);
    if (payload?.finish_reason || payload?.done) terminalState = 'complete';
  }

  async function cancel() {
    cancelling = true;
    try {
      await engineStream.cancelGeneration();
      terminalState = 'aborted';
    } catch {
      terminalState = 'error';
    } finally {
      cancelling = false;
    }
  }

  $effect(() => {
    engineStream.subscribe({ message: append, error: () => { terminalState = 'error'; } });
    return engineStream.unsubscribe;
  });
</script>

<section class="stream-panel" aria-label="Agent generation stream">
  <header><h3>Live generation</h3><span class="terminal {terminalState}">{terminalState}</span></header>
  <pre role="status" aria-live="polite" aria-atomic="false">{text || 'Waiting for tokens…'}</pre>
  <button type="button" onclick={cancel} disabled={cancelling || terminalState !== 'streaming'}>
    {cancelling ? 'Cancelling…' : 'Cancel generation'}
  </button>
</section>

<style>
  .stream-panel { display: grid; gap: 8px; padding: 12px; border: 1px solid var(--border-default); border-radius: 8px; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  h3 { margin: 0; font-size: .9375rem; }
  pre { margin: 0; min-height: 80px; max-height: 240px; overflow: auto; white-space: pre-wrap; font-family: inherit; }
  .terminal { text-transform: capitalize; font-size: .75rem; }
  .complete { color: var(--success); } .aborted, .error { color: var(--warning); }
  button { justify-self: start; }
</style>
