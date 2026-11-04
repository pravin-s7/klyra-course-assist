"""Gemini Live API browser WebSocket voice session handler."""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import time

from fastapi import WebSocket

logger = logging.getLogger(__name__)


def _sanitize(args: dict) -> dict:
    """Remove confirmation tokens and limit arg display size."""
    return {k: v for k, v in args.items() if k != "confirmation_token"}


async def _stop_voice(task, ref):
    """Cancel a running voice session task."""
    ref["active"] = False
    ref["session"] = None
    if task and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass


async def _run_voice_session(
    ws: WebSocket,
    ref: dict,
    chat_history: list,
    orchestrator,
    session_id: str,
    user,
    sync_save,
    allowed_subjects: set | None = None,
):
    """Run a Gemini Live API voice session, forwarding audio/events to the WebSocket."""
    from google import genai
    from google.genai import types
    from klyra.core import SYSTEM_INSTRUCTION
    from klyra.voice.tool_declarations import get_all_declarations
    from klyra.voice.tool_executor import ToolExecutor

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    model = os.environ.get("KLYRA_VOICE_MODEL", "gemini-3.8-live")
    voice_name = os.environ.get("KLYRA_VOICE_NAME", "Zephyr")
    client = genai.Client(api_key=api_key)

    ws_lock = asyncio.Lock()

    async def safe_send_json(data):
        async with ws_lock:
            try:
                await ws.send_json(data)
            except Exception:
                pass

    async def safe_send_bytes(data):
        async with ws_lock:
            try:
                await ws.send_bytes(data)
            except Exception:
                pass

    async def on_tool_log(name: str, text: str):
        await safe_send_json({"type": "tool_log", "tool": name, "text": text})

    executor = ToolExecutor(on_log_callback=on_tool_log, allowed_subjects=allowed_subjects)

    sys_inst = SYSTEM_INSTRUCTION
    history_lines: list[str] = []
    if orchestrator and hasattr(orchestrator, "_history") and orchestrator._history:
        for msg in orchestrator._history[-20:]:
            if not hasattr(msg, "role") or not hasattr(msg, "parts"):
                continue
            role = msg.role.upper()
            for part in msg.parts:
                if part.text:
                    history_lines.append(f"{role}: {part.text}")
                elif getattr(part, "function_call", None):
                    history_lines.append(f"{role} (TOOL CALL): {part.function_call.name}")
                elif getattr(part, "function_response", None):
                    resp_str = str(part.function_response.response)
                    if len(resp_str) > 1500:
                        resp_str = resp_str[:1500] + "... (truncated)"
                    history_lines.append(
                        f"{role} (TOOL RESULT for {part.function_response.name}): {resp_str}"
                    )

    if history_lines:
        history_text = "\n".join(history_lines)
        sys_inst += f"\n\n### RECENT CONVERSATION & TOOL HISTORY (for context):\n{history_text}"

    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=voice_name)
            )
        ),
        system_instruction=types.Content(parts=[types.Part(text=sys_inst)]),
        tools=[
            types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(**decl) for decl in get_all_declarations()
                ]
            )
        ],
    )
    loop = asyncio.get_running_loop()
    if not hasattr(loop, "_ipv4_patched"):
        orig_getaddrinfo = loop.getaddrinfo

        async def _ipv4_getaddrinfo(*args, **kwargs):
            res = await orig_getaddrinfo(*args, **kwargs)
            return [r for r in res if r[0] == socket.AF_INET] or res

        loop.getaddrinfo = _ipv4_getaddrinfo
        loop._ipv4_patched = True

    try:
        async with client.aio.live.connect(model=model, config=config) as session:
            ref["session"] = session
            ref["active"] = True
            await safe_send_json({"type": "voice_started"})

            current_model_text = ""

            async for response in session.receive():
                if response.tool_call and response.tool_call.function_calls:
                    tool_responses = []
                    for fc in response.tool_call.function_calls:
                        fc_args = dict(fc.args) if fc.args else {}
                        if allowed_subjects is not None:
                            subj = str(fc_args.get("subject", "")).lower()
                            if subj and subj not in allowed_subjects:
                                deny = {
                                    "status": "error",
                                    "message": f"Access denied: you are not assigned to the course '{subj}'.",
                                    "tool": fc.name,
                                }
                                tool_responses.append(
                                    types.FunctionResponse(name=fc.name, id=fc.id, response=deny)
                                )
                                await safe_send_json(
                                    {
                                        "type": "tool_done",
                                        "tool": fc.name,
                                        "status": "error",
                                        "message": deny["message"][:500],
                                    }
                                )
                                continue

                        orchestrator._history.append(
                            types.Content(
                                role="model",
                                parts=[
                                    types.Part(
                                        function_call=types.FunctionCall(name=fc.name, args=fc_args)
                                    )
                                ],
                            )
                        )

                        start_event = {
                            "type": "tool_start",
                            "tool": fc.name,
                            "args": _sanitize(fc_args),
                        }
                        chat_history.append(start_event)
                        await safe_send_json(start_event)

                        tool_start_time = time.time()
                        result = await executor.execute(fc.name, fc_args)
                        status = result.get("status", "error")
                        msg = result.get("message", "")

                        done_event = {
                            "type": "tool_done",
                            "tool": fc.name,
                            "status": status,
                            "message": msg[:500],
                        }
                        chat_history.append(done_event)
                        await safe_send_json(done_event)

                        if status == "success" and orchestrator is not None:
                            try:
                                await orchestrator._detect_files(
                                    fc.name, fc_args, msg, since_time=tool_start_time
                                )
                            except Exception as e:
                                logger.debug("file detection/ownership tagging failed: %s", e)

                        orchestrator._history.append(
                            types.Content(
                                role="user",
                                parts=[
                                    types.Part(
                                        function_response=types.FunctionResponse(
                                            name=fc.name, response=result
                                        )
                                    )
                                ],
                            )
                        )
                        tool_responses.append(
                            types.FunctionResponse(name=fc.name, id=fc.id, response=result)
                        )

                    if tool_responses:
                        await session.send_tool_response(function_responses=tool_responses)

                if response.server_content is not None:
                    sc = response.server_content

                    if sc.input_transcription:
                        text = sc.input_transcription.text
                        user_event = {"type": "user_transcript", "text": text, "final": True}
                        await safe_send_json(user_event)
                        chat_history.append(user_event)
                        orchestrator._history.append(
                            types.Content(role="user", parts=[types.Part(text=text)])
                        )

                    if sc.output_transcription:
                        text = sc.output_transcription.text
                        await safe_send_json({"type": "klyra_transcript", "text": text})
                        current_model_text += text

                    if sc.model_turn is not None:
                        for part in sc.model_turn.parts:
                            if part.inline_data and part.inline_data.data:
                                await safe_send_bytes(part.inline_data.data)

                    if sc.turn_complete:
                        if current_model_text:
                            model_event = {"type": "klyra_text", "text": current_model_text}
                            chat_history.append(model_event)
                            orchestrator._history.append(
                                types.Content(
                                    role="model", parts=[types.Part(text=current_model_text)]
                                )
                            )
                            current_model_text = ""

                        turn_event = {"type": "turn_complete"}
                        chat_history.append(turn_event)
                        await safe_send_json(turn_event)

                        if session_id:
                            sync_save()

    except asyncio.CancelledError:
        pass
    except Exception as e:
        e_str = str(e).lower()
        if e_str == "1" or "close" in e_str or "disconnect" in e_str or "cancelled" in e_str:
            pass
        else:
            logger.error("Voice session error: %r", e)
            try:
                await ws.send_json({"type": "voice_error", "message": str(e)})
            except Exception:
                pass
    finally:
        ref["session"] = None
        ref["active"] = False
        try:
            await ws.send_json({"type": "voice_stopped"})
        except Exception:
            pass

