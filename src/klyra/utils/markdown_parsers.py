"""Markdown-to-Pydantic fallback parsers and report enrichment helpers."""

from __future__ import annotations

import json
import re
from typing import Any, TypeVar

from crewai.tasks.task_output import TaskOutput
from pydantic import BaseModel

from klyra.crews.base_task import get_active_crew_inputs
from klyra.models.task_outputs import (
    AnswerKeyOutput,
    CodingDiscrepancyItem,
    CodingDiscrepancyReportOutput,
    CodingFinalSummaryOutput,
    CodingProblemVerdict,
    ConversionReportOutput,
    DiscrepancyReportOutput,
    DocumentParseReportOutput,
    FileConversionRecord,
    FinalReviewSummaryOutput,
    ParsedDocumentEntry,
    QuestionItem,
    QuestionPaperOutput,
    RevisedQuestionPaperOutput,
    SyllabusIndexReportOutput,
    WorkedSolution,
)
from klyra.utils.format_renderer import strip_option_prefix

T = TypeVar("T", bound=BaseModel)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
_Q_HEADER_RE = re.compile(
    r"^#{2,4}\s*Question\s+(\d+)(?:\s*\(([^)]+)\))?(?:\s*\[(MCQ|MSQ|NAT|SA)\])?(?:\s*\[Marks:\s*(\d+)\])?.*$",
    re.IGNORECASE | re.MULTILINE,
)
_OPT_LINE_RE = re.compile(
    r"^\s*(?:\(([a-zA-Z0-9]+)\)|([a-zA-Z0-9]+)[.)])\s+(.+?)\s*$"
)


def _active_subject() -> str:
    return str(get_active_crew_inputs().get("subject") or "general")


def _try_parse_json_dict(raw_text: str) -> dict[str, Any] | None:
    """Attempt to extract a JSON object from raw LLM output."""
    text = (raw_text or "").strip()
    if not text:
        return None

    # 1. Direct JSON parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    # 2. Fenced ```json ... ``` block
    fence_match = _JSON_FENCE_RE.search(text)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    # 3. Outermost { ... } substring
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidate = text[first_brace : last_brace + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return None


def _parse_qp_from_markdown(
    raw: str, subject: str, is_revised: bool = False
) -> QuestionPaperOutput | None:
    matches = list(_Q_HEADER_RE.finditer(raw))
    if not matches:
        return None

    questions: list[QuestionItem] = []
    for idx, m in enumerate(matches):
        q_num = int(m.group(1))
        q_code = (m.group(2) or f"Q{q_num:02d}").strip()
        q_type = (m.group(3) or "MCQ").upper()
        marks = int(m.group(4)) if m.group(4) else 1
        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(raw)
        block = raw[start:end].strip()

        options: list[str] = []
        stem_lines: list[str] = []
        diagram_md: str | None = None

        for line in block.splitlines():
            opt_m = _OPT_LINE_RE.match(line)
            if opt_m and q_type in {"MCQ", "MSQ"}:
                options.append(strip_option_prefix(opt_m.group(3)))
            elif _IMAGE_LINK_RE.search(line) and diagram_md is None:
                diagram_md = line.strip()
            elif line.strip() != "---":
                stem_lines.append(line)

        stem = "\n".join(stem_lines).strip()
        if not stem:
            continue

        if q_type in {"MCQ", "MSQ"} and len(options) < 2:
            # Infer NAT/SA if no options were present
            q_type = "NAT"

        questions.append(
            QuestionItem(
                question_number=q_num,
                question_code=q_code,
                question_type=q_type,  # type: ignore[arg-type]
                marks=marks,
                diagram_markdown=diagram_md,
                stem=stem,
                options=options,
            )
        )

    if not questions:
        return None

    inputs = get_active_crew_inputs()
    cls = RevisedQuestionPaperOutput if is_revised else QuestionPaperOutput
    return cls(
        subject=subject,
        title=f"{subject.upper()} — Question Paper",
        scope_label=str(inputs.get("scope_label") or ""),
        difficulty=str(inputs.get("difficulty") or "medium"),
        total_marks=sum(q.marks for q in questions),
        questions=questions,
    )


def _parse_answer_key_from_markdown(
    raw: str, subject: str
) -> AnswerKeyOutput | None:
    q_blocks = re.split(r"(?=^#{2,4}\s*Question\s+\d+)", raw, flags=re.MULTILINE)
    solutions: list[WorkedSolution] = []
    for block in q_blocks:
        m = re.match(
            r"^#{2,4}\s*Question\s+(\d+)(?:\s*\[?(MCQ|MSQ|NAT|SA)\]?)?",
            block.strip(),
            re.IGNORECASE,
        )
        if not m:
            continue
        q_num = int(m.group(1))
        q_type = (m.group(2) or "MCQ").upper()
        ans_m = re.search(
            r"(?:\*\*Correct\s+Answer:?\*\*|###\s*Answer:?|Answer:)\s*(.+)",
            block,
            re.IGNORECASE,
        )
        correct_ans = ans_m.group(1).strip() if ans_m else "See derivation below"
        solutions.append(
            WorkedSolution(
                question_number=q_num,
                question_type=q_type,  # type: ignore[arg-type]
                correct_answer=correct_ans,
                derivation=block.strip(),
            )
        )
    if not solutions:
        return None
    return AnswerKeyOutput(subject=subject, solutions=solutions)


def extract_structured_model(
    task_output: TaskOutput,
    model_cls: type[T],
    subject: str | None = None,
) -> tuple[T | None, str | None]:
    """Extract and validate `model_cls` from `task_output` (JSON first, Markdown fallback)."""
    subj = subject or _active_subject()

    if isinstance(task_output.pydantic, model_cls):
        return task_output.pydantic, None

    raw_text = (task_output.raw or "").strip()
    if not raw_text:
        return None, f"Task output is empty; expected structured `{model_cls.__name__}`."

    data = _try_parse_json_dict(raw_text)
    if data is not None:
        if "subject" not in data or not data.get("subject"):
            data["subject"] = subj
        try:
            return model_cls.model_validate(data), None
        except Exception as exc:
            return (
                None,
                f"JSON output failed `{model_cls.__name__}` schema validation: {exc}",
            )

    # Fallback Markdown parsers for key models
    if model_cls is QuestionPaperOutput:
        parsed_qp = _parse_qp_from_markdown(raw_text, subj, is_revised=False)
        if parsed_qp is not None:
            return parsed_qp, None  # type: ignore[return-value]
    elif model_cls is RevisedQuestionPaperOutput:
        parsed_rqp = _parse_qp_from_markdown(raw_text, subj, is_revised=True)
        if parsed_rqp is not None:
            return parsed_rqp, None  # type: ignore[return-value]
    elif model_cls is AnswerKeyOutput:
        parsed_ak = _parse_answer_key_from_markdown(raw_text, subj)
        if parsed_ak is not None:
            return parsed_ak, None  # type: ignore[return-value]
    elif model_cls is DiscrepancyReportOutput:
        status = (
            "NEEDS_REVISION"
            if "DISCREPANCY" in raw_text.upper() or "NEEDS_REVISION" in raw_text.upper()
            else "PASS"
        )
        return (
            DiscrepancyReportOutput(
                subject=subj,
                overall_status=status,  # type: ignore[arg-type]
                summary_notes=[raw_text],
            ),
            None,
        )  # type: ignore[return-value]
    elif model_cls is FinalReviewSummaryOutput:
        verdict = (
            "NEEDS_REVISION"
            if "NEEDS_REVISION" in raw_text.upper() or "NEEDS REVISION" in raw_text.upper()
            else "APPROVED"
        )
        return (
            FinalReviewSummaryOutput(
                subject=subj,
                overall_verdict=verdict,  # type: ignore[arg-type]
                consolidated_action_items=[raw_text],
            ),
            None,
        )  # type: ignore[return-value]
    elif model_cls is CodingDiscrepancyReportOutput:
        status = (
            "NEEDS_REVISION"
            if "DISCREPANCY" in raw_text.upper() or "NEEDS_REVISION" in raw_text.upper()
            else "PASS"
        )
        return (
            CodingDiscrepancyReportOutput(
                subject=subj,
                overall_status=status,  # type: ignore[arg-type]
                problem_reviews=[
                    CodingDiscrepancyItem(
                        problem_number=1,
                        status="DISCREPANCY" if status == "NEEDS_REVISION" else "OK",
                        syllabus_alignment="Reviewed",
                        language_compliance="Reviewed",
                        issues=[raw_text] if status == "NEEDS_REVISION" else [],
                    )
                ],
            ),
            None,
        )  # type: ignore[return-value]
    elif model_cls is CodingFinalSummaryOutput:
        verdict = (
            "NEEDS_REVISION"
            if "NEEDS_REVISION" in raw_text.upper() or "NEEDS REVISION" in raw_text.upper()
            else "APPROVED"
        )
        return (
            CodingFinalSummaryOutput(
                subject=subj,
                overall_verdict=verdict,  # type: ignore[arg-type]
                problem_verdicts=[
                    CodingProblemVerdict(
                        problem_number=1,
                        verdict="NEEDS_REVISION" if verdict == "NEEDS_REVISION" else "OK",
                        rationale=raw_text,
                    )
                ],
            ),
            None,
        )  # type: ignore[return-value]
    elif model_cls is DocumentParseReportOutput:
        return (
            DocumentParseReportOutput(
                subject=subj,
                documents=[
                    ParsedDocumentEntry(
                        source_file="ingested_sources",
                        category="week",
                        summary=raw_text,
                    )
                ],
            ),
            None,
        )  # type: ignore[return-value]
    elif model_cls is SyllabusIndexReportOutput:
        return (
            SyllabusIndexReportOutput(
                subject=subj,
                indexing_notes=[raw_text],
            ),
            None,
        )  # type: ignore[return-value]
    elif model_cls is ConversionReportOutput:
        return (
            ConversionReportOutput(
                subject=subj,
                records=[
                    FileConversionRecord(
                        source_path="batch",
                        status="converted",
                        detail=raw_text,
                    )
                ],
            ),
            None,
        )  # type: ignore[return-value]

    return (
        None,
        f"Expected valid JSON matching `{model_cls.__name__}` schema, but output could not be parsed.",
    )


def enrich_ingestion_parse_report(model: DocumentParseReportOutput, subject: str) -> None:
    """Populate `model.documents` from on-disk manifest if empty."""
    try:
        from klyra.utils import fs

        manifest = fs.load_manifest(subject)
        all_files = fs.discover_subject_files(subject)
        if not model.documents and manifest:
            for f in all_files:
                key = str(f.path)
                if key in manifest:
                    chunks_n = int(manifest[key].get("chunks", 0))
                    model.documents.append(
                        ParsedDocumentEntry(
                            source_file=key,
                            category=f.category,
                            week=f.week,
                            summary=f"Ingested ({chunks_n} chunks)",
                        )
                    )
    except Exception:
        pass


def enrich_ingestion_index_report(model: SyllabusIndexReportOutput, subject: str) -> None:
    """Populate `model` chunk counts, weeks covered, missing weeks, and category status notes from manifest."""
    try:
        from klyra.utils import fs

        manifest = fs.load_manifest(subject)
        all_files = fs.discover_subject_files(subject)
        gaps = fs.missing_weeks(subject)
        covered_weeks = [w for w in range(1, fs.NUM_WEEKS + 1) if w not in gaps]

        total_manifest_chunks = sum(
            int(v.get("chunks", 0))
            for v in manifest.values()
            if isinstance(v, dict)
        )
        if model.total_chunks_indexed == 0 and total_manifest_chunks > 0:
            model.total_chunks_indexed = total_manifest_chunks

        if not model.weeks_covered and covered_weeks:
            model.weeks_covered = covered_weeks

        if not model.missing_weeks and gaps:
            model.missing_weeks = gaps

        cat_labels = {
            "week": "weeks",
            "pyq": "pyq",
            "transcript": "transcripts",
            "additional": "additional",
        }
        active_cats: list[str] = list(model.categories_indexed)
        status_notes: list[str] = []
        for cat_key in ("week", "pyq", "transcript", "additional"):
            cat_files = [f for f in all_files if f.category == cat_key]
            ingested = [f for f in cat_files if str(f.path) in manifest]
            chunks_n = sum(
                int(manifest.get(str(f.path), {}).get("chunks", 0))
                for f in ingested
            )
            label = cat_labels[cat_key]
            if ingested and label not in active_cats and cat_key not in active_cats:
                active_cats.append(label)
            if cat_key == "week":
                w_str = ", ".join(str(w) for w in covered_weeks) if covered_weeks else "None"
                status_notes.append(
                    f"**weeks (`week_1..week_12`):** {len(cat_files)} file(s) on disk, "
                    f"{len(ingested)} ingested ({chunks_n} chunks) | Covered weeks: {w_str}"
                )
            else:
                folder_name = "transcripts" if cat_key == "transcript" else cat_key
                status_notes.append(
                    f"**{label} (`docs/{subject}/{folder_name}`):** {len(cat_files)} file(s) on disk, "
                    f"{len(ingested)} ingested ({chunks_n} chunks)"
                )
        model.categories_indexed = active_cats
        cleaned_existing = [
            n
            for n in model.indexing_notes
            if n.strip()
            and not n.strip().startswith("# ")
            and not n.strip().startswith("Missing/empty weeks:")
            and not n.strip().startswith("Folder / Category Status")
            and not n.strip().startswith("- weeks:")
            and not n.strip().startswith("- pyq:")
            and not n.strip().startswith("- transcripts:")
            and not n.strip().startswith("- additional:")
        ]
        for sn in status_notes:
            if sn not in cleaned_existing:
                cleaned_existing.append(sn)
        model.indexing_notes = cleaned_existing
    except Exception:
        pass

