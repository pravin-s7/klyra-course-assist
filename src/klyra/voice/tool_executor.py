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
import sys
import contextlib
import io
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)

class LogStreamer(io.StringIO):
    """Intercepts stdout, prints to terminal, and sends via async callback."""
    def __init__(self, loop: asyncio.AbstractEventLoop, executor: 'ToolExecutor', tool_name: str):
        super().__init__()
        self.loop = loop
        self.executor = executor
        self.tool_name = tool_name
        self._buffer = ""

    def write(self, string: str):
        sys.__stdout__.write(string)
        sys.__stdout__.flush()
        self._buffer += string
        if '\n' in self._buffer:
            lines = self._buffer.split('\n')
            self._buffer = lines.pop()
            for line in lines:
                text = line + '\n'
                if self.executor.on_log_callback and self.loop:
                    asyncio.run_coroutine_threadsafe(
                        self.executor.on_log_callback(self.tool_name, text),
                        self.loop
                    )
                    
    def flush(self):
        sys.__stdout__.flush()
        if self._buffer and self.executor.on_log_callback and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.executor.on_log_callback(self.tool_name, self._buffer),
                self.loop
            )
            self._buffer = ""


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
        "generate_diagram": "_run_generate_diagram",
        "get_subject_skill": "_run_get_subject_skill",
        "update_subject_skill": "_run_update_subject_skill",
        "verify_coding_package": "_run_verify_coding_package",
    }

    def __init__(
        self,
        on_log_callback: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
        allowed_subjects: set[str] | None = None,
    ):
        self._confirmation_tokens: dict[str, str] = {}
        self.on_log_callback = on_log_callback
        # RBAC: None → unrestricted (admin/CLI); otherwise tool calls on
        # subjects outside this set are rejected (used by the web app).
        self.allowed_subjects = allowed_subjects

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

        # RBAC: reject tool calls targeting subjects outside the user's courses
        if (
            self.allowed_subjects is not None
            and isinstance(args.get("subject"), str)
            and args["subject"]
            and args["subject"] not in self.allowed_subjects
        ):
            return {
                "status": "error",
                "message": (
                    f"Access denied: you are not assigned to the course "
                    f"'{args['subject']}'. Ask your admin to assign it."
                ),
                "tool": name,
            }

        handler = getattr(self, handler_name)
        logger.info("Executing tool %s with args %s", name, args)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
            
        def _run_with_redirect(h, a, l):
            if self.on_log_callback and l:
                streamer = LogStreamer(l, self, name)
                
                # Capture standard python logging in this thread
                log_handler = logging.StreamHandler(streamer)
                log_handler.setLevel(logging.INFO)
                log_handler.setFormatter(logging.Formatter('%(message)s'))
                root_logger = logging.getLogger()
                root_logger.addHandler(log_handler)
                
                try:
                    with contextlib.redirect_stdout(streamer), contextlib.redirect_stderr(streamer):
                        logger.info(f"--- Starting tool: {name} ---")
                        if "subject" in a:
                            logger.info(f"Subject: {a['subject']}")
                        logger.info("Initializing task...\n")
                        res = h(a)
                        streamer.flush()
                        return res
                finally:
                    root_logger.removeHandler(log_handler)
            return h(a)

        try:
            result = await asyncio.to_thread(_run_with_redirect, handler, args, loop)
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
            categories=args.get("categories"),
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
        import logging
        from klyra.services.coding_problem_service import run_coding_generation
        
        logger = logging.getLogger(__name__)
        logger.info(f"Starting generation of coding problems for '{args.get('subject', 'unknown')}'...")
        logger.info("Booting up CrewAI agents. This may take a moment...")

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
        from klyra.services.ingestion_service import run_ingestion
        from klyra.utils.fs import discover_subject_files
        from pathlib import Path

        subject = args["subject"]
        files_to_delete = []

        if "source_files" in args:
            files_to_delete.extend(args["source_files"])

        # Also support the old source_file parameter just in case
        if "source_file" in args:
            files_to_delete.append(args["source_file"])

        if "folder" in args:
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

        files_to_delete = list(set(files_to_delete))

        for f in files_to_delete:
            run_ingestion(subject, delete_file=f)

        return {
            "message": f"Successfully deleted {len(files_to_delete)} files from the index.",
        }

    @staticmethod
    def _run_list_documents(args: dict) -> dict:
        import logging
        from pathlib import Path
        from klyra.utils import fs
        
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)
        logger.info("Executing list_documents tool...")
        
        # Normalize subject to lowercase because Klyra's voice transcriptions often capitalize it,
        # and the underlying Linux folders expect lowercase names.
        if "subject" in args and isinstance(args["subject"], str):
            args["subject"] = args["subject"].lower()
        
        subject = args["subject"]
        folder = args.get("folder", "")
        
        try:
            logger.info(f"Target subject: '{subject}'")
            logger.info(f"Target folder: '{folder}'")
            
            subject_dir = fs.subject_dir(subject).resolve()
            target_dir = (subject_dir / folder).resolve()
            
            logger.info(f"Resolved path: {target_dir}")
            
            if not str(target_dir).startswith(str(subject_dir)):
                logger.error(f"Security error: Path {folder} is outside the subject's docs directory.")
                return {"message": f"Error: Path {folder} is outside the subject's docs directory."}
                
            if not target_dir.exists() or not target_dir.is_dir():
                logger.error(f"Directory not found: {target_dir}")
                return {"message": f"Error: Folder '{folder}' not found in docs/{subject}/"}
                
            files = sorted([str(p.relative_to(subject_dir)) for p in target_dir.rglob("*") if p.is_file()])
            
            if not files:
                logger.info("No files found.")
                return {"message": f"No files found in docs/{subject}/{folder}"}
                
            logger.info(f"Found {len(files)} files.")
                
            return {"message": f"Files available in docs/{subject}/{folder}:\n- " + "\n- ".join(files)}
        except Exception as exc:
            return {"message": f"Error listing documents: {exc}"}

    @staticmethod
    def _run_read_document(args: dict) -> dict:
        import logging
        from pathlib import Path
        from klyra.utils import fs
        import io
        import re
        import difflib
        
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)

        subject = args["subject"]
        filename = args["filename"]
        
        logger.info(f"Executing read_document tool...")
        logger.info(f"Target subject: '{subject}'")
        logger.info(f"Target file: '{filename}'")
        
        # Resolve path safely inside the subject's docs directory
        subject_dir = fs.subject_dir(subject).resolve()
        target_path = (subject_dir / filename).resolve()
        
        logger.info(f"Resolved path: {target_path}")
        
        # If the exact path doesn't exist, try to find it recursively anywhere in the subject's folder
        if not target_path.exists():
            logger.info("File not found at exact path. Attempting recursive search...")
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
            
        logger.info("Reading document content...")
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

    @staticmethod
    def _run_generate_diagram(args: dict) -> dict:
        from klyra.tools.latex_diagram_tool import GenerateDiagramTool
        import re
        
        tool = GenerateDiagramTool()
        try:
            result = tool._run(
                subject=args["subject"],
                latex_body=args["latex_body"],
                caption=args["caption"],
                ground_truth=args["ground_truth"]
            )
            match = re.search(r'\[.*?\]\((.*?)\)', result)
            if match:
                rel_path = match.group(1)
                file_name = rel_path.split("/")[-1]
                web_path = f"/files/{args['subject']}/assets/generated/{file_name}"
                result = result.replace(rel_path, web_path)
                
            return {"status": "success", "markdown": result}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    @staticmethod
    def _run_get_subject_skill(args: dict) -> dict:
        from klyra.services.skill_service import (
            load_subject_skill,
            render_subject_skill_markdown,
        )
        from klyra.utils import fs

        subject = args["subject"]
        cfg = load_subject_skill(subject)
        path = fs.subject_skill_path(subject)
        md = path.read_text(encoding="utf-8") if path.exists() else render_subject_skill_markdown(cfg)
        return {
            "message": f"Skill configuration for '{subject}':\n\n{md}",
            "config": cfg.model_dump(mode="json"),
        }

    @staticmethod
    def _run_update_subject_skill(args: dict) -> dict:
        from klyra.services.skill_service import (
            parse_subject_skill,
            render_subject_skill_markdown,
        )
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

    @staticmethod
    def _run_verify_coding_package(args: dict) -> dict:
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
