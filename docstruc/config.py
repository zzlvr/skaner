"""Runtime settings, read from environment variables (and a local .env file)."""

import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))

DEFAULT_BACKEND = "gemini"
DEFAULT_MODELS = {
    "gemini": "gemini-3.8-flash",
    "claude": "claude-opus-5-5",
}


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set. Put it in the .env file (see .env.example).")
    return value


def gemini_api_key() -> str:
    return _required("GEMINI_API_KEY")


def anthropic_api_key() -> str:
    return _required("ANTHROPIC_API_KEY")


def backend_name() -> str:
    return os.environ.get("DOCSTRUC_BACKEND", DEFAULT_BACKEND).strip().lower() or DEFAULT_BACKEND


def model_name(backend: str) -> str:
    """DOCSTRUC_MODEL overrides the model of the active backend only."""
    override = os.environ.get("DOCSTRUC_MODEL", "").strip()
    if override and backend == backend_name():
        return override
    return DEFAULT_MODELS[backend]


def openai_base_url() -> str:
    """Any OpenAI-compatible endpoint; the default is a local Ollama."""
    return os.environ.get("DOCSTRUC_BASE_URL", "").strip() or "http://localhost:11434/v1"


def openai_api_key() -> str:
    """Optional: a local server needs none."""
    return os.environ.get("DOCSTRUC_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()


def openai_model() -> str:
    model = os.environ.get("DOCSTRUC_MODEL", "").strip()
    if not model:
        raise RuntimeError("Name the model: --model, or DOCSTRUC_MODEL in .env (for example a vision model in Ollama).")
    return model
