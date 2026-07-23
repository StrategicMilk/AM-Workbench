import * as api from '$lib/api.js';

export class HabitHealthStore {
  userId = $state('default');
  summary = $state(null);
  review = $state(null);
  downstreamPreview = $state(null);
  loading = $state(false);
  error = $state(null);

  constructor(userId = 'default') {
    this.userId = userId;
  }

  async runMutation(operation) {
    this.loading = true;
    this.error = null;
    try {
      const result = await operation();
      await this.load();
      return result;
    } catch (err) {
      this.error = err?.message ?? String(err);
      throw err;
    } finally {
      this.loading = false;
    }
  }

  async load() {
    this.loading = true;
    this.error = null;
    try {
      this.summary = await api.getHabitHealthSummary(this.userId);
      this.review = await api.reviewHabitHealthData(this.userId);
    } catch (err) {
      this.error = err?.message ?? String(err);
    } finally {
      this.loading = false;
    }
  }

  async createRoutine(payload) {
    return this.runMutation(() => api.createHabitHealthRoutine({ user_id: this.userId, ...payload }));
  }

  async recordCheckIn(payload) {
    return this.runMutation(() => api.recordHabitHealthCheckIn({ user_id: this.userId, ...payload }));
  }

  async previewDownstream(payload) {
    try {
      this.downstreamPreview = await api.previewHabitHealthDownstreamSignal({ user_id: this.userId, ...payload });
      this.error = null;
      return this.downstreamPreview;
    } catch (err) {
      this.downstreamPreview = null;
      this.error = err?.message ?? String(err);
      throw err;
    }
  }

  exportData(payload = {}) {
    return api.exportHabitHealthData({ user_id: this.userId, ...payload });
  }

  async deleteData(reason = 'user-request') {
    return this.runMutation(() => api.deleteHabitHealthData(this.userId, reason));
  }
}
