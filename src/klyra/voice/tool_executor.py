"""Bridge between Gemini Live function calls and the Klyra service layer.

When Gemini decides to invoke a tool, ``ToolExecutor.execute()`` dispatches
the call to the matching ``klyra.services.*`` function, runs it in a thread
(CrewAI is synchronous), and returns a result dict that Gemini can
summarise verbally.
"""
from __future__ import annotations

import asyncio
import logging
import traceback
from typing import Any

logger = logging.getLogger(__name__)


class ToolExecutor:
    """Stateless dispatcher: function-call name → service function."""

    # Maps tool name → handler method name
    _DISPATCH = {
        "ingest": "_run_ingest",
        "generate_question_paper": "_run_generate",
        "revise_question_paper": "_run_revise",
        "convert_documents": "_run_convert",
        "prepare_teaching_material": "_run_teach",
        "revise_teaching_material": "_run_revise_teach",
        "fetch_transcript": "_run_transcript",
        "generate_coding_problems": "_run_code",
        "revise_coding_problems": "_run_revise_code",
        "delete_index": "_run_delete_index",
        "read_document": "_run_read_document",
        "list_documents": "_run_list_documents",
    }

    def __init__(self):
        self._confirmation_tokens: dict[str, str] = {}

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch *name* with *args* to the appropriate service function.

        Returns a dict with ``{"status": "success"|"error", ...}`` that
        Gemini will use to compose a spoken summary for the user.

        The actual service call runs in ``asyncio.to_thread`` because every
        CrewAI ``kickoff()`` is blocking.
        """
        handler_name = self._DISPATCH.get(name)
        if handler_name is None:
            return {"status": "error", "message": f"Unknown tool: {name}"}

        # Normalize subject to lowercase to prevent case-sensitivity issues
        if "subject" in args and isinstance(args["subject"], str):
            args["subject"] = args["subject"].lower()

        # Require explicit confirmation for write actions
        read_only_tools = {"read_document", "list_documents", "fetch_transcript"}
        if name not in read_only_tools:
            provided_token = args.get("confirmation_token")
            
            if provided_token != "confirmed":
                return {
                    "status": "confirmation_required", 
                    "message": (
                        f"The tool '{name}' performs a significant action or takes a long time. "
                        "You MUST ask the user for explicit confirmation before proceeding. "
                        "CRITICAL: Do NOT call this tool again in this same turn! "
                        "You MUST generate a response asking the user for permission, "
                        "and wait for them to reply. Only if they reply 'yes' in the NEXT turn, "
                        "call this tool again with confirmation_token='confirmed'."
                    )
                }

        handler = getattr(self, handler_name)
        logger.info("Executing tool %s with args %s", name, args)

        try:
            result = await asyncio.to_thread(handler, args)
            logger.info("Tool %s completed successfully", name)
            return {"status": "success", **result}
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc, exc_info=True)
            return {
                "status": "error",
                "message": str(exc),
                "tool": name,
            }

    # ── Individual handlers (all run in a worker thread) ─────────────

    @staticmethod
    def _run_ingest(args: dict) -> dict:
        from klyra.services.ingestion_service import run_ingestion

        result = run_ingestion(
            subject=args["subject"],
            week=args.get("week"),
            force=args.get("force", False),
        )
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_generate(args: dict) -> dict:
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
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_revise(args: dict) -> dict:
        from klyra.services.revision_service import run_revision

        result = run_revision(
            subject=args["subject"],
            qp_name=args["qp_name"],
            notes=args.get("notes"),
        )
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_convert(args: dict) -> dict:
        from klyra.services.conversion_service import run_conversion

        result = run_conversion(
            subject=args["subject"],
            request=args["request"],
        )
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_teach(args: dict) -> dict:
        from klyra.services.teaching_material_service import run_teaching_material

        result = run_teaching_material(
            subject=args["subject"],
            session_name=args["session_name"],
            scope=args["scope"],
            concepts=args.get("concepts"),
            notes=args.get("notes"),
        )
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_revise_teach(args: dict) -> dict:
        from klyra.services.teaching_material_service import run_teaching_revision

        result = run_teaching_revision(
            subject=args["subject"],
            session_name=args["session_name"],
            notes=args["notes"],
        )
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_transcript(args: dict) -> dict:
        from klyra.services.transcript_service import run_transcript

        path = run_transcript(
            url=args["url"],
            subject=args["subject"],
            week=args.get("week"),
            title=args.get("title"),
        )
        return {
            "message": f"Transcript saved to {path}.",
        }

    @staticmethod
    def _run_code(args: dict) -> dict:
        from klyra.services.coding_problem_service import run_coding_generation

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
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_revise_code(args: dict) -> dict:
        from klyra.services.coding_problem_service import run_coding_revision

        result = run_coding_revision(
            subject=args["subject"],
            name=args["name"],
            language=args["language"],
            notes=args.get("notes"),
        )
        return {
            "message": result.raw if hasattr(result, "raw") else str(result),
        }

    @staticmethod
    def _run_delete_index(args: dict) -> dict:
        from klyra.tools import vector_store

        subject = args["subject"]
        source_file = args["source_file"]
        where = {"source_file": source_file}

        for name_func in [
            vector_store.parsed_collection_name,
            vector_store.syllabus_collection_name,
            vector_store.pyq_collection_name,
            vector_store.images_collection_name,
        ]:
            name = name_func(subject)
            vector_store.delete_by_metadata(name, where)

        return {
            "message": (
                f"Deleted index entries from '{subject}' collections "
                f"where source_file='{source_file}'."
            ),
        }

    @staticmethod
    def _run_list_documents(args: dict) -> dict:
        from pathlib import Path
        from klyra.utils import fs
        
        subject = args["subject"]
        folder = args.get("folder", "")
        
        try:
            subject_dir = fs.subject_dir(subject).resolve()
            target_dir = (subject_dir / folder).resolve()
            
            if not str(target_dir).startswith(str(subject_dir)):
                return {"message": f"Error: Path {folder} is outside the subject's docs directory."}
                
            if not target_dir.exists() or not target_dir.is_dir():
                return {"message": f"Error: Folder '{folder}' not found in docs/{subject}/"}
                
            files = sorted([str(p.relative_to(subject_dir)) for p in target_dir.rglob("*") if p.is_file()])
            
            if not files:
                return {"message": f"No files found in docs/{subject}/{folder}"}
                
            return {"message": f"Files available in docs/{subject}/{folder}:\n- " + "\n- ".join(files)}
        except Exception as exc:
            return {"message": f"Error listing documents: {exc}"}

    @staticmethod
    def _run_read_document(args: dict) -> dict:
        from pathlib import Path
        from klyra.utils import fs
        import io
        import re
        import difflib

        subject = args["subject"]
        filename = args["filename"]
        
        # Resolve path safely inside the subject's docs directory
        subject_dir = fs.subject_dir(subject).resolve()
        target_path = (subject_dir / filename).resolve()
        
        # If the exact path doesn't exist, try to find it recursively anywhere in the subject's folder
        if not target_path.exists():
            basename = Path(filename).name
            all_files = [p for p in subject_dir.rglob("*") if p.is_file()]
            
            # 1. Case-insensitive exact match
            matches = [p for p in all_files if p.name.lower() == basename.lower()]
            
            # 2. Substring match
            if not matches:
                matches = [p for p in all_files if basename.lower() in p.name.lower()]
                
            # 3. Normalized alphanumeric match (ignores spaces, hyphens, underscores)
            if not matches:
                def norm(s): return re.sub(r'[^a-z0-9]', '', s.lower())
                norm_base = norm(basename)
                if norm_base:
                    matches = [p for p in all_files if norm_base in norm(p.name) or norm(p.name) in norm_base]
                    
            # 4. Fuzzy match using difflib
            if not matches:
                file_names = {p.name.lower(): p for p in all_files}
                close = difflib.get_close_matches(basename.lower(), file_names.keys(), n=1, cutoff=0.3)
                if close:
                    matches = [file_names[close[0]]]
                        
            if not matches:
                # Provide a list of available files to help the LLM self-correct
                available = "\n- ".join([str(p.relative_to(subject_dir)) for p in all_files[:20]])
                if len(all_files) > 20:
                    available += "\n... (and more)"
                return {"message": f"Error: Could not find any file matching '{basename}'. Available files in docs/{subject}/:\n- {available}"}
                
            target_path = matches[0].resolve()
        
        if not str(target_path).startswith(str(subject_dir)):
            return {"message": f"Error: Path {target_path} is outside the subject's docs directory."}
            
        if not target_path.is_file():
            return {"message": f"Error: '{target_path.name}' is not a file."}
            
        try:
            if target_path.suffix.lower() == ".pdf":
                from pypdf import PdfReader
                reader = PdfReader(str(target_path))
                content = "\n".join(page.extract_text() for page in reader.pages if page.extract_text())
            else:
                content = target_path.read_text(encoding="utf-8")
                
            # If it's too long, truncate it so Gemini voice doesn't choke on a 50k token context blast
            # But normally we want the whole document. Let's just return it.
            if len(content) > 100000:
                content = content[:100000] + "\n...[TRUNCATED]..."
            return {"message": f"File '{filename}' contents:\n\n{content}"}
        except Exception as exc:
            return {"message": f"Error reading file '{filename}': {exc}"}
