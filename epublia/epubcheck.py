"""Optional W3C EPUBCheck run: reports only the problems the translation added to the original."""
from __future__ import annotations

import glob
import io
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

log = logging.getLogger("epublia")

RELEASES = "https://api.github.com/repos/w3c/epubcheck/releases/latest"


def find_java() -> str | None:
    found = shutil.which("java")
    if found:
        return found
    candidates = []
    if os.getenv("JAVA_HOME"):
        candidates.append(os.path.join(os.environ["JAVA_HOME"], "bin", "java.exe" if os.name == "nt" else "java"))
    for base in (os.getenv("ProgramFiles", r"C:\Program Files"), os.getenv("ProgramFiles(x86)", "")):
        if base:
            for vendor in ("Eclipse Adoptium", "Java", "Microsoft", "Zulu", "Amazon Corretto"):
                candidates += sorted(glob.glob(os.path.join(base, vendor, "*", "bin", "java.exe")), reverse=True)
    return next((c for c in candidates if os.path.isfile(c)), None)


def find_command(setting: str, home: Path) -> list[str] | None:
    """Command to run EPUBCheck, or None. setting: EPUBCHECK_JAR ('' = auto-detect, 'off' = disabled)."""
    if setting.lower() in ("off", "false", "no", "0"):
        return None
    jar = setting or next(iter(sorted(glob.glob(str(home / "tools" / "epubcheck*" / "epubcheck.jar")), reverse=True)), "")
    if jar:
        java = find_java()
        if java and os.path.isfile(jar):
            return [java, "-jar", jar]
        if not java:
            log.info("EPUBCheck found but Java is not installed; skipping it")
        return None
    wrapper = shutil.which("epubcheck")  # e.g. Homebrew / Linux packages
    return [wrapper] if wrapper else None


def run(cmd: list[str], epub: Path) -> list[dict] | None:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "report.json"
        try:
            subprocess.run(cmd + [str(epub), "--json", str(out), "--locale", "en", "-q"],
                           capture_output=True, timeout=600, check=False)
            data = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            log.warning("EPUBCheck could not run: %s", exc)
            return None
    msgs = []
    for m in data.get("messages", []):
        locs = m.get("locations") or [{}]
        msgs.append({"severity": m.get("severity", ""), "id": m.get("ID", ""),
                     "path": locs[0].get("path", ""), "line": locs[0].get("line"),
                     "message": m.get("message", "")})
    return msgs


def new_problems(cmd: list[str], original: Path, output: Path) -> dict | None:
    """Messages present in ``output`` but not in ``original``. Compared by ID and text only: EPUBCheck
    reports each distinct message once per book, so a problem the source already had can show up
    under another file once the translation (or the cleanup) changes which occurrence comes first."""
    after = run(cmd, output)
    if after is None:
        return None
    before = run(cmd, original) or []

    def key(m):
        # The "; expected ..." tail depends on the surrounding markup, not on the problem itself.
        return m["id"], re.sub(r";\s*expected\b.*$", "", m["message"], flags=re.S)

    known = {key(m) for m in before}
    added = [m for m in after if key(m) not in known]

    def fmt(m):
        return f"{m['severity']} {m['id']} {m['path']}:{m['line']} {m['message']}"

    return {
        "new_errors": [fmt(m) for m in added if m["severity"] in ("ERROR", "FATAL")],
        "new_warnings": [fmt(m) for m in added if m["severity"] == "WARNING"],
        "preexisting_messages": len(before),
    }


def install(home: Path) -> Path:
    """Download the latest EPUBCheck release into <home>/tools/. Returns the jar path."""
    import httpx

    release = httpx.get(RELEASES, timeout=60, follow_redirects=True).json()
    asset = next(a for a in release["assets"] if a["name"].startswith("epubcheck-") and a["name"].endswith(".zip"))
    data = httpx.get(asset["browser_download_url"], timeout=300, follow_redirects=True).content
    tools = home / "tools"
    tools.mkdir(parents=True, exist_ok=True)
    zipfile.ZipFile(io.BytesIO(data)).extractall(tools)
    jar = next(iter(sorted(tools.glob("epubcheck*/epubcheck.jar"), reverse=True)))
    return jar
