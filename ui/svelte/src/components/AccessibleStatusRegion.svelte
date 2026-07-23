<script>
  let { message = '', critical = false } = $props();
</script>

<div class="sr-status" role={critical ? 'alert' : 'status'} aria-live={critical ? 'assertive' : 'polite'} aria-atomic="true">
  {message}
</div>

<style>
  .sr-status {
    position: absolute;
    width: 1px;
    height: 1px;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }
</style>
