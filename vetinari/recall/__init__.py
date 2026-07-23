"""Episodic recall facade — public surface for vetinari memory and episodic recall.

Unifies vetinari/memory/recall_contract and vetinari/learning/episodic_recall
under one stable import path.

Public surface:

- ``RecallStore`` — unified facade for recall queries and recording.
  Delegates to vetinari.learning.episodic_recall (read) and
  vetinari.learning.episode_memory (write).

- ``EpisodicRecall`` — re-export of the planning-oriented retrieval
  functions from vetinari.learning.episodic_recall, exposed as a
  module-level reference for callers that prefer functional access.

- ``recall_contract_version`` — integer schema version from
  vetinari.memory.recall_contract.SCHEMA_VERSION; lets consumers detect
  breaking schema changes without importing the contract module directly.

- Contract types re-exported for callers that need typed access:
  ``MemoryRecallPack``, ``MemoryRecallItem``, ``RecallProfile``,
  ``RecallStatus``, ``AuthorityTier``, ``TaintSignal``,
  ``RecallTokenBudget``, ``RecallContractError``.
"""

from __future__ import annotations

from vetinari.learning import episodic_recall as EpisodicRecall
from vetinari.memory.recall_contract import (
    SCHEMA_VERSION as recall_contract_version,
)
from vetinari.memory.recall_contract import (
    AuthorityTier,
    MemoryRecallItem,
    MemoryRecallPack,
    RecallContractError,
    RecallProfile,
    RecallStatus,
    RecallTokenBudget,
    TaintSignal,
)
from vetinari.recall.store import RecallStore, RecallStoreError

__all__ = [
    "AuthorityTier",
    "EpisodicRecall",
    "MemoryRecallItem",
    "MemoryRecallPack",
    "RecallContractError",
    "RecallProfile",
    "RecallStatus",
    "RecallStore",
    "RecallStoreError",
    "RecallTokenBudget",
    "TaintSignal",
    "recall_contract_version",
]
