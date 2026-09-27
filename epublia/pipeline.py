"""End-to-end book translation: extract -> TXT -> Gemini -> TXT -> rebuilt EPUB -> validation."""
from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from . import epubcheck
from .cleanup import Cleaner
from .config import ROOT, Config
from .epub import NCX_NS, EpubPackage, write_epub
from .glossary import Glossary, find_candidates, merge
from .segmenter import (
    _HAS_LETTERS,
    Segment,
    add_original,
    apply_translation,
    check_placeholders,
    extract_segments,
    parse_xhtml,
    plain_text,
    serialize_xhtml,
    set_document_language,
    unwrap_runs,
)
from .translator import GeminiTranslator, split_text

log = logging.getLogger("epublia")


def slugify(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "book"


def _key(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


@dataclass
class Stats:
    documents: int = 0
    segments: int = 0
    translated: int = 0
    unchanged: list[str] = field(default_factory=list)
    tag_warnings: list[str] = field(default_factory=list)
    recovered_docs: list[str] = field(default_factory=list)
    requests: int = 0
    name_issues: list[str] = field(default_factory=list)
    incomplete: list[str] = field(default_factory=list)
    fallback: list[str] = field(default_factory=list)
    renamed: int = 0  # cached segments re-translated because they broke the glossary
    pending: int = 0
    pending_chars: int = 0
    planned_requests: int = 0


class SegmentCache:
    """Per-document JSON cache {sha1(source): translation}; makes runs resumable."""

    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.data: dict[str, str] = {}
        if enabled and path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except ValueError:
                log.warning("corrupt cache %s ignored", path.name)

    def get(self, text: str) -> str | None:
        return self.data.get(_key(text))

    def put_many(self, pairs: list[tuple[str, str]]) -> None:
        for src, dst in pairs:
            self.data[_key(src)] = dst
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=0), encoding="utf-8")
        tmp.replace(self.path)


def _chunks(items: list[tuple[int, str]], max_chars: int) -> list[list[tuple[int, str]]]:
    out, cur, size = [], [], 0
    for item in items:
        n = len(item[1]) + 8
        if cur and size + n > max_chars:
            out.append(cur)
            cur, size = [], 0
        cur.append(item)
        size += n
    if cur:
        out.append(cur)
    return out


def _write_txt(path: Path, segments: list[tuple[int, str]], marked: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if marked:
        body = "\n".join(f"[[{i}]] {t}" for i, t in segments)
    else:
        body = "\n\n".join(p for p in (plain_text(t) for _, t in segments) if p)
    path.write_text(body + "\n", encoding="utf-8")


GLOSSARY_HEADER = """# Auto-generated glossary for this book: edit freely, it is never regenerated while it exists
# (delete it to build a new one). Format: "source = rendering", alternatives with " | ".
# Entries in GLOSSARY_FILE override these. After editing, run with --fix-names to re-translate
# only the segments that break a rule.
"""

SERIES_HEADER = """# Series glossary for "{series}": every book translated with this series adds or updates its terms here
# (the book's own glossary wins). New books of the series start from these renderings. Editable.
"""


def _tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else "…" + text[-limit:].split(" ", 1)[-1]


_WORD = re.compile(r"[^\W\d_]{3,}")


def _completeness_problem(source: str, translation: str) -> str | None:
    """Flag translations that look partial: much shorter than the source (a dropped passage) or
    still sharing many ordinary words with it (a sentence left in the source language)."""
    src, dst = plain_text(source), plain_text(translation)
    if len(src) < 300:
        return None
    ratio = len(dst) / len(src)
    if ratio < 0.6:
        return f"translation is {ratio:.0%} of the source length (passage dropped?)"
    src_words = {w for w in _WORD.findall(src) if w.islower()}
    dst_words = [w for w in _WORD.findall(dst) if w.islower()]
    if len(dst_words) >= 30 and src_words:
        shared = sum(w in src_words for w in dst_words) / len(dst_words)
        if shared > 0.3:
            return f"{shared:.0%} of the words are still in the source language (partly untranslated?)"
    return None


_LANG_FILES = ("cache", "translated", "translated-plain", "glossary.txt", "names.txt", "report.json")


def _migrate_legacy_layout(book_work: Path, lang: str) -> None:
    """Older versions kept translation files directly in work/<book>/; move them to work/<book>/<lang>/
    (the language comes from the old report, so a cache is never reused for another language)."""
    if not (book_work / "cache").is_dir():
        return
    old_lang = lang
    try:
        old_lang = json.loads((book_work / "report.json").read_text(encoding="utf-8"))["target_language"].lower()
    except (OSError, ValueError, KeyError, AttributeError):
        pass
    dest = book_work / old_lang
    if dest.exists():
        return
    dest.mkdir(parents=True)
    for name in _LANG_FILES:
        if (book_work / name).exists():
            (book_work / name).replace(dest / name)
    log.info("Moved previous %s translation files to %s", old_lang.upper(), dest)


class BookTranslator:
    def __init__(self, cfg: Config, translator: GeminiTranslator | None, *, use_cache: bool = True,
                 clean: bool = True, fix_names: bool = False, bilingual: bool = False):
        self.cfg = cfg
        self.clean = clean
        self.translator = translator
        self.use_cache = use_cache
        self.fix_names = fix_names
        self.bilingual = bilingual
        self.dry_run = False
        self.glossary = Glossary(cfg.glossary)
        self.series = ""
        self._carry: list[tuple[str, str]] = []  # last (source, translation) pairs of the previous document

    def output_path(self, epub_path: Path) -> Path:
        suffix = "_bilingual" if self.bilingual else ""
        return self.cfg.output_dir / f"{epub_path.stem}_{self.cfg.target_code.upper()}{suffix}.epub"

    def series_glossary_path(self) -> Path | None:
        if not self.series:
            return None
        return self.cfg.work_dir / "series" / slugify(self.series.lower()) / self.cfg.target_code.lower() / "glossary.txt"

    # ------------------------------------------------------------------ helpers

    def _context(self, segs: list[Segment], result: dict[int, str], first: int) -> list[tuple[str, str]] | None:
        """(source, translation) text right before segment ``first``, up to CONTEXT_CHARS of source."""
        limit = self.cfg.context_chars
        if not limit:
            return None
        before = [(plain_text(segs[i - 1].text), plain_text(result[i]))
                  for i in range(1, first) if segs[i - 1].translatable and i in result]
        out: list[tuple[str, str]] = []
        size = 0
        for src, dst in reversed(self._carry + before):
            if not src.strip():
                continue
            room = limit - size
            if room < 200:
                break
            if len(src) > room:
                # Keep roughly the same share of the translation as of the source.
                share = max(1, int(len(dst) * room / len(src)))
                out.insert(0, (_tail(src, room), _tail(dst, share)))
                break
            out.insert(0, (src, dst))
            size += len(src)
        return out or None

    def _split_units(self, items: list[tuple[int, str]]) -> tuple[list[tuple[int, str]], dict[int, tuple[int, int]]]:
        """Very long segments (e.g. a whole PDF chapter in one <p>) are sent as sentence-aligned parts,
        so a content-filter hit or a dropped marker costs one part, not the whole segment.
        Returns ([(unit_id, text)], {unit_id: (segment index, number of parts)})."""
        units: list[tuple[int, str]] = []
        owner: dict[int, tuple[int, int]] = {}
        for idx, text in items:
            parts = split_text(text, self.cfg.segment_chars)
            for part in parts:
                units.append((len(units) + 1, part))
                owner[len(units)] = (idx, len(parts))
        return units, owner

    def _translate_units(self, items: list[tuple[int, str]]) -> dict[int, str]:
        """Translate whole segments [(index, text)] in one go (used for re-requests)."""
        units, owner = self._split_units(items)
        got = self.translator.translate_items(units, self.glossary)
        parts: dict[int, list[str]] = {}
        for u, t in units:
            parts.setdefault(owner[u][0], []).append(got.get(u, t))
        return {idx: " ".join(p) for idx, p in parts.items()}

    def _translate_segments(self, segs: list[Segment], cache: SegmentCache, label: str, stats: Stats) -> dict[int, str]:
        """Return {segment_index: translated_text} for every translatable segment
        (in a dry run, only for segments that need no request)."""
        result: dict[int, str] = {}
        pending: list[tuple[int, str]] = []
        previous: dict[int, tuple[str, int]] = {}  # --fix-names: old translation and its rule breaks
        for idx, seg in enumerate(segs, 1):
            if not seg.translatable:
                result[idx] = seg.text
                continue
            hit = cache.get(seg.text)
            broken_rules = (self.fix_names and hit is not None
                            and self.glossary.violations(plain_text(seg.text), plain_text(hit)))
            if broken_rules:
                stats.renamed += 1
                previous[idx] = (hit, len(broken_rules))
                hit = None
            if hit is not None:
                result[idx] = hit
            else:
                pending.append((idx, seg.text))
        units, owner = self._split_units(pending)
        chunks = _chunks(units, self.cfg.max_chars)
        if self.dry_run:
            stats.pending += len(pending)
            stats.pending_chars += sum(len(t) for _, t in pending)
            stats.planned_requests += len(chunks)
            result.update({i: old for i, (old, _) in previous.items()})  # still reported in names.txt
            return result
        if pending and self.translator is None:
            raise RuntimeError("translator not configured")
        partial: dict[int, list[tuple[int, str]]] = {}
        for n, chunk in enumerate(chunks, 1):
            t0 = time.monotonic()
            first = owner[chunk[0][0]][0]
            got = self.translator.translate_items(chunk, self.glossary, self._context(segs, result, first))
            done: dict[int, str] = {}
            for u, t in chunk:
                idx, total = owner[u]
                partial.setdefault(idx, []).append((u, got.get(u, t)))
                if len(partial[idx]) == total:
                    done[idx] = " ".join(t for _, t in sorted(partial.pop(idx)))
            # Placeholder tags must survive; re-ask once, alone, for segments that broke them.
            broken = [idx for idx in done if check_placeholders(segs[idx - 1], done[idx])]
            if broken:
                log.info("  %d segment(s) with damaged inline tags, re-requesting", len(broken))
                retry = self._translate_units([(idx, segs[idx - 1].text) for idx in broken])
                for idx in broken:
                    if not check_placeholders(segs[idx - 1], retry.get(idx, "")):
                        done[idx] = retry[idx]
            # A re-translation for --fix-names must not make things worse (blocked, missing, more breaks).
            for idx, new in done.items():
                if idx in previous:
                    old, n_old = previous[idx]
                    src = segs[idx - 1].text
                    if (new.strip() == src.strip() or check_placeholders(segs[idx - 1], new)
                            or len(self.glossary.violations(plain_text(src), plain_text(new))) >= n_old):
                        done[idx] = old
            cache.put_many([(segs[idx - 1].text, t) for idx, t in done.items()])
            result.update(done)
            log.info("  %s: request %d/%d (%d segments) done in %.1fs",
                     label, n, len(chunks), len({owner[u][0] for u, _ in chunk}), time.monotonic() - t0)
        return result

    # ------------------------------------------------------------------ main

    def _load_glossary(self, work: Path, parsed: list, *, generate: bool) -> None:
        """Auto glossary in work/<book>/<lang>/glossary.txt (created once), overridden by GLOSSARY_FILE.
        The raw candidate list is language-independent and lives in work/<book>/."""
        path = work / "glossary.txt"
        series_path = self.series_glossary_path()
        if not path.exists() and self.cfg.auto_glossary:
            texts = [plain_text(s.text) for *_, segs in parsed for s in segs if s.translatable]
            candidates = find_candidates(texts)
            work.mkdir(parents=True, exist_ok=True)
            cand_path = work.parent / "glossary-candidates.txt"
            cand_path.write_text("".join(f"{n:5d}  {t}  |  {ctx}\n" for t, n, ctx in candidates), encoding="utf-8")
            # Terms already decided for the series are reused as they are, not rendered again.
            series = Glossary(series_path.read_text(encoding="utf-8")) if series_path and series_path.exists() else Glossary()
            inherited = series.relevant("\n".join(texts))
            known = {e.source for e in inherited}
            candidates = [c for c in candidates if c[0] not in known]
            if inherited:
                log.info("Glossary: %d terms inherited from series '%s'", len(inherited), self.series)
            if (candidates or inherited) and generate and self.translator is not None:
                text = ""
                if candidates:
                    log.info("Glossary: asking the model to render %d candidate terms...", len(candidates))
                    text = self.translator.generate_glossary(candidates)
                    if not text.strip():
                        log.warning("Glossary: the model returned no terms")
                series_block = ("# From the series glossary\n" + "\n".join(e.line for e in inherited) + "\n\n"
                                if inherited else "")
                path.write_text(GLOSSARY_HEADER + series_block + (text or "# (no new terms)") + "\n",
                                encoding="utf-8")
            elif candidates:
                log.info("Glossary: %d candidate terms found (%s); it will be generated with the model "
                         "on the first translating run", len(candidates), cand_path)
        auto = path.read_text(encoding="utf-8") if path.exists() else ""
        self.glossary = Glossary(auto, self.cfg.glossary)
        if self.glossary:
            log.info("Glossary: %d terms (%s%s)", len(self.glossary), path if auto else "",
                     " + GLOSSARY_FILE" if self.cfg.glossary else "")

    def _update_series_glossary(self, work: Path) -> None:
        """After a translation, fold this book's glossary into the series one (the book's choices win),
        so the next book of the series starts from the same names."""
        series_path = self.series_glossary_path()
        book = work / "glossary.txt"
        if not series_path or not book.exists():
            return
        base = series_path.read_text(encoding="utf-8") if series_path.exists() else ""
        text, added, changed = merge(base, book.read_text(encoding="utf-8"))
        if added or changed or not series_path.exists():
            series_path.parent.mkdir(parents=True, exist_ok=True)
            series_path.write_text(SERIES_HEADER.format(series=self.series) + text + "\n", encoding="utf-8")
            log.info("Series glossary '%s': %d new, %d changed -> %s", self.series, added, changed, series_path)

    def _translate_metadata(self, book: EpubPackage, work: Path) -> tuple[str | None, str | None]:
        """Translated (title, description) for the OPF, cached like everything else."""
        if self.translator is None and not self.use_cache:
            return None, None
        cache = SegmentCache(work / "cache" / "metadata.json", self.use_cache)
        title = None
        if book.title:
            key = "title\n" + book.title
            title = cache.get(key)
            if title is None and self.translator is not None:
                title = self.translator.translate_title(book.title, book.author)
                cache.put_many([(key, title)])
                log.info("Title: %s -> %s", book.title, title)
        desc = None
        # Descriptions holding (escaped) HTML are left alone: translating markup as text is risky.
        if book.description and "<" not in book.description and _HAS_LETTERS.search(book.description):
            desc = cache.get(book.description)
            if desc is None and self.translator is not None:
                got = self.translator.translate_items([(1, book.description)], self.glossary)
                desc = plain_text(got.get(1, book.description))
                cache.put_many([(book.description, desc)])
        return title, desc

    def translate(self, epub_path: Path, *, extract_only: bool = False, clean_only: bool = False,
                  dry_run: bool = False, glossary_only: bool = False) -> Path | None:
        book = EpubPackage(epub_path)
        # work/<book>/ holds language-independent files (source TXT, cleanup log); translation
        # artefacts (cache, glossary, output TXT, reports) go to work/<book>/<lang>/.
        book_work = self.cfg.work_dir / slugify(epub_path.stem)
        work = book_work / self.cfg.target_code.lower()
        _migrate_legacy_layout(book_work, self.cfg.target_code.lower())
        stats = Stats()
        self.dry_run = dry_run
        self._carry = []
        log.info("Book: %s — %s (lang=%s, EPUB %s)", book.title, book.author or "?", book.language or "?", book.version)
        log.info("Work dir: %s", work)
        self.series = "" if self.cfg.series.lower() == "none" else (self.cfg.series or book.series)
        if self.series:
            log.info("Series: %s", self.series)

        replacements: dict[str, bytes] = {}
        docs = book.content_documents()
        stats.documents = len(docs)

        # Phase 1: parse every document (the cleaner needs to see the whole book).
        parsed = []
        for n, item in enumerate(docs, 1):
            tree, recovered = parse_xhtml(book.read(item.path))
            if recovered:
                stats.recovered_docs.append(item.path)
                log.warning("%s was malformed XML; repaired by the parser", item.path)
            parsed.append((n, item, tree, extract_segments(tree)))

        # Phase 2: remove PDF-conversion artefacts before anything is sent to the model.
        if self.clean:
            cleaner = Cleaner(book.title, book.author)
            cleaner.learn([s for *_, segs in parsed for s in segs])
            parsed = [(n, item, tree, cleaner.clean_document(segs, item.path)) for n, item, tree, segs in parsed]
            book_work.mkdir(parents=True, exist_ok=True)
            (book_work / "cleanup.txt").write_text(
                cleaner.log.summary() + "\n\n" + "\n".join(cleaner.log.entries) + "\n", encoding="utf-8")
            log.info("Cleanup: %s", cleaner.log.summary())
            log.info("Cleanup details: %s", book_work / "cleanup.txt")

        # Phase 2b: glossary of names/terms that must stay consistent (needs the whole cleaned book).
        if not (extract_only or clean_only):
            self._load_glossary(work, parsed, generate=not dry_run)
            if glossary_only:
                log.info("Review/edit %s, then run the translation (add --fix-names to apply it to "
                         "an existing translation).", work / "glossary.txt")
                return None

        # Phase 3: per chapter TXT -> Gemini -> TXT -> XHTML.
        for n, item, tree, segs in parsed:
            name = posixpath.basename(item.path)
            label = f"[{n:03d}/{len(docs):03d}] {name}"
            stem = f"{n:03d}_{Path(name).stem}"
            numbered = [(i, s.text) for i, s in enumerate(segs, 1)]
            _write_txt(book_work / "source" / f"{stem}.txt", numbered, marked=True)
            _write_txt(book_work / "source-plain" / f"{stem}.txt", numbered, marked=False)
            stats.segments += len(segs)
            if extract_only:
                log.info("%s: %d segments extracted", label, len(segs))
                continue

            if clean_only or not segs:
                translations = {i: s.text for i, s in enumerate(segs, 1)}
            else:
                log.info("%s: %d segments", label, len(segs))
                cache = SegmentCache(work / "cache" / f"{stem}.json", self.use_cache)
                translations = self._translate_segments(segs, cache, label, stats)
                pairs = [(plain_text(s.text), plain_text(translations[i]))
                         for i, s in enumerate(segs, 1) if s.translatable and i in translations]
                self._carry = pairs[-3:] or self._carry
                for i, s in enumerate(segs, 1):
                    if s.translatable and i in translations:
                        for v in self.glossary.violations(plain_text(s.text), plain_text(translations[i])):
                            stats.name_issues.append(f"{item.path}#{i}: {v}")
            if dry_run:
                continue

            out_numbered = []
            for i, seg in enumerate(segs, 1):
                dst = translations.get(i, seg.text)
                out_numbered.append((i, dst))
                if clean_only:
                    pass
                elif seg.translatable and dst.strip() == seg.text.strip() and len(_HAS_LETTERS.findall(seg.text)) > 25:
                    stats.unchanged.append(f"{item.path}#{i}: {plain_text(seg.text)[:80]}")
                elif seg.translatable:
                    stats.translated += 1
                    problem = _completeness_problem(seg.text, dst)
                    if problem:
                        stats.incomplete.append(f"{item.path}#{i}: {problem}")
                for w in apply_translation(seg, dst):
                    stats.tag_warnings.append(f"{item.path}#{i}: {w}")
                if self.bilingual and not clean_only and seg.translatable and dst.strip() != seg.text.strip():
                    add_original(seg, seg.text)
            unwrap_runs(tree)
            if not clean_only:
                set_document_language(tree, self.cfg.target_code)
                _write_txt(work / "translated" / f"{stem}.txt", out_numbered, marked=True)
                _write_txt(work / "translated-plain" / f"{stem}.txt", out_numbered, marked=False)
            replacements[item.path] = serialize_xhtml(tree)

        if extract_only:
            log.info("Extracted %d segments from %d documents into %s", stats.segments, stats.documents, book_work)
            return None
        if dry_run:
            self._dry_run_summary(work, stats)
            return None
        if clean_only:
            out = self.cfg.output_dir / f"{epub_path.stem}_CLEAN.epub"
            write_epub(book.zip, out, replacements)
            problems = validate_epub(out, epub_path)
            for p in problems:
                log.error("VALIDATION: %s", p)
            log.info("Cleaned (untranslated) EPUB: %s", out)
            return out

        title, description = self._translate_metadata(book, work)
        opf_bytes, old_uid, new_uid = book.translated_opf(self.cfg.target_code, self.cfg.target_name, self.cfg.model,
                                                          title=title, description=description)
        replacements[book.opf_path] = opf_bytes
        if book.ncx_path:
            replacements[book.ncx_path] = self._translate_ncx(book, work, old_uid, new_uid, stats, title)

        if self.translator:
            stats.requests = self.translator.requests_made + (
                self.translator.fallback.requests_made if self.translator.fallback else 0)
            stats.fallback = list(self.translator.fallback_used)
            self.translator.fallback_used.clear()
        out = self.output_path(epub_path)
        write_epub(book.zip, out, replacements)
        problems = validate_epub(out, epub_path)
        checked = None
        cmd = epubcheck.find_command(self.cfg.epubcheck, ROOT)
        if cmd:
            log.info("Running EPUBCheck...")
            checked = epubcheck.new_problems(cmd, epub_path, out)
            if checked:
                problems += [f"EPUBCheck: {e}" for e in checked["new_errors"]]
        self._update_series_glossary(work)
        self._report(work, out, stats, problems, checked)
        return out

    def _translate_ncx(self, book: EpubPackage, work: Path, old_uid, new_uid, stats: Stats,
                       title: str | None = None) -> bytes:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
        ncx = etree.fromstring(book.read(book.ncx_path), parser)
        ncx.set("{http://www.w3.org/XML/1998/namespace}lang", self.cfg.target_code)
        doc_title = ncx.find(f"{{{NCX_NS}}}docTitle/{{{NCX_NS}}}text")
        if title and doc_title is not None:
            doc_title.text = title
        if old_uid and new_uid:
            for meta in ncx.iter(f"{{{NCX_NS}}}meta"):
                if meta.get("name") == "dtb:uid":
                    meta.set("content", new_uid)
        labels = [t for t in ncx.iterfind(f".//{{{NCX_NS}}}navMap//{{{NCX_NS}}}text") if (t.text or "").strip()]
        if labels:
            cache = SegmentCache(work / "cache" / "toc_ncx.json", self.use_cache)
            items = [(i, " ".join(t.text.split())) for i, t in enumerate(labels, 1)]
            pending = [(i, s) for i, s in items if cache.get(s) is None and _HAS_LETTERS.search(s)]
            if pending:
                got = self.translator.translate_items(pending, self.glossary)
                cache.put_many([(s, got.get(i, s)) for i, s in pending])
                log.info("  toc.ncx: %d labels translated", len(pending))
            for (i, s), el in zip(items, labels):
                hit = cache.get(s)
                if hit:
                    el.text = plain_text(hit)
        return etree.tostring(ncx, xml_declaration=True, encoding="utf-8")

    def _write_names(self, work: Path, stats: Stats) -> None:
        path = work / "names.txt"
        if not self.glossary:
            return
        header = (f"{len(stats.name_issues)} glossary rule(s) broken. Fix the glossary if a rule is wrong, or run "
                  f"with --fix-names to re-translate these segments.\n\n")
        path.write_text(header + "\n".join(stats.name_issues) + "\n", encoding="utf-8")
        segs = len({i.split(": ", 1)[0] for i in stats.name_issues})
        log.info("Name consistency: %d issue(s) in %d segment(s) -> %s", len(stats.name_issues), segs, path)

    def _dry_run_summary(self, work: Path, stats: Stats) -> None:
        self._write_names(work, stats)
        glossary_requests = 0
        if self.cfg.auto_glossary and not (work / "glossary.txt").exists():
            cand = work.parent / "glossary-candidates.txt"
            n = len(cand.read_text(encoding="utf-8").splitlines()) if cand.exists() else 0
            glossary_requests = -(-n // 150)
        total = stats.planned_requests + glossary_requests
        log.info("Dry run: %d documents, %d segments; %d to translate (%s chars, ~%s tokens in)%s",
                 stats.documents, stats.segments, stats.pending, f"{stats.pending_chars:,}",
                 f"{stats.pending_chars // 4:,}",
                 f", {stats.renamed} of them because they break the glossary" if stats.renamed else "")
        log.info("Dry run: ~%d API request(s) (%d translation + %d glossary)%s", total,
                 stats.planned_requests, glossary_requests,
                 f", ~{-(-total // self.cfg.rpd)} day(s) at GEMINI_RPD={self.cfg.rpd}" if self.cfg.rpd and total else "")

    def _report(self, work: Path, out: Path, stats: Stats, problems: list[str], checked: dict | None = None) -> None:
        self._write_names(work, stats)
        report = {
            "output": str(out),
            "model": self.cfg.model,
            "target_language": self.cfg.target_code,
            "bilingual": self.bilingual,
            "series": self.series,
            "documents": stats.documents,
            "segments": stats.segments,
            "translated_segments": stats.translated,
            "api_requests_this_run": stats.requests,
            "possibly_untranslated": stats.unchanged,
            "possibly_incomplete": stats.incomplete,
            "inline_tag_warnings": stats.tag_warnings,
            "glossary_terms": len(self.glossary),
            "retranslated_for_names": stats.renamed,
            "name_consistency_issues": stats.name_issues,
            "translated_by_backup_model": stats.fallback,
            "malformed_source_documents": stats.recovered_docs,
            "validation_problems": problems,
            "epubcheck": checked if checked is not None else "not run (install Java + epubcheck --install-epubcheck)",
        }
        (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Segments: %d | translated: %d | possibly untranslated: %d | possibly incomplete: %d | "
                 "tag warnings: %d | API requests: %d", stats.segments, stats.translated, len(stats.unchanged),
                 len(stats.incomplete), len(stats.tag_warnings), stats.requests)
        for entry in stats.unchanged + stats.incomplete:
            log.warning("CHECK: %s", entry)
        if stats.fallback:
            log.info("%d passage(s) translated by the backup model (%s); see report.json",
                     len(stats.fallback), self.cfg.fallback_model)
        if checked is not None:
            log.info("EPUBCheck: %d new error(s), %d new warning(s) (%d already in the original)",
                     len(checked["new_errors"]), len(checked["new_warnings"]), checked["preexisting_messages"])
            for w in checked["new_warnings"]:
                log.warning("EPUBCheck: %s", w)
        if problems:
            for p in problems:
                log.error("VALIDATION: %s", p)
        else:
            log.info("Validation OK")
        log.info("Report: %s", work / "report.json")
        log.info("Output: %s", out)


def validate_epub(out: Path, original: Path) -> list[str]:
    """Structural checks on the generated EPUB against the original."""
    problems: list[str] = []
    with zipfile.ZipFile(out) as z, zipfile.ZipFile(original) as o:
        infos = z.infolist()
        if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
            problems.append("mimetype is not the first, uncompressed entry")
        if z.testzip() is not None:
            problems.append("zip CRC error")
        missing = set(o.namelist()) - set(z.namelist())
        if missing:
            problems.append(f"files missing from output: {sorted(missing)}")
        pkg = EpubPackage(out)
        for item in pkg.manifest.values():
            if item.path not in pkg.names:
                problems.append(f"manifest item missing: {item.path}")
        strict = etree.XMLParser(resolve_entities=False, no_network=True)
        for item in pkg.content_documents():
            try:
                root = etree.fromstring(z.read(item.path), strict)
            except etree.XMLSyntaxError as exc:
                problems.append(f"{item.path} is not well-formed XML: {exc}")
                continue
            orig_root, _ = parse_xhtml(o.read(item.path))
            for tag in ("img", "image", "a", "table", "svg"):
                a = len(orig_root.getroot().findall(f".//{{*}}{tag}"))
                b = len(root.findall(f".//{{*}}{tag}"))
                if a != b:
                    problems.append(f"{item.path}: <{tag}> count changed {a} -> {b}")
        for name in o.namelist():
            if not name.lower().endswith((".html", ".xhtml", ".htm", ".opf", ".ncx")) and name != "mimetype":
                if o.read(name) != z.read(name):
                    problems.append(f"non-text resource changed: {name}")
    return problems
