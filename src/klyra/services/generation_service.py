"""Generation phase: create + review + solve a question paper."""
from __future__ import annotations

from typing import Any


class GenerationInputError(ValueError):
    """Raised when the request doesn't have enough info to build a QP."""


def verify_on_disk_qp(subject: str, qp_name: str) -> tuple[bool, str]:
    """Inspect structured JSON sidecars and rendered Markdown for a generated QP.

    Checks:
    1. `<slug>-final-summary.json` (`FinalReviewSummaryOutput`) for `NEEDS_REVISION` verdicts.
    2. `<slug>-discrepancy-report.json` (`DiscrepancyReportOutput`) for `DISCREPANCY` findings.
    3. `<slug>-answer-key.json` (`AnswerKeyOutput`) for non-empty `ambiguity_flag`s.
    4. `<slug>-qp.md` against subject format rules via `verify_rendered_markdown`.

    Returns `(is_ready, diagnostic_report)`.
    """
    from klyra.models.task_outputs import (
        AnswerKeyOutput,
        DiscrepancyReportOutput,
        FinalReviewSummaryOutput,
        QuestionPaperOutput,
    )
    from klyra.services.skill_service import load_subject_skill
    from klyra.utils import fs
    from klyra.utils.format_renderer import verify_rendered_markdown

    slug = fs.slugify(qp_name)
    gen_dir = fs.generated_dir(subject)
    issues: list[str] = []

    def _pick(rev_name: str, orig_name: str):
        rev_path = gen_dir / rev_name
        return rev_path if rev_path.exists() else (gen_dir / orig_name)

    # 1. Check FinalReviewSummaryOutput (prefer revised if present)
    final_json = _pick(f"{slug}-final-summary-revised.json", f"{slug}-final-summary.json")
    revised_approved = False
    if final_json.exists():
        try:
            summary = FinalReviewSummaryOutput.model_validate_json(
                final_json.read_text(encoding="utf-8")
            )
            if summary.overall_verdict == "NEEDS_REVISION":
                for v in summary.verdicts:
                    if v.verdict == "NEEDS_REVISION":
                        issues.append(
                            f"Q{v.question_number} flagged NEEDS_REVISION: {v.rationale}"
                            + (f" (Fix: {v.required_change})" if v.required_change else "")
                        )
                for item in summary.consolidated_action_items:
                    if item and item not in issues:
                        issues.append(f"Action item: {item}")
                if not issues:
                    issues.append("Final review summary marked overall_verdict as NEEDS_REVISION.")
            elif final_json.name.endswith("-revised.json") and summary.overall_verdict == "APPROVED":
                revised_approved = True
        except Exception as exc:
            issues.append(f"Invalid final-summary JSON ({final_json.name}): {exc}")

    # 2. Check DiscrepancyReportOutput
    disc_json = _pick(f"{slug}-discrepancy-report-revised.json", f"{slug}-discrepancy-report.json")
    if disc_json.exists() and not revised_approved:
        try:
            disc = DiscrepancyReportOutput.model_validate_json(
                disc_json.read_text(encoding="utf-8")
            )
            if not disc.mix_compliance.is_compliant:
                issues.append(
                    f"Question type mix mismatch: requested={disc.mix_compliance.requested_mix}, "
                    f"actual={disc.mix_compliance.actual_mix}"
                )
            for qr in disc.question_reviews:
                if qr.status == "DISCREPANCY":
                    detail = "; ".join(qr.issues) if qr.issues else "Discrepancy flagged"
                    fix = f" (Suggested fix: {qr.suggested_fix})" if qr.suggested_fix else ""
                    issues.append(f"Q{qr.question_number} discrepancy: {detail}{fix}")
        except Exception as exc:
            issues.append(f"Invalid discrepancy-report JSON ({disc_json.name}): {exc}")

    # 3. Check AnswerKeyOutput for unsolvable/ambiguous questions
    ak_json = _pick(f"{slug}-answer-key-revised.json", f"{slug}-answer-key.json")
    if ak_json.exists() and not revised_approved:
        try:
            ak = AnswerKeyOutput.model_validate_json(
                ak_json.read_text(encoding="utf-8")
            )
            for sol in ak.solutions:
                flag = (sol.ambiguity_flag or "").strip()
                if flag and flag.lower() not in {"none", "n/a", "no", "null", "ok"}:
                    issues.append(f"Q{sol.question_number} solver ambiguity flag: {flag}")
        except Exception as exc:
            issues.append(f"Invalid answer-key JSON ({ak_json.name}): {exc}")

    # 4. Check rendered QP markdown format compliance
    qp_md = _pick(f"{slug}-qp-revised.md", f"{slug}-qp.md")
    qp_json = _pick(f"{slug}-qp-revised.json", f"{slug}-qp.json")
    if qp_md.exists() and qp_json.exists():
        try:
            qp_model = QuestionPaperOutput.model_validate_json(
                qp_json.read_text(encoding="utf-8")
            )
            skill = load_subject_skill(subject)
            fmt_errors = verify_rendered_markdown(
                qp_md.read_text(encoding="utf-8"), qp_model, skill
            )
            issues.extend(f"QP format violation: {err}" for err in fmt_errors)
        except Exception as exc:
            issues.append(f"Invalid qp JSON ({qp_json.name}): {exc}")

    if issues:
        return False, "\n".join(f"- {i}" for i in issues)
    return True, f"Question paper '{slug}' passed all review, solver, and format checks."


def run_generation(
    subject: str,
    qp_name: str,
    scope: str,
    difficulty: str,
    source: str | None = None,
    pyq_similar: int = 0,
    concepts: str | None = None,
    type_counts: str | None = None,
    types: str | None = None,
    num_questions: int | None = None,
    notes: str | None = None,
    max_rewrite_attempts: int = 1,
) -> Any:
    from klyra.crews.generation_crew.generation_crew import GenerationCrew
    from klyra.services.revision_service import run_revision
    from klyra.services.skill_service import build_skill_prompt_inputs, load_subject_skill
    from klyra.utils import fs

    fs.check_subject_exists(subject)
    from klyra.utils.parsing import parse_type_counts, parse_week_scope

    skill = load_subject_skill(subject)
    if type_counts and type_counts.strip().lower() == "default":
        counts = parse_type_counts(skill.default_type_counts)
        question_types = ", ".join(f"{qtype} x{count}" for qtype, count in counts.items())
        resolved_num_questions = sum(counts.values())
    elif type_counts:
        counts = parse_type_counts(type_counts)
        question_types = ", ".join(f"{qtype} x{count}" for qtype, count in counts.items())
        resolved_num_questions = sum(counts.values())
    elif types and num_questions:
        question_types = types
        resolved_num_questions = num_questions
    else:
        raise GenerationInputError(
            "generate needs either type_counts (e.g. 'MCQ:5,MSQ:3,NAT:2') or both types and num_questions."
        )

    if not fs.subject_dir(subject).exists():
        raise GenerationInputError(f"Subject '{subject}' does not exist in docs/. Please run ingest first.")

    slug = fs.slugify(qp_name)
    fs.generated_dir(subject).mkdir(parents=True, exist_ok=True)
    inputs = {
        "subject": subject,
        "qp_name": slug,
        "syllabus_scope": parse_week_scope(scope),
        "source_filter": source or "any",
        "difficulty": difficulty,
        "pyq_similar_count": pyq_similar,
        "concepts": concepts or "any",
        "question_types": question_types,
        "num_questions": resolved_num_questions,
        "notes": notes or "(none)",
        **build_skill_prompt_inputs(subject),
    }
    with fs.crewai_storage_context(f".db/{subject}"):
        result = GenerationCrew().crew().kickoff(inputs=inputs)

    # Validation: Ensure the generation didn't fail due to empty context
    qp_file = fs.generated_dir(subject) / f"{slug}-qp.md"
    if qp_file.exists() and len(qp_file.read_text().strip()) < 150:
        raise GenerationInputError(
            f"Generation failed: The generated file is suspiciously short. "
            f"This usually means the vector store returned no context for the requested scope ({scope}). "
            f"Please ensure you have ingested content for this scope before generating."
        )

    # Post-generation self-healing loop: if structured review/solver sidecars
    # flagged discrepancies or ambiguous questions, auto-trigger RevisionCrew.
    if max_rewrite_attempts > 0:
        ok, diag = verify_on_disk_qp(subject, qp_name)
        attempt = 0
        while not ok and attempt < max_rewrite_attempts:
            attempt += 1
            rewrite_notes = (
                f"AUTOMATED REVIEW / SOLVER VERIFICATION FLAGGED ISSUES (Attempt {attempt}/{max_rewrite_attempts}).\n"
                f"Fix every flagged question below while preserving all compliant questions and the subject's format:\n{diag}"
            )
            result = run_revision(subject=subject, qp_name=qp_name, notes=rewrite_notes)
            ok, diag = verify_on_disk_qp(subject, qp_name)

    return result
