"""WebSocket chat & voice multiplexed endpoint (`/ws`) for Klyra Web."""
from __future__ import annotations

import asyncio
import json
import logging
import struct

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from klyra.auth.dependencies import websocket_user
from klyra.storage import repo
from klyra.web.routes.sessions import (
    ACTIVE_SESSIONS,
    _load_session_from_db,
    _save_session_to_db,
)
from klyra.web.routes.ws_voice import _run_voice_session, _sanitize, _stop_voice

logger = logging.getLogger(__name__)

router = APIRouter()


@router.websocket("/ws")
async def websocket_chat(ws: WebSocket, session_id: str = None):
    """Handle a single chat/voice session over WebSocket (auth required)."""
    user = websocket_user(ws)
    if user is None:
        await ws.close(code=4401)
        return

    await ws.accept()
    logger.info("WebSocket connected (user=%s)", user.username)

    from klyra.chat import ChatOrchestrator

    if session_id:
        existing = repo.get_chat_session(session_id)
        if existing is not None and existing.owner_id != user.id and user.role != "admin":
            await ws.close(code=4403)
            return

    chat_history_for_voice: list[dict] = []
    voice_session_task = None
    voice_session_ref = {"session": None, "active": False}
    allowed_subjects = None if user.role == "admin" else set(repo.user_courses(user))

    def _sync_save():
        if session_id:
            title = "New Chat"
            if len(chat_history_for_voice) > 0:
                for e in chat_history_for_voice:
                    if isinstance(e, dict) and e.get("type") == "user_transcript":
                        title = e.get("text", "")
                        break
                    elif isinstance(e, (list, tuple)):
                        title = e[1]
                        break
            title = (title[:30] + "...") if len(title) > 30 else title
            _save_session_to_db(session_id, user.id, title, orchestrator, chat_history_for_voice)

    async def on_text(text: str):
        event = {"type": "klyra_text", "text": text}
        chat_history_for_voice.append(event)
        await ws.send_json(event)

    async def on_tool_start(name: str, args: dict):
        event = {"type": "tool_start", "tool": name, "args": _sanitize(args)}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_tool_done(name: str, status: str, message: str):
        event = {"type": "tool_done", "tool": name, "status": status, "message": message[:500]}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_file_link(name: str, url: str, file_type: str):
        event = {"type": "file_link", "name": name, "url": url, "file_type": file_type}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_tool_log(name: str, text: str):
        try:
            await ws.send_json({"type": "tool_log", "tool": name, "text": text})
        except Exception as e:
            logger.debug(f"Failed to send tool log: {e}")

    if session_id:
        if session_id in ACTIVE_SESSIONS:
            session_data = ACTIVE_SESSIONS[session_id]
            orchestrator = session_data["orchestrator"]
            chat_history_for_voice = session_data["history"]
        else:
            db_data = _load_session_from_db(session_id)
            if db_data:
                chat_history_for_voice = db_data.get("ui_history", [])
                orchestrator = ChatOrchestrator(
                    on_text=on_text,
                    on_tool_start=on_tool_start,
                    on_tool_done=on_tool_done,
                    on_file_link=on_file_link,
                    on_tool_log=on_tool_log,
                    allowed_subjects=allowed_subjects,
                    user_id=user.id,
                )
                await orchestrator.initialize()
                orchestrator.load_from_dict(db_data.get("gemini_history", {}))
                ACTIVE_SESSIONS[session_id] = {
                    "orchestrator": orchestrator,
                    "history": chat_history_for_voice,
                }

    if session_id and session_id in ACTIVE_SESSIONS:
        orchestrator._on_text = on_text
        orchestrator._on_tool_start = on_tool_start
        orchestrator._on_tool_done = on_tool_done
        orchestrator._on_file_link = on_file_link
        orchestrator._on_tool_log = on_tool_log
        if orchestrator._executor:
            orchestrator._executor.on_log_callback = on_tool_log

        for event in chat_history_for_voice:
            if isinstance(event, (list, tuple)):
                role, text = event
                if role == "user":
                    await ws.send_json({"type": "user_transcript", "text": text, "final": True})
                elif role == "model":
                    await ws.send_json({"type": "klyra_text", "text": text})
                    await ws.send_json({"type": "turn_complete"})
            else:
                await ws.send_json(event)
    else:
        orchestrator = ChatOrchestrator(
            on_text=on_text,
            on_tool_start=on_tool_start,
            on_tool_done=on_tool_done,
            on_file_link=on_file_link,
            on_tool_log=on_tool_log,
            allowed_subjects=allowed_subjects,
            user_id=user.id,
        )
        await orchestrator.initialize()
        if session_id:
            ACTIVE_SESSIONS[session_id] = {
                "orchestrator": orchestrator,
                "history": chat_history_for_voice,
            }
    text_session_task = None

    try:
        while True:
            raw = await ws.receive()

            if "bytes" in raw and raw["bytes"]:
                audio_data = raw["bytes"]
                if voice_session_ref["active"] and voice_session_ref["session"]:
                    try:
                        if not hasattr(ws, "_audio_bytes"):
                            ws._audio_bytes = 0
                            ws._audio_sum = 0
                        ws._audio_bytes += len(audio_data)
                        shorts = struct.unpack(f"<{len(audio_data)//2}h", audio_data)
                        ws._audio_sum += sum(abs(x) for x in shorts)

                        if ws._audio_bytes > 16000 * 2:
                            avg = ws._audio_sum / (ws._audio_bytes / 2)
                            logger.info(
                                f"Received {ws._audio_bytes} bytes from web. Avg Volume: {avg:.2f}"
                            )
                            ws._audio_bytes = 0
                            ws._audio_sum = 0

                        await voice_session_ref["session"].send_realtime_input(
                            audio={"data": audio_data, "mime_type": "audio/pcm;rate=16000"}
                        )
                    except Exception as e:
                        logger.debug("Failed to send audio: %s", e)
                continue

            if "text" in raw and raw["text"]:
                try:
                    msg = json.loads(raw["text"])
                except json.JSONDecodeError:
                    continue

                msg_type = msg.get("type", "")

                if msg_type == "chat":
                    user_text = msg.get("text", "").strip()
                    if not user_text:
                        continue

                    if voice_session_ref["active"]:
                        await _stop_voice(voice_session_task, voice_session_ref)

                    user_event = {"type": "user_transcript", "text": user_text, "final": True}
                    chat_history_for_voice.append(user_event)
                    _sync_save()

                    async def process_chat(text):
                        try:
                            await orchestrator.send_message(text)
                        except asyncio.CancelledError:
                            logger.info("Chat task cancelled by user")
                            await ws.send_json(
                                {
                                    "type": "tool_log",
                                    "tool": "System",
                                    "text": "Generation stopped by user.",
                                }
                            )
                        except Exception as e:
                            logger.error("Error in chat task: %s", e)
                        finally:
                            chat_history_for_voice.append({"type": "turn_complete"})
                            try:
                                await ws.send_json({"type": "turn_complete"})
                            except Exception:
                                pass
                            _sync_save()

                    text_session_task = asyncio.create_task(process_chat(user_text))

                elif msg_type == "chat_stop":
                    if text_session_task and not text_session_task.done():
                        text_session_task.cancel()
                        text_session_task = None

                elif msg_type == "voice_start":
                    if not voice_session_ref["active"]:
                        voice_session_task = asyncio.create_task(
                            _run_voice_session(
                                ws,
                                voice_session_ref,
                                chat_history_for_voice,
                                orchestrator,
                                session_id,
                                user,
                                _sync_save,
                                allowed_subjects,
                            )
                        )

                elif msg_type == "voice_stop":
                    await _stop_voice(voice_session_task, voice_session_ref)
                    voice_session_task = None

                elif msg_type == "clear_chat":
                    orchestrator._history.clear()
                    chat_history_for_voice.clear()
                    if session_id and session_id in ACTIVE_SESSIONS:
                        del ACTIVE_SESSIONS[session_id]
                    if voice_session_ref["active"]:
                        await _stop_voice(voice_session_task, voice_session_ref)
                        voice_session_task = None

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
    except RuntimeError as e:
        if "disconnect message has been received" in str(e):
            logger.info("WebSocket disconnected (RuntimeError)")
        else:
            logger.error("WebSocket error: %s", e, exc_info=True)
    except Exception as e:
        logger.error("WebSocket error: %s", e, exc_info=True)
    finally:
        await _stop_voice(voice_session_task, voice_session_ref)

