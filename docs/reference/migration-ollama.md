# Ollama Migration

Ollama is no longer a supported Vetinari inference backend. Remove `ollama`
from `inference_backend.primary`, fallback lists, provider preferences, support
matrix entries, and environment-based runtime configuration.

Use one of these supported paths instead:

- `llama_cpp` for in-process local GGUF inference.
- LM Studio or another OpenAI-compatible local server through the local backend
  probe and OpenAI-compatible server adapter surfaces.
- `vllm`, `nim`, or `sglang` for explicit GPU/server backends.
- `litellm` or named cloud providers when prompt egress is explicitly allowed.

If `vetinari doctor` reports an Ollama migration warning, clear
`VETINARI_OLLAMA_ENDPOINT`, `VETINARI_OLLAMA_BASE_URL`, and `OLLAMA_HOST`, then
remove any `inference_backend.ollama` block or `primary: ollama` value from the
runtime config.
