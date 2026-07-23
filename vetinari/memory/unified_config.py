"""Configuration constants for the unified long-term memory store.

The unified store reads these values once at import time so the storage,
search, embedding, and consolidation helpers agree on the same operational
limits and model settings.
"""

from __future__ import annotations

import os

from vetinari.constants import DEFAULT_EMBEDDING_API_URL

EMBEDDING_API_URL = DEFAULT_EMBEDDING_API_URL
EMBEDDING_MODEL = os.environ.get("VETINARI_EMBEDDING_MODEL", "text-embedding-nomic-embed-text-v1.5")
MAX_LONG_TERM_ENTRIES = int(os.environ.get("VETINARI_MAX_MEMORY_ENTRIES", "10000"))
SEMANTIC_DEDUP_THRESHOLD = float(os.environ.get("VETINARI_SEMANTIC_DEDUP_THRESHOLD", "0.85"))
CONSOLIDATION_QUALITY_THRESHOLD = float(os.environ.get("VETINARI_CONSOLIDATION_QUALITY_THRESHOLD", "0.7"))
EPISODE_PROMOTION_THRESHOLD = int(os.environ.get("VETINARI_EPISODE_PROMOTION_THRESHOLD", "10"))
SESSION_MAX_ENTRIES = int(os.environ.get("VETINARI_SESSION_MAX_ENTRIES", "100"))
EMBEDDING_DIMENSIONS = int(os.environ.get("VETINARI_EMBEDDING_DIMENSIONS", "768"))
