import re

from lxml import etree

from epublia.repair import repair_document, repair_ncx
from epublia.segmenter import parse_xhtml, serialize_xhtml

# Mirrors the Firstborn credits page: inline content straight in <body>, <p> inside <span>, no alt.
DOC = (b'<html xmlns="http://www.w3.org/1999/xhtml"><head><title>T</title></head><body class="c">'
       b'<img src="a.jpg" class="i"/><span class="s">POCKET <span class="b">STAR</span> BOOKS <br/>'
       b'<span class="t">An Original <p class="p">A Pocket Star Book</p></span></span>'
       b'<p class="p">Real paragraph.</p> Loose tail text <br/> more. <p>End.</p></body></html>')


def _text(root):
    return re.sub(r"\s+", " ", "".join(root.itertext())).strip()


def test_repair_fixes_source_errors_without_changing_text():
    tree, _ = parse_xhtml(DOC)
    before = _text(tree.getroot())
    fixes = repair_document(tree, "2.0")
    root = etree.fromstring(serialize_xhtml(tree))
    assert _text(root) == before  # not a single visible character changes
    body = root.find("{*}body")
    assert all(child.tag.split("}")[1] in ("div", "p") for child in body)  # only blocks in <body>
    assert root.find(".//{*}img").get("alt") == ""
    assert not root.findall(".//{*}span/{*}p")  # no block inside an inline element
    assert root.find(".//{*}div[@class='s']") is not None  # the wrapper kept its class
    assert "img alt added" in fixes and any("wrapped" in f for f in fixes)
    assert len(root.findall(".//{*}img")) == 1 and len(root.findall(".//{*}br")) == 2


def test_repair_leaves_valid_documents_alone():
    doc = (b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A <i>b</i>.</p>'
           b'<div>Inline text in a div is fine <img src="x.jpg" alt="x"/></div></body></html>')
    tree, _ = parse_xhtml(doc)
    before = serialize_xhtml(tree)
    assert repair_document(tree, "2.0") == []
    assert serialize_xhtml(tree) == before


def test_epub3_keeps_inline_content_in_body():
    tree, _ = parse_xhtml(b'<html xmlns="http://www.w3.org/1999/xhtml"><body>Hi <b>there</b></body></html>')
    assert repair_document(tree, "3.0") == []  # HTML5 allows phrasing content in <body>


def test_repair_ncx_ids():
    ncx = etree.fromstring(b'<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/"><navMap>'
                           b'<navPoint id="1" playOrder="1"/><navPoint id="ok_2" playOrder="2"/>'
                           b'<navPoint id="a b" playOrder="3"/></navMap></ncx>')
    assert repair_ncx(ncx) == ["invalid NCX id renamed"] * 2
    ids = [n.get("id") for n in ncx.iter("{*}navPoint")]
    assert ids == ["np_1", "ok_2", "np_a_b"]
