"""Structured Pydantic output schemas for all CrewAI tasks across Klyra's 8 crews."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field, field_validator


# ============================================================================
# 1. Question Paper Generation & Revision Crew Schemas
# ============================================================================


class QuestionItem(BaseModel):
    """A single exam question in a Question Paper."""

    question_number: int = Field(ge=1, description="1-based sequential question number.")
    question_code: str = Field(
        default="",
        description="Week and question code, e.g. 'W03-Q01'.",
    )
    question_type: Literal["MCQ", "MSQ", "NAT", "SA"] = Field(
        default="MCQ",
        description="Question type: MCQ, MSQ, NAT, or SA.",
    )
    marks: int = Field(default=1, ge=1, description="Marks allocated to this question.")
    week: int | None = Field(
        default=None, description="Syllabus week number this question draws from."
    )
    topic: str = Field(default="", description="Specific syllabus topic tested.")
    comprehension_stem: str | None = Field(
        default=None,
        description="Shared comprehension passage, schema block, code snippet, or pseudocode block if applicable.",
    )
    diagram_markdown: str | None = Field(
        default=None,
        description="Markdown image link `![alt](../.assets/...)` if a visual diagram is attached.",
    )
    stem: str = Field(
        description="The question text/prompt ONLY. Must NEVER contain the answer or solution."
    )
    options: list[str] = Field(
        default_factory=list,
        description="List of option texts (without leading (a)/(b) labels, or with labels which will be normalized). Empty for NAT/SA.",
    )

    @field_validator("question_type", mode="before")
    @classmethod
    def _normalize_qtype(cls, v: object) -> str:
        s = str(v or "MCQ").strip().upper()
        if s in {"MCQ", "MSQ", "NAT", "SA"}:
            return s
        if "MULTI" in s or "MSQ" in s:
            return "MSQ"
        if "NUM" in s or "NAT" in s:
            return "NAT"
        if "SHORT" in s or "SUBJ" in s or "SA" in s:
            return "SA"
        return "MCQ"


class QuestionPaperOutput(BaseModel):
    """Structured output for `generate_qp_task`."""

    subject: str = Field(description="Subject identifier (e.g. 'dbms', 'ct').")
    title: str = Field(description="Title of the Question Paper.")
    scope_label: str = Field(default="", description="Scope label (e.g. 'Week 3' or 'All Weeks').")
    difficulty: str = Field(default="medium", description="Target difficulty level.")
    total_marks: int = Field(default=0, ge=0, description="Sum of marks across all questions.")
    instructions: list[str] = Field(
        default_factory=list,
        description="General exam instructions shown at the top of the question paper.",
    )
    questions: list[QuestionItem] = Field(
        default_factory=list,
        description="Ordered list of exam questions.",
    )


class RevisedQuestionPaperOutput(QuestionPaperOutput):
    """Structured output for `revise_qp_task`."""

    changes_applied: list[str] = Field(
        default_factory=list,
        description="List of specific revisions applied in response to reviewer/faculty feedback.",
    )


class QuestionDiscrepancy(BaseModel):
    """Per-question review finding in `review_qp_task` / `review_material_task`."""

    question_number: int = Field(ge=1, description="1-based question or section number.")
    status: Literal["OK", "DISCREPANCY"] = Field(
        default="OK", description="'OK' if compliant, 'DISCREPANCY' if issues found."
    )
    syllabus_source: str = Field(
        default="",
        description="Supporting syllabus week/chunk reference or 'NOT FOUND IN SYLLABUS'.",
    )
    issues: list[str] = Field(
        default_factory=list,
        description="Specific issues found (empty if status is 'OK').",
    )
    suggested_fix: str | None = Field(
        default=None,
        description="Concrete fix recommendation when status is 'DISCREPANCY'.",
    )

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, v: object) -> str:
        s = str(v or "OK").strip().upper()
        return "DISCREPANCY" if "DISC" in s or "FAIL" in s or "ERR" in s or "ISSUE" in s else "OK"


class MixComplianceSummary(BaseModel):
    """Question-type mix compliance summary."""

    requested_mix: str = Field(default="", description="Requested type counts (e.g. 'MCQ:5,MSQ:3,NAT:2').")
    actual_mix: str = Field(default="", description="Actual counts observed in the paper.")
    is_compliant: bool = Field(default=True, description="True if actual_mix matches requested_mix.")


class DiscrepancyReportOutput(BaseModel):
    """Structured output for `review_qp_task` and `review_material_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    overall_status: Literal["PASS", "NEEDS_REVISION"] = Field(
        default="PASS", description="Overall review verdict."
    )
    mix_compliance: MixComplianceSummary = Field(
        default_factory=MixComplianceSummary,
        description="Question-type mix or structural compliance check.",
    )
    question_reviews: list[QuestionDiscrepancy] = Field(
        default_factory=list,
        description="Per-question or per-section review findings.",
    )
    summary_notes: list[str] = Field(
        default_factory=list,
        description="Additional high-level review observations.",
    )

    @field_validator("overall_status", mode="before")
    @classmethod
    def _normalize_overall(cls, v: object) -> str:
        s = str(v or "PASS").strip().upper()
        return "NEEDS_REVISION" if "REV" in s or "FAIL" in s or "DISC" in s else "PASS"


class WorkedSolution(BaseModel):
    """Worked solution and answer key entry for a single question."""

    question_number: int = Field(ge=1, description="1-based question number.")
    question_type: Literal["MCQ", "MSQ", "NAT", "SA"] = Field(
        default="MCQ", description="Question type."
    )
    correct_answer: str = Field(
        description="Exact option letter(s) (e.g. '(b)' or '(a), (c)'), numerical value/range, or model answer."
    )
    derivation: str = Field(
        description="Step-by-step worked derivation, execution trace, or relational algebra breakdown."
    )
    distractor_analysis: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of option label (e.g. '(a)') to why that option is correct or incorrect.",
    )
    ambiguity_flag: str | None = Field(
        default=None,
        description="Non-empty warning if the question is unsolvable, has 0 correct options, or has multiple valid MCQ answers.",
    )

    @field_validator("question_type", mode="before")
    @classmethod
    def _normalize_qtype(cls, v: object) -> str:
        return QuestionItem._normalize_qtype(v)


class AnswerKeyOutput(BaseModel):
    """Structured output for `solve_qp_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    solutions: list[WorkedSolution] = Field(
        default_factory=list,
        description="Worked solutions for every question in the Question Paper.",
    )


class QuestionVerdict(BaseModel):
    """Final sign-off verdict for an individual question or section."""

    question_number: int = Field(ge=1, description="1-based question or section number.")
    verdict: Literal["OK", "NEEDS_REVISION"] = Field(
        default="OK", description="Final sign-off verdict for this item."
    )
    rationale: str = Field(default="", description="Reason for verdict.")
    required_change: str | None = Field(
        default=None,
        description="Required change if verdict is 'NEEDS_REVISION'.",
    )

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v: object) -> str:
        s = str(v or "OK").strip().upper()
        return "NEEDS_REVISION" if "REV" in s or "FAIL" in s or "DISC" in s else "OK"


class FinalReviewSummaryOutput(BaseModel):
    """Structured output for `final_review_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    overall_verdict: Literal["APPROVED", "NEEDS_REVISION"] = Field(
        default="APPROVED", description="Overall final sign-off decision."
    )
    verdicts: list[QuestionVerdict] = Field(
        default_factory=list,
        description="Per-question or per-section verdicts.",
    )
    consolidated_action_items: list[str] = Field(
        default_factory=list,
        description="Actionable checklist of changes required if NEEDS_REVISION.",
    )

    @field_validator("overall_verdict", mode="before")
    @classmethod
    def _normalize_overall(cls, v: object) -> str:
        s = str(v or "APPROVED").strip().upper()
        return "NEEDS_REVISION" if "REV" in s or "FAIL" in s or "REJECT" in s else "APPROVED"


# ============================================================================
# 2. Coding Problem Generation & Revision Crew Schemas
# ============================================================================


class CodingTestCase(BaseModel):
    """A single public or private test case for a coding problem."""

    index: int = Field(ge=1, description="1-based test case index.")
    visibility: Literal["public", "private"] = Field(
        default="public", description="Whether this test case is public or private."
    )
    stdin_input: str = Field(description="Exact stdin content passed to the program.")
    expected_stdout: str = Field(description="Exact expected stdout produced by the reference solution.")
    explanation: str | None = Field(
        default=None,
        description="Explanation of why this input produces the expected output or what edge case it tests.",
    )


class CodingProblemSpec(BaseModel):
    """Complete specification of a single coding problem."""

    problem_number: int = Field(ge=1, description="1-based problem number.")
    title: str = Field(description="Problem title.")
    language: str = Field(default="python", description="Target programming language.")
    difficulty: str = Field(default="medium", description="Difficulty level.")
    week: int | None = Field(default=None, description="Syllabus week number.")
    topics: list[str] = Field(default_factory=list, description="Syllabus topics covered.")
    problem_statement: str = Field(description="Detailed problem statement.")
    input_format: str = Field(description="Input format specification.")
    output_format: str = Field(description="Output format specification.")
    constraints: list[str] = Field(
        default_factory=list,
        description="Input size, value range, and time/space complexity constraints.",
    )
    sample_tests: list[CodingTestCase] = Field(
        default_factory=list,
        description="Student-facing sample/public test cases with explanations (no hidden/private tests).",
    )
    public_tests: list[CodingTestCase] = Field(
        default_factory=list,
        description="Full public test cases for evaluation (including any SQL schema/data seed statements for python-db/postgresql).",
    )
    private_tests: list[CodingTestCase] = Field(
        default_factory=list,
        description="Full private/hidden test cases for evaluation (excluded from student-facing README and problem description).",
    )
    reference_solution: str = Field(
        description="Complete, runnable reference solution source code in the target language (saved to solution.{ext}; excluded from student-facing README)."
    )
    complexity_analysis: str = Field(
        default="",
        description="Time and space complexity analysis of the reference solution.",
    )


class CodingProblemSetOutput(BaseModel):
    """Structured output for `generate_problems_task`."""

    subject: str = Field(description="Subject identifier.")
    language: str = Field(default="python", description="Target programming language.")
    scope_label: str = Field(default="", description="Scope label.")
    problems: list[CodingProblemSpec] = Field(
        default_factory=list,
        description="List of generated coding problems.",
    )


class RevisedCodingProblemSetOutput(CodingProblemSetOutput):
    """Structured output for `revise_problems_task`."""

    changes_applied: list[str] = Field(
        default_factory=list,
        description="List of specific revisions applied to the coding problems.",
    )


class CodingDiscrepancyItem(BaseModel):
    """Review finding for a single coding problem."""

    problem_number: int = Field(ge=1, description="1-based problem number.")
    status: Literal["OK", "DISCREPANCY"] = Field(default="OK")
    syllabus_alignment: str = Field(default="", description="Syllabus topic/week verification.")
    language_compliance: str = Field(default="", description="Language & idiom compliance check.")
    issues: list[str] = Field(default_factory=list, description="Specific issues identified.")
    suggested_fix: str | None = Field(default=None, description="Suggested fix if status is DISCREPANCY.")

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_status(cls, v: object) -> str:
        return QuestionDiscrepancy._normalize_status(v)


class CodingDiscrepancyReportOutput(BaseModel):
    """Structured output for `review_problems_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    overall_status: Literal["PASS", "NEEDS_REVISION"] = Field(default="PASS")
    problem_reviews: list[CodingDiscrepancyItem] = Field(
        default_factory=list,
        description="Per-problem review findings.",
    )

    @field_validator("overall_status", mode="before")
    @classmethod
    def _normalize_overall(cls, v: object) -> str:
        return DiscrepancyReportOutput._normalize_overall(v)


class ValidatedProblemResult(BaseModel):
    """Validated coding problem with verified reference solution and public/private test suites."""

    problem_number: int = Field(ge=1, description="1-based problem number.")
    title: str = Field(default="", description="Problem title.")
    language: str = Field(default="python", description="Programming language.")
    readme_markdown: str = Field(
        default="",
        description="Student-facing README.md content (without reference solution).",
    )
    reference_solution: str = Field(
        description="Verified reference solution code that passes 100% of public and private tests."
    )
    public_tests: list[CodingTestCase] = Field(
        default_factory=list,
        description="Verified public test cases (at least 3).",
    )
    private_tests: list[CodingTestCase] = Field(
        default_factory=list,
        description="Verified private edge/boundary/stress test cases (at least 5).",
    )
    execution_verified: bool = Field(
        default=True,
        description="True if verified via sandbox execution.",
    )
    sandbox_available: bool = Field(
        default=True,
        description="True if Docker sandbox was available during validation.",
    )
    rewrite_attempts: int = Field(
        default=0,
        ge=0,
        description="Number of rewrite/fix iterations performed before all tests passed.",
    )
    execution_summary: str = Field(
        default="",
        description="Summary of test case execution results (pass/total, exit codes, timings).",
    )
    package_files_written: list[str] = Field(
        default_factory=list,
        description="List of package files written to disk.",
    )


class TestCaseValidationOutput(BaseModel):
    """Structured output for `validate_test_cases_task`."""

    __test__ = False

    subject: str = Field(default="", description="Subject identifier.")
    language: str = Field(default="python", description="Programming language.")
    all_passed: bool = Field(
        default=True,
        description="True only if every problem's reference solution passed all public and private test cases.",
    )
    validated_problems: list[ValidatedProblemResult] = Field(
        default_factory=list,
        description="Validation results and test suites for each problem.",
    )


class CodingProblemVerdict(BaseModel):
    """Final sign-off verdict for a single coding problem."""

    problem_number: int = Field(ge=1, description="1-based problem number.")
    verdict: Literal["OK", "NEEDS_REVISION"] = Field(default="OK")
    rationale: str = Field(default="", description="Justification for the verdict.")
    required_changes: list[str] = Field(
        default_factory=list,
        description="Changes required if verdict is NEEDS_REVISION.",
    )

    @field_validator("verdict", mode="before")
    @classmethod
    def _normalize_verdict(cls, v: object) -> str:
        return QuestionVerdict._normalize_verdict(v)


class CodingFinalSummaryOutput(BaseModel):
    """Structured output for `final_coding_review_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    overall_verdict: Literal["APPROVED", "NEEDS_REVISION"] = Field(default="APPROVED")
    problem_verdicts: list[CodingProblemVerdict] = Field(
        default_factory=list,
        description="Per-problem final verdicts.",
    )

    @field_validator("overall_verdict", mode="before")
    @classmethod
    def _normalize_overall(cls, v: object) -> str:
        return FinalReviewSummaryOutput._normalize_overall(v)


# ============================================================================
# 3. Teaching Material Generation & Revision Crew Schemas
# ============================================================================


class ConceptSection(BaseModel):
    """A concept section in the teaching material."""

    heading: str = Field(description="Concept heading.")
    explanation_markdown: str = Field(
        description="Detailed explanation, definitions, tables, or formal syntax."
    )
    diagram_markdown: str | None = Field(
        default=None,
        description="Markdown image link `![alt](../.assets/...)` if a visual diagram is included.",
    )
    worked_example_prompt: str = Field(
        default="",
        description="Worked example problem statement (walkthrough derivation goes in companion answers).",
    )


class PracticeQuestion(BaseModel):
    """An unworked practice question at the end of teaching material."""

    question_number: int = Field(ge=1, description="1-based practice question number.")
    question_type: Literal["MCQ", "MSQ", "NAT", "SA"] = Field(default="MCQ")
    prompt: str = Field(description="Practice question stem (no answer or solution here).")
    options: list[str] = Field(
        default_factory=list,
        description="Choices for MCQ/MSQ questions.",
    )

    @field_validator("question_type", mode="before")
    @classmethod
    def _normalize_qtype(cls, v: object) -> str:
        return QuestionItem._normalize_qtype(v)


class TeachingMaterialOutput(BaseModel):
    """Structured output for `generate_material_task`."""

    subject: str = Field(description="Subject identifier.")
    title: str = Field(description="Study handout title.")
    scope_label: str = Field(default="", description="Scope label (e.g. 'Week 3').")
    learning_objectives: list[str] = Field(
        default_factory=list,
        description="Measurable learning objectives for this module.",
    )
    concept_sections: list[ConceptSection] = Field(
        default_factory=list,
        description="Structured concept breakdown sections.",
    )
    common_pitfalls: list[str] = Field(
        default_factory=list,
        description="Common student misconceptions and pitfalls to avoid.",
    )
    practice_questions: list[PracticeQuestion] = Field(
        default_factory=list,
        description="Unworked practice questions for self-assessment.",
    )
    key_takeaways: list[str] = Field(
        default_factory=list,
        description="Concise summary takeaways.",
    )


class RevisedTeachingMaterialOutput(TeachingMaterialOutput):
    """Structured output for `revise_material_task`."""

    changes_applied: list[str] = Field(
        default_factory=list,
        description="List of specific revisions applied to the teaching material.",
    )


class WorkedExampleDerivation(BaseModel):
    """Step-by-step derivation for a worked example in the teaching material."""

    example_title: str = Field(description="Heading or title of the worked example.")
    step_by_step_derivation: str = Field(description="Complete step-by-step walkthrough.")
    final_result: str = Field(default="", description="Final result or output of the example.")


class PracticeQuestionAnswer(BaseModel):
    """Complete solution for a practice question in the teaching material."""

    question_number: int = Field(ge=1, description="1-based practice question number.")
    correct_answer: str = Field(description="Exact answer option(s), value, or short answer.")
    explanation: str = Field(description="Step-by-step explanation and option rationale.")


class TeachingAnswersOutput(BaseModel):
    """Structured output for `solve_examples_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    worked_example_derivations: list[WorkedExampleDerivation] = Field(
        default_factory=list,
        description="Full walkthroughs for the worked examples in the handout.",
    )
    practice_answers: list[PracticeQuestionAnswer] = Field(
        default_factory=list,
        description="Solutions and explanations for all practice questions.",
    )


# ============================================================================
# 4. Ingestion & Format Converter Crew Schemas
# ============================================================================


class ParsedDocumentEntry(BaseModel):
    """Summary of a single parsed source document during ingestion."""

    source_file: str = Field(description="Path to the source file.")
    category: str = Field(default="week", description="Category: week, pyq, transcript, additional.")
    week: int | None = Field(default=None, description="Week number if applicable.")
    extracted_topics: list[str] = Field(
        default_factory=list,
        description="Key syllabus topics extracted from this document.",
    )
    extracted_assets: list[str] = Field(
        default_factory=list,
        description="Paths to diagram/image assets extracted from this document.",
    )
    summary: str = Field(default="", description="Concise summary of document contents.")


class DocumentParseReportOutput(BaseModel):
    """Structured output for `parse_documents_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    documents: list[ParsedDocumentEntry] = Field(
        default_factory=list,
        description="Per-document parse summaries.",
    )


class SyllabusIndexReportOutput(BaseModel):
    """Structured output for `index_syllabus_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    total_chunks_indexed: int = Field(default=0, ge=0, description="Total chunks written to Chroma.")
    weeks_covered: list[int] = Field(default_factory=list, description="List of week numbers indexed.")
    categories_indexed: list[str] = Field(
        default_factory=list,
        description="Categories indexed (week, pyq, transcript, additional).",
    )
    missing_weeks: list[int] = Field(
        default_factory=list,
        description="Weeks 1..12 with no source material.",
    )
    indexing_notes: list[str] = Field(
        default_factory=list,
        description="Notes on indexing status and coverage.",
    )


class FileConversionRecord(BaseModel):
    """Conversion outcome for a single file."""

    source_path: str = Field(description="Input file path.")
    output_path: str = Field(default="", description="Converted output file path.")
    status: Literal["converted", "skipped", "failed"] = Field(default="converted")
    detail: str = Field(default="", description="Conversion details or error message.")


class ConversionReportOutput(BaseModel):
    """Structured output for `convert_files_task`."""

    subject: str = Field(default="", description="Subject identifier.")
    records: list[FileConversionRecord] = Field(
        default_factory=list,
        description="Per-file conversion records.",
    )
