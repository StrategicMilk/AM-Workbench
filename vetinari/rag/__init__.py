"""Retrieval-Augmented Generation: knowledge base, chunking, and vector search."""

from __future__ import annotations

from vetinari.rag.knowledge_base import (
    KBDocument,
    KnowledgeBase,
    get_knowledge_base,
    ingest_project_docs,
)

__all__ = [
    "KBDocument",
    "KnowledgeBase",
    "get_knowledge_base",
    "ingest_project_docs",
]
