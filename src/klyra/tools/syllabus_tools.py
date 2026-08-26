"""Tools for building and querying the per-subject syllabus index.

Concept extraction itself is an LLM reasoning task performed by the Syllabus
Indexer Agent; these tools only handle I/O (fetch parsed text, persist
structured entries, search) so agents never hand-roll vector-store access.
"""
from __future__ import annotations

from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.tools import vector_store
from klyra.utils import fs


def _week_source_where(week: int | None, source_file: str | None) -> dict | None:
    conditions = []
    if week is not None:
        conditions.append({"week": week})
    if source_file is not None:
        conditions.append({"source_file": source_file})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


class ListSourceFilesInput(BaseModel):
    subject: str
    week: int | None = Field(default=None, description="Restrict to a specific week (1-12)")


class ListSourceFilesTool(BaseTool):
    name: str = "list_source_files"
    description: str = (
        "List the exact indexed source_file paths for a subject (optionally "
        "filtered by week). Separates syllabus/parsed files from PYQ files. "
        "Use this to resolve a fuzzy reference like 'from Lecture 1.5' to an "
        "exact source_file before passing source_file= to search_syllabus/"
        "search_pyq/fetch_pyq_chunks. If syllabus files are empty but PYQ "
        "files exist, generate from the PYQ bank - do not guess the course "
        "from the subject slug."
    )
    args_schema: Type[BaseModel] = ListSourceFilesInput

    def _run(self, subject: str, week: int | None = None) -> str:
        try:
            where = {"week": week} if week is not None else None
            files = vector_store.list_distinct_metadata_values(
                vector_store.parsed_collection_name(subject), "source_file", where=where
            )
            pyq_files = vector_store.list_distinct_metadata_values(
                vector_store.pyq_collection_name(subject), "source_file"
            )
            if not files and not pyq_files:
                return "No indexed source files found - has this subject been ingested yet?"
            lines = []
            if files:
                lines.append("Syllabus/parsed files:")
                lines.extend(f"- {f}" for f in files)
            else:
                lines.append("No syllabus/parsed files indexed.")
            if pyq_files:
                lines.append("PYQ files:")
                lines.extend(f"- {f}" for f in pyq_files)
            else:
                lines.append("No PYQ files indexed.")
            return "\n".join(lines)
        finally:
            vector_store.close_client()


class FetchParsedChunksInput(BaseModel):
    subject: str
    week: int | None = Field(default=None, description="1-12, or omit for 'additional' content")
    limit: int = Field(default=20, description="Max chunks to return")


class FetchParsedChunksTool(BaseTool):
    name: str = "fetch_parsed_chunks"
    description: str = (
        "Fetch normalized text chunks already parsed for a subject, filtered "
        "by week (1-12) or 'additional' content when week is omitted, so you "
        "can extract concepts from them."
    )
    args_schema: Type[BaseModel] = FetchParsedChunksInput

    def _run(self, subject: str, week: int | None = None, limit: int = 20) -> str:
        where = {"week": week} if week is not None else {"category": "additional"}
        matches = vector_store.get_by_metadata(
            vector_store.parsed_collection_name(subject), where=where, limit=limit
        )
        if not matches:
            scope = f"week {week}" if week is not None else "additional"
            return f"No parsed content found for {scope} (run parse_subject_documents first if unexpected)."
        return "\n---\n".join(
            f"[source_file: {m['metadata'].get('source_file')}]\n{m['text']}" for m in matches
        )


class ReportSyllabusGapsInput(BaseModel):
    subject: str


class ReportSyllabusGapsTool(BaseTool):
    name: str = "report_syllabus_gaps"
    description: str = "List which week_1..week_12 folders for a subject are missing or empty."
    args_schema: Type[BaseModel] = ReportSyllabusGapsInput

    def _run(self, subject: str) -> str:
        gaps = fs.missing_weeks(subject)
        if not gaps:
            return "No gaps - all 12 weeks have source material."
        return "Missing/empty weeks: " + ", ".join(str(w) for w in gaps)


class SyllabusEntry(BaseModel):
    week: int
    concept: str
    source_file: str


class WriteSyllabusEntriesInput(BaseModel):
    subject: str
    entries: list[SyllabusEntry]


class WriteSyllabusEntriesTool(BaseTool):
    name: str = "write_syllabus_entries"
    description: str = (
        "Persist extracted {week, concept, source_file} entries into the "
        "subject's syllabus index (vector store) so they can be searched later."
    )
    args_schema: Type[BaseModel] = WriteSyllabusEntriesInput

    def _run(self, subject: str, entries: list) -> str:
        chunks = []
        for entry in entries:
            entry = entry if isinstance(entry, dict) else entry.model_dump()
            chunks.append(
                {
                    "text": entry["concept"],
                    "metadata": {
                        "subject": subject,
                        "week": entry["week"],
                        "source_file": entry["source_file"],
                        "concept": entry["concept"],
                    },
                }
            )
        count = vector_store.add_chunks(vector_store.syllabus_collection_name(subject), chunks)
        return f"Wrote {count} syllabus entries for {subject}."


class SyllabusSearchInput(BaseModel):
    subject: str
    query: str = Field(..., description="Concept/topic text to search for")
    week: int | None = Field(default=None, description="Restrict to a specific week (1-12)")
    source_file: str | None = Field(
        default=None, description="Exact source_file from list_source_files, to restrict to one document"
    )
    top_k: int = Field(default=5)


class SyllabusSearchTool(BaseTool):
    name: str = "search_syllabus"
    description: str = (
        "Search the subject's indexed syllabus for concepts matching a query. "
        "Use this to ground question content and to check whether a "
        "concept/week is actually in scope (no good match => likely "
        "out-of-syllabus). Pass source_file (see list_source_files) to "
        "restrict to one specific lecture/document."
    )
    args_schema: Type[BaseModel] = SyllabusSearchInput

    def _run(
        self,
        subject: str,
        query: str,
        week: int | None = None,
        source_file: str | None = None,
        top_k: int = 5,
    ) -> str:
        where = _week_source_where(week, source_file)
        matches = vector_store.query(
            vector_store.syllabus_collection_name(subject), query, n_results=top_k, where=where
        )
        if not matches:
            return "No matching syllabus entries found (possibly out-of-syllabus, or not indexed yet)."
        return "\n".join(
            f"- week {m['metadata'].get('week')}: {m['text']} "
            f"(source: {m['metadata'].get('source_file')}, distance={m['distance']:.3f})"
            for m in matches
        )


class FetchPyqChunksInput(BaseModel):
    subject: str
    source_file: str | None = Field(
        default=None, description="Exact source_file from list_source_files; omit to sample across the PYQ bank"
    )
    limit: int = Field(default=8, description="Max chunks to return")


class FetchPyqChunksTool(BaseTool):
    name: str = "fetch_pyq_chunks"
    description: str = (
        "Fetch indexed previous-year-question text for a subject, without "
        "needing a topic query. Call this FIRST when the syllabus index is "
        "empty or concepts are 'any', so you see what the PYQ bank actually "
        "covers. Never guess the course from the subject slug. Pass "
        "source_file to read one paper. Results ground similar new questions "
        "- never copy a PYQ verbatim."
    )
    args_schema: Type[BaseModel] = FetchPyqChunksInput

    def _run(self, subject: str, source_file: str | None = None, limit: int = 8) -> str:
        where = {"source_file": source_file} if source_file else None
        matches = vector_store.get_by_metadata(
            vector_store.pyq_collection_name(subject), where=where, limit=limit
        )
        if not matches:
            return "No indexed PYQ content found - has pyq/ been ingested for this subject?"
        return "\n---\n".join(
            f"[source_file: {m['metadata'].get('source_file')}]\n{_truncate(m['text'], 1500)}"
            for m in matches
        )


class PyqSearchInput(BaseModel):
    subject: str
    query: str = Field(
        default="any",
        description="Concept/topic text taken from real PYQs or --concepts. "
        "Use 'any' to sample the bank. Do not invent a course name from the subject slug.",
    )
    week: int | None = Field(
        default=None,
        description="Ignored: PYQs live in pyq/ and are not week-tagged. Use source_file instead.",
    )
    source_file: str | None = Field(default=None, description="Exact source_file from list_source_files")
    top_k: int = Field(default=5)


class PyqSearchTool(BaseTool):
    name: str = "search_pyq"
    description: str = (
        "Search the subject's previous-year-question bank for content similar "
        "to a query. Query using wording from fetch_pyq_chunks / --concepts / "
        "notes - never a guessed expansion of the subject slug. PYQs are not "
        "week-tagged; pass source_file to restrict to one paper, not week=. "
        "Pass query='any' to sample. Results ground a *similar* new question "
        "- never copy one verbatim."
    )
    args_schema: Type[BaseModel] = PyqSearchInput

    def _run(
        self,
        subject: str,
        query: str = "any",
        week: int | None = None,
        source_file: str | None = None,
        top_k: int = 5,
    ) -> str:
        _ = week  # PYQs live in pyq/ (metadata week=0); never filter by syllabus week.
        collection = vector_store.pyq_collection_name(subject)
        where = {"source_file": source_file} if source_file else None
        if not (query or "").strip() or query.strip().lower() in {"any", "*", "all"}:
            matches = vector_store.get_by_metadata(collection, where=where, limit=top_k)
            if not matches:
                return "No indexed PYQ content found."
            return "\n".join(
                f"- {_truncate(m['text'], 300)} (source: {m['metadata'].get('source_file')})"
                for m in matches
            )
        matches = vector_store.query(collection, query, n_results=top_k, where=where)
        if not matches:
            return "No matching PYQ entries found."
        return "\n".join(
            f"- {_truncate(m['text'], 300)} (source: {m['metadata'].get('source_file')}, "
            f"distance={m['distance']:.3f})"
            for m in matches
        )


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"
