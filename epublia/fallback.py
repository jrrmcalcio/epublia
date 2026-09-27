"""Backup translator for passages Gemini refuses: any OpenAI-compatible chat-completions API
(OpenRouter, Groq, DeepSeek, Mistral, a local Ollama...). Never log or print the key."""
from __future__ import annotations

import logging
import re
import time

import httpx

from .rate_limiter import DailyLimitReached, RateLimiter

log = logging.getLogger("epublia")

_THINK = re.compile(r"<think>.*?</think>", re.S)  # reasoning models may prepend their thoughts


class FallbackError(Exception):
    pass


class OpenAICompatClient:
    def __init__(self, base_url: str, api_key: str, model: str, limiter: RateLimiter,
                 temperature: float = 0.3, redact=lambda s: s, timeout: float = 300.0, transport=None):
        """``model`` may be a comma-separated list, tried in order: a busy (429), failing or
        refusing model hands the request to the next one without waiting."""
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.models = [m.strip() for m in model.split(",") if m.strip()]
        self.limiter = limiter
        self.temperature = temperature
        self.redact = redact
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "epublia"}
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self.requests_made = 0
        self.last_model = ""

    def call(self, prompt: str, system: str, attempts: int = 3) -> str:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        last = "no attempt"
        for attempt in range(1, attempts + 1):
            wait = 0.0
            for model in self.models:
                try:
                    self.limiter.acquire()
                except DailyLimitReached as exc:
                    raise FallbackError(str(exc)) from None
                self.requests_made += 1
                try:
                    resp = self._http.post(self.url, headers=self._headers, json={
                        "model": model, "temperature": self.temperature, "messages": messages})
                except httpx.HTTPError as exc:
                    last = self.redact(f"{model}: {type(exc).__name__}: {exc}")
                    wait = max(wait, 2.0 ** attempt)
                    continue
                if resp.status_code == 429 or resp.status_code >= 500:
                    last = f"{model}: HTTP {resp.status_code}"
                    try:
                        wait = max(wait, float(resp.headers.get("retry-after", "") or 0))
                    except ValueError:
                        pass
                    continue
                if resp.status_code in (404, 410):  # model retired or misspelt: try the next one
                    last = self.redact(f"{model}: HTTP {resp.status_code}: {resp.text[:200]}")
                    continue
                if resp.status_code >= 400:
                    raise FallbackError(self.redact(f"{model}: HTTP {resp.status_code}: {resp.text[:300]}"))
                try:
                    choice = resp.json()["choices"][0]
                    text = _THINK.sub("", choice["message"]["content"] or "").strip()
                except (ValueError, KeyError, IndexError, TypeError):
                    last = self.redact(f"{model}: unexpected response: {resp.text[:200]}")
                    continue
                if choice.get("finish_reason") in ("content_filter", "length") or not text:
                    last = f"{model}: finish_reason={choice.get('finish_reason')}, {len(text)} chars"
                    continue
                self.last_model = model
                return text
            if attempt < attempts:
                wait = min(max(wait, 10.0 * attempt), 120.0)
                log.warning("backup model(s) unavailable (%s), attempt %d/%d, waiting %.0fs",
                            last, attempt, attempts, wait)
                time.sleep(wait)
        raise FallbackError(f"giving up after {attempts} attempts: {last}")
