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
                 temperature: float = 0.3, redact=lambda s: s, timeout: float = 300.0):
        self.url = base_url.rstrip("/") + "/chat/completions"
        self.model = model
        self.limiter = limiter
        self.temperature = temperature
        self.redact = redact
        self._headers = {"Authorization": f"Bearer {api_key}", "X-Title": "epublia"}
        self._http = httpx.Client(timeout=timeout)
        self.requests_made = 0

    def call(self, prompt: str, system: str, attempts: int = 3) -> str:
        body = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        last = "no attempt"
        for attempt in range(1, attempts + 1):
            try:
                self.limiter.acquire()
            except DailyLimitReached as exc:
                raise FallbackError(str(exc)) from None
            self.requests_made += 1
            try:
                resp = self._http.post(self.url, json=body, headers=self._headers)
            except httpx.HTTPError as exc:
                last = self.redact(f"{type(exc).__name__}: {exc}")
                time.sleep(2 ** attempt)
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                wait = float(resp.headers.get("retry-after", "") or 10 * attempt)
                log.warning("fallback %s (attempt %d/%d), waiting %.0fs", last, attempt, attempts, wait)
                time.sleep(min(wait, 120))
                continue
            if resp.status_code >= 400:
                raise FallbackError(self.redact(f"HTTP {resp.status_code}: {resp.text[:300]}"))
            try:
                choice = resp.json()["choices"][0]
                text = choice["message"]["content"] or ""
            except (ValueError, KeyError, IndexError, TypeError):
                raise FallbackError(self.redact(f"unexpected response: {resp.text[:300]}")) from None
            if choice.get("finish_reason") in ("content_filter", "length"):
                raise FallbackError(f"finish_reason={choice.get('finish_reason')}")
            text = _THINK.sub("", text).strip()
            if not text:
                raise FallbackError("empty response")
            return text
        raise FallbackError(f"giving up after {attempts} attempts: {last}")
