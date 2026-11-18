"""MCP server: the Ops graph as tools an AI agent can call.

Built on the official MCP Python SDK 2.x (`MCPServer`, formerly FastMCP).
Every tool is read-only and says so (`read_only_hint`). Run it with
`ops-graph serve` (stdio); see .mcp.json for a client configuration.

Tools take what a human would type ("db01.par", "claims-api") and resolve
it; when a name is ambiguous the tool refuses and lists the candidates, so
the agent asks instead of guessing.
"""

from __future__ import annotations

from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from neo4j import Driver
from neo4j.exceptions import Neo4jError
from neo4j_graphrag.embeddings.base import Embedder

from ops_graph import queries, rag
from ops_graph.guard import CypherRejected, run_read_only_cypher
from ops_graph.rag import make_embedder

INSTRUCTIONS = """\
You are querying the Ops knowledge graph of a (fictional) insurer: servers, applications,
databases, teams and monitoring alerts, reconciled from a CMDB, a monitoring export and a
service catalog.

- Start with describe_schema if you do not know the graph.
- Use find_asset to turn a name into an id; tools also accept names directly.
- search_docs finds runbook passages and returns the live alerts and dependencies of
  the assets each passage mentions. Cite the source document when you use it.
- Every answer carries its evidence: paths, matching rules, confidence. Report
  `uncertain` and `needs_review` flags to the user; never present them as facts.
- read_only_cypher is for questions no other tool answers. It is read-only,
  times out after 5 s and returns at most 200 rows.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def create_server(get_driver: Callable[[], Driver], get_embedder: Callable[[], Embedder] | None = None) -> MCPServer:
    """Build the server. Dependencies are called lazily, so tests can inject a test database and embedder."""
    mcp = MCPServer("ops-graph", instructions=INSTRUCTIONS)
    get_embedder = get_embedder or make_embedder

    def resolve(asset: str) -> str:
        matches = queries.find_assets(get_driver(), asset)
        if len(matches) == 1:
            return matches[0]["id"]
        if not matches:
            raise ToolError(f"No asset matches {asset!r}. Try find_asset with another spelling.")
        listed = ", ".join(f"{m['id']} ({m['type']} {m['name']})" for m in matches)
        raise ToolError(f"{asset!r} is ambiguous: {listed}. Call again with one of these ids.")

    @mcp.tool(annotations=READ_ONLY)
    def find_asset(name: str) -> list[dict[str, Any]]:
        """Find servers, applications or databases by id, name or any hostname spelling (e.g. 'db01.par')."""
        return queries.find_assets(get_driver(), name)

    @mcp.tool(annotations=READ_ONLY)
    def impact_analysis(asset: str, max_depth: int = 6) -> dict[str, Any]:
        """What stops working if this asset stops? Returns every impacted asset with the dependency path that proves it."""
        asset_id = resolve(asset)
        impacted = queries.impact_analysis(get_driver(), asset_id, max_depth)
        return {"asset": asset_id, "impacted_count": len(impacted), "impacted": impacted}

    @mcp.tool(annotations=READ_ONLY)
    def root_cause_candidates(asset: str) -> dict[str, Any]:
        """Why is this asset failing? Alerting dependencies, most likely root cause first ('deepest' = nothing below it alerts)."""
        asset_id = resolve(asset)
        return {"asset": asset_id, "candidates": queries.root_cause_candidates(get_driver(), asset_id)}

    @mcp.tool(annotations=READ_ONLY)
    def decommission_candidates(stale_days: int = 90) -> dict[str, Any]:
        """Servers with no live workload, with reasons. Servers whose workload is unknown are listed as 'blocked', never as candidates."""
        return queries.decommission_candidates(get_driver(), stale_days)

    @mcp.tool(annotations=READ_ONLY)
    def compliance_findings() -> dict[str, Any]:
        """Personal data on servers with an unsupported OS or not registered in the CMDB."""
        return {"findings": queries.compliance_findings(get_driver())}

    @mcp.tool(annotations=READ_ONLY)
    def reconciliation_report() -> dict[str, Any]:
        """Data quality: records reconciliation could not settle, links to review, and how many links each rule made."""
        return queries.reconciliation_report(get_driver())

    @mcp.tool(annotations=READ_ONLY)
    def describe_schema() -> dict[str, Any]:
        """Node and relationship counts and the direction conventions of the graph."""
        return queries.describe_schema(get_driver())

    @mcp.tool(annotations=READ_ONLY)
    def search_docs(question: str, k: int = 3) -> dict[str, Any]:
        """Search the runbooks and postmortems (GraphRAG): best passages plus the live state of the assets they mention."""
        try:
            hits = rag.search_docs(get_driver(), question, get_embedder(), k)
        except Neo4jError as exc:
            raise ToolError(f"Document index unavailable ({exc.code}). Run `ops-graph embed`.") from exc
        except Exception as exc:  # embedding backend down
            raise ToolError(f"Document search unavailable: {type(exc).__name__}: {exc}") from exc
        return {"question": question, "passages": hits}

    @mcp.tool(annotations=READ_ONLY)
    def read_only_cypher(query: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Run your own read-only Cypher. Refuses writes, LOAD CSV and dbms/apoc procedures; 5 s timeout; at most 200 rows."""
        try:
            return run_read_only_cypher(get_driver(), query, params)
        except CypherRejected as exc:
            raise ToolError(str(exc)) from exc
        except Neo4jError as exc:
            raise ToolError(f"{exc.code}: {exc.message}") from exc

    return mcp


def serve() -> None:
    """Entry point for `ops-graph serve`: stdio transport, one Neo4j driver for the process."""
    from functools import cache

    from ops_graph.cli import connect

    create_server(cache(connect), cache(make_embedder)).run()
