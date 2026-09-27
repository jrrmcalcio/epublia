"""Fix common validity errors that the *source* EPUB already had, so the translation comes out
cleaner than the original. Only markup changes (never text, never resources), and each fix is
chosen so the page renders the same:

- <img> without alt                       -> alt="" (decorative, what readers assume anyway)
- block element inside an inline one      -> the inline wrapper becomes a <div> (same attributes)
  (<span><p>…</p></span>), or a <p> holding blocks becomes a <div>
- EPUB 2 only: text or inline elements directly in <body>/<blockquote> (XHTML 1.1 wants blocks
  there)                                   -> wrapped in a <div>, which is how readers render them
- empty <body> in EPUB 2                   -> gets an empty <div>
- NCX ids that are not valid XML names    -> prefixed/sanitised
"""
from __future__ import annotations

import re

from lxml import etree

from .segmenter import BLOCK_TAGS, local

XHTML_NS = "http://www.w3.org/1999/xhtml"
_INLINE_WRAPPERS = {"span", "font", "em", "i", "b", "strong", "small", "big", "u", "s", "strike", "cite",
                    "q", "sub", "sup", "label", "abbr", "acronym", "tt", "dfn", "kbd", "samp", "var", "bdo"}
_FLOW_BLOCKS = {"address", "blockquote", "div", "dl", "h1", "h2", "h3", "h4", "h5", "h6", "hr", "ol", "p",
                "pre", "table", "ul", "form", "fieldset", "noscript", "script", "ins", "del"}
_STRICT_BLOCK_PARENTS = {"body", "blockquote", "form", "noscript"}  # XHTML 1.1: Block content only
_BLOCKISH = (BLOCK_TAGS | _FLOW_BLOCKS) - {"body"}
_NCNAME = re.compile(r"^[A-Za-z_][\w.\-]*$")


def _is_el(node) -> bool:
    return isinstance(node.tag, str)


def _q(el, name: str) -> str:
    return f"{{{el.tag[1:].split('}', 1)[0]}}}{name}" if el.tag.startswith("{") else name


def repair_document(tree: etree._ElementTree, epub_version: str = "2.0") -> list[str]:
    """Repair ``tree`` in place; returns one label per fix applied."""
    fixes: list[str] = []
    root = tree.getroot()

    for img in root.iter("{*}img", "img"):
        if "alt" not in img.attrib:
            img.set("alt", "")
            fixes.append("img alt added")

    # Inline elements (or <p>) that contain blocks: turn the wrapper into a <div>.
    for el in list(root.iter()):
        if not _is_el(el):
            continue
        tag = local(el.tag)
        if tag in _INLINE_WRAPPERS or tag == "p":
            if any(_is_el(d) and local(d.tag) in _BLOCKISH for d in el.iterdescendants()):
                el.tag = _q(el, "div")
                fixes.append(f"<{tag}> holding blocks -> <div>")

    if epub_version.startswith("2"):
        body = next((e for e in root.iter() if _is_el(e) and local(e.tag) == "body"), None)
        for parent in [e for e in root.iter() if _is_el(e) and local(e.tag) in _STRICT_BLOCK_PARENTS]:
            n = _wrap_inline_runs(parent)
            fixes += ["inline content in <%s> wrapped in <div>" % local(parent.tag)] * n
        if body is not None and len(body) == 0 and not (body.text or "").strip():
            etree.SubElement(body, _q(body, "div"))
            fixes.append("empty <body> given a <div>")
    return fixes


def _wrap_inline_runs(parent) -> int:
    """Wrap each run of text/inline children of ``parent`` in a <div>. Returns runs wrapped."""
    wrapped = 0
    run: list = []
    lead = parent.text
    parent_text_in_run = bool((lead or "").strip())

    def flush(before) -> None:
        nonlocal wrapped, run, parent_text_in_run
        if not run and not parent_text_in_run and not (before is not None and (before.tail or "").strip()):
            run = []
            return
        div = etree.Element(_q(parent, "div"))
        if before is None:
            div.text, parent.text = parent.text, None
            index = 0
        else:
            div.text, before.tail = before.tail, None
            index = parent.index(before) + 1
        parent.insert(index, div)
        for node in run:
            div.append(node)  # moves the node with its tail
        # Whitespace-only trailing tail stays outside, for tidy source.
        if len(div) and div[-1].tail and not div[-1].tail.strip():
            div.tail, div[-1].tail = div[-1].tail, None
        wrapped += 1
        run = []
        parent_text_in_run = False

    before = None
    for child in list(parent):
        block = _is_el(child) and local(child.tag) in _BLOCKISH
        if block:
            if run or (before is None and parent_text_in_run) or (before is not None and (before.tail or "").strip()):
                flush(before)
            before = child
            parent_text_in_run = False
        else:
            if not _is_el(child) and not run and not (child.tail or "").strip():
                continue  # comments / PIs between blocks are fine where they are
            run.append(child)
    if run or (before is None and parent_text_in_run) or (before is not None and (before.tail or "").strip()):
        flush(before)
    return wrapped


def repair_ncx(ncx) -> list[str]:
    """Make navPoint/pageTarget ids valid XML names (they are not referenced from elsewhere)."""
    fixes = []
    seen: set[str] = set()
    for el in ncx.iter():
        if not _is_el(el) or local(el.tag) not in ("navpoint", "pagetarget", "navtarget"):
            continue
        ident = el.get("id")
        if ident is None or (_NCNAME.match(ident) and ident not in seen):
            seen.add(ident or "")
            continue
        new = "np_" + re.sub(r"[^\w.\-]", "_", ident)
        while new in seen:
            new += "_"
        el.set("id", new)
        seen.add(new)
        fixes.append("invalid NCX id renamed")
    return fixes
