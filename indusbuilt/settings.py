"""
Persistent user settings for IndusBuilt.
"""
import json
import os
from pathlib import Path
from typing import Any, Dict


PROVIDERS = ["openai", "anthropic", "gemini"]

DEFAULT_MODELS = {
    "openai": "gpt-4o",
    "anthropic": "claude-3-5-sonnet-latest",
    "gemini": "gemini-2.0-flash",
}

MODEL_CHOICES = {
    "openai": ["gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini"],
    "anthropic": ["claude-3-5-sonnet-latest", "claude-3-7-sonnet-latest"],
    "gemini": ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro"],
}


def _config_path() -> Path:
    """Return the per-user config file path."""
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) if appdata else (Path.home() / "AppData" / "Roaming")
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg_config) if xdg_config else (Path.home() / ".config")

    return base / "IndusBuilt" / "config.json"


def _default_settings() -> Dict[str, Any]:
    return {
        "provider": "openai",
        "api_keys": {provider: "" for provider in PROVIDERS},
        "models": dict(DEFAULT_MODELS),
    }


def load_settings() -> Dict[str, Any]:
    """Load settings with migration fallback for legacy single-key config."""
    config_file = _config_path()
    if not config_file.exists():
        return _default_settings()

    defaults = _default_settings()

    try:
        data = json.loads(config_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return defaults

        # Legacy migration support
        legacy_key = data.get("openai_api_key", "")
        if isinstance(legacy_key, str) and legacy_key.strip():
            defaults["api_keys"]["openai"] = legacy_key.strip()

        provider = data.get("provider", defaults["provider"])
        if provider in PROVIDERS:
            defaults["provider"] = provider

        api_keys = data.get("api_keys", {})
        if isinstance(api_keys, dict):
            for provider_name in PROVIDERS:
                key = api_keys.get(provider_name, "")
                defaults["api_keys"][provider_name] = key.strip() if isinstance(key, str) else ""

        models = data.get("models", {})
        if isinstance(models, dict):
            for provider_name in PROVIDERS:
                model_name = models.get(provider_name, "")
                if isinstance(model_name, str) and model_name.strip():
                    defaults["models"][provider_name] = model_name.strip()

        return defaults
    except Exception:
        return defaults


def save_settings(settings: Dict[str, Any]) -> None:
    """Persist full settings dictionary."""
    config_file = _config_path()
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_file.write_text(json.dumps(settings, indent=2), encoding="utf-8")


def get_active_provider(settings: Dict[str, Any]) -> str:
    provider = settings.get("provider", "openai")
    return provider if provider in PROVIDERS else "openai"


def set_active_provider(settings: Dict[str, Any], provider: str) -> None:
    if provider in PROVIDERS:
        settings["provider"] = provider


def get_api_key(settings: Dict[str, Any], provider: str) -> str:
    keys = settings.get("api_keys", {})
    key = keys.get(provider, "") if isinstance(keys, dict) else ""
    return key.strip() if isinstance(key, str) else ""


def set_api_key(settings: Dict[str, Any], provider: str, api_key: str) -> None:
    if "api_keys" not in settings or not isinstance(settings["api_keys"], dict):
        settings["api_keys"] = {}
    settings["api_keys"][provider] = api_key.strip()


def clear_api_key(settings: Dict[str, Any], provider: str) -> None:
    if "api_keys" not in settings or not isinstance(settings["api_keys"], dict):
        settings["api_keys"] = {}
    settings["api_keys"][provider] = ""


def get_model(settings: Dict[str, Any], provider: str) -> str:
    models = settings.get("models", {})
    value = models.get(provider, "") if isinstance(models, dict) else ""
    if isinstance(value, str) and value.strip():
        return value.strip()
    return DEFAULT_MODELS.get(provider, "")


def set_model(settings: Dict[str, Any], provider: str, model: str) -> None:
    if "models" not in settings or not isinstance(settings["models"], dict):
        settings["models"] = {}
    settings["models"][provider] = model.strip()


def load_saved_api_key() -> str:
    """Compatibility helper for old callers expecting OpenAI key only."""
    settings = load_settings()
    return get_api_key(settings, "openai")


def save_api_key(api_key: str) -> None:
    """Compatibility helper that saves OpenAI key."""
    settings = load_settings()
    set_api_key(settings, "openai", api_key)
    save_settings(settings)


def clear_saved_api_key() -> bool:
    """Compatibility helper that clears OpenAI key."""
    settings = load_settings()
    had_key = bool(get_api_key(settings, "openai"))
    clear_api_key(settings, "openai")
    save_settings(settings)
    return had_key


def get_config_file_path() -> Path:
    """Expose config path for user-facing messages."""
    return _config_path()
