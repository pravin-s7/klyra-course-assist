"""Per-subject Markdown renderer and format verifier for structured task outputs."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from klyra.models.subject_skill import SubjectOutputFormats, SubjectSkillConfig
from klyra.models.task_outputs import (
    AnswerKeyOutput,
    CodingDiscrepancyReportOutput,
    CodingFinalSummaryOutput,
    CodingProblemSetOutput,
    CodingProblemSpec,
    CodingTestCase,
    ConversionReportOutput,
    DiscrepancyReportOutput,
    DocumentParseReportOutput,
    FinalReviewSummaryOutput,
    QuestionItem,
    QuestionPaperOutput,
    RevisedCodingProblemSetOutput,
    RevisedQuestionPaperOutput,
    RevisedTeachingMaterialOutput,
    SyllabusIndexReportOutput,
    TeachingAnswersOutput,
    TeachingMaterialOutput,
    TestCaseValidationOutput,
)
from klyra.services.skill_service import load_subject_skill
from klyra.utils.fs import DOCS_ROOT

_LEADING_OPT_RE = re.compile(
    r"^\s*(?:\([a-zA-Z0-9]+\)|[a-zA-Z0-9]+[.)])\s*"
)
_LEAKED_ANSWER_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,4}\s*(?:Correct\s+)?Answer\b|\*\*(?:Correct\s+)?Answer:?\*\*|\*\*Solution:?\*\*|Correct\s+Option\s*:)",
    re.IGNORECASE,
)


def format_option_label(index: int, style: str = "(a)") -> str:
    """Format 0-based option index according to subject `option_label_style`."""
    letter_lower = chr(ord("a") + (index % 26))
    letter_upper = chr(ord("A") + (index % 26))
    if style == "A.":
        return f"{letter_upper}."
    if style == "1.":
        return f"{index + 1}."
    return f"({letter_lower})"


def strip_option_prefix(option_text: str) -> str:
    """Remove any existing `(a)`, `A.`, `1.` prefix so we can apply the subject's label style."""
    return _LEADING_OPT_RE.sub("", option_text.strip()).strip()


def _resolve_skill(
    model: BaseModel, skill: SubjectSkillConfig | None, root: Path = DOCS_ROOT
) -> SubjectSkillConfig:
    if skill is not None:
        return skill
    subj = getattr(model, "subject", "") or "general"
    try:
        return load_subject_skill(subj, root=root)
    except Exception:
        from klyra.services.skill_service import default_subject_skill

        return default_subject_skill("general")


def _render_single_question(
    q: QuestionItem, fmt: SubjectOutputFormats
) -> str:
    q_code = q.question_code.strip() or (
        f"W{q.week:02d}-Q{q.question_number:02d}"
        if q.week
        else f"Q{q.question_number:02d}"
    )
    marks_val = q.marks or fmt.default_marks_by_type.get(q.question_type, 1)
    marks_badge = (
        f" [Marks: {marks_val}]" if fmt.include_marks_per_question else ""
    )
    header = (
        f"### Question {q.question_number} ({q_code}) [{q.question_type}]{marks_badge}"
    )

    lines: list[str] = [header, ""]
    if q.comprehension_stem and q.comprehension_stem.strip():
        lines.append(q.comprehension_stem.strip())
        lines.append("")
    if q.diagram_markdown and q.diagram_markdown.strip():
        lines.append(q.diagram_markdown.strip())
        lines.append("")

    lines.append(q.stem.strip())

    if q.question_type in {"MCQ", "MSQ"} and q.options:
        lines.append("")
        for idx, opt in enumerate(q.options):
            label = format_option_label(idx, fmt.option_label_style)
            cleaned = strip_option_prefix(opt)
            lines.append(f"{label} {cleaned}")

    return "\n".join(lines).strip()


def render_qp_markdown(
    model: QuestionPaperOutput, skill: SubjectSkillConfig
) -> str:
    """Render `QuestionPaperOutput` or `RevisedQuestionPaperOutput` per subject `skill.md`."""
    fmt = skill.output_formats
    total_marks = model.total_marks or sum(
        (q.marks or fmt.default_marks_by_type.get(q.question_type, 1))
        for q in model.questions
    )

    lines: list[str] = [
        f"# {model.title or f'{skill.display_name} — Question Paper'}",
        "",
        f"**Subject:** {skill.display_name} (`{skill.subject_id}`) | "
        f"**Scope:** {model.scope_label or 'Course-wide'} | "
        f"**Difficulty:** {model.difficulty} | "
        f"**Total Marks:** {total_marks}",
        "",
    ]

    if isinstance(model, RevisedQuestionPaperOutput) and model.changes_applied:
        lines.append("## Revision Log")
        for change in model.changes_applied:
            lines.append(f"- {change}")
        lines.append("")

    if model.instructions:
        lines.append("## General Instructions")
        for inst in model.instructions:
            lines.append(f"- {inst}")
        lines.append("")

    lines.append("---")
    lines.append("")

    separator = "\n\n---\n\n" if fmt.page_break_between_questions else "\n\n"

    if fmt.group_by_section:
        section_order = ["MCQ", "MSQ", "NAT", "SA"]
        section_titles = {
            "MCQ": "Multiple Choice Questions (MCQ)",
            "MSQ": "Multiple Select Questions (MSQ)",
            "NAT": "Numerical Answer Type (NAT)",
            "SA": "Short Answer / Analytical Questions (SA)",
        }
        sec_idx = 0
        for qtype in section_order:
            group = [q for q in model.questions if q.question_type == qtype]
            if not group:
                continue
            sec_letter = chr(ord("A") + sec_idx)
            sec_idx += 1
            lines.append(f"## Section {sec_letter} — {section_titles.get(qtype, qtype)}")
            lines.append("")
            rendered_qs = [_render_single_question(q, fmt) for q in group]
            lines.append(separator.join(rendered_qs))
            lines.append("")
    else:
        rendered_qs = [_render_single_question(q, fmt) for q in model.questions]
        lines.append(separator.join(rendered_qs))
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_discrepancy_markdown(
    model: DiscrepancyReportOutput, skill: SubjectSkillConfig
) -> str:
    """Render `DiscrepancyReportOutput` into structured Markdown."""
    lines: list[str] = [
        f"# {skill.display_name} — Review & Discrepancy Report",
        "",
        f"**Overall Status:** `{model.overall_status}`",
        "",
        "## Mix & Structural Compliance",
        f"- **Requested:** `{model.mix_compliance.requested_mix or 'N/A'}`",
        f"- **Actual:** `{model.mix_compliance.actual_mix or 'N/A'}`",
        f"- **Compliant:** `{'YES' if model.mix_compliance.is_compliant else 'NO'}`",
        "",
        "## Item-by-Item Review",
        "",
    ]
    for item in model.question_reviews:
        lines.append(f"### Question {item.question_number} — `{item.status}`")
        lines.append(f"- **Syllabus Source:** {item.syllabus_source or 'Verified'}")
        if item.issues:
            lines.append("- **Issues Found:**")
            for issue in item.issues:
                lines.append(f"  - {issue}")
        else:
            lines.append("- **Issues Found:** None")
        if item.suggested_fix:
            lines.append(f"- **Suggested Fix:** {item.suggested_fix}")
        lines.append("")

    if model.summary_notes:
        lines.append("## Summary Notes")
        for note in model.summary_notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_answer_key_markdown(
    model: AnswerKeyOutput, skill: SubjectSkillConfig
) -> str:
    """Render `AnswerKeyOutput` according to subject `answer_key_style`."""
    style = skill.output_formats.answer_key_style
    derivation_heading = {
        "execution_trace": "Execution Trace & Step-by-Step Walkthrough",
        "concise_with_rationale": "Conceptual Rationale & Derivation",
        "step_by_step": "Step-by-Step Worked Derivation",
    }.get(style, "Step-by-Step Worked Derivation")

    lines: list[str] = [
        f"# {skill.display_name} — Worked Answer Key & Solutions",
        "",
        f"**Subject:** {skill.display_name} (`{skill.subject_id}`) | **Format Style:** `{style}`",
        "",
        "---",
        "",
    ]

    for sol in model.solutions:
        lines.append(
            f"### Question {sol.question_number} [{sol.question_type}]"
        )
        lines.append(f"- **Correct Answer:** {sol.correct_answer}")
        if sol.ambiguity_flag:
            lines.append(f"- **Ambiguity / Solvability Alert:** {sol.ambiguity_flag}")
        lines.append(f"- **{derivation_heading}:**")
        lines.append("")
        lines.append(sol.derivation.strip())
        lines.append("")
        if sol.distractor_analysis:
            lines.append("- **Option / Distractor Analysis:**")
            for opt_key, reason in sol.distractor_analysis.items():
                lines.append(f"  - **{opt_key}**: {reason}")
            lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_final_summary_markdown(
    model: FinalReviewSummaryOutput, skill: SubjectSkillConfig
) -> str:
    """Render `FinalReviewSummaryOutput` into Markdown."""
    lines: list[str] = [
        f"# {skill.display_name} — Final Sign-Off Summary",
        "",
        f"**Overall Verdict:** `{model.overall_verdict}`",
        "",
        "## Per-Item Verdicts",
        "",
    ]
    for v in model.verdicts:
        lines.append(f"### Question {v.question_number} — `{v.verdict}`")
        lines.append(f"- **Rationale:** {v.rationale or 'Verified OK'}")
        if v.required_change:
            lines.append(f"- **Required Change:** {v.required_change}")
        lines.append("")

    if model.consolidated_action_items:
        lines.append("## Consolidated Action Items")
        for item in model.consolidated_action_items:
            lines.append(f"- {item}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_coding_readme_markdown(
    problem: CodingProblemSpec, skill: SubjectSkillConfig | None = None
) -> str:
    """Render a student-facing `README.md` for a single coding problem (no solution leaked)."""
    sections_order = (
        skill.output_formats.coding_readme_sections
        if skill is not None
        else [
            "Problem Statement",
            "Input Format",
            "Output Format",
            "Constraints",
            "Sample Test Cases",
        ]
    )
    lines: list[str] = [
        f"# Problem {problem.problem_number}: {problem.title}",
        "",
        f"**Language:** `{problem.language}` | **Difficulty:** `{problem.difficulty}`"
        + (f" | **Week:** {problem.week}" if problem.week else ""),
        "",
    ]

    for sec in sections_order:
        sec_lower = sec.lower()
        lines.append(f"## {sec}")
        lines.append("")
        if "statement" in sec_lower or "scenario" in sec_lower:
            lines.append(problem.problem_statement.strip())
        elif "input" in sec_lower or "schema" in sec_lower or "environment" in sec_lower or "prototype" in sec_lower or "class" in sec_lower:
            lines.append(problem.input_format.strip())
        elif "output" in sec_lower:
            lines.append(problem.output_format.strip())
        elif "constraint" in sec_lower or "edge" in sec_lower or "compilation" in sec_lower:
            if problem.constraints:
                for c in problem.constraints:
                    lines.append(f"- {c}")
            else:
                lines.append("- Standard time and memory limits apply.")
        elif "sample" in sec_lower or "test" in sec_lower:
            visible_samples = [
                tc for tc in problem.sample_tests if tc.visibility != "private"
            ]
            if not visible_samples and problem.public_tests:
                visible_samples = [
                    tc for tc in problem.public_tests if tc.visibility != "private"
                ][:2]
            if visible_samples:
                for idx_s, tc in enumerate(visible_samples, start=1):
                    label_idx = tc.index if tc.index > 0 else idx_s
                    lines.append(f"### Sample {label_idx}")
                    lines.append("**Input:**")
                    lines.append(f"```text\n{tc.stdin_input.rstrip()}\n```")
                    lines.append("**Output:**")
                    lines.append(f"```text\n{tc.expected_stdout.rstrip()}\n```")
                    if tc.explanation:
                        lines.append(f"**Explanation:** {tc.explanation}")
                    lines.append("")
            else:
                lines.append("See `public/` directory for sample test case files.")
        else:
            lines.append(problem.problem_statement.strip())
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def _should_hide_student_reference_solution() -> bool:
    """Return True when active crew inputs instruct hiding reference solutions from student files."""
    try:
        from klyra.crews.base_task import get_active_crew_inputs

        inputs = get_active_crew_inputs()
    except Exception:
        inputs = {}
    inc_ref = str(inputs.get("include_reference") or "").strip().lower()
    if inc_ref == "no":
        return True
    notes_combined = (
        f"{inputs.get('manual_instructions') or ''} {inputs.get('notes') or ''}".lower()
    )
    if (
        ("remove" in notes_combined or "no " in notes_combined or "without" in notes_combined or "exclude" in notes_combined)
        and ("reference solution" in notes_combined or "reference solutions" in notes_combined)
    ):
        return True
    return False


def render_coding_problems_markdown(
    model: CodingProblemSetOutput,
    skill: SubjectSkillConfig,
    *,
    for_student_file: bool = False,
) -> str:
    """Render `CodingProblemSetOutput` or `RevisedCodingProblemSetOutput` into Markdown."""
    lines: list[str] = [
        f"# {skill.display_name} — Coding Problem Set ({model.language})",
        "",
        f"**Subject:** {skill.display_name} (`{skill.subject_id}`) | "
        f"**Language:** `{model.language}` | "
        f"**Scope:** {model.scope_label or 'Course-wide'}",
        "",
    ]

    if isinstance(model, RevisedCodingProblemSetOutput) and model.changes_applied:
        lines.append("## Revision Log")
        for change in model.changes_applied:
            lines.append(f"- {change}")
        lines.append("")

    hide_reference = for_student_file and _should_hide_student_reference_solution()

    for prob in model.problems:
        lines.append("---")
        lines.append("")
        lines.append(render_coding_readme_markdown(prob, skill).strip())
        lines.append("")
        if not hide_reference:
            lines.append("## Reference Solution")
            lines.append("")
            lines.append(f"```{prob.language}\n{prob.reference_solution.strip()}\n```")
            if prob.complexity_analysis:
                lines.append("")
                lines.append(f"**Complexity Analysis:** {prob.complexity_analysis}")
            lines.append("")

        if not for_student_file and (prob.public_tests or prob.private_tests):
            lines.append("## Full Draft Test Suite (Validator Context — Excluded from Student-Facing Files)")
            lines.append("")
            if prob.public_tests:
                lines.append("### Public Test Cases")
                lines.append("")
                for idx_p, tc in enumerate(prob.public_tests, start=1):
                    lines.append(f"#### Public Test {idx_p}")
                    lines.append("**Input (`stdin_input`):**")
                    lines.append(f"```text\n{tc.stdin_input.rstrip()}\n```")
                    lines.append("**Expected Output (`expected_stdout`):**")
                    lines.append(f"```text\n{tc.expected_stdout.rstrip()}\n```")
                    lines.append("")
            if prob.private_tests:
                lines.append("### Private Test Cases")
                lines.append("")
                for idx_p, tc in enumerate(prob.private_tests, start=1):
                    lines.append(f"#### Private Test {idx_p}")
                    lines.append("**Input (`stdin_input`):**")
                    lines.append(f"```text\n{tc.stdin_input.rstrip()}\n```")
                    lines.append("**Expected Output (`expected_stdout`):**")
                    lines.append(f"```text\n{tc.expected_stdout.rstrip()}\n```")
                    lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_coding_discrepancy_markdown(
    model: CodingDiscrepancyReportOutput, skill: SubjectSkillConfig
) -> str:
    """Render `CodingDiscrepancyReportOutput` into Markdown."""
    lines: list[str] = [
        f"# {skill.display_name} — Coding Problems Review Report",
        "",
        f"**Overall Status:** `{model.overall_status}`",
        "",
    ]
    for item in model.problem_reviews:
        lines.append(f"## Problem {item.problem_number} — `{item.status}`")
        lines.append(f"- **Syllabus Alignment:** {item.syllabus_alignment or 'Verified'}")
        lines.append(f"- **Language Compliance:** {item.language_compliance or 'Verified'}")
        if item.issues:
            lines.append("- **Issues:**")
            for issue in item.issues:
                lines.append(f"  - {issue}")
        else:
            lines.append("- **Issues:** None")
        if item.suggested_fix:
            lines.append(f"- **Suggested Fix:** {item.suggested_fix}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_test_validation_markdown(
    model: TestCaseValidationOutput, skill: SubjectSkillConfig
) -> str:
    """Render `TestCaseValidationOutput` into Markdown (`-tests.md`)."""
    lines: list[str] = [
        f"# {skill.display_name} — Coding Test Suite & Execution Verification",
        "",
        f"**Language:** `{model.language}` | **All Tests Passed:** `{'YES' if model.all_passed else 'NO'}`",
        "",
    ]
    for vp in model.validated_problems:
        lines.append(f"## Problem {vp.problem_number}: {vp.title}")
        lines.append(
            f"- **Execution Verified:** `{'YES' if vp.execution_verified else 'NO'}` "
            f"(Sandbox Available: `{'YES' if vp.sandbox_available else 'NO'}`, "
            f"Rewrite Attempts: `{vp.rewrite_attempts}`)"
        )
        if vp.execution_summary:
            lines.append(f"- **Execution Summary:** {vp.execution_summary}")
        if vp.package_files_written:
            lines.append(
                f"- **Package Files Written:** {', '.join(f'`{p}`' for p in vp.package_files_written)}"
            )
        lines.append("")
        lines.append("### Verified Reference Solution")
        lines.append(f"```{vp.language}\n{vp.reference_solution.strip()}\n```")
        lines.append("")
        lines.append(f"### Public Test Cases ({len(vp.public_tests)})")
        for tc in vp.public_tests:
            lines.append(f"#### Public Test {tc.index}")
            if tc.explanation:
                lines.append(f"- **Covers:** {tc.explanation}")
            lines.append(f"- **Input (`in.txt`):**\n```text\n{tc.stdin_input.rstrip()}\n```")
            lines.append(
                f"- **Expected Output (`out.txt`):**\n```text\n{tc.expected_stdout.rstrip()}\n```"
            )
        lines.append("")
        lines.append(f"### Private Test Cases ({len(vp.private_tests)})")
        for tc in vp.private_tests:
            lines.append(f"#### Private Test {tc.index}")
            if tc.explanation:
                lines.append(f"- **Covers:** {tc.explanation}")
            lines.append(f"- **Input (`in.txt`):**\n```text\n{tc.stdin_input.rstrip()}\n```")
            lines.append(
                f"- **Expected Output (`out.txt`):**\n```text\n{tc.expected_stdout.rstrip()}\n```"
            )
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_coding_final_summary_markdown(
    model: CodingFinalSummaryOutput, skill: SubjectSkillConfig
) -> str:
    """Render `CodingFinalSummaryOutput` into Markdown."""
    lines: list[str] = [
        f"# {skill.display_name} — Coding Final Sign-Off Summary",
        "",
        f"**Overall Verdict:** `{model.overall_verdict}`",
        "",
    ]
    for pv in model.problem_verdicts:
        lines.append(f"## Problem {pv.problem_number} — `{pv.verdict}`")
        lines.append(f"- **Rationale:** {pv.rationale or 'Verified OK'}")
        if pv.required_changes:
            lines.append("- **Required Changes:**")
            for chg in pv.required_changes:
                lines.append(f"  - {chg}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_teaching_material_markdown(
    model: TeachingMaterialOutput, skill: SubjectSkillConfig
) -> str:
    """Render `TeachingMaterialOutput` or `RevisedTeachingMaterialOutput` per subject `teaching_sections`."""
    fmt = skill.output_formats
    lines: list[str] = [
        f"# {model.title or f'{skill.display_name} — Study Material'}",
        "",
        f"**Subject:** {skill.display_name} (`{skill.subject_id}`) | "
        f"**Scope:** {model.scope_label or 'Course-wide'}",
        "",
    ]

    if isinstance(model, RevisedTeachingMaterialOutput) and model.changes_applied:
        lines.append("## Revision Log")
        for change in model.changes_applied:
            lines.append(f"- {change}")
        lines.append("")

    # Render sections in the order specified by `skill.output_formats.teaching_sections`
    rendered_categories: set[str] = set()

    def _render_category(cat: str, heading_title: str) -> None:
        first_for_cat = cat not in rendered_categories
        rendered_categories.add(cat)
        lines.append(f"## {heading_title}")
        lines.append("")
        if cat == "objectives" and first_for_cat:
            for obj in model.learning_objectives:
                lines.append(f"- {obj}")
            lines.append("")
        elif cat == "concepts":
            if first_for_cat:
                for sec in model.concept_sections:
                    lines.append(f"### {sec.heading}")
                    lines.append("")
                    if sec.diagram_markdown:
                        lines.append(sec.diagram_markdown.strip())
                        lines.append("")
                    lines.append(sec.explanation_markdown.strip())
                    lines.append("")
            else:
                for sec in model.concept_sections:
                    lines.append(f"- **{sec.heading}:** {sec.explanation_markdown.strip()}")
                lines.append("")
        elif cat == "examples" and first_for_cat:
            for idx, sec in enumerate(model.concept_sections, start=1):
                if sec.worked_example_prompt.strip():
                    lines.append(f"### Worked Example {idx}: {sec.heading}")
                    lines.append("")
                    lines.append(sec.worked_example_prompt.strip())
                    lines.append("")
        elif cat == "pitfalls" and first_for_cat:
            for pit in model.common_pitfalls:
                lines.append(f"- {pit}")
            lines.append("")
        elif cat == "practice" and first_for_cat:
            for pq in model.practice_questions:
                lines.append(
                    f"### Practice Question {pq.question_number} [{pq.question_type}]"
                )
                lines.append("")
                lines.append(pq.prompt.strip())
                if pq.options:
                    lines.append("")
                    for idx, opt in enumerate(pq.options):
                        label = format_option_label(idx, fmt.option_label_style)
                        lines.append(f"{label} {strip_option_prefix(opt)}")
                lines.append("")

    for sec_title in fmt.teaching_sections:
        low = sec_title.lower()
        if "objective" in low:
            _render_category("objectives", sec_title)
        elif "example" in low or "trace" in low or "walkthrough" in low:
            _render_category("examples", sec_title)
        elif "pitfall" in low or "bug" in low or "trap" in low or "safety" in low or "error" in low or "anti-pattern" in low:
            _render_category("pitfalls", sec_title)
        elif "practice" in low or "exercise" in low or "challenge" in low:
            _render_category("practice", sec_title)
        else:
            _render_category("concepts", sec_title)

    # Ensure no populated section is omitted even if custom section titles didn't match
    if model.learning_objectives and "objectives" not in rendered_categories:
        _render_category("objectives", "Learning Objectives")
    if model.concept_sections and "concepts" not in rendered_categories:
        _render_category("concepts", "Concept Breakdown")
    if any(s.worked_example_prompt for s in model.concept_sections) and "examples" not in rendered_categories:
        _render_category("examples", "Worked Examples")
    if model.common_pitfalls and "pitfalls" not in rendered_categories:
        _render_category("pitfalls", "Common Pitfalls")
    if model.practice_questions and "practice" not in rendered_categories:
        _render_category("practice", "Practice Questions")

    if model.key_takeaways:
        lines.append("## Key Takeaways")
        lines.append("")
        for kt in model.key_takeaways:
            lines.append(f"- {kt}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_teaching_answers_markdown(
    model: TeachingAnswersOutput, skill: SubjectSkillConfig
) -> str:
    """Render `TeachingAnswersOutput` into companion `-answers.md`."""
    lines: list[str] = [
        f"# {skill.display_name} — Worked Examples & Practice Solutions",
        "",
        "## Part 1: Worked Example Walkthroughs",
        "",
    ]
    for idx, ex in enumerate(model.worked_example_derivations, start=1):
        lines.append(f"### Worked Example {idx}: {ex.example_title}")
        lines.append("")
        lines.append(ex.step_by_step_derivation.strip())
        if ex.final_result:
            lines.append("")
            lines.append(f"**Final Result:** {ex.final_result}")
        lines.append("")

    lines.append("## Part 2: Practice Question Solutions")
    lines.append("")
    for ans in model.practice_answers:
        lines.append(f"### Practice Question {ans.question_number}")
        lines.append(f"- **Correct Answer:** {ans.correct_answer}")
        lines.append(f"- **Explanation:** {ans.explanation}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def render_parse_report_markdown(
    model: DocumentParseReportOutput, skill: SubjectSkillConfig
) -> str:
    lines: list[str] = [
        f"# {skill.display_name} — Document Parse Report",
        "",
    ]
    for doc in model.documents:
        lines.append(f"## `{doc.source_file}` (`{doc.category}`)")
        if doc.week is not None:
            lines.append(f"- **Week:** {doc.week}")
        if doc.extracted_topics:
            lines.append(f"- **Topics:** {', '.join(doc.extracted_topics)}")
        if doc.extracted_assets:
            lines.append(f"- **Extracted Assets:** {', '.join(doc.extracted_assets)}")
        if doc.summary:
            lines.append(f"- **Summary:** {doc.summary}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_index_report_markdown(
    model: SyllabusIndexReportOutput, skill: SubjectSkillConfig
) -> str:
    lines: list[str] = [
        f"# {skill.display_name} — Syllabus Indexing Report",
        "",
        f"- **Total Chunks Indexed:** {model.total_chunks_indexed}",
        f"- **Weeks Covered:** {', '.join(str(w) for w in model.weeks_covered) or 'None'}",
        f"- **Categories Indexed:** {', '.join(model.categories_indexed) or 'None'}",
        f"- **Missing Weeks (1..12):** {', '.join(str(w) for w in model.missing_weeks) or 'None'}",
        "",
    ]
    if model.indexing_notes:
        lines.append("## Notes")
        for note in model.indexing_notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def render_conversion_report_markdown(
    model: ConversionReportOutput, skill: SubjectSkillConfig
) -> str:
    lines: list[str] = [
        f"# {skill.display_name} — Format Conversion Report",
        "",
    ]
    for rec in model.records:
        lines.append(
            f"- `{rec.source_path}` -> `{rec.output_path or 'N/A'}` (**{rec.status}**): {rec.detail}"
        )
    return "\n".join(lines).strip() + "\n"


def render_task_output_markdown(
    model: BaseModel,
    skill: SubjectSkillConfig | None = None,
    root: Path = DOCS_ROOT,
    *,
    for_student_file: bool = False,
) -> str:
    """Render any task output Pydantic model into subject-formatted Markdown."""
    resolved_skill = _resolve_skill(model, skill, root=root)

    if isinstance(model, QuestionPaperOutput):
        return render_qp_markdown(model, resolved_skill)
    if isinstance(model, DiscrepancyReportOutput):
        return render_discrepancy_markdown(model, resolved_skill)
    if isinstance(model, AnswerKeyOutput):
        return render_answer_key_markdown(model, resolved_skill)
    if isinstance(model, FinalReviewSummaryOutput):
        return render_final_summary_markdown(model, resolved_skill)
    if isinstance(model, CodingProblemSetOutput):
        return render_coding_problems_markdown(
            model, resolved_skill, for_student_file=for_student_file
        )
    if isinstance(model, CodingDiscrepancyReportOutput):
        return render_coding_discrepancy_markdown(model, resolved_skill)
    if isinstance(model, TestCaseValidationOutput):
        return render_test_validation_markdown(model, resolved_skill)
    if isinstance(model, CodingFinalSummaryOutput):
        return render_coding_final_summary_markdown(model, resolved_skill)
    if isinstance(model, TeachingMaterialOutput):
        return render_teaching_material_markdown(model, resolved_skill)
    if isinstance(model, TeachingAnswersOutput):
        return render_teaching_answers_markdown(model, resolved_skill)
    if isinstance(model, DocumentParseReportOutput):
        return render_parse_report_markdown(model, resolved_skill)
    if isinstance(model, SyllabusIndexReportOutput):
        return render_index_report_markdown(model, resolved_skill)
    if isinstance(model, ConversionReportOutput):
        return render_conversion_report_markdown(model, resolved_skill)

    return model.model_dump_json(indent=2)


def verify_rendered_markdown(
    markdown_text: str,
    model: BaseModel,
    skill: SubjectSkillConfig | None = None,
    root: Path = DOCS_ROOT,
) -> list[str]:
    """Verify that both the structured `model` and `markdown_text` conform to subject rules.

    Returns a list of human-readable error strings (empty list == valid).
    """
    resolved_skill = _resolve_skill(model, skill, root=root)
    fmt = resolved_skill.output_formats
    errors: list[str] = []

    if isinstance(model, QuestionPaperOutput):
        if not model.questions:
            errors.append("QuestionPaperOutput contains 0 questions.")
        if _LEAKED_ANSWER_RE.search(markdown_text):
            errors.append(
                "Question Paper contains leaked answer/solution headings or markers (`Answer:` / `Solution:`)."
            )
        allowed = {t.upper() for t in resolved_skill.allowed_question_types}
        for q in model.questions:
            if allowed and q.question_type.upper() not in allowed:
                errors.append(
                    f"Question {q.question_number} has type '{q.question_type}', which is not in subject '{resolved_skill.subject_id}' allowed_question_types {sorted(allowed)}."
                )
            if _LEAKED_ANSWER_RE.search(q.stem):
                errors.append(
                    f"Question {q.question_number} stem leaks the answer or solution."
                )
            if q.question_type in {"MCQ", "MSQ"} and len(q.options) < 2:
                errors.append(
                    f"Question {q.question_number} ({q.question_type}) must provide at least 2 options (found {len(q.options)})."
                )
        if fmt.group_by_section and model.questions and "## Section " not in markdown_text:
            errors.append(
                f"Subject '{resolved_skill.subject_id}' requires `group_by_section=True`, but no `## Section` headers were found."
            )
        if fmt.include_marks_per_question and model.questions and "[Marks:" not in markdown_text:
            errors.append(
                f"Subject '{resolved_skill.subject_id}' requires `include_marks_per_question=True`, but `[Marks: ...]` badges are missing."
            )

    elif isinstance(model, TeachingMaterialOutput):
        if _LEAKED_ANSWER_RE.search(markdown_text):
            errors.append(
                "Teaching material handout leaks worked-example derivations or practice question answers (these belong in `-answers.md`)."
            )
        for sec_title in fmt.teaching_sections:
            if f"## {sec_title}" not in markdown_text:
                errors.append(
                    f"Teaching material for '{resolved_skill.subject_id}' is missing required section heading `## {sec_title}`."
                )

    elif isinstance(model, CodingProblemSetOutput):
        if not model.problems:
            errors.append("CodingProblemSetOutput contains 0 problems.")
        for prob in model.problems:
            if not prob.reference_solution.strip():
                errors.append(
                    f"Problem {prob.problem_number} ('{prob.title}') is missing a reference_solution."
                )

    return errors
