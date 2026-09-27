"""Remove PDF-conversion artefacts from segments before translation.

Works on the placeholder text of each Segment (see segmenter.py), so the DOM is rebuilt by
apply_translation exactly like a translation would be. Handles:

- running headers/footers: "2 CHRISTIE GOLDEN", "FIRSTBORN 3", any short numbered line that
  repeats >= 3 times across the book, and ALL-CAPS lines repeated >= 5 times mid-paragraph;
- page numbers on their own line: "12", "- 12 -", "[12]", "Page 12", "p. 12", "12 of 300", "xii";
- scanner / ebook-site watermarks ("OceanofPDF.com", "This page intentionally left blank", ...);
- line breaks (<br/>) that cut a sentence in half ("had done <br/> to get inside");
- paragraphs split mid-sentence (</p><p> followed by a lowercase continuation);
- words hyphenated across a line break ("some- <br/> thing") or a space ("some- thing");
- ligatures (ﬁ ﬂ ﬀ ﬃ ﬄ), soft hyphens, zero-width and control characters;
- letter-spaced words ("C H A P T E R") and stray spaces before , . ; ! ?

Probable PDF footnote bodies that interrupt a paragraph are only *reported*, never deleted:
they cannot be told apart from story text reliably, and deleting story text is worse.
Real EPUB footnotes (links/noterefs) are kept as inline tags and translated normally.

Every change is recorded so it can be reviewed in work/<book>/cleanup.txt.
"""
from __future__ import annotations

import html
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from .segmenter import _TOKEN, WRAPPER_TAG, Segment, apply_translation, local

_ANY_TAG = re.compile(r"</?x\d+/?>")
_HAS_NUM = re.compile(r"\b\d{1,4}\b")
_PAGE_NO = re.compile(
    r"^(?:[-–—\[(|]\s*)?(?:(?:page|p[aá]gina|p[aá]g|p)\.?\s*)?\d{1,4}(?:\s*(?:of|de|/)\s*\d{1,4})?"
    r"(?:\s*[-–—\])|])?$"
    r"|^(?=[ivxlc]{2,7}$)c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})$",
    re.IGNORECASE,
)
# Ebook-site names: stripped as a single token wherever they appear.
_WATERMARK_SITES = re.compile(
    r"[ ]?(?:https?://)?(?:www\.)?[\w.-]*(?:oceanofpdf|z-?library|z-lib|libgen|b-ok|dokumen|pdfdrive|"
    r"epubpub|ebook3000)\.(?:com|org|net|pub|lib|sk|se|is|rs|li|to|io)[\w./-]*",
    re.IGNORECASE,
)
# Scanner / blank-page notes: the whole (short) line is removed.
_WATERMARKS = re.compile(
    r"scanned (?:and|&(?:amp;)?) proofed|scanned by|\bocr(?:'d| by)|proofed by|converted by|uploaded by|"
    r"created (?:with|by) .{0,30}(?:pdf|converter)|get (?:more|free) e-?books|"
    r"(?:this )?page (?:is )?intentionally (?:left )?blank|"
    r"downloaded from|visit .{0,30}for more (?:free )?e-?books",
    re.IGNORECASE,
)
_FOOTNOTE_BODY = re.compile(r"^(?:\d{1,2}|\*{1,3}|†|‡)\s+[A-Z]")
_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
_INVISIBLE = re.compile("[­​‌‍⁠﻿\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LETTER_SPACED = re.compile(r"\b(?:[A-ZÁÉÍÓÚÑ] ){3,}[A-ZÁÉÍÓÚÑ]\b")
_HYPHEN_SPACE = re.compile(
    r"(?<=[a-záéíóúñü])- (?!(?:and|or|to|nor|y|o|e|u|et|ou|und|oder)\b)(?=[a-záéíóúñü])"
)
# "word ," -> "word," but never touch spaced ellipses (". . .").
_SPACE_BEFORE_PUNCT = re.compile(r"(?<=[\w”’\"])[ ]+(?![.] [.])(?=[,.;!?](?:\s|$|<))")
_JOIN_BEFORE = re.compile(r"[a-záéíóúñüàèìòùâêîôûç0-9,;:\-]$")
_MERGEABLE = {"p", "div", WRAPPER_TAG}
_HEADINGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _plain(text: str) -> str:
    return " ".join(html.unescape(_ANY_TAG.sub(" ", text)).split())


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^\w\s]", " ", text.casefold()).split())


def _header_key(line: str) -> str:
    return _norm(_HAS_NUM.sub(" ", line))


@dataclass
class CleanupLog:
    entries: list[str] = field(default_factory=list)
    headers: int = 0
    page_numbers: int = 0
    watermarks: int = 0
    line_joins: int = 0
    paragraph_merges: int = 0
    char_fixes: int = 0
    footnote_suspects: int = 0

    def summary(self) -> str:
        return (f"headers/footers: {self.headers} | page numbers: {self.page_numbers} | "
                f"watermarks: {self.watermarks} | broken lines joined: {self.line_joins} | "
                f"split paragraphs merged: {self.paragraph_merges} | segments with character fixes: "
                f"{self.char_fixes} | possible footnotes (kept): {self.footnote_suspects}")


class Cleaner:
    def __init__(self, title: str = "", author: str = ""):
        names = {title, title.split(":")[0], author}
        if author:
            names.add(author.split()[-1])
        self.names = {_norm(n) for n in names if n and len(_norm(n)) >= 3}
        self.repeated: set[str] = set()
        self.repeated_caps: set[str] = set()
        self.log = CleanupLog()

    # ------------------------------------------------------------------ lines

    @staticmethod
    def _br_ids(seg: Segment) -> set[int]:
        return {k for k in seg.void_ids if local(getattr(seg.refs[k], "tag", "")) == "br"}

    def _split_lines(self, seg: Segment) -> list[tuple[int, int]]:
        """(start, end) spans of the text between <br/> placeholders."""
        brs = self._br_ids(seg)
        spans, start = [], 0
        for m in _TOKEN.finditer(seg.text):
            if m.group(3) == "/" and int(m.group(2)) in brs:
                spans.append((start, m.start()))
                start = m.end()
        spans.append((start, len(seg.text)))
        return spans

    def learn(self, all_segments: list[Segment]) -> None:
        """First pass over the whole book: find running heads that repeat."""
        counts: Counter[str] = Counter()
        caps: Counter[str] = Counter()
        for seg in all_segments:
            spans = self._split_lines(seg)
            for s, e in spans:
                line = _plain(seg.text[s:e])
                if 0 < len(line) <= 60 and _HAS_NUM.search(line) and not _PAGE_NO.match(line):
                    key = _header_key(line)
                    if key and len(key) >= 3:
                        counts[key] += 1
                elif len(spans) > 1 and 6 <= len(line) <= 40 and line.isupper() \
                        and sum(c.isalpha() for c in line) >= 5:
                    caps[_norm(line)] += 1
        self.repeated = {k for k, n in counts.items() if n >= 3}
        self.repeated_caps = {k for k, n in caps.items() if n >= 5}

    def _is_header(self, line: str) -> bool:
        if not _HAS_NUM.search(line) or len(line) > 60:
            return False
        key = _header_key(line)
        return bool(key) and (key in self.names or key in self.repeated)

    # ------------------------------------------------------------------ per segment

    def _classify(self, line: str, multi_line: bool) -> str | None:
        if self._is_header(line):
            return "header"
        if multi_line and _PAGE_NO.match(line):
            return "page"
        if len(line) <= 90 and _WATERMARKS.search(line):
            return "watermark"
        if multi_line and line.isupper() and _norm(line) in self.repeated_caps:
            return "header"
        return None

    def _remove_line_artefacts(self, seg: Segment, where: str) -> None:
        if local(seg.element.tag) in _HEADINGS:
            return
        spans = self._split_lines(seg)
        text = seg.text
        # Walk backwards so earlier offsets stay valid.
        for idx in range(len(spans) - 1, -1, -1):
            s, e = spans[idx]
            line = _plain(text[s:e])
            if not line:
                continue
            kind = self._classify(line, len(spans) > 1)
            if not kind:
                if idx > 0 and _FOOTNOTE_BODY.match(line) and len(line) < 300:
                    self.log.footnote_suspects += 1
                    self.log.entries.append(f"{where}: POSSIBLE FOOTNOTE (kept, review) {line[:90]!r}")
                continue
            # Keep wrapper open/close tags (drop only text and void tags) so nesting stays valid.
            kept = "".join(m.group(0) for m in _TOKEN.finditer(text[s:e]) if m.group(3) != "/")
            prev_kept = None
            while prev_kept != kept:  # drop wrappers left empty: <x2></x2>
                prev_kept, kept = kept, re.sub(r"<x(\d+)></x\1>", "", kept)
            # Also drop the <br/> that introduced (or, for the first line, ended) this line.
            if idx > 0:
                text = text[:spans[idx - 1][1]] + kept + text[e:]
            elif len(spans) > 1:
                text = kept + text[spans[1][0]:]
            else:
                text = kept
            if kind == "header":
                self.log.headers += 1
            elif kind == "watermark":
                self.log.watermarks += 1
            else:
                self.log.page_numbers += 1
            self.log.entries.append(f"{where}: removed {kind} {line!r}")
        seg.text = " ".join(text.split())

    def _join_broken_lines(self, seg: Segment, where: str) -> None:
        brs = self._br_ids(seg)
        text = seg.text
        out, pos, line_start = [], 0, 0
        for m in _TOKEN.finditer(text):
            if not (m.group(3) == "/" and int(m.group(2)) in brs):
                continue
            before = _plain(text[:m.start()])
            after = _plain(text[m.end():])
            line = _plain(text[line_start:m.start()])
            line_start = m.end()
            if not (before and after):
                continue
            # "had done ⏎ to get", "how I ⏎ wanted": continuation in lowercase.
            lower_next = after[0].islower() and (_JOIN_BEFORE.search(before) or before[-1].isalpha())
            # "about the ⏎ Marines": previous line is long prose that stops on a lowercase word
            # (length guard so short title-like lines are never glued to the next one).
            upper_next = (after[0].isupper() and len(line) >= 25
                          and re.search(r"[a-záéíóúñü,;]$", before) is not None)
            if lower_next or upper_next:
                hyphen = before.endswith("-") and before[-2:-1].isalpha()
                chunk = text[pos:m.start()].rstrip()
                if hyphen and chunk.endswith("-"):
                    chunk = chunk[:-1]
                out.append(chunk)
                out.append("" if hyphen else " ")
                pos = m.end()
                while pos < len(text) and text[pos] == " ":
                    pos += 1
                self.log.line_joins += 1
                self.log.entries.append(f"{where}: joined broken line …{before[-30:]!r} ⏎ {after[:30]!r}…")
        out.append(text[pos:])
        seg.text = "".join(out)

    def _fix_characters(self, seg: Segment, where: str) -> None:
        text = seg.text.translate(_LIGATURES)
        text = _INVISIBLE.sub("", text)
        sites = _WATERMARK_SITES.findall(text)
        if sites:
            text = _WATERMARK_SITES.sub("", text)
            self.log.watermarks += len(sites)
            self.log.entries.append(f"{where}: removed watermark {', '.join(s.strip() for s in sites)!r}")
        text = _LETTER_SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)
        text = _HYPHEN_SPACE.sub("", text)
        text = _SPACE_BEFORE_PUNCT.sub("", text)
        if text != seg.text:
            self.log.char_fixes += 1
            self.log.entries.append(f"{where}: character fixes (ligatures/hyphenation/spacing)")
            seg.text = text

    @staticmethod
    def _prune_refs(seg: Segment) -> None:
        present = {int(m.group(2)) for m in _TOKEN.finditer(seg.text)}
        seg.refs = {k: v for k, v in seg.refs.items() if k in present}
        seg.void_ids &= present

    # ------------------------------------------------------------------ merging

    @staticmethod
    def _adjacent(a: Segment, b: Segment) -> bool:
        ea, eb = a.element, b.element
        return (ea.getparent() is eb.getparent() and ea.getnext() is eb
                and not (ea.tail or "").strip()
                and local(ea.tag) in _MERGEABLE and local(eb.tag) in _MERGEABLE)

    @staticmethod
    def _merge(a: Segment, b: Segment) -> None:
        offset = max(a.refs, default=0)

        def renum(m):
            return f"<{m.group(1)}x{int(m.group(2)) + offset}{m.group(3)}>"

        a.text = a.text.rstrip() + " " + _TOKEN.sub(renum, b.text).lstrip()
        for k, v in b.refs.items():
            a.refs[k + offset] = v
        a.void_ids |= {k + offset for k in b.void_ids}
        a.trail_ws = b.trail_ws
        # Drop b's element from the tree, keeping whatever text followed it.
        eb = b.element
        parent = eb.getparent()
        prev = eb.getprevious()
        if eb.tail:
            if prev is not None:
                prev.tail = (prev.tail or "") + eb.tail
            else:
                parent.text = (parent.text or "") + eb.tail
        parent.remove(eb)

    # ------------------------------------------------------------------ entry point

    def clean_document(self, segs: list[Segment], doc: str) -> list[Segment]:
        """Clean one document's segments in place; returns the surviving segments."""
        for i, seg in enumerate(segs, 1):
            original = seg.text
            self._remove_line_artefacts(seg, f"{doc}#{i}")
            self._join_broken_lines(seg, f"{doc}#{i}")
            if seg.text != original:
                self._prune_refs(seg)

        result: list[Segment] = []
        for i, seg in enumerate(segs, 1):
            if not _plain(seg.text) and not seg.void_ids:
                apply_translation(seg, seg.text)  # nothing but artefacts: empty it in the DOM
                continue
            prev = result[-1] if result else None
            if prev is not None and self._adjacent(prev, seg):
                end, start = _plain(prev.text), _plain(seg.text)
                if end and start and _JOIN_BEFORE.search(end) and start[0].islower():
                    self.log.paragraph_merges += 1
                    self.log.entries.append(
                        f"{doc}#{i}: merged split paragraph …{end[-30:]!r} + {start[:30]!r}…")
                    self._merge(prev, seg)
                    continue
            result.append(seg)
        for i, seg in enumerate(result, 1):
            self._fix_characters(seg, f"{doc}#{i}")
        return result
