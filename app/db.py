"""SQLite page index.

Keyed by (url, content_hash) rather than url alone (design spec §7.4): many
developers looking at the same page share one index and one cached prompt prefix,
and staleness is detected by re-hashing rather than by TTL expiry - cheap *and*
correct, rather than cheap *or* fresh.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from app.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL,
    resolved_url TEXT,
    content_hash TEXT NOT NULL,
    scanned_at TEXT,
    dom_node_count INTEGER,
    indexed_node_count INTEGER,
    perf_json TEXT,
    UNIQUE (url, content_hash)
);

CREATE TABLE IF NOT EXISTS nodes (
    page_id INTEGER NOT NULL,
    selector TEXT NOT NULL,
    idx INTEGER,
    tag TEXT,
    attrs_json TEXT,
    own_text TEXT,
    landmark_path TEXT,
    explicit_role TEXT,
    implicit_role TEXT,
    styles_json TEXT,
    rect_json TEXT,
    parent_selector TEXT,
    outer_html TEXT,
    PRIMARY KEY (page_id, selector)
);
CREATE INDEX IF NOT EXISTS idx_nodes_page ON nodes(page_id);
CREATE INDEX IF NOT EXISTS idx_nodes_parent ON nodes(page_id, parent_selector);

CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY,
    page_id INTEGER NOT NULL,
    rule TEXT NOT NULL,
    impact TEXT,
    help TEXT,
    description TEXT,
    help_url TEXT,
    tags_json TEXT,
    sc_tags_json TEXT,
    target TEXT,
    node_html TEXT,
    failure_summary TEXT,
    node_selector TEXT
);
CREATE INDEX IF NOT EXISTS idx_issues_page ON issues(page_id);

CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    issue_id INTEGER NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    turn INTEGER,
    role TEXT,
    content TEXT,
    bundle_json TEXT,
    citations_json TEXT,
    tool_calls_json TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def ingest_scan(scan: dict[str, Any]) -> int:
    """Load a scanner JSON payload into the page index. Idempotent per (url, hash)."""
    with connect() as conn:
        cur = conn.execute(
            "SELECT id FROM pages WHERE url = ? AND content_hash = ?",
            (scan["url"], scan["content_hash"]),
        )
        row = cur.fetchone()
        if row:
            return int(row["id"])  # already indexed, and provably unchanged

        cur = conn.execute(
            """INSERT INTO pages (url, resolved_url, content_hash, scanned_at,
                                  dom_node_count, indexed_node_count, perf_json)
               VALUES (?,?,?,?,?,?,?)""",
            (scan["url"], scan.get("resolved_url"), scan["content_hash"],
             scan.get("scanned_at"), scan.get("dom_node_count"),
             scan.get("indexed_node_count"), json.dumps(scan.get("perf") or {})),
        )
        page_id = int(cur.lastrowid)

        conn.executemany(
            """INSERT OR REPLACE INTO nodes
               (page_id, selector, idx, tag, attrs_json, own_text, landmark_path,
                explicit_role, implicit_role, styles_json, rect_json,
                parent_selector, outer_html)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            [(page_id, n["selector"], n["idx"], n["tag"], json.dumps(n["attrs"]),
              n["own_text"], n["landmark_path"], n["explicit_role"],
              n["implicit_role"], json.dumps(n["styles"]), json.dumps(n["rect"]),
              n["parent_selector"], n["outer_html"]) for n in scan["nodes"]],
        )

        rows = []
        for v in scan["violations"]:
            for node in v["nodes"]:
                rows.append((page_id, v["rule"], v.get("impact"), v.get("help"),
                             v.get("description"), v.get("help_url"),
                             json.dumps(v.get("tags") or []),
                             json.dumps(v.get("sc_tags") or []),
                             node["target"], node["html"], node.get("failure_summary"), node.get("node_selector")))
        # Performance is a first-class issue alongside the axe violations, because
        # the developer experiences them in the same place. Note it carries NO
        # sc_tags: there is no normative anchor for LCP, so the deterministic
        # retrieval path returns nothing and the assistant has to lean on
        # lower-authority community sources. That asymmetry is deliberate and
        # visible in the inspector (design spec §5.2).
        perf = scan.get("perf") or {}
        lcp = perf.get("lcp")
        if lcp and lcp.get("element_selector"):
            rows.append((
                page_id, "lcp", "serious",
                f"Largest Contentful Paint is {lcp['value_ms']}ms",
                "The largest element painted in the viewport during load. Core Web "
                "Vitals treats 2500ms or less as good.",
                "https://web.dev/articles/lcp",
                json.dumps(["perf", "web-vitals"]), json.dumps([]),
                lcp["element_selector"],
                f"<{lcp.get('element_tag', 'element')}> {lcp.get('url') or ''}",
                f"LCP element rendered at {lcp['value_ms']}ms.",
                lcp["element_selector"],
            ))

        conn.executemany(
            """INSERT INTO issues (page_id, rule, impact, help, description, help_url,
                                   tags_json, sc_tags_json, target, node_html,
                                   failure_summary, node_selector)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", rows)
        return page_id


# ---------------------------------------------------------------- reads

def get_page(page_id: int) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM pages WHERE id = ?", (page_id,)).fetchone()
        return dict(r) if r else None


def latest_pages(limit: int = 20) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*, COUNT(i.id) AS issue_count
               FROM pages p LEFT JOIN issues i ON i.page_id = p.id
               GROUP BY p.id ORDER BY p.id DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]


def get_issues(page_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM issues WHERE page_id = ? ORDER BY rule, id", (page_id,)
        ).fetchall()
        return [dict(r) for r in rows]


def get_issue(issue_id: int) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM issues WHERE id = ?", (issue_id,)).fetchone()
        return dict(r) if r else None


def get_node(page_id: int, selector: str) -> dict | None:
    with connect() as conn:
        r = conn.execute(
            "SELECT * FROM nodes WHERE page_id = ? AND selector = ?",
            (page_id, selector)).fetchone()
        return dict(r) if r else None


def get_children(page_id: int, selector: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE page_id = ? AND parent_selector = ? ORDER BY idx",
            (page_id, selector)).fetchall()
        return [dict(r) for r in rows]


def all_nodes(page_id: int) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM nodes WHERE page_id = ? ORDER BY idx", (page_id,)).fetchall()
        return [dict(r) for r in rows]


def find_issues_by_rule(page_id: int, rule: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM issues WHERE page_id = ? AND rule = ?", (page_id, rule)
        ).fetchall()
        return [dict(r) for r in rows]


def find_issues_by_target(page_id: int, target: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM issues WHERE page_id = ? AND target = ?", (page_id, target)
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- conversations

def create_conversation(conv_id: str, issue_id: int, created_at: str) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO conversations (id, issue_id, created_at) VALUES (?,?,?)",
            (conv_id, issue_id, created_at))


def get_conversation(conv_id: str) -> dict | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        return dict(r) if r else None


def add_message(conv_id: str, turn: int, role: str, content: str,
                bundle_json: str | None = None, citations_json: str | None = None,
                tool_calls_json: str | None = None, created_at: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO messages (conversation_id, turn, role, content, bundle_json,
                                     citations_json, tool_calls_json, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (conv_id, turn, role, content, bundle_json, citations_json,
             tool_calls_json, created_at))
        return int(cur.lastrowid)


def get_messages(conv_id: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id", (conv_id,)
        ).fetchall()
        return [dict(r) for r in rows]
