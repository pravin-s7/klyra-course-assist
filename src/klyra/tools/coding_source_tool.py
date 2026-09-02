"""Find actual coding-problem source files for `klyra code`.

Coding generation does not require the syllabus index. If lab/assignment/
problem-set files exist under the subject's week folders (any supported
format), those are the primary grounding - not lecture theory. Concepts
still live under the same week_N folders.
"""
from __future__ import annotations

from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.utils import fs

CODING_NAME_HINTS = (
    "lab",
    "assignment",
    "problem",
    "exercise",
    "homework",
    "coding",
    "practice",
    "worksheet",
    "pyq",
    "program",
    "question",
)

_EXCERPT_CHARS = 1600


def is_coding_problem_file(path: Path) -> bool:
    """True for source files that look like problems/labs, not theory slides."""
    if path.suffix.lower() in fs.CODE_SUFFIXES:
        return True
    name = path.stem.lower()
    return any(hint in name for hint in CODING_NAME_HINTS)


def _read_excerpt(path: Path) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix in fs.PLAIN_TEXT_SUFFIXES or suffix in fs.CODE_SUFFIXES:
            text = path.read_text(errors="replace")
        elif suffix == fs.PDF_SUFFIX:
            text = _pdf_text(path)
        else:
            return (
                f"(binary {suffix} file - filename suggests a coding problem; "
                "ingest it if you need the full extracted text)"
            )
    except OSError as exc:
        return f"(could not read: {exc})"
    text = " ".join(text.split())
    if not text:
        return "(file is empty or has no extractable text)"
    if len(text) > _EXCERPT_CHARS:
        return text[:_EXCERPT_CHARS] + "…"
    return text


def _pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages[:8]:
        pages.append(page.extract_text() or "")
    return "\n".join(pages)


class SearchCodingSourcesInput(BaseModel):
    subject: str
    week: int | None = Field(
        default=None,
        description="Restrict to one week (1-12). Omit to scan every week plus pyq/additional.",
    )
    query: str | None = Field(
        default=None,
        description="Optional keywords (concepts, notes) to rank/filter filenames and excerpts",
    )


class SearchCodingSourcesTool(BaseTool):
    name: str = "search_coding_sources"
    description: str = (
        "Find coding-problem source files (labs, assignments, problem sets, "
        "code files, coding PYQs) under docs/<subject>/ for the requested week "
        "scope. Reads md/pdf/code directly from disk - does NOT require ingest "
        "or the syllabus index. Prefer these files over lecture theory when "
        "writing coding problems. If none exist, say so and write original "
        "problems from --concepts / --notes / search_leetcode / search_pyq."
    )
    args_schema: Type[BaseModel] = SearchCodingSourcesInput

    def _run(self, subject: str, week: int | None = None, query: str | None = None) -> str:
        try:
            files = fs.discover_subject_files(subject, week=week)
        except FileNotFoundError as exc:
            return (
                f"{exc} Coding generation does not require indexed files - "
                "write original problems from the requested concepts, notes, "
                "search_leetcode, and/or search_pyq."
            )

        if week is not None:
            pyq = fs.pyq_dir(subject)
            if pyq.exists():
                files = list(files) + [
                    fs.SourceFile(p, subject, None, "pyq")
                    for p in sorted(pyq.rglob("*"))
                    if fs.is_supported_file(p)
                ]

        coding = [f for f in files if is_coding_problem_file(f.path)]
        theory = [f for f in files if not is_coding_problem_file(f.path)]
        query_l = (query or "").strip().lower()

        def _score(item: fs.SourceFile) -> int:
            if not query_l:
                return 0
            blob = f"{item.path.name} {_read_excerpt(item.path)}".lower()
            return sum(1 for token in query_l.replace(",", " ").split() if token and token in blob)

        coding.sort(key=_score, reverse=True)

        if not coding:
            theory_note = ""
            if theory:
                listed = ", ".join(f"{t.path.name} (week {t.week or t.category})" for t in theory[:8])
                theory_note = (
                    f" Theory/lecture files exist but should not be the primary "
                    f"grounding for coding problems: {listed}."
                )
            return (
                "No coding-problem files (labs/assignments/code/problem sets) "
                f"found for {subject}"
                + (f" week {week}" if week is not None else "")
                + ". That is fine - coding generation does not require indexed "
                "syllabus files. Write original problems from --concepts, "
                "--notes, search_leetcode, and/or search_pyq."
                + theory_note
            )

        blocks = [
            "CODING PROBLEM FILES (prefer these over lecture theory):",
        ]
        for item in coding[:12]:
            loc = f"week {item.week}" if item.week is not None else item.category
            blocks.append(
                f"--- {item.path.as_posix()} ({loc}) ---\n{_read_excerpt(item.path)}"
            )
        if theory:
            blocks.append(
                "THEORY/LECTURE FILES (same weeks, lower priority for coding "
                "problems; use only for concept names if needed): "
                + ", ".join(f"{t.path.name} ({t.week or t.category})" for t in theory[:10])
            )
        return "\n\n".join(blocks)
