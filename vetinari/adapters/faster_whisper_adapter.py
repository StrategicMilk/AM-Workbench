"""Faster Whisper ASR adapter."""

from __future__ import annotations

import importlib.util
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from vetinari.adapters.base import (
    InferenceRequest,
    InferenceResponse,
    ModelInfo,
    ProviderConfig,
    ProviderType,
    derive_model_cache_maxsize,
)
from vetinari.constants import INFERENCE_STATUS_ERROR, INFERENCE_STATUS_OK
from vetinari.utils.bounded_collections import BoundedDict

logger = logging.getLogger(__name__)
Transcriber = Callable[[Path, str], tuple[str, int]]


class FasterWhisperAdapter:
    """Optional Faster Whisper adapter with real ASR execution semantics."""

    lease_class = "audio"

    def __init__(self, config: ProviderConfig):
        self.config = config
        self.provider_type = ProviderType.FASTER_WHISPER
        self.name = config.name
        self.endpoint = config.endpoint
        self.model_size = str(config.extra_config.get("model_size") or config.endpoint or "base")
        transcriber = config.extra_config.get("transcriber")
        self._transcriber: Transcriber | None = cast(Transcriber, transcriber) if callable(transcriber) else None
        # Cache constructed WhisperModel instances keyed by (model_id, device)
        # so repeated infer() calls for the same model do not reload the
        # model weights from disk on every request (operability contract:
        # faster-whisper must reuse the model instance across infers).
        self._model_cache: BoundedDict[tuple[str, str], Any] = BoundedDict(
            derive_model_cache_maxsize(config, model_memory_gb=config.extra_config.get("memory_gb", 4))
        )

    def discover_models(self) -> list[ModelInfo]:
        """Return the configured ASR model only when faster-whisper is importable.

        Returns:
            Value produced for the caller.
        """
        if not self._is_installed() and self._transcriber is None:
            return []
        return [
            ModelInfo(
                id=self.model_size,
                name=f"faster-whisper-{self.model_size}",
                provider=self.provider_type.value,
                endpoint=self.endpoint,
                capabilities=["audio_transcription", "speech_to_text"],
                context_len=0,
                memory_gb=int(self.config.extra_config.get("memory_gb", 4)),
                version="local",
                tags=["asr", "faster-whisper"],
            )
        ]

    def health_check(self) -> dict[str, Any]:
        """Report dependency availability without constructing a model.

        Returns:
            Value produced for the caller.
        """
        if self._transcriber is not None:
            return {"healthy": True, "reason": "transcriber_configured", "timestamp": None}
        if self._is_installed():
            return {"healthy": True, "reason": "faster_whisper_importable", "timestamp": None}
        return {"healthy": False, "reason": "faster_whisper_not_installed", "timestamp": None}

    def infer(self, request: InferenceRequest) -> InferenceResponse:
        """Transcribe an audio file path from request metadata or prompt.

        Returns:
            Value produced for the caller.
        """
        start = time.monotonic()
        audio_path = _audio_path_from_request(request)
        if audio_path is None:
            return _error_response(request, "audio_path_required", start)
        if not audio_path.exists():
            return _error_response(request, "audio_path_not_found", start)
        if not audio_path.is_file():
            return _error_response(request, "audio_path_must_be_file", start)
        try:
            transcript, token_count = self._run_transcription(audio_path, request.model_id or self.model_size)
        except ImportError:
            logger.warning("Exception handled by infer fallback", exc_info=True)
            return _error_response(request, "faster_whisper_not_installed", start)
        except Exception as exc:
            logger.warning("Exception handled by infer fallback", exc_info=True)
            return _error_response(request, f"transcription_failed: {exc}", start)
        return InferenceResponse(
            model_id=request.model_id or self.model_size,
            output=transcript,
            latency_ms=_latency_ms(start),
            tokens_used=token_count,
            status=INFERENCE_STATUS_OK,
            metadata={"audio_ref": _path_ref(audio_path), "provider": self.provider_type.value},
        )

    def get_capabilities(self) -> dict[str, list[str]]:
        """Return ASR capabilities exposed by this adapter."""
        return {self.provider_type.value: ["audio_transcription", "speech_to_text"]}

    def _run_transcription(self, audio_path: Path, model_id: str) -> tuple[str, int]:
        transcriber = self._transcriber
        if transcriber is not None:
            return transcriber(audio_path, model_id)
        device = str(self.config.extra_config.get("device", "cpu"))
        cache_key = (model_id, device)
        model = self._model_cache.get(cache_key)
        if model is None:
            from faster_whisper import WhisperModel

            model = WhisperModel(model_id, device=device)
            self._model_cache[cache_key] = model
        segments, _info = model.transcribe(str(audio_path))
        text = " ".join(str(segment.text).strip() for segment in segments if str(segment.text).strip())
        return text, max(1, len(text.split()))

    @staticmethod
    def _is_installed() -> bool:
        return importlib.util.find_spec("faster_whisper") is not None


def _audio_path_from_request(request: InferenceRequest) -> Path | None:
    raw = request.metadata.get("audio_path")
    if not raw and request.metadata.get("allow_prompt_audio_path") is True:
        raw = request.prompt
    if not isinstance(raw, str) or not raw.strip():
        return None
    return Path(raw).expanduser()


def _path_ref(path: Path) -> str:
    resolved = path.expanduser().resolve()
    return f"{resolved.name}:len:{len(str(resolved))}"


def _error_response(request: InferenceRequest, error: str, start: float) -> InferenceResponse:
    return InferenceResponse(
        model_id=request.model_id,
        output="",
        latency_ms=_latency_ms(start),
        tokens_used=0,
        status=INFERENCE_STATUS_ERROR,
        error=error,
    )


def _latency_ms(start: float) -> int:
    return max(0, int((time.monotonic() - start) * 1000))


__all__ = ["FasterWhisperAdapter"]
