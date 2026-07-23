<script lang="ts">
  let { id = undefined, message = '', politeness = 'polite', atomic = true } = $props<{
    id?: string;
    message?: string;
    politeness?: 'polite' | 'assertive';
    atomic?: boolean;
  }>();
</script>

<div
  {id}
  class="sr-only"
  role={politeness === 'assertive' ? 'alert' : 'status'}
  aria-live={politeness}
  aria-atomic={atomic ? 'true' : 'false'}
>
  {message}
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
