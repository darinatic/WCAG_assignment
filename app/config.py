"""Central configuration. Everything tunable lives here so the design doc and the
code cannot drift apart on numbers."""
from __future__ import annotations

import os
from pathlib import Path

# Must be set before transformers is imported anywhere. transformers will happily
# load TensorFlow *and* Torch side by side, which roughly doubles resident memory
# for no benefit here - we only ever run a small Torch sentence-transformer.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# mlflow >= 3.15 refuses a filesystem tracking backend unless this is set. We want
# the file store: traces are a local debugging aid for a reviewer running this once,
# and `mlflow ui --backend-store-uri ./mlruns` needs no server to stand up first.
os.environ.setdefault("MLFLOW_ALLOW_FILE_STORE", "true")

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# --- paths -------------------------------------------------------------------
DATA_DIR = ROOT / "data"
CORPUS_DIR = ROOT / "corpus_data"
FIXTURES_DIR = ROOT / "fixtures"
SCANNER_CLI = ROOT / "scanner" / "cli.mjs"
DB_PATH = DATA_DIR / "app.db"
CHUNKS_PATH = CORPUS_DIR / "chunks.jsonl"
EMBEDDINGS_PATH = CORPUS_DIR / "embeddings.npy"

DATA_DIR.mkdir(exist_ok=True)

# --- models ------------------------------------------------------------------
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Haiku, on evidence rather than instinct. Running the eval suite across three models
# on identical context: Opus 1.62, Sonnet 1.51, Haiku 1.51 - a spread inside the
# suite's own +/-0.10 judge-noise band, so it cannot show Opus is better here. What it
# does show is latency: 797ms to first token and 4.6s complete on Haiku, against
# 6,353ms and 15.8s on Opus. The brief asks for an answer in seconds.
#
# That result IS the design's thesis: when the criterion text is looked up exactly,
# the contrast maths is computed for the model, and the page evidence is selected by
# recipe, the remaining job is explanation - and a small model explains well. The one
# place the smaller models wobbled was `lcp`, the only issue type with no normative
# anchor. Weakest grounding, largest model-capability effect. Set ASSISTANT_MODEL to
# override per environment.
ASSISTANT_MODEL = os.getenv("ASSISTANT_MODEL", "claude-haiku-4-5")

# ...with one exception, and it is the same finding stated as a rule. Issue types that
# resolve to an exact normative criterion get the fast model, because the grounding is
# carrying the answer. Issue types with NO normative anchor do not: measured over three
# trials on `lcp`, Haiku's own patch cleared / regressed / cleared - one in three
# introduced a new critical violation, caught by validate_fix rather than by a judge.
# Weakest grounding, largest model dependence. So the ungrounded path buys capability.
UNANCHORED_MODEL = os.getenv("UNANCHORED_MODEL", "claude-sonnet-5")
# Recipes whose issues have no WCAG criterion behind them.
UNANCHORED_RECIPES = {"lcp"}


def model_for(recipe: str | None) -> str:
    """Which model answers this issue. See UNANCHORED_MODEL for the evidence."""
    return UNANCHORED_MODEL if recipe in UNANCHORED_RECIPES else ASSISTANT_MODEL
# The verifier's independence comes from CONTEXT isolation, not from being a different
# size of model: it sees only the quoted spec text and the one claiming sentence, never
# the page or the conversation. Worth knowing that it now shares a model with the
# generator, which makes it a weaker check than a cross-model one would be.
VERIFIER_MODEL = os.getenv("VERIFIER_MODEL", "claude-haiku-4-5")
EMBED_MODEL = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")

# A developer clicked "explain and fix", not "write me an essay". Measured turns
# were spending ~100s generating 3,000+ output tokens while every tool call in the
# same turn cost under 25ms - the answer length was the whole latency budget. This
# caps it; the system prompt asks for the same brevity so the cap is a backstop
# rather than a guillotine mid-sentence.
MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "1600"))

# --- context budget (design spec §7.3) ---------------------------------------
# Per-turn input allocation. Blocks are admitted in priority order; anything that
# does not fit is recorded with a drop_reason rather than silently vanishing.
BUDGET = {
    "system": 1_200,          # frozen, never dropped
    "spec_normative": 1_500,  # the requirement itself, never dropped
    "page_evidence": 4_000,   # recipe output, ranked and truncated from the bottom
    "spec_advisory": 2_500,   # techniques / understanding, dropped first
    "conversation": 4_000,    # oldest turns dropped first
}
TOTAL_BUDGET = sum(BUDGET.values())

# Blocks in these kinds are never evicted, whatever the budget says.
NEVER_DROP = {"system", "spec_normative"}

# --- retrieval ---------------------------------------------------------------
SEMANTIC_TOP_K = 6
BM25_TOP_K = 6
HYBRID_TOP_K = 5
# Reciprocal-rank-fusion constant; 60 is the value from the original RRF paper.
RRF_K = 60

# --- scanner -----------------------------------------------------------------
SCAN_TIMEOUT_S = 90
VALIDATE_TIMEOUT_S = 90

# --- observability -----------------------------------------------------------
MLFLOW_TRACKING_URI = os.getenv("MLFLOW_TRACKING_URI", f"file:///{(ROOT / 'mlruns').as_posix()}")
MLFLOW_EXPERIMENT = os.getenv("MLFLOW_EXPERIMENT", "a11y-assistant")


def require_api_key() -> str:
    if not ANTHROPIC_API_KEY:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set. Copy .env.example to .env and add your key."
        )
    return ANTHROPIC_API_KEY
