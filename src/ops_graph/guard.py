"""Free Cypher for an agent, with layered guards.

An agent that can write its own Cypher is useful (questions nobody wrote a
tool for) and dangerous (it reads untrusted text: alert messages, runbooks,
whatever a prompt injection put there). No single layer is enough, so:

  1. read transaction       Neo4j refuses any write (Neo.ClientError.Statement.AccessMode)
  2. denylist               LOAD CSV, CALL dbms.* / apoc.*, CALL ... IN TRANSACTIONS
                            (checked after removing comments, string literals kept,
                            so neither "LOAD/**/CSV" nor a fake comment inside a
                            string can smuggle a keyword through)
  3. transaction timeout    5 s, then Neo4j cancels the query
  4. row cap                200 rows, extra rows are never pulled from the server
  5. server config          file URL import disabled (docker-compose.yml)

Why the denylist: a probe on Neo4j 5.26 showed LOAD CSV runs inside a read
transaction, so layer 1 alone would let the database fetch arbitrary URLs.
A denylist is not a security boundary on its own (Neo4j Community has no
role-based access control to lean on); it is one layer of five.
"""

from __future__ import annotations

import re
from typing import Any

from neo4j import READ_ACCESS, Driver, ManagedTransaction, unit_of_work

from ops_graph.queries import _plain

ROW_LIMIT = 200
TIMEOUT_SECONDS = 5.0

_FORBIDDEN = [
    (re.compile(r"\bLOAD\s+CSV\b", re.IGNORECASE), "LOAD CSV can read files and URLs"),
    (re.compile(r"\bCALL\s+(dbms|apoc)\s*\.", re.IGNORECASE), "dbms.* and apoc.* procedures are not allowed"),
    (re.compile(r"\bIN\s+TRANSACTIONS\b", re.IGNORECASE), "CALL ... IN TRANSACTIONS is not allowed"),
]

# Strings first, so a "//" or "/*" inside a literal is not taken for a comment.
_STRINGS_OR_COMMENTS = re.compile(
    r"""('(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*"|`[^`]*`)"""  # group 1: literals and quoted names, kept
    r"""|//[^\n]*"""  # line comment, dropped
    r"""|/\*.*?\*/""",  # block comment, dropped
    re.DOTALL,
)


class CypherRejected(ValueError):
    """The query was refused before reaching the database."""


def check_cypher(query: str) -> None:
    """Raise CypherRejected if the query uses a forbidden construct."""
    if not query.strip():
        raise CypherRejected("empty query")
    code = _STRINGS_OR_COMMENTS.sub(lambda m: m.group(1) or " ", query)
    for pattern, why in _FORBIDDEN:
        if pattern.search(code):
            raise CypherRejected(f"refused: {why}")


def run_read_only_cypher(
    driver: Driver,
    query: str,
    params: dict[str, Any] | None = None,
    *,
    row_limit: int = ROW_LIMIT,
    timeout_seconds: float = TIMEOUT_SECONDS,
) -> dict[str, Any]:
    check_cypher(query)

    @unit_of_work(timeout=timeout_seconds)
    def work(tx: ManagedTransaction) -> tuple[list[dict[str, Any]], bool]:
        rows: list[dict[str, Any]] = []
        for record in tx.run(query, params or {}):
            if len(rows) == row_limit:
                return rows, True
            rows.append(_plain(record.data()))
        return rows, False

    with driver.session(default_access_mode=READ_ACCESS) as session:
        rows, truncated = session.execute_read(work)
    return {"rows": rows, "row_count": len(rows), "truncated": truncated, "row_limit": row_limit}
