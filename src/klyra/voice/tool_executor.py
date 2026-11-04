"""Bridge between Gemini Live / Chat function calls and the single-source tool registry."""
from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import sys
from typing import Any, Callable, Coroutine

from klyra.voice.tool_registry import (
    get_registered_tools,
    rewrite_markdown_asset_urls,
)

logger = logging.getLogger(__name__)


class LogStreamer(io.StringIO):
    """Intercepts stdout, prints to terminal, and sends via async callback."""

    def __init__(self, loop: asyncio.AbstractEventLoop, executor: "ToolExecutor", tool_name: str):
        super().__init__()
        self.loop = loop
        self.executor = executor
        self.tool_name = tool_name
        self._buffer = ""

    def write(self, string: str):
        sys.__stdout__.write(string)
        sys.__stdout__.flush()
        self._buffer += string
        if "\n" in self._buffer:
            lines = self._buffer.split("\n")
            self._buffer = lines.pop()
            for line in lines:
                text = line + "\n"
                if self.executor.on_log_callback and self.loop:
                    asyncio.run_coroutine_threadsafe(
                        self.executor.on_log_callback(self.tool_name, text),
                        self.loop,
                    )

    def flush(self):
        sys.__stdout__.flush()
        if self._buffer and self.executor.on_log_callback and self.loop:
            asyncio.run_coroutine_threadsafe(
                self.executor.on_log_callback(self.tool_name, self._buffer),
                self.loop,
            )
            self._buffer = ""


class ToolExecutor:
    """Stateless dispatcher backed by the single-source `tool_registry`."""

    _DISPATCH = {name: defn.handler for name, defn in get_registered_tools().items()}
    _rewrite_markdown_asset_urls = staticmethod(rewrite_markdown_asset_urls)

    def __init__(
        self,
        on_log_callback: Callable[[str, str], Coroutine[Any, Any, None]] | None = None,
        allowed_subjects: set[str] | None = None,
    ):
        self._confirmation_tokens: dict[str, str] = {}
        self.on_log_callback = on_log_callback
        self.allowed_subjects = allowed_subjects

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Dispatch *name* with *args* to the registered tool handler in a worker thread."""
        tools = get_registered_tools()
        tool_def = tools.get(name)
        if tool_def is None:
            return {"status": "error", "message": f"Unknown tool: {name}"}

        if "subject" in args and isinstance(args["subject"], str):
            args["subject"] = args["subject"].lower()

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

        handler = tool_def.handler
        logger.info("Executing tool %s with args %s", name, args)

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        def _run_with_redirect(h, a, l):
            if self.on_log_callback and l:
                streamer = LogStreamer(l, self, name)
                log_handler = logging.StreamHandler(streamer)
                log_handler.setLevel(logging.INFO)
                log_handler.setFormatter(logging.Formatter("%(message)s"))
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
            subj = str(args.get("subject", "")).strip().lower()
            if subj and isinstance(result, dict):
                for k in ("message", "markdown"):
                    if isinstance(result.get(k), str):
                        result[k] = self._rewrite_markdown_asset_urls(result[k], subj)
            return {"status": "success", **result}
        except Exception as exc:
            logger.error("Tool %s failed: %s", name, exc, exc_info=True)
            return {
                "status": "error",
                "message": str(exc),
                "tool": name,
            }
