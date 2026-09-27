"""Gemini translation client: prompt building, response parsing, retries and throttling."""
from __future__ import annotations

import logging
import random
import re
import time

from google import genai
from google.genai import errors, types

from .config import Config
from .rate_limiter import DailyLimitReached, RateLimiter

log = logging.getLogger("epublia")

MARKER = re.compile(r"^\s*\[\[(\d+)\]\]\s?", re.MULTILINE)
MAX_ATTEMPTS = 6


class TranslationError(Exception):
    pass


class BlockedError(TranslationError):
    """The model refused/blocked the content (safety, recitation...)."""


SYSTEM_PROMPT = """You are a professional literary translator. Translate book text into {language}.

Input format: one segment per line, each starting with a marker like [[12]].
Output format: exactly the same markers, same order, one line per segment, nothing else —
no preamble, no notes, no code fences, no blank commentary.

Rules:
- Translate faithfully and naturally, as a published {language} edition would read. Keep the author's
  tone, register, rhythm, humour and paragraph meaning. Do not summarise, censor, add or omit content.
- Keep every inline tag such as <x3>...</x3> or <x7/> exactly as written (same number, same form) and
  place it around the translated words that correspond to the original ones. Never invent new tags.
- Keep HTML entities (&amp; &lt; &gt;) as entities.
- Use the typographic conventions of {language} (e.g. for Spanish: dialogue em-dashes «—» instead of
  English quotation marks for dialogue, ¿? and ¡!, and correct capitalisation of titles).
- Keep proper names of people, places, ships, factions and invented terms consistent across the book;
  only translate them when an established {language} translation exists.
- If a segment is only a number, URL, ISBN, code or a name, return it unchanged.
- If the source text is in ALL CAPS as a stylistic chapter opening, keep the same style.
{glossary}"""


class GeminiTranslator:
    def __init__(self, cfg: Config, limiter: RateLimiter):
        self.cfg = cfg
        self.limiter = limiter
        self.client = genai.Client(
            api_key=cfg.api_key,
            http_options=types.HttpOptions(
                timeout=300_000,
                retry_options=types.HttpRetryOptions(attempts=1),  # we retry ourselves (quota-aware)
            ),
        )
        glossary = ""
        if cfg.glossary:
            glossary = "\nGlossary (always use these renderings):\n" + cfg.glossary + "\n"
        self.system_prompt = SYSTEM_PROMPT.format(language=cfg.target_name, glossary=glossary)
        self.requests_made = 0

    # ------------------------------------------------------------------ API

    def _gen_config(self) -> types.GenerateContentConfig:
        kwargs = dict(
            system_instruction=self.system_prompt,
            temperature=self.cfg.temperature,
            max_output_tokens=65_536,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            safety_settings=[
                types.SafetySetting(category=c, threshold=types.HarmBlockThreshold.BLOCK_NONE)
                for c in (
                    types.HarmCategory.HARM_CATEGORY_HARASSMENT,
                    types.HarmCategory.HARM_CATEGORY_HATE_SPEECH,
                    types.HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT,
                    types.HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT,
                )
            ],
        )
        if self.cfg.thinking_level:
            kwargs["thinking_config"] = types.ThinkingConfig(thinking_level=self.cfg.thinking_level)
        return types.GenerateContentConfig(**kwargs)

    @staticmethod
    def _retry_delay(err: errors.APIError) -> float | None:
        text = str(getattr(err, "details", "") or "") + " " + str(getattr(err, "message", "") or "")
        m = re.search(r"retryDelay['\"]?\s*[:=]\s*['\"]?(\d+(?:\.\d+)?)s", text) or \
            re.search(r"retry in (\d+(?:\.\d+)?)\s*s", text, re.I)
        return float(m.group(1)) if m else None

    @staticmethod
    def _is_daily_quota(err: errors.APIError) -> bool:
        text = (str(getattr(err, "details", "") or "") + str(getattr(err, "message", "") or "")).lower()
        return "perday" in text or "per_day" in text or "requests per day" in text

    def call(self, prompt: str) -> str:
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.limiter.acquire()
            self.requests_made += 1
            try:
                resp = self.client.models.generate_content(
                    model=self.cfg.model, contents=prompt, config=self._gen_config()
                )
            except errors.APIError as err:
                msg = self.cfg.redact(f"{err.code} {err.status}: {err.message}")
                if err.code == 429:
                    if self._is_daily_quota(err):
                        raise DailyLimitReached("Gemini daily quota exhausted: " + msg) from None
                    wait = (self._retry_delay(err) or 30.0) + 2
                    log.warning("429 rate limited (attempt %d/%d), waiting %.0fs", attempt, MAX_ATTEMPTS, wait)
                    self.limiter.penalize(wait)
                    last = TranslationError(msg)
                    continue
                if err.code in (400, 401, 403, 404):
                    raise TranslationError(msg) from None
                last = TranslationError(msg)
            except Exception as exc:  # network errors, timeouts
                last = TranslationError(self.cfg.redact(f"{type(exc).__name__}: {exc}"))
            else:
                return self._extract_text(resp)
            wait = min(120, 2 ** attempt + random.random() * 2)
            log.warning("Gemini error (attempt %d/%d): %s — retrying in %.0fs", attempt, MAX_ATTEMPTS, last, wait)
            time.sleep(wait)
        raise TranslationError(f"giving up after {MAX_ATTEMPTS} attempts: {last}")

    @staticmethod
    def _extract_text(resp) -> str:
        fb = getattr(resp, "prompt_feedback", None)
        if fb is not None and getattr(fb, "block_reason", None):
            raise BlockedError(f"prompt blocked: {fb.block_reason}")
        cands = resp.candidates or []
        if not cands:
            raise BlockedError("no candidates returned")
        reason = str(cands[0].finish_reason or "")
        text = resp.text or ""
        if "MAX_TOKENS" in reason:
            raise BlockedError("output truncated (MAX_TOKENS)")
        if not text.strip():
            raise BlockedError(f"empty response (finish_reason={reason})")
        if reason and not reason.endswith("STOP"):
            raise BlockedError(f"finish_reason={reason}")
        return text

    # ------------------------------------------------------------------ segments

    @staticmethod
    def build_prompt(items: list[tuple[int, str]]) -> str:
        return "\n".join(f"[[{i}]] {t}" for i, t in items)

    @staticmethod
    def parse_response(text: str) -> dict[int, str]:
        text = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text.strip())
        out: dict[int, str] = {}
        matches = list(MARKER.finditer(text))
        for n, m in enumerate(matches):
            end = matches[n + 1].start() if n + 1 < len(matches) else len(text)
            out[int(m.group(1))] = " ".join(text[m.end():end].split())
        return out

    def translate_items(self, items: list[tuple[int, str]]) -> dict[int, str]:
        """Translate [(id, text)] and return {id: translation}. Items that the model keeps
        blocking are returned untranslated (and reported by the caller as untranslated)."""
        if not items:
            return {}
        try:
            result = self.parse_response(self.call(self.build_prompt(items)))
        except BlockedError as exc:
            if len(items) == 1:
                log.warning("segment %d could not be translated (%s); keeping original", items[0][0], exc)
                return {items[0][0]: items[0][1]}
            log.warning("request blocked/truncated (%s); splitting %d segments in half", exc, len(items))
            mid = len(items) // 2
            return {**self.translate_items(items[:mid]), **self.translate_items(items[mid:])}

        wanted = {i for i, _ in items}
        missing = [(i, t) for i, t in items if not result.get(i)]
        extra = set(result) - wanted
        for k in extra:
            result.pop(k, None)
        if missing:
            if len(missing) == len(items) and len(items) == 1:
                log.warning("segment %d missing from response; keeping original", items[0][0])
                return {items[0][0]: items[0][1]}
            log.info("%d segment(s) missing from response, re-requesting them", len(missing))
            result.update(self.translate_items(missing) if len(missing) < len(items) else
                          {**self.translate_items(missing[: len(missing) // 2]),
                           **self.translate_items(missing[len(missing) // 2:])})
        return result
