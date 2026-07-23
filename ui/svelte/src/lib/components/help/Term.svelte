<script module>
  import { listGlossary } from '$lib/api.js';

  /** @type {Map<string, {term:string,short:string,long:string,see_also:string[],category:string}> | null} */
  let sharedGlossaryMap = null;
  let sharedFetchPromise = null;

  function getSharedGlossaryMap() {
    if (sharedGlossaryMap !== null) return Promise.resolve(sharedGlossaryMap);
    if (sharedFetchPromise) return sharedFetchPromise;
    sharedFetchPromise = listGlossary().then((entries) => {
      const map = new Map();
      for (const entry of entries) {
        map.set(entry.term.toLowerCase(), entry);
      }
      sharedGlossaryMap = map;
      sharedFetchPromise = null;
      return map;
    });
    return sharedFetchPromise;
  }
</script>

<script>
  /**
   * Inline glossary term renderer.
   *
   * Wraps a domain term in an <abbr> element whose title comes from the
   * glossary API. The glossary is fetched lazily on first mount and cached
   * at module level so all Term instances on the same page share one request.
   *
   * When the term is not found in the glossary the text is rendered plain
   * with a data-glossary-miss attribute — no silent failure, no exception.
   *
   * see_also links open an inline HelpPopover on click.
   */
  import HelpPopover from '$lib/components/help/HelpPopover.svelte';

  const { term, fallback = undefined } = $props();

  // -- Module-level shared glossary cache (single fetch per page load) ---------
  // Anti-pattern guard: NOT populated at import time — lazy on first $effect.

  /**
   * Return the module-level glossary map, fetching it once if needed.
   * Subsequent calls reuse the in-flight promise so only one request fires
   * even when multiple Term instances mount concurrently.
   *
   * @returns {Promise<Map<string, object>>}
   */
  function getGlossaryMap() {
    return getSharedGlossaryMap();
  }

  // -- Per-instance resolved state ---------------------------------------------

  /** Resolved glossary entry, null while loading, undefined when not found. */
  let entry = $state(null);
  let loaded = $state(false);

  $effect(() => {
    loaded = false;
    entry = null;
    getGlossaryMap().then((map) => {
      entry = map.get(term.toLowerCase()) ?? undefined;
      loaded = true;
    }).catch(() => {
      entry = undefined;
      loaded = true;
    });
  });

  const displayText = $derived(fallback ?? term);
</script>

{#if loaded && entry !== undefined}
  <abbr
    title={entry.short}
    class="glossary-term"
    data-term={term}
    data-category={entry.category}
    aria-label={`${displayText}: ${entry.short}`}
  >{displayText}{#if entry.see_also && entry.see_also.length > 0}<span class="glossary-related" aria-label="Related terms">
      {#each entry.see_also as related}
        <HelpPopover
          title={related}
          body="See also: {related}"
          id="see-also-{term}-{related}"
        />
      {/each}
    </span>{/if}</abbr>
{:else if loaded && entry === undefined}
  <span
    data-glossary-miss={term}
    class="glossary-term-miss"
    role="note"
    aria-label="Glossary miss"
    title="Term not in glossary: {term}"
  >{displayText}</span>
{:else}
  <!-- Loading state — render plain text while fetch completes -->
  <span class="glossary-term-loading">{displayText}</span>
{/if}

<style>
  .glossary-term {
    text-decoration: underline dotted var(--text-muted, #888);
    text-underline-offset: 2px;
    cursor: help;
  }

  .glossary-related {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    margin-left: 4px;
    font-size: 0.75em;
  }

  .glossary-term-miss {
    /* No special styling — just plain text with the data attribute */
    font-style: inherit;
  }

  .glossary-term-loading {
    opacity: 0.7;
  }
</style>
