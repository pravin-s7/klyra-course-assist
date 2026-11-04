"""Single-source tool registry for Gemini Live Voice & Chat tool declarations and execution.

Each tool is defined once with a Pydantic input schema (`BaseModel` + `Field(description=...)`)
and a handler function. Gemini `FunctionDeclaration` schemas and `ToolExecutor` dispatch tables
are generated automatically from this registry.
"""
from __future__ import annotations

import difflib
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

_CONFIRM_DESCRIPTION = (
    "Set to true ONLY if you have explicitly asked the user for permission to run this tool in previous messages and they said yes. "
    "You MUST also ask them if they want to provide optional arguments (concepts/notes). "
    "If you haven't asked them yet, set it to false, and the tool will safely return an error so you can ask the user."
)

_JSON_TO_GEMINI_TYPE = {
    "string": "STRING",
    "integer": "INTEGER",
    "number": "NUMBER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "object": "OBJECT",
}


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    schema: type[BaseModel]
    handler: Callable[[dict[str, Any]], dict[str, Any]]
    required_override: list[str] | None = None

    def to_gemini_declaration(self) -> dict[str, Any]:
        json_schema = self.schema.model_json_schema()
        props_raw = json_schema.get("properties", {})
        properties: dict[str, dict[str, Any]] = {}

        for field_name, fdef in props_raw.items():
            prop_type = fdef.get("type")
            items_def = fdef.get("items")
            if not prop_type and "anyOf" in fdef:
                for branch in fdef["anyOf"]:
                    if branch.get("type") and branch.get("type") != "null":
                        prop_type = branch["type"]
                        items_def = branch.get("items")
                        break
            gemini_type = _JSON_TO_GEMINI_TYPE.get(str(prop_type or "string"), "STRING")
            entry: dict[str, Any] = {
                "type": gemini_type,
                "description": fdef.get("description", f"Parameter {field_name}."),
            }
            if gemini_type == "ARRAY":
                item_type = "STRING"
                if isinstance(items_def, dict) and items_def.get("type"):
                    item_type = _JSON_TO_GEMINI_TYPE.get(str(items_def["type"]), "STRING")
                entry["items"] = {"type": item_type}
            properties[field_name] = entry

        required = list(
            self.required_override
            if self.required_override is not None
            else json_schema.get("required", [])
        )
        if "user_has_confirmed" not in required:
            required.append("user_has_confirmed")
        properties["user_has_confirmed"] = {
            "type": "BOOLEAN",
            "description": _CONFIRM_DESCRIPTION,
        }

        return {
            "name": self.name,
            "description": self.description,
            "parameters": {
                "type": "OBJECT",
                "properties": properties,
                "required": required,
            },
        }


_REGISTRY: dict[str, ToolDefinition] = {}


def register_tool(
    name: str,
    description: str,
    schema: type[BaseModel],
    required_override: list[str] | None = None,
) -> Callable[[Callable[[dict[str, Any]], dict[str, Any]]], Callable[[dict[str, Any]], dict[str, Any]]]:
    """Decorator registering a single-source Klyra tool."""

    def _decorator(fn: Callable[[dict[str, Any]], dict[str, Any]]) -> Callable[[dict[str, Any]], dict[str, Any]]:
        _REGISTRY[name] = ToolDefinition(
            name=name,
            description=description,
            schema=schema,
            handler=fn,
            required_override=required_override,
        )
        return fn

    return _decorator


def get_registered_tools() -> dict[str, ToolDefinition]:
    return dict(_REGISTRY)


def get_all_declarations() -> list[dict[str, Any]]:
    """Return all tool function declarations for the Gemini Live / Chat session."""
    return [t.to_gemini_declaration() for t in _REGISTRY.values()]


def rewrite_markdown_asset_urls(text: str, subject: str) -> str:
    """Rewrite relative markdown image paths (e.g. '../.assets/...') to web '/files/<subject>/.assets/...' URLs."""
    if not text or not subject:
        return text
    text = re.sub(
        r"(!\[[^\]]*\]\()(?:\.\./|\./)?\.assets/",
        rf"\1/files/{subject}/.assets/",
        text,
    )
    text = re.sub(
        rf"(!\[[^\]]*\]\()docs/{re.escape(subject)}/\.assets/",
        rf"\1/files/{subject}/.assets/",
        text,
    )
    text = re.sub(
        rf"(!\[[^\]]*\]\()/files/{re.escape(subject)}/assets/",
        rf"\1/files/{subject}/.assets/",
        text,
    )
    return text


# ── Pydantic Input Schemas & Handlers ───────────────────────────────────


class IngestInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'data-structures', 'dbms'). Must match a folder name under docs/.")
    week: int | None = Field(default=None, description="Optional: only ingest a specific week (1-12). If omitted, all weeks and additional materials are ingested.")
    force: bool = Field(default=False, description="If true, re-parse files even if they haven't changed since the last ingestion. Default is false.")
    categories: list[str] | None = Field(default=None, description="Optional: specific folders/categories to ingest (e.g. ['pyq', 'additional', 'transcripts']). If omitted, all materials are ingested.")


@register_tool(
    name="ingest",
    description=(
        "Parse and index a subject's source documents (PDFs, slides, transcripts, etc.) "
        "into the vector store so they can be used for question paper and coding problem generation. "
        "CRITICAL: Do NOT run this automatically. ONLY run this tool if the user explicitly asks to 'ingest', 'index', or 'upload' materials."
    ),
    schema=IngestInput,
)
def run_ingest_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.ingestion_service import run_ingestion

    result = run_ingestion(
        subject=args["subject"],
        week=args.get("week"),
        categories=args.get("categories"),
        force=args.get("force", False),
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class GenerateQPInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'data-structures').")
    qp_name: str = Field(..., description="Name for the question paper (e.g. 'midterm', 'quiz-1'). Used as the output filename.")
    scope: str = Field(..., description="Which weeks to cover. Examples: 'all', 'week_3', 'week_1-week_4', '1-4', '1,3,5', '1-3,5'.")
    difficulty: str = Field(..., description="Difficulty level. Examples: 'easy', 'medium', 'hard', or a mix like '40% easy, 60% hard'.")
    type_counts: str | None = Field(default=None, description="Precise per-type question counts, e.g. 'MCQ:5,MSQ:3,NAT:2'. Preferred over types+num_questions. If provided, types and num_questions are ignored.")
    types: str | None = Field(default=None, description="Comma-separated question types (e.g. 'MCQ,MSQ,NAT'). Only used if type_counts is not provided.")
    num_questions: int | None = Field(default=None, description="Total number of questions. Only used if type_counts is not provided (paired with types).")
    source: str | None = Field(default=None, description="Optional: restrict to one source document (fuzzy matched), e.g. 'Lecture 1.5'.")
    pyq_similar: int = Field(default=0, description="How many questions should be similar in style to real past-year questions (never verbatim copies). Default 0.")
    concepts: str | None = Field(default=None, description="Optional: specific topics to focus on, e.g. 'arrays, linked lists'.")
    notes: str | None = Field(default=None, description="Optional free-form guidance, e.g. 'make B-tree questions hard, keep normalization easy'.")


@register_tool(
    name="generate_question_paper",
    description=(
        "Create a complete question paper with review and answer key. "
        "The user must specify the subject, a name for the paper, which weeks/scope to cover, "
        "and the difficulty level. Either provide type_counts (e.g. 'MCQ:5,MSQ:3,NAT:2') for precise control, "
        "or provide both types and num_questions for a simpler spec."
    ),
    schema=GenerateQPInput,
    required_override=["subject", "qp_name", "scope", "difficulty", "types", "num_questions"],
)
def run_generate_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.generation_service import run_generation

    result = run_generation(
        subject=args["subject"],
        qp_name=args["qp_name"],
        scope=args["scope"],
        difficulty=args["difficulty"],
        source=args.get("source"),
        pyq_similar=args.get("pyq_similar", 0),
        concepts=args.get("concepts"),
        type_counts=args.get("type_counts"),
        types=args.get("types"),
        num_questions=args.get("num_questions"),
        notes=args.get("notes"),
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class ReviseQPInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    qp_name: str = Field(..., description="The base name of the question paper to revise (e.g. 'endterm-qp').")
    notes: str | None = Field(default=None, description="Optional specific edits, e.g. 'reformat Q1 as a table; replace Q3 with a question about hashing'.")


@register_tool(
    name="revise_question_paper",
    description=(
        "Revise a previously generated question paper. Uses the paper's own review and answer key "
        "to fix flagged issues. Optionally accepts manual notes for specific edits."
    ),
    schema=ReviseQPInput,
)
def run_revise_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.revision_service import run_revision

    result = run_revision(
        subject=args["subject"],
        qp_name=args["qp_name"],
        notes=args.get("notes"),
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class ConvertDocumentsInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    request: str = Field(
        ...,
        description=(
            "What to convert, e.g. 'convert midterm-qp-qp.md and midterm-qp-answer-key.md to LaTeX'. "
            "CRITICAL: Do NOT guess filenames. If you don't know the exact filenames, run list_documents first to find them."
        ),
    )


@register_tool(
    name="convert_documents",
    description="Convert generated documents between Markdown and LaTeX using an independent agent with pandoc.",
    schema=ConvertDocumentsInput,
)
def run_convert_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.conversion_service import run_conversion

    result = run_conversion(
        subject=args["subject"],
        request=args["request"],
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class PrepareTeachingMaterialInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    session_name: str = Field(..., description="Name for the session, e.g. 'week-4-live'. Used as the output filename.")
    scope: str = Field(..., description="Which weeks to cover. Same syntax as generate scope.")
    concepts: str | None = Field(default=None, description="Optional: specific concepts to focus on.")
    notes: str | None = Field(default=None, description="Optional guidance, e.g. '45 min, focus on intuition before formalism, 2 worked examples'.")


@register_tool(
    name="prepare_teaching_material",
    description=(
        "Prepare structured teaching material for a live classroom session. "
        "Generates a comprehensive session plan with content, worked examples, and activity suggestions."
    ),
    schema=PrepareTeachingMaterialInput,
)
def run_teach_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.teaching_material_service import run_teaching_material

    result = run_teaching_material(
        subject=args["subject"],
        session_name=args["session_name"],
        scope=args["scope"],
        concepts=args.get("concepts"),
        notes=args.get("notes"),
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class ReviseTeachingMaterialInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    session_name: str = Field(..., description="Name of the teaching session to revise.")
    notes: str = Field(..., description="Required revision instructions, e.g. 'add a worked example for recursion before the formal definition'.")


@register_tool(
    name="revise_teaching_material",
    description=(
        "Revise previously prepared teaching material based on instructor notes. "
        "Notes are required since teaching material has no automated review to fall back on."
    ),
    schema=ReviseTeachingMaterialInput,
)
def run_revise_teach_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.teaching_material_service import run_teaching_revision

    result = run_teaching_revision(
        subject=args["subject"],
        session_name=args["session_name"],
        notes=args["notes"],
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class FetchTranscriptInput(BaseModel):
    url: str = Field(..., description="YouTube URL or video ID.")
    subject: str = Field(..., description="Subject identifier.")
    week: int | None = Field(default=None, description="Optional: which week this lecture belongs to.")
    title: str | None = Field(default=None, description="Optional: title for the transcript file.")


@register_tool(
    name="fetch_transcript",
    description="Download and save a transcript from a YouTube video. Useful for ingesting lecture recordings.",
    schema=FetchTranscriptInput,
)
def run_transcript_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.transcript_service import run_transcript

    path = run_transcript(
        url=args["url"],
        subject=args["subject"],
        week=args.get("week"),
        title=args.get("title"),
    )
    return {"message": f"Transcript saved to {path}."}


class GenerateCodingProblemsInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    name: str = Field(..., description="Name for the problem set, e.g. 'lab-3'. Used as the output filename.")
    language: str = Field(..., description="Programming language: python, java, c, cpp, bash, sed, awk, gawk, sql, postgresql, or python-db.")
    scope: str = Field(..., description="Which weeks to cover.")
    difficulty: str = Field(..., description="Difficulty level, e.g. 'medium' or '40% easy, 60% hard'.")
    num_questions: int = Field(..., description="Number of coding problems to generate.")
    public_tests: int = Field(..., description="Number of public (visible) test cases per problem.")
    private_tests: int = Field(..., description="Number of private (hidden) test cases per problem.")
    source: str | None = Field(default=None, description="Optional: restrict to one source document, e.g. 'Lab 3 handout'.")
    pyq_similar: int = Field(default=0, description="How many problems should be similar to real past-year questions. Default 0.")
    leetcode_similar: bool = Field(default=False, description="If true, look up related LeetCode problems and write original problems inspired by them. Default false.")
    include_reference: bool = Field(default=False, description="If true, include reference solutions in the student-facing problems file. Default false.")
    concepts: str | None = Field(default=None, description="Optional: specific topics, e.g. 'recursion, file I/O'.")
    notes: str | None = Field(default=None, description="Optional free-form guidance, e.g. 'focus on recursion, make the linked-list one hard'.")


@register_tool(
    name="generate_coding_problems",
    description=(
        "Generate a set of coding problems with test cases. Problems are independently validated "
        "by actually compiling and running reference solutions against the generated test cases in Docker."
    ),
    schema=GenerateCodingProblemsInput,
)
def run_code_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.coding_problem_service import run_coding_generation

    logger.info("Starting generation of coding problems for '%s'...", args.get("subject", "unknown"))
    result = run_coding_generation(
        subject=args["subject"],
        name=args["name"],
        language=args["language"],
        scope=args["scope"],
        difficulty=args["difficulty"],
        num_questions=args["num_questions"],
        public_tests=args["public_tests"],
        private_tests=args["private_tests"],
        source=args.get("source"),
        pyq_similar=args.get("pyq_similar", 0),
        leetcode_similar=args.get("leetcode_similar", False),
        include_reference=args.get("include_reference", False),
        concepts=args.get("concepts"),
        notes=args.get("notes"),
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class ReviseCodingProblemsInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    name: str = Field(..., description="Name of the problem set to revise.")
    language: str = Field(..., description="Programming language: python, java, c, cpp, bash, sed, awk, gawk, sql, postgresql, or python-db.")
    notes: str | None = Field(default=None, description="Optional specific edits, e.g. 'problem 2's constraints are unclear about negative numbers'.")


@register_tool(
    name="revise_coding_problems",
    description=(
        "Revise a previously generated coding problem set. Uses the automated review and "
        "validation results to fix flagged issues."
    ),
    schema=ReviseCodingProblemsInput,
)
def run_revise_code_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.coding_problem_service import run_coding_revision

    result = run_coding_revision(
        subject=args["subject"],
        name=args["name"],
        language=args["language"],
        notes=args.get("notes"),
    )
    return {"message": result.raw if hasattr(result, "raw") else str(result)}


class DeleteIndexInput(BaseModel):
    subject: str = Field(..., description="Subject identifier.")
    source_files: list[str] | None = Field(default=None, description="Optional list of exact paths of the source files to remove from the index.")
    folder: str | None = Field(default=None, description="Optional specific folder (e.g. 'pyq') to remove from the index.")
    source_file: str | None = Field(default=None, description="Optional single exact path of the source file to remove from the index.")


@register_tool(
    name="delete_index",
    description=(
        "Delete indexed entries from the vector store by metadata. "
        "For example, remove all chunks from a specific source file that was re-uploaded or is no longer needed."
    ),
    schema=DeleteIndexInput,
)
def run_delete_index_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.ingestion_service import run_ingestion
    from klyra.utils.fs import discover_subject_files

    subject = args["subject"]
    files_to_delete: list[str] = []

    if "source_files" in args and args["source_files"]:
        files_to_delete.extend(args["source_files"])
    if "source_file" in args and args["source_file"]:
        files_to_delete.append(args["source_file"])
    if "folder" in args and args["folder"]:
        folder = args["folder"]
        try:
            sources = discover_subject_files(subject)
            source_dir = Path("docs") / subject
            for s in sources:
                try:
                    rel = s.path.relative_to(source_dir)
                    if str(rel).startswith(folder):
                        files_to_delete.append(str(s.path.resolve()))
                except ValueError:
                    pass
        except FileNotFoundError:
            pass

    if not files_to_delete:
        return {"message": "No files specified or found to delete."}

    unique_files = list(set(files_to_delete))
    for f in unique_files:
        run_ingestion(subject, delete_file=f)
    return {"message": f"Successfully deleted {len(unique_files)} files from the index."}


class GenerateDiagramInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'data-structures').")
    latex_body: str = Field(
        ...,
        description=(
            "LaTeX for the diagram - only the content that goes inside \\begin{document}...\\end{document}. "
            "CRITICAL: Adapt your TikZ/LaTeX structures to the precise semantics of the subject! "
            "For example, if CT, MUST be a DIRECTED graph."
        ),
    )
    caption: str = Field(..., description="Short caption describing the diagram.")
    ground_truth: str = Field(..., description="The FULL factual content of the diagram in plain text.")


@register_tool(
    name="generate_diagram",
    description=(
        "Render a NEW diagram (tree, B-tree, graph, ER diagram, table, dependency diagram, etc.) via LaTeX/TikZ. "
        "Use this ONLY for academic purposes based on instructions. "
        "You MUST output the returned image markdown directly to the user."
    ),
    schema=GenerateDiagramInput,
)
def run_generate_diagram_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.tools.latex_diagram_tool import GenerateDiagramTool

    tool = GenerateDiagramTool()
    try:
        result = tool._run(
            subject=args["subject"],
            latex_body=args["latex_body"],
            caption=args["caption"],
            ground_truth=args["ground_truth"],
        )
        match = re.search(r"\[.*?\]\((.*?)\)", result)
        if match:
            rel_path = match.group(1)
            file_name = rel_path.split("/")[-1]
            web_path = f"/files/{args['subject']}/.assets/generated/{file_name}"
            result = result.replace(rel_path, web_path)
        return {"status": "success", "markdown": result, "message": result}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


class ListDocumentsInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'dbms').")
    folder: str | None = Field(
        default=None,
        description="Optional subfolder to list (e.g. 'pyq', 'generated'). If omitted, lists all files in the subject's docs.",
    )


@register_tool(
    name="list_documents",
    description=(
        "List all files available in a subject's docs/ directory or a specific subfolder. "
        "Use this when the user asks you to read all files in a folder, or when you need "
        "to find out the exact filenames before calling read_document."
    ),
    schema=ListDocumentsInput,
)
def run_list_documents_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.utils import fs
    from klyra.web.routes.files import (
        _collect_prefix_bundle_files,
        _resolve_download_target,
    )

    subject = str(args["subject"]).lower()
    folder = (args.get("folder") or "").strip()
    try:
        subject_dir = fs.subject_dir(subject).resolve()
        target_dir = (subject_dir / folder).resolve() if folder else subject_dir
        if not str(target_dir).startswith(str(subject_dir)):
            return {"message": f"Error: Path {folder} is outside the subject's docs directory."}

        if folder and (not target_dir.exists() or not target_dir.is_dir()):
            resolved = _resolve_download_target(subject, folder)
            if resolved is not None and resolved.exists() and resolved.is_dir():
                target_dir = resolved
            else:
                prefix, bundle_files = _collect_prefix_bundle_files(subject, folder)
                if bundle_files:
                    rel_files = sorted(str(p.relative_to(subject_dir)) for p in bundle_files)
                    return {
                        "message": (
                            f"Files available for '{prefix}' in docs/{subject}/:\n- "
                            + "\n- ".join(rel_files)
                            + f"\n\nDownloadable ZIP bundle link: [Download {prefix}.zip](/api/download/{subject}/{prefix})"
                        )
                    }
                return {"message": f"Error: Folder '{folder}' not found in docs/{subject}/"}

        files_list = [p for p in target_dir.rglob("*") if p.is_file()]
        if folder and target_dir != subject_dir and target_dir.parent.exists():
            pkg_prefix = target_dir.name.lower()
            for sib in target_dir.parent.iterdir():
                if sib.is_file() and (
                    sib.name.lower().startswith(pkg_prefix + "-")
                    or sib.name.lower().startswith(pkg_prefix + ".")
                ):
                    files_list.append(sib)

        files = sorted({str(p.resolve().relative_to(subject_dir)) for p in files_list})
        if not files:
            return {"message": f"No files found in docs/{subject}/{folder}"}
        msg = f"Files available in docs/{subject}/{folder}:\n- ".rstrip("/") + "\n- ".join(files)
        if folder and target_dir != subject_dir:
            msg += f"\n\nDownloadable ZIP bundle link: [Download {target_dir.name}.zip](/api/download/{subject}/{target_dir.name})"
        return {"message": msg}
    except Exception as exc:
        return {"message": f"Error listing documents: {exc}"}


class ReadDocumentInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'dbms').")
    filename: str = Field(..., description="Filename or path relative to the subject's docs folder (e.g. 'generated/end-term-qp.md').")


@register_tool(
    name="read_document",
    description=(
        "Read the contents of a generated document or any file in the subject's docs/ directory. "
        "Use this when the user asks you to verify the actual content of a generated question paper, "
        "answer key, or other files instead of just relying on the tool success message."
    ),
    schema=ReadDocumentInput,
)
def run_read_document_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.utils import fs

    subject = str(args["subject"]).lower()
    filename = args["filename"]
    subject_dir = fs.subject_dir(subject).resolve()
    target_path = (subject_dir / filename).resolve()

    if not target_path.exists():
        basename = Path(filename).name
        all_files = [p for p in subject_dir.rglob("*") if p.is_file()]
        matches = [p for p in all_files if p.name.lower() == basename.lower()]
        if not matches:
            matches = [p for p in all_files if basename.lower() in p.name.lower()]
        if not matches:
            norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
            norm_base = norm(basename)
            if norm_base:
                matches = [p for p in all_files if norm_base in norm(p.name) or norm(p.name) in norm_base]
        if not matches:
            file_names = {p.name.lower(): p for p in all_files}
            close = difflib.get_close_matches(basename.lower(), file_names.keys(), n=1, cutoff=0.3)
            if close:
                matches = [file_names[close[0]]]
        if not matches:
            available = "\n- ".join(str(p.relative_to(subject_dir)) for p in all_files[:20])
            if len(all_files) > 20:
                available += "\n... (and more)"
            return {"message": f"Error: Could not find any file matching '{basename}'. Available files in docs/{subject}/:\n- {available}"}
        target_path = matches[0].resolve()

    if not str(target_path).startswith(str(subject_dir)):
        return {"message": f"Error: Path {target_path} is outside the subject's docs directory."}
    if not target_path.is_file():
        return {"message": f"Error: '{target_path.name}' is not a file."}

    try:
        suffix = target_path.suffix.lower()
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}:
            rel_to_docs = target_path.relative_to(subject_dir.parent).as_posix()
            web_url = f"/files/{rel_to_docs}"
            return {
                "message": (
                    f"Image file '{target_path.name}' found at {web_url}.\n\n"
                    f"Include this exact Markdown image syntax in your reply to display it in the chat UI:\n"
                    f"![{target_path.stem}]({web_url})"
                )
            }
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(target_path))
            content = "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
        else:
            content = target_path.read_text(encoding="utf-8")
            content = rewrite_markdown_asset_urls(content, subject)

        if len(content) > 100000:
            content = content[:100000] + "\n...[TRUNCATED]..."
        return {"message": f"File '{filename}' contents:\n\n{content}"}
    except Exception as exc:
        return {"message": f"Error reading file '{filename}': {exc}"}


class GetSubjectSkillInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'dbms', 'ct', 'python').")


@register_tool(
    name="get_subject_skill",
    description=(
        "Read a subject's skill.md configuration (domain rules, allowed question types, "
        "coding languages, and custom output formats for question papers, answer keys, "
        "coding problems, and teaching material)."
    ),
    schema=GetSubjectSkillInput,
)
def run_get_subject_skill_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.skill_service import load_subject_skill, render_subject_skill_markdown
    from klyra.utils import fs

    subject = args["subject"]
    cfg = load_subject_skill(subject)
    path = fs.subject_skill_path(subject)
    md = path.read_text(encoding="utf-8") if path.exists() else render_subject_skill_markdown(cfg)
    return {
        "message": f"Skill configuration for '{subject}':\n\n{md}",
        "config": cfg.model_dump(mode="json"),
    }


class UpdateSubjectSkillInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'dbms').")
    markdown: str = Field(..., description="Complete skill.md content with YAML frontmatter and markdown sections.")


@register_tool(
    name="update_subject_skill",
    description=(
        "Validate and update a subject's docs/<subject>/skill.md configuration file "
        "with custom YAML frontmatter and markdown formatting instructions."
    ),
    schema=UpdateSubjectSkillInput,
)
def run_update_subject_skill_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.skill_service import parse_subject_skill, render_subject_skill_markdown
    from klyra.utils import fs

    subject = fs.validate_subject(args["subject"])
    cfg = parse_subject_skill(args["markdown"], fallback_subject=subject)
    path = fs.subject_skill_path(subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_subject_skill_markdown(cfg)
    path.write_text(rendered, encoding="utf-8")
    return {
        "message": f"Updated skill.md for '{subject}' at {path}.",
        "config": cfg.model_dump(mode="json"),
    }


class VerifyCodingPackageInput(BaseModel):
    subject: str = Field(..., description="Subject identifier (e.g. 'python', 'c_programming', 'dbms').")
    name: str = Field(..., description="Name of the coding problem set (e.g. 'lab-3').")
    language: str | None = Field(
        default=None,
        description="Optional language override: python, java, c, cpp, bash, sed, awk, gawk, sql, postgresql, or python-db.",
    )


@register_tool(
    name="verify_coding_package",
    description=(
        "Execute all public and private test cases of an on-disk coding problem package "
        "against its reference solution in the isolated Docker sandbox and return the pass/fail report."
    ),
    schema=VerifyCodingPackageInput,
)
def run_verify_coding_package_tool(args: dict[str, Any]) -> dict[str, Any]:
    from klyra.services.coding_problem_service import verify_on_disk_coding_package
    from klyra.services.skill_service import load_subject_skill

    subject = args["subject"]
    name = args["name"]
    skill = load_subject_skill(subject)
    language = args.get("language") or skill.default_coding_language or "python"
    ok, report = verify_on_disk_coding_package(subject, name, language)
    return {
        "all_passed": ok,
        "message": report,
    }

