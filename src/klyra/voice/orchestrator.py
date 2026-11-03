"""Gemini Live API voice orchestrator — transport-agnostic core.

``VoiceOrchestrator`` manages the full lifecycle of a Gemini Live
session: connecting, streaming audio in/out through an ``AudioTransport``,
handling tool calls, and reconnecting on session timeout.

Usage (from CLI)::

    transport = PyAudioTransport()
    orchestrator = VoiceOrchestrator(transport, voice_name="Puck")
    asyncio.run(orchestrator.run())
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import socket
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# Fix IPv6 websocket handshake timeout on some Linux/WSL systems
_old_getaddrinfo = socket.getaddrinfo
def _ipv4_only_getaddrinfo(*args, **kwargs):
    responses = _old_getaddrinfo(*args, **kwargs)
    return [r for r in responses if r[0] == socket.AF_INET] or responses
socket.getaddrinfo = _ipv4_only_getaddrinfo

# Defaults (overridable via env vars)
_DEFAULT_MODEL = "gemini-3.1-flash-live-preview"
_DEFAULT_VOICE = "Puck"
_RECONNECT_DELAY_SECONDS = 2
_MAX_RECONNECT_ATTEMPTS = 5

from klyra.core import SYSTEM_INSTRUCTION, VOICE_ADDENDUM

_SYSTEM_INSTRUCTION = SYSTEM_INSTRUCTION + VOICE_ADDENDUM


class VoiceOrchestrator:
    """Transport-agnostic Gemini Live session manager.

    Parameters
    ----------
    transport : AudioTransport
        The audio I/O backend (e.g. ``PyAudioTransport`` for CLI).
    voice_name : str | None
        Prebuilt voice name.  Falls back to ``KLYRA_VOICE_NAME`` env var,
        then to ``"Puck"``.
    """

    def __init__(
        self,
        transport,  # AudioTransport — import-free for type hint
        voice_name: str | None = None,
    ) -> None:
        self._transport = transport
        self._voice_name = (
            voice_name
            or os.environ.get("KLYRA_VOICE_NAME", _DEFAULT_VOICE)
        )
        self._model = os.environ.get("KLYRA_VOICE_MODEL", _DEFAULT_MODEL)

        # Resolved lazily in run() to avoid top-level heavy imports
        self._client = None
        self._session = None
        self._executor = None
        self._shutdown_event = asyncio.Event()
        self._chat_history = []
        self._current_model_response = ""

    # ── Public entry point ──────────────────────────────────────────

    async def run(self) -> None:
        """Main entry point. Connects to the Live API and handles reconnects."""
        from google import genai
        from google.genai import types
        from klyra.voice.tool_declarations import get_all_declarations
        from klyra.voice.tool_executor import ToolExecutor
        import sys
        import threading
        import os

        class _ThreadAwareStream:
            def __init__(self, original_stream):
                self.original_stream = original_stream

            def write(self, s):
                self.original_stream.write(s)

            def flush(self):
                self.original_stream.flush()

            def __getattr__(self, name):
                return getattr(self.original_stream, name)

        original_stdout = sys.stdout
        original_stderr = sys.stderr
        
        # Open a log file for background tool executions
        try:
            bg_log = open("voice_tools.log", "w", encoding="utf-8")
        except Exception:
            bg_log = None

        try:
            old_stderr_fd = os.dup(2)
            devnull_fd = os.open(os.devnull, os.O_WRONLY)
            sys.stderr.flush()
            os.dup2(devnull_fd, 2)
            new_stderr = os.fdopen(os.dup(old_stderr_fd), "w")
        except Exception:
            new_stderr = sys.stderr
            old_stderr_fd = None
            devnull_fd = None

        sys.stdout = _ThreadAwareStream(sys.stdout)
        sys.stderr = _ThreadAwareStream(new_stderr)

        api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        if not api_key:
            raise SystemExit(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) must be set in your .env file."
            )

        self._client = genai.Client(api_key=api_key)
        
        self._executor = ToolExecutor()

        # Start audio transport
        await self._transport.start()

        print(f"\n  🎙  Klyra voice ready.  (model={self._model}, voice={self._voice_name})")
        print("  Speak naturally. Press Ctrl+C to exit.\n")

        attempt = 0
        try:
            while not self._shutdown_event.is_set():
                # Build LiveConnectConfig dynamically to inject history
                sys_inst = _SYSTEM_INSTRUCTION
                if self._chat_history:
                    history_text = "\n".join(f"{role.upper()}: {text}" for role, text in self._chat_history[-15:])
                    sys_inst += f"\n\n### RECENT CONVERSATION HISTORY (for context after reconnection):\n{history_text}"

                config = types.LiveConnectConfig(
                    response_modalities=["AUDIO"],
                    speech_config=types.SpeechConfig(
                        voice_config=types.VoiceConfig(
                            prebuilt_voice_config=types.PrebuiltVoiceConfig(
                                voice_name=self._voice_name,
                            )
                        )
                    ),
                    system_instruction=types.Content(
                        parts=[types.Part(text=sys_inst)]
                    ),
                    tools=[
                        types.Tool(
                            function_declarations=[
                                types.FunctionDeclaration(**decl)
                                for decl in get_all_declarations()
                            ]
                        )
                    ],
                )

                # Patch IPv4 for asyncio to avoid IPv6 WSL timeouts
                loop = asyncio.get_running_loop()
                if not hasattr(loop, "_ipv4_patched"):
                    orig_getaddrinfo = loop.getaddrinfo
                    async def _ipv4_getaddrinfo(*args, **kwargs):
                        import socket
                        res = await orig_getaddrinfo(*args, **kwargs)
                        return [r for r in res if r[0] == socket.AF_INET] or res
                    loop.getaddrinfo = _ipv4_getaddrinfo
                    loop._ipv4_patched = True

                try:
                    await self._session_loop(config)
                except Exception as exc:
                    if self._shutdown_event.is_set():
                        break
                    attempt += 1
                    if attempt > _MAX_RECONNECT_ATTEMPTS:
                        logger.error(
                            "Max reconnect attempts (%d) reached, giving up.",
                            _MAX_RECONNECT_ATTEMPTS,
                        )
                        raise
                    logger.debug(
                        "Session ended (%s). Reconnecting in %ds (attempt %d/%d)...",
                        exc,
                        _RECONNECT_DELAY_SECONDS,
                        attempt,
                        _MAX_RECONNECT_ATTEMPTS,
                    )
                    print(
                        f"\n  ⟳  Session ended ({exc}). Reconnecting "
                        f"({attempt}/{_MAX_RECONNECT_ATTEMPTS})...\n"
                    )
                    
                    # Restart transport to recover from PyAudio Stream closed errors
                    try:
                        await self._transport.stop()
                    except Exception:
                        pass
                    
                    await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
                    
                    try:
                        await self._transport.start()
                    except Exception:
                        pass
                else:
                    # Clean session end - we should just reconnect and keep listening
                    print("\n  ⟳  Session closed by server. Reconnecting...\n")
                    await asyncio.sleep(_RECONNECT_DELAY_SECONDS)
                    continue
        except KeyboardInterrupt:
            import logging
            logging.getLogger("asyncio").setLevel(logging.CRITICAL)
            pass
        finally:
            sys.stdout = original_stdout
            sys.stderr = original_stderr
            if old_stderr_fd is not None:
                try:
                    os.dup2(old_stderr_fd, 2)
                    os.close(old_stderr_fd)
                    os.close(devnull_fd)
                except Exception:
                    pass
            print("\n  👋  Klyra voice session ended.\n")
            await self._transport.stop()

    # ── Session loop ────────────────────────────────────────────────

    async def _session_loop(self, config) -> None:
        """Single Gemini Live session — runs until timeout or error."""
        async with self._client.aio.live.connect(
            model=self._model, config=config
        ) as session:
            self._session = session
            logger.info("Gemini Live session connected")
            print("  ✓  Connected. Listening...\n", flush=True)

            # Two concurrent tasks
            input_task = asyncio.create_task(
                self._stream_input(session), name="voice-input"
            )
            output_task = asyncio.create_task(
                self._stream_output(session), name="voice-output"
            )

            try:
                # Wait until any task finishes (error or session end)
                done, pending = await asyncio.wait(
                    [input_task, output_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Cancel remaining tasks
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except (asyncio.CancelledError, Exception):
                        pass

                # Collect exceptions from completed tasks (retrieves them
                # so Python won't print "Task exception was never retrieved")
                real_exc = None
                for task in done:
                    try:
                        exc = task.exception()
                    except asyncio.CancelledError:
                        continue
                    if exc is not None:
                        # APIError "1000" is just a normal WebSocket close — harmless
                        exc_str = str(exc).strip()
                        if exc_str in ("1000 None.", "1000", "1") or "1000" in exc_str:
                            logger.debug("Session closed normally (1000)")
                            continue
                        if real_exc is None:
                            real_exc = exc
                if real_exc is not None:
                    raise real_exc
            except asyncio.CancelledError:
                pass
            finally:
                # Ensure we retrieve exceptions from tasks to prevent
                # "Task exception was never retrieved" warnings
                for task in [input_task, output_task]:
                    if task.done() and not task.cancelled():
                        try:
                            task.exception()
                        except (asyncio.CancelledError, Exception):
                            pass
                self._session = None

    # ── Audio streaming ─────────────────────────────────────────────

    async def _stream_input(self, session) -> None:
        """Continuously read mic audio and send to Gemini."""
        while not self._shutdown_event.is_set():
            try:
                chunk = await self._transport.read_chunk()
                if chunk:
                    await session.send_realtime_input(
                        audio={"data": chunk, "mime_type": "audio/pcm;rate=16000"}
                    )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if self._shutdown_event.is_set():
                    return
                if "1000" in str(exc) or "ConnectionClosedOK" in type(exc).__name__:
                    return
                logger.debug("Input stream error: %s", exc)
                raise

    async def _stream_output(self, session) -> None:
        """Receive responses from Gemini — audio playback + tool handling."""
        agent_speaking = False
        try:
            while not self._shutdown_event.is_set():
                had_responses = False
                async for response in session.receive():
                    had_responses = True
                    # 1. Handle tool calls
                    if (
                        response.tool_call is not None
                        and response.tool_call.function_calls
                    ):
                        if agent_speaking:
                            print("\033[0m\n", end="", flush=True)
                            agent_speaking = False
                        else:
                            print("\n", end="", flush=True)
                        await self._handle_tool_calls(
                            session, response.tool_call.function_calls
                        )

                    # 2. Extract directly from server_content to avoid SDK warnings
                    # which are triggered by accessing response.text or response.data
                    if response.server_content is not None:
                        # Handle user transcription
                        if response.server_content.interim_input_transcription:
                            if agent_speaking:
                                print("\033[0m\n", end="", flush=True)
                                agent_speaking = False
                            print(f"\r  🎙  You: {response.server_content.interim_input_transcription.text}", end="\033[K", flush=True)
                        
                        if response.server_content.input_transcription:
                            if agent_speaking:
                                print("\033[0m\n", end="", flush=True)
                                agent_speaking = False
                            user_text = response.server_content.input_transcription.text
                            print(f"\r  🎙  You: {user_text}\033[K")
                            self._chat_history.append(("user", user_text))

                        # Handle agent text (from output_transcription)
                        if response.server_content.output_transcription:
                            if not agent_speaking:
                                print("\r  🤖  Klyra: \033[36m", end="", flush=True)
                                agent_speaking = True
                            print(f"{response.server_content.output_transcription.text}", end="", flush=True)
                            self._current_model_response += response.server_content.output_transcription.text

                        # Handle agent response (text and audio)
                        if response.server_content.model_turn is not None:
                            for part in response.server_content.model_turn.parts:
                                if part.inline_data and part.inline_data.data:
                                    await self._transport.write_chunk(part.inline_data.data)
                                if part.text:
                                    if not agent_speaking:
                                        print("\r  🤖  Klyra: \033[36m", end="", flush=True)
                                        agent_speaking = True
                                    print(f"{part.text}", end="", flush=True)
                                    self._current_model_response += part.text
                        
                        # Handle barge-in interruption
                        if response.server_content.interrupted:
                            if self._current_model_response:
                                self._chat_history.append(("model", self._current_model_response))
                                self._current_model_response = ""
                            if agent_speaking:
                                print("\033[0m\n", end="", flush=True)
                                agent_speaking = False

                        if response.server_content.turn_complete:
                            if self._current_model_response:
                                self._chat_history.append(("model", self._current_model_response))
                                self._current_model_response = ""
                            if agent_speaking:
                                print("\033[0m\n", end="", flush=True)
                                agent_speaking = False

                if not had_responses:
                    # Clean session end / EOF from server
                    break

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            if self._shutdown_event.is_set():
                return
            if "1000" in str(exc) or "ConnectionClosedOK" in type(exc).__name__:
                return
            logger.debug("Output stream error: %s", exc)
            raise

    # ── Tool call handling ──────────────────────────────────────────

    async def _handle_tool_calls(self, session, function_calls) -> None:
        """Execute tool calls and send results back to Gemini."""
        from google.genai import types

        responses = []
        for fc in function_calls:
            logger.info("Tool call: %s(%s)", fc.name, fc.args)
            print(f"  ⚙  Executing: {fc.name}...")

            result = await self._executor.execute(fc.name, dict(fc.args))

            if result.get("status") == "success":
                print(f"  ✓  {fc.name} completed.")
            else:
                print(f"  ✗  {fc.name} failed: {result.get('message', 'unknown error')}")

            responses.append(
                types.FunctionResponse(
                    name=fc.name,
                    id=fc.id,
                    response=result,
                )
            )

        # Send all responses back to Gemini
        await session.send_tool_response(
            function_responses=responses
        )
