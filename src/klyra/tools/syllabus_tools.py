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


def _read_disk_excerpt(path, max_chars: int = 1500) -> str:
    """Read text excerpt directly from an on-disk source file (.md, .txt, .pdf, .docx, .pptx, code)
    so QP generation works even before `klyra ingest` has been run."""
    suffix = path.suffix.lower()
    try:
        if suffix in fs.PLAIN_TEXT_SUFFIXES or suffix in fs.CODE_SUFFIXES:
            text = path.read_text(errors="replace")
        elif suffix == fs.PDF_SUFFIX:
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages[:8])
        elif suffix in {".pptx", ".docx"}:
            from klyra.tools.document_parser_tool import _fallback_office_text

            text = _fallback_office_text(path)
        else:
            return ""
    except Exception:
        return ""
    text = text.strip()
    if not text:
        return ""
    return _truncate(text, max_chars)


def _match_source_file(candidate_path: str, source_file: str | None) -> bool:
    if not source_file or source_file.strip().lower() in {"any", "*", "all"}:
        return True
    sf = source_file.strip().lower()
    cp = candidate_path.lower()
    return sf == cp or sf in cp or cp.endswith(sf)


def _score_disk_text(query: str, path_str: str, text: str) -> int:
    q = (query or "").strip().lower()
    if not q or q in {"any", "*", "all"}:
        return 1
    blob = f"{path_str} {text}".lower()
    tokens = [t for t in q.replace(",", " ").split() if len(t) >= 2]
    if not tokens:
        return 1
    return sum(1 for tok in tokens if tok in blob)


class ListSourceFilesInput(BaseModel):
    subject: str
    week: int | None = Field(default=None, description="Restrict to a specific week (1-12)")


class ListSourceFilesTool(BaseTool):
    name: str = "list_source_files"
    description: str = (
        "List the exact indexed and on-disk source_file paths for a subject "
        "(optionally filtered by week). Separates syllabus/parsed files from "
        "PYQ files. Use this to see what material is available (weeks, "
        "additional, transcripts, or PYQs). If syllabus files are empty but "
        "PYQ files exist, generate from the PYQ bank - do not guess the course "
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
            disk_week_files: list[str] = []
            disk_pyq_files: list[str] = []
            try:
                all_disk = fs.discover_subject_files(subject, week=week)
                disk_week_files = [
                    str(f.path) for f in all_disk if f.category != "pyq"
                ]
                all_pyq_disk = fs.discover_subject_files(subject, categories=["pyq"])
                disk_pyq_files = [str(f.path) for f in all_pyq_disk]
            except Exception:
                pass

            if not files and not pyq_files:
                lines = [
                    "No indexed source files found - has this subject been ingested yet?"
                ]
                if disk_week_files:
                    lines.append("On-disk syllabus/week files (available directly without ingest):")
                    lines.extend(f"- {f}" for f in disk_week_files)
                if disk_pyq_files:
                    lines.append("On-disk PYQ files (available directly without ingest):")
                    lines.extend(f"- {f}" for f in disk_pyq_files)
                return "\n".join(lines)

            lines = []
            merged_week = sorted(set(files) | set(disk_week_files))
            merged_pyq = sorted(set(pyq_files) | set(disk_pyq_files))
            if merged_week:
                lines.append("Syllabus/parsed files:")
                lines.extend(f"- {f}" for f in merged_week)
            else:
                lines.append("No syllabus/parsed files indexed.")
            if merged_pyq:
                lines.append("PYQ files:")
                lines.extend(f"- {f}" for f in merged_pyq)
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
    description: str = (
        "List which week_1..week_12 folders for a subject are missing or empty, "
        "along with the status of pyq, transcripts, additional, and weeks folders."
    )
    args_schema: Type[BaseModel] = ReportSyllabusGapsInput

    def _run(self, subject: str) -> str:
        gaps = fs.missing_weeks(subject)
        first_line = (
            "No gaps - all 12 weeks have source material."
            if not gaps
            else "Missing/empty weeks: " + ", ".join(str(w) for w in gaps)
        )
        try:
            all_files = fs.discover_subject_files(subject)
            manifest = fs.load_manifest(subject)
            covered_weeks = [w for w in range(1, fs.NUM_WEEKS + 1) if w not in gaps]
            lines = [first_line, "Folder / Category Status:"]
            for cat_key, label in (
                ("week", "weeks"),
                ("pyq", "pyq"),
                ("transcript", "transcripts"),
                ("additional", "additional"),
            ):
                cat_files = [f for f in all_files if f.category == cat_key]
                ingested = [f for f in cat_files if str(f.path) in manifest]
                chunks_n = sum(
                    int(manifest.get(str(f.path), {}).get("chunks", 0))
                    for f in ingested
                )
                extra = (
                    f" (covered weeks: {', '.join(str(w) for w in covered_weeks) or 'none'})"
                    if cat_key == "week"
                    else ""
                )
                lines.append(
                    f"- {label}: {len(cat_files)} file(s) on disk, "
                    f"{len(ingested)} ingested ({chunks_n} chunks){extra}"
                )
            return "\n".join(lines)
        except Exception:
            return first_line


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
        "Search the subject's indexed syllabus (or available week/additional/PYQ "
        "material on disk) for concepts matching a query. Use this to ground "
        "question content. Pass source_file (see list_source_files) to restrict "
        "to one specific lecture/document."
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
        try:
            where = _week_source_where(week, source_file)
            matches = vector_store.query(
                vector_store.syllabus_collection_name(subject), query, n_results=top_k, where=where
            )
            if not matches:
                matches = vector_store.query(
                    vector_store.parsed_collection_name(subject), query, n_results=top_k, where=where
                )
            if matches:
                return "\n".join(
                    f"- week {m['metadata'].get('week')}: {_truncate(m['text'], 500)} "
                    f"(source: {m['metadata'].get('source_file')}, distance={m['distance']:.3f})"
                    for m in matches
                )

            # Fallback 1: check indexed PYQ collection if week/syllabus collections are empty
            pyq_where = {"source_file": source_file} if source_file else None
            pyq_matches = vector_store.query(
                vector_store.pyq_collection_name(subject), query, n_results=top_k, where=pyq_where
            )
            if pyq_matches:
                return "\n".join(
                    f"- week pyq: {_truncate(m['text'], 500)} "
                    f"(source: {m['metadata'].get('source_file')}, distance={m['distance']:.3f})"
                    for m in pyq_matches
                )
        except Exception:
            pass

        # Fallback 2: read directly from on-disk files (weeks, additional, transcripts, or pyq)
        try:
            disk_files = fs.discover_subject_files(subject, week=week)
            if not disk_files and week is not None:
                # If requested week folder is empty, check all available files on disk (including pyq)
                disk_files = fs.discover_subject_files(subject)
            scored: list[tuple[int, fs.SourceFile, str]] = []
            for f in disk_files:
                if not _match_source_file(str(f.path), source_file):
                    continue
                excerpt = _read_disk_excerpt(f.path, max_chars=800)
                if not excerpt:
                    continue
                sc = _score_disk_text(query, str(f.path), excerpt)
                scored.append((sc, f, excerpt))
            if scored:
                scored.sort(key=lambda item: item[0], reverse=True)
                return "\n".join(
                    f"- week {f.week if f.week is not None else f.category}: {excerpt} "
                    f"(source: {f.path.as_posix()}, on-disk)"
                    for _, f, excerpt in scored[:top_k]
                )
        except Exception:
            pass

        return "No matching syllabus entries found (possibly out-of-syllabus, or not indexed yet)."


class FetchPyqChunksInput(BaseModel):
    subject: str
    source_file: str | None = Field(
        default=None, description="Exact source_file from list_source_files; omit to sample across the PYQ bank"
    )
    limit: int = Field(default=8, description="Max chunks to return")


class FetchPyqChunksTool(BaseTool):
    name: str = "fetch_pyq_chunks"
    description: str = (
        "Fetch previous-year-question text for a subject (from the vector index "
        "or directly from docs/<subject>/pyq/ on disk), without needing a topic "
        "query. Call this FIRST when the syllabus index is empty or concepts are "
        "'any', so you see what the PYQ bank actually covers. Pass source_file "
        "to read one paper. Results ground similar new questions - never copy a "
        "PYQ verbatim."
    )
    args_schema: Type[BaseModel] = FetchPyqChunksInput

    def _run(self, subject: str, source_file: str | None = None, limit: int = 8) -> str:
        try:
            where = {"source_file": source_file} if source_file else None
            matches = vector_store.get_by_metadata(
                vector_store.pyq_collection_name(subject), where=where, limit=limit
            )
            if matches:
                return "\n---\n".join(
                    f"[source_file: {m['metadata'].get('source_file')}]\n{_truncate(m['text'], 1500)}"
                    for m in matches
                )
        except Exception:
            pass

        # Fallback: read PYQ files (or week files if pyq/ is empty) directly from disk
        try:
            pyq_disk = fs.discover_subject_files(subject, categories=["pyq"])
            if not pyq_disk:
                pyq_disk = fs.discover_subject_files(subject)
            blocks: list[str] = []
            for f in pyq_disk:
                if not _match_source_file(str(f.path), source_file):
                    continue
                excerpt = _read_disk_excerpt(f.path, max_chars=1500)
                if excerpt:
                    blocks.append(f"[source_file: {f.path.as_posix()}]\n{excerpt}")
                if len(blocks) >= limit:
                    break
            if blocks:
                return "\n---\n".join(blocks)
        except Exception:
            pass

        return "No indexed PYQ content found - has pyq/ been ingested for this subject?"


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
        "Search the subject's previous-year-question bank (indexed or on-disk) "
        "for content similar to a query. Query using wording from "
        "fetch_pyq_chunks / --concepts / notes - never a guessed expansion of "
        "the subject slug. PYQs are not week-tagged; pass source_file to "
        "restrict to one paper, not week=. Pass query='any' to sample. Results "
        "ground a *similar* new question - never copy one verbatim."
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
        try:
            if not (query or "").strip() or query.strip().lower() in {"any", "*", "all"}:
                matches = vector_store.get_by_metadata(collection, where=where, limit=top_k)
                if matches:
                    return "\n".join(
                        f"- {_truncate(m['text'], 300)} (source: {m['metadata'].get('source_file')})"
                        for m in matches
                    )
            else:
                matches = vector_store.query(collection, query, n_results=top_k, where=where)
                if matches:
                    return "\n".join(
                        f"- {_truncate(m['text'], 300)} (source: {m['metadata'].get('source_file')}, "
                        f"distance={m['distance']:.3f})"
                        for m in matches
                    )
        except Exception:
            pass

        # Fallback: search on-disk PYQ files (or week files if pyq/ is empty) directly
        try:
            pyq_disk = fs.discover_subject_files(subject, categories=["pyq"])
            if not pyq_disk:
                pyq_disk = fs.discover_subject_files(subject)
            scored: list[tuple[int, fs.SourceFile, str]] = []
            for f in pyq_disk:
                if not _match_source_file(str(f.path), source_file):
                    continue
                excerpt = _read_disk_excerpt(f.path, max_chars=400)
                if not excerpt:
                    continue
                sc = _score_disk_text(query, str(f.path), excerpt)
                scored.append((sc, f, excerpt))
            if scored:
                scored.sort(key=lambda item: item[0], reverse=True)
                return "\n".join(
                    f"- {_truncate(excerpt, 300)} (source: {f.path.as_posix()}, on-disk)"
                    for _, f, excerpt in scored[:top_k]
                )
        except Exception:
            pass

        return "No matching PYQ entries found."


def _truncate(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "…"
