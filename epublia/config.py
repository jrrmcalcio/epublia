"""Configuration loaded from .env (never log or print the API key)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

from .languages import resolve_language



def _home() -> Path:
    """Folder holding .env, books-input/, books-outputs/ and work/: EPUBLIA_HOME if set, the source
    checkout when running from one (python -m epublia / pip install -e), else the current folder."""
    env = os.getenv("EPUBLIA_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    checkout = Path(__file__).resolve().parent.parent
    if (checkout / "pyproject.toml").exists() and (checkout / "epublia").is_dir():
        return checkout
    return Path.cwd()


ROOT = _home()


class ConfigError(Exception):
    pass


def _int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _path(name: str, default: str) -> Path:
    p = Path(os.getenv(name, "").strip() or default)
    return p if p.is_absolute() else ROOT / p


@dataclass
class Config:
    api_key: str = field(repr=False)
    model: str
    target_code: str
    target_name: str
    rpm: int
    rpd: int
    max_chars: int
    temperature: float
    thinking_level: str
    input_dir: Path
    output_dir: Path
    work_dir: Path
    glossary: str
    context_chars: int = 1500
    auto_glossary: bool = True
    segment_chars: int = 6000
    fallback_base_url: str = ""
    fallback_api_key: str = field(default="", repr=False)
    fallback_model: str = ""
    fallback_rpm: int = 10
    fallback_rpd: int = 0
    series: str = ""
    epubcheck: str = ""  # path to epubcheck.jar ("" = auto-detect, "off" = disabled)
    repair: bool = True  # fix validity errors the source EPUB already had

    @property
    def fallback_enabled(self) -> bool:
        return bool(self.fallback_base_url and self.fallback_model)

    def redact(self, text: str) -> str:
        """Remove the API keys from any string before it is shown or logged."""
        for key in (self.api_key, self.fallback_api_key):
            if key and key in text:
                text = text.replace(key, "***")
        return text


def load_config(require_key: bool = True) -> Config:
    load_dotenv(ROOT / ".env", override=False)

    api_key = (os.getenv("GEMINI_API_TOKEN") or os.getenv("GEMINI_API_KEY") or "").strip().strip('"').strip("'")
    if require_key and not api_key:
        raise ConfigError("GEMINI_API_TOKEN is not set. Copy .env.example to .env and fill it in.")

    target_code, target_name = resolve_language(os.getenv("TARGET_LANGUAGE", "ES"))

    thinking_level = os.getenv("GEMINI_THINKING_LEVEL", "").strip().lower()
    if thinking_level and thinking_level not in {"minimal", "low", "medium", "high"}:
        raise ConfigError("GEMINI_THINKING_LEVEL must be minimal, low, medium or high")

    glossary = ""
    glossary_path = os.getenv("GLOSSARY_FILE", "").strip()
    if glossary_path:
        gp = Path(glossary_path)
        gp = gp if gp.is_absolute() else ROOT / gp
        if not gp.exists():
            raise ConfigError(f"GLOSSARY_FILE not found: {gp}")
        glossary = gp.read_text(encoding="utf-8").strip()

    return Config(
        api_key=api_key,
        model=os.getenv("GEMINI_MODEL", "gemini-3.8-flash").strip(),
        target_code=target_code,
        target_name=target_name,
        rpm=max(1, _int("GEMINI_RPM", 10)),
        rpd=max(0, _int("GEMINI_RPD", 0)),
        max_chars=max(2000, _int("MAX_CHARS_PER_REQUEST", 24000)),
        temperature=_float("GEMINI_TEMPERATURE", 0.3),
        thinking_level=thinking_level,
        input_dir=_path("INPUT_DIR", "books-input"),
        output_dir=_path("OUTPUT_DIR", "books-outputs"),
        work_dir=_path("WORK_DIR", "work"),
        glossary=glossary,
        context_chars=max(0, _int("CONTEXT_CHARS", 1500)),
        segment_chars=max(1500, _int("SEGMENT_SPLIT_CHARS", 6000)),
        auto_glossary=os.getenv("AUTO_GLOSSARY", "true").strip().lower() not in ("0", "false", "no", "off"),
        fallback_base_url=os.getenv("FALLBACK_BASE_URL", "").strip(),
        fallback_api_key=os.getenv("FALLBACK_API_KEY", "").strip().strip('"').strip("'"),
        fallback_model=os.getenv("FALLBACK_MODEL", "").strip(),
        fallback_rpm=max(1, _int("FALLBACK_RPM", 10)),
        fallback_rpd=max(0, _int("FALLBACK_RPD", 0)),
        series=os.getenv("SERIES", "").strip(),
        epubcheck=os.getenv("EPUBCHECK_JAR", "").strip(),
        repair=os.getenv("REPAIR", "true").strip().lower() not in ("0", "false", "no", "off"),
    )
