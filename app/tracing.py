"""MLflow tracing (design spec §8).

Spans are typed so the MLflow UI renders them meaningfully - retrieval spans show
their documents, tool spans show arguments and status, the LLM span shows usage
including cache reads.

Tracing must never take the app down: if MLflow is misconfigured or unavailable,
every helper degrades to a no-op and the request still succeeds. Observability that
can break the thing it observes is worse than none.
"""
from __future__ import annotations

import contextlib
import logging
import sys
from typing import Any, Iterator

log = logging.getLogger(__name__)

_ENABLED = False
_SpanType: Any = None


def init() -> bool:
    global _ENABLED, _SpanType
    if _ENABLED:
        return True
    try:
        import mlflow
        from mlflow.entities import SpanType

        from app.config import MLFLOW_EXPERIMENT, MLFLOW_TRACKING_URI

        mlflow.set_tracking_uri(MLFLOW_TRACKING_URI)
        mlflow.set_experiment(MLFLOW_EXPERIMENT)

        # Nothing here is a notebook, and leaving this on deadlocks the eval suite.
        # On every trace export mlflow asks "am I in Jupyter?" by running
        # `from IPython import get_ipython`. IPython is not installed, so that
        # import *fails* - and a failing import re-runs the full finder machinery,
        # taking import locks, every single time. Meanwhile mlflow's scorer threads
        # are lazily importing their own submodules. The two sides take one lock
        # each and wait for the other, and the run hangs *after* every case has been
        # answered and scored, which reads as a slow eval rather than a stuck one.
        # Observed on 2 of 3 consecutive runs; py-spy showed both threads parked in
        # `_find_and_load`. Disabling the notebook display short-circuits the check
        # before it ever reaches the import.
        with contextlib.suppress(Exception):
            mlflow.tracing.disable_notebook_display()
        try:
            mlflow.anthropic.autolog()
        except Exception as exc:  # noqa: BLE001
            log.warning("anthropic autolog unavailable: %s", exc)
        _SpanType = SpanType
        _ENABLED = True
        log.info("MLflow tracing enabled -> %s / %s", MLFLOW_TRACKING_URI, MLFLOW_EXPERIMENT)
    except Exception as exc:  # noqa: BLE001
        log.warning("MLflow tracing disabled: %s", exc)
        _ENABLED = False
    return _ENABLED


def enabled() -> bool:
    return _ENABLED


def span_type(name: str) -> Any:
    """Map our vocabulary to MLflow's SpanType enum, tolerating version drift."""
    if not _ENABLED or _SpanType is None:
        return None
    return {
        "retriever": getattr(_SpanType, "RETRIEVER", None),
        "tool": getattr(_SpanType, "TOOL", None),
        "llm": getattr(_SpanType, "LLM", None),
        "chain": getattr(_SpanType, "CHAIN", None),
        "parser": getattr(_SpanType, "PARSER", None),
    }.get(name)


@contextlib.contextmanager
def span(name: str, kind: str = "chain", **attributes: Any) -> Iterator[Any]:
    """Typed span that degrades to a no-op when tracing is off."""
    if not _ENABLED:
        yield _NullSpan()
        return
    # Only failures *opening* the span degrade to a no-op. An exception raised by
    # the caller's body must propagate: catching it here and yielding a second time
    # made @contextmanager raise "generator didn't stop", which replaced the real
    # error with a meaningless one and hid the actual failure.
    try:
        import mlflow
        cm = mlflow.start_span(name=name, span_type=span_type(kind))
        sp = cm.__enter__()
    except Exception as exc:  # noqa: BLE001
        log.debug("span %s unavailable: %s", name, exc)
        yield _NullSpan()
        return

    try:
        for k, v in attributes.items():
            with contextlib.suppress(Exception):
                sp.set_attribute(k, v)
        yield sp
    finally:
        # Closing must never mask whatever the body raised.
        with contextlib.suppress(Exception):
            cm.__exit__(*sys.exc_info())


class _NullSpan:
    def set_attribute(self, *_: Any, **__: Any) -> None:
        pass

    def set_inputs(self, *_: Any, **__: Any) -> None:
        pass

    def set_outputs(self, *_: Any, **__: Any) -> None:
        pass


def set_io(sp: Any, inputs: Any = None, outputs: Any = None) -> None:
    with contextlib.suppress(Exception):
        if inputs is not None:
            sp.set_inputs(inputs)
        if outputs is not None:
            sp.set_outputs(outputs)
