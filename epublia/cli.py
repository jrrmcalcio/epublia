"""Command line entry point:  python -m epublia <name or word> [options]"""
from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import unicodedata
from pathlib import Path

from . import __version__
from .config import ConfigError, load_config
from .epub import EpubError
from .rate_limiter import DailyLimitReached, RateLimiter

log = logging.getLogger("epublia")

ENV_TEMPLATE = """# epublia settings (full reference: https://github.com/jrrmcalcio/epublia#configuration)
# Free Gemini key: https://aistudio.google.com/apikey (use a project WITHOUT billing to stay free)
GEMINI_API_TOKEN=
TARGET_LANGUAGE=ES
GEMINI_MODEL=gemini-3.8-flash
GEMINI_RPM=10
GEMINI_THINKING_LEVEL=low

# Optional backup model for passages Gemini refuses (any OpenAI-compatible API)
# FALLBACK_BASE_URL=https://openrouter.ai/api/v1
# FALLBACK_API_KEY=
# FALLBACK_MODEL=
"""


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"[\s_\-.]+", " ", text.lower()).strip()


def find_books(query: str, input_dir: Path) -> list[Path]:
    """Exact path, exact file name, or every .epub in input_dir whose name contains all query words."""
    direct = Path(query)
    if direct.suffix.lower() == ".epub" and direct.is_file():
        return [direct.resolve()]
    candidates = sorted(p for p in input_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".epub")
    exact = [p for p in candidates if p.name.lower() in (query.lower(), query.lower() + ".epub")]
    if exact:
        return exact
    words = _norm(query).split()
    return [p for p in candidates if all(w in _norm(p.stem) for w in words)]


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # Library loggers can include request URLs/headers at debug level; keep them quiet.
    for noisy in ("httpx", "httpcore", "google_genai", "google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_check(cfg) -> int:
    from google import genai

    from .translator import GeminiTranslator

    client = genai.Client(api_key=cfg.api_key)
    try:
        models = [m.name.removeprefix("models/") for m in client.models.list()]
    except Exception as exc:  # noqa: BLE001
        log.error("Could not list models: %s", cfg.redact(str(exc)))
        return 2
    flash = sorted(m for m in models if "flash" in m)
    print("Flash models available to this key:")
    for m in flash:
        print("  ", m, "<- configured" if m == cfg.model else "")
    if cfg.model not in models:
        print(f"\nWARNING: GEMINI_MODEL={cfg.model} is not available for this key.")
        return 2
    tr = GeminiTranslator(cfg, RateLimiter(cfg.rpm))
    try:
        out = tr.translate_items([(1, "The <x1>quick</x1> brown fox jumps over the lazy dog.")])
    except Exception as exc:  # noqa: BLE001
        print("Test request failed:", cfg.redact(str(exc)))
        return 2
    print(f"\nTest translation ({cfg.target_name}): {out.get(1)}")
    if tr.fallback is not None:
        try:
            reply = tr.fallback.call(tr.build_prompt([(1, "The brown fox jumps over the lazy dog.")]), tr.system_prompt)
            print(f"Backup model {cfg.fallback_model}: {tr.parse_response(reply).get(1)}")
        except Exception as exc:  # noqa: BLE001
            print("Backup model test failed:", cfg.redact(str(exc)))
            return 2
    else:
        print("Backup model: not configured (FALLBACK_BASE_URL / FALLBACK_MODEL)")
    print("\nThe API cannot tell whether billing is enabled on the key's project. To stay free, check in")
    print("https://aistudio.google.com/ -> 'Usage & Billing' that the project shows 'Free tier' (no billing account).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="epublia",
        description="Translate EPUB books with Gemini, preserving structure, styles and images.",
    )
    ap.add_argument("query", nargs="*", help="EPUB path, full file name, or word(s) contained in the file name")
    ap.add_argument("--lang", help="override TARGET_LANGUAGE (e.g. ES, FR, pt-br)")
    ap.add_argument("--model", help="override GEMINI_MODEL")
    ap.add_argument("--extract-only", action="store_true", help="only extract per-chapter TXT files, no API calls")
    ap.add_argument("--clean-only", action="store_true",
                    help="only remove PDF artefacts and write <name>_CLEAN.epub (no API calls)")
    ap.add_argument("--no-clean", action="store_true", help="skip PDF-artefact cleanup (also CLEANUP=false)")
    ap.add_argument("--no-cache", action="store_true", help="ignore cached translations and translate again")
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would be translated, requests needed and name issues (no API calls)")
    ap.add_argument("--glossary-only", action="store_true",
                    help="build work/<book>/glossary.txt with the model and stop, to review it first")
    ap.add_argument("--fix-names", action="store_true",
                    help="re-translate cached segments that break the glossary")
    ap.add_argument("--bilingual", action="store_true",
                    help="write <name>_<LANG>_bilingual.epub: each paragraph in the original, then translated")
    ap.add_argument("--series", help="share the glossary with other books of this series ('none' to disable; "
                                     "default: SERIES or the book's series metadata)")
    ap.add_argument("--install-epubcheck", action="store_true",
                    help="download W3C EPUBCheck into tools/ (needs Java) to validate every output")
    ap.add_argument("--init", action="store_true",
                    help="create .env, books-input/ and books-outputs/ in the current folder (EPUBLIA_HOME)")
    ap.add_argument("--version", action="version", version=f"epublia {__version__}")
    ap.add_argument("--list", action="store_true", help="list matching books and exit")
    ap.add_argument("--check", action="store_true", help="verify API key/model with one tiny request")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    _setup_logging(args.verbose)

    if args.lang:
        os.environ["TARGET_LANGUAGE"] = args.lang
    if args.model:
        os.environ["GEMINI_MODEL"] = args.model
    if args.series:
        os.environ["SERIES"] = args.series
    if args.init:
        from .config import ROOT
        for d in ("books-input", "books-outputs"):
            (ROOT / d).mkdir(parents=True, exist_ok=True)
        env = ROOT / ".env"
        if env.exists():
            log.info("%s already exists; left untouched", env)
        else:
            env.write_text(ENV_TEMPLATE, encoding="utf-8")
            log.info("Created %s: put your Gemini key in GEMINI_API_TOKEN, then run: epublia --check", env)
        log.info("Copy your .epub files into %s", ROOT / "books-input")
        return 0
    if args.install_epubcheck:
        from . import epubcheck
        from .config import ROOT
        try:
            jar = epubcheck.install(ROOT)
        except Exception as exc:  # noqa: BLE001
            log.error("EPUBCheck download failed: %s", exc)
            return 1
        log.info("EPUBCheck installed: %s", jar)
        if not epubcheck.find_java():
            log.warning("Java was not found: install a Java 17+ runtime (e.g. https://adoptium.net) to use it")
        return 0
    offline = args.extract_only or args.clean_only or args.dry_run
    needs_key = args.check or not (offline or args.list)
    try:
        cfg = load_config(require_key=needs_key)
    except ConfigError as exc:
        log.error("%s", exc)
        return 2

    if args.check:
        return cmd_check(cfg)
    if not args.query:
        ap.print_usage()
        log.error("give a file name or a word to search in %s", cfg.input_dir)
        return 2

    query = " ".join(args.query)
    books = find_books(query, cfg.input_dir)
    if not books:
        log.error("no .epub in %s matches %r", cfg.input_dir, query)
        return 1
    log.info("%d book(s) match %r:", len(books), query)
    for b in books:
        log.info("  - %s", b.name)
    if args.list:
        return 0

    from .pipeline import BookTranslator
    from .translator import GeminiTranslator

    translator = None
    if not offline:
        limiter = RateLimiter(cfg.rpm, cfg.rpd, cfg.work_dir / ".quota.json")
        translator = GeminiTranslator(cfg, limiter)
        log.info("Model %s -> %s | throttle %d req/min%s", cfg.model, cfg.target_name, cfg.rpm,
                 f", {cfg.rpd} req/day" if cfg.rpd else "")
    clean = not args.no_clean and os.getenv("CLEANUP", "true").strip().lower() not in ("0", "false", "no", "off")
    bt = BookTranslator(cfg, translator, use_cache=not args.no_cache, clean=clean, fix_names=args.fix_names,
                        bilingual=args.bilingual)

    failures = 0
    for book in books:
        try:
            bt.translate(book, extract_only=args.extract_only, clean_only=args.clean_only,
                         dry_run=args.dry_run, glossary_only=args.glossary_only)
        except DailyLimitReached as exc:
            log.error("%s", cfg.redact(str(exc)))
            log.error("Progress is saved. Run the same command again after the quota resets to resume.")
            return 3
        except EpubError as exc:
            log.error("%s", exc)
            failures += 1
        except KeyboardInterrupt:
            log.error("Interrupted. Progress is saved; run the same command again to resume.")
            return 130
        except Exception as exc:  # noqa: BLE001
            log.error("%s failed: %s", book.name, cfg.redact(f"{type(exc).__name__}: {exc}"))
            if args.verbose:
                raise
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
