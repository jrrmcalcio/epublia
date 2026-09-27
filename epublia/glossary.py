"""Name/term consistency: glossary parsing, candidate extraction and translation checks.

Glossary format, one rule per line (``#`` starts a comment):

    Gray Tiger = Tigre Gris
    Preserver = Preservador | Preservadora   # alternatives: any of them is accepted
    Shelak = shelak                          # lowercase rendering: capitalised mid-sentence is flagged
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

_WORD = r"[A-Z][A-Za-z'’\-]*[A-Za-z]"
_TERM = re.compile(rf"{_WORD}(?:(?:\s+(?:of|from|the|de|del|la)){{0,2}}\s+{_WORD})*")
# A capitalised word right after these (or at the start) is sentence-initial, not evidence of a name.
_SENTENCE_START = re.compile(r"(?:^|[.!?¿¡…:;«»“”\"‘’'()\[\]•—–-])\s*$")
_CONTRACTION = re.compile(r"['’](?:m|d|ll|ve|re|t|s)$", re.I)
_POSSESSIVE = re.compile(r"['’]s$")
_STOP = set("""
I A An The He She It They We You Me Him Her Us Them His Hers Its Our Their Your My Mine This That These Those
There Here What When Where Why How Who Whom Which Whose Then Now Yes No Not Oh Ah Um Uh Hey Well So But And Or
Nor For Yet If As At In On Of To By Up With From Into Onto Over Under After Before Once Only Just Even Still
All Any Some Each Every Both Either Neither One Two Three Maybe Perhaps Please Sorry Okay OK Good Very Really
Indeed Is Are Was Were Be Been Do Does Did Don't Can't Won't Let Let's Get Got Come Go Look Tell See Say Said
Chapter Part Book Prologue Epilogue Contents Mr Mrs Ms Dr Sir Madam
""".split())


@dataclass
class Entry:
    source: str
    renderings: list[str]
    line: str  # original line (with any note) as shown to the model


def parse(text: str) -> dict[str, Entry]:
    entries: dict[str, Entry] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        rule = line.split("#", 1)[0].strip()
        src, _, dst = rule.partition("=")
        src = src.strip()
        alts = [a.strip() for a in dst.split("|") if a.strip()]
        if src and alts:
            entries[src] = Entry(src, alts, line)
    return entries


def _boundary(term: str) -> re.Pattern:
    """Whole-word, case-sensitive match; straight and curly apostrophes are interchangeable."""
    body = re.escape(term.replace("’", "'")).replace("'", "['’]")
    return re.compile(rf"(?<![\w'’]){body}(?!\w)")


def _is_sentence_initial(text: str, pos: int) -> bool:
    return bool(_SENTENCE_START.search(text[max(0, pos - 4):pos]))


class Glossary:
    def __init__(self, *texts: str):
        """Later texts override earlier ones (auto glossary first, user glossary last)."""
        self.entries: dict[str, Entry] = {}
        for t in texts:
            self.entries.update(parse(t or ""))
        self._src_re = {k: _boundary(k) for k in self.entries}
        self._dst_re = {k: [_boundary(r) for r in e.renderings] for k, e in self.entries.items()}

    def __bool__(self) -> bool:
        return bool(self.entries)

    def __len__(self) -> int:
        return len(self.entries)

    def relevant(self, text: str) -> list[Entry]:
        """Entries whose source term appears in ``text`` (case-sensitive, whole words)."""
        return [self.entries[k] for k, rx in self._src_re.items() if rx.search(text)]

    def prompt_block(self, text: str) -> str:
        return "\n".join(e.line for e in self.relevant(text))

    def violations(self, source: str, translation: str) -> list[str]:
        """Rules broken by ``translation`` of ``source`` (both plain text)."""
        problems: list[str] = []
        for entry in self.relevant(source):
            n_src = len(self._src_re[entry.source].findall(source))
            case_issues: list[str] = []
            found = False
            for r, rx in zip(entry.renderings, self._dst_re[entry.source]):
                n_ok = len(rx.findall(translation))
                found = found or n_ok > 0
                if len(r) < 2 or r.lower() == r.upper():
                    continue
                if r[:1].islower():
                    other = r[:1].upper() + r[1:]
                    hits = [_is_sentence_initial(translation, m.start()) for m in _boundary(other).finditer(translation)]
                    found = found or any(hits)  # capitalised at the start of a sentence is correct
                    if not all(hits):
                        case_issues.append(f"{entry.source}: '{other}' should be '{r}'")
                else:
                    # The lowercase form may be an ordinary word ("Les" vs "les"); only flag it when
                    # the counts show it stands for the name.
                    other = r[:1].lower() + r[1:]
                    low = len(_boundary(other).findall(translation))
                    if low and low + n_ok <= n_src:
                        case_issues.append(f"{entry.source}: '{other}' should be '{r}'")
            if case_issues:
                problems += case_issues
            elif not found:
                problems.append(f"{entry.source}: expected {' | '.join(entry.renderings)}")
        return problems


def find_candidates(texts: list[str], limit: int = 300) -> list[tuple[str, int, str]]:
    """Likely proper names / invented terms in the book: [(term, count, example context)].

    A term qualifies when it appears at least twice and at least once capitalised mid-sentence,
    so ordinary words that are only capitalised at the start of a sentence are left out."""
    total: Counter[str] = Counter()
    mid: Counter[str] = Counter()
    example: dict[str, str] = {}
    for text in texts:
        for m in _TERM.finditer(text):
            term = _POSSESSIVE.sub("", m.group(0))
            words = term.split()
            if words and _CONTRACTION.search(words[-1]):  # I'm, He'd...
                continue
            # Drop leading/trailing function words ("The Gray Tiger" -> "Gray Tiger").
            while words and (words[0] in _STOP or words[0].islower()):
                words.pop(0)
            while words and (words[-1] in _STOP or words[-1].islower()):
                words.pop()
            if not words:
                continue
            term = " ".join(words)
            if len(term) < 2 or term in _STOP:
                continue
            total[term] += 1
            start = m.start() + m.group(0).find(words[0])
            if not _is_sentence_initial(text, start):
                mid[term] += 1
                if term not in example:
                    example[term] = " ".join(text[max(0, start - 70):start + len(term) + 70].split())
    ranked = [(t, n, example[t]) for t, n in total.most_common() if n >= 2 and mid[t] >= 1]
    return ranked[:limit]


def merge(base_text: str, new_text: str) -> tuple[str, int, int]:
    """Merge glossary ``new_text`` into ``base_text`` (new wins). Returns (rules text, added, changed)."""
    merged = parse(base_text)
    added = changed = 0
    for key, entry in parse(new_text).items():
        old = merged.get(key)
        if old is None:
            added += 1
        elif old.renderings != entry.renderings:
            changed += 1
        merged[key] = entry
    lines = [e.line for e in sorted(merged.values(), key=lambda e: e.source.lower())]
    return "\n".join(lines), added, changed
