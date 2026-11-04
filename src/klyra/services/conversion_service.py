"""Format conversion service with deterministic Pandoc/Exam fast-path and LLM fallback."""
from __future__ import annotations

import re
from typing import Any


def _try_direct_conversion(subject: str, request: str) -> str | None:
    """Attempt deterministic zero-token conversion when `request` targets on-disk
    `.md` -> `.tex` or `.tex` -> `.md` (without unrendered TikZ blocks)."""
    from klyra.tools.format_conversion import (
        ConvertLatexToMarkdownTool,
        ConvertMarkdownToLatexTool,
    )
    from klyra.utils import fs

    req_low = (request or "").lower()
    gen_dir = fs.generated_dir(subject)
    if not gen_dir.exists():
        return None

    # Case 1: Explicit `.tex` -> `.md` conversion when no TikZ blocks need LLM recovery
    tex_matches = re.findall(r"([A-Za-z0-9_.-]+\.tex)\b", request)
    if tex_matches and ("markdown" in req_low or ".md" in req_low):
        valid_tex: list[str] = []
        for tname in tex_matches:
            tpath = gen_dir / tname
            if not tpath.exists():
                return None
            raw_tex = tpath.read_text(encoding="utf-8", errors="ignore")
            if "\\begin{tikzpicture}" in raw_tex:
                return None
            valid_tex.append(tname)
        if valid_tex:
            tool_l2m = ConvertLatexToMarkdownTool()
            return "\n\n".join(
                tool_l2m._run(subject=subject, source_filename=fname)
                for fname in dict.fromkeys(valid_tex)
            )

    # Case 2: `.md` -> `.tex` (LaTeX / Overleaf / Exam) conversion
    if not any(k in req_low for k in ("latex", ".tex", "overleaf", "exam")):
        return None
    if "markdown" in req_low and any(
        req_low.find(k) < req_low.find("markdown") for k in ("latex", ".tex") if k in req_low
    ):
        return None

    md_matches = re.findall(r"([A-Za-z0-9_.-]+\.md)\b", request)
    candidates: list[str] = [m for m in md_matches if (gen_dir / m).exists()]
    if not candidates:
        for p in sorted(gen_dir.glob("*-qp*.md")):
            if p.stem.lower() in req_low:
                candidates.append(p.name)
    if not candidates:
        return None

    tool_m2l = ConvertMarkdownToLatexTool()
    outputs = [
        tool_m2l._run(subject=subject, source_filename=fname)
        for fname in dict.fromkeys(candidates)
    ]
    return "\n\n".join(outputs)


# Backward-compatible alias
_try_direct_md_to_latex = _try_direct_conversion


def run_conversion(subject: str, request: str) -> Any:
    from klyra.crews.format_converter_crew.format_converter_crew import (
        FormatConverterCrew,
    )
    from klyra.services.skill_service import build_skill_prompt_inputs
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    direct_result = _try_direct_conversion(subject, request)
    if direct_result is not None:
        return direct_result

    inputs = {
        "subject": subject,
        "conversion_request": request,
        **build_skill_prompt_inputs(subject),
    }
    with fs.crewai_storage_context(f".db/{subject}"):
        return FormatConverterCrew().crew().kickoff(inputs=inputs)
