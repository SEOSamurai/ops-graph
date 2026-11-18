"""Acceptance: the demo story, end to end, through an MCP client.

Day 1: the three sources are ingested and reconciled, the runbooks indexed.
An agent asks why customer-portal is slow, gets db01 with its evidence,
and finds the matching runbook passage with the live state of db01.
Day 2: the CMDB registers the shadow server mq01. After a new batch and a
reconciliation, the agent's impact question lands on the CMDB id, and the
runbook that mentions mq01 still points at it.

The README demo script follows the same steps.
"""

import pytest
from mcp import Client

from ops_graph.ingest import ingest
from ops_graph.rag import HashingEmbedder, embed_documents
from ops_graph.reconcile import reconcile
from ops_graph.server import create_server
from tests.conftest import DATA

pytestmark = [pytest.mark.integration, pytest.mark.anyio]


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def test_demo_story(neo4j_driver):
    server = create_server(lambda: neo4j_driver, HashingEmbedder)

    # --- day 1 -----------------------------------------------------------------------
    ingest(neo4j_driver, DATA, "day1")
    reconcile(neo4j_driver)
    embed_documents(neo4j_driver, DATA / "runbooks", HashingEmbedder())

    async with Client(server) as agent:
        cause = (await agent.call_tool("root_cause_candidates", {"asset": "customer-portal"})).structured_content
        top = cause["candidates"][0]
        assert top["id"] == "CI00301" and top["deepest"]
        assert top["path"] == ["customer-portal", "claims-api", "claims-db", "SRV-PAR-DB01"]
        assert {a["severity"] for a in top["alerts"]} == {"warning", "critical"}

        docs = (await agent.call_tool("search_docs", {"question": "PostgreSQL replication lag on the primary", "k": 3})).structured_content
        db01_passages = [p for p in docs["passages"] if any(a["id"] == "CI00301" for a in p["assets"])]
        assert db01_passages and db01_passages[0]["source"] == "db01-postgresql.md"
        live = next(a for a in db01_passages[0]["assets"] if a["id"] == "CI00301")
        assert {a["id"] for a in live["alerts"]} == {"ALR-9001", "ALR-9002"}

        shadow = (await agent.call_tool("impact_analysis", {"asset": "mq01"})).structured_content
        assert shadow["asset"] == "UNREG-par/mq01"

    # --- day 2: mq01 registered in the CMDB ------------------------------------------
    ingest(neo4j_driver, DATA / "day2", "day2")
    reconcile(neo4j_driver)

    async with Client(server) as agent:
        impact = (await agent.call_tool("impact_analysis", {"asset": "mq01"})).structured_content
        assert impact["asset"] == "CI00801"
        assert {i["name"] for i in impact["impacted"]} >= {"notification-service", "claims-api", "customer-portal"}

        report = (await agent.call_tool("reconciliation_report", {})).structured_content
        assert "not_in_cmdb" not in {i["problem"] for i in report["issues"]}

        docs = (await agent.call_tool("search_docs", {"question": "notification queue depth on mq01", "k": 5})).structured_content
        mentioned = {a["id"] for p in docs["passages"] if p["source"] == "notification-queue.md" for a in p["assets"]}
        assert "CI00801" in mentioned and "UNREG-par/mq01" not in mentioned
