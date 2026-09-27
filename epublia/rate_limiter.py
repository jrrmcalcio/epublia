"""Client-side throttling so we stay inside the Gemini free-tier quotas."""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


class DailyLimitReached(Exception):
    pass


class RateLimiter:
    """Sliding-window limiter: at most `rpm` calls in any 60 s window, plus an optional
    requests-per-day counter persisted to disk (resets at midnight Pacific ≈ Google's reset;
    we approximate with UTC-8)."""

    def __init__(self, rpm: int, rpd: int = 0, state_file: Path | None = None):
        self.rpm = rpm
        self.rpd = rpd
        self.state_file = state_file
        self._calls: deque[float] = deque()
        self._lock = threading.Lock()
        self._day, self._day_count = self._load()

    @staticmethod
    def _today() -> str:
        return datetime.fromtimestamp(time.time() - 8 * 3600, tz=timezone.utc).strftime("%Y-%m-%d")

    def _load(self) -> tuple[str, int]:
        if self.state_file and self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                if data.get("day") == self._today():
                    return data["day"], int(data.get("count", 0))
            except (ValueError, KeyError):
                pass
        return self._today(), 0

    def _save(self) -> None:
        if self.state_file:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"day": self._day, "count": self._day_count}), encoding="utf-8")

    def acquire(self) -> None:
        with self._lock:
            if self._day != self._today():
                self._day, self._day_count = self._today(), 0
            if self.rpd and self._day_count >= self.rpd:
                raise DailyLimitReached(f"local daily limit of {self.rpd} requests reached (GEMINI_RPD)")
            while True:
                now = time.monotonic()
                while self._calls and now - self._calls[0] >= 60:
                    self._calls.popleft()
                if len(self._calls) < self.rpm:
                    break
                time.sleep(60 - (now - self._calls[0]) + 0.05)
            self._calls.append(time.monotonic())
            self._day_count += 1
            self._save()

    def penalize(self, seconds: float) -> None:
        """After a server 429, block the window for `seconds`."""
        time.sleep(max(0.0, seconds))
