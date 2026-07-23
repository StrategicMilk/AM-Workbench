<!--
  IntakeWizard: small prompt-framing form with no props and no emitted events.
-->
<script lang="ts">
  import { submitIntakeWizard } from "$lib/api.js";

  type WizardResponse = {
    request_frame: {
      goal: string;
      persona_name: string | null;
      preferred_worker_mode: string | null;
      preferred_model_tier: string | null;
      urgency: "low" | "medium" | "high";
      scope_hint: string | null;
      destructive_intent: boolean;
      budget_tokens: number | null;
      raw_prompt: string;
    };
    persona_applied: string | null;
    worker_mode_cluster: string[];
  };

  const personas = [
    { value: "", label: "No persona" },
    { value: "rapid-prototyper", label: "Rapid prototyper" },
    { value: "quality-craftsperson", label: "Quality craftsperson" },
    { value: "analytical-researcher", label: "Analytical researcher" },
    { value: "refactor-surgeon", label: "Refactor surgeon" },
    { value: "infra-operator", label: "Infra operator" },
  ];

  let rawPrompt = $state("");
  let personaName = $state("");
  let response = $state<WizardResponse | null>(null);
  let errorMessage = $state("");
  let isSubmitting = $state(false);

  async function submitWizard(event: SubmitEvent) {
    event.preventDefault();
    errorMessage = "";
    response = null;
    isSubmitting = true;

    try {
      response = (await submitIntakeWizard({
        raw_prompt: rawPrompt,
        persona_name: personaName || null,
      })) as WizardResponse;
    } catch (err) {
      const detail = err instanceof Error ? err.message : String(err);
      if (detail) {
        errorMessage = `Intake failed. ${detail.slice(0, 180)}`;
      } else {
        errorMessage = "The intake service is unavailable. Try again when the native kernel is ready.";
      }
    } finally {
      isSubmitting = false;
    }
  }
</script>

<section class="intake-wizard">
  <form class="intake-wizard__form" onsubmit={submitWizard}>
    <div class="intake-wizard__field">
      <label for="intake-raw-prompt">Prompt</label>
      <textarea
        id="intake-raw-prompt"
        bind:value={rawPrompt}
        rows="6"
        required
        class="intake-wizard__textarea"
      ></textarea>
    </div>

    <div class="intake-wizard__field">
      <label for="intake-persona">Persona</label>
      <select id="intake-persona" bind:value={personaName} class="intake-wizard__select">
        {#each personas as persona}
          <option value={persona.value}>{persona.label}</option>
        {/each}
      </select>
    </div>

    <button type="submit" class="intake-wizard__button" disabled={isSubmitting}>
      {isSubmitting ? "Building" : "Build frame"}
    </button>
  </form>

  {#if errorMessage}
    <p class="intake-wizard__error" role="alert" aria-live="assertive">{errorMessage}</p>
  {/if}

  {#if response}
    <div class="intake-wizard__result" aria-live="polite">
      <h2>Request frame</h2>
      <dl>
        <div>
          <dt>Goal</dt>
          <dd>{response.request_frame.goal}</dd>
        </div>
        <div>
          <dt>Worker modes</dt>
          <dd>{response.worker_mode_cluster.join(", ")}</dd>
        </div>
      </dl>
    </div>
  {/if}
</section>

<style>
  .intake-wizard {
    display: grid;
    gap: 1rem;
    max-width: 48rem;
  }

  .intake-wizard__form {
    display: grid;
    gap: 0.875rem;
  }

  .intake-wizard__field {
    display: grid;
    gap: 0.375rem;
  }

  .intake-wizard__textarea,
  .intake-wizard__select {
    border: 1px solid var(--border-color, #8a8f98);
    border-radius: 6px;
    font: inherit;
    padding: 0.625rem;
    width: 100%;
  }

  .intake-wizard__button {
    align-self: start;
    border: 0;
    border-radius: 6px;
    cursor: pointer;
    font: inherit;
    font-weight: 600;
    padding: 0.625rem 0.875rem;
  }

  .intake-wizard__button:disabled {
    cursor: wait;
    opacity: 0.7;
  }

  .intake-wizard__error {
    font-weight: 600;
  }

  .intake-wizard__result {
    border-top: 1px solid var(--border-color, #8a8f98);
    padding-top: 1rem;
  }

  .intake-wizard__result dl {
    display: grid;
    gap: 0.75rem;
    margin: 0;
  }

  .intake-wizard__result div {
    display: grid;
    gap: 0.25rem;
  }

  .intake-wizard__result dt {
    font-weight: 700;
  }

  .intake-wizard__result dd {
    margin: 0;
  }

  @media (max-width: 640px) {
    .intake-wizard {
      max-width: none;
      width: 100%;
    }

    .intake-wizard__button {
      width: 100%;
    }

    .intake-wizard__result dl {
      gap: 0.5rem;
    }
  }
</style>
