import { workbenchKernelRequest } from '$lib/api.js';

export class ToolGuideStore {
  catalog = $state(null);
  selection = $state(null);
  loading = $state(false);
  error = $state(null);

  get selectedGuides() {
    return this.selection?.selected_guides ?? [];
  }

  get diagnostics() {
    return this.selection?.diagnostics ?? [];
  }

  async loadCatalog() {
    this.loading = true;
    this.error = null;
    try {
      this.catalog = await workbenchKernelRequest('/api/workbench/tool-guides/catalog');
      return this.catalog;
    } catch (err) {
      this.error = err?.message ?? String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  }

  async selectGuides(activeTools = [], tokenBudget = null) {
    this.loading = true;
    this.error = null;
    try {
      const body = { active_tools: activeTools };
      if (tokenBudget !== null && tokenBudget !== undefined) {
        body.token_budget = tokenBudget;
      }
      this.selection = await workbenchKernelRequest('/api/workbench/tool-guides/select', {
        method: 'POST',
        body: JSON.stringify(body),
      });
      return this.selection;
    } catch (err) {
      this.error = err?.message ?? String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  }
}
