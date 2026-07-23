"""Tiny deterministic tokenizer for Workbench scratch-model experiments."""

from __future__ import annotations

from dataclasses import dataclass


class TinyTokenizerError(ValueError):
    """Raised when tokenizer input or state cannot be trusted."""


SPECIAL_TOKENS = ("<pad>", "<bos>", "<eos>", "<unk>")
PAD_TOKEN, BOS_TOKEN, EOS_TOKEN, UNK_TOKEN = SPECIAL_TOKENS


@dataclass(frozen=True, slots=True)
class TinyTokenizer:
    """Character tokenizer with explicit BOS/EOS and unknown-token handling."""

    token_to_id: dict[str, int]

    def __post_init__(self) -> None:
        if not isinstance(self.token_to_id, dict):
            raise TinyTokenizerError("token_to_id must be a dict")
        expected = {token: index for index, token in enumerate(SPECIAL_TOKENS)}
        for token, index in expected.items():
            if self.token_to_id.get(token) != index:
                raise TinyTokenizerError(f"special token {token!r} must have id {index}")
        if len(set(self.token_to_id.values())) != len(self.token_to_id):
            raise TinyTokenizerError("token ids must be unique")
        if any(not isinstance(token, str) or not token for token in self.token_to_id):
            raise TinyTokenizerError("tokens must be non-empty strings")

    @classmethod
    def train(cls, samples: tuple[str, ...]) -> TinyTokenizer:
        """Build a deterministic vocabulary from governed text samples.

        Returns:
            TinyTokenizer value produced by train().
        """
        _require_samples(samples)
        chars = sorted({char for sample in samples for char in sample})
        token_to_id = {token: index for index, token in enumerate(SPECIAL_TOKENS)}
        for char in chars:
            if char not in token_to_id:
                token_to_id[char] = len(token_to_id)
        return cls(token_to_id=token_to_id)

    @property
    def id_to_token(self) -> dict[int, str]:
        """Return the inverse vocabulary."""
        return {index: token for token, index in self.token_to_id.items()}

    @property
    def vocab_size(self) -> int:
        """Return vocabulary size."""
        return len(self.token_to_id)

    def encode(self, text: str, *, add_bos_eos: bool = True) -> tuple[int, ...]:
        """Encode text, mapping unseen characters to ``<unk>``.

        Returns:
            tuple[int, ...] value produced by encode().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(text, str) or not text:
            raise TinyTokenizerError("text must be a non-empty string")
        ids = [self.token_to_id.get(char, self.token_to_id[UNK_TOKEN]) for char in text]
        if add_bos_eos:
            return (self.token_to_id[BOS_TOKEN], *ids, self.token_to_id[EOS_TOKEN])
        return tuple(ids)

    def decode(self, token_ids: tuple[int, ...], *, skip_special: bool = True) -> str:
        """Decode token ids and fail closed on invalid ids.

        Returns:
            str value produced by decode().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        if not isinstance(token_ids, tuple) or not token_ids:
            raise TinyTokenizerError("token_ids must be a non-empty tuple")
        inverse = self.id_to_token
        pieces: list[str] = []
        for token_id in token_ids:
            if token_id not in inverse:
                raise TinyTokenizerError(f"unknown token id {token_id}")
            token = inverse[token_id]
            if skip_special and token in SPECIAL_TOKENS:
                continue
            pieces.append(token)
        return "".join(pieces)

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-safe tokenizer payload."""
        return {"token_to_id": dict(sorted(self.token_to_id.items(), key=lambda item: item[1]))}

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> TinyTokenizer:
        """Load a tokenizer from a JSON-safe payload.

        Returns:
            TinyTokenizer value produced by from_dict().

        Raises:
            Exception: Propagates validation or runtime failures from the underlying operation.
        """
        raw = payload.get("token_to_id") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            raise TinyTokenizerError("payload missing token_to_id")
        return cls(token_to_id={str(token): int(index) for token, index in raw.items()})


def _require_samples(samples: tuple[str, ...]) -> None:
    if not isinstance(samples, tuple) or not samples:
        raise TinyTokenizerError("samples must be a non-empty tuple")
    if any(not isinstance(sample, str) or not sample.strip() for sample in samples):
        raise TinyTokenizerError("samples must contain non-empty strings")


__all__ = [
    "BOS_TOKEN",
    "EOS_TOKEN",
    "PAD_TOKEN",
    "SPECIAL_TOKENS",
    "UNK_TOKEN",
    "TinyTokenizer",
    "TinyTokenizerError",
]
