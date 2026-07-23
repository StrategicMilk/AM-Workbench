"""Static dependency specifications for startup preflight checks."""

from __future__ import annotations

from vetinari.preflight_models import DependencyReadinessSpec

_DEPENDENCY_GROUPS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "Local Inference (GGUF)": (
        "local",
        ("llama_cpp",),
        "Run GGUF models locally via llama-cpp-python",
    ),
    "Cloud LLM Providers": (
        "cloud",
        ("anthropic", "openai", "google.genai"),
        "Route inference to OpenAI, Anthropic, and Google via native provider SDKs",
    ),
    "ML Utilities": (
        "ml",
        ("numpy", "onnxruntime", "joblib", "gguf"),
        "Vector search, ONNX runtime, GGUF metadata (embeddings are AM Engine-owned)",
    ),
    "Training & Fine-tuning": (
        "training",
        ("datasets", "peft", "trl", "transformers", "bitsandbytes", "tiktoken"),
        "QLoRA fine-tuning, dataset loading, tokenization, quantized training backends",
    ),
    "Image Generation": (
        "image",
        ("diffusers", "accelerate", "PIL"),
        "Stable Diffusion / FLUX image generation",
    ),
    "Web Search": (
        "search",
        ("ddgs", "brave_search"),
        "DuckDuckGo and Brave web search backends",
    ),
    "Guardrails": (
        "guardrails",
        ("nemoguardrails", "llm_guard"),
        "NeMo Guardrails and LLM Guard content safety",
    ),
    "Observability": (
        "observability",
        ("opentelemetry",),
        "OpenTelemetry distributed tracing",
    ),
    "Notifications": (
        "notifications",
        ("desktop_notifier", "pystray"),
        "Desktop notifications and system tray",
    ),
    "File Watcher": (
        "watcher",
        ("watchfiles",),
        "File system change monitoring",
    ),
    "Encryption": (
        "crypto",
        ("cryptography",),
        "Credential encryption via AES-256-GCM",
    ),
}

# Groups recommended for NVIDIA GPU systems
_GPU_RECOMMENDED_GROUPS = {"Local Inference (GGUF)", "ML Utilities", "Cloud LLM Providers", "Training & Fine-tuning"}
# Groups recommended for all systems
_BASE_RECOMMENDED_GROUPS = {"Local Inference (GGUF)", "Cloud LLM Providers", "ML Utilities", "Encryption"}

_DEPENDENCY_READINESS_SPECS: tuple[DependencyReadinessSpec, ...] = (
    DependencyReadinessSpec(
        package="pydantic",
        import_names=("pydantic",),
        channel="core",
        expected="required",
        description="Core settings and validation runtime",
        install_command="pip install vetinari",
        distribution="pydantic",
    ),
    DependencyReadinessSpec(
        package="litestar",
        import_names=("litestar",),
        channel="core",
        expected="required",
        description="Primary ASGI application framework",
        install_command="pip install vetinari",
        distribution="litestar",
    ),
    DependencyReadinessSpec(
        package="httpx",
        import_names=("httpx",),
        channel="core",
        expected="required",
        description="HTTP client used by backend setup and health probes",
        install_command="pip install vetinari",
        distribution="httpx",
    ),
    DependencyReadinessSpec(
        package="defusedxml",
        import_names=("defusedxml",),
        channel="core",
        expected="required",
        description="Safer XML parsing for runtime integrations",
        install_command="pip install vetinari",
        distribution="defusedxml",
    ),
    DependencyReadinessSpec(
        package="llama_cpp",
        import_names=("llama_cpp",),
        channel="extra",
        expected="recommended",
        description="Local GGUF inference fallback",
        install_command='pip install "vetinari[local]"',
        distribution="llama-cpp-python",
        gpu_expected="recommended",
    ),
    DependencyReadinessSpec(
        package="torch",
        import_names=("torch",),
        channel="extra",
        expected="optional",
        description="Training, image, and CUDA-backed ML runtime",
        install_command='pip install "vetinari[image,training]"',
        distribution="torch",
        gpu_expected="recommended",
    ),
    DependencyReadinessSpec(
        package="bitsandbytes",
        import_names=("bitsandbytes",),
        channel="extra",
        expected="optional",
        description="Quantized training backend for QLoRA flows",
        install_command='pip install "vetinari[training]"',
        distribution="bitsandbytes",
        gpu_expected="recommended",
    ),
    DependencyReadinessSpec(
        package="pynvml",
        import_names=("pynvml",),
        channel="extra",
        expected="optional",
        description="NVIDIA telemetry used for GPU diagnostics",
        install_command='pip install "vetinari[ml]"',
        distribution="nvidia-ml-py",
        gpu_expected="recommended",
    ),
    DependencyReadinessSpec(
        package="vllm",
        import_names=("vllm",),
        channel="extra",
        expected="optional",
        description="Native vLLM server package when running in-process on Linux/WSL",
        install_command='pip install "vetinari[vllm]"',
        distribution="vllm",
    ),
    DependencyReadinessSpec(
        package="duckduckgo_search",
        import_names=("ddgs", "duckduckgo_search"),
        channel="extra",
        expected="optional",
        description="DuckDuckGo search backend (current package: ddgs)",
        install_command='pip install "vetinari[search]"',
        distribution="ddgs",
    ),
    DependencyReadinessSpec(
        package="pytest_cov",
        import_names=("pytest_cov",),
        channel="dev",
        expected="dev",
        description="Coverage reporting for test runs",
        install_command='pip install "vetinari[dev]"',
        distribution="pytest-cov",
    ),
    DependencyReadinessSpec(
        package="pytest_asyncio",
        import_names=("pytest_asyncio",),
        channel="dev",
        expected="dev",
        description="Async test support",
        install_command='pip install "vetinari[dev]"',
        distribution="pytest-asyncio",
    ),
    DependencyReadinessSpec(
        package="pytest_xdist",
        import_names=("xdist",),
        channel="dev",
        expected="dev",
        description="Parallel test execution",
        install_command='pip install "vetinari[dev]"',
        distribution="pytest-xdist",
    ),
    DependencyReadinessSpec(
        package="schemathesis",
        import_names=("schemathesis",),
        channel="dev",
        expected="dev",
        description="API contract and property-based testing",
        install_command='pip install "vetinari[dev]"',
        distribution="schemathesis",
    ),
)


# ── Hardware detection ───────────────────────────────────────────────────────
