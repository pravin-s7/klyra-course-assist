"""Pydantic models for per-subject `docs/<subject>/skill.md` configuration."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, Field


class SubjectOutputFormats(BaseModel):
    """Per-subject formatting preferences declared in `skill.md` frontmatter."""

    # Question Paper formatting preferences
    qp_style: Literal["iitm_exam", "comprehension_grouped", "worksheet"] = "iitm_exam"
    option_label_style: Literal["(a)", "A.", "1."] = "(a)"
    include_marks_per_question: bool = True
    default_marks_by_type: dict[str, int] = Field(
        default_factory=lambda: {"MCQ": 1, "MSQ": 2, "NAT": 2, "SA": 4}
    )
    group_by_section: bool = False
    page_break_between_questions: bool = False
    required_question_blocks: list[str] = Field(default_factory=list)

    # Answer Key formatting preferences
    answer_key_style: Literal[
        "step_by_step", "execution_trace", "concise_with_rationale"
    ] = "step_by_step"

    # Coding Problem formatting preferences
    coding_readme_sections: list[str] = Field(
        default_factory=lambda: [
            "Problem Statement",
            "Input Format",
            "Output Format",
            "Constraints",
            "Sample Test Cases",
        ]
    )

    # Teaching Material formatting preferences
    teaching_sections: list[str] = Field(
        default_factory=lambda: [
            "Learning Objectives",
            "Concept Breakdown",
            "Worked Examples",
            "Common Pitfalls",
            "Practice Questions",
        ]
    )


class SubjectSkillConfig(BaseModel):
    """Full parsed representation of `docs/<subject>/skill.md`."""

    subject_id: str
    display_name: str
    domain: str = "general"
    allowed_question_types: list[str] = Field(
        default_factory=lambda: ["MCQ", "MSQ", "NAT"]
    )
    default_type_counts: str = "MCQ:5,MSQ:3,NAT:2"
    coding_languages: list[str] = Field(default_factory=lambda: ["python"])
    default_coding_language: str = "python"
    diagram_style: str = "flowchart"
    strict_syllabus_grounding: bool = True
    output_formats: SubjectOutputFormats = Field(default_factory=SubjectOutputFormats)

    # Markdown section bodies parsed from `docs/<subject>/skill.md`
    domain_pedagogical_rules: str = ""
    diagram_visual_conventions: str = ""
    qp_output_format_rules: str = ""
    answer_key_output_format_rules: str = ""
    coding_output_format_rules: str = ""
    teaching_output_format_rules: str = ""
    raw_markdown: str = ""

    @property
    def domain_rules(self) -> str:
        return self.domain_pedagogical_rules

    @property
    def domain_rules_markdown(self) -> str:
        return self.domain_pedagogical_rules

    @property
    def diagram_conventions_markdown(self) -> str:
        return self.diagram_visual_conventions

    @property
    def qp_format_instructions(self) -> str:
        return self.qp_output_format_rules

    @property
    def qp_format_markdown(self) -> str:
        return self.qp_output_format_rules

    @property
    def answer_key_format_markdown(self) -> str:
        return self.answer_key_output_format_rules

    @property
    def coding_format_markdown(self) -> str:
        return self.coding_output_format_rules

    @property
    def teaching_format_markdown(self) -> str:
        return self.teaching_output_format_rules
