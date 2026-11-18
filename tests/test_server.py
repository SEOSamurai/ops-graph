"""The MCP server as an agent sees it: tools over an in-memory MCP client."""

import pytest
from mcp import Client

from ops_graph.ingest import ingest
from ops_graph.rag import HashingEmbedder
from ops_graph.reconcile import reconcile
from ops_graph.server import create_server

pytestmark = [pytest.mark.integration, pytest.mark.anyio]

TOOLS = {
    "find_asset", "impact_analysis", "root_cause_candidates", "decommission_candidates",
    "compliance_findings", "reconciliation_report", "describe_schema", "read_only_cypher", "search_docs",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(neo4j_driver, data_copy):
    ingest(neo4j_driver, data_copy, "b1")
    reconcile(neo4j_driver)
    return create_server(lambda: neo4j_driver, HashingEmbedder)


def text(result):
    return " ".join(c.text for c in result.content if hasattr(c, "text"))


async def test_tools_are_listed_with_schemas_and_read_only(server):
    async with Client(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == TOOLS
    assert all(t.annotations.read_only_hint for t in tools.values())
    assert "asset" in tools["impact_analysis"].input_schema["properties"]


async def test_impact_by_hostname_spelling(server):
    async with Client(server) as client:
        result = await client.call_tool("impact_analysis", {"asset": "db01.par"})
    assert not result.is_error
    data = result.structured_content
    assert data["asset"] == "CI00301"
    assert data["impacted_count"] == 9


async def test_ambiguous_name_is_refused_with_candidates(server):
    async with Client(server) as client:
        result = await client.call_tool("root_cause_candidates", {"asset": "app01"})
    assert result.is_error
    assert "CI00201" in text(result) and "CI00203" in text(result)


async def test_unknown_name_is_refused(server):
    async with Client(server) as client:
        result = await client.call_tool("impact_analysis", {"asset": "does-not-exist"})
    assert result.is_error
    assert "No asset matches" in text(result)


async def test_read_only_cypher_answers_and_refuses(server):
    async with Client(server) as client:
        ok = await client.call_tool("read_only_cypher", {"query": "MATCH (s:Server {in_cmdb: false}) RETURN s.name AS name"})
        load = await client.call_tool("read_only_cypher", {"query": "LOAD CSV FROM 'http://x' AS r RETURN r"})
        write = await client.call_tool("read_only_cypher", {"query": "MATCH (s:Server) DETACH DELETE s"})
    assert ok.structured_content["rows"] == [{"name": "mq01.par.acme.local"}]
    assert load.is_error and "LOAD CSV" in text(load)
    assert write.is_error and "AccessMode" in text(write)
