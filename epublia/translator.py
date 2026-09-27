"""Gemini translation client: prompt building, response parsing, retries and throttling."""
from __future__ import annotations

import logging
import random
import re
import time

from google import genai
from google.genai import errors, types

from .config import Config
from .fallback import FallbackError, OpenAICompatClient
from .glossary import Glossary
from .rate_limiter import DailyLimitReached, RateLimiter

log = logging.getLogger("epublia")

MARKER = re.compile(r"^\s*\[\[(\d+)\]\]\s?", re.MULTILINE)
MAX_ATTEMPTS = 6
_TAG = re.compile(r"<(/?)x\d+(/?)>")
_SENTENCE_END = re.compile(r"[.!?…][\"”’»)]*\s+")
MIN_SPLIT = 800  # a blocked/missing segment shorter than this is kept as is


def split_text(text: str, limit: int) -> list[str]:
    """Split placeholder text into pieces of about ``limit`` chars at sentence ends that are not
    inside an inline tag, so every piece keeps balanced <xN>…</xN> pairs. Rejoin with a space."""
    if len(text) <= limit:
        return [text]
    depth, tags, cuts = 0, list(_TAG.finditer(text)), []
    t = 0
    for m in _SENTENCE_END.finditer(text):
        while t < len(tags) and tags[t].start() < m.end():
            closing, void = tags[t].group(1) == "/", tags[t].group(2) == "/"
            depth += 0 if void else (-1 if closing else 1)
            t += 1
        if depth == 0 and m.end() < len(text):
            cuts.append(m.end())
    pieces, start = [], 0
    while len(text) - start > limit:
        fitting = [c for c in cuts if start < c <= start + limit]
        later = [c for c in cuts if c > start]
        cut = fitting[-1] if fitting else (later[0] if later else None)
        if cut is None:
            break
        pieces.append(text[start:cut].strip())
        start = cut
    pieces.append(text[start:].strip())
    return [p for p in pieces if p]


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
- The message may start with a GLOSSARY: always use those renderings (with the same capitalisation;
  where alternatives are given with "|", pick the one that fits gender/number). It may also include a
  PREVIOUS PASSAGE, already translated, only so you keep names, tone and forms of address
  consistent; never output it. Only output the marked segments after "SEGMENTS:"."""

TITLE_PROMPT = """Give the title of this book as its published {language} edition shows it. If there is an
established {language} title, use it; otherwise translate it naturally, keeping proper names and any
series prefix in the form a {language} edition would use. Reply with the title only, on one line."""

GLOSSARY_PROMPT = """You prepare the terminology sheet for a {language} translation of a book.
You receive candidate terms extracted automatically, one per line: "term (count): example context".

Return ONLY lines of the form
  term = rendering
for every term that is a proper name, place, ship, organisation, species, tribe, title, rank or
invented concept whose rendering must stay identical across the book. Skip ordinary words,
exclamations and oaths (God, Hell, Lord...), forms of address that depend on context (Sir, Master,
Your Excellency), sentence fragments and publishing boilerplate (publisher, ISBN, addresses).

Rules for the rendering:
- Keep personal names unchanged. Translate descriptive names, titles and invented concepts only when
  a {language} edition would (use the established official translation of the franchise if one exists).
- Give the capitalisation to use mid-sentence, following {language} conventions (e.g. Spanish writes
  demonyms and species in lowercase unless they are used as proper names).
- If gender or number changes the form, list the alternatives separated by " | ",
  e.g. "Preserver = Preservador | Preservadora".
- One line per term, no numbering, no commentary, no code fences."""


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
        self.system_prompt = SYSTEM_PROMPT.format(language=cfg.target_name)
        self.requests_made = 0
        self.fallback: OpenAICompatClient | None = None
        self.fallback_used: list[str] = []  # source snippets the backup model translated
        if cfg.fallback_enabled:
            self.fallback = OpenAICompatClient(
                cfg.fallback_base_url, cfg.fallback_api_key, cfg.fallback_model,
                RateLimiter(cfg.fallback_rpm, cfg.fallback_rpd, cfg.work_dir / ".quota-fallback.json"),
                temperature=cfg.temperature, redact=cfg.redact)

    # ------------------------------------------------------------------ API

    def _gen_config(self, system: str | None = None) -> types.GenerateContentConfig:
        kwargs = dict(
            system_instruction=system or self.system_prompt,
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

    def call(self, prompt: str, system: str | None = None) -> str:
        last: Exception | None = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.limiter.acquire()
            self.requests_made += 1
            try:
                resp = self.client.models.generate_content(
                    model=self.cfg.model, contents=prompt, config=self._gen_config(system)
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
    def build_prompt(items: list[tuple[int, str]], glossary: str = "",
                     context: list[tuple[str, str]] | None = None) -> str:
        segments = "\n".join(f"[[{i}]] {t}" for i, t in items)
        if not glossary and not context:
            return segments
        parts = []
        if glossary:
            parts.append("GLOSSARY:\n" + glossary)
        if context:
            parts.append("PREVIOUS PASSAGE (context only, do not output):\n"
                         + "\n".join(f"SOURCE: {s}\nTRANSLATION: {t}" for s, t in context))
        parts.append("SEGMENTS:\n" + segments)
        return "\n\n".join(parts)

    @staticmethod
    def parse_response(text: str) -> dict[int, str]:
        text = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text.strip())
        out: dict[int, str] = {}
        matches = list(MARKER.finditer(text))
        for n, m in enumerate(matches):
            end = matches[n + 1].start() if n + 1 < len(matches) else len(text)
            out[int(m.group(1))] = " ".join(text[m.end():end].split())
        return out

    def translate_items(self, items: list[tuple[int, str]], glossary: Glossary | None = None,
                        context: list[tuple[str, str]] | None = None) -> dict[int, str]:
        """Translate [(id, text)] and return {id: translation}. Items that the model keeps
        blocking are returned untranslated (and reported by the caller as untranslated).
        Only the glossary entries that occur in ``items`` are sent; ``context`` is the preceding
        (source, translation) text, sent for continuity."""
        if not items:
            return {}
        block = glossary.prompt_block("\n".join(t for _, t in items)) if glossary else ""
        try:
            result = self.parse_response(self.call(self.build_prompt(items, block, context)))
        except BlockedError as exc:
            if len(items) == 1:
                return self._split_single(items[0], glossary, context, str(exc))
            log.warning("request blocked/truncated (%s); splitting %d segments in half", exc, len(items))
            mid = len(items) // 2
            return {**self.translate_items(items[:mid], glossary, context),
                    **self.translate_items(items[mid:], glossary, context)}

        wanted = {i for i, _ in items}
        missing = [(i, t) for i, t in items if not result.get(i)]
        extra = set(result) - wanted
        for k in extra:
            result.pop(k, None)
        if missing:
            if len(missing) == len(items) and len(items) == 1:
                return self._split_single(items[0], glossary, context, "missing from response")
            log.info("%d segment(s) missing from response, re-requesting them", len(missing))
            half = len(missing) // 2
            result.update(self.translate_items(missing, glossary, context) if len(missing) < len(items) else
                          {**self.translate_items(missing[:half], glossary, context),
                           **self.translate_items(missing[half:], glossary, context)})
        return result

    def _split_single(self, item: tuple[int, str], glossary: Glossary | None,
                      context: list[tuple[str, str]] | None, reason: str) -> dict[int, str]:
        """A lone segment failed: translate it in halves (a filter often trips on one passage only);
        whatever still fails is kept in the original language and reported by the caller."""
        i, text = item
        parts = split_text(text, len(text) // 2 + 1) if len(text) >= MIN_SPLIT else [text]
        if len(parts) < 2:
            return self._fallback_single(item, glossary, context, reason)
        log.warning("segment %d failed (%s); retrying it in %d parts", i, reason, len(parts))
        got = self.translate_items(list(enumerate(parts, 1)), glossary, context)
        return {i: " ".join(got.get(n, p) for n, p in enumerate(parts, 1))}

    def _fallback_single(self, item: tuple[int, str], glossary: Glossary | None,
                         context: list[tuple[str, str]] | None, reason: str) -> dict[int, str]:
        """Last resort for a passage Gemini will not translate: the backup provider, if configured."""
        i, text = item
        if self.fallback is not None:
            block = glossary.prompt_block(text) if glossary else ""
            try:
                reply = self.fallback.call(self.build_prompt([item], block, context), self.system_prompt)
                got = self.parse_response(reply).get(i)
            except FallbackError as exc:
                log.warning("segment %d: backup translator failed too (%s)", i, exc)
                got = None
            if got:
                self.fallback_used.append(" ".join(_TAG.sub("", text).split())[:70])
                log.info("segment %d translated by the backup model (%s)", i, reason)
                return {i: got}
        log.warning("segment %d could not be translated (%s); keeping original", i, reason)
        return {i: text}

    def translate_title(self, title: str, author: str = "") -> str:
        """Book title as a published edition in the target language would print it."""
        system = TITLE_PROMPT.format(language=self.cfg.target_name)
        try:
            reply = self.call(f"Title: {title}\nAuthor: {author}", system)
        except TranslationError as exc:
            log.warning("title not translated: %s", exc)
            return title
        line = next((ln.strip() for ln in reply.splitlines() if ln.strip()), "")
        return line.strip('"“”«»*').removeprefix("Title:").strip() or title

    # ------------------------------------------------------------------ glossary

    def generate_glossary(self, candidates: list[tuple[str, int, str]], batch: int = 150) -> str:
        """Ask the model which candidates must stay consistent and how to render them.
        A batch that fails is skipped (logged) instead of aborting the book."""
        system = GLOSSARY_PROMPT.format(language=self.cfg.target_name)
        lines: list[str] = []
        for start in range(0, len(candidates), batch):
            part = candidates[start:start + batch]
            try:
                reply = self.call("\n".join(f"{t} ({n}): {ctx}" for t, n, ctx in part), system)
            except TranslationError as exc:
                # The example contexts can trip a content filter; the bare terms rarely do.
                log.warning("glossary request failed (%s); retrying without context", exc)
                try:
                    reply = self.call("\n".join(f"{t} ({n})" for t, n, _ in part), system)
                except TranslationError as exc2:
                    log.warning("glossary batch skipped: %s", exc2)
                    continue
            text = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", reply.strip())
            lines += [ln.strip() for ln in text.splitlines() if "=" in ln and not ln.lstrip().startswith("#")]
        return "\n".join(lines)
