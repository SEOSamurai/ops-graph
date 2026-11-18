"""GraphRAG over the runbooks: vector search to find the passage, the graph to explain it.

    runbooks/*.md ──chunk by "## " heading──► (:Document)-[:HAS_CHUNK]->(:Chunk {text, embedding})
                                                                │
                                         entity linking ──► (:Chunk)-[:MENTIONS]->(:Asset)

A question is embedded with the same model, the vector index returns the
closest chunks, and our own Cypher (RETRIEVAL_QUERY) walks from each chunk
to the assets it mentions: their live alerts, what they depend on, what
depends on them. The agent gets the passage *and* the current state of the
things it talks about, which plain vector RAG cannot give.

Retrieval uses neo4j-graphrag's VectorCypherRetriever. Embeddings come from
a local Ollama model (nomic-embed-text by default): no document leaves the
machine. Entity linking reuses the hostname grammar from normalize.py, plus
exact names for applications and databases; an ambiguous spelling ("app01"
with no site) links to nothing rather than to the wrong server.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from neo4j import Driver, ManagedTransaction
from neo4j_graphrag.embeddings.base import Embedder
from neo4j_graphrag.retrievers import VectorCypherRetriever
from neo4j_graphrag.types import RetrieverResultItem

from ops_graph.normalize import parse_host
from ops_graph.queries import _plain

INDEX_NAME = "chunk_embedding"


# --- embedders -------------------------------------------------------------------------


class PrefixedEmbedder(Embedder):
    """nomic-embed-text expects task prefixes: 'search_query: ' and 'search_document: '."""

    def __init__(self, inner: Embedder, query_prefix: str = "", document_prefix: str = "") -> None:
        super().__init__()
        self.inner, self.query_prefix, self.document_prefix = inner, query_prefix, document_prefix

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed_query(self.query_prefix + text)

    def embed_document(self, text: str) -> list[float]:
        return self.inner.embed_query(self.document_prefix + text)


def make_embedder() -> Embedder:
    """Local Ollama embedder from OLLAMA_URL / EMBED_MODEL."""
    from neo4j_graphrag.embeddings import OllamaEmbeddings

    model = os.environ.get("EMBED_MODEL", "nomic-embed-text")
    inner = OllamaEmbeddings(model=model, host=os.environ.get("OLLAMA_URL", "http://localhost:11434"))
    if "nomic" in model:
        return PrefixedEmbedder(inner, "search_query: ", "search_document: ")
    return inner


def _embed_document(embedder: Embedder, text: str) -> list[float]:
    return embedder.embed_document(text) if hasattr(embedder, "embed_document") else embedder.embed_query(text)


# --- chunking --------------------------------------------------------------------------


@dataclass(frozen=True)
class Chunk:
    id: str
    heading: str
    text: str

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()


def chunk_markdown(path: Path, doc_id: str) -> tuple[str, list[Chunk]]:
    """Split a runbook on '## ' headings. Each chunk carries the document title for context."""
    lines = path.read_text(encoding="utf-8").splitlines()
    title = next((l[2:].strip() for l in lines if l.startswith("# ")), path.stem)
    sections: list[tuple[str, list[str]]] = [("Overview", [])]
    for line in lines:
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif not line.startswith("# "):
            sections[-1][1].append(line)
    chunks = []
    for heading, body in sections:
        text = "\n".join(body).strip()
        if text:
            chunks.append(Chunk(f"{doc_id}#{len(chunks)}", heading, f"{title} / {heading}\n{text}"))
    return title, chunks


# --- entity linking --------------------------------------------------------------------

_HOST_TOKEN = re.compile(r"\b[A-Za-z][A-Za-z0-9_]*(?:[-.][A-Za-z0-9_]+)*\b")


def find_mentions(text: str, named: dict[str, str], servers: list[dict[str, str]]) -> set[str]:
    """Ids of the assets a passage talks about.

    `named`: application/database name -> id, matched as whole words.
    `servers`: [{id, host_key}], matched through the hostname grammar; a
    spelling without a site that fits two servers links to neither.
    """
    found = {
        asset_id
        for name, asset_id in named.items()
        if re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text, re.IGNORECASE)
    }
    for token in _HOST_TOKEN.findall(text):
        if not any(c.isdigit() for c in token):
            continue
        try:
            key = parse_host(token)
        except ValueError:
            continue
        if key.number is None:
            continue
        if key.site:
            hits = [s["id"] for s in servers if s["host_key"] == str(key)]
        else:
            hits = [s["id"] for s in servers if s["host_key"].endswith("/" + key.name)]
        if len(hits) == 1:
            found.add(hits[0])
    return found


def relink_mentions_tx(tx: ManagedTransaction) -> int:
    """Rebuild every Chunk-[:MENTIONS]->Asset edge from the current entities.

    Called at the end of each reconciliation, so a link follows its asset when
    the asset's identity changes (a shadow server registered in the CMDB).
    """
    named = {r["name"]: r["id"] for r in tx.run("MATCH (a:Application|Database) RETURN a.name AS name, a.id AS id")}
    servers = [r.data() for r in tx.run("MATCH (s:Server) RETURN s.id AS id, s.host_key AS host_key")]
    chunks = [r.data() for r in tx.run("MATCH (c:Chunk) RETURN c.id AS id, c.text AS text")]
    tx.run("MATCH (:Chunk)-[m:MENTIONS]->() DELETE m")
    links = [{"chunk": c["id"], "asset": a} for c in chunks for a in sorted(find_mentions(c["text"], named, servers))]
    tx.run(
        "UNWIND $links AS l MATCH (c:Chunk {id: l.chunk}) MATCH (a:Asset {id: l.asset}) CREATE (c)-[:MENTIONS]->(a)",
        links=links,
    )
    return len(links)


# --- indexing --------------------------------------------------------------------------


def ensure_vector_index(driver: Driver, dimensions: int) -> None:
    """Create the vector index; if the embedding model changed size, rebuild it and force re-embedding."""
    driver.execute_query("CREATE CONSTRAINT chunk_id IF NOT EXISTS FOR (c:Chunk) REQUIRE c.id IS UNIQUE")
    driver.execute_query("CREATE CONSTRAINT document_path IF NOT EXISTS FOR (d:Document) REQUIRE d.path IS UNIQUE")
    rows = driver.execute_query(
        "SHOW VECTOR INDEXES YIELD name, options WHERE name = $name RETURN options", name=INDEX_NAME
    ).records
    if rows and rows[0]["options"]["indexConfig"]["vector.dimensions"] != dimensions:
        driver.execute_query(f"DROP INDEX {INDEX_NAME}")
        driver.execute_query("MATCH (c:Chunk) REMOVE c.hash, c.embedding")
        rows = []
    if not rows:
        driver.execute_query(
            f"CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS FOR (c:Chunk) ON c.embedding "
            "OPTIONS {indexConfig: {`vector.dimensions`: $dims, `vector.similarity_function`: 'cosine'}}",
            dims=dimensions,
        )


def embed_documents(driver: Driver, docs_dir: Path, embedder: Embedder) -> dict[str, int]:
    """Chunk, embed (only what changed) and link every runbook. Re-running is cheap and safe."""
    ensure_vector_index(driver, len(embedder.embed_query("dimension probe")))
    known = {r["id"]: r["hash"] for r in driver.execute_query("MATCH (c:Chunk) RETURN c.id AS id, c.hash AS hash").records}

    documents, chunk_rows, embedded = [], [], 0
    for path in sorted(docs_dir.glob("*.md")):
        doc_id = path.name
        title, chunks = chunk_markdown(path, doc_id)
        documents.append({"path": doc_id, "title": title, "chunks": [c.id for c in chunks]})
        for c in chunks:
            row = {"id": c.id, "doc": doc_id, "heading": c.heading, "text": c.text, "hash": c.content_hash, "embedding": None}
            if known.get(c.id) != c.content_hash:
                row["embedding"] = _embed_document(embedder, c.text)
                embedded += 1
            chunk_rows.append(row)

    def write(tx: ManagedTransaction) -> int:
        tx.run(
            "MATCH (d:Document) WHERE NOT d.path IN $paths "
            "OPTIONAL MATCH (d)-[:HAS_CHUNK]->(c) DETACH DELETE d, c",
            paths=[d["path"] for d in documents],
        )
        tx.run(
            """
            UNWIND $docs AS doc
            MERGE (d:Document {path: doc.path}) SET d.title = doc.title
            WITH d, doc
            OPTIONAL MATCH (d)-[:HAS_CHUNK]->(old:Chunk) WHERE NOT old.id IN doc.chunks
            DETACH DELETE old
            """,
            docs=documents,
        )
        tx.run(
            """
            UNWIND $rows AS row
            MATCH (d:Document {path: row.doc})
            MERGE (c:Chunk {id: row.id})
            SET c.heading = row.heading, c.text = row.text, c.hash = row.hash,
                c.embedding = coalesce(row.embedding, c.embedding)
            MERGE (d)-[:HAS_CHUNK]->(c)
            """,
            rows=chunk_rows,
        )
        return relink_mentions_tx(tx)

    with driver.session() as session:
        mentions = session.execute_write(write)
    return {"documents": len(documents), "chunks": len(chunk_rows), "embedded": embedded, "mentions": mentions}


# --- retrieval -------------------------------------------------------------------------

# Runs after the vector search, once per hit (`node`, `score` are bound by the retriever).
RETRIEVAL_QUERY = """
MATCH (doc:Document)-[:HAS_CHUNK]->(node)
OPTIONAL MATCH (node)-[:MENTIONS]->(a:Asset)
CALL (a) {
  OPTIONAL MATCH (alert:Alert)-[:RAISED_ON]->(a)
  RETURN collect(alert {.id, .severity, .message}) AS alerts
}
CALL (a) {
  OPTIONAL MATCH (a)-[:DEPENDS_ON|RUNS_ON|HOSTED_ON]->(dependency)
  RETURN collect(DISTINCT dependency.name) AS depends_on
}
CALL (a) {
  OPTIONAL MATCH (dependent)-[:DEPENDS_ON|RUNS_ON|HOSTED_ON]->(a)
  RETURN collect(DISTINCT dependent.name) AS used_by
}
WITH doc, node, score,
     collect(CASE WHEN a IS NULL THEN null
                  ELSE {id: a.id, name: a.name, alerts: alerts, depends_on: depends_on, used_by: used_by} END) AS assets
RETURN doc.path AS source, doc.title AS document, node.heading AS heading, node.text AS text, score, assets
"""


def search_docs(driver: Driver, question: str, embedder: Embedder, k: int = 3) -> list[dict[str, Any]]:
    retriever = VectorCypherRetriever(
        driver,
        INDEX_NAME,
        RETRIEVAL_QUERY,
        embedder,
        result_formatter=lambda record: RetrieverResultItem(content=_plain(record.data())),
    )
    result = retriever.search(query_text=question, top_k=max(1, min(int(k), 10)))
    return [item.content for item in result.items]


class HashingEmbedder(Embedder):
    """Deterministic bag-of-words embedder, for tests and offline demos (no semantics)."""

    def __init__(self, dimensions: int = 256) -> None:
        super().__init__()
        self.dimensions = dimensions

    def embed_query(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for word in re.findall(r"[a-z0-9]+", text.lower()):
            vector[int(hashlib.md5(word.encode()).hexdigest(), 16) % self.dimensions] += 1.0
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]
