"""Successful real-process contract proof for native AM Engine embeddings."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from tests.test_engine_contract import _load_model, _request, _running_engine


def _embedding_vectors(payload: dict[str, Any]) -> list[list[float]]:
    assert payload["object"] == "list"
    data = payload["data"]
    assert [item["index"] for item in data] == list(range(len(data)))
    return [item["embedding"] for item in data]


def test_governed_fixture_returns_stable_normalized_native_embeddings(tmp_path: Path) -> None:
    """The governed GGUF must exercise embedding success through the real server."""
    with _running_engine(tmp_path) as engine:
        loaded = _load_model(engine, engine.healthy_model)["model"]
        assert loaded["supports_embeddings"] is True
        dimension = loaded["embedding_length"]
        assert dimension > 0

        request = {
            "model": engine.healthy_model,
            "input": ["governed embedding probe", "distinct semantic input"],
        }
        status, first_payload = _request(
            engine.port,
            "/v1/embeddings",
            body=request,
            timeout=30.0,
        )
        assert status == 200, first_payload
        status, repeated_payload = _request(
            engine.port,
            "/v1/embeddings",
            body=request,
            timeout=30.0,
        )
        assert status == 200, repeated_payload

        first = _embedding_vectors(first_payload)
        repeated = _embedding_vectors(repeated_payload)
        assert len(first) == len(request["input"])
        assert all(len(vector) == dimension for vector in first)
        assert all(math.isfinite(value) for vector in first for value in vector)
        for vector in first:
            norm = math.sqrt(sum(value * value for value in vector))
            assert math.isclose(norm, 1.0, rel_tol=1.0e-6, abs_tol=1.0e-6)
        assert first[0] != first[1]
        assert repeated == first
