const API_ROOT = '/api/v1/workbench/migration';
import { workbenchKernelRequest } from '$lib/api.js';
import { requireObject } from '../../../api_contract.js';

async function jsonFetch(url, options = {}) {
  const body = await workbenchKernelRequest(url, {
    headers: { 'content-type': 'application/json', ...(options.headers ?? {}) },
    ...options,
  });
  return body;
}

class WorkbenchMigrationStore {
  plan = $state(null);
  result = $state(null);
  selected = $state(new Set());
  secretSelection = $state(new Set());
  conflictSelections = $state({});
  backupConfirmed = $state(false);
  isLoading = $state(false);
  lastError = $state(null);

  riskyCount = $derived((this.plan?.findings ?? []).filter((item) => item.risk !== 'low').length);
  selectedCount = $derived(this.selected.size);

  async loadPlan(options = {}) {
    this.isLoading = true;
    this.lastError = null;
    try {
      const response = await jsonFetch(`${API_ROOT}/plan`, {
        method: 'POST',
        body: JSON.stringify({ dry_run: true, ...options }),
      });
      this.plan = requireObject(response.plan, 'migration plan response.plan');
      this.result = null;
      this.selected = new Set(
        (this.plan?.findings ?? []).filter((item) => item.default_selected).map((item) => item.item_id)
      );
      this.secretSelection = new Set();
      this.conflictSelections = {};
      this.backupConfirmed = false;
      return this.plan;
    } catch (error) {
      this.lastError = error.message ?? String(error);
      throw error;
    } finally {
      this.isLoading = false;
    }
  }

  toggleItem(item) {
    const next = new Set(this.selected);
    if (next.has(item.item_id)) {
      next.delete(item.item_id);
    } else {
      next.add(item.item_id);
    }
    this.selected = next;
  }

  setSecretIncluded(item, included) {
    const next = new Set(this.secretSelection);
    if (included) {
      next.add(item.item_id);
    } else {
      next.delete(item.item_id);
    }
    this.secretSelection = next;
  }

  chooseConflict(conflict, itemId) {
    this.conflictSelections = { ...this.conflictSelections, [conflict.conflict_key]: itemId };
    this.selected = new Set([...this.selected, itemId]);
  }

  async applySelection() {
    if (!this.plan) return null;
    this.isLoading = true;
    this.lastError = null;
    try {
      const response = await jsonFetch(`${API_ROOT}/apply`, {
        method: 'POST',
        body: JSON.stringify({
          proposal_id: this.plan.proposal_id,
          selected_item_ids: Array.from(this.selected),
          include_secret_item_ids: Array.from(this.secretSelection),
          conflict_selections: this.conflictSelections,
          backup_confirmed: this.backupConfirmed,
        }),
      });
      this.result = requireObject(response.result, 'migration apply response.result');
      return this.result;
    } catch (error) {
      this.lastError = error.message ?? String(error);
      throw error;
    } finally {
      this.isLoading = false;
    }
  }
}

export const migrationStore = new WorkbenchMigrationStore();
export { WorkbenchMigrationStore };
