"""WebSocket chat & voice multiplexed endpoint (`/ws`) for Klyra Web."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import struct


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from klyra.auth.dependencies import can_read_generated_file, websocket_user
from klyra.storage import repo
from klyra.web.routes.files import _resolve_docs_file
from klyra.web.routes.sessions import (
    ACTIVE_SESSIONS,
    _load_session_from_db,
    _save_session_to_db,
)
from klyra.web.routes.ws_voice import _run_voice_session, _sanitize, _stop_voice

logger = logging.getLogger(__name__)

router = APIRouter()


def _format_chat_error(exc: Exception) -> str:
    """Format an LLM / backend exception into a clear user-facing Markdown alert."""
    raw = str(exc).strip()
    upper = raw.upper()
    if "503" in upper or "UNAVAILABLE" in upper:
        return (
            "⚠️ **Model Temporarily Unavailable (503):** "
            "The Gemini model is currently experiencing high demand. "
            "Automatic retries were attempted, but the service is still busy. "
            "Please wait a few seconds and try sending your message again."
        )
    if "429" in upper or "RESOURCE_EXHAUSTED" in upper or "QUOTA" in upper:
        return (
            "⚠️ **Rate Limit / Quota Exceeded (429):** "
            "The Gemini API rate limit or quota was reached. "
            "Please wait a moment before retrying, or check your API key quota in the Admin Panel."
        )
    if "API_KEY_INVALID" in upper or "401" in upper or "403" in upper:
        return (
            "⚠️ **Authentication / Permission Error:** "
            f"The Gemini API rejected the request (`{raw[:200]}`). "
            "Please verify `GEMINI_API_KEY` in the Admin Panel."
        )
    return f"⚠️ **Error generating response:** `{raw[:300]}`"


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
                        cand = (e.get("text") or "").strip()
                        if not cand and isinstance(e.get("attachments"), list) and e["attachments"]:
                            first_att = e["attachments"][0]
                            cand = f"Attachment: {first_att.get('name', 'file')}"
                        if cand:
                            title = cand
                            break
                    elif isinstance(e, (list, tuple)) and len(e) >= 2 and e[1]:
                        title = str(e[1])
                        break
            title = (title[:30] + "...") if len(title) > 30 else title
            _save_session_to_db(session_id, user.id, title, orchestrator, chat_history_for_voice)

    turn_state = {"emitted_text": False}

    async def on_text(text: str):
        if not text:
            return
        ts = _utc_now()
        turn_state["emitted_text"] = True
        if (
            chat_history_for_voice
            and isinstance(chat_history_for_voice[-1], dict)
            and chat_history_for_voice[-1].get("type") == "klyra_text"
        ):
            chat_history_for_voice[-1]["text"] += text
            chat_history_for_voice[-1]["timestamp"] = ts
        else:
            chat_history_for_voice.append({"type": "klyra_text", "text": text, "timestamp": ts})
        await ws.send_json({"type": "klyra_text", "text": text, "timestamp": ts})

    async def on_tool_start(name: str, args: dict):
        event = {"type": "tool_start", "tool": name, "args": _sanitize(args), "timestamp": _utc_now()}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_tool_done(name: str, status: str, message: str):
        event = {"type": "tool_done", "tool": name, "status": status, "message": message[:500], "timestamp": _utc_now()}
        chat_history_for_voice.append(event)
        await ws.send_json(event)
        _sync_save()

    async def on_file_link(name: str, url: str, file_type: str):
        event = {"type": "file_link", "name": name, "url": url, "file_type": file_type, "timestamp": _utc_now()}
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
                    raw_attachments = msg.get("attachments") or []
                    stored_attachments: list[dict] = []
                    model_attachments: list[dict] = []
                    if isinstance(raw_attachments, list):
                        for item in raw_attachments[:10]:
                            if not isinstance(item, dict):
                                continue
                            rel_raw = str(item.get("rel_path") or "").strip()
                            if not rel_raw:
                                continue
                            fp, rel = _resolve_docs_file(rel_raw, user)
                            if not fp or not fp.exists() or not fp.is_file():
                                continue
                            if not can_read_generated_file(user, rel):
                                continue
                            name = str(item.get("name") or fp.name)
                            mime_type = str(item.get("mime_type") or "application/octet-stream")
                            subj = str(item.get("subject") or "")
                            ext = fp.suffix.lower()
                            is_img = bool(
                                item.get("is_image")
                                if "is_image" in item
                                else ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
                            )
                            meta = {
                                "name": name,
                                "rel_path": rel,
                                "url": f"/files/{rel}",
                                "preview_url": f"/preview/{rel}",
                                "download_url": f"/files/{rel}",
                                "mime_type": mime_type,
                                "file_type": ext,
                                "is_image": is_img,
                                "subject": subj,
                                "source": "uploaded",
                            }
                            stored_attachments.append(meta)
                            model_attachments.append({**meta, "path": str(fp)})

                    if not user_text and not stored_attachments:
                        continue

                    if voice_session_ref["active"]:
                        await _stop_voice(voice_session_task, voice_session_ref)

                    user_event = {
                        "type": "user_transcript",
                        "text": user_text,
                        "final": True,
                        "timestamp": _utc_now(),
                    }
                    if stored_attachments:
                        user_event["attachments"] = stored_attachments
                    chat_history_for_voice.append(user_event)
                    logger.info(
                        "Prompt from @%s (session=%s, attachments=%d): %s",
                        user.username,
                        session_id,
                        len(stored_attachments),
                        user_text[:120],
                    )
                    _sync_save()

                    async def process_chat(text, atts):
                        turn_state["emitted_text"] = False
                        try:
                            await orchestrator.send_message(text, attachments=atts or None)
                        except asyncio.CancelledError:
                            logger.info("Chat task cancelled by user (@%s)", user.username)
                            stop_msg = (
                                "\n\n_The response has been stopped._"
                                if turn_state["emitted_text"]
                                else "The response has been stopped."
                            )
                            try:
                                await on_text(stop_msg)
                            except Exception:
                                pass
                        except Exception as e:
                            logger.error("Error in chat task (user=@%s): %s", user.username, e)
                            err_msg = _format_chat_error(e)
                            try:
                                await on_text(err_msg)
                            except Exception:
                                pass
                        finally:
                            tc_event = {"type": "turn_complete", "timestamp": _utc_now()}
                            chat_history_for_voice.append(tc_event)
                            try:
                                await ws.send_json(tc_event)
                            except Exception:
                                pass
                            _sync_save()

                    text_session_task = asyncio.create_task(
                        process_chat(user_text, model_attachments)
                    )

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

