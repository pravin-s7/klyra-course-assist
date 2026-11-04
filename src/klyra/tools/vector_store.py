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
import math
import os
import re
import uuid
from collections import Counter
from functools import lru_cache
from typing import Any, Iterable

from klyra.core.retry import call_with_retry
from klyra.utils import fs

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    VectorParams,
)

_COLLECTION_SUFFIXES = ("-syllabus", "-pyq", "-parsed", "-images")

_BM25_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "he",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "to",
        "was",
        "were",
        "will",
        "with",
        "what",
        "which",
        "how",
        "why",
        "when",
        "where",
        "who",
    }
)


def _validate_collection_subject(subject: str) -> str:
    return fs.validate_subject(subject)


def syllabus_collection_name(subject: str) -> str:
    return f"{_validate_collection_subject(subject)}-syllabus"


def pyq_collection_name(subject: str) -> str:
    return f"{_validate_collection_subject(subject)}-pyq"


def parsed_collection_name(subject: str) -> str:
    return f"{_validate_collection_subject(subject)}-parsed"


def images_collection_name(subject: str) -> str:
    return f"{_validate_collection_subject(subject)}-images"


def extract_subject_from_collection(collection_name: str) -> str | None:
    """Extract the validated `<subject>` prefix from a standard Klyra collection name."""
    for suffix in _COLLECTION_SUFFIXES:
        if collection_name.endswith(suffix) and len(collection_name) > len(suffix):
            candidate = collection_name[: -len(suffix)]
            try:
                return fs.validate_subject(candidate)
            except Exception:
                return None
    return None


def _build_conditions(where: dict[str, Any]) -> tuple[list[Any], list[Any]]:
    """Recursively convert a metadata `where` dict into Qdrant `must` and `should` clauses.

    Supports:
      - Flat key-value pairs: `{"week": 1, "source_file": "lec1.pdf"}`
      - Multi-value lists/tuples/sets: `{"week": [1, 2]}` -> `MatchAny`
      - Nested logical operators: `{"$and": [{...}, {...}]}` and `{"$or": [{...}, {...}]}`
    """
    must_clauses: list[Any] = []
    should_clauses: list[Any] = []

    for key, val in where.items():
        if key == "$and":
            if isinstance(val, (list, tuple)):
                for sub in val:
                    if isinstance(sub, dict):
                        sub_must, sub_should = _build_conditions(sub)
                        must_clauses.extend(sub_must)
                        if sub_should:
                            must_clauses.append(Filter(should=sub_should))
        elif key == "$or":
            if isinstance(val, (list, tuple)):
                for sub in val:
                    if isinstance(sub, dict):
                        sub_must, sub_should = _build_conditions(sub)
                        if len(sub_must) == 1 and not sub_should:
                            should_clauses.append(sub_must[0])
                        elif sub_must or sub_should:
                            should_clauses.append(
                                Filter(
                                    must=sub_must or None,
                                    should=sub_should or None,
                                )
                            )
        else:
            if val is None:
                continue
            if isinstance(val, (list, tuple, set)):
                values = list(val)
                if len(values) == 1:
                    must_clauses.append(
                        FieldCondition(key=key, match=MatchValue(value=values[0]))
                    )
                elif values:
                    must_clauses.append(
                        FieldCondition(key=key, match=MatchAny(any=values))
                    )
            else:
                must_clauses.append(FieldCondition(key=key, match=MatchValue(value=val)))

    return must_clauses, should_clauses


def _build_filter(where: dict[str, Any] | None, subject: str | None = None) -> Filter | None:
    """Build a Qdrant `Filter` from a `where` dictionary (supporting `$and`, `$or`,
    and list values), optionally enforcing explicit `subject` payload matching."""
    effective_where: dict[str, Any] = {}
    if where:
        effective_where.update(where)
    if subject:
        effective_where["subject"] = subject
    if not effective_where:
        return None

    must_clauses, should_clauses = _build_conditions(effective_where)
    if not must_clauses and not should_clauses:
        return None
    return Filter(
        must=must_clauses or None,
        should=should_clauses or None,
    )


def _is_subject_compatible(payload: dict[str, Any] | None, expected_subject: str | None) -> bool:
    """Ensure a retrieved record does not belong to a different course/subject."""
    if not expected_subject or not payload:
        return True
    rec_subj = payload.get("subject")
    if rec_subj is None:
        return True
    return str(rec_subj) == str(expected_subject)


def _tokenize_for_bm25(text: str) -> list[str]:
    """Tokenize text for BM25 while preserving technical identifiers (`3nf`, `bcnf`,
    `strtok`, `chmod`, `4755`, `w03-q02`, `c++`)."""
    if not text:
        return []
    raw_tokens = re.findall(r"[A-Za-z0-9_+#.-]+", text.lower())
    tokens: list[str] = []
    for tok in raw_tokens:
        cleaned = tok.strip("._-")
        if not cleaned or cleaned in _BM25_STOPWORDS:
            continue
        tokens.append(cleaned)
        # Also split hyphenated/snake_case compound identifiers so both exact and sub-tokens match
        if "-" in cleaned or "_" in cleaned:
            for part in re.split(r"[-_]+", cleaned):
                part = part.strip(".")
                if part and part not in _BM25_STOPWORDS and part != cleaned:
                    tokens.append(part)
    return tokens


def compute_bm25_scores(
    query_text: str,
    documents: list[str],
    *,
    k1: float = 1.5,
    b: float = 0.75,
) -> list[float]:
    """Compute Okapi BM25 scores for `documents` against `query_text`, with an
    additional lexical boost for exact technical identifiers / acronyms."""
    n_docs = len(documents)
    if n_docs == 0:
        return []

    query_tokens = _tokenize_for_bm25(query_text)
    if not query_tokens:
        return [0.0] * n_docs

    doc_token_lists = [_tokenize_for_bm25(doc) for doc in documents]
    doc_lengths = [len(tokens) for tokens in doc_token_lists]
    avgdl = (sum(doc_lengths) / n_docs) if n_docs > 0 else 1.0
    if avgdl <= 0:
        avgdl = 1.0

    # Document frequencies for query terms
    unique_query_terms = set(query_tokens)
    df: dict[str, int] = {term: 0 for term in unique_query_terms}
    doc_counters: list[Counter[str]] = []
    for tokens in doc_token_lists:
        counts = Counter(tokens)
        doc_counters.append(counts)
        for term in unique_query_terms:
            if counts.get(term, 0) > 0:
                df[term] += 1

    # Extract high-signal technical tokens (acronyms like BCNF, identifiers with digits/underscores)
    raw_identifiers = [
        tok.strip(".,;:!?()[]{}\"'")
        for tok in query_text.split()
        if len(tok.strip(".,;:!?()[]{}\"'")) >= 2
        and (
            any(ch.isupper() for ch in tok[1:])
            or tok.isupper()
            or "_" in tok
            or any(ch.isdigit() for ch in tok)
        )
    ]

    scores: list[float] = []
    for idx, counts in enumerate(doc_counters):
        dl = doc_lengths[idx]
        score = 0.0
        for term in query_tokens:
            tf = counts.get(term, 0)
            if tf == 0:
                continue
            n_q = df.get(term, 0)
            idf = math.log(1.0 + (n_docs - n_q + 0.5) / (n_q + 0.5))
            denom = tf + k1 * (1.0 - b + b * (dl / avgdl))
            score += idf * ((tf * (k1 + 1.0)) / denom)

        if score > 0 and raw_identifiers:
            doc_lower = documents[idx].lower()
            for ident in raw_identifiers:
                if ident.lower() in doc_lower:
                    score *= 1.25

        scores.append(score)

    return scores


def reciprocal_rank_fusion(
    dense_ranked_ids: list[str],
    sparse_ranked_ids: list[str],
    sparse_scores: dict[str, float],
    *,
    rrf_k: int = 60,
    dense_weight: float = 1.0,
    sparse_weight: float = 1.0,
) -> dict[str, float]:
    """Fuse dense vector rankings and sparse BM25 rankings using Reciprocal Rank Fusion (RRF)."""
    fused: dict[str, float] = {}
    for rank, doc_id in enumerate(dense_ranked_ids, start=1):
        fused[doc_id] = fused.get(doc_id, 0.0) + dense_weight / (rrf_k + rank)

    for rank, doc_id in enumerate(sparse_ranked_ids, start=1):
        bm25_val = sparse_scores.get(doc_id, 0.0)
        if bm25_val <= 0.0:
            continue
        fused[doc_id] = fused.get(doc_id, 0.0) + sparse_weight / (rrf_k + rank)

    return fused


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    url = os.environ.get("QDRANT_URL")
    if url:
        client = QdrantClient(url=url)
    else:
        qdrant_dir = os.environ.get("KLYRA_QDRANT_DIR", ".qdrant")
        os.makedirs(qdrant_dir, exist_ok=True)
        try:
            client = QdrantClient(path=qdrant_dir)
        except RuntimeError as exc:
            if "already accessed by another instance" not in str(exc):
                raise
            # A stale or suspended process may be holding the old .lock inode.
            # Unlink the stale .lock entry so a fresh lock inode can be acquired.
            lock_path = os.path.join(qdrant_dir, ".lock")
            try:
                if os.path.exists(lock_path):
                    os.unlink(lock_path)
            except OSError:
                pass
            client = QdrantClient(path=qdrant_dir)

    def _close_client():
        try:
            client.close()
        except Exception:
            pass

    atexit.register(_close_client)
    return client


def close_client() -> None:
    try:
        if get_client.cache_info().currsize > 0:
            client = get_client()
            client.close()
    except Exception:
        pass
    finally:
        get_client.cache_clear()


def _patch_google_client_options() -> None:
    """Ensure `google.api_core.client_options.ClientOptions` accepts `headers`
    so legacy ChromaDB/CrewAI `GoogleGenerativeAiEmbeddingFunction` calls never
    raise `ValueError: ClientOptions does not accept an option 'headers'`."""
    try:
        from google.api_core import client_options

        if not hasattr(client_options.ClientOptions, "headers"):
            setattr(client_options.ClientOptions, "headers", None)
    except Exception:
        pass


_patch_google_client_options()


try:
    from chromadb.api.types import EmbeddingFunction as _ChromaEmbeddingFunction
except Exception:
    _ChromaEmbeddingFunction = object  # type: ignore[assignment,misc]

DEFAULT_GOOGLE_EMBEDDING_MODEL = "gemini-embedding-2-preview"

_MODEL_ALIASES: dict[str, str] = {
    "gemini-embedding-2": "gemini-embedding-2-preview",
    "models/gemini-embedding-2": "gemini-embedding-2-preview",
    "gemini-embedding-002": "gemini-embedding-2-preview",
    "models/gemini-embedding-002": "gemini-embedding-2-preview",
}


class _GoogleGenAIEmbeddingFunction(_ChromaEmbeddingFunction):  # type: ignore[misc]
    """Multimodal & text embedding callable backed by the official `google-genai` SDK (`google.genai`).

    Uses Gemini Embedding 2 (`gemini-embedding-2-preview` / `gemini-embedding-2`) by default,
    supporting both text chunks and multimodal (text + image bytes) chunks.
    Subclasses `chromadb.api.types.EmbeddingFunction` so CrewAI's `CustomProviderSpec`
    Pydantic validation passes without errors.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str | None = None,
        task_type: str | None = None,
        api_key_env_var: str | None = None,
        dimension: int | None = None,
        **kwargs: Any,
    ) -> None:
        _ = (task_type, api_key_env_var, dimension, kwargs)
        self.api_key = (
            api_key
            or os.environ.get("GOOGLE_API_KEY")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("CHROMA_GOOGLE_GENAI_API_KEY")
        )
        if not self.api_key:
            raise ValueError(
                "Set GOOGLE_API_KEY or GEMINI_API_KEY when EMBEDDING_PROVIDER='google'."
            )
        raw_model = (
            model_name
            or os.environ.get("GOOGLE_EMBEDDING_MODEL")
            or DEFAULT_GOOGLE_EMBEDDING_MODEL
        )
        self.model_name = _MODEL_ALIASES.get(raw_model, raw_model)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            from google import genai
            from klyra.core.retry import sdk_retry_options

            self._client = genai.Client(
                api_key=self.api_key,
                http_options=sdk_retry_options(retry_attempts=2),
            )
        return self._client

    def _candidate_models(self) -> list[str]:
        models_to_try = [self.model_name]
        for fallback in (
            "gemini-embedding-2-preview",
            "models/gemini-embedding-2-preview",
            "gemini-embedding-2",
            "models/gemini-embedding-2",
        ):
            if fallback not in models_to_try:
                models_to_try.append(fallback)
        return models_to_try

    def __call__(self, input: Any) -> list[list[float]]:
        if not input:
            return []
        client = self._get_client()
        results: list[list[float]] = []
        batch_size = 100
        models_to_try = self._candidate_models()

        for i in range(0, len(input), batch_size):
            batch = [
                str(text) if str(text).strip() else " "
                for text in input[i : i + batch_size]
            ]
            last_exc: Exception | None = None
            for candidate_model in models_to_try:
                try:
                    resp = client.models.embed_content(
                        model=candidate_model,
                        contents=batch,
                    )
                    self.model_name = candidate_model
                    for emb in resp.embeddings or []:
                        results.append(list(emb.values or []))
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    if "not found" in msg or "404" in msg or "not supported" in msg:
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
        return results

    def embed_multimodal(self, items: list[dict[str, Any]]) -> list[list[float]]:
        """Embed items that may contain both `text` and `image_bytes` using
        Gemini Embedding 2 (`gemini-embedding-2-preview`) multimodal capabilities."""
        if not items:
            return []
        from google.genai import types

        client = self._get_client()
        models_to_try = self._candidate_models()
        results: list[list[float]] = []

        for item in items:
            text_val = str(item.get("text") or "").strip()
            img_bytes: bytes | None = item.get("image_bytes")
            mime_type: str = str(item.get("image_mime_type") or "image/png")

            if img_bytes:
                parts: list[Any] = []
                if text_val:
                    parts.append(types.Part.from_text(text=text_val))
                parts.append(types.Part.from_bytes(data=img_bytes, mime_type=mime_type))
                contents: Any = parts
            else:
                contents = [text_val if text_val else " "]

            last_exc: Exception | None = None
            for candidate_model in models_to_try:
                try:
                    resp = client.models.embed_content(
                        model=candidate_model,
                        contents=contents,
                    )
                    self.model_name = candidate_model
                    if resp.embeddings:
                        results.append(list(resp.embeddings[0].values or []))
                    last_exc = None
                    break
                except Exception as exc:
                    last_exc = exc
                    msg = str(exc).lower()
                    if "not found" in msg or "404" in msg or "not supported" in msg:
                        continue
                    raise
            if last_exc is not None:
                raise last_exc
        return results


@lru_cache(maxsize=1)
def get_embedding_function():
    from chromadb.utils import embedding_functions

    default_provider = (
        "google"
        if (os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))
        else "default"
    )
    provider = os.environ.get("EMBEDDING_PROVIDER", default_provider).lower()
    if provider == "openai":
        api_key = os.environ["OPENAI_API_KEY"]
        model_name = os.environ.get("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
        return embedding_functions.OpenAIEmbeddingFunction(api_key=api_key, model_name=model_name)
    if provider == "google":
        api_key = os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")
        model_name = os.environ.get("GOOGLE_EMBEDDING_MODEL", DEFAULT_GOOGLE_EMBEDDING_MODEL)
        return _GoogleGenAIEmbeddingFunction(api_key=api_key, model_name=model_name)
    if provider == "default":
        return embedding_functions.DefaultEmbeddingFunction()
    raise ValueError(
        f"Unknown EMBEDDING_PROVIDER={provider!r}. Supported: 'default' (local, "
        "no API key), 'openai' (requires OPENAI_API_KEY), or 'google' (requires GOOGLE_API_KEY)."
    )


def _collection_vector_size(client: QdrantClient, name: str) -> int | None:
    try:
        info = client.get_collection(name)
        vectors_cfg = info.config.params.vectors
        if hasattr(vectors_cfg, "size") and vectors_cfg.size:
            return int(vectors_cfg.size)  # type: ignore[arg-type]
    except Exception:
        pass
    return None


def get_or_create_collection(name: str, vector_size: int | None = None):
    client = get_client()
    if not client.collection_exists(name):
        if vector_size is None:
            emb_fn = get_embedding_function()
            sample_emb = emb_fn(["test"])[0]
            size = len(sample_emb)
        else:
            size = vector_size
        client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=size, distance=Distance.COSINE)
        )
    elif vector_size is not None:
        existing_size = _collection_vector_size(client, name)
        if existing_size is not None and existing_size != vector_size:
            # Collection was created under a different embedding model dimension;
            # preserve existing documents and re-create with the new vector_size.
            existing_records: list[ Any ] = []
            try:
                existing_records, _ = client.scroll(
                    collection_name=name,
                    limit=10000,
                    with_payload=True,
                )
            except Exception:
                existing_records = []
            client.delete_collection(collection_name=name)
            client.create_collection(
                collection_name=name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE),
            )
            if existing_records:
                try:
                    emb_fn = get_embedding_function()
                    docs_to_reembed = [
                        str((rec.payload or {}).get("_document") or " ")
                        for rec in existing_records
                    ]
                    re_embeddings = call_with_retry(lambda: emb_fn(docs_to_reembed))
                    migrated_points = [
                        PointStruct(
                            id=rec.id,
                            vector=vec,
                            payload=rec.payload or {},
                        )
                        for rec, vec in zip(existing_records, re_embeddings)
                    ]
                    client.upsert(collection_name=name, points=migrated_points)
                except Exception:
                    pass
    return name


def add_chunks(collection_name: str, chunks: Iterable[dict[str, Any]]) -> int:
    """chunks: iterable of {"text": str, "metadata": dict, "id"?: str,
    "image_bytes"?: bytes, "image_mime_type"?: str}."""
    client = get_client()
    emb_fn = get_embedding_function()
    inferred_subject = extract_subject_from_collection(collection_name)

    ids = []
    documents = []
    metadatas = []
    multimodal_items = []
    has_multimodal = False
    for chunk in chunks:
        ids.append(chunk.get("id") or str(uuid.uuid4()))
        documents.append(chunk["text"])
        metadatas.append(chunk.get("metadata") or {})
        img_bytes = chunk.get("image_bytes")
        if img_bytes:
            has_multimodal = True
        multimodal_items.append(
            {
                "text": chunk["text"],
                "image_bytes": img_bytes,
                "image_mime_type": chunk.get("image_mime_type") or "image/png",
            }
        )

    if not documents:
        get_or_create_collection(collection_name)
        return 0

    if has_multimodal and hasattr(emb_fn, "embed_multimodal"):
        embeddings = call_with_retry(lambda: emb_fn.embed_multimodal(multimodal_items))
    else:
        embeddings = call_with_retry(lambda: emb_fn(documents))
    get_or_create_collection(collection_name, vector_size=len(embeddings[0]))

    points = []
    for id_, doc, meta, emb in zip(ids, documents, metadatas, embeddings):
        meta_with_doc = meta.copy()
        if inferred_subject and "subject" not in meta_with_doc:
            meta_with_doc["subject"] = inferred_subject
        meta_with_doc["_document"] = doc
        points.append(PointStruct(id=id_, vector=emb, payload=meta_with_doc))

    client.upsert(collection_name=collection_name, points=points)
    return len(points)


def _format_searchable_text(doc: str, meta: dict[str, Any]) -> str:
    """Combine document text with key metadata fields (`concept`, `topic`, `source_file`)
    so BM25 lexical scoring matches both body text and indexed concept titles."""
    extra_parts: list[str] = []
    for key in ("concept", "topic", "source_file"):
        val = meta.get(key)
        if val:
            extra_parts.append(str(val))
    if extra_parts:
        return f"{' '.join(extra_parts)}\n{doc}"
    return doc


def query(
    collection_name: str,
    query_text: str,
    n_results: int = 5,
    where: dict | None = None,
    *,
    hybrid: bool = True,
    rrf_k: int = 60,
) -> list[dict]:
    """Query `collection_name` using hybrid search (dense cosine vector similarity
    fused with sparse BM25 lexical ranking via Reciprocal Rank Fusion) and
    strict cross-course subject isolation."""
    client = get_client()
    if not client.collection_exists(collection_name):
        return []

    inferred_subject = extract_subject_from_collection(collection_name)
    emb_fn = get_embedding_function()
    query_emb = call_with_retry(lambda: emb_fn([query_text]))[0]
    get_or_create_collection(collection_name, vector_size=len(query_emb))

    query_filter = _build_filter(where)
    dense_limit = max(n_results * 4, 20) if hybrid else n_results

    try:
        dense_hits = client.query_points(
            collection_name=collection_name,
            query=query_emb,
            limit=dense_limit,
            query_filter=query_filter,
            with_payload=True,
        ).points
    except Exception:
        dense_hits = []

    candidates: dict[str, dict[str, Any]] = {}
    dense_ranked_ids: list[str] = []

    for hit in dense_hits:
        raw_payload = hit.payload.copy() if hit.payload else {}
        if not _is_subject_compatible(raw_payload, inferred_subject):
            continue
        doc = raw_payload.pop("_document", "")
        doc_id = str(hit.id)
        dense_score = float(hit.score) if hit.score is not None else 0.0
        candidates[doc_id] = {
            "id": doc_id,
            "text": doc,
            "metadata": raw_payload,
            "dense_score": dense_score,
        }
        dense_ranked_ids.append(doc_id)

    if not hybrid:
        return [
            {
                "id": item["id"],
                "text": item["text"],
                "metadata": item["metadata"],
                "distance": item["dense_score"],
            }
            for item in list(candidates.values())[:n_results]
        ]

    # Gather additional lexical candidates from the collection so exact technical terms
    # (e.g. `BCNF`, `strtok`, `chmod 4755`) that rank outside dense top-K are still retrieved
    try:
        scroll_records, _ = client.scroll(
            collection_name=collection_name,
            scroll_filter=query_filter,
            limit=max(n_results * 10, 200),
            with_payload=True,
        )
        for rec in scroll_records or []:
            raw_payload = rec.payload.copy() if rec.payload else {}
            if not _is_subject_compatible(raw_payload, inferred_subject):
                continue
            doc_id = str(rec.id)
            if doc_id not in candidates:
                doc = raw_payload.pop("_document", "")
                candidates[doc_id] = {
                    "id": doc_id,
                    "text": doc,
                    "metadata": raw_payload,
                    "dense_score": 0.0,
                }
    except Exception:
        # Gracefully degrade if client is a lightweight mock without scroll support
        pass

    if not candidates:
        return []

    candidate_ids = list(candidates.keys())
    searchable_texts = [
        _format_searchable_text(candidates[cid]["text"], candidates[cid]["metadata"])
        for cid in candidate_ids
    ]
    bm25_raw = compute_bm25_scores(query_text, searchable_texts)
    sparse_scores = {cid: score for cid, score in zip(candidate_ids, bm25_raw)}

    sparse_ranked_ids = [
        cid
        for cid, score in sorted(
            sparse_scores.items(),
            key=lambda kv: (kv[1], candidates[kv[0]]["dense_score"]),
            reverse=True,
        )
        if score > 0.0
    ]

    fused_scores = reciprocal_rank_fusion(
        dense_ranked_ids=dense_ranked_ids,
        sparse_ranked_ids=sparse_ranked_ids,
        sparse_scores=sparse_scores,
        rrf_k=rrf_k,
    )

    # Include any candidate that had 0 BM25 score and wasn't in dense_ranked_ids
    for cid in candidate_ids:
        fused_scores.setdefault(cid, 0.0)

    ranked_ids = sorted(
        candidate_ids,
        key=lambda cid: (
            fused_scores.get(cid, 0.0),
            sparse_scores.get(cid, 0.0),
            candidates[cid]["dense_score"],
        ),
        reverse=True,
    )

    matches: list[dict[str, Any]] = []
    for cid in ranked_ids[:n_results]:
        item = candidates[cid]
        matches.append(
            {
                "id": item["id"],
                "text": item["text"],
                "metadata": item["metadata"],
                "distance": item["dense_score"] or fused_scores.get(cid, 0.0),
                "rrf_score": round(fused_scores.get(cid, 0.0), 6),
                "bm25_score": round(sparse_scores.get(cid, 0.0), 4),
            }
        )
    return matches


def delete_by_source_file(collection_name: str, source_file: str) -> None:
    client = get_client()
    if not client.collection_exists(collection_name):
        return

    query_filter = _build_filter({"source_file": source_file})
    client.delete(
        collection_name=collection_name,
        points_selector=query_filter,
    )


def get_by_metadata(collection_name: str, where: dict | None = None, limit: int = 20) -> list[dict]:
    client = get_client()
    if not client.collection_exists(collection_name):
        return []

    inferred_subject = extract_subject_from_collection(collection_name)
    query_filter = _build_filter(where)

    records, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=query_filter,
        limit=limit,
        with_payload=True,
    )

    docs = []
    for r in records:
        meta = r.payload.copy() if r.payload else {}
        if not _is_subject_compatible(meta, inferred_subject):
            continue
        doc = meta.pop("_document", "")
        docs.append({"text": doc, "metadata": meta})
    return docs


def list_distinct_metadata_values(collection_name: str, field: str, where: dict | None = None) -> list[str]:
    client = get_client()
    if not client.collection_exists(collection_name):
        return []

    inferred_subject = extract_subject_from_collection(collection_name)
    query_filter = _build_filter(where)

    records, _ = client.scroll(
        collection_name=collection_name,
        scroll_filter=query_filter,
        limit=10000,
        with_payload=True,
    )

    values = set()
    for r in records:
        if r.payload and field in r.payload:
            if not _is_subject_compatible(r.payload, inferred_subject):
                continue
            values.add(str(r.payload[field]))

    return sorted(list(values))


def delete_by_metadata(collection_name: str, where: dict) -> None:
    client = get_client()
    if not client.collection_exists(collection_name):
        return

    query_filter = _build_filter(where)
    if query_filter is None:
        return

    client.delete(
        collection_name=collection_name,
        points_selector=query_filter,
    )


def reset_collection(collection_name: str) -> None:
    try:
        get_client().delete_collection(collection_name)
    except Exception:
        pass
