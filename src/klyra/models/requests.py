"""Request bodies for every write endpoint.

`subject` is deliberately a path parameter (not a body field) on every route
- see api/dependencies.py and utils/fs.validate_subject for why it's
validated at the boundary before touching the filesystem.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class IngestRequest(BaseModel):
    week: int | None = Field(default=None, ge=1, le=12, description="Restrict ingestion to one week (1-12)")
    force: bool = Field(default=False, description="Re-parse files even if unchanged since last ingest")


class GenerateRequest(BaseModel):
    qp_name: str = Field(..., min_length=1, description='Human name for this QP, e.g. "midterm"')
    scope: str = Field(..., description='Which weeks: "all", "week_3", "week_1-week_4", "1-4", or "1,3,5"')
    difficulty: str = Field(..., description='e.g. "medium" or "40%% easy, 60%% hard"')
    source: str | None = Field(
        default=None, description='Restrict to one document, e.g. "Lecture 1.5" (fuzzy-matched by the agent)'
    )
    pyq_similar: int = Field(
        default=0, ge=0, description="How many questions should be PYQ-similar (never a verbatim copy)"
    )
    concepts: str | None = Field(default=None, description='Optional specific topics, e.g. "arrays,linked lists"')
    type_counts: str | None = Field(
        default=None, description='Precise per-type breakdown, e.g. "MCQ:5,MSQ:3,NAT:2" (preferred)'
    )
    types: str | None = Field(default=None, description="Comma-separated types if not using type_counts")
    num_questions: int | None = Field(default=None, ge=1, description="Total, if not using type_counts")
    notes: str | None = Field(
        default=None, description='Free-form guidance, e.g. "make B-tree questions hard, keep normalization easy"'
    )


class ReviseRequest(BaseModel):
    qp_name: str = Field(..., min_length=1)
    notes: str | None = Field(
        default=None,
        description='Specific manual edits, e.g. "reformat Q1 as a table; replace Q3 with a question about hashing"',
    )


class ConvertRequest(BaseModel):
    request: str = Field(..., min_length=1, description='e.g. "convert midterm-qp.md and midterm-answer-key.md to LaTeX"')


class TeachRequest(BaseModel):
    session_name: str = Field(..., min_length=1)
    scope: str = Field(..., description="Which week(s) - same syntax as GenerateRequest.scope")
    concepts: str | None = Field(default=None, description="Optional specific concepts to focus on")
    notes: str | None = Field(
        default=None,
        description='What you want covered/emphasized, duration, format, etc., '
        'e.g. "45 min, focus on intuition before formalism"',
    )


class TranscriptRequest(BaseModel):
    url: str = Field(..., min_length=1, description="YouTube URL or video ID")
    week: int | None = Field(default=None, ge=1, le=12)
    title: str | None = None


class ReviseTeachRequest(BaseModel):
    notes: str = Field(
        ...,
        min_length=1,
        description="Required (unlike QP/coding revision): teaching material has no automated "
        "review to fall back on, so without instructions there is nothing to revise.",
    )


class CodeRequest(BaseModel):
    name: str = Field(..., min_length=1, description='Name for this problem set, e.g. "lab-3"')
    language: str = Field(..., description="One of: python, java, c, cpp, bash, sql, postgresql, python-db")
    scope: str = Field(..., description='Which weeks: "all", "week_3", "week_1-week_4", "1-4", or "1,3,5"')
    difficulty: str = Field(..., description='e.g. "medium" or "40%% easy, 60%% hard"')
    num_questions: int = Field(..., ge=1, description="Total number of coding problems")
    public_tests: int = Field(..., ge=0, description="Public test cases PER problem")
    private_tests: int = Field(..., ge=0, description="Private (hidden) test cases PER problem")
    source: str | None = Field(default=None, description='Restrict to one document, e.g. "Lab 3 handout"')
    pyq_similar: int = Field(
        default=0, ge=0, description="How many problems should be PYQ-similar (never a verbatim copy)"
    )
    leetcode_similar: bool = Field(
        default=False,
        description="If true, look up related LeetCode problems (by concepts/subject/notes) "
        "and write original LeetCode-style problems inspired by them - never copies",
    )
    include_reference: bool = Field(
        default=False,
        description="If true, also write a reference solution into the student-facing problems file. "
        "Off by default - these papers test coding concepts.",
    )
    concepts: str | None = Field(default=None, description='Optional specific topics, e.g. "recursion,file I/O"')
    notes: str | None = Field(
        default=None, description='Free-form guidance, e.g. "focus on recursion, make the linked-list one hard"'
    )


class ReviseCodeRequest(BaseModel):
    language: str = Field(..., description="One of: python, java, c, cpp, bash, sql, postgresql, python-db")
    notes: str | None = Field(
        default=None,
        description='Specific manual edits, e.g. "problem 2\'s constraints are unclear about negative numbers"',
    )


class SkillUpdateRequest(BaseModel):
    markdown: str = Field(
        ...,
        min_length=10,
        description="Complete skill.md content with YAML frontmatter and markdown sections.",
    )
