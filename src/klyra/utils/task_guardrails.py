"""CrewAI task guardrails enforcing structured Pydantic schemas, per-subject
formatting rules, and sandboxed code execution verification."""

import re
from pathlib import Path
from typing import Any, Tuple

from crewai.tasks.task_output import TaskOutput
from pydantic import BaseModel

from klyra.crews.base_task import get_active_crew_inputs
from klyra.models.subject_skill import SubjectSkillConfig
from klyra.models.task_outputs import (
    AnswerKeyOutput,
    CodingDiscrepancyReportOutput,
    CodingFinalSummaryOutput,
    CodingProblemSetOutput,
    ConversionReportOutput,
    DiscrepancyReportOutput,
    DocumentParseReportOutput,
    FinalReviewSummaryOutput,
    QuestionPaperOutput,
    RevisedCodingProblemSetOutput,
    RevisedQuestionPaperOutput,
    RevisedTeachingMaterialOutput,
    SyllabusIndexReportOutput,
    TeachingAnswersOutput,
    TeachingMaterialOutput,
    TestCaseValidationOutput,
    ValidatedProblemResult,
)
from klyra.services.skill_service import load_subject_skill
from klyra.tools.code_execution_tool import (
    DockerNotAvailableError,
    TestCaseSpec,
    docker_available,
    run_against_test_cases,
)
from klyra.utils.coding_verifier import (
    _backfill_problem_tests_from_disk,
    _discover_disk_test_cases_for_problem,
    _load_on_disk_problem_specs,
    _read_on_disk_test_cases,
    _sanitize_student_readme,
    _sync_problem_spec_to_disk,
    verify_and_sync_validated_problem,
)
from klyra.utils.format_renderer import (
    render_coding_readme_markdown,
    render_task_output_markdown,
    save_dual_output,
    strip_option_prefix,
    verify_rendered_markdown,
)
from klyra.utils.fs import DOCS_ROOT, generated_dir
from klyra.utils.markdown_parsers import (
    _IMAGE_LINK_RE,
    _JSON_FENCE_RE,
    _parse_answer_key_from_markdown,
    _parse_qp_from_markdown,
    _try_parse_json_dict,
    enrich_ingestion_index_report,
    enrich_ingestion_parse_report,
    extract_structured_model,
)

_MISSING_DIAGRAM_REF_RE = re.compile(
    r"\b(?:shown\s+in\s+the\s+(?:diagram|figure|graph)|(?:diagram|figure|graph)\s+(?:shown\s+)?below|refer\s+to\s+the\s+(?:diagram|figure|graph)\s+below)\b",
    re.IGNORECASE,
)


def _active_subject() -> str:
    return str(get_active_crew_inputs().get("subject") or "general")


def _active_skill(subject: str | None = None) -> SubjectSkillConfig:
    subj = subject or _active_subject()
    return load_subject_skill(subj)


def _check_diagram_links(markdown_text: str, subject: str, root: Path = DOCS_ROOT) -> list[str]:
    """Verify that any relative image paths in `markdown_text` actually exist on disk."""
    from klyra.utils.fs import subject_dir

    errors: list[str] = []
    gen_dir = generated_dir(subject, root=root)
    subj_dir = subject_dir(subject, root=root)
    for match in _IMAGE_LINK_RE.finditer(markdown_text):
        rel_path = match.group(1).strip()
        if rel_path.startswith(("http://", "https://", "data:", "/files/")):
            continue
        candidates = [
            (gen_dir / rel_path).resolve(),
            (gen_dir / "coding" / rel_path).resolve(),
            (subj_dir / rel_path.lstrip("./")).resolve(),
        ]
        if not any(c.exists() for c in candidates):
            errors.append(
                f"Broken or hallucinated diagram image path `{rel_path}` (resolved to `{candidates[0]}`, which does not exist on disk). Call `generate_diagram` or `generate_graph` and use only the exact markdown image link it returns."
            )
    return errors


def _check_missing_question_diagrams(model: QuestionPaperOutput) -> list[str]:
    """Ensure any question whose stem explicitly says 'shown in the diagram/figure/graph below'
    actually embeds a rendered diagram when pdflatex is available."""
    from klyra.tools.latex_diagram_tool import _pdflatex_path

    if not _pdflatex_path():
        return []
    errors: list[str] = []
    for q in model.questions:
        has_img = bool(
            (q.diagram_markdown and _IMAGE_LINK_RE.search(q.diagram_markdown))
            or _IMAGE_LINK_RE.search(q.stem or "")
        )
        if not has_img and _MISSING_DIAGRAM_REF_RE.search(q.stem or ""):
            errors.append(
                f"Question {q.question_number} references a diagram/figure/graph below in its stem, "
                "but `diagram_markdown` has no rendered image link. Call `generate_diagram` or "
                "`generate_graph` with LaTeX/TikZ to render and visually verify the diagram, then "
                "set `diagram_markdown` to the returned `![caption](...)` link."
            )
    return errors


def _finalize_guardrail_output(
    task_output: TaskOutput, model: BaseModel, skill: SubjectSkillConfig
) -> tuple[bool, Any]:
    """Render `model` to subject-specific Markdown, verify it, and return updated `TaskOutput`."""
    rendered_md = render_task_output_markdown(model, skill=skill)
    errors = verify_rendered_markdown(rendered_md, model, skill=skill)
    errors.extend(_check_diagram_links(rendered_md, skill.subject_id))
    if isinstance(model, QuestionPaperOutput):
        errors.extend(_check_missing_question_diagrams(model))
    if errors:
        return False, "Output failed subject format verification:\n- " + "\n- ".join(errors)

    task_output.pydantic = model
    task_output.raw = rendered_md
    return True, task_output


# ============================================================================
# Task Guardrail Callables (1 positional parameter: `task_output: TaskOutput`)
# ============================================================================


def qp_output_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `generate_qp_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(task_output, QuestionPaperOutput, subject=subj)
    if err or model is None:
        return False, err or "Failed to parse QuestionPaperOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def revised_qp_output_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `revise_qp_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, RevisedQuestionPaperOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse RevisedQuestionPaperOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def discrepancy_report_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `review_qp_task` and `review_material_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, DiscrepancyReportOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse DiscrepancyReportOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def answer_key_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `solve_qp_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(task_output, AnswerKeyOutput, subject=subj)
    if err or model is None:
        return False, err or "Failed to parse AnswerKeyOutput."
    if not model.solutions:
        return False, "AnswerKeyOutput must contain worked solutions for every question (found 0)."
    for sol in model.solutions:
        if not sol.correct_answer.strip() or not sol.derivation.strip():
            return (
                False,
                f"Question {sol.question_number} in AnswerKeyOutput is missing `correct_answer` or `derivation`.",
            )
    return _finalize_guardrail_output(task_output, model, skill)


def final_summary_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `final_review_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, FinalReviewSummaryOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse FinalReviewSummaryOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def coding_problems_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `generate_problems_task`."""
    inputs = get_active_crew_inputs()
    subj = _active_subject()
    skill = _active_skill(subj)
    pkg_name = str(inputs.get("name") or "")
    model, err = extract_structured_model(
        task_output, CodingProblemSetOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse CodingProblemSetOutput."
    if not model.problems:
        return False, "CodingProblemSetOutput must contain at least 1 coding problem."

    try:
        expected_nq = int(inputs.get("num_questions") or 0)
    except (TypeError, ValueError):
        expected_nq = 0
    if expected_nq > 1 and len(model.problems) < expected_nq:
        return (
            False,
            f"Requested {expected_nq} coding problems (num_questions={expected_nq}), "
            f"but `CodingProblemSetOutput.problems` only contains {len(model.problems)}. "
            f"You MUST generate all {expected_nq} distinct coding problems.",
        )

    if len({p.problem_number for p in model.problems}) < len(model.problems):
        for idx, p in enumerate(model.problems, start=1):
            p.problem_number = idx

    total_probs = len(model.problems)
    for prob in model.problems:
        if not prob.reference_solution.strip():
            return (
                False,
                f"Problem {prob.problem_number} ('{prob.title}') is missing a runnable `reference_solution`.",
            )
        _backfill_problem_tests_from_disk(prob, subj, pkg_name, total_probs)
        _sync_problem_spec_to_disk(prob, subj, pkg_name, total_probs, skill)
    return _finalize_guardrail_output(task_output, model, skill)


def revised_coding_problems_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `revise_problems_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    pkg_name = str(get_active_crew_inputs().get("name") or "")
    model, err = extract_structured_model(
        task_output, RevisedCodingProblemSetOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse RevisedCodingProblemSetOutput."
    if not model.problems:
        return False, "RevisedCodingProblemSetOutput must contain at least 1 coding problem."

    if len({p.problem_number for p in model.problems}) < len(model.problems):
        for idx, p in enumerate(model.problems, start=1):
            p.problem_number = idx

    if pkg_name:
        disk_specs = _load_on_disk_problem_specs(subj, pkg_name)
        if len(disk_specs) > len(model.problems):
            existing_nums = {p.problem_number for p in model.problems}
            existing_titles = {p.title.strip().lower() for p in model.problems if p.title.strip()}
            for spec in disk_specs:
                if (
                    spec.problem_number not in existing_nums
                    and spec.title.strip().lower() not in existing_titles
                ):
                    model.problems.append(spec)
                    existing_nums.add(spec.problem_number)
            model.problems.sort(key=lambda p: p.problem_number)

    total_probs = len(model.problems)
    for prob in model.problems:
        if not prob.reference_solution.strip():
            return (
                False,
                f"Problem {prob.problem_number} ('{prob.title}') is missing a runnable `reference_solution`.",
            )
        _backfill_problem_tests_from_disk(prob, subj, pkg_name, total_probs)
        _sync_problem_spec_to_disk(prob, subj, pkg_name, total_probs, skill)
    return _finalize_guardrail_output(task_output, model, skill)


def coding_discrepancy_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `review_problems_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, CodingDiscrepancyReportOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse CodingDiscrepancyReportOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def coding_test_cases_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `validate_test_cases_task` that deterministically executes
    every reference solution against every public and private test case."""
    inputs = get_active_crew_inputs()
    subj = str(inputs.get("subject") or "general")
    pkg_name = str(inputs.get("name") or "")
    skill = _active_skill(subj)

    model, err = extract_structured_model(
        task_output, TestCaseValidationOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse TestCaseValidationOutput."
    if not model.validated_problems:
        return (
            False,
            "TestCaseValidationOutput contains 0 `validated_problems`. Every problem must be validated.",
        )

    if len({vp.problem_number for vp in model.validated_problems}) < len(model.validated_problems):
        for idx, vp in enumerate(model.validated_problems, start=1):
            vp.problem_number = idx

    disk_specs = _load_on_disk_problem_specs(subj, pkg_name) if pkg_name else []
    if len(disk_specs) > len(model.validated_problems):
        existing_nums = {vp.problem_number for vp in model.validated_problems}
        existing_titles = {vp.title.strip().lower() for vp in model.validated_problems if vp.title.strip()}
        for spec in disk_specs:
            if (
                spec.problem_number not in existing_nums
                and spec.title.strip().lower() not in existing_titles
            ):
                _backfill_problem_tests_from_disk(spec, subj, pkg_name, len(disk_specs))
                pub_tcs = spec.public_tests or spec.sample_tests
                priv_tcs = spec.private_tests
                if pub_tcs and priv_tcs and spec.reference_solution.strip():
                    model.validated_problems.append(
                        ValidatedProblemResult(
                            problem_number=spec.problem_number,
                            title=spec.title,
                            language=spec.language,
                            reference_solution=spec.reference_solution,
                            readme_markdown=render_coding_readme_markdown(spec, skill),
                            public_tests=pub_tcs,
                            private_tests=priv_tcs,
                        )
                    )
                    existing_nums.add(spec.problem_number)
        model.validated_problems.sort(key=lambda vp: vp.problem_number)

    try:
        expected_nq = int(inputs.get("num_questions") or 0)
    except (TypeError, ValueError):
        expected_nq = 0
    expected_total = max(len(model.validated_problems), len(disk_specs), expected_nq)
    if expected_total > 1 and len(model.validated_problems) < expected_total:
        return (
            False,
            f"Expected {expected_total} validated coding problems, but `TestCaseValidationOutput.validated_problems` "
            f"only contains {len(model.validated_problems)}. You MUST validate and return all {expected_total} problems.",
        )

    total_probs = max(len(model.validated_problems), expected_total)
    for vp in model.validated_problems:
        ok, detail = verify_and_sync_validated_problem(
            vp=vp,
            subject=subj,
            package_name=pkg_name,
            total_problems=total_probs,
            skill=skill,
            sync_to_disk=bool(pkg_name),
        )
        if not ok:
            model.all_passed = False
            return False, detail

    model.all_passed = True
    return _finalize_guardrail_output(task_output, model, skill)


def coding_final_summary_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `final_coding_review_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, CodingFinalSummaryOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse CodingFinalSummaryOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def teaching_material_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `generate_material_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, TeachingMaterialOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse TeachingMaterialOutput."
    if not model.concept_sections:
        return False, "TeachingMaterialOutput must contain at least 1 `concept_sections` entry."
    return _finalize_guardrail_output(task_output, model, skill)


def revised_teaching_material_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `revise_material_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, RevisedTeachingMaterialOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse RevisedTeachingMaterialOutput."
    if not model.concept_sections:
        return False, "RevisedTeachingMaterialOutput must contain at least 1 `concept_sections` entry."
    return _finalize_guardrail_output(task_output, model, skill)


def teaching_answers_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `solve_examples_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, TeachingAnswersOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse TeachingAnswersOutput."
    return _finalize_guardrail_output(task_output, model, skill)


def ingestion_parse_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `parse_documents_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, DocumentParseReportOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse DocumentParseReportOutput."
    enrich_ingestion_parse_report(model, subj)
    return _finalize_guardrail_output(task_output, model, skill)


def ingestion_index_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `index_syllabus_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, SyllabusIndexReportOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse SyllabusIndexReportOutput."
    enrich_ingestion_index_report(model, subj)
    return _finalize_guardrail_output(task_output, model, skill)


def conversion_report_guardrail(task_output: TaskOutput) -> tuple[bool, Any]:
    """Guardrail for `convert_files_task`."""
    subj = _active_subject()
    skill = _active_skill(subj)
    model, err = extract_structured_model(
        task_output, ConversionReportOutput, subject=subj
    )
    if err or model is None:
        return False, err or "Failed to parse ConversionReportOutput."
    return _finalize_guardrail_output(task_output, model, skill)
