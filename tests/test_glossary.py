from epublia.glossary import Glossary, find_candidates, parse
from epublia.translator import GeminiTranslator

RULES = """
# comment
Gray Tiger = Tigre Gris
Preserver = Preservador | Preservadora   # gender
Shelak = shelak
Les = Les
Xel'Naga = xel'naga
"""


def test_parse_alternatives_comments_and_override():
    entries = parse(RULES)
    assert entries["Preserver"].renderings == ["Preservador", "Preservadora"]
    assert "comment" not in entries
    g = Glossary(RULES, "Gray Tiger = Gray Tiger")
    assert g.entries["Gray Tiger"].renderings == ["Gray Tiger"]


def test_relevant_is_case_sensitive_whole_word():
    g = Glossary(RULES)
    assert [e.source for e in g.relevant("The Gray Tiger landed near the Shelak camp.")] == ["Gray Tiger", "Shelak"]
    assert g.relevant("a gray tiger, Shelaks, Lesley") == []
    assert "Tigre Gris" in g.prompt_block("aboard the Gray Tiger")


def test_violations():
    g = Glossary(RULES)
    assert g.violations("The Gray Tiger landed.", "El Tigre Gris aterrizó.") == []
    assert g.violations("The Gray Tiger landed.", "El Gray Tiger aterrizó.") == ["Gray Tiger: expected Tigre Gris"]
    # any alternative is fine
    assert g.violations("I am a Preserver.", "Soy una Preservadora.") == []
    # lowercase rendering: capitalised is fine at sentence start, flagged mid-sentence
    assert g.violations("Shelak were near.", "Shelak cerca. Los shelak llegaron.") == []
    assert g.violations("the Shelak came", "los Shelak llegaron") == ["Shelak: 'Shelak' should be 'shelak'"]
    assert g.violations("Shelak?", "—¿Shelak? ¡Shelak!") == []
    # capitalised rendering: an ordinary lowercase word must not be flagged ("les" pronoun)...
    assert g.violations("Les said so.", "Les dijo que les daría las llaves.") == []
    # ...but a lowercased name is, when the counts show it stands for the name
    g2 = Glossary("Ara = Ara")
    assert g2.violations("The Ara attacked.", "Los ara atacaron.") == ["Ara: 'ara' should be 'Ara'"]
    # straight and curly apostrophes are the same
    assert g.violations("the Xel’Naga", "los xel'naga") == []


def test_find_candidates_keeps_names_and_drops_sentence_starters():
    texts = [
        "Jake looked at Rosemary. Suddenly the Gray Tiger shook.",
        "“Well,” said Jake, “the Gray Tiger is late.” I’m sure Rosemary knows.",
        "Suddenly it was over. Well, almost. Rosemary’s ship, the Gray Tiger, left.",
    ]
    terms = {t for t, _, _ in find_candidates(texts)}
    assert {"Jake", "Rosemary", "Gray Tiger"} <= terms
    assert not terms & {"Suddenly", "Well", "I’m", "I", "The"}


def test_build_prompt_with_glossary_and_context():
    plain = GeminiTranslator.build_prompt([(1, "Hi")])
    assert plain == "[[1]] Hi"
    p = GeminiTranslator.build_prompt([(3, "Hi")], "Gray Tiger = Tigre Gris", [("Before.", "Antes.")])
    assert p.index("GLOSSARY:") < p.index("PREVIOUS PASSAGE") < p.index("SEGMENTS:\n[[3]] Hi")
    assert "[[" not in p.split("SEGMENTS:")[0]
    assert GeminiTranslator.parse_response("[[3]] Hola") == {3: "Hola"}
