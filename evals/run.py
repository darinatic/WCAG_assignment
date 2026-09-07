"""Eval suite, run through MLflow.

    python -m evals.run                                  # all cases, full arm
    python -m evals.run --case contrast-ancestor-background
    python -m evals.run --arm minimal                    # ablation: see ablation.py

`cases.json` and `rubric.md` stay plain files on purpose - the test set and the
reasoning behind the rubric should be readable in two minutes without launching
anything. What MLflow owns is the *results*: every run is an experiment run with
per-dimension metrics and a full trace per case, so comparing two prompt versions
or two ablation arms is a UI operation rather than a diff of JSON files. That
comparison is the regression suite the design write-up lists as not built.

Three of the five dimensions are decided by running code rather than by a judge
(see rubric.md for why). The judge grades only the two that genuinely require
reading, and it grades both in one call.

    mlflow ui --backend-store-uri ./mlruns
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import mlflow
from mlflow.genai import evaluate, scorer

from app import agent, db, retrieval, scanner, tracing, verifier
from app.config import FIXTURES_DIR, MLFLOW_EXPERIMENT, MLFLOW_TRACKING_URI, ROOT, VERIFIER_MODEL

CASES_PATH = ROOT / "evals" / "cases.json"
SELF_ORIGIN = "http://127.0.0.1:8000"

_CODE_RE = re.compile(r"```(?:html|HTML)?\s*\n(.*?)```", re.S)

# MLflow is the store of record. This dict exists only so the terminal table can be
# printed in case order without re-deriving it from the result frame.
_ROWS: dict[str, dict[str, Any]] = {}
_CASES: dict[str, dict[str, Any]] = {}

JUDGE_PROMPT = """\
You are grading one answer from an accessibility assistant. Grade only what is asked.

THE ISSUE: {rule} on the developer's page.
WHAT THE DEVELOPER ASKED: {question}

THE ANSWER:
\"\"\"
{answer}
\"\"\"

Grade two dimensions, each 0, 1 or 2.

SPECIFICITY - does the answer use this developer's actual page content, or could it
have been written without ever seeing the page?
  2 = references concrete specifics from this page (real text, real computed values,
      real filenames)
  1 = broadly correct but generic; would apply to any page with this rule
  0 = boilerplate, or references content that is not on this page

SCOPE - did it stay in its lane?
  2 = stayed on accessibility/performance for this page. If asked something adjacent
      (framework choice, rewriting the site), declined in about a sentence and
      redirected, without lecturing
  1 = answered an adjacent question but hedged, or declined but moralised
  0 = fully answered an out-of-scope question, or refused something legitimately in
      scope, or hid a failed tool result and answered as though it had succeeded

Respond with JSON only:
{{"specificity": <0-2>, "scope": <0-2>, "reason": "<one sentence>"}}
"""


# ---------------------------------------------------------------- case running

def ensure_page(fixture: str) -> int:
    return db.ingest_scan(scanner.scan(f"{SELF_ORIGIN}/fixtures/{fixture}"))


def find_issue(page_id: int, rule: str, selector_contains: str | None) -> dict | None:
    for issue in db.get_issues(page_id):
        if issue["rule"] != rule:
            continue
        sel = issue.get("node_selector") or issue.get("target") or ""
        if not selector_contains or selector_contains in sel:
            return issue
    return None


def extract_patch(answer: str) -> str | None:
    m = _CODE_RE.search(answer or "")
    if not m:
        return None
    code = m.group(1).strip()
    return code if code.startswith("<") else None


def mutate_fixture(fixture: str) -> Path:
    """Delete the news <figure> so the page hash moves under the assistant.

    The stale-tool case: the index the assistant holds no longer describes the live
    page, and the failure contract says it must say so rather than answer anyway.
    """
    path = FIXTURES_DIR / fixture
    backup = path.with_suffix(".html.bak")
    shutil.copy(path, backup)
    html = path.read_text(encoding="utf-8")
    html = re.sub(r"<figure class=\"news-card\">.*?</figure>", "<p>removed</p>",
                  html, flags=re.S)
    path.write_text(html, encoding="utf-8")
    return backup


def restore_fixture(fixture: str, backup: Path) -> None:
    if backup.exists():
        shutil.copy(backup, FIXTURES_DIR / fixture)
        backup.unlink()


def predict_fn(case_id: str) -> dict[str, Any]:
    """Run one case end to end and hand the scorers everything they need."""
    case = _CASES[case_id]

    # A case that rewrites the page mid-conversation gets a PRIVATE copy of the
    # fixture. MLflow evaluates rows concurrently, so mutating the shared file
    # corrupts every other case in flight - they scan a page that changes under
    # them, their content hashes stop matching, and validate_fix returns `stale`
    # for reasons that have nothing to do with the answer being graded.
    fixture = case["fixture"]
    private_fixture = None
    if case.get("mutate_before_turn"):
        private_fixture = f"_case-{case_id}.html"
        shutil.copy(FIXTURES_DIR / fixture, FIXTURES_DIR / private_fixture)
        fixture = private_fixture

    try:
        return _run_case(case, case_id, fixture)
    finally:
        if private_fixture:
            (FIXTURES_DIR / private_fixture).unlink(missing_ok=True)
            (FIXTURES_DIR / private_fixture).with_suffix(".html.bak").unlink(missing_ok=True)


def _run_case(case: dict, case_id: str, fixture: str) -> dict[str, Any]:
    page_id = ensure_page(fixture)
    page = db.get_page(page_id)
    issue = find_issue(page_id, case["rule"], case.get("selector_contains"))
    if not issue:
        return {"case_id": case_id, "error": f"issue not found: {case['rule']}",
                "answer": "", "citations": [], "tools": [], "bundle": {}}

    history: list[dict] = []
    answer, citations, tools, bundle = "", [], [], {}
    backup = None
    started = time.time()
    try:
        for turn_no, question in enumerate(case["turns"], start=1):
            if case.get("mutate_before_turn") == turn_no:
                backup = mutate_fixture(fixture)
            answer, citations, tools, bundle = "", [], [], {}
            for event in agent.run_turn(page_id, issue, history, question):
                if event["type"] == "done":
                    answer = event["answer"]
                    citations = event["citations"]
                    tools = event["tools"]
                    bundle = event["bundle"]
            history = history + [{"role": "user", "content": question},
                                 {"role": "assistant", "content": answer}]
    finally:
        if backup:
            restore_fixture(fixture, backup)

    return {
        "case_id": case_id,
        "answer": answer,
        "citations": citations,
        "tools": tools,
        "bundle": bundle,
        "page": {"url": page["resolved_url"] or page["url"],
                 "content_hash": page["content_hash"]},
        "issue": {"rule": issue["rule"],
                  "selector": issue.get("node_selector") or issue.get("target")},
        "recipe": bundle.get("recipe"),
        "elapsed_s": round(time.time() - started, 1),
    }


# ---------------------------------------------------------------- scorers
# Three of these five never ask a model anything. Criterion correctness comes from
# the citation verifier, and fix validity plus collateral damage come from actually
# applying the model's own patch in a browser and re-running axe. A judge that
# shares the generator's blind spots is the wrong instrument for those.

def _record(case_id: str, key: str, value: Any, note: str = "") -> None:
    row = _ROWS.setdefault(case_id, {})
    row[key] = value
    if note:
        row.setdefault("notes", {})[key] = note


@scorer
def criterion(outputs) -> int | None:
    """Did every citation survive the verifier?"""
    cits = outputs.get("citations") or []
    cid = outputs.get("case_id", "?")
    if not cits:
        _record(cid, "criterion", 1, "no citations to check")
        return 1
    verdicts = {c["verdict"] for c in cits}
    if verdicts & {"fabricated", "unsupported"}:
        bad = [c["raw"] for c in cits if c["verdict"] in {"fabricated", "unsupported"}]
        _record(cid, "criterion", 0, f"bad citations: {bad}")
        return 0

    # "unchecked" means the support check did not run - no client, or the call threw.
    # It must never earn full marks: a broken verifier would otherwise report perfect
    # citation correctness, which is exactly the confident-and-wrong failure this
    # dimension exists to catch. (Observed for real: an invalid SDK argument made
    # every support check throw, and the suite scored 2/2 here on the way past.)
    if verdicts == {"unchecked"}:
        _record(cid, "criterion", None, "n/a - support check did not run")
        return None
    if "unchecked" in verdicts:
        _record(cid, "criterion", 1, "at least one claim could not be checked")
        return 1

    if "partial" in verdicts:
        _record(cid, "criterion", 1, "at least one claim only partially supported")
        return 1
    _record(cid, "criterion", 2, "all citations in-context and supported")
    return 2


def _validate(outputs) -> dict | None:
    """Apply the model's own patch to the real page and re-run axe. Memoised per
    case so fix validity and collateral damage share one browser run."""
    cid = outputs.get("case_id", "?")
    row = _ROWS.setdefault(cid, {})
    if "_validation" in row:
        return row["_validation"]

    patch = extract_patch(outputs.get("answer", ""))
    if not patch:
        row["_validation"] = None
        row.setdefault("notes", {})["fix"] = "no HTML patch offered"
        return None
    try:
        res = scanner.validate_fix(
            url=outputs["page"]["url"],
            selector=outputs["issue"]["selector"],
            patched_html=patch,
            rule=outputs["issue"]["rule"],
            expect_hash=outputs["page"]["content_hash"])
    except Exception as exc:  # noqa: BLE001
        res = {"status": "error", "reason": str(exc)}
    if res.get("status") in {"stale", "not_found", "error"}:
        row["_validation"] = None
        row.setdefault("notes", {})["fix"] = f"could not validate: {res.get('status')}"
        return None
    row["_validation"] = res
    return res


@scorer
def fix(outputs) -> int | None:
    """Does the model's own patch actually clear the violation? None = no patch."""
    res = _validate(outputs)
    if res is None:
        return None
    score = {"cleared": 2, "partial": 1, "regressed": 0}.get(res.get("status"), 0)
    _record(outputs.get("case_id", "?"), "fix", score,
            f"{res.get('status')}; {len(res.get('introduced', []))} newly introduced")
    return score


@scorer
def collateral(outputs) -> int | None:
    """Did the fix break something else? The brief's 'fixed one thing, broke another'."""
    res = _validate(outputs)
    if res is None:
        return None
    introduced = res.get("introduced", [])
    if not introduced:
        score = 2
    elif all(v.get("impact") == "minor" for v in introduced):
        score = 1
    else:
        score = 0
    _record(outputs.get("case_id", "?"), "collateral", score,
            f"{len(introduced)} newly introduced")
    return score


def _judgement(outputs) -> dict:
    """One judge call grades both reading dimensions. Memoised per case."""
    cid = outputs.get("case_id", "?")
    row = _ROWS.setdefault(cid, {})
    if "_judge" in row:
        return row["_judge"]
    case = _CASES.get(cid, {})
    prompt = JUDGE_PROMPT.format(rule=case.get("rule", "?"),
                                 question=(case.get("turns") or ["?"])[-1],
                                 answer=(outputs.get("answer") or "")[:6000])
    try:
        resp = agent.get_client().messages.create(
            model=VERIFIER_MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        data = verifier._first_json_object(text)
        verdict = data if data is not None else {"specificity": 0, "scope": 0,
                                                 "reason": "unparsable"}
    except Exception as exc:  # noqa: BLE001
        verdict = {"specificity": 0, "scope": 0, "reason": f"judge failed: {exc}"}
    row["_judge"] = verdict
    row.setdefault("notes", {})["judge"] = verdict.get("reason", "")
    return verdict


@scorer
def specificity(outputs, expectations) -> int | None:
    """Judge's read, then held to the case's own string expectations."""
    exp = expectations or {}
    if exp.get("scope_refusal"):
        # The correct answer to this case is a one-sentence decline that cites no
        # page detail at all. Grading it on how much of this page it references
        # punishes exactly the behaviour the case exists to test, so the dimension
        # does not apply - the same way fix validity does not apply without a patch.
        _record(outputs.get("case_id", "?"), "specificity", None,
                "n/a - a correct scope refusal has no page detail to be specific about")
        return None

    v = _judgement(outputs)
    score = int(v.get("specificity", 0))
    low = (outputs.get("answer") or "").lower()
    any_ok = (not exp.get("must_mention_any")
              or any(s.lower() in low for s in exp["must_mention_any"]))
    not_ok = (not exp.get("must_not_mention")
              or not any(s.lower() in low for s in exp["must_not_mention"]))
    if not any_ok or not not_ok:
        score = min(score, 1 if any_ok else 0)
    _record(outputs.get("case_id", "?"), "specificity", score,
            f"must_mention_any={any_ok} must_not_mention={not_ok}")
    return score


@scorer
def scope(outputs) -> int:
    """Stayed in its lane - and surfaced any tool that did not return ok."""
    v = _judgement(outputs)
    score = int(v.get("scope", 0))
    bad_tools = [t for t in (outputs.get("tools") or [])
                 if t.get("status") not in (None, "ok")]
    if bad_tools:
        low = (outputs.get("answer") or "").lower()
        surfaced = any(w in low for w in
                       ("changed", "stale", "no longer", "could not", "not found",
                        "re-scan", "rescan", "removed"))
        if not surfaced:
            score = 0
    _record(outputs.get("case_id", "?"), "scope", score,
            f"{len(bad_tools)} non-ok tool call(s)")
    return score


SCORERS = [criterion, fix, specificity, collateral, scope]
DIMENSIONS = ["criterion", "fix", "specificity", "collateral", "scope"]


# ---------------------------------------------------------------- runner

def print_table(case_ids: list[str], arm: str) -> float:
    hdr = f"{'case':<32}{'crit':>6}{'fix':>6}{'spec':>6}{'coll':>6}{'scope':>7}{'mean':>7}"
    print()
    print(hdr)
    print("-" * len(hdr))
    means = []
    for cid in case_ids:
        row = _ROWS.get(cid, {})
        cells = ""
        nums = []
        for d in DIMENSIONS:
            v = row.get(d)
            cells += f"{('n/a' if v is None else str(v)):>6}" if d != "scope" \
                else f"{('n/a' if v is None else str(v)):>7}"
            if isinstance(v, int):
                nums.append(v)
        mean = round(sum(nums) / len(nums), 2) if nums else 0.0
        means.append(mean)
        print(f"{cid:<32}{cells}{mean:>7}")
    overall = round(sum(means) / len(means), 2) if means else 0.0
    print("-" * len(hdr))
    print(f"{'OVERALL (arm=' + arm + ')':<32}{overall:>38}")
    return overall


def _preimport_mlflow_lazy_modules() -> None:
    """Second half of the mlflow 3.15 import-deadlock workaround.

    `tracing.init()` removes the thread that was always one side of it. This
    removes the other: mlflow's scorer threads import `mlflow.metrics.genai`,
    `mlflow.genai.discovery` and `mlflow.telemetry.events` lazily, on first use,
    from inside the evaluation thread pool. Importing them here - on the main
    thread, before any pool exists - means no scorer has to take an import lock
    while another thread holds one.

    Cheap, and it fails loudly rather than silently if a future mlflow moves them.
    """
    import mlflow.genai.discovery  # noqa: F401
    import mlflow.metrics.genai  # noqa: F401
    import mlflow.telemetry.events  # noqa: F401


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", help="run a single case id")
    ap.add_argument("--arm", default="full",
                    help="context arm: full | minimal | firehose (ablation)")
    ap.add_argument("--workers", type=int, default=4,
                    help="concurrent cases (default 4). Each one launches a Chromium "
                         "for the scan, so MLflow's default of 10 will exhaust memory "
                         "on a laptop long before it saturates the API.")
    args = ap.parse_args()

    os.environ["MLFLOW_GENAI_EVAL_MAX_WORKERS"] = str(max(1, args.workers))

    if args.arm != "full":
        from evals import ablation
        ablation.activate(args.arm)

    db.init()
    tracing.init()
    # Block here, unlike the server: a case that silently ran BM25-only because the
    # encoder had not finished loading is not comparable to one that did not.
    if not retrieval.ensure_encoder():
        print("warning: dense encoder unavailable - scoring BM25-only retrieval")

    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
        if not cases:
            sys.exit(f"no such case: {args.case}")
    _CASES.update({c["id"]: c for c in cases})

    # MLflow's Expectation entity rejects a null value, and cases.json legitimately
    # carries some: `"criterion": null` on the LCP case means "no WCAG anchor exists
    # for this", which is the point of that case rather than a missing field. Drop
    # the nulls here instead of editing meaning out of the test set.
    data = [{"inputs": {"case_id": c["id"]},
             "expectations": {k: v for k, v in (c.get("expects") or {}).items()
                              if v is not None}} for c in cases]

    mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
    mlflow.set_experiment(MLFLOW_EXPERIMENT)

    _preimport_mlflow_lazy_modules()

    result = evaluate(data=data, scorers=SCORERS, predict_fn=predict_fn)

    overall = print_table([c["id"] for c in cases], args.arm)
    with mlflow.start_run(run_id=result.run_id):
        mlflow.log_metric("overall_mean", overall)
        mlflow.log_param("arm", args.arm)

    print(f"\nrun_id {result.run_id} in experiment '{MLFLOW_EXPERIMENT}'")
    print("view:  mlflow ui --backend-store-uri ./mlruns")


if __name__ == "__main__":
    main()
