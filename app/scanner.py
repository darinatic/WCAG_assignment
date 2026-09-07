"""Subprocess wrapper around the Node scanner CLI.

Deliberately not an HTTP service (design spec §3): the scanner runs once per page,
writes JSON, and exits. `validate_fix` reuses the same binary in a different mode.
"""
from __future__ import annotations

import json
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from app.config import (FIXTURES_DIR, SCANNER_CLI, SCAN_TIMEOUT_S,
                        VALIDATE_TIMEOUT_S)


class ScannerError(RuntimeError):
    pass


def _run(args: list[str], timeout: int) -> dict[str, Any]:
    out_path = Path(tempfile.mktemp(suffix=".json"))
    cmd = ["node", str(SCANNER_CLI), *args, "--out", str(out_path)]
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              cwd=str(SCANNER_CLI.parent))
    except subprocess.TimeoutExpired as exc:
        raise ScannerError(f"scanner timed out after {timeout}s") from exc

    if not out_path.exists():
        raise ScannerError(
            f"scanner produced no output (exit {proc.returncode}): "
            f"{(proc.stderr or '').strip()[:400]}"
        )
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    finally:
        out_path.unlink(missing_ok=True)

    payload["_elapsed_ms"] = int((time.time() - started) * 1000)
    payload["_stderr"] = (proc.stderr or "").strip()[-400:]
    return payload


def resolve_input(url_or_name: str) -> str:
    """Fixtures may be referenced by bare name; anything else is passed through.

    Fixtures are served over HTTP by the app rather than read from disk, because
    file:// gives no Resource Timing and therefore no LCP data.
    """
    if url_or_name.startswith(("http://", "https://")):
        return url_or_name
    candidate = FIXTURES_DIR / url_or_name
    if candidate.exists():
        return f"fixture:{candidate.name}"
    return url_or_name


def scan(url: str) -> dict[str, Any]:
    return _run(["scan", "--url", url], SCAN_TIMEOUT_S)


def validate_fix(url: str, selector: str, patched_html: str,
                 rule: str | None = None,
                 expect_hash: str | None = None) -> dict[str, Any]:
    """Apply a patch in a real browser and re-run axe (design spec §6.3)."""
    patch_path = Path(tempfile.mktemp(suffix=".html"))
    patch_path.write_text(patched_html, encoding="utf-8")
    args = ["validate", "--url", url, "--selector", selector,
            "--patch-file", str(patch_path)]
    if rule:
        args += ["--rule", rule]
    if expect_hash:
        args += ["--expect-hash", expect_hash]
    try:
        return _run(args, VALIDATE_TIMEOUT_S)
    finally:
        patch_path.unlink(missing_ok=True)
