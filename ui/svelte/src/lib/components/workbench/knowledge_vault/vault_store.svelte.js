import {
  exportKnowledgeVault,
  getKnowledgeVaultEntries,
  getKnowledgeVaultRejected,
  getMemoryRefinementJournal,
  rebuildKnowledgeVault,
  reverseMemoryRefinementEntry,
} from '$lib/api.js';
import { requireEvidence } from '$lib/evidence/evidenceGuard.js';

function entryEvidenceRefs(entry) {
  return [
    ...(Array.isArray(entry?.evidence_refs) ? entry.evidence_refs : []),
    ...(Array.isArray(entry?.provenance_refs) ? entry.provenance_refs : []),
    entry?.source_ref,
    entry?.receipt_ref,
  ].filter(Boolean);
}

function validateEntries(entries, context) {
  for (const entry of entries) {
    const refs = entryEvidenceRefs(entry);
    if (refs.length === 0) {
      throw new Error(`${context}:missing_evidence_refs:${entry?.entry_id ?? entry?.id ?? 'entry'}`);
    }
    requireEvidence(refs, `${context}:${entry?.entry_id ?? entry?.id ?? 'entry'}`);
  }
}

export class KnowledgeVaultStore {
  entries = $state([]);
  selectedEntry = $state(null);
  filters = $state({ kind: '', min_confidence: 0, requested_scope: 'shareable' });
  rejectedEntries = $state([]);
  journalEntries = $state([]);
  loading = $state(false);
  error = $state(null);

  async loadEntries() {
    this.loading = true;
    this.error = null;
    try {
      const payload = await getKnowledgeVaultEntries(this.filters);
      this.entries = payload.entries ?? [];
      this.rejectedEntries = payload.rejected ?? [];
      validateEntries(this.entries, 'knowledge-vault:entries');
    } catch (err) {
      this.error = err.message ?? String(err);
    } finally {
      this.loading = false;
    }
  }

  selectEntry(entry) {
    this.selectedEntry = entry;
  }

  async triggerExport(scope = this.filters.requested_scope) {
    validateEntries(this.entries, 'knowledge-vault:export');
    return exportKnowledgeVault(scope);
  }

  async triggerRebuild() {
    validateEntries(this.entries, 'knowledge-vault:rebuild');
    return rebuildKnowledgeVault();
  }

  async loadRejected() {
    const payload = await getKnowledgeVaultRejected();
    this.rejectedEntries = payload.rejected ?? [];
  }

  async loadJournal() {
    const payload = await getMemoryRefinementJournal();
    this.journalEntries = payload.entries ?? [];
  }

  async reverseJournalEntry(eventId, reason) {
    requireEvidence([eventId, reason], 'knowledge-vault:reverse-journal-entry');
    const entry = await reverseMemoryRefinementEntry(eventId, reason);
    await this.loadJournal();
    return entry;
  }
}
