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

from .cleanup import Cleaner
from .config import Config
from .epub import NCX_NS, EpubPackage, write_epub
from .segmenter import (
    _HAS_LETTERS,
    Segment,
    apply_translation,
    check_placeholders,
    extract_segments,
    parse_xhtml,
    plain_text,
    serialize_xhtml,
    set_document_language,
    unwrap_runs,
)
from .translator import GeminiTranslator

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


class BookTranslator:
    def __init__(self, cfg: Config, translator: GeminiTranslator | None, *, use_cache: bool = True,
                 clean: bool = True):
        self.cfg = cfg
        self.clean = clean
        self.translator = translator
        self.use_cache = use_cache

    def output_path(self, epub_path: Path) -> Path:
        return self.cfg.output_dir / f"{epub_path.stem}_{self.cfg.target_code.upper()}.epub"

    # ------------------------------------------------------------------ helpers

    def _translate_segments(self, segs: list[Segment], cache: SegmentCache, label: str, stats: Stats) -> dict[int, str]:
        """Return {segment_index: translated_text} for every translatable segment."""
        result: dict[int, str] = {}
        pending: list[tuple[int, str]] = []
        for idx, seg in enumerate(segs, 1):
            if not seg.translatable:
                result[idx] = seg.text
                continue
            hit = cache.get(seg.text)
            if hit is not None:
                result[idx] = hit
            else:
                pending.append((idx, seg.text))
        if pending and self.translator is None:
            raise RuntimeError("translator not configured")
        chunks = _chunks(pending, self.cfg.max_chars)
        for n, chunk in enumerate(chunks, 1):
            t0 = time.monotonic()
            got = self.translator.translate_items(chunk)
            # Placeholder tags must survive; re-ask once, alone, for segments that broke them.
            broken = [(i, t) for i, t in chunk if check_placeholders(segs[i - 1], got.get(i, t))]
            if broken:
                log.info("  %d segment(s) with damaged inline tags, re-requesting", len(broken))
                retry = self.translator.translate_items(broken)
                for i, _ in broken:
                    if not check_placeholders(segs[i - 1], retry.get(i, "")):
                        got[i] = retry[i]
            cache.put_many([(t, got.get(i, t)) for i, t in chunk])
            for i, t in chunk:
                result[i] = got.get(i, t)
            log.info("  %s: request %d/%d (%d segments) done in %.1fs",
                     label, n, len(chunks), len(chunk), time.monotonic() - t0)
        return result

    # ------------------------------------------------------------------ main

    def translate(self, epub_path: Path, *, extract_only: bool = False, clean_only: bool = False) -> Path | None:
        book = EpubPackage(epub_path)
        work = self.cfg.work_dir / slugify(epub_path.stem)
        stats = Stats()
        log.info("Book: %s — %s (lang=%s, EPUB %s)", book.title, book.author or "?", book.language or "?", book.version)
        log.info("Work dir: %s", work)

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
            (work / "cleanup.txt").parent.mkdir(parents=True, exist_ok=True)
            (work / "cleanup.txt").write_text(
                cleaner.log.summary() + "\n\n" + "\n".join(cleaner.log.entries) + "\n", encoding="utf-8")
            log.info("Cleanup: %s", cleaner.log.summary())
            log.info("Cleanup details: %s", work / "cleanup.txt")

        # Phase 3: per chapter TXT -> Gemini -> TXT -> XHTML.
        for n, item, tree, segs in parsed:
            name = posixpath.basename(item.path)
            label = f"[{n:03d}/{len(docs):03d}] {name}"
            stem = f"{n:03d}_{Path(name).stem}"
            numbered = [(i, s.text) for i, s in enumerate(segs, 1)]
            _write_txt(work / "source" / f"{stem}.txt", numbered, marked=True)
            _write_txt(work / "source-plain" / f"{stem}.txt", numbered, marked=False)
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
                for w in apply_translation(seg, dst):
                    stats.tag_warnings.append(f"{item.path}#{i}: {w}")
            unwrap_runs(tree)
            if not clean_only:
                set_document_language(tree, self.cfg.target_code)
                _write_txt(work / "translated" / f"{stem}.txt", out_numbered, marked=True)
                _write_txt(work / "translated-plain" / f"{stem}.txt", out_numbered, marked=False)
            replacements[item.path] = serialize_xhtml(tree)

        if extract_only:
            log.info("Extracted %d segments from %d documents into %s", stats.segments, stats.documents, work)
            return None
        if clean_only:
            out = self.cfg.output_dir / f"{epub_path.stem}_CLEAN.epub"
            write_epub(book.zip, out, replacements)
            problems = validate_epub(out, epub_path)
            for p in problems:
                log.error("VALIDATION: %s", p)
            log.info("Cleaned (untranslated) EPUB: %s", out)
            return out

        opf_bytes, old_uid, new_uid = book.translated_opf(self.cfg.target_code, self.cfg.target_name, self.cfg.model)
        replacements[book.opf_path] = opf_bytes
        if book.ncx_path:
            replacements[book.ncx_path] = self._translate_ncx(book, work, old_uid, new_uid, stats)

        if self.translator:
            stats.requests = self.translator.requests_made
        out = self.output_path(epub_path)
        write_epub(book.zip, out, replacements)
        problems = validate_epub(out, epub_path)
        self._report(work, out, stats, problems)
        return out

    def _translate_ncx(self, book: EpubPackage, work: Path, old_uid, new_uid, stats: Stats) -> bytes:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
        ncx = etree.fromstring(book.read(book.ncx_path), parser)
        ncx.set("{http://www.w3.org/XML/1998/namespace}lang", self.cfg.target_code)
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
                got = self.translator.translate_items(pending)
                cache.put_many([(s, got.get(i, s)) for i, s in pending])
                log.info("  toc.ncx: %d labels translated", len(pending))
            for (i, s), el in zip(items, labels):
                hit = cache.get(s)
                if hit:
                    el.text = plain_text(hit)
        return etree.tostring(ncx, xml_declaration=True, encoding="utf-8")

    def _report(self, work: Path, out: Path, stats: Stats, problems: list[str]) -> None:
        report = {
            "output": str(out),
            "model": self.cfg.model,
            "target_language": self.cfg.target_code,
            "documents": stats.documents,
            "segments": stats.segments,
            "translated_segments": stats.translated,
            "api_requests_this_run": stats.requests,
            "possibly_untranslated": stats.unchanged,
            "inline_tag_warnings": stats.tag_warnings,
            "malformed_source_documents": stats.recovered_docs,
            "validation_problems": problems,
        }
        (work / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        log.info("Segments: %d | translated: %d | possibly untranslated: %d | tag warnings: %d | API requests: %d",
                 stats.segments, stats.translated, len(stats.unchanged), len(stats.tag_warnings), stats.requests)
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
