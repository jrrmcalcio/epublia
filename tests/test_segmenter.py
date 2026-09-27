import re

from lxml import etree

from epublia.cli import find_books
from epublia.segmenter import apply_translation, extract_segments, parse_xhtml, serialize_xhtml, unwrap_runs
from epublia.translator import GeminiTranslator

DOC = b"""<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head>
<body class="c"><h1 id="h">Chapter 1</h1> LOOSE TEXT here. <p class="p">Hello <span class="italic">brave</span> world.<br/>Next&nbsp;line <img src="images/a.jpg"/> end.</p>
<span class="s">Inline <p>nested block</p> tail text</span><pre>keep  me</pre></body></html>"""


def _text(tree):
    return re.sub(r"\s+", " ", "".join(tree.getroot().itertext())).strip()


def test_identity_roundtrip_preserves_everything():
    tree, recovered = parse_xhtml(DOC)
    assert not recovered
    before = _text(tree)
    segs = extract_segments(tree)
    for s in segs:
        assert apply_translation(s, s.text) == []
    unwrap_runs(tree)
    out = etree.fromstring(serialize_xhtml(tree))
    assert _text(etree.ElementTree(out)) == before
    assert len(out.findall(".//{*}img")) == 1
    assert len(out.findall(".//{*}br")) == 1
    assert b"epublia" not in serialize_xhtml(tree)


def test_segments_and_placeholders():
    tree, _ = parse_xhtml(DOC)
    texts = [s.text for s in extract_segments(tree)]
    assert "Chapter 1" in texts
    assert "LOOSE TEXT here." in texts
    assert any(t.startswith("Hello <x1>brave</x1> world.<x2/>") for t in texts)
    assert not any("keep" in t for t in texts)  # <pre> is never translated


def test_translation_moves_inline_tags_and_keeps_attributes():
    tree, _ = parse_xhtml(DOC)
    seg = next(s for s in extract_segments(tree) if s.text.startswith("Hello"))
    assert apply_translation(seg, "Hola mundo <x1>valiente</x1>.<x2/>Siguiente línea <x3/> fin.") == []
    unwrap_runs(tree)
    xml = serialize_xhtml(tree).decode()
    assert '<p class="p">Hola mundo <span class="italic">valiente</span>.<br/>' in xml
    assert '<img src="images/a.jpg"/>' in xml


def test_broken_tags_do_not_lose_images():
    tree, _ = parse_xhtml(DOC)
    seg = next(s for s in extract_segments(tree) if s.text.startswith("Hello"))
    warnings = apply_translation(seg, "Hola <x1>valiente mundo. Siguiente línea fin.")
    assert warnings
    unwrap_runs(tree)
    out = etree.fromstring(serialize_xhtml(tree))
    assert len(out.findall(".//{*}img")) == 1


def test_parse_response_handles_wrapped_lines_and_fences():
    text = "```\n[[1]] Uno\n[[2]] Dos\ncontinúa\n[[3]] Tres\n```"
    assert GeminiTranslator.parse_response(text) == {1: "Uno", 2: "Dos continúa", 3: "Tres"}


def test_find_books_by_word(tmp_path):
    for name in ("Firstborn_-_Christie_Golden.epub", "Shadow_Hunters.epub", "notes.txt"):
        (tmp_path / name).write_bytes(b"x")
    assert [p.name for p in find_books("firstborn", tmp_path)] == ["Firstborn_-_Christie_Golden.epub"]
    assert [p.name for p in find_books("christie golden", tmp_path)] == ["Firstborn_-_Christie_Golden.epub"]
    assert find_books("zzz", tmp_path) == []


def test_unclosed_wrapper_does_not_swallow_rest_of_paragraph():
    tree, _ = parse_xhtml(DOC)
    seg = next(s for s in extract_segments(tree) if s.text.startswith("Hello"))
    apply_translation(seg, "Hola <x1>valiente mundo.<x2/>Siguiente</x9> línea <x3/> fin.")
    unwrap_runs(tree)
    xml = serialize_xhtml(tree).decode()
    assert 'class="italic"' not in xml  # unbalanced wrapper dropped, text kept
    assert "Hola valiente mundo.<br/>Siguiente línea <img" in xml


# ----------------------------------------------------------------------------- cleanup

from epublia.cleanup import Cleaner  # noqa: E402

PDF_DOC = """<?xml version='1.0' encoding='utf-8'?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head><body>
<p class="p">It was a mere drop in <br class="b"/> 2 <span class="h">JANE DOE </span></p>
<p class="p">the vast ocean. He had done <br class="b"/> to get inside. He did not care about the <br class="b"/> Marines at all, that's how I <br class="b"/> wanted it.<br class="b"/>- 14 -<br class="b"/>Then it
ended. The ﬁnal thing was sepa- rate, but pre- and post-war stayed. OceanofPDF.com<br class="b"/>Wait . . . what?</p>
<h1>12</h1><p>Contents<br/>The First Part<br/>the second part</p></body></html>"""


def _cleaned(doc=PDF_DOC):
    tree, _ = parse_xhtml(doc.encode())
    segs = extract_segments(tree)
    cl = Cleaner("Some Title", "Jane Doe")
    cl.learn(segs)
    segs = cl.clean_document(segs, "doc")
    for s in segs:
        apply_translation(s, s.text)
    unwrap_runs(tree)
    return serialize_xhtml(tree).decode(), cl.log


def test_cleanup_removes_pdf_artefacts():
    xml, log = _cleaned()
    text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", xml))
    assert "JANE DOE" not in xml and "- 14 -" not in xml and "OceanofPDF" not in xml
    assert "a mere drop in the vast ocean" in text          # split paragraph merged
    assert "had done to get inside" in text                  # broken line joined
    assert "about the Marines" in text and "how I wanted" in text
    assert 'class="h"' not in xml                            # header wrapper removed, not left empty
    assert "final thing was separate" in text                # ligature + hyphenation
    assert "pre- and post-war" in text                       # real suspended hyphen kept
    assert "Wait . . . what?" in text                        # spaced ellipsis untouched
    assert "<h1>12</h1>" in xml                              # numeric headings kept
    assert "Contents<br/>The First Part" in xml              # short lines never glued
    assert log.headers == 1 and log.page_numbers == 1 and log.watermarks == 1
    assert log.paragraph_merges == 1


def test_page_number_variants():
    from epublia.cleanup import _PAGE_NO
    for ok in ("12", "- 12 -", "[12]", "Page 12", "p. 12", "Página 7", "12 of 300", "12 / 300", "xii", "iv"):
        assert _PAGE_NO.match(ok), ok
    for no in ("I", "mix", "12 monkeys", "Chapter 12", "civil"):
        assert not _PAGE_NO.match(no), no
