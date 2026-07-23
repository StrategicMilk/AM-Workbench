<script>
  import { getToasts, dismissToast } from '$lib/stores/toast.svelte.js';
  import AriaLiveRegion from '$lib/a11y/AriaLiveRegion.svelte';
  import Icon from '$lib/a11y/Icon.svelte';

  let toasts = $derived(getToasts());

  const ICONS = {
    info: 'info-circle',
    success: 'check-circle',
    warning: 'exclamation-triangle',
    error: 'times-circle',
  };
</script>

<AriaLiveRegion />

{#if toasts.length > 0}
  <div class="toast-stack" aria-label="Notifications">
    {#each toasts as toast (toast.id)}
      <div
        class="toast toast-{toast.type}"
        role={toast.type === 'error' ? 'alert' : 'status'}
        aria-live={toast.type === 'error' ? 'assertive' : 'polite'}
      >
        <Icon name={ICONS[toast.type] ?? ICONS.info} />
        <span class="toast-message">{toast.message}</span>
        <button
          type="button"
          class="toast-close"
          onclick={() => dismissToast(toast.id)}
          aria-label={`Dismiss notification: ${toast.message}`}
        >
          <Icon name="times" />
        </button>
      </div>
    {/each}
  </div>
{/if}

<style>
  .toast-stack {
    position: fixed;
    bottom: 24px;
    right: 24px;
    z-index: 2000;
    display: flex;
    flex-direction: column-reverse;
    gap: 8px;
    max-width: 420px;
  }

  .toast {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 12px 16px;
    border-radius: 8px;
    background: var(--surface-elevated, #1a202d);
    border: 1px solid var(--border-default);
    color: var(--text-primary);
    font-size: 14px;
    box-shadow: 0 8px 24px rgba(0, 0, 0, 0.25);
    animation: toast-in 200ms ease-out;
  }

  .toast-success { border-left: 3px solid var(--success, #38d39f); }
  .toast-warning { border-left: 3px solid var(--warning, #f5a524); }
  .toast-error   { border-left: 3px solid var(--danger, #f06262); }
  .toast-info    { border-left: 3px solid var(--primary, #4e9af9); }

  .toast-success i { color: var(--success); }
  .toast-warning i { color: var(--warning); }
  .toast-error i   { color: var(--danger); }
  .toast-info i    { color: var(--primary); }

  .toast-message {
    flex: 1;
  }

  .toast-close {
    background: none;
    border: none;
    color: var(--text-muted);
    cursor: pointer;
    padding: 4px;
    font-size: 12px;
  }

  .toast-close:hover {
    color: var(--text-primary);
  }

  @keyframes toast-in {
    from { opacity: 0; transform: translateY(10px); }
    to   { opacity: 1; transform: translateY(0); }
  }

  @media (prefers-reduced-motion: reduce) {
    .toast {
      animation: none;
      transition: none;
    }
  }
</style>
