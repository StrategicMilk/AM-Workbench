<script>
  import Icon from '$lib/a11y/Icon.svelte';

  let {
    disabled = false,
    supportsVision = true,
    images = [],
    onImagesChange = () => {},
  } = $props();

  let inputEl = $state(null);
  let localImages = $state([]);
  let error = $state('');
  let reading = $state(false);
  const MAX_IMAGES = 6;
  const privacyNotice = 'Images are attached only to this prompt and are not retained after the run unless you explicitly save the output.';

  function boundedImages(values) {
    if (!Array.isArray(values)) return [];
    return values.slice(0, MAX_IMAGES);
  }

  function openPicker() {
    if (!disabled && supportsVision) {
      inputEl?.click();
    }
  }

  function readAsDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.onerror = () => reject(new Error(`Could not read ${file.name}`));
      reader.readAsDataURL(file);
    });
  }

  async function handleFiles(files) {
    error = '';
    reading = true;
    try {
      const availableSlots = Math.max(MAX_IMAGES - localImages.length, 0);
      if (availableSlots === 0) {
        error = `Image limit reached (${MAX_IMAGES})`;
        return;
      }
      const nextImages = [];
      for (const file of files.slice(0, availableSlots)) {
        if (!file.type.startsWith('image/')) {
          error = `${file.name} is not an image`;
          continue;
        }
        const dataUrl = await readAsDataUrl(file);
        nextImages.push({
          id: crypto.randomUUID(),
          name: file.name,
          data_url: dataUrl,
          mime_type: file.type,
          size: file.size,
        });
      }
      if (files.length > availableSlots) {
        error = `Only ${MAX_IMAGES} images can be attached`;
      }
      localImages = boundedImages([...localImages, ...nextImages]);
      onImagesChange(localImages);
    } catch (err) {
      error = err.message ?? String(err);
    } finally {
      reading = false;
    }
  }

  function handleInput(event) {
    const files = Array.from(event.currentTarget.files ?? []);
    event.currentTarget.value = '';
    if (files.length > 0) {
      void handleFiles(files);
    }
  }

  function removeImage(id) {
    localImages = localImages.filter((item) => item.id !== id);
    onImagesChange(localImages);
  }

  $effect(() => {
    localImages = boundedImages(images);
  });
</script>

<div class="vision-input" data-testid="fsa0047-vision-input">
  <input
    type="file"
    accept="image/*"
    multiple
    hidden
    bind:this={inputEl}
    onchange={handleInput}
    aria-hidden="true"
  />
  <button
    type="button"
    class="vision-button"
    onclick={openPicker}
    disabled={disabled || !supportsVision || reading}
    aria-label="Attach image"
    aria-describedby="vision-input-privacy"
    title={supportsVision ? 'Attach image' : 'Selected model does not accept images'}
  >
    <Icon name={reading ? 'spinner' : 'image'} class={reading ? 'fa-spin' : ''} />
  </button>

  {#if localImages.length > 0}
    <div class="vision-strip" aria-label="Selected images">
      {#each localImages as image (image.id)}
        <button
          type="button"
          class="vision-chip"
          onclick={() => removeImage(image.id)}
          aria-label="Remove image {image.name}"
          title="Remove {image.name}"
        >
          <img src={image.data_url} alt="" />
          <span>{image.name}</span>
          <Icon name="times" />
        </button>
      {/each}
    </div>
  {/if}

  {#if error}
    <span class="vision-error" role="alert">{error}</span>
  {/if}
  <span id="vision-input-privacy" class="privacy-notice">{privacyNotice}</span>
</div>

<style>
  .vision-input {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
    min-width: 0;
  }

  .vision-button {
    width: 38px;
    height: 38px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-elevated);
    color: var(--text-primary);
    flex-shrink: 0;
  }

  .vision-button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }

  .vision-strip {
    display: flex;
    gap: 6px;
    overflow-x: auto;
    min-width: 0;
  }

  .vision-chip {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    max-width: 180px;
    min-height: 34px;
    border: 1px solid var(--border-default);
    border-radius: 8px;
    background: var(--surface-bg);
    color: var(--text-secondary);
    padding: 4px 8px 4px 4px;
  }

  .vision-chip img {
    width: 26px;
    height: 26px;
    object-fit: cover;
    border-radius: 4px;
    flex-shrink: 0;
  }

  .vision-chip span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    font-size: 0.76rem;
  }

  .vision-error {
    color: var(--danger);
    font-size: 0.76rem;
  }

  .privacy-notice {
    flex-basis: 100%;
    color: var(--text-muted);
    font-size: 0.72rem;
    line-height: 1.3;
  }
</style>
