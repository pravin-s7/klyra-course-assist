"""CrewAI task guardrails enforcing structured Pydantic schemas, per-subject
formatting rules, and sandboxed code execution verification."""

import inspect
import json
import re
from pathlib import Path
from typing import Any, Tuple, TypeVar

from crewai.tasks.task_output import TaskOutput
from pydantic import BaseModel

from klyra.crews.base_task import get_active_crew_inputs
from klyra.models.subject_skill import SubjectSkillConfig
from klyra.models.task_outputs import (
    AnswerKeyOutput,
    CodingDiscrepancyItem,
    CodingDiscrepancyReportOutput,
    CodingFinalSummaryOutput,
    CodingProblemSetOutput,
    CodingProblemSpec,
    CodingProblemVerdict,
    CodingTestCase,
    ConceptSection,
    ConversionReportOutput,
    DiscrepancyReportOutput,
    DocumentParseReportOutput,
    FileConversionRecord,
    FinalReviewSummaryOutput,
    MixComplianceSummary,
    ParsedDocumentEntry,
    PracticeQuestion,
    PracticeQuestionAnswer,
    QuestionDiscrepancy,
    QuestionItem,
    QuestionPaperOutput,
    QuestionVerdict,
    RevisedCodingProblemSetOutput,
    RevisedQuestionPaperOutput,
    RevisedTeachingMaterialOutput,
    SyllabusIndexReportOutput,
    TeachingAnswersOutput,
    TeachingMaterialOutput,
    TestCaseValidationOutput,
    ValidatedProblemResult,
    WorkedExampleDerivation,
    WorkedSolution,
)
from klyra.services.skill_service import load_subject_skill
from klyra.tools.code_execution_tool import (
    DockerNotAvailableError,
    TestCaseSpec,
    docker_available,
    run_against_test_cases,
)
from klyra.tools.subject_fs_tool import TestCaseFiles, WriteCodingPackageTool
from klyra.utils.format_renderer import (
    render_coding_readme_markdown,
    render_task_output_markdown,
    strip_option_prefix,
    verify_rendered_markdown,
)
from klyra.utils.fs import DOCS_ROOT, generated_dir

T = TypeVar("T", bound=BaseModel)

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_IMAGE_LINK_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")


def _active_subject() -> str:
    return str(get_active_crew_inputs().get("subject") or "general")


def _active_skill(subject: str | None = None) -> SubjectSkillConfig:
    subj = subject or _active_subject()
    return load_subject_skill(subj)


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


# ============================================================================
# Markdown-to-Pydantic Fallback Parsers (when LLM returns Markdown instead of JSON)
# ============================================================================

_Q_HEADER_RE = re.compile(
    r"^#{2,4}\s*Question\s+(\d+)(?:\s*\(([^)]+)\))?(?:\s*\[(MCQ|MSQ|NAT|SA)\])?(?:\s*\[Marks:\s*(\d+)\])?.*$",
    re.IGNORECASE | re.MULTILINE,
)
_OPT_LINE_RE = re.compile(
    r"^\s*(?:\(([a-zA-Z0-9]+)\)|([a-zA-Z0-9]+)[.)])\s+(.+?)\s*$"
)


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


_MISSING_DIAGRAM_REF_RE = re.compile(
    r"\b(?:shown\s+in\s+the\s+(?:diagram|figure|graph)|(?:diagram|figure|graph)\s+(?:shown\s+)?below|refer\s+to\s+the\s+(?:diagram|figure|graph)\s+below)\b",
    re.IGNORECASE,
)


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


def _read_on_disk_test_cases(base_dir: Path, visibility: str) -> list[CodingTestCase]:
    """Read `test_case_*/input.txt` and `output.txt` from `base_dir / visibility`."""
    vis_dir = base_dir / visibility
    if not vis_dir.is_dir():
        return []

    def _sort_key(p: Path) -> tuple[int, str]:
        suffix = p.name.rsplit("_", 1)[-1]
        return (int(suffix) if suffix.isdigit() else 999999, p.name)

    cases: list[CodingTestCase] = []
    for idx, tc_dir in enumerate(sorted(vis_dir.glob("test_case_*"), key=_sort_key), start=1):
        if not tc_dir.is_dir():
            continue
        in_file = tc_dir / "input.txt"
        out_file = tc_dir / "output.txt"
        if in_file.exists() and out_file.exists():
            cases.append(
                CodingTestCase(
                    index=idx,
                    visibility="private" if visibility == "private" else "public",
                    stdin_input=in_file.read_text(encoding="utf-8"),
                    expected_stdout=out_file.read_text(encoding="utf-8"),
                )
            )
    return cases


def _discover_disk_test_cases_for_problem(
    subject: str, pkg_name: str, problem_number: int, total_problems: int
) -> tuple[list[CodingTestCase], list[CodingTestCase]]:
    """Locate on-disk public and private test cases for `problem_number`."""
    if not pkg_name:
        return [], []
    from klyra.utils import fs

    pkg_root = fs.coding_package_dir(subject, pkg_name)
    if not pkg_root.exists():
        return [], []

    candidates: list[Path] = []
    sub_dir = pkg_root / f"problem-{problem_number}"
    if total_problems > 1:
        if sub_dir.is_dir():
            candidates.append(sub_dir)
        if problem_number == 1:
            candidates.append(pkg_root)
    else:
        candidates.append(pkg_root)
        if sub_dir.is_dir():
            candidates.append(sub_dir)

    best_pub: list[CodingTestCase] = []
    best_priv: list[CodingTestCase] = []
    for cand in candidates:
        pub = _read_on_disk_test_cases(cand, "public")
        priv = _read_on_disk_test_cases(cand, "private")
        if pub and (
            not best_pub
            or sum(len(x.stdin_input) for x in pub) > sum(len(x.stdin_input) for x in best_pub)
        ):
            best_pub = pub
        if priv and (
            not best_priv
            or sum(len(x.stdin_input) for x in priv) > sum(len(x.stdin_input) for x in best_priv)
        ):
            best_priv = priv
    return best_pub, best_priv


def _backfill_problem_tests_from_disk(
    prob: CodingProblemSpec, subject: str, pkg_name: str, total_problems: int
) -> None:
    """Ensure `prob.sample_tests` has no private tests and backfill `prob.public_tests` /
    `prob.private_tests` from on-disk `write_coding_package` files when available."""
    if prob.sample_tests:
        leaked_priv = [tc for tc in prob.sample_tests if tc.visibility == "private"]
        if leaked_priv:
            prob.sample_tests = [
                tc for tc in prob.sample_tests if tc.visibility != "private"
            ]
            if not prob.private_tests:
                prob.private_tests = leaked_priv

    disk_pub, disk_priv = _discover_disk_test_cases_for_problem(
        subject, pkg_name, prob.problem_number, total_problems
    )
    if disk_pub:
        if not prob.public_tests or (
            sum(len(x.stdin_input) for x in disk_pub)
            > sum(len(x.stdin_input) for x in prob.public_tests)
        ):
            prob.public_tests = disk_pub
    elif not prob.public_tests and prob.sample_tests:
        prob.public_tests = [
            CodingTestCase(
                index=idx,
                visibility="public",
                stdin_input=tc.stdin_input,
                expected_stdout=tc.expected_stdout,
                explanation=tc.explanation,
            )
            for idx, tc in enumerate(prob.sample_tests, start=1)
        ]

    if disk_priv:
        if not prob.private_tests or (
            sum(len(x.stdin_input) for x in disk_priv)
            > sum(len(x.stdin_input) for x in prob.private_tests)
        ):
            prob.private_tests = disk_priv


def _sanitize_student_readme(readme_text: str) -> str:
    """Remove any accidentally included Reference Solution or Private Test Cases sections from README.md."""
    cleaned = re.split(
        r"\n##+\s+(?:Reference\s+Solution|Full\s+Draft\s+Test\s+Suite|Private\s+Test\s+Cases)\b",
        readme_text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    cleaned = re.split(
        r"\n###+\s+Private\s+Test\b",
        cleaned,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    return cleaned.strip() + "\n"


def _load_on_disk_problem_specs(subject: str, pkg_name: str) -> list[CodingProblemSpec]:
    """Load `CodingProblemSpec` entries from `{slug}-problems-revised.json` or `{slug}-problems.json` if present."""
    if not pkg_name:
        return []
    from klyra.utils import fs

    slug = fs.slugify(pkg_name)
    coding_dir = generated_dir(subject) / "coding"
    for candidate_name in (f"{slug}-problems-revised.json", f"{slug}-problems.json"):
        candidate = coding_dir / candidate_name
        if candidate.exists():
            try:
                parsed = CodingProblemSetOutput.model_validate_json(
                    candidate.read_text(encoding="utf-8")
                )
                if parsed.problems:
                    return list(parsed.problems)
            except Exception:
                pass
    return []


def _sync_problem_spec_to_disk(
    prob: CodingProblemSpec,
    subject: str,
    package_name: str,
    total_problems: int,
    skill: SubjectSkillConfig | None = None,
) -> None:
    """Deterministically materialize `prob` into `docs/<subject>/generated/coding/<package_name>/`
    (using `problem-<N>` subdirectory when `total_problems > 1`)."""
    if not package_name or not prob.reference_solution.strip():
        return
    pub_tests = prob.public_tests or prob.sample_tests
    priv_tests = prob.private_tests
    if not pub_tests:
        return

    active_lang = str(get_active_crew_inputs().get("language") or "").strip().lower()
    prob_lang = (prob.language or "").strip().lower()
    if active_lang and (not prob_lang or (prob_lang == "python" and active_lang != "python")):
        lang = active_lang
    else:
        lang = prob_lang or active_lang or "python"
    if lang == "python" and re.search(r"\b(?:psycopg2|pg8000)\b", prob.reference_solution):
        lang = "python-db"
    prob.language = lang

    readme_text = _sanitize_student_readme(render_coding_readme_markdown(prob, skill))
    subdir = f"problem-{prob.problem_number}" if total_problems > 1 else None
    pub_files = [
        TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
        for tc in pub_tests
    ]
    priv_files = [
        TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
        for tc in priv_tests
    ]
    writer = WriteCodingPackageTool()
    writer._run(
        subject=subject,
        name=package_name,
        language=lang,
        readme=readme_text,
        public_tests=pub_files,
        private_tests=priv_files,
        problem_subdir=subdir,
        reference_solution=prob.reference_solution,
    )


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


def verify_and_sync_validated_problem(
    vp: ValidatedProblemResult,
    subject: str,
    package_name: str,
    total_problems: int,
    skill: SubjectSkillConfig | None = None,
    sync_to_disk: bool = True,
) -> tuple[bool, str]:
    """Execute `vp.reference_solution` against every public & private test case in the
    sandbox (when available) and sync the verified coding package to disk.

    Returns `(True, summary_str)` when all tests pass, or `(False, detailed_error_report)`
    when any test case fails compilation, crashes, times out, or produces wrong stdout.
    """
    if not vp.reference_solution.strip():
        return (
            False,
            f"Problem {vp.problem_number} ('{vp.title}') has an empty `reference_solution`.",
        )
    if not vp.public_tests:
        return (
            False,
            f"Problem {vp.problem_number} ('{vp.title}') must include at least 1 public test case in `public_tests`.",
        )
    if not vp.private_tests:
        return (
            False,
            f"Problem {vp.problem_number} ('{vp.title}') must include at least 1 private test case in `private_tests`.",
        )

    active_lang = str(get_active_crew_inputs().get("language") or "").strip().lower()
    vp_lang = (vp.language or "").strip().lower()
    if active_lang and (not vp_lang or (vp_lang == "python" and active_lang != "python")):
        lang = active_lang
    else:
        lang = vp_lang or active_lang or "python"
    if lang == "python" and re.search(r"\b(?:psycopg2|pg8000)\b", vp.reference_solution):
        lang = "python-db"
    vp.language = lang

    # If the validator stripped out SQL seed setup (`CREATE TABLE` / `INSERT INTO` / `\copy`)
    # that the creator/reviser wrote to disk, recover the full SQL input from disk before running.
    if lang in ("python-db", "postgresql", "sql") and package_name:
        disk_pub, disk_priv = _discover_disk_test_cases_for_problem(
            subject, package_name, vp.problem_number, total_problems
        )
        sql_kw_re = re.compile(
            r"\b(?:CREATE\s+TABLE|INSERT\s+INTO|DROP\s+TABLE)\b|\\copy\b",
            re.IGNORECASE,
        )
        for idx_p, tc in enumerate(vp.public_tests):
            if (
                not sql_kw_re.search(tc.stdin_input)
                and idx_p < len(disk_pub)
                and sql_kw_re.search(disk_pub[idx_p].stdin_input)
            ):
                tc.stdin_input = disk_pub[idx_p].stdin_input
        for idx_p, tc in enumerate(vp.private_tests):
            if (
                not sql_kw_re.search(tc.stdin_input)
                and idx_p < len(disk_priv)
                and sql_kw_re.search(disk_priv[idx_p].stdin_input)
            ):
                tc.stdin_input = disk_priv[idx_p].stdin_input

    sandbox_ok = docker_available()
    vp.sandbox_available = sandbox_ok

    if sandbox_ok:
        all_specs: list[dict[str, str]] = []
        test_labels: list[tuple[str, int, CodingTestCase]] = []
        for idx, tc in enumerate(vp.public_tests, start=1):
            all_specs.append(
                {"input": tc.stdin_input, "expected_output": tc.expected_stdout}
            )
            test_labels.append(("public", idx, tc))
        for idx, tc in enumerate(vp.private_tests, start=1):
            all_specs.append(
                {"input": tc.stdin_input, "expected_output": tc.expected_stdout}
            )
            test_labels.append(("private", idx, tc))

        run_kwargs: dict[str, Any] = {
            "language": lang,
            "source_code": vp.reference_solution,
            "test_cases": all_specs,
        }
        try:
            if "batch" in inspect.signature(run_against_test_cases).parameters:
                run_kwargs["batch"] = True
        except (ValueError, TypeError):
            pass

        try:
            exec_result = run_against_test_cases(**run_kwargs)
        except DockerNotAvailableError as exc:
            exec_result = None
            sandbox_ok = False
            vp.sandbox_available = False
            vp.execution_verified = False
            vp.execution_summary = (
                f"UNVERIFIED (Docker sandbox unavailable for `{lang}`: {exc}); "
                f"{len(vp.public_tests)} public and {len(vp.private_tests)} private test cases recorded."
            )

        if exec_result is not None:
            if not exec_result.compiled or exec_result.compile_error:
                return (
                    False,
                    f"Problem {vp.problem_number} ('{vp.title}') FAILED compilation in `{lang}` sandbox:\n"
                    f"```text\n{exec_result.compile_error}\n```\n"
                    "Rewrite the `reference_solution` so it compiles cleanly without errors, re-run `execute_code_in_sandbox`, and return the updated result.",
                )

            total_cases = len(all_specs)
            failed_details: list[str] = []
            for (visibility, idx, tc), res in zip(test_labels, exec_result.results):
                if not res.passed:
                    reason = (
                        "TIMED OUT"
                        if res.timed_out
                        else f"exit_code={res.exit_code}"
                        if res.exit_code != 0
                        else "STDOUT MISMATCH"
                    )
                    failed_details.append(
                        f"  - [{visibility.upper()} test #{idx}] {reason}:\n"
                        f"    stdin_input:     {tc.stdin_input!r}\n"
                        f"    expected_stdout: {res.expected!r}\n"
                        f"    actual_stdout:   {res.stdout!r}\n"
                        f"    stderr:          {res.stderr!r}"
                    )

            if failed_details:
                return (
                    False,
                    f"Problem {vp.problem_number} ('{vp.title}') failed {len(failed_details)}/{total_cases} test cases in `{lang}` sandbox:\n"
                    + "\n".join(failed_details)
                    + "\n\nACTION REQUIRED: Inspect whether the `reference_solution` has a bug or a test case's `expected_stdout` was miscalculated. "
                    "Rewrite the broken `reference_solution` or test case, verify 100% pass rate with `execute_code_in_sandbox`, update the package with `write_coding_package`, and return the corrected output.",
                )

            vp.execution_verified = True
            vp.execution_summary = (
                f"Verified {exec_result.passed_count}/{total_cases} test cases passed in `{lang}` sandbox "
                f"({len(vp.public_tests)} public, {len(vp.private_tests)} private)."
            )
    else:
        vp.execution_verified = False
        vp.execution_summary = (
            f"UNVERIFIED (Docker sandbox unavailable for `{lang}`); "
            f"{len(vp.public_tests)} public and {len(vp.private_tests)} private test cases recorded."
        )

    if sync_to_disk and package_name:
        readme_text = vp.readme_markdown.strip()
        if not readme_text:
            from klyra.utils import fs

            pkg_root = fs.coding_package_dir(subject, package_name)
            sub_readme = pkg_root / f"problem-{vp.problem_number}" / "README.md"
            root_readme = pkg_root / "README.md"
            if total_problems > 1 and sub_readme.exists():
                readme_text = sub_readme.read_text(encoding="utf-8").strip()
            elif total_problems == 1 and root_readme.exists():
                readme_text = root_readme.read_text(encoding="utf-8").strip()
            elif sub_readme.exists():
                readme_text = sub_readme.read_text(encoding="utf-8").strip()

        if not readme_text:
            matched_spec: CodingProblemSpec | None = None
            for spec_cand in _load_on_disk_problem_specs(subject, package_name):
                if spec_cand.problem_number == vp.problem_number or (
                    vp.title and spec_cand.title.strip().lower() == vp.title.strip().lower()
                ):
                    matched_spec = spec_cand
                    break
            if matched_spec is not None:
                readme_text = render_coding_readme_markdown(matched_spec, skill)
            else:
                spec = CodingProblemSpec(
                    problem_number=vp.problem_number,
                    title=vp.title or f"Problem {vp.problem_number}",
                    language=lang,
                    problem_statement=f"Solve {vp.title or f'Problem {vp.problem_number}'}.",
                    input_format="See sample test cases.",
                    output_format="See sample test cases.",
                    sample_tests=vp.public_tests[:2],
                    reference_solution=vp.reference_solution,
                )
                readme_text = render_coding_readme_markdown(spec, skill)
        readme_text = _sanitize_student_readme(readme_text)
        vp.readme_markdown = readme_text

        subdir = f"problem-{vp.problem_number}" if total_problems > 1 else None
        pub_files = [
            TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
            for tc in vp.public_tests
        ]
        priv_files = [
            TestCaseFiles(input=tc.stdin_input, output=tc.expected_stdout)
            for tc in vp.private_tests
        ]
        writer = WriteCodingPackageTool()
        msg = writer._run(
            subject=subject,
            name=package_name,
            language=lang,
            readme=readme_text,
            public_tests=pub_files,
            private_tests=priv_files,
            problem_subdir=subdir,
            reference_solution=vp.reference_solution,
        )
        vp.package_files_written = [msg]

    return True, vp.execution_summary


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
    try:
        from klyra.utils import fs

        manifest = fs.load_manifest(subj)
        all_files = fs.discover_subject_files(subj)
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
    try:
        from klyra.utils import fs

        manifest = fs.load_manifest(subj)
        all_files = fs.discover_subject_files(subj)
        gaps = fs.missing_weeks(subj)
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
                    f"**{label} (`docs/{subj}/{folder_name}`):** {len(cat_files)} file(s) on disk, "
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
