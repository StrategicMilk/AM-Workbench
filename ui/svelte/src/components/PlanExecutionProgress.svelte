<script>
  import { asArray, clampPercent, nonEmptyString } from '$lib/utils/safe.js';

  let { shards = [] } = $props();

  let safeShards = $derived(
    asArray(shards).map((shard, index) => ({
      id: nonEmptyString(shard?.shard_id, `shard-${index + 1}`),
      progress: clampPercent(shard?.progress, 0),
    })),
  );
</script>

<section class="plan-execution-progress" role="status" aria-live="polite" aria-label="Plan execution progress">
  {#each safeShards as shard (shard.id)}
    <article class="shard-row">
      <span id={`shard-${shard.id}-label`}>{shard.id}</span>
      <progress
        max="100"
        value={shard.progress}
        aria-labelledby={`shard-${shard.id}-label`}
        aria-valuetext={`${shard.progress}% complete`}
      ></progress>
    </article>
  {/each}
</section>

<style>
  .plan-execution-progress {
    display: grid;
    gap: 8px;
  }

  .shard-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr) 160px;
    align-items: center;
    min-height: 44px;
    gap: 12px;
  }
</style>
