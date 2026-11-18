import pytest

from ops_graph.guard import CypherRejected, check_cypher, run_read_only_cypher


@pytest.mark.parametrize(
    "query",
    [
        "MATCH (s:Server) RETURN s.name",
        "MATCH p = (a)-[:DEPENDS_ON*1..3]->(b) RETURN p LIMIT 5",
        "CALL db.labels() YIELD label RETURN label",
        "RETURN 'http://example.com' AS url",  # a URL in a string is just data
        "MATCH (n) // a comment\nRETURN count(n)",
    ],
)
def test_ordinary_reads_pass(query):
    check_cypher(query)


@pytest.mark.parametrize(
    ("query", "why"),
    [
        ("LOAD CSV FROM 'http://attacker.example/x.csv' AS row RETURN row", "LOAD CSV"),
        ("load   csv with headers from 'file:///etc/passwd' AS r RETURN r", "LOAD CSV"),
        ("LOAD/**/CSV FROM 'http://x' AS r RETURN r", "LOAD CSV"),  # comment between keywords
        ("LOAD // hidden\n CSV FROM 'http://x' AS r RETURN r", "LOAD CSV"),
        # a fake comment opener inside a string must not swallow the keyword
        ("RETURN '/*' AS a LOAD/**/CSV FROM 'http://x' AS r RETURN '*/'", "LOAD CSV"),
        ("CALL dbms.components() YIELD name RETURN name", "dbms"),
        ("CALL apoc.load.json('http://x') YIELD value RETURN value", "apoc"),
        ("MATCH (n) CALL (n) { RETURN 1 AS one } IN TRANSACTIONS RETURN one", "IN TRANSACTIONS"),
        ("   ", "empty"),
    ],
)
def test_forbidden_constructs_are_refused(query, why):
    with pytest.raises(CypherRejected, match=why):
        check_cypher(query)


# --- against the database --------------------------------------------------------


@pytest.mark.integration
def test_writes_are_refused_by_neo4j_itself(neo4j_driver):
    from neo4j.exceptions import ClientError

    with pytest.raises(ClientError) as exc:
        run_read_only_cypher(neo4j_driver, "CREATE (n:Hacked) RETURN n")
    assert exc.value.code == "Neo.ClientError.Statement.AccessMode"
    assert neo4j_driver.execute_query("MATCH (n:Hacked) RETURN count(n)").records[0][0] == 0


@pytest.mark.integration
def test_rows_are_capped(neo4j_driver):
    result = run_read_only_cypher(neo4j_driver, "UNWIND range(1, 1000) AS x RETURN x")
    assert result["row_count"] == 200
    assert result["truncated"] is True
    small = run_read_only_cypher(neo4j_driver, "UNWIND range(1, 3) AS x RETURN x")
    assert small == {"rows": [{"x": 1}, {"x": 2}, {"x": 3}], "row_count": 3, "truncated": False, "row_limit": 200}


@pytest.mark.integration
def test_long_queries_are_cancelled(neo4j_driver):
    from neo4j.exceptions import Neo4jError

    with pytest.raises(Neo4jError) as exc:
        run_read_only_cypher(
            neo4j_driver,
            "UNWIND range(1, 100000) AS a UNWIND range(1, 100000) AS b RETURN count(*)",
            timeout_seconds=1.0,
        )
    assert exc.value.code == "Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration"
