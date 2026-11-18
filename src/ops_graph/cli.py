"""Command line: `ops-graph ingest | reconcile | embed | run | serve`.

Configuration comes from the environment (see .env.example):
NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD, OLLAMA_URL, EMBED_MODEL.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import os

from neo4j import Driver, GraphDatabase

from ops_graph.ingest import ingest
from ops_graph.reconcile import reconcile


def connect() -> Driver:
    driver = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "ops-graph-demo")),
        # schema hints like "label Chunk does not exist yet" are expected on a fresh database
        notifications_min_severity="OFF",
    )
    driver.verify_connectivity()
    return driver


def embed(driver: Driver, docs: Path) -> None:
    """Embed the runbooks with the local Ollama model; skip cleanly if Ollama is not there."""
    from ops_graph.rag import embed_documents, make_embedder

    try:
        embedder = make_embedder()
        embedder.embed_query("ping")
    except Exception as exc:  # connection refused, model not pulled...
        print(f"embed skipped: Ollama not usable ({type(exc).__name__}: {exc}). "
              "Graph tools work without it; search_docs needs it.")
        return
    print("embed", json.dumps(embed_documents(driver, docs, embedder)))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="ops-graph", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, help_text in [
        ("ingest", "stage 1: load the source files as raw SourceRecords"),
        ("run", "ingest, reconcile, then embed the runbooks"),
    ]:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--data", type=Path, default=Path("data"), help="folder with cmdb.csv, monitoring.json, services.yaml")
        p.add_argument("--batch", help="batch id (default: current UTC timestamp)")
    sub.add_parser("reconcile", help="stage 2: rebuild the canonical graph from the live SourceRecords")
    p = sub.add_parser("embed", help="chunk, embed and link the runbooks (needs Ollama)")
    p.add_argument("--docs", type=Path, default=Path("data/runbooks"))
    sub.add_parser("serve", help="run the MCP server on stdio")
    args = parser.parse_args(argv)

    if args.command == "serve":
        from ops_graph.server import serve

        serve()
        return

    with connect() as driver:
        if args.command in ("ingest", "run"):
            print("ingest", json.dumps(ingest(driver, args.data, args.batch)))
        if args.command in ("reconcile", "run"):
            print("reconcile", json.dumps(reconcile(driver)))
        if args.command == "embed":
            embed(driver, args.docs)
        if args.command == "run":
            embed(driver, args.data / "runbooks")


if __name__ == "__main__":
    main()
