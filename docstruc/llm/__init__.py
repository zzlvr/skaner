"""Model backends. `get_backend()` picks one by name (default: DOCSTRUC_BACKEND)."""

from docstruc import config
from docstruc.llm.base import LLMBackend

BACKENDS = ("gemini", "claude")


def get_backend(name: str | None = None, model: str | None = None, api_key: str | None = None) -> LLMBackend:
    """`api_key` is the caller's own key (bring-your-own-key); without it the key comes from the environment."""
    name = (name or config.backend_name()).lower()
    if name == "gemini":
        from docstruc.llm.gemini import GeminiBackend

        return GeminiBackend(model=model, api_key=api_key)
    if name == "claude":
        from docstruc.llm.claude import ClaudeBackend

        return ClaudeBackend(model=model, api_key=api_key)
    raise ValueError(f"Unknown backend '{name}'. Available: {', '.join(BACKENDS)}")
