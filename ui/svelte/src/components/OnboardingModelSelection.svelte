<script>
  let { selected = '', models = [], onSelectionChange = () => {} } = $props();

  let availableModels = $derived(
    Array.isArray(models)
      ? models.filter((model) => typeof model?.id === 'string' && model.id.length > 0)
      : []
  );
  let selectedModel = $state(selected);
  let selectedIsAvailable = $derived(
    selectedModel === '' || availableModels.some((model) => model.id === selectedModel)
  );
  let invalidSelectionReason = $derived(
    selectedIsAvailable ? '' : 'Selected model is unavailable.'
  );

  $effect(() => {
    selectedModel = typeof selected === 'string' ? selected : '';
  });

  function selectModel(model) {
    if (!model || typeof model.id !== 'string' || model.id.length === 0) {
      return;
    }
    selectedModel = model.id;
    onSelectionChange({
      modelId: model.id,
      model,
    });
  }
</script>

<fieldset aria-describedby={invalidSelectionReason ? 'model-selection-error' : undefined}>
  <legend>Model selection</legend>
  {#if invalidSelectionReason}
    <p id="model-selection-error" class="selection-error" role="alert">{invalidSelectionReason}</p>
  {/if}
  {#each availableModels as model (model.id)}
    <label>
      <input
        type="radio"
        value={model.id}
        checked={selectedModel === model.id}
        onchange={() => selectModel(model)}
      />
      <span>{model.label ?? model.id}</span>
    </label>
  {/each}
</fieldset>

<style>
  fieldset {
    display: grid;
    gap: 8px;
    border: 0;
    padding: 0;
    margin: 0;
  }

  legend {
    font-weight: 700;
    margin-bottom: 4px;
  }

  label {
    min-height: 44px;
    display: flex;
    align-items: center;
    gap: 8px;
  }

  .selection-error {
    margin: 0;
    color: var(--danger-color, #b91c1c);
    font-size: 0.9rem;
  }
</style>
