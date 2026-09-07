# Accessibility & Performance Fix Assistant

An LLM assistant that sits between an automated scan result and the developer who has to act
on it. It scans a page, explains each finding in plain language grounded in the actual WCAG
text, proposes a fix written against that page's markup, and verifies its own fix in a real
browser before you read it.

The design write-up is **[DESIGN.md](DESIGN.md)** ([PDF](DESIGN.pdf)): what I decided, what
I deliberately did not build, and why, for each layer. This file is setup and commands only.

Layers tackled: Layer 1 (Retrieval & Grounding), Layer 2 (Tool Use & Agentic), plus the
Context Engineering stretch.

---

## Setup

Requires **Python 3.11+** and **Node 20+**.

```bash
# 1. Node scanner (Playwright + axe-core)
cd scanner && npm install && npx playwright install chromium && cd ..

# 2. Python virtual environment
python -m venv .venv
.venv\Scripts\activate                # Windows
# source .venv/bin/activate           # macOS / Linux

# 3. CPU-only torch FIRST, otherwise pip resolves the CUDA wheel as a dependency
#    of sentence-transformers. Built and evaluated against 2.14.0+cpu.
pip install torch==2.14.0 --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt

# 4. API key
cp .env.example .env        # then edit .env and add your key
```

### Environment variables

Only `ANTHROPIC_API_KEY` is required.

```
ANTHROPIC_API_KEY=sk-ant-...

# Optional overrides
# ASSISTANT_MODEL=claude-haiku-4-5     # default for issues with a WCAG anchor
# UNANCHORED_MODEL=claude-sonnet-5     # default for LCP, which has no anchor
# VERIFIER_MODEL=claude-haiku-4-5      # citation support check and eval judge
# EMBED_MODEL=BAAI/bge-small-en-v1.5
# MAX_OUTPUT_TOKENS=1600
# MLFLOW_EXPERIMENT=a11y-assistant
```

The grounding corpus and its embeddings are committed, so there is nothing to download and
the evals run offline. To rebuild them from source:

```bash
python scripts/build_corpus.py   # fetches W3C sources, caches under corpus_data/raw/
python scripts/embed_corpus.py   # ~30s on CPU
```

## Running the app

```bash
uvicorn app.main:app --reload
# http://127.0.0.1:8000
```

Scanning and the Context Inspector work without an API key. Only the answer itself needs one.

Regenerate the design PDF from `DESIGN.md` (renders Mermaid via the Playwright chromium the
scanner already installed):

```bash
node scripts/md2pdf.mjs DESIGN.md DESIGN.pdf
```

## The interesting paths

### 1. The context recipe that changes the answer (start here)

Scan `gov-homepage.html`, open the **`color-contrast`** issue, then open the **Prompt** tab in
the Context Inspector.

The failing `<p class="muted">` has `background-color: rgba(0,0,0,0)`, i.e. transparent. The
colour a user actually sees is painted by `<section class="news">` three levels up. The recipe
walks up to find it and computes **3.08:1**. An assistant that assumes a white page background
computes **3.45:1** and recommends a colour that still fails.

That single number is the whole argument for issue-type-specific context extraction.

### 2. The assistant verifying its own fix

On any issue, ask a follow-up:

> Validate that fix for me.

Watch the **Tools** tab. `validate_fix` applies the model's proposed markup to the real page in
a headless browser and re-runs axe, reporting both whether the violation cleared **and whether
the patch introduced anything new**.

### 3. Citation verification catching a fabrication

Every criterion the model cites is checked twice: was it in the supplied context, and does that
text actually support the claim. Badges appear above the answer. To see it fire, ask something
that invites over-citation:

> Which other WCAG criteria does this element violate?

### 4. Turn 0 makes no tool calls

The first answer streams immediately, because the recipe pre-fetched everything. Compare the
**Tools** tab on turn 1 (empty, by design) with a follow-up. The `first token Nms` readout is
in the top right of the answer pane.

### 5. Prompt caching, measured not claimed

**Prompt** tab, on any turn after the first: `cache_read_tokens` should be non-zero, showing the
page context being re-read from cache rather than re-billed.

### 6. The failure contract

Edit `fixtures/gov-homepage.html` (delete the news `<figure>`) while a conversation is open,
then ask the assistant to validate a fix. The page hash no longer matches, `validate_fix`
returns `stale`, and the assistant must tell you the page changed rather than answering from a
stale index.

### 7. Scanning a real site

Any public URL works: paste it into the scan box. Live pages change under you, which is exactly
why the eval suite runs against fixtures.

### Observability

The **Context Inspector** (right-hand pane) has three tabs:

- **Prompt**: budget allocation and what was actually billed (measured input tokens, cache
  reads), then every block that was sent with its token count, then everything that was
  dropped, with the reason.
- **Retrieval**: hits with flavour and scores, anchor vs. semantic.
- **Tools**: calls with statuses and latencies.

That panel is the answer to *what the model received*. MLflow answers a different question;
see below.

## Evals

The server must be running first, since fixtures are served over HTTP so that Resource Timing and
LCP are real.

```bash
python -m evals.run                                  # all 8 cases
python -m evals.run --case contrast-ancestor-background
python -m evals.run --arm minimal                    # ablation arm
python -m evals.run --arm firehose                   # same token count, no recipe
python -m evals.run --workers 2                      # fewer concurrent Chromium instances
```

The test set is [`evals/cases.json`](evals/cases.json) and the rubric, with the reasoning
behind it and an honest note on run-to-run variance, is
[`evals/rubric.md`](evals/rubric.md). Both are plain files you can read without launching
anything.

Results go to MLflow rather than to a results directory. Each run is an experiment run with
per-dimension metrics and a full trace per case, so comparing two prompt versions or two
ablation arms is a UI operation instead of a diff of JSON files.

## MLflow

```bash
mlflow ui --backend-store-uri ./mlruns
```

`mlruns/` is committed and contains three consecutive runs of the full suite, which is what
the variance discussion in the rubric is based on. MLflow answers two questions the in-app
Context Inspector does not: where the time went (typed `RETRIEVER` / `TOOL` / `LLM` / `CHAIN`
spans per turn) and how eval runs compare to each other over time.

## Layout

One flat package. A directory has to earn its place, and a package holding a single module is
just indirection.

```
app/
  main.py         FastAPI routes + SSE streaming
  config.py       Every tunable, so the design doc and the code can't drift
  models.py       ContextBlock / ContextBundle / Citation
  db.py           SQLite: pages, issues, conversations
  scanner.py      Bridge to the Node scanner
  retrieval.py    Anchor (axe tag -> exact criterion, no embeddings) + semantic
                  (hybrid BM25 + bge-small, RRF fusion)
  context.py      The stretch layer: recipes -> budget -> prompt. Which facts
                  determine this fix, what fits, and how it's presented.
  agent.py        Tool surface (discriminated-union failure contract) + the tool
                  loop and turn asymmetry
  verifier.py     Citation verification
  contrast.py     WCAG contrast maths, so the model never does it
  tracing.py      MLflow spans
  templates/      Four Jinja files
scanner/cli.mjs   Playwright + axe-core. Two modes: scan, validate.
evals/            cases.json, rubric.md, run.py, ablation.py
scripts/          build_corpus.py, embed_corpus.py, md2pdf.mjs
corpus_data/      wcag22.json (vendored W3C source), chunks.jsonl, embeddings.npy
fixtures/         Deliberately-broken pages, served over HTTP
mlruns/           Committed MLflow runs and traces
```
