"""Split XHTML documents into translatable segments and write translations back.

A *segment* is one block of running text (a paragraph, heading, list item, or a
run of loose inline content between blocks). Inline markup inside a segment is
replaced by numbered placeholders so the model can move it with the words:

    <span class="italic">this</span> knowledge<br/>   ->   <x1>this</x1> knowledge<x2/>

Only text nodes change on write-back; element names, attributes, ids, images
and every non-text node are copied from the original.
"""
from __future__ import annotations

import copy
import html
import html.entities
import re
from dataclasses import dataclass, field

from lxml import etree

XHTML_NS = "http://www.w3.org/1999/xhtml"

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "caption", "center", "dd", "details",
    "dialog", "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form", "h1",
    "h2", "h3", "h4", "h5", "h6", "header", "hgroup", "hr", "li", "main", "nav", "ol", "p",
    "section", "summary", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul",
}
# Subtrees whose text is never translated.
SKIP_TAGS = {"head", "script", "style", "svg", "math", "pre", "code", "kbd", "samp", "var", "rt", "rp"}

WRAPPER_TAG = "epublia-run"
_WS = re.compile(r"\s+")
_TOKEN = re.compile(r"<(/?)x(\d+)(/?)>")
_HAS_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def local(tag) -> str:
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1].lower()


def _is_element(node) -> bool:
    return isinstance(node.tag, str)


@dataclass
class Segment:
    element: etree._Element
    text: str                      # placeholder text sent to the model
    lead_ws: str = ""
    trail_ws: str = ""
    refs: dict[int, etree._Element] = field(default_factory=dict)
    void_ids: set[int] = field(default_factory=set)

    @property
    def translatable(self) -> bool:
        return bool(_HAS_LETTERS.search(_TOKEN.sub("", self.text)))


# --------------------------------------------------------------------------- parsing

_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}
_ENTITY = re.compile(r"&([A-Za-z][A-Za-z0-9]*);")


def _numeric_entities(text: str) -> str:
    """Turn HTML named entities (&nbsp; &mdash; ...) into numeric refs so a plain XML parser accepts them."""
    def repl(m):
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};" if cp else m.group(0)
    return _ENTITY.sub(repl, text)


def parse_xhtml(data: bytes) -> tuple[etree._ElementTree, bool]:
    """Return (tree, recovered). recovered=True means the source was malformed and lxml repaired it."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1252", errors="replace")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text)  # decl is re-added on write
    raw = _numeric_entities(text).encode("utf-8")
    try:
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
        return etree.ElementTree(etree.fromstring(raw, parser)), False
    except etree.XMLSyntaxError:
        parser = etree.XMLParser(recover=True, resolve_entities=False, no_network=True, huge_tree=True)
        return etree.ElementTree(etree.fromstring(raw, parser)), True


VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
             "source", "track", "wbr"}


def serialize_xhtml(tree: etree._ElementTree) -> bytes:
    # Write empty non-void HTML elements as <div></div>, not <div/>: some reading systems
    # parse content as HTML and would treat <div/> as an unclosed opening tag.
    for el in tree.getroot().iter():
        if _is_element(el) and el.text is None and len(el) == 0 and el.tag.startswith(f"{{{XHTML_NS}}}") \
                and local(el.tag) not in VOID_TAGS:
            el.text = ""
    return etree.tostring(tree, xml_declaration=True, encoding="utf-8")


# --------------------------------------------------------------------------- segmentation

def _has_block_descendant(el) -> bool:
    for d in el.iterdescendants():
        if _is_element(d) and local(d.tag) in BLOCK_TAGS:
            return True
    return False


def _wrap_runs(el) -> None:
    """Inside a container, wrap each run of loose inline content in a temporary <epublia-run>."""
    children = list(el)
    runs: list[list] = []
    current: list = []
    boundaries = []  # the block child preceding each run (None = run starts at el.text)
    prev_block = None
    for child in children:
        if _is_element(child) and (local(child.tag) in BLOCK_TAGS or _has_block_descendant(child)
                                   or local(child.tag) in SKIP_TAGS):
            if current or (prev_block is None and (el.text or "").strip()) or \
                    (prev_block is not None and (prev_block.tail or "").strip()):
                runs.append(current)
                boundaries.append(prev_block)
            current = []
            prev_block = child
        else:
            current.append(child)
    if current or (prev_block is None and (el.text or "").strip()) or \
            (prev_block is not None and (prev_block.tail or "").strip()):
        runs.append(current)
        boundaries.append(prev_block)

    ns = el.nsmap.get(None)
    wrapper_tag = f"{{{ns}}}{WRAPPER_TAG}" if ns else WRAPPER_TAG
    for run, before in zip(runs, boundaries):
        lead = el.text if before is None else before.tail
        if not run and not (lead or "").strip():
            continue
        w = etree.Element(wrapper_tag)
        w.text = lead
        if before is None:
            el.text = None
            index = 0
        else:
            before.tail = None
            index = el.index(before) + 1
        el.insert(index, w)
        for node in run:
            w.append(node)  # moves node (with its tail)


def _collect(el, out: list[etree._Element]) -> None:
    tag = local(el.tag)
    if not _is_element(el) or tag in SKIP_TAGS:
        return
    if tag == WRAPPER_TAG or not _has_block_descendant(el):
        if tag not in BLOCK_TAGS and tag != WRAPPER_TAG:
            return  # stray inline element handled by its container's run
        out.append(el)
        return
    _wrap_runs(el)
    for child in el:
        if _is_element(child):
            if local(child.tag) == WRAPPER_TAG:
                out.append(child)
            elif local(child.tag) in BLOCK_TAGS or _has_block_descendant(child):
                _collect(child, out)


def _encode(el, seg: Segment, counter: list[int]) -> str:
    parts = [html.escape(el.text or "", quote=False)]
    for child in el:
        if not _is_element(child):  # comments / processing instructions: keep verbatim as void refs
            counter[0] += 1
            k = counter[0]
            seg.refs[k] = child
            seg.void_ids.add(k)
            parts.append(f"<x{k}/>")
        else:
            counter[0] += 1
            k = counter[0]
            seg.refs[k] = child
            if local(child.tag) in SKIP_TAGS or (len(child) == 0 and not (child.text or "").strip()):
                seg.void_ids.add(k)
                parts.append(f"<x{k}/>")
            else:
                parts.append(f"<x{k}>{_encode(child, seg, counter)}</x{k}>")
        parts.append(html.escape(child.tail or "", quote=False))
    return "".join(parts)


def extract_segments(tree: etree._ElementTree) -> list[Segment]:
    root = tree.getroot()
    body = next((e for e in root.iter() if _is_element(e) and local(e.tag) == "body"), None)
    if body is None:
        return []
    elements: list[etree._Element] = []
    _collect(body, elements)
    segments = []
    for el in elements:
        seg = Segment(element=el, text="")
        raw = _encode(el, seg, [0])
        seg.lead_ws = " " if raw[:1].isspace() else ""
        seg.trail_ws = " " if raw[-1:].isspace() and raw.strip() else ""
        seg.text = _WS.sub(" ", raw).strip()
        if seg.text:
            segments.append(seg)
    return segments


# --------------------------------------------------------------------------- write-back

def check_placeholders(seg: Segment, translated: str) -> list[str]:
    """Return a list of problems with the placeholder tags in a translation (empty = OK)."""
    problems = []
    stack: list[int] = []
    seen: set[int] = set()
    for m in _TOKEN.finditer(translated):
        closing, k, selfclose = m.group(1) == "/", int(m.group(2)), m.group(3) == "/"
        if k not in seg.refs:
            problems.append(f"unknown tag x{k}")
            continue
        if selfclose:
            seen.add(k)
        elif closing:
            if not stack or stack[-1] != k:
                problems.append(f"misnested </x{k}>")
            else:
                stack.pop()
        else:
            stack.append(k)
            seen.add(k)
    if stack:
        problems.append("unclosed tags " + ",".join(f"x{k}" for k in stack))
    missing = set(seg.refs) - seen
    # Only descendants of the segment root count; nested refs are inside parents.
    if missing:
        problems.append("missing tags " + ",".join(f"x{k}" for k in sorted(missing)))
    return problems


def _append_text(parent, text: str) -> None:
    if not text:
        return
    if len(parent):
        last = parent[-1]
        last.tail = (last.tail or "") + text
    else:
        parent.text = (parent.text or "") + text


def close_unclosed(seg: Segment, translated: str) -> str:
    """The model sometimes drops a closing tag (``<x5>…`` with no ``</x5>``). Close such a tag where
    its original element must have ended: before the next tag that was not inside it, or at the end."""
    tokens = list(_TOKEN.finditer(translated))
    first_open: dict[int, int] = {}
    closed: set[int] = set()
    for t, m in enumerate(tokens):
        k = int(m.group(2))
        if m.group(1) == "/":
            closed.add(k)
        elif m.group(3) != "/":
            first_open.setdefault(k, t)
    never = [k for k in first_open if k not in closed and k in seg.refs and k not in seg.void_ids]
    if not never:
        return translated
    at: dict[int, list[int]] = {}
    for k in never:
        el = seg.refs[k]
        pos = len(translated)
        for m in tokens[first_open[k] + 1:]:
            other = seg.refs.get(int(m.group(2)))
            if other is not None and other is not el and not any(a is el for a in other.iterancestors()):
                pos = m.start()
                break
        at.setdefault(pos, []).append(k)
    out, last = [], 0
    for pos in sorted(at):
        # Innermost first when several close at the same spot.
        ks = sorted(at[pos], key=lambda k: -sum(1 for _ in seg.refs[k].iterancestors()))
        out.append(translated[last:pos] + "".join(f"</x{k}>" for k in ks))
        last = pos
    out.append(translated[last:])
    return "".join(out)


def sanitize_placeholders(seg: Segment, translated: str) -> str:
    """Drop wrapper tags that are unknown, duplicated, unclosed or misnested, so a model mistake can
    at worst lose a bit of inline formatting instead of e.g. italicising the rest of the paragraph.
    Void tags (<xN/>, images, line breaks) are always kept."""
    tokens = list(_TOKEN.finditer(translated))
    keep = [True] * len(tokens)
    stack: list[tuple[int, int]] = []  # (id, token index)
    opened: set[int] = set()
    for t, m in enumerate(tokens):
        closing, k, selfclose = m.group(1) == "/", int(m.group(2)), m.group(3) == "/"
        if k not in seg.refs:
            keep[t] = False
        elif selfclose or k in seg.void_ids:
            keep[t] = not closing
        elif not closing:
            if k in opened:
                keep[t] = False
            else:
                opened.add(k)
                stack.append((k, t))
        elif any(sk == k for sk, _ in stack):
            while stack[-1][0] != k:
                keep[stack.pop()[1]] = False
            stack.pop()
        else:
            keep[t] = False
    for _, t in stack:
        keep[t] = False
    out, pos = [], 0
    for t, m in enumerate(tokens):
        out.append(translated[pos:m.start()])
        if keep[t]:
            out.append(m.group(0))
        pos = m.end()
    out.append(translated[pos:])
    return "".join(out)


def apply_translation(seg: Segment, translated: str) -> list[str]:
    """Replace the segment's text with `translated`. Returns warnings (never raises on bad tags)."""
    warnings = check_placeholders(seg, translated)
    if warnings:
        repaired = close_unclosed(seg, translated)
        if repaired != translated and not check_placeholders(seg, repaired):
            warnings = [w + " (closing tag restored)" for w in warnings]
        translated = sanitize_placeholders(seg, repaired)
    el = seg.element
    tail = el.tail
    for child in list(el):
        el.remove(child)
    el.text = None
    el.tail = tail

    body = translated.strip()
    body = seg.lead_ws + body + seg.trail_ws

    stack = [el]
    used: set[int] = set()
    pos = 0
    for m in _TOKEN.finditer(body):
        _append_text(stack[-1], html.unescape(body[pos:m.start()]))
        pos = m.end()
        closing, k, selfclose = m.group(1) == "/", int(m.group(2)), m.group(3) == "/"
        orig = seg.refs.get(k)
        if orig is None:
            continue
        if closing:
            # Close up to the matching element if it is open; otherwise ignore.
            for i in range(len(stack) - 1, 0, -1):
                if stack[i].get("data-epublia-id") == str(k):
                    del stack[i:]
                    break
            continue
        if selfclose or k in seg.void_ids:
            node = copy.deepcopy(orig)
            node.tail = None
            stack[-1].append(node)
            used.add(k)
            continue
        node = etree.SubElement(stack[-1], orig.tag, attrib=dict(orig.attrib))
        node.set("data-epublia-id", str(k))
        stack.append(node)
        used.add(k)
    _append_text(stack[-1], html.unescape(body[pos:]))

    for node in el.iter():
        if _is_element(node) and "data-epublia-id" in node.attrib:
            del node.attrib["data-epublia-id"]

    # Never lose images, line breaks, anchors-with-ids etc.: re-append missing void refs.
    for k in sorted(seg.void_ids - used):
        orig = seg.refs[k]
        node = copy.deepcopy(orig)
        node.tail = None
        el.append(node)
    return warnings


# Bilingual output: blocks whose original can sit next to them as a sibling of the same kind.
_SIBLING_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "address", "center"}
# Containers that accept block content but must not be duplicated (lists, tables): original goes inside.
_FLOW_TAGS = {"li", "td", "th", "dd", "figcaption", "section", "article", "aside", "header", "footer", "nav"}
# Parents where loose text's original may go in a <div>.
_BLOCK_PARENTS = _FLOW_TAGS | {"body", "div", "blockquote", "center", "figure", "form", "fieldset", "main"}
# Media and embedded content are not repeated in the original-language copy.
_DROP_IN_COPY = {"img", "image", "svg", "video", "audio", "object", "embed", "iframe", "math", "picture"}
ORIGINAL_STYLE = "opacity:0.7"


def add_original(seg: Segment, source_text: str) -> None:
    """Bilingual output: show ``source_text`` (the segment's source placeholders) next to the already
    translated element. Paragraphs and headings get a sibling copy placed before them, loose text a
    <div> before it; list items and table cells get a <div> inside (so lists and tables keep their
    shape); inline-only containers get a <span> and a line break. The copy never repeats images, ids
    or links, so the book stays valid and its links keep one target."""
    el = seg.element
    ns = el.tag[1:].split("}", 1)[0] if el.tag.startswith("{") else None

    def q(name: str) -> str:
        return f"{{{ns}}}{name}" if ns else name

    tag = local(el.tag)
    parent = el.getparent()
    if tag == WRAPPER_TAG and (parent is None or local(parent.tag) not in _BLOCK_PARENTS):
        tag = "span"  # loose text inside an inline element (malformed source): stay inline
    if tag in _SIBLING_TAGS or tag == WRAPPER_TAG:
        # Loose text sits in a block container (e.g. <body>) that only accepts blocks: use a <div>.
        mode, holder = "sibling", etree.Element(el.tag if tag in _SIBLING_TAGS else q("div"))
    elif tag in _FLOW_TAGS:
        mode, holder = "inside", etree.Element(q("div"))
    else:
        mode, holder = "inline", etree.Element(q("span"))
    if tag in _SIBLING_TAGS:
        holder.attrib.update({k: v for k, v in el.attrib.items() if k != "id"})
    holder.set("style", ";".join(s for s in (holder.get("style", "").rstrip(";"), ORIGINAL_STYLE) if s))
    apply_translation(Segment(element=holder, text=source_text, refs=seg.refs, void_ids=seg.void_ids),
                      source_text)
    for node in list(holder.iter()):
        if node is holder or not _is_element(node):
            continue
        tag = local(node.tag)
        if tag in _DROP_IN_COPY:
            parent = node.getparent()
            prev = node.getprevious()
            if node.tail:
                if prev is not None:
                    prev.tail = (prev.tail or "") + node.tail
                else:
                    parent.text = (parent.text or "") + node.tail
            parent.remove(node)
            continue
        if tag == "a":
            keep = {k: v for k, v in node.attrib.items() if k in ("class", "style")}
            node.attrib.clear()
            node.attrib.update(keep)
            node.tag = q("span")
        node.attrib.pop("id", None)
    holder.tail = None
    if mode == "sibling":
        el.addprevious(holder)
        return
    holder.tail = el.text
    el.text = None
    el.insert(0, holder)
    if mode == "inline":
        br = etree.Element(q("br"))
        br.tail, holder.tail = holder.tail, None
        el.insert(1, br)


def unwrap_runs(tree: etree._ElementTree) -> None:
    """Remove the temporary <epublia-run> wrappers, splicing their content back into the parent."""
    for w in list(tree.getroot().iter()):
        if not _is_element(w) or local(w.tag) != WRAPPER_TAG:
            continue
        parent = w.getparent()
        idx = parent.index(w)
        prev = parent[idx - 1] if idx > 0 else None
        if w.text:
            if prev is None:
                parent.text = (parent.text or "") + w.text
            else:
                prev.tail = (prev.tail or "") + w.text
        children = list(w)
        tail = w.tail
        parent.remove(w)
        for offset, child in enumerate(children):
            parent.insert(idx + offset, child)
        if tail:
            if children:
                children[-1].tail = (children[-1].tail or "") + tail
            elif prev is None:
                parent.text = (parent.text or "") + tail
            else:
                prev.tail = (prev.tail or "") + tail


def plain_text(placeholder_text: str) -> str:
    """Placeholder text -> human-readable plain text."""
    return html.unescape(_TOKEN.sub("", placeholder_text)).strip()


def set_document_language(tree: etree._ElementTree, lang: str) -> None:
    root = tree.getroot()
    xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
    root.set(xml_lang, lang)
    if "lang" in root.attrib:
        root.set("lang", lang)
