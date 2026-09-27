# epublia

Translate EPUB books with Google Gemini (free tier friendly) while keeping everything but the
words intact: styles, images, cover, table of contents, ids, links and inline formatting
(italics, bold, line breaks). Only text nodes change; every other file in the book stays
byte-identical, and that is verified on every run.

- **Consistent names across the whole book**: an automatic glossary, the previous passage sent as
  context, a consistency report and `--fix-names` to repair only the segments that break it.
- **Series support**: books of the same series share one glossary.
- **Bilingual edition** on request (`--bilingual`): each paragraph in the original, then translated.
- **Robust on messy books**: PDF-conversion artefacts are cleaned before translating, very long
  paragraphs are sent in sentence-aligned parts, passages the model refuses are retried in halves
  and, optionally, sent to a backup model (any OpenAI-compatible API).
- **Comes out cleaner than it went in**: common validity errors of the source EPUB (images without
  `alt`, loose text in `<body>`, blocks inside inline elements, invalid NCX ids) are repaired
  without changing how the book looks.
- **Checks its own output**: untranslated or incomplete passages, broken inline tags, structural
  validation and, if Java is available, W3C EPUBCheck (only problems *added* by the translation are
  reported).
- **Free-tier aware**: client-side requests-per-minute and per-day limits, resumable per-chapter
  cache, clean stop when the daily quota runs out.

## Install

From PyPI:

```bash
pip install epublia
mkdir my-books && cd my-books
epublia --init            # creates .env, books-input/ and books-outputs/ here
```

From source:

```bash
git clone https://github.com/jrrmcalcio/epublia && cd epublia
python -m venv .venv
.venv\Scripts\pip install -e .          # Windows
# .venv/bin/pip install -e .            # Linux / macOS
copy .env.example .env                  # then fill in GEMINI_API_TOKEN
```

Get a free Gemini key at <https://aistudio.google.com/apikey>. To stay free, create it in a
Google Cloud project **without a billing account** (AI Studio → "Usage & Billing" must say
"Free tier"). Then check the key and model (one small test request; lists the Flash models your
key can use):

```bash
epublia --check
```

epublia looks for `.env`, `books-input/`, `books-outputs/` and `work/` in `EPUBLIA_HOME` if set,
otherwise in the source checkout (when running from one) or the current folder.

## Usage

Copy your `.epub` files into `books-input/` and run with the full name or just a word of it:

```bash
epublia firstborn                          # every EPUB whose name contains "firstborn"
epublia "Firstborn_-_Christie_Golden.epub" # exact name
epublia christie golden                    # all words must appear in the name
epublia C:\path\to\book.epub               # direct path
epublia firstborn --list                   # only show which books match
epublia firstborn --dry-run                # what would be translated, requests needed, name issues (no API)
epublia firstborn --glossary-only          # build the book's glossary and stop, to review it first
epublia firstborn --fix-names              # re-translate only segments that break the glossary
epublia firstborn --bilingual              # bilingual edition -> <name>_<LANG>_bilingual.epub
epublia firstborn --series StarCraft       # share the glossary with the other books of the series
epublia firstborn --lang FR                # another language without editing .env
epublia firstborn --extract-only           # only write the per-chapter TXT files (no API)
epublia firstborn --clean-only             # only remove PDF artefacts -> <name>_CLEAN.epub (no API)
epublia firstborn --no-clean               # translate without the PDF cleanup
epublia firstborn --no-cache               # translate everything again
epublia firstborn --no-repair              # keep the source's markup errors as they are
epublia --install-epubcheck                # download W3C EPUBCheck into tools/ (needs Java)
```

In a source checkout without installing, `epublia` is `.\epublia.bat` (PowerShell/cmd),
`./epublia.sh` (bash) or `python -m epublia`.

The translation is written to `books-outputs/<name>_<LANG>.epub`.

Exit codes: `0` ok, `1` a book failed, `2` configuration error, `3` daily quota exhausted
(progress is saved: run the same command again after the quota resets).

Everything is automatic: a plain `epublia <book>` builds the glossary (if missing), translates,
validates and writes the report in one run. The other options are for reviewing or repairing.

## How it works

1. Reads the EPUB (OPF, spine, manifest, NCX) and walks every XHTML document in reading order.
2. Splits each chapter into segments (paragraphs, headings, loose text). Inline markup becomes
   placeholders `<x1>…</x1>` / `<x2/>` so the model can move it along with the words.
3. **Cleans PDF-conversion artefacts** (see below) and logs every change in `work/<book>/cleanup.txt`.
4. Writes `work/<book>/source/NNN_chapter.txt` (with `[[n]]` markers) and `source-plain/` (clean text).
5. Builds the book's **glossary** the first time (see [Name consistency](#name-consistency)).
6. Sends each chapter to Gemini (grouping up to `MAX_CHARS_PER_REQUEST` characters per request)
   with the relevant glossary entries and the previous passage as context, throttled to
   `GEMINI_RPM` requests/minute, with backoff on 429/5xx. Very long paragraphs (common in converted
   PDFs) go out in sentence-aligned parts of up to `SEGMENT_SPLIT_CHARS`. If Gemini blocks or skips
   a segment it is retried in halves, and whatever still fails goes to the backup model if one is
   configured, so at most the offending passage stays untranslated.
7. Stores the translation in `work/<book>/<lang>/` (`translated/`, `translated-plain/` and a
   per-chapter cache), so a run can be **resumed** after a network error or an exhausted quota.
   Each language has its own cache and glossary.
8. Rebuilds the EPUB copying every non-text file byte for byte, sets `dc:language` and `xml:lang`,
   translates the table of contents, title and description, and gives the book a new identifier so
   reading apps don't confuse it with the original. If the model forgets to close an inline tag,
   it is closed where the original element ended.
9. Validates the result (well-formed XML, same images/links/tables, untouched resources, EPUBCheck
   when available) and writes `work/<book>/<lang>/report.json` with untranslated or **possibly
   incomplete** segments (much shorter than the source, or still containing source-language
   sentences), name issues and formatting warnings. Doubtful ones are also printed as `CHECK:`.

## Name consistency

Chapters are translated in separate requests, so without help a model may write `Tigre Gris` in
one chapter and `Gray Tiger` in the next. epublia prevents it in three ways:

1. **Automatic glossary.** Before translating, it finds the recurring proper names and invented
   terms in the book (locally, no API) and asks the model how to render each one, in 1–2 requests.
   The result is saved in `work/<book>/<lang>/glossary.txt`: edit it freely; it is never
   regenerated while it exists (delete it to build a new one). The raw candidates are in
   `work/<book>/glossary-candidates.txt`. Each request only carries the entries its text uses.
2. **Context.** Each request includes the end of the previous passage, already translated
   (`CONTEXT_CHARS`, 1500 by default; `0` disables it), to keep names, tone and forms of address.
3. **Review.** After every run, `work/<book>/<lang>/names.txt` (and `report.json`) lists the
   segments that break the glossary: a different rendering or inconsistent capitalisation
   (`los shelak` vs `los Shelak`). `--fix-names` re-translates only those segments; if the new
   version is worse (blocked, incomplete or with more issues), the old one is kept.

Glossary format (one rule per line, `#` for comments):

```
Gray Tiger = Tigre Gris
Preserver = Preservador | Preservadora    # alternatives for gender/number
Shelak = Shelak
```

For rules that apply to all your books, create a file (see `glossary.example.txt`) and point
`GLOSSARY_FILE` at it in `.env`: its entries override the automatic glossary.
`AUTO_GLOSSARY=false` turns the automatic glossary off.

Recommended flow if you want to review the glossary before spending quota on the translation:

```bash
epublia book --dry-run          # estimated cost
epublia book --glossary-only    # 1-2 requests; review work/<book>/<lang>/glossary.txt
epublia book                    # translate
```

For a book that is already translated: `--glossary-only`, review the glossary, then
`--dry-run --fix-names` to see the cost and `--fix-names` to apply it.

### Series

Books of the same series share a glossary in `work/series/<series>/<lang>/glossary.txt`. When a new
book of the series is translated, the terms already decided are copied into its glossary as they
are (and not sent to the model again); after the translation, the book's glossary is merged back
into the series one (the book's choices win, so your edits propagate to the next books).

The series comes from `--series NAME`, the `SERIES` setting, or the book's own metadata
(Calibre `calibre:series` or EPUB 3 `belongs-to-collection`). `--series none` disables it.

## Bilingual edition

`--bilingual` writes `books-outputs/<name>_<LANG>_bilingual.epub`, where every paragraph and
heading appears first in the original language (slightly faded) and then translated. In list
items, table cells and loose text, the original and the translation share the element, separated
by a line break, so lists and tables keep their shape. Images, links and ids are not repeated, so
the book stays valid and every link keeps a single target. It is only written on request and
reuses the same cache: making a bilingual copy of an already translated book costs no requests.

## Backup model

Gemini occasionally refuses a passage (`PROHIBITED_CONTENT`), which its safety settings cannot
switch off. epublia first retries it in smaller parts; what still fails can go to a backup model
through any OpenAI-compatible chat-completions API. It is used only for those passages, which
are listed under `translated_by_backup_model` in `report.json`.

```ini
FALLBACK_BASE_URL=https://openrouter.ai/api/v1
FALLBACK_API_KEY=sk-or-...
FALLBACK_MODEL=<model id>
FALLBACK_RPM=10        # client-side throttle
FALLBACK_RPD=0         # optional local daily cap
```

`epublia --check` sends one test request to the backup model too. Where to get a key (free tiers
and model names change often; check the provider's page):

| Provider | Key | Base URL | Cost |
|---|---|---|---|
| OpenRouter | <https://openrouter.ai/keys> | `https://openrouter.ai/api/v1` | Free models (ids ending in `:free`, list at <https://openrouter.ai/models?max_price=0>) with a small daily request limit |
| Groq | <https://console.groq.com/keys> | `https://api.groq.com/openai/v1` | Free tier with rate limits (open models such as Llama) |
| Mistral | <https://console.mistral.ai/api-keys> | `https://api.mistral.ai/v1` | Free "Experiment" plan with rate limits |
| DeepSeek | <https://platform.deepseek.com/api_keys> | `https://api.deepseek.com` | Paid, very cheap per token |
| Ollama (local) | none | `http://localhost:11434/v1` | Free, runs on your machine |

OpenAI's API has no free tier.

## Validation and EPUBCheck

Every output is checked for a valid ZIP layout (`mimetype` first and uncompressed), well-formed
XHTML, the same number of images, links, tables and SVGs as the original, and byte-identical
non-text resources.

If Java 17+ and [W3C EPUBCheck](https://github.com/w3c/epubcheck) are available, epublia also runs
EPUBCheck on both the original and the translation and reports only the problems the translation
added (many retail EPUBs already carry errors of their own). Setup:

1. Install Java, e.g. Eclipse Temurin from <https://adoptium.net> (Windows:
   `winget install EclipseAdoptium.Temurin.21.JRE`).
2. `epublia --install-epubcheck` downloads the latest EPUBCheck into `tools/`.

`EPUBCHECK_JAR` can point at another jar, or be `off` to skip the check.

## Repairing the source's errors

Many retail and converted EPUBs fail validation. epublia repairs the common cases in the
translated book (the original file is never touched), choosing fixes that render the same:

| Source error | Repair |
|---|---|
| `<img>` without `alt` | `alt=""` (what readers assume for a decorative image) |
| Text or inline elements directly in `<body>`/`<blockquote>` (EPUB 2 / XHTML 1.1) | Wrapped in a `<div>`, which is how readers already lay them out |
| Block element inside an inline one (`<span><p>…</p></span>`) or inside a `<p>` | The wrapper becomes a `<div>` with the same attributes |
| Empty `<body>` (EPUB 2) | Gets an empty `<div>` |
| NCX ids that are not valid XML names (`id="1"`) | Renamed (`np_1`); NCX ids are not referenced elsewhere |

Only markup changes, never text or resources. The repairs are counted in `report.json`
(`markup_repairs`), and when EPUBCheck runs it also reports how many of the original's problems
are gone. Disable with `--no-repair` or `REPAIR=false`.

## PDF artefact cleanup

Many EPUBs are converted PDFs. Before translating, epublia removes or fixes:

| Problem | Example | Action |
|---|---|---|
| Running headers/footers | `2 CHRISTIE GOLDEN`, `FIRSTBORN 3` | Removed (title/author + number, or a numbered line repeated ≥3 times) |
| Repeated all-caps headers | `CHAPTER ONE` on every page | Removed if seen ≥5 times mid-paragraph |
| Stray page numbers | `12`, `- 12 -`, `[12]`, `Page 12`, `12 of 300`, `xii` | Removed (never in `<h1>`… headings) |
| Watermarks | `OceanofPDF.com`, "Scanned by…", "This page intentionally left blank" | Removed |
| Lines broken mid-sentence | `had done ⏎ to get inside` | Joined |
| Split paragraphs | `</p><p>` followed by lowercase | Merged |
| Words hyphenated at line end | `some- ⏎ thing`, `sepa- rate` | Joined (keeps `pre- and post-war`) |
| Ligatures and invisible characters | `ﬁ ﬂ ﬀ`, soft hyphen, zero-width | Normalised |
| Letter-spaced words | `C H A P T E R` | Joined |
| Space before punctuation | `word ,` | Fixed (keeps `. . .`) |
| Embedded PDF footnotes | `1 See the appendix…` mid-paragraph | **Only reported** in `cleanup.txt`: they cannot be told apart from story text reliably |

Real EPUB footnotes (links/`noteref`) are kept and translated. Review `work/<book>/cleanup.txt`,
and use `--clean-only` to see the result without spending quota. Disable with `--no-clean` or
`CLEANUP=false`.

## Gemini free tier

- Limits (requests per minute and per day) depend on the model and account; see
  <https://aistudio.google.com/rate-limit> and set `GEMINI_RPM` accordingly.
- When the **daily** quota runs out, epublia stops cleanly (exit code 3) and continues where it
  left off on the next run.
- To never pay, create the key in a project **without a billing account**. With billing enabled,
  Google charges per use. The API cannot tell which one you have.
- On the free tier Google may use the submitted content to improve its products.

## Configuration

Settings are read from `.env` (see `.env.example` for a commented template):

| Setting | Default | Meaning |
|---|---|---|
| `GEMINI_API_TOKEN` | — | Gemini API key (required) |
| `TARGET_LANGUAGE` | `ES` | ISO code (`ES`, `FR`, `pt-br`…) or language name |
| `GEMINI_MODEL` | `gemini-3.8-flash` | Model id; `epublia --check` lists yours |
| `GEMINI_RPM` / `GEMINI_RPD` | `10` / `0` | Client-side requests per minute / per day (0 = no local daily cap) |
| `MAX_CHARS_PER_REQUEST` | `24000` | Source characters per request |
| `GEMINI_TEMPERATURE` | `0.3` | Sampling temperature |
| `GEMINI_THINKING_LEVEL` | model default | `minimal`, `low`, `medium` or `high` |
| `CLEANUP` | `true` | PDF artefact cleanup |
| `GLOSSARY_FILE` | — | Global glossary that overrides the automatic one |
| `AUTO_GLOSSARY` | `true` | Build the per-book glossary with the model |
| `CONTEXT_CHARS` | `1500` | Previous translated text sent as context (0 = off) |
| `SEGMENT_SPLIT_CHARS` | `6000` | Longer paragraphs are sent in sentence-aligned parts |
| `SERIES` | book metadata | Series name for the shared glossary (`none` = off) |
| `FALLBACK_BASE_URL` / `FALLBACK_API_KEY` / `FALLBACK_MODEL` | — | Backup model (OpenAI-compatible API) |
| `FALLBACK_RPM` / `FALLBACK_RPD` | `10` / `0` | Backup model throttling |
| `EPUBCHECK_JAR` | auto-detect | EPUBCheck jar path, or `off` |
| `REPAIR` | `true` | Repair validity errors the source EPUB already had |
| `INPUT_DIR` / `OUTPUT_DIR` / `WORK_DIR` | `books-input` / `books-outputs` / `work` | Folders, relative to `EPUBLIA_HOME` |

## Limitations

- DRM-protected EPUBs cannot be translated (detected and reported).
- Text inside images, SVG, `<pre>`/`<code>` and descriptions containing HTML is not translated.
- The PDF cleanup is heuristic and conservative: it would rather leave an artefact than delete
  story text.
- Only translate books you are allowed to; `books-input/`, `books-outputs/` and `work/` are
  git-ignored for that reason.

## Development

```bash
pip install -e ".[dev]"
pytest -q
```

### Publishing

Releases are published to PyPI by `.github/workflows/publish.yml` using PyPI trusted publishing
(no token stored anywhere):

1. Once, on <https://pypi.org/manage/account/publishing/>, add a pending publisher: project
   `epublia`, owner `jrrmcalcio`, repository `epublia`, workflow `publish.yml`, environment `pypi`.
2. Bump `__version__` in `epublia/__init__.py`, commit, then `git tag v0.3.0 && git push --tags`.

## License

MIT
