"""Map TARGET_LANGUAGE values (codes or names) to a BCP-47 code and an English name."""
from __future__ import annotations

LANGUAGES = {
    "es": "Spanish",
    "en": "English",
    "pt": "Portuguese",
    "pt-br": "Brazilian Portuguese",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ca": "Catalan",
    "gl": "Galician",
    "eu": "Basque",
    "nl": "Dutch",
    "pl": "Polish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "ar": "Arabic",
    "tr": "Turkish",
    "sv": "Swedish",
    "no": "Norwegian",
    "da": "Danish",
    "fi": "Finnish",
    "el": "Greek",
    "he": "Hebrew",
    "hi": "Hindi",
    "ro": "Romanian",
    "cs": "Czech",
    "hu": "Hungarian",
}

_NAME_ALIASES = {
    "spanish": "es", "español": "es", "espanol": "es", "castellano": "es",
    "english": "en", "inglés": "en", "ingles": "en",
    "portuguese": "pt", "portugués": "pt", "portugues": "pt",
    "french": "fr", "francés": "fr", "frances": "fr",
    "german": "de", "alemán": "de", "aleman": "de",
    "italian": "it", "italiano": "it",
}


def resolve_language(value: str) -> tuple[str, str]:
    raw = (value or "").strip()
    key = raw.lower().replace("_", "-")
    if key in LANGUAGES:
        return key, LANGUAGES[key]
    if key in _NAME_ALIASES:
        code = _NAME_ALIASES[key]
        return code, LANGUAGES[code]
    base = key.split("-")[0]
    if base in LANGUAGES:
        return key, LANGUAGES[base]
    # Unknown value: trust it as a free-form language name.
    return base[:8] or "und", raw
