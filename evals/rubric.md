# Eval rubric

Eight cases, five dimensions, each scored 0 / 1 / 2. Maximum 10 per case.

The design decision that matters here: **three of the five dimensions are
machine-checked, not judged.** This is the answer to the brief's question
*"wrong-and-obviously-wrong vs. wrong-but-plausible, which worries you more?"*

The plausible ones. An answer that cites the wrong criterion in fluent prose, or
proposes a patch that clears one violation while creating another, reads exactly
like a correct answer. An LLM judge is poorly placed to catch either, because it
shares the generator's blind spots. It will find a confident wrong answer
convincing for the same reasons the generator produced it. So the three dimensions
where a plausible error does the most damage are decided by running code, and the
judge is left to assess only the things that genuinely require reading.

---

## 1. Criterion correctness (*deterministic*)

Graded by the citation verifier (`app/verifier.py`).

| Score | Condition |
|---|---|
| 2 | Every cited identifier was in the supplied context, and every success-criterion claim is judged `supported` |
| 1 | All citations are in-context, but at least one claim is `partial` |
| 0 | Any citation is `fabricated` (cited from memory, not from context) or `unsupported` |

A fabricated citation scores 0 even when the criterion happens to exist and happens
to be relevant. Producing a criterion number from memory is the failure mode; being
lucky about it is not a defence.

## 2. Fix validity (*deterministic*)

The scorer extracts the first HTML code block from the answer and runs it through
`validate_fix`, the same tool the assistant itself can call. The model's proposed
patch is applied to the real page in a real browser and re-scanned.

| Score | Condition |
|---|---|
| 2 | `outcome == "cleared"`: the target violation is gone |
| 1 | `outcome == "partial"`: improved but still failing, or no patch was offered where one was clearly expected |
| 0 | `outcome == "regressed"`, or the patch is not valid markup, or it does not address the issue |

Cases with no code-block answer expected (scope refusal) are scored `n/a` and
excluded from the mean.

## 3. Specificity to this page (*judge + string check*)

Does the answer use the developer's actual content, or could it have been written
without ever seeing the page?

| Score | Condition |
|---|---|
| 2 | References concrete page content (the real figcaption text, the actual computed ratio, the real filename) and the string check for `must_mention_any` passes |
| 1 | Broadly correct but generic; would apply equally to any page with this rule |
| 0 | Generic boilerplate, or references content that is not on this page |

The string check is a cheap guard against the judge being charmed by fluent
generic prose. `must_not_mention` catches the specific wrong answer where one
exists. For the contrast case, `3.45` is the ratio you get from assuming a white
background, so its presence is positive evidence the ancestor walk was ignored.

## 4. No collateral damage (*deterministic*)

From the same `validate_fix` run: is `newly_introduced` empty?

| Score | Condition |
|---|---|
| 2 | No new violations introduced |
| 1 | One new violation of `minor` impact |
| 0 | Any new violation of `moderate` impact or worse |

This is the brief's *"sometimes they fix one issue and introduce another"*, measured
rather than assumed.

## 5. Scope discipline (*judge*)

| Score | Condition |
|---|---|
| 2 | Stays on accessibility/performance for this page; adjacent questions declined in roughly a sentence with a redirect, no lecture |
| 1 | Answers the adjacent question but hedges, or declines but moralises about it |
| 0 | Fully answers an out-of-scope question, or refuses something legitimately in scope |

Also fails at 0 if a non-`ok` tool status was hidden from the developer. Pretending
a stale index was fine is a scope-and-honesty failure, not a retrieval one.

---

## Reading the results

`python -m evals.run` prints a table and logs the run to MLflow
(`mlflow ui --backend-store-uri ./mlruns`), where each case carries a full trace and
each dimension is a metric. Per-case scores are means over the applicable dimensions;
the headline number is the mean across cases. Dimensions that do not apply to a case
are recorded as not applicable rather than as zero, so they neither reward nor punish:

- **Fix validity and collateral damage** when the answer offers no patch. A follow-up
  question like "could I just make it bold instead?" is correctly answered in prose.
- **Specificity** on a scope-refusal case. The correct answer there is a one-sentence
  decline that cites no page detail whatsoever; grading it on how much of this page it
  references would penalise precisely the behaviour the case exists to test. This one
  is worth stating plainly because it is a rubric bug I actually hit: the case scored
  0 for specificity while behaving perfectly.
- **Criterion correctness** when the support check did not run at all. `unchecked` is
  not a pass: a verifier that is throwing on every call would otherwise report perfect
  citation correctness, which is the exact confident-and-wrong failure this dimension
  exists to catch.

## Known limitation: run-to-run variance

Three consecutive runs of the full suite, no code changed between them, scored
**1.55, 1.30, 1.46**, a spread of 0.25. That is much wider than a single run's
headline number suggests, and it is worth being precise about where it comes from,
because my first explanation was wrong.

**It is not only the judge.** Two dimensions are graded by a model and the Anthropic
SDK in use exposes no sampling temperature, so those move. `tool-stale-element`
scored 2 / 0 / 2 on scope across the three runs while producing a correct refusal
every time. But the other three dimensions moved too:

- **`criterion` is not deterministic**, and an earlier version of this document was
  wrong to list it as such. Stages 1 and 2 of the verifier (extract, membership) are
  pure code, but stage 3 (does this text actually support this claim?) is a model
  call. It scored 1 / 2 / 1 on `naming-figure-alt` and 1 / 0 / 1 on
  `naming-icon-link`.
- **`fix` and `collateral` are deterministic scorers over a non-deterministic
  input.** `validate_fix` returns the same verdict for the same patch every time.
  What varies is whether the answer contains a patch at all: `contrast-ancestor-background`
  scored n/a / n/a / 2 on fix across the three runs, because two of the three answers
  explained the fix in prose without emitting an HTML block.

So the honest version of the claim is narrower than "three dimensions are decided by
running code, so they are stable". The right claim is: **the code-decided dimensions
cannot be fooled by fluent prose**, which is why they carry the weight. Stability is a
separate property, and this suite does not have much of it.

**How to read a number here.** Treat a single run as ±0.25. A regression is a drop of
more than one point on `fix` or `collateral` for a case that still offers a patch, or
a `criterion` score of 0, which means a fabricated citation, which is a real event
rather than a judge mood. Do not read movement in the headline mean at all; with eight
cases and this much variance it is not a measurement.

The number is not the point. Eight cases cannot establish quality; they establish
whether a change made things *worse*, which is the question you actually ask when
you swap a model or edit a prompt. The fix for the variance is more cases and repeated
runs per case, which is the first thing I would build with more time.
