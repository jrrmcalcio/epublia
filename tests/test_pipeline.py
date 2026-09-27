import json
import re
from pathlib import Path

from epublia.config import Config
from epublia.glossary import Glossary
from epublia.pipeline import BookTranslator, SegmentCache, Stats, _completeness_problem, _migrate_legacy_layout
from epublia.segmenter import check_placeholders, extract_segments, parse_xhtml
from epublia.translator import BlockedError, GeminiTranslator, split_text


def _cfg(tmp_path: Path, **kw) -> Config:
    base = dict(api_key="k", model="m", target_code="es", target_name="Spanish", rpm=60, rpd=0,
                max_chars=24000, temperature=0.3, thinking_level="", input_dir=tmp_path,
                output_dir=tmp_path, work_dir=tmp_path, glossary="")
    base.update(kw)
    return Config(**base)


class FakeTranslator(GeminiTranslator):
    """Echoes each segment as 'ES(<text>)'; can be told to block prompts containing a word."""

    def __init__(self, cfg, block_word=None):
        self.cfg, self.block_word, self.prompts, self.requests_made = cfg, block_word, [], 0
        self.system_prompt = "sys"
        self.fallback, self.fallback_used = None, []

    def call(self, prompt, system=None):
        self.prompts.append(prompt)
        self.requests_made += 1
        segments = prompt.split("SEGMENTS:\n")[-1]
        if self.block_word and self.block_word in segments:
            raise BlockedError("finish_reason=PROHIBITED_CONTENT")
        return "\n".join(f"[[{i}]] ES({t})" for i, t in re.findall(r"^\[\[(\d+)\]\] (.*)$", segments, re.M))


LONG = " ".join(f"Sentence number {n} has <x1>some</x1> words in it." for n in range(1, 400))


def test_split_text_cuts_at_sentences_outside_tags():
    text = "One <x1>two. Three</x1> four. Five six. " * 60
    parts = split_text(text.strip(), 300)
    assert len(parts) > 1 and all(len(p) <= 400 for p in parts)
    assert " ".join(parts).split() == text.split()
    for p in parts:  # every piece keeps its tag pairs balanced
        assert p.count("<x1>") == p.count("</x1>")
    assert split_text("short", 300) == ["short"]


def test_blocked_long_segment_is_retried_in_parts(tmp_path):
    cfg = _cfg(tmp_path)
    text = LONG.replace("number 350 ", "number 350 FORBIDDEN ")
    got = FakeTranslator(cfg, block_word="FORBIDDEN").translate_items([(1, text)])[1]
    # Only the part holding the blocked sentence stays in English; the rest is translated.
    assert got.count("ES(") > 3
    assert "FORBIDDEN" in got and len(got) > len(text)


def test_long_segments_are_sent_in_parts_and_cached_whole(tmp_path):
    cfg = _cfg(tmp_path, segment_chars=2000, context_chars=0)
    tree, _ = parse_xhtml(f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>Short one.</p>'
                          f'<p>{LONG.replace("<x1>", "<i>").replace("</x1>", "</i>")}</p></body></html>'.encode())
    segs = extract_segments(tree)
    fake = FakeTranslator(cfg)
    bt = BookTranslator(cfg, fake)
    cache = SegmentCache(tmp_path / "c.json")
    out = bt._translate_segments(segs, cache, "doc", Stats())
    assert out[1] == "ES(Short one.)"
    assert out[2].count("ES(") > 5 and not check_placeholders(segs[1], out[2])
    assert cache.get(segs[1].text) == out[2]  # cache keyed by the whole segment


def test_prompt_carries_relevant_glossary_and_previous_context(tmp_path):
    cfg = _cfg(tmp_path, context_chars=1500)
    tree, _ = parse_xhtml(b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                          b'<p>Jake boarded.</p><p>The Gray Tiger left.</p></body></html>')
    segs = extract_segments(tree)
    fake = FakeTranslator(cfg)
    bt = BookTranslator(cfg, fake)
    bt.glossary = Glossary("Gray Tiger = Tigre Gris\nAiur = Aiur")
    cache = SegmentCache(tmp_path / "c.json")
    cache.put_many([(segs[0].text, "Jake subió.")])
    bt._translate_segments(segs, cache, "doc", Stats())
    prompt = fake.prompts[0]
    assert "Gray Tiger = Tigre Gris" in prompt and "Aiur" not in prompt
    assert "SOURCE: Jake boarded.\nTRANSLATION: Jake subió." in prompt


def test_fix_names_keeps_old_translation_when_retry_is_blocked(tmp_path):
    cfg = _cfg(tmp_path, context_chars=0)
    tree, _ = parse_xhtml(b'<html xmlns="http://www.w3.org/1999/xhtml"><body>'
                          b'<p>The Gray Tiger left FORBIDDEN.</p></body></html>')
    segs = extract_segments(tree)
    bt = BookTranslator(cfg, FakeTranslator(cfg, block_word="FORBIDDEN"), fix_names=True)
    bt.glossary = Glossary("Gray Tiger = Tigre Gris")
    cache = SegmentCache(tmp_path / "c.json")
    cache.put_many([(segs[0].text, "El Gray Tiger se fue.")])
    out = bt._translate_segments(segs, cache, "doc", Stats())
    assert out[1] == "El Gray Tiger se fue."  # not replaced by the untranslated source
    assert cache.get(segs[0].text) == "El Gray Tiger se fue."


def test_completeness_problem():
    src = "He walked into the room and looked at the old table for a long time. " * 8
    assert _completeness_problem(src, "Entró en la habitación y miró la vieja mesa durante mucho rato. " * 8) is None
    assert "dropped" in _completeness_problem(src, "Entró en la habitación.")
    half = "Entró en la habitación y miró la vieja mesa durante mucho rato. " * 4 + src[: len(src) // 2]
    assert "source language" in _completeness_problem(src, half)
    assert _completeness_problem("Short.", "") is None


def test_legacy_work_dir_is_moved_under_its_language(tmp_path):
    book = tmp_path / "Book"
    (book / "cache").mkdir(parents=True)
    (book / "cache" / "001.json").write_text("{}", encoding="utf-8")
    (book / "glossary.txt").write_text("A = B", encoding="utf-8")
    (book / "report.json").write_text(json.dumps({"target_language": "es"}), encoding="utf-8")
    (book / "source").mkdir()
    _migrate_legacy_layout(book, "fr")  # running in French must not adopt the Spanish cache
    assert (book / "es" / "cache" / "001.json").exists() and (book / "es" / "glossary.txt").exists()
    assert not (book / "cache").exists() and (book / "source").exists()
    assert not (book / "fr").exists()
