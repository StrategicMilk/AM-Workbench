<script>
  let { active = false, restoreTo = null, children } = $props();
  let container = $state();
  let previousFocus = $state(null);

  const focusableSelector = [
    'a[href]',
    'button:not([disabled])',
    'textarea:not([disabled])',
    'input:not([disabled])',
    'select:not([disabled])',
    '[role="button"]:not([aria-disabled="true"])',
    '[tabindex]:not([tabindex="-1"])',
  ].join(',');

  $effect(() => {
    if (!active || !container) return;
    previousFocus = document.activeElement;
    const focusable = Array.from(container.querySelectorAll(focusableSelector));
    focusable[0]?.focus();

    return () => {
      const target = restoreTo ?? previousFocus;
      if (target && typeof target.focus === 'function') target.focus();
    };
  });

  function onKeydown(event) {
    if (!active || event.key !== 'Tab' || !container) return;
    const focusable = Array.from(container.querySelectorAll(focusableSelector));
    if (!focusable.length) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }
</script>

<svelte:window onkeydown={onKeydown} />

<div bind:this={container}>
  {@render children?.()}
</div>
