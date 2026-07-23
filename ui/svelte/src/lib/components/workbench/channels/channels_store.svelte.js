import {
  deliverWorkbenchChannel,
  fetchWorkbenchChannelActivity,
  routeWorkbenchChannelCommand,
  workbenchChannels,
} from '$lib/api.js';
import { redactSupplyChainPayload } from '$lib/security';

export class WorkbenchChannelsStore {
  config = $state(null);
  activity = $state([]);
  deliveryResult = $state(null);
  commandResult = $state(null);
  loading = $state(false);
  error = $state('');

  async load() {
    this.loading = true;
    this.error = '';
    try {
      this.config = redactSupplyChainPayload(await workbenchChannels());
      const activity = await fetchWorkbenchChannelActivity();
      this.activity = Array.isArray(activity?.items) ? redactSupplyChainPayload(activity.items) : [];
    } catch (err) {
      this.config = null;
      this.activity = [];
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  async previewDelivery(payload) {
    this.loading = true;
    this.error = '';
    try {
      this.deliveryResult = redactSupplyChainPayload(await deliverWorkbenchChannel(payload));
      this.activity = [this.deliveryResult.activity, ...this.activity].filter(Boolean);
    } catch (err) {
      this.deliveryResult = null;
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }

  async routeCommand(payload) {
    this.loading = true;
    this.error = '';
    try {
      this.commandResult = redactSupplyChainPayload(await routeWorkbenchChannelCommand(payload));
      this.activity = [this.commandResult.activity, ...this.activity].filter(Boolean);
    } catch (err) {
      this.commandResult = null;
      this.error = err instanceof Error ? err.message : String(err);
    } finally {
      this.loading = false;
    }
  }
}

export function createWorkbenchChannelsStore() {
  return new WorkbenchChannelsStore();
}
