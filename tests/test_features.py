import zipfile

from lxml import etree

from epublia import epubcheck
from epublia.epub import EpubPackage, write_epub
from epublia.fallback import FallbackError
from epublia.glossary import Glossary, merge
from epublia.segmenter import add_original, apply_translation, extract_segments, parse_xhtml, serialize_xhtml, unwrap_runs
from test_pipeline import FakeTranslator, _cfg

DOC = b"""<html xmlns="http://www.w3.org/1999/xhtml"><body>
<h1 id="c1">Chapter One</h1>
<p id="p1" class="t">He saw <a href="#n1" id="r1">the ship</a> <img src="i.jpg" alt=""/> leave.</p>
<ul><li>First item</li></ul>
<table><tr><td>Cell text</td></tr></table>
</body></html>"""


def _bilingual(doc=DOC):
    tree, _ = parse_xhtml(doc)
    segs = extract_segments(tree)
    for seg in segs:
        src = seg.text
        apply_translation(seg, "ES " + src)
        add_original(seg, src)
    unwrap_runs(tree)
    return etree.fromstring(serialize_xhtml(tree)), serialize_xhtml(tree).decode()


def test_bilingual_paragraph_gets_sibling_original_without_duplicates():
    root, xml = _bilingual()
    ps = root.findall(".//{*}p")
    assert len(ps) == 2
    orig, trans = ps
    assert orig.get("style") == "opacity:0.7" and orig.get("class") == "t" and orig.get("id") is None
    assert trans.get("id") == "p1" and "ES He saw" in "".join(trans.itertext())
    assert "He saw the ship" in " ".join("".join(orig.itertext()).split())
    # images, links and ids are not repeated
    assert len(root.findall(".//{*}img")) == 1 and len(root.findall(".//{*}a")) == 1
    assert xml.count('id="r1"') == 1 and xml.count('id="c1"') == 1
    assert len(root.findall(".//{*}h1")) == 2


def test_bilingual_list_items_and_cells_stay_single():
    root, _ = _bilingual()
    assert len(root.findall(".//{*}li")) == 1 and len(root.findall(".//{*}td")) == 1
    li = root.find(".//{*}li")
    assert li[0].tag.endswith("div") and len(li) == 1
    assert "".join(li.itertext()) == "First itemES First item"


def test_bilingual_loose_text_in_body_gets_a_block_not_inline_elements():
    root, _ = _bilingual(b'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>A para.</p> Loose line '
                         b'<br/> more. <p>B.</p></body></html>')
    body = root.find(".//{*}body")
    assert [c.tag.split("}")[1] for c in body] == ["p", "p", "div", "br", "p", "p"]
    assert body[2].get("style") == "opacity:0.7" and "Loose line" in "".join(body[2].itertext())
    assert "ES Loose line" in (body[2].tail or "")


def test_fallback_translates_the_passage_gemini_blocks(tmp_path):
    cfg = _cfg(tmp_path)

    class Backup:
        requests_made = 0
        last_model = "m1"

        def call(self, prompt, system):
            return "[[1]] RESPALDO"

    fake = FakeTranslator(cfg, block_word="FORBIDDEN")
    fake.fallback = Backup()
    assert fake.translate_items([(1, "A short FORBIDDEN line.")]) == {1: "RESPALDO"}
    assert fake.fallback_used == ["[m1] A short FORBIDDEN line."]

    class Broken:
        def call(self, prompt, system):
            raise FallbackError("HTTP 401")

    fake.fallback = Broken()
    assert fake.translate_items([(1, "A short FORBIDDEN line.")]) == {1: "A short FORBIDDEN line."}


def test_series_merge_book_wins_and_counts():
    text, added, changed = merge("A = a\nB = b", "B = bb\nC = c")
    assert Glossary(text).entries["B"].renderings == ["bb"]
    assert (added, changed) == (1, 1)


def _epub(tmp_path, opf_meta: str):
    path = tmp_path / "b.epub"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                   '<rootfiles><rootfile full-path="c.opf"/></rootfiles></container>')
        z.writestr("c.opf", f'<package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
                   f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Firstborn</dc:title>'
                   f'<dc:identifier id="id">x</dc:identifier><dc:language>en</dc:language>{opf_meta}</metadata>'
                   f'<manifest/><spine/></package>')
    return path


def test_series_metadata_and_translated_title(tmp_path):
    book = EpubPackage(_epub(tmp_path, '<meta name="calibre:series" content="StarCraft: Dark Templar"/>'))
    assert book.series == "StarCraft: Dark Templar"
    opf, *_ = book.translated_opf("es", "Spanish", "m", title="Primogénito")
    assert b"<dc:title>Primog\xc3\xa9nito</dc:title>" in opf
    assert EpubPackage(_epub(tmp_path, "")).series == ""


def test_epubcheck_reports_only_new_problems(tmp_path, monkeypatch):
    old = [{"severity": "ERROR", "id": "RSC-005", "path": "a.html", "line": 7, "message": "img lacks alt"}]
    new = old[:1] + [{"severity": "ERROR", "id": "RSC-012", "path": "b.html", "line": 3, "message": "bad link"},
                     {"severity": "WARNING", "id": "OPF-085", "path": "c.opf", "line": 1, "message": "uuid"}]
    new[0] = dict(new[0], path="z.html", line=9)  # same problem reported under another file: not new
    monkeypatch.setattr(epubcheck, "run", lambda cmd, p: new if p.name == "out.epub" else old)
    got = epubcheck.new_problems(["x"], tmp_path / "in.epub", tmp_path / "out.epub")
    assert got["new_errors"] == ["ERROR RSC-012 b.html:3 bad link"]
    assert got["new_warnings"] == ["WARNING OPF-085 c.opf:1 uuid"]
    assert epubcheck.find_command("off", tmp_path) is None


def test_home_is_cwd_when_installed(tmp_path, monkeypatch):
    import epublia.config as config

    monkeypatch.setenv("EPUBLIA_HOME", str(tmp_path))
    assert config._home() == tmp_path.resolve()
    monkeypatch.delenv("EPUBLIA_HOME")
    assert config._home().name  # source checkout (pyproject.toml present) or cwd


def test_write_epub_roundtrip_keeps_mimetype_first(tmp_path):
    src = _epub(tmp_path, "")
    out = tmp_path / "o.epub"
    write_epub(zipfile.ZipFile(src), out, {})
    assert zipfile.ZipFile(out).infolist()[0].filename == "mimetype"


def test_bilingual_loose_text_inside_inline_element_stays_inline():
    root, _ = _bilingual(b'<html xmlns="http://www.w3.org/1999/xhtml"><body><span class="c">Loose '
                         b'<p>Para.</p></span></body></html>')
    span = root.find(".//{*}body/{*}span")
    assert span.find("{*}div") is None and span[0].tag.endswith("span") and span[1].tag.endswith("br")


def test_epubcheck_ignores_context_dependent_expected_tail(tmp_path, monkeypatch):
    old = [{"severity": "ERROR", "id": "RSC-005", "path": "a", "line": 1,
            "message": 'element "img" not allowed here; expected element "p"'}]
    new = [dict(old[0], message='element "img" not allowed here; expected the element end-tag or element "p"')]
    monkeypatch.setattr(epubcheck, "run", lambda cmd, p: new if p.name == "out.epub" else old)
    assert epubcheck.new_problems(["x"], tmp_path / "in.epub", tmp_path / "out.epub")["new_errors"] == []


def test_backup_model_list_skips_busy_and_retired_models(tmp_path, monkeypatch):
    import httpx

    from epublia.fallback import OpenAICompatClient
    from epublia.rate_limiter import RateLimiter

    seen = []

    def handler(request):
        model = __import__("json").loads(request.content)["model"]
        seen.append(model)
        if model == "busy":
            return httpx.Response(429)
        if model == "gone":
            return httpx.Response(404, text="no such model")
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop",
                                                      "message": {"content": "<think>hmm</think>[[1]] Hola"}}]})

    client = OpenAICompatClient("https://x/api/v1", "k", "busy, gone, good", RateLimiter(1000),
                                transport=httpx.MockTransport(handler))
    assert client.call("[[1]] Hi", "sys") == "[[1]] Hola"
    assert seen == ["busy", "gone", "good"] and client.last_model == "good"

    refusing = OpenAICompatClient("https://x/api/v1", "k", "busy", RateLimiter(1000),
                                  transport=httpx.MockTransport(handler))
    import epublia.fallback as fb
    monkeypatch.setattr(fb.time, "sleep", lambda s: None)  # no real waiting in tests
    try:
        refusing.call("x", "sys", attempts=2)
    except FallbackError as exc:
        assert "429" in str(exc)
    else:
        raise AssertionError("expected FallbackError")
