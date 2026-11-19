"""Text-only chat orchestrator using the standard Gemini API.

Used by both ``klyra chat`` (CLI) and the web UI's text-chat mode.
Tool calls are dispatched through the same ``ToolExecutor`` that the
voice interface uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import AsyncIterator

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_DEFAULT_CHAT_MODEL = "gemini-3.5-flash-lite"


class ChatOrchestrator:
    """Manages a Gemini chat session with tool-call support.

    Parameters
    ----------
    on_text : callable(str) | None
        Streaming text callback.  Called with each chunk of text as Gemini
        produces it.  If *None*, text is accumulated and returned at the end.
    on_tool_start : callable(name, args) | None
        Called when a tool call begins.
    on_tool_done : callable(name, status, message) | None
        Called when a tool call finishes.
    """

    def __init__(
        self,
        on_text=None,
        on_tool_start=None,
        on_tool_done=None,
        on_file_link=None,
        on_tool_log=None,
        allowed_subjects: set[str] | None = None,
        user_id: str | None = None,
    ):
        self._on_text = on_text
        self._on_tool_start = on_tool_start
        self._on_tool_done = on_tool_done
        self._on_file_link = on_file_link
        self._on_tool_log = on_tool_log
        # RBAC: None → unrestricted (admin/CLI); otherwise tool calls are
        # limited to these subjects (courses). user_id tags generated files.
        self._allowed_subjects = allowed_subjects
        self._user_id = user_id
        self._model = os.environ.get("KLYRA_CHAT_MODEL") or os.environ.get("KLYRA_MODEL", _DEFAULT_CHAT_MODEL)
        self._client = None
        self._chat = None
        self._executor = None
        self._history: list[dict] = []
        self._has_uploaded_attachments: bool = False

    async def initialize(self):
        """Set up the Gemini client and chat session."""
        from google import genai
        from google.genai import types
        from klyra.voice.tool_declarations import get_all_declarations
        from klyra.voice.tool_executor import ToolExecutor
        from klyra.core import SYSTEM_INSTRUCTION

        load_dotenv()
        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise SystemExit("GEMINI_API_KEY (or GOOGLE_API_KEY) must be set in your .env file.")

        self._client = genai.Client(api_key=api_key)
        self._executor = ToolExecutor(
            on_log_callback=self._on_tool_log,
            allowed_subjects=self._allowed_subjects,
        )

        decls = get_all_declarations()
        func_decls = [types.FunctionDeclaration(**d) for d in decls]
        from klyra.chat.guardrails import build_subject_guardrail_instruction

        sys_inst = SYSTEM_INSTRUCTION + build_subject_guardrail_instruction(self._allowed_subjects)

        self._config = types.GenerateContentConfig(
            system_instruction=sys_inst,
            tools=[types.Tool(function_declarations=func_decls)],
            temperature=0.7,
        )

    @staticmethod
    def _is_transient_error(exc: Exception) -> bool:
        err_str = str(exc).upper()
        return any(
            token in err_str
            for token in (
                "503",
                "UNAVAILABLE",
                "429",
                "RESOURCE_EXHAUSTED",
                "500",
                "502",
                "504",
                "DEADLINE_EXCEEDED",
                "OVERLOADED",
            )
        )

    async def _generate_with_retry(self):
        """Call `generate_content` with automatic backoff on transient 503/429 errors."""
        delays = (2.0, 4.0)
        last_exc = None
        for attempt in range(len(delays) + 1):
            try:
                return await self._client.aio.models.generate_content(
                    model=self._model,
                    contents=self._history,
                    config=self._config,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                if not self._is_transient_error(exc) or attempt >= len(delays):
                    raise
                wait_s = delays[attempt]
                logger.warning(
                    "Transient Gemini error (%s), retrying in %.1fs (attempt %d/%d)...",
                    exc,
                    wait_s,
                    attempt + 1,
                    len(delays),
                )
                await asyncio.sleep(wait_s)
        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise RuntimeError("Gemini generation failed after retries.")  # pragma: no cover

    async def _generate_stream_round(self):
        """Stream one Gemini response round, emitting text chunks incrementally.

        Returns ``(assembled_content, function_calls, streamed_text)``.
        Falls back to `_generate_with_retry()` if the client lacks `generate_content_stream`.
        """
        from google.genai import types

        models_api = getattr(getattr(self._client, "aio", None), "models", None)
        stream_fn = getattr(models_api, "generate_content_stream", None)
        if not callable(stream_fn):
            response = await self._generate_with_retry()
            if not response.candidates:
                return None, [], ""
            cand = response.candidates[0]
            parts = list(cand.content.parts) if (cand.content and cand.content.parts) else []
            fcs = [p for p in parts if getattr(p, "function_call", None)]
            text_acc = ""
            if not fcs:
                for p in parts:
                    if getattr(p, "text", None) and not getattr(p, "thought", False):
                        text_acc += p.text
                        if self._on_text:
                            await _maybe_await(self._on_text(p.text))
            return cand.content, fcs, text_acc

        delays = (2.0, 4.0)
        last_exc: Exception | None = None
        for attempt in range(len(delays) + 1):
            emitted_any = False
            text_acc = ""
            raw_parts: list = []
            function_calls: list = []
            try:
                stream = await stream_fn(
                    model=self._model,
                    contents=self._history,
                    config=self._config,
                )
                async for chunk in stream:
                    if not getattr(chunk, "candidates", None):
                        continue
                    cand = chunk.candidates[0]
                    content = getattr(cand, "content", None)
                    parts = getattr(content, "parts", None) or []
                    for part in parts:
                        if getattr(part, "thought", False):
                            continue
                        if getattr(part, "function_call", None):
                            function_calls.append(part)
                            raw_parts.append(part)
                        elif getattr(part, "text", None):
                            delta = part.text
                            text_acc += delta
                            emitted_any = True
                            if self._on_text:
                                await _maybe_await(self._on_text(delta))
                assembled_parts: list = []
                if text_acc:
                    assembled_parts.append(types.Part(text=text_acc))
                assembled_parts.extend(raw_parts)
                assembled_content = (
                    types.Content(role="model", parts=assembled_parts)
                    if assembled_parts
                    else None
                )
                return assembled_content, function_calls, text_acc
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                if emitted_any or not self._is_transient_error(exc) or attempt >= len(delays):
                    raise
                wait_s = delays[attempt]
                logger.warning(
                    "Transient Gemini stream error (%s), retrying in %.1fs (attempt %d/%d)...",
                    exc,
                    wait_s,
                    attempt + 1,
                    len(delays),
                )
                await asyncio.sleep(wait_s)
        if last_exc is not None:  # pragma: no cover
            raise last_exc
        raise RuntimeError("Gemini streaming failed after retries.")  # pragma: no cover

    @staticmethod
    def _extract_docx_text(fp: Path, max_chars: int = 60000) -> str:
        """Extract plain text paragraphs from a .docx file using stdlib zipfile + XML."""
        import zipfile
        import xml.etree.ElementTree as ET

        with zipfile.ZipFile(fp) as zf:
            xml_bytes = zf.read("word/document.xml")
        root = ET.fromstring(xml_bytes)
        paragraphs: list[str] = []
        for p in root.iter():
            if p.tag.endswith("}p"):
                runs = [t.text for t in p.iter() if t.tag.endswith("}t") and t.text]
                if runs:
                    paragraphs.append("".join(runs))
        return "\n".join(paragraphs)[:max_chars]

    async def send_message(
        self,
        user_text: str,
        attachments: list[dict] | None = None,
    ) -> str:
        """Send a user message (with optional image/file attachments) and stream Klyra's response.

        Enforces assigned-subject guardrails (deterministic pre-check + LLM verification)
        before calling Gemini, streams text deltas via ``on_text``, and handles tool calls
        automatically in a loop until Gemini finishes.
        """
        from google.genai import types
        from klyra.chat.guardrails import (
            check_prompt_subject_scope,
            verify_request_against_indexed_syllabus,
        )

        # 1. Fast pre-check (empty assigned subjects or explicit unassigned docs/ folder)
        allowed, refusal = check_prompt_subject_scope(
            user_text,
            self._allowed_subjects,
            has_attachments=bool(attachments),
        )
        if not allowed:
            self._history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
            self._history.append(types.Content(role="model", parts=[types.Part(text=refusal)]))
            if self._on_text:
                await _maybe_await(self._on_text(refusal))
            return refusal

        # 2. Load attached artifacts (images, PDFs, DOCX, text/code) before syllabus verification
        #    so the verifier inspects both the prompt AND the actual artifact content.
        user_parts: list = []
        att_notes: list[str] = []
        binary_image_mimes = {"image/png", "image/jpeg", "image/gif", "image/webp"}
        text_exts = {
            ".txt", ".md", ".tex", ".csv", ".json", ".py", ".c", ".cpp", ".h",
            ".java", ".js", ".ts", ".sql", ".sh", ".bash", ".yaml", ".yml",
            ".xml", ".html", ".css", ".svg", ".log", ".rtf",
        }

        if attachments:
            for att in attachments:
                att_path = att.get("path")
                if not att_path:
                    continue
                fp = Path(att_path)
                if not fp.exists() or not fp.is_file():
                    continue
                ext = fp.suffix.lower()
                mime_type = str(att.get("mime_type") or "application/octet-stream").split(";")[0].strip().lower()
                name = str(att.get("name") or fp.name)
                rel_path = str(att.get("rel_path") or name)
                try:
                    if mime_type in binary_image_mimes:
                        raw_bytes = fp.read_bytes()
                        user_parts.append(
                            types.Part.from_bytes(data=raw_bytes, mime_type=mime_type)
                        )
                        att_notes.append(f"[Attached image: {name} ({rel_path})]")
                    elif ext == ".pdf" or mime_type == "application/pdf":
                        raw_bytes = fp.read_bytes()
                        user_parts.append(
                            types.Part.from_bytes(data=raw_bytes, mime_type="application/pdf")
                        )
                        att_notes.append(f"[Attached PDF document: {name} ({rel_path})]")
                    elif ext == ".docx":
                        doc_text = self._extract_docx_text(fp)
                        att_notes.append(
                            f"[Attached DOCX document: {name} ({rel_path})]\n{doc_text}"
                        )
                    elif ext in text_exts or mime_type.startswith("text/") or mime_type == "application/json":
                        file_text = fp.read_text(encoding="utf-8", errors="replace")[:60000]
                        att_notes.append(
                            f"[Attached file: {name} ({rel_path})]\n```\n{file_text}\n```"
                        )
                    else:
                        att_notes.append(f"[Attached file: {name} ({rel_path}, {mime_type})]")
                except Exception as exc:
                    logger.warning("Failed to read attachment %s: %s", fp, exc)

        # 3. Dynamic indexed-syllabus verification check (prompt + attached artifacts)
        allowed_syl, refusal_syl = await verify_request_against_indexed_syllabus(
            self._client,
            self._model,
            user_text,
            self._allowed_subjects,
            attachment_parts=user_parts or None,
            attachment_notes=att_notes or None,
            history_summary=self.get_history_summary(),
        )
        if not allowed_syl:
            self._history.append(types.Content(role="user", parts=[types.Part(text=user_text)]))
            self._history.append(types.Content(role="model", parts=[types.Part(text=refusal_syl)]))
            if self._on_text:
                await _maybe_await(self._on_text(refusal_syl))
            return refusal_syl

        history_snapshot_len = len(self._history)

        effective_text = (user_text or "").strip()
        if not effective_text and att_notes:
            effective_text = "Please inspect and help with the attached artifact(s)."
        if att_notes:
            self._has_uploaded_attachments = True
            from klyra.chat.guardrails import fetch_indexed_syllabus_context

            target_subjs: set[str] = set()
            for att in attachments or []:
                s = str(att.get("subject") or "").strip().lower()
                if s and (self._allowed_subjects is None or s in self._allowed_subjects):
                    target_subjs.add(s)
            if not target_subjs and self._allowed_subjects:
                target_subjs = set(self._allowed_subjects)

            alignment_note = (
                "\n\n[SYLLABUS ALIGNMENT REQUIREMENT FOR ATTACHED ARTIFACTS]: "
                "If the user asks to prepare or generate questions, a question paper, coding problems, "
                "or teaching material based on the attached artifact(s), you MUST align every question "
                "strictly with the indexed syllabus of the target subject (only using concepts, scope, "
                "and notation covered in the indexed syllabus; omit or reframe any out-of-syllabus parts). "
                "When calling generate_question_paper or related tools, include the relevant artifact "
                "details in `notes` and enforce strict alignment with the subject's indexed syllabus."
            )
            if target_subjs:
                syl_map = fetch_indexed_syllabus_context(
                    target_subjs,
                    query_text=effective_text,
                    max_concepts_per_subject=40,
                    max_chars_per_subject=1200,
                )
                syl_block = "\n".join(
                    f"[{subj}] {summary}" for subj, summary in syl_map.items() if summary
                )
                if syl_block:
                    alignment_note += f"\nIndexed syllabus reference:\n{syl_block}"

            effective_text = f"{effective_text}\n\n" + "\n".join(att_notes) + alignment_note

        user_parts.append(types.Part(text=effective_text))
        self._history.append(types.Content(role="user", parts=user_parts))

        full_response = ""
        max_tool_rounds = 10

        try:
            for _ in range(max_tool_rounds):
                model_content, function_calls, streamed_text = await self._generate_stream_round()

                if function_calls:
                    if model_content is not None:
                        self._history.append(model_content)

                    func_response_parts = []
                    for part in function_calls:
                        fc = part.function_call
                        tool_name = fc.name
                        tool_args = dict(fc.args) if fc.args else {}

                        if (
                            self._has_uploaded_attachments
                            and tool_name in {
                                "generate_question_paper",
                                "revise_question_paper",
                                "generate_coding_problems",
                                "prepare_teaching_material",
                            }
                        ):
                            subj_label = str(tool_args.get("subject") or "the target subject")
                            directive = (
                                f"STRICT INDEXED-SYLLABUS ALIGNMENT: Any questions based on uploaded "
                                f"artifact(s) MUST strictly align with the indexed syllabus of '{subj_label}'. "
                                "Cross-check all topics against search_syllabus and exclude out-of-syllabus content."
                            )
                            existing_notes = str(tool_args.get("notes") or "").strip()
                            if "STRICT INDEXED-SYLLABUS ALIGNMENT" not in existing_notes:
                                tool_args["notes"] = (
                                    f"{existing_notes}\n\n{directive}" if existing_notes else directive
                                )

                        if self._on_tool_start:
                            await _maybe_await(self._on_tool_start(tool_name, tool_args))

                        import time as _time
                        tool_start_time = _time.time()
                        result = await self._executor.execute(tool_name, tool_args)

                        status = result.get("status", "error")
                        message = result.get("message", "")

                        if self._on_tool_done:
                            await _maybe_await(self._on_tool_done(tool_name, status, message))

                        if status == "success" and self._on_file_link:
                            await self._detect_files(tool_name, tool_args, message, since_time=tool_start_time)

                        func_response_parts.append(
                            types.Part(
                                function_response=types.FunctionResponse(
                                    name=tool_name,
                                    response=result,
                                )
                            )
                        )

                    self._history.append(
                        types.Content(role="user", parts=func_response_parts)
                    )
                    continue

                if streamed_text:
                    full_response += streamed_text
                if model_content is not None:
                    self._history.append(model_content)
                break
        except asyncio.CancelledError:
            if not full_response:
                del self._history[history_snapshot_len:]
            raise
        except Exception:
            if not full_response:
                del self._history[history_snapshot_len:]
            raise

        if full_response and self._on_file_link:
            await self._emit_download_cards_from_text(full_response)

        return full_response

    async def _emit_download_cards_from_text(self, text: str) -> None:
        """If the assistant's text mentions `/api/download/<subject>/<folder>` or `/files/<subject>/.../<name>.zip`,
        resolve it on disk, tag file ownership, and emit a `.zip` file card."""
        import re
        from pathlib import Path
        from klyra.web.routes.files import (
            _collect_prefix_bundle_files,
            _resolve_download_target,
        )

        seen: set[str] = set()
        candidates: list[tuple[str, str]] = []
        for m in re.finditer(r"/api/download/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_./-]+)", text):
            candidates.append((m.group(1).lower(), m.group(2).rstrip(".,;:)")))
        for m in re.finditer(r"/files/([a-zA-Z0-9_-]+)/([a-zA-Z0-9_./-]+\.zip)", text):
            candidates.append((m.group(1).lower(), m.group(2).rstrip(".,;:)")))

        docs_root = Path("docs").resolve()
        for subj, folder in candidates:
            subj_root = (docs_root / subj).resolve()
            target = _resolve_download_target(subj, folder)
            if target is not None and target.exists() and target.is_dir():
                key = str(target.resolve())
                if key in seen:
                    continue
                seen.add(key)
                if self._user_id:
                    for p in target.rglob("*"):
                        if p.is_file():
                            try:
                                rel_p = p.resolve().relative_to(docs_root).as_posix()
                                from klyra.storage import repo as _repo

                                _repo.set_file_owner(rel_p, self._user_id, subj)
                            except Exception:
                                pass
                try:
                    rel_to_subj = target.resolve().relative_to(subj_root).as_posix()
                except ValueError:
                    rel_to_subj = folder
                await _maybe_await(
                    self._on_file_link(
                        name=f"{target.name}.zip",
                        url=f"/api/download/{subj}/{rel_to_subj}",
                        file_type=".zip",
                    )
                )
            else:
                prefix, bundle_files = _collect_prefix_bundle_files(subj, folder)
                if not bundle_files:
                    continue
                key = f"{subj}:{prefix}"
                if key in seen:
                    continue
                seen.add(key)
                if self._user_id:
                    for p in bundle_files:
                        try:
                            rel_p = p.resolve().relative_to(docs_root).as_posix()
                            from klyra.storage import repo as _repo

                            _repo.set_file_owner(rel_p, self._user_id, subj)
                        except Exception:
                            pass
                await _maybe_await(
                    self._on_file_link(
                        name=f"{prefix}.zip",
                        url=f"/api/download/{subj}/{prefix}",
                        file_type=".zip",
                    )
                )

    async def _detect_files(self, tool_name, tool_args, message, since_time: float | None = None):
        """Detect generated files and diagram images from tool results and send file links."""
        import time
        from pathlib import Path

        subject = str(tool_args.get("subject", "")).strip().lower()
        if not subject:
            return

        now = time.time()
        # Use tool start time (with a 2s buffer) so files created in early CrewAI tasks
        # (like question_generator diagrams & -qp.md) are never missed when later tasks take >60s.
        cutoff = (since_time - 2.0) if since_time is not None else (now - 180.0)

        # Read-only tools shouldn't re-emit recently created files unless they are read_document on an image
        readonly_tools = {"list_documents", "get_subject_skill", "verify_coding_package"}
        if tool_name in readonly_tools:
            return

        if tool_name == "read_document":
            # If the user asked to read/display a specific file or image, emit any referenced images
            import re
            for m in re.finditer(r"!\[([^\]]*)\]\((/files/[^)]+|\.\.?/\.assets/[^)]+)\)", message or ""):
                img_url = m.group(2)
                if not img_url.startswith("/files/"):
                    clean_rel = img_url.lstrip("./")
                    img_url = f"/files/{subject}/{clean_rel}"
                img_name = Path(img_url).name
                img_ext = Path(img_url).suffix.lower() or ".png"
                if self._on_file_link:
                    await _maybe_await(
                        self._on_file_link(
                            name=img_name,
                            url=img_url,
                            file_type=img_ext,
                        )
                    )
            return

        docs_dir = Path("docs") / subject / "generated"
        if docs_dir.exists():
            # 1. Detect coding package folders (for .zip download)
            # Packages live under docs/<subject>/generated/coding/<pkg> (or nested problem-N)
            # or directly under docs/<subject>/generated/<pkg>.
            pkg_dirs: list[Path] = []
            coding_root = docs_dir / "coding"
            if coding_root.exists() and coding_root.is_dir():
                for d in sorted(coding_root.iterdir()):
                    if d.is_dir():
                        pkg_dirs.append(d)
            for d in sorted(docs_dir.iterdir()):
                if d.is_dir() and d.name not in {"coding", "teaching"}:
                    pkg_dirs.append(d)

            emitted_pkg_roots: list[Path] = []
            target_pkg_name = str(tool_args.get("name", "")).strip().lower()
            for d in pkg_dirs:
                all_pkg_files = [p for p in d.rglob("*") if p.is_file()]
                is_recent = (
                    (target_pkg_name and d.name.lower() == target_pkg_name)
                    or d.stat().st_mtime >= cutoff
                    or any(p.stat().st_mtime >= cutoff for p in all_pkg_files)
                )
                if not is_recent:
                    continue
                emitted_pkg_roots.append(d.resolve())
                # Tag ownership on all files inside the package so zip download RBAC succeeds
                if self._user_id:
                    for p in all_pkg_files:
                        try:
                            rel_p = p.relative_to(Path("docs")).as_posix()
                            from klyra.storage import repo as _repo
                            _repo.set_file_owner(rel_p, self._user_id, subject)
                        except Exception:
                            pass
                rel_to_subject = d.relative_to(Path("docs") / subject).as_posix()
                if self._on_file_link:
                    await _maybe_await(
                        self._on_file_link(
                            name=f"{d.name}.zip",
                            url=f"/api/download/{subject}/{rel_to_subject}",
                            file_type=".zip",
                        )
                    )

            # 2. Detect individual top-level files (skip internal test_case/solution files inside zipped packages)
            for f in sorted(docs_dir.rglob("*")):
                if f.is_file() and f.stat().st_mtime >= cutoff:
                    rel_path = f.relative_to(Path("docs"))
                    # Tag ownership so other users can't read this file (RBAC)
                    if self._user_id:
                        try:
                            from klyra.storage import repo as _repo
                            _repo.set_file_owner(rel_path.as_posix(), self._user_id, subject)
                        except Exception:
                            pass
                    # Skip internal structured JSON sidecar files in UI file cards
                    if f.suffix.lower() == ".json":
                        continue
                    # Skip files inside a coding package directory that was already emitted as a .zip card
                    f_resolved = f.resolve()
                    if any(
                        str(f_resolved).startswith(str(pkg_root) + "/")
                        for pkg_root in emitted_pkg_roots
                    ):
                        continue
                    if self._on_file_link:
                        await _maybe_await(
                            self._on_file_link(
                                name=f.name,
                                url=f"/files/{rel_path.as_posix()}",
                                file_type=f.suffix.lower(),
                            )
                        )

            # 2b. Also emit a bundled .zip card when a Question Paper (`qp_name`) or Teaching Session (`session_name`) was generated/revised
            bundle_name = str(
                tool_args.get("qp_name") or tool_args.get("session_name") or ""
            ).strip()
            if bundle_name and self._on_file_link:
                from klyra.utils.fs import slugify

                slug = slugify(bundle_name)
                await _maybe_await(
                    self._on_file_link(
                        name=f"{slug}.zip",
                        url=f"/api/download/{subject}/{slug}",
                        file_type=".zip",
                    )
                )

        # 3. Detect newly generated diagram/graph images in docs/<subject>/.assets/generated/
        assets_gen_dir = Path("docs") / subject / ".assets" / "generated"
        if assets_gen_dir.exists():
            image_exts = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif"}
            for img_file in sorted(assets_gen_dir.iterdir()):
                if (
                    img_file.is_file()
                    and img_file.suffix.lower() in image_exts
                    and img_file.stat().st_mtime >= cutoff
                ):
                    rel_path = img_file.relative_to(Path("docs"))
                    if self._on_file_link:
                        await _maybe_await(
                            self._on_file_link(
                                name=img_file.name,
                                url=f"/files/{rel_path.as_posix()}",
                                file_type=img_file.suffix.lower(),
                            )
                        )

    def get_history_summary(self) -> list[tuple[str, str]]:
        """Return chat history as [(role, text), ...] for display."""
        result = []
        for content in self._history:
            role = content.role if hasattr(content, 'role') else 'user'
            texts = []
            if hasattr(content, 'parts'):
                for p in content.parts:
                    if hasattr(p, 'text') and p.text:
                        texts.append(p.text)
            if texts:
                result.append((role, " ".join(texts)))
        return result

    def to_dict(self) -> dict:
        """Serialize the internal Gemini history to a dictionary.

        Strips binary inline_data blobs so SQLite session storage stays compact;
        filename/path references remain in the accompanying text parts.
        """
        import json

        serialized = []
        for content in self._history:
            dumped = json.loads(content.model_dump_json())
            parts = dumped.get("parts")
            if isinstance(parts, list):
                cleaned_parts = []
                for p in parts:
                    if isinstance(p, dict) and p.get("inline_data"):
                        mime = (p["inline_data"] or {}).get("mime_type") or "image"
                        cleaned_parts.append({"text": f"[Attached {mime} artifact]"})
                    else:
                        cleaned_parts.append(p)
                dumped["parts"] = cleaned_parts
            serialized.append(dumped)
        return {"history": serialized}

    def load_from_dict(self, data: dict):
        """Restore the internal Gemini history from a dictionary."""
        from google.genai import types
        self._history = []
        for item in data.get("history", []):
            try:
                self._history.append(types.Content(**item))
            except Exception as e:
                logger.warning("Failed to restore history item: %s", e)


async def _maybe_await(result):
    """Await if coroutine, otherwise return as-is."""
    if asyncio.iscoroutine(result) or asyncio.isfuture(result):
        return await result
    return result
