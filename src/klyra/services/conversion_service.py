"""Independent utility: convert generated documents between Markdown and LaTeX."""
from __future__ import annotations

from typing import Any


def _try_direct_md_to_latex(subject: str, request: str) -> str | None:
    """If `request` explicitly asks to convert a `.md` file in `docs/<subject>/generated/`
    to LaTeX/Overleaf/Exam `.tex`, run `ConvertMarkdownToLatexTool` deterministically."""
    import re
    from klyra.tools.format_conversion import ConvertMarkdownToLatexTool
    from klyra.utils import fs

    req_low = (request or "").lower()
    if not any(k in req_low for k in ("latex", ".tex", "overleaf", "exam")):
        return None
    if "markdown" in req_low and any(req_low.find(k) < req_low.find("markdown") for k in ("latex", ".tex")):
        # Converting FROM latex TO markdown -> let crew handle TikZ recovery
        return None

    gen_dir = fs.generated_dir(subject)
    if not gen_dir.exists():
        return None

    # Find explicit `.md` filenames mentioned in the request
    md_matches = re.findall(r"([A-Za-z0-9_.-]+\.md)\b", request)
    candidates: list[str] = []
    for m in md_matches:
        if (gen_dir / m).exists():
            candidates.append(m)

    if not candidates:
        # Check if a base QP name (e.g. `endterm-qp` or `endterm`) was mentioned
        for p in sorted(gen_dir.glob("*-qp*.md")):
            stem = p.stem.lower()
            if stem in req_low:
                candidates.append(p.name)

    if not candidates:
        return None

    tool = ConvertMarkdownToLatexTool()
    outputs = [tool._run(subject=subject, source_filename=fname) for fname in dict.fromkeys(candidates)]
    return "\n\n".join(outputs)


def run_conversion(subject: str, request: str) -> Any:
    import os

    os.environ["CREWAI_STORAGE_DIR"] = f".db/{subject}"

    from klyra.crews.format_converter_crew.format_converter_crew import FormatConverterCrew
    from klyra.services.skill_service import build_skill_prompt_inputs
    from klyra.utils import fs

    fs.check_subject_exists(subject)

    direct_result = _try_direct_md_to_latex(subject, request)
    if direct_result is not None:
        return direct_result

    inputs = {
        "subject": subject,
        "conversion_request": request,
        **build_skill_prompt_inputs(subject),
    }
    return FormatConverterCrew().crew().kickoff(inputs=inputs)
