"""Dynamic indexed-syllabus subject guardrails & verification checks for Klyra.

Enforces that non-admin users only receive answers and artifact analysis related to
the actual indexed syllabus/materials of their assigned subject(s). Contains NO
hardcoded subject-alias dictionaries — all scope context is derived dynamically
from Qdrant indexed syllabus/parsed collections and the subject's `docs/` folder.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_META_OR_GREETING_RE = re.compile(
    r"^\s*(?:"
    r"(?:hi|hello|hey|good\s+(?:morning|afternoon|evening)|greetings|thanks|thank\s+you|ok|okay|sure|yes|no|bye|goodbye)[!.?,\s]*"
    r"|(?:who\s+are\s+you|what\s+can\s+you\s+do|help|what\s+subjects?\s+.*assigned.*|which\s+subjects?\s+.*assigned.*|my\s+assigned\s+(?:subjects?|courses?)|list\s+my\s+(?:subjects?|courses?))"
    r")\s*$",
    re.IGNORECASE,
)


def _docs_base(docs_dir: Path | None = None) -> Path:
    if docs_dir is not None:
        return docs_dir
    try:
        from klyra.web.routes.files import _docs_dir

        return _docs_dir()
    except Exception:
        return Path.cwd() / "docs"


def discover_docs_subjects(docs_dir: Path | None = None) -> set[str]:
    """Dynamically list all subject folders currently present under `docs/`."""
    base = _docs_base(docs_dir)
    if not base.exists():
        return set()
    return {
        entry.name.lower()
        for entry in base.iterdir()
        if entry.is_dir() and not entry.name.startswith(".")
    }


def _slug_pattern(slug: str) -> re.Pattern[str]:
    escaped = re.escape(slug.strip().lower())
    return re.compile(rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])", re.IGNORECASE)


def _mentions_any_slug(text: str, slugs: set[str]) -> set[str]:
    if not text or not slugs:
        return set()
    matched: set[str] = set()
    for slug in slugs:
        variants = {slug}
        if "_" in slug:
            variants.add(slug.replace("_", " "))
        if "-" in slug:
            variants.add(slug.replace("-", " "))
        if any(_slug_pattern(v).search(text) for v in variants if v):
            matched.add(slug)
    return matched


def is_meta_or_greeting(text: str) -> bool:
    """Return True for pure greetings or meta questions about Klyra / assigned subjects."""
    return bool(_META_OR_GREETING_RE.match((text or "").strip()))


def format_out_of_scope_reply(allowed_subjects: set[str], *, has_attachments: bool = False) -> str:
    """Return a clear refusal stating Klyra does not know outside assigned subject syllabi."""
    if not allowed_subjects:
        return (
            "I don't know. You currently have no assigned subjects, so I cannot answer "
            "subject questions or analyze artifacts. Please ask an administrator to assign your subjects."
        )
    subj_list = ", ".join(sorted(allowed_subjects))
    if has_attachments:
        return (
            f"I don't know. Your request or attached artifact does not appear to be related "
            f"to the indexed syllabus of your assigned subject(s): **{subj_list}**."
        )
    return (
        f"I don't know. I can only answer questions and generate content related to the "
        f"indexed syllabus of your assigned subject(s): **{subj_list}**."
    )


def fetch_indexed_syllabus_context(
    allowed_subjects: set[str],
    *,
    query_text: str = "",
    docs_dir: Path | None = None,
    max_concepts_per_subject: int = 80,
    max_chars_per_subject: int = 2400,
) -> dict[str, str]:
    """Dynamically load indexed syllabus topics/snippets from Qdrant (with on-disk fallback).

    Returns ``{subject: formatted_syllabus_summary}``.
    """
    from klyra.tools import vector_store

    base = _docs_base(docs_dir)
    summaries: dict[str, str] = {}

    for subj in sorted({s.strip().lower() for s in allowed_subjects if s and s.strip()}):
        parts: list[str] = []

        # 1. Indexed syllabus concepts & entries from Qdrant (<subject>-syllabus)
        try:
            syl_col = vector_store.syllabus_collection_name(subj)
            concepts = vector_store.list_distinct_metadata_values(syl_col, "concept")
            if concepts:
                parts.append(
                    "Indexed syllabus concepts: "
                    + ", ".join(concepts[:max_concepts_per_subject])
                )

            syl_entries = vector_store.get_by_metadata(syl_col, limit=25)
            entry_lines: list[str] = []
            for item in syl_entries:
                meta = item.get("metadata") or {}
                week = meta.get("week")
                concept = meta.get("concept") or ""
                txt = (item.get("text") or "").strip().replace("\n", " ")[:180]
                prefix = f"Week {week}: " if week is not None else ""
                line = f"- {prefix}{concept} — {txt}" if concept and txt else f"- {prefix}{concept or txt}"
                if line.strip("- "):
                    entry_lines.append(line)
            if entry_lines:
                parts.append("Syllabus entries:\n" + "\n".join(entry_lines[:20]))

            # If a prompt query is available, also pull top hybrid search matches from syllabus
            if query_text.strip() and syl_entries:
                try:
                    q_hits = vector_store.query(syl_col, query_text=query_text[:400], n_results=5)
                    matched = [
                        (hit.get("text") or "").strip().replace("\n", " ")[:200]
                        for hit in q_hits
                        if isinstance(hit, dict) and hit.get("text")
                    ]
                    if matched:
                        parts.append("Closest indexed syllabus matches:\n- " + "\n- ".join(matched))
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("Syllabus collection read skipped for %s: %s", subj, exc)

        # 2. Fallback / supplement: parsed & PYQ indexed collections in Qdrant
        if not parts:
            try:
                parsed_col = vector_store.parsed_collection_name(subj)
                parsed_files = vector_store.list_distinct_metadata_values(parsed_col, "source_file")
                if parsed_files:
                    parts.append("Indexed source files: " + ", ".join(parsed_files[:30]))
                parsed_docs = vector_store.get_by_metadata(parsed_col, limit=10)
                if parsed_docs:
                    snippets = [
                        (d.get("text") or "").strip().replace("\n", " ")[:180]
                        for d in parsed_docs
                        if d.get("text")
                    ]
                    if snippets:
                        parts.append("Indexed content excerpts:\n- " + "\n- ".join(snippets[:8]))
            except Exception as exc:
                logger.debug("Parsed collection read skipped for %s: %s", subj, exc)

            try:
                pyq_col = vector_store.pyq_collection_name(subj)
                pyq_docs = vector_store.get_by_metadata(pyq_col, limit=8)
                if pyq_docs:
                    snippets = [
                        (d.get("text") or "").strip().replace("\n", " ")[:160]
                        for d in pyq_docs
                        if d.get("text")
                    ]
                    if snippets:
                        parts.append("Indexed PYQ excerpts:\n- " + "\n- ".join(snippets[:6]))
            except Exception as exc:
                logger.debug("PYQ collection read skipped for %s: %s", subj, exc)

        # 3. On-disk fallback (subject skill.md & non-generated folder/file names)
        subj_dir = base / subj
        if subj_dir.exists() and subj_dir.is_dir():
            skill_file = subj_dir / "skill.md"
            if skill_file.exists():
                try:
                    skill_text = skill_file.read_text(encoding="utf-8", errors="replace").strip()[:700]
                    if skill_text:
                        parts.append(f"Subject profile (skill.md):\n{skill_text}")
                except OSError:
                    pass
            if not parts:
                disk_files = [
                    p.relative_to(subj_dir).as_posix()
                    for p in sorted(subj_dir.rglob("*"))
                    if p.is_file()
                    and not p.name.startswith(".")
                    and "generated" not in p.relative_to(subj_dir).parts
                ][:25]
                if disk_files:
                    parts.append("Course source files: " + ", ".join(disk_files))

        combined = "\n".join(parts).strip()
        summaries[subj] = combined[:max_chars_per_subject] if combined else "(No syllabus indexed yet)"

    return summaries


def build_subject_guardrail_instruction(
    allowed_subjects: set[str] | None,
    *,
    docs_dir: Path | None = None,
) -> str:
    """Build a dynamic system-instruction guardrail grounded in the user's assigned subjects."""
    if allowed_subjects is None:
        return ""
    if not allowed_subjects:
        return (
            "\n\nSTRICT ASSIGNED-SUBJECT GUARDRAIL:\n"
            "This user has NO subjects assigned to their account.\n"
            "For any subject question, artifact analysis, or content generation request, you MUST reply: "
            "\"I don't know. You currently have no assigned subjects. Please contact an administrator.\"\n"
            "Do NOT answer academic or general-knowledge questions."
        )

    allowed_norm = sorted({s.strip().lower() for s in allowed_subjects if s and s.strip()})
    subj_list = ", ".join(allowed_norm)
    syllabus_map = fetch_indexed_syllabus_context(
        set(allowed_norm),
        docs_dir=docs_dir,
        max_concepts_per_subject=40,
        max_chars_per_subject=1200,
    )
    syllabus_block = "\n".join(
        f"[{subj}]\n{summary}" for subj, summary in syllabus_map.items() if summary
    )

    return (
        "\n\nSTRICT ASSIGNED-SUBJECT SYLLABUS GUARDRAIL (MANDATORY):\n"
        f"This user is ONLY assigned to the following subject(s): {subj_list}.\n"
        "1. You MUST restrict EVERY explanation, answer, uploaded image/file artifact analysis, and content generation strictly to the indexed syllabus of the user's assigned subject(s).\n"
        "2. If the user's question OR any attached image/file artifact is NOT related to the indexed syllabus of "
        f"({subj_list}), you MUST refuse and reply: "
        f"\"I don't know. I can only answer questions and analyze artifacts related to the indexed syllabus of your assigned subject(s): {subj_list}.\"\n"
        "3. NEVER answer out-of-scope questions or describe unrelated artifacts from general pretraining knowledge.\n"
        "4. You may still greet the user or list their assigned subjects if asked.\n\n"
        f"Indexed syllabus overview for assigned subject(s):\n{syllabus_block}"
    )


def check_prompt_subject_scope(
    user_text: str,
    allowed_subjects: set[str] | None,
    *,
    known_subjects: set[str] | None = None,
    has_attachments: bool = False,
    docs_dir: Path | None = None,
) -> tuple[bool, str]:
    """Fast deterministic pre-check against dynamically discovered `docs/` subject slugs.

    Never uses hardcoded subject alias lists; handles empty assigned-subject accounts
    and explicit references to another subject folder on disk.
    """
    if allowed_subjects is None:
        return True, ""

    allowed_norm = {s.strip().lower() for s in allowed_subjects if s and s.strip()}
    text = (user_text or "").strip()

    if is_meta_or_greeting(text) and not has_attachments:
        return True, ""

    if not allowed_norm:
        return False, format_out_of_scope_reply(allowed_norm, has_attachments=has_attachments)

    if not text:
        return True, ""

    all_known = (
        {s.strip().lower() for s in known_subjects if s and s.strip()}
        if known_subjects is not None
        else discover_docs_subjects(docs_dir=docs_dir)
    ) | allowed_norm

    mentioned_allowed = _mentions_any_slug(text, allowed_norm)
    disallowed_slugs = all_known - allowed_norm
    mentioned_disallowed = _mentions_any_slug(text, disallowed_slugs)

    if mentioned_disallowed and not mentioned_allowed:
        logger.info(
            "Subject guardrail blocked prompt targeting unassigned subject folder(s) %s (allowed=%s, has_attachments=%s)",
            sorted(mentioned_disallowed),
            sorted(allowed_norm),
            has_attachments,
        )
        return False, format_out_of_scope_reply(allowed_norm, has_attachments=has_attachments)

    return True, ""


async def verify_request_against_indexed_syllabus(
    client,
    model: str,
    user_text: str,
    allowed_subjects: set[str] | None,
    *,
    attachment_parts: list | None = None,
    attachment_notes: list[str] | None = None,
    history_summary: list[tuple[str, str]] | None = None,
    docs_dir: Path | None = None,
) -> tuple[bool, str]:
    """Verify whether the prompt AND any attached artifacts relate to the indexed syllabus
    of the user's assigned subject(s).

    Inspects the real indexed syllabus from Qdrant (no hardcoded subject maps) and passes
    any attached image/PDF/document parts directly into the verifier model so off-syllabus
    artifacts (e.g. uploading an unrelated image/file and saying 'describe') are caught.
    """
    if allowed_subjects is None or client is None:
        return True, ""

    allowed_norm = {s.strip().lower() for s in allowed_subjects if s and s.strip()}
    has_attachments = bool(attachment_parts or attachment_notes)
    if not allowed_norm:
        return False, format_out_of_scope_reply(allowed_norm, has_attachments=has_attachments)

    text = (user_text or "").strip()
    if not has_attachments and (not text or is_meta_or_greeting(text)):
        return True, ""

    syllabus_map = fetch_indexed_syllabus_context(
        allowed_norm,
        query_text=text,
        docs_dir=docs_dir,
    )
    subj_list = ", ".join(sorted(allowed_norm))
    syllabus_text = "\n\n".join(
        f"=== Assigned Subject: {subj} ===\n{summary}"
        for subj, summary in syllabus_map.items()
    )

    context_lines: list[str] = []
    if history_summary:
        for role, msg in history_summary[-4:]:
            snippet = (msg or "").strip().replace("\n", " ")[:240]
            if snippet:
                context_lines.append(f"{role}: {snippet}")

    verifier_text = (
        "You are a strict syllabus-scope verifier for an academic assistant.\n"
        f"User's assigned subject(s): {subj_list}\n\n"
        f"Indexed syllabus & course material for the user's assigned subject(s):\n{syllabus_text}\n\n"
        "Task: Decide whether the user's request AND any attached artifact(s) (images, PDFs, documents, code) "
        "are related to the indexed syllabus / course topics of the user's assigned subject(s).\n"
        "- Reply ALLOW if:\n"
        "  (a) The prompt (and any attached artifact content, if attachments are provided) relates to the indexed syllabus "
        "or core academic topics of the user's assigned subject(s), OR\n"
        "  (b) It is a valid follow-up/parameter answer to an ongoing in-scope conversation for the assigned subject(s), OR\n"
        "  (c) It asks about Klyra's capabilities or which subjects are assigned.\n"
        "- Reply DENY if:\n"
        "  (a) The prompt asks about a different academic subject, unrelated programming language, or general non-syllabus trivia, OR\n"
        "  (b) Any attached image/file artifact is clearly unrelated to the indexed syllabus of the user's assigned subject(s).\n"
        "Output strictly a single word: ALLOW or DENY.\n\n"
    )
    if context_lines:
        verifier_text += "Recent conversation:\n" + "\n".join(context_lines) + "\n\n"
    if attachment_notes:
        verifier_text += "Attached artifact details:\n" + "\n".join(attachment_notes)[:8000] + "\n\n"
    verifier_text += f"User message: {text or '(no text — inspect attached artifact(s))'}\nDecision:"

    try:
        from google.genai import types

        verify_parts: list = []
        if attachment_parts:
            verify_parts.extend(attachment_parts)
        verify_parts.append(types.Part(text=verifier_text))

        resp = await client.aio.models.generate_content(
            model=model,
            contents=[types.Content(role="user", parts=verify_parts)],
            config=types.GenerateContentConfig(
                temperature=0.0,
                max_output_tokens=16,
            ),
        )
        verdict = (getattr(resp, "text", None) or "").strip().upper()
        if verdict.startswith("DENY"):
            logger.info(
                "Indexed-syllabus verifier DENIED request (allowed=%s, attachments=%s): %s",
                sorted(allowed_norm),
                has_attachments,
                text[:120],
            )
            return False, format_out_of_scope_reply(allowed_norm, has_attachments=has_attachments)
    except Exception as exc:
        logger.debug("Indexed-syllabus verifier skipped due to error: %s", exc)

    return True, ""
