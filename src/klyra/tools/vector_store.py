"""Thin wrapper around a local or remote Qdrant instance.

We talk to Qdrant directly (rather than going through CrewAI's managed Knowledge
system) so we get explicit control over per-subject collections and metadata filtering:

    <subject>-parsed    normalized text chunks awaiting syllabus indexing
    <subject>-syllabus  indexed {week, concept, source_file} entries
    <subject>-pyq       previous-year-question chunks
    <subject>-images    extracted diagrams {week, source_file, page, image_path}
"""
from __future__ import annotations

import atexit
import os
import uuid
from functools import lru_cache
from typing import Any, Iterable

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchValue, PointStruct, VectorParams


def syllabus_collection_name(subject: str) -> str:
    return f"{subject}-syllabus"


def pyq_collection_name(subject: str) -> str:
    return f"{subject}-pyq"


def parsed_collection_name(subject: str) -> str:
    return f"{subject}-parsed"


def images_collection_name(subject: str) -> str:
    return f"{subject}-images"


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    url = os.environ.get("QDRANT_URL")
    if url:
        client = QdrantClient(url=url)
    else:
        qdrant_dir = os.environ.get("KLYRA_QDRANT_DIR", ".qdrant")
        os.makedirs(qdrant_dir, exist_ok=True)
        client = QdrantClient(path=qdrant_dir)
        
    def _close_client():
        try:
            client.close()
        except Exception:
            pass
            
    atexit.register(_close_client)
    return client


@lru_cache(maxsize=1)
def get_embedding_function():
    from chromadb.utils import embedding_functions

    provider = os.environ.get("EMBEDDING_PROVIDER", "default").lower()
    if provider == "openai":
        api_key = os.environ["OPENAI_API_KEY"]
        model_name = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        return embedding_functions.OpenAIEmbeddingFunction(api_key=api_key, model_name=model_name)
    if provider == "google":
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        model_name = os.environ.get("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")
        return embedding_functions.GoogleGenerativeAiEmbeddingFunction(api_key=api_key, model_name=model_name)
    if provider == "default":
        return embedding_functions.DefaultEmbeddingFunction()
    raise ValueError(
        f"Unknown EMBEDDING_PROVIDER={provider!r}. Supported: 'default' (local, "
        "no API key), 'openai' (requires OPENAI_API_KEY), or 'google' (requires GOOGLE_API_KEY)."
    )


def get_or_create_collection(name: str):
    client = get_client()
    if not client.collection_exists(name):
        emb_fn = get_embedding_function()
        sample_emb = emb_fn(["test"])[0]
        size = len(sample_emb)
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE)
        )
    return name


def add_chunks(collection_name: str, chunks: Iterable[dict[str, Any]]) -> int:
    """chunks: iterable of {"text": str, "metadata": dict, "id"?: str}."""
    client = get_client()
    get_or_create_collection(collection_name)
    emb_fn = get_embedding_function()
    
    ids = []
    documents = []
    metadatas = []
    for chunk in chunks:
        ids.append(chunk.get("id") or str(uuid.uuid4()))
        documents.append(chunk["text"])
        metadatas.append(chunk.get("metadata") or {})
        
    if not documents:
        return 0
        
    embeddings = emb_fn(documents)
    
    points = []
    for id_, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
        meta_with_doc = meta.copy()
        meta_with_doc["_document"] = doc
        points.append(PointStruct(id=id_, vector=emb, payload=meta_with_doc))
        
    client.upsert(collection_name=collection_name, points=points)
    return len(points)


def query(
    collection_name: str, query_text: str, n_results: int = 5, where: dict | None = None
) -> list[dict]:
    client = get_client()
    if not client.collection_exists(collection_name):
        return []
    
    emb_fn = get_embedding_function()
    query_emb = emb_fn([query_text])[0]
    
    query_filter = None
    if where:
        must_conditions = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in where.items()]
        query_filter = Filter(must=must_conditions)
        
    results = client.search(
        collection_name=collection_name,
        query_vector=query_emb,
        limit=n_results,
        query_filter=query_filter,
        with_payload=True
    )
    
    matches = []
    for hit in results:
        meta = hit.payload.copy() if hit.payload else {}
        doc = meta.pop("_document", "")
        matches.append({"id": str(hit.id), "text": doc, "metadata": meta, "distance": hit.score})
    return matches


def get_by_metadata(collection_name: str, where: dict | None = None, limit: int = 20) -> list[dict]:
    client = get_client()
    if not client.collection_exists(collection_name):
        return []
    
    query_filter = None
    if where:
        must_conditions = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in where.items()]
        query_filter = Filter(must=must_conditions)
        
    records, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=query_filter,
        limit=limit,
        with_payload=True
    )
    
    docs = []
    for r in records:
        meta = r.payload.copy() if r.payload else {}
        doc = meta.pop("_document", "")
        docs.append({"text": doc, "metadata": meta})
    return docs


def list_distinct_metadata_values(collection_name: str, field: str, where: dict | None = None) -> list[str]:
    client = get_client()
    if not client.collection_exists(collection_name):
        return []
        
    query_filter = None
    if where:
        must_conditions = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in where.items()]
        query_filter = Filter(must=must_conditions)
        
    records, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=query_filter,
        limit=10000,
        with_payload=True
    )
    
    values = set()
    for r in records:
        if r.payload and field in r.payload:
            values.add(str(r.payload[field]))
            
    return sorted(list(values))


def delete_by_metadata(collection_name: str, where: dict) -> None:
    client = get_client()
    if not client.collection_exists(collection_name):
        return
        
    must_conditions = [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in where.items()]
    query_filter = Filter(must=must_conditions)
    
    client.delete(
        collection_name=collection_name,
        points_selector=query_filter
    )


def reset_collection(collection_name: str) -> None:
    try:
        get_client().delete_collection(collection_name)
    except Exception:
        pass
