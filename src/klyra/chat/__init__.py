"""Text-only chat orchestrator using the standard Gemini API.

Used by both ``klyra chat`` (CLI) and the web UI's text-chat mode.
Tool calls are dispatched through the same ``ToolExecutor`` that the
voice interface uses.
"""
from __future__ import annotations

import asyncio
import logging
import os
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

        self._config = types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            tools=[types.Tool(function_declarations=func_decls)],
            temperature=0.7,
        )

    async def send_message(self, user_text: str) -> str:
        """Send a user message and get Klyra's full response.

        Handles tool calls automatically in a loop until Gemini returns
        a text response.  Callbacks (on_text, on_tool_start, etc.) are
        invoked along the way.

        Returns the full accumulated text response.
        """
        from google.genai import types

        self._history.append(
            types.Content(role="user", parts=[types.Part(text=user_text)])
        )

        full_response = ""
        max_tool_rounds = 10

        for _ in range(max_tool_rounds):
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=self._history,
                config=self._config,
            )

            if not response.candidates:
                break

            candidate = response.candidates[0]
            parts = candidate.content.parts if candidate.content else []

            # Check for function calls
            function_calls = [p for p in parts if p.function_call]
            text_parts = [p for p in parts if p.text]

            if function_calls:
                # Add the model's function-call response to history
                self._history.append(candidate.content)

                # Execute each function call
                func_response_parts = []
                for part in function_calls:
                    fc = part.function_call
                    tool_name = fc.name
                    tool_args = dict(fc.args) if fc.args else {}

                    if self._on_tool_start:
                        await _maybe_await(self._on_tool_start(tool_name, tool_args))

                    import time as _time
                    tool_start_time = _time.time()
                    result = await self._executor.execute(tool_name, tool_args)

                    status = result.get("status", "error")
                    message = result.get("message", "")

                    if self._on_tool_done:
                        await _maybe_await(self._on_tool_done(tool_name, status, message))

                    # Detect generated files and images
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

                # Send function responses back
                self._history.append(
                    types.Content(role="user", parts=func_response_parts)
                )
                # Loop to get Gemini's text response after tool execution
                continue

            # Text response — stream it out
            for part in text_parts:
                if part.text:
                    full_response += part.text
                    if self._on_text:
                        await _maybe_await(self._on_text(part.text))

            # Add the model's text response to history
            if candidate.content:
                self._history.append(candidate.content)
            break

        return full_response

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
            # 1. Detect folders (for zip download)
            for d in sorted(docs_dir.iterdir()):
                if d.is_dir() and d.stat().st_mtime >= cutoff:
                    if self._on_file_link:
                        await _maybe_await(
                            self._on_file_link(
                                name=f"{d.name}.zip",
                                url=f"/api/download/{subject}/{d.name}",
                                file_type=".zip",
                            )
                        )

            # 2. Detect individual files
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
                    if self._on_file_link:
                        await _maybe_await(
                            self._on_file_link(
                                name=f.name,
                                url=f"/files/{rel_path.as_posix()}",
                                file_type=f.suffix.lower(),
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
        """Serialize the internal Gemini history to a dictionary."""
        import json
        serialized = []
        for content in self._history:
            # model_dump_json() handles Pydantic bytes -> base64 serialization
            serialized.append(json.loads(content.model_dump_json()))
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
