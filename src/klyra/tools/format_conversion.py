"""Deterministic Markdown <-> LaTeX conversion via pandoc.

Same principle as latex_diagram_tool.py: a reliable, deterministic tool
does the actual conversion (math, tables, and images survive exactly)
rather than having an LLM hand-retype technical content, which risks subtle
errors (a mis-copied functional dependency, a dropped table row, ...).
Verified empirically: real generated QP markdown (with inline LaTeX math and
embedded images) converts to a standalone .tex file that compiles cleanly
with pdflatex.

IMPORTANT: pandoc's default `latex` reader *silently drops* any LaTeX
environment it doesn't understand (e.g. a raw `tikzpicture`) rather than
preserving it - confirmed empirically, and confirmed to cause a real
failure: the format-converter agent saw a gap where a diagram should be and
fabricated an unrelated one to fill it, rather than reporting real content
had been lost. The `raw_tex` extension (both reader and writer) makes pandoc
preserve untranslatable LaTeX as a literal block in the markdown output
instead of discarding it - always enabled below for exactly this reason.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

from klyra.utils import fs

_RAW_ENV_RE = re.compile(r"\\begin\{(\w+)\}.*?\\end\{\1\}", re.DOTALL)


class PandocNotAvailableError(RuntimeError):
    """Raised when pandoc is not on PATH."""


def _pandoc_path() -> str | None:
    return shutil.which("pandoc")


def find_raw_latex_blocks(markdown_text: str) -> list[str]:
    """Finds LaTeX environments pandoc couldn't translate and preserved
    verbatim (via the raw_tex extension) - e.g. a raw tikzpicture block that
    needs generate_diagram to become a real image instead of dead source
    code sitting in the markdown."""
    return [m.group(0) for m in _RAW_ENV_RE.finditer(markdown_text)]


def _run_pandoc(args: list[str], resource_dir: Path) -> None:
    pandoc = _pandoc_path()
    if not pandoc:
        raise PandocNotAvailableError("pandoc not found on PATH - install it to enable format conversion.")
    result = subprocess.run(
        [pandoc, f"--resource-path={resource_dir}", *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed:\n{result.stderr[-2000:]}")


def convert_markdown_to_latex(md_path: Path, output_path: Path, title: str | None = None, template_path: Path | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f", "markdown+raw_tex",
        "-t", "latex",
        "--standalone",
        str(md_path),
        "-o", str(output_path),
    ]
    if title:
        args.extend(["-V", f"title={title}"])
    if template_path and template_path.exists():
        args.extend(["--template", str(template_path)])
    _run_pandoc(args, resource_dir=md_path.parent)


def convert_latex_to_markdown(tex_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    args = [
        "-f", "latex+raw_tex",
        "-t", "markdown+raw_tex",
        str(tex_path),
        "-o", str(output_path),
    ]
    _run_pandoc(args, resource_dir=tex_path.parent)


class ConvertMarkdownToLatexInput(BaseModel):
    subject: str
    source_filename: str = Field(
        ..., description="Markdown filename inside docs/<subject>/generated/, e.g. 'quiz-1-qp.md'"
    )
    title: str | None = Field(default=None, description="Optional document title for the LaTeX output")
    template_name: str | None = Field(
        default=None, 
        description="Optional filename of a LaTeX template (e.g., 'overleaf.tex') located in docs/<subject>/."
    )


class ConvertMarkdownToLatexTool(BaseTool):
    name: str = "convert_markdown_to_latex"
    description: str = (
        "Convert a generated markdown file (QP, answer key, etc.) into a "
        "standalone, Overleaf-ready .tex file via pandoc. Deterministic - "
        "math/tables/images are preserved exactly, never hand-retyped. "
        "Output is saved next to the source with a .tex extension. "
        "If a template_name is provided, it uses that specific LaTeX format."
    )
    args_schema: Type[BaseModel] = ConvertMarkdownToLatexInput

    def _run(self, subject: str, source_filename: str, title: str | None = None, template_name: str | None = None) -> str:
        md_path = fs.generated_dir(subject) / source_filename
        if not md_path.exists():
            return f"No such file: {md_path}"

        # Whenever converting a Question Paper (-qp.md / -qp-revised.md) or its paired
        # Answer Key (-answer-key.md / -answer-key-revised.md), always produce a single
        # unified Overleaf 'exam' class .tex file combining BOTH questions and solutions
        # in the exact sample_latex_format.tex structure.
        if _is_qp_or_answer_key_filename(source_filename):
            try:
                unified_tex_path = render_combined_exam_latex(
                    subject=subject,
                    source_filename=source_filename,
                    title_override=title,
                )
                return (
                    f"Wrote unified Overleaf exam LaTeX (questions + solutions in same file): "
                    f"{unified_tex_path}"
                )
            except Exception as exc:  # noqa: BLE001
                return f"Conversion failed: {exc}"
            
        template_path = None
        if template_name:
            template_path = fs.subject_dir(subject) / template_name
            if not template_path.exists():
                return f"Template file not found: {template_path}"
                
        output_path = md_path.with_suffix(".tex")
        try:
            convert_markdown_to_latex(md_path, output_path, title=title, template_path=template_path)
        except PandocNotAvailableError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            return f"Conversion failed: {exc}"
        return f"Wrote {output_path}"


class ConvertLatexToMarkdownInput(BaseModel):
    subject: str
    source_filename: str = Field(
        ..., description="LaTeX filename inside docs/<subject>/generated/, e.g. 'exam.tex'"
    )


class ConvertLatexToMarkdownTool(BaseTool):
    name: str = "convert_latex_to_markdown"
    description: str = (
        "Convert a LaTeX (.tex) file into markdown via pandoc. Deterministic "
        "- content is preserved exactly, never hand-retyped. Output is saved "
        "next to the source with a .md extension."
    )
    args_schema: Type[BaseModel] = ConvertLatexToMarkdownInput

    def _run(self, subject: str, source_filename: str) -> str:
        tex_path = fs.generated_dir(subject) / source_filename
        if not tex_path.exists():
            return f"No such file: {tex_path}"
        output_path = tex_path.with_suffix(".md")
        try:
            convert_latex_to_markdown(tex_path, output_path)
        except PandocNotAvailableError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            return f"Conversion failed: {exc}"

        raw_blocks = find_raw_latex_blocks(output_path.read_text())
        if not raw_blocks:
            return f"Wrote {output_path} (no raw/untranslatable LaTeX found)."

        blocks_text = "\n\n---\n\n".join(raw_blocks)
        return (
            f"Wrote {output_path}, BUT pandoc could not translate "
            f"{len(raw_blocks)} raw LaTeX block(s) - they were preserved "
            f"verbatim in the output rather than silently dropped. You MUST "
            f"handle each one: call generate_diagram with the EXACT block "
            f"below as latex_body (plus a complete ground_truth), then call "
            f"replace_in_generated_file with old_text set to that exact "
            f"block and new_text set to whatever generate_diagram returned, "
            f"on file '{output_path.name}'. Never substitute a different "
            f"diagram - use this exact code:\n\n{blocks_text}"
        )


class ReplaceInGeneratedFileInput(BaseModel):
    subject: str
    filename: str = Field(..., description="File inside docs/<subject>/generated/ to edit")
    old_text: str = Field(..., description="Exact text to find and replace (e.g. a raw LaTeX block)")
    new_text: str = Field(
        ..., description="Replacement text (e.g. the markdown image syntax generate_diagram returned)"
    )


class ReplaceInGeneratedFileTool(BaseTool):
    name: str = "replace_in_generated_file"
    description: str = (
        "Replace one exact occurrence of old_text with new_text in a file "
        "under docs/<subject>/generated/ - e.g. to swap a raw LaTeX block "
        "(reported by convert_latex_to_markdown) for the markdown image "
        "reference generate_diagram returned for it. Fails clearly if "
        "old_text isn't found verbatim rather than silently doing nothing."
    )
    args_schema: Type[BaseModel] = ReplaceInGeneratedFileInput

    def _run(self, subject: str, filename: str, old_text: str, new_text: str) -> str:
        path = fs.generated_dir(subject) / filename
        if not path.exists():
            return f"No such file: {path}"
        content = path.read_text()
        if old_text not in content:
            return "old_text not found verbatim in the file - re-check it matches exactly and retry."
        path.write_text(content.replace(old_text, new_text, 1))
        return f"Updated {path}"


def _is_qp_or_answer_key_filename(filename: str) -> bool:
    name = Path(filename).name.lower()
    return any(
        name.endswith(suffix)
        for suffix in (
            "-qp.md",
            "-qp-revised.md",
            "-qp.tex",
            "-qp-revised.tex",
            "-answer-key.md",
            "-answer-key-revised.md",
            "-answer-key.tex",
            "-answer-key-revised.tex",
        )
    )


def _resolve_qp_prefix_and_revision(filename: str) -> tuple[str, bool]:
    """Return (base_prefix, is_revised) for any QP or Answer Key filename."""
    stem = Path(filename).stem
    is_revised = False
    if stem.endswith("-revised"):
        is_revised = True
        stem = stem[: -len("-revised")]
    for suffix in ("-qp", "-answer-key", "-discrepancy-report", "-final-summary"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return stem, is_revised


def _escape_latex_prose(text: str) -> str:
    """Escape LaTeX special characters in plain prose while preserving math & LaTeX commands."""
    if not text:
        return ""
    # Split out inline/display math ($...$, $$...$$, \(...\), \[...\]) so we don't mangle math
    math_pattern = re.compile(
        r"(\$\$.*?\$\$|\$[^$\n]+\$|\\\(.*?\\\)|\\\[.*?\\\])",
        re.DOTALL,
    )
    parts = math_pattern.split(text)
    out: list[str] = []
    for idx, part in enumerate(parts):
        if idx % 2 == 1:
            # Inside math mode: keep as-is
            out.append(part)
            continue
        s = part
        # Protect inline code `...` first
        code_spans: list[str] = []

        def _save_code(m: re.Match) -> str:
            inner = m.group(1)
            escaped_inner = (
                inner.replace("\\", r"\textbackslash{}")
                .replace("{", r"\{")
                .replace("}", r"\}")
                .replace("_", r"\_")
                .replace("%", r"\%")
                .replace("&", r"\&")
                .replace("#", r"\#")
                .replace("$", r"\$")
                .replace("^", r"\textasciicircum{}")
                .replace("~", r"\textasciitilde{}")
            )
            code_spans.append(rf"\texttt{{{escaped_inner}}}")
            return f"@@CODESPAN{len(code_spans) - 1}@@"

        s = re.sub(r"`([^`\n]+)`", _save_code, s)

        # Convert bold and italic before escaping braces
        bold_spans: list[str] = []

        def _save_bold(m: re.Match) -> str:
            bold_spans.append(m.group(1))
            return f"@@BOLDSPAN{len(bold_spans) - 1}@@"

        s = re.sub(r"\*\*([^\*\n]+)\*\*", _save_bold, s)

        italic_spans: list[str] = []

        def _save_italic(m: re.Match) -> str:
            italic_spans.append(m.group(1))
            return f"@@ITALICSPAN{len(italic_spans) - 1}@@"

        s = re.sub(r"(?<!\*)\*([^\*\n]+)\*(?!\*)", _save_italic, s)

        def _escape_plain(chunk: str) -> str:
            # Replace common relational/arrow operators in prose
            chunk = chunk.replace("\\", r"\textbackslash{}")
            chunk = chunk.replace("{", r"\{").replace("}", r"\}")
            chunk = chunk.replace("&", r"\&")
            chunk = chunk.replace("%", r"\%")
            chunk = chunk.replace("#", r"\#")
            chunk = chunk.replace("_", r"\_")
            chunk = chunk.replace("~", r"\textasciitilde{}")
            chunk = chunk.replace("^", r"\textasciicircum{}")
            chunk = re.sub(r"\s*->\s*", r" $\\rightarrow$ ", chunk)
            chunk = re.sub(r"\s*<=\s*", r" $\\le$ ", chunk)
            chunk = re.sub(r"\s*>=\s*", r" $\\ge$ ", chunk)
            chunk = re.sub(r"\s*!=\s*", r" $\\neq$ ", chunk)
            return chunk

        s = _escape_plain(s)

        for i_b, b_txt in enumerate(bold_spans):
            s = s.replace(f"@@BOLDSPAN{i_b}@@", rf"\textbf{{{_escape_plain(b_txt)}}}")
        for i_it, it_txt in enumerate(italic_spans):
            s = s.replace(f"@@ITALICSPAN{i_it}@@", rf"\textit{{{_escape_plain(it_txt)}}}")
        for i_c, c_tex in enumerate(code_spans):
            s = s.replace(f"@@CODESPAN{i_c}@@", c_tex)

        out.append(s)
    return "".join(out)


def _convert_markdown_table_to_latex(table_lines: list[str]) -> str:
    """Convert a Markdown pipe table into a centered LaTeX tabular with \\hline borders."""
    rows: list[list[str]] = []
    for line in table_lines:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        # Skip separator line like |---|---|
        if all(re.fullmatch(r"[:\-\s]+", c or "-") for c in cells):
            continue
        rows.append(cells)

    if not rows:
        return ""
    num_cols = max(len(r) for r in rows)
    col_spec = "|" + "|".join(["c"] * num_cols) + "|"
    tex_lines = [
        r"\begin{center}",
        rf"\begin{{tabular}}{{{col_spec}}}",
        r"\hline",
    ]
    for r_idx, row in enumerate(rows):
        padded = row + [""] * (num_cols - len(row))
        if r_idx == 0:
            formatted_cells = [
                rf"\textbf{{{_escape_latex_prose(c.replace('**', '').strip())}}}"
                for c in padded
            ]
            tex_lines.append(" & ".join(formatted_cells) + r" \\")
            tex_lines.append(r"\hline")
        else:
            formatted_cells = [_escape_latex_prose(c) for c in padded]
            tex_lines.append(" & ".join(formatted_cells) + r" \\")
    tex_lines.append(r"\hline")
    tex_lines.append(r"\end{tabular}")
    tex_lines.append(r"\end{center}")
    return "\n".join(tex_lines)


def _markdown_block_to_exam_latex(md_text: str) -> str:
    """Convert a multi-line Markdown block (with code blocks, tables, images, math) to Exam LaTeX."""
    if not md_text or not md_text.strip():
        return ""

    # 1. Extract fenced code blocks ```...``` -> \begin{verbatim}...\end{verbatim}
    verbatim_blocks: list[str] = []

    def _replace_code_block(m: re.Match) -> str:
        code_body = m.group(1).strip("\n")
        verbatim_blocks.append(f"\\begin{{verbatim}}\n{code_body}\n\\end{{verbatim}}")
        return f"\n@@VERBATIMBLOCK{len(verbatim_blocks) - 1}@@\n"

    text = re.sub(r"```[^\n]*\n(.*?)```", _replace_code_block, md_text, flags=re.DOTALL)

    # 2. Extract raw LaTeX environments (e.g. \begin{tikzpicture}...\end{tikzpicture})
    raw_tex_blocks: list[str] = []

    def _replace_raw_tex(m: re.Match) -> str:
        raw_tex_blocks.append(m.group(0))
        return f"\n@@RAWTEXBLOCK{len(raw_tex_blocks) - 1}@@\n"

    text = _RAW_ENV_RE.sub(_replace_raw_tex, text)

    # 3. Extract Markdown images ![alt](path) -> \begin{center}\includegraphics...\end{center}
    image_blocks: list[str] = []

    def _replace_image(m: re.Match) -> str:
        img_path = m.group(2).strip()
        # Normalize web path '/files/<subject>/.assets/...' back to relative '../.assets/...' for LaTeX
        img_path = re.sub(r"^/files/[^/]+/\.?assets/", "../.assets/", img_path)
        image_blocks.append(
            "\\begin{center}\n"
            f"\\includegraphics[width=0.65\\textwidth]{{{img_path}}}\n"
            "\\end{center}"
        )
        return f"\n@@IMAGEBLOCK{len(image_blocks) - 1}@@\n"

    text = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", _replace_image, text)

    # 4. Process line-by-line for Markdown pipe tables and prose paragraphs
    lines = text.splitlines()
    out_lines: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Check for start of Markdown pipe table
        if (
            stripped.startswith("|")
            and stripped.endswith("|")
            and i + 1 < len(lines)
            and re.match(r"^\s*\|[\s:\-\|]+\|\s*$", lines[i + 1])
        ):
            table_buf: list[str] = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_buf.append(lines[i])
                i += 1
            out_lines.append(_convert_markdown_table_to_latex(table_buf))
            continue

        if re.fullmatch(r"@@(VERBATIMBLOCK|RAWTEXBLOCK|IMAGEBLOCK)\d+@@", stripped):
            out_lines.append(stripped)
        else:
            out_lines.append(_escape_latex_prose(line))
        i += 1

    result = "\n".join(out_lines)
    for idx_v, v_block in enumerate(verbatim_blocks):
        result = result.replace(f"@@VERBATIMBLOCK{idx_v}@@", v_block)
    for idx_r, r_block in enumerate(raw_tex_blocks):
        result = result.replace(f"@@RAWTEXBLOCK{idx_r}@@", r_block)
    for idx_im, im_block in enumerate(image_blocks):
        result = result.replace(f"@@IMAGEBLOCK{idx_im}@@", im_block)

    return result.strip()


def _extract_correct_option_letters(correct_answer: str, num_options: int = 4) -> list[str]:
    """Extract normalized uppercase option letters (e.g. ['B'] or ['A', 'C']) from correct_answer."""
    if not correct_answer:
        return []
    valid_letters = {chr(ord("A") + i) for i in range(max(num_options, 6))}
    found: list[str] = []
    # Match (A), (b), Option A, or standalone capital/lowercase letters separated by commas/spaces
    for m in re.finditer(
        r"\(([a-zA-Z])\)|\bOption(?:s)?\s+([a-zA-Z])\b|\b([A-Fa-f])\b",
        correct_answer,
    ):
        letter = (m.group(1) or m.group(2) or m.group(3) or "").upper()
        if letter in valid_letters and letter not in found:
            found.append(letter)
    return found


def _format_answer_line(qtype: str, correct_letters: list[str], raw_correct_answer: str) -> str:
    """Format the \\textbf{Answer:} line matching sample_latex_format.tex."""
    qtype_u = qtype.upper()
    if qtype_u in {"MCQ", "MSQ"} and correct_letters:
        if len(correct_letters) == 1:
            return rf"\textbf{{Answer:}} Option {correct_letters[0]}"
        if len(correct_letters) == 2:
            return rf"\textbf{{Answer:}} Options {correct_letters[0]} and {correct_letters[1]}"
        joined = ", ".join(correct_letters[:-1]) + f" and {correct_letters[-1]}"
        return rf"\textbf{{Answer:}} Options {joined}"
    cleaned_ans = _escape_latex_prose(raw_correct_answer.strip())
    return rf"\textbf{{Answer: {cleaned_ans}}}"


def _format_solution_environment(
    qtype: str,
    options: list[str],
    correct_letters: list[str],
    derivation: str,
    distractor_analysis: dict[str, str],
) -> str:
    """Build the \\begin{solution}\\begin{itemize}...\\end{itemize}\\end{solution} block."""
    items: list[str] = []

    # 1. Include worked derivation steps if present
    if derivation and derivation.strip():
        deriv_latex = _markdown_block_to_exam_latex(derivation.strip())
        # Split derivation into logical bullet paragraphs if it isn't just a short duplicate
        deriv_paras = [p.strip() for p in re.split(r"\n\s*\n", deriv_latex) if p.strip()]
        for p_idx, para in enumerate(deriv_paras):
            # Clean leading markdown bullet '- ' if present
            para_clean = re.sub(r"^(?:-\s+|\*\s+)", "", para).strip()
            if p_idx == 0:
                items.append(rf"    \item \textbf{{Step-by-Step Analysis:}} {para_clean}")
            else:
                items.append(rf"    \item {para_clean}")

    # 2. Include per-option breakdown matching sample_latex_format.tex:
    #    \item \textbf{Option A (Incorrect):} ...
    #    \item \textbf{Option B (Correct):} ...
    if qtype.upper() in {"MCQ", "MSQ"} and options:
        norm_analysis: dict[str, str] = {}
        for k, v in (distractor_analysis or {}).items():
            m = re.search(r"([a-zA-Z])", str(k))
            if m:
                norm_analysis[m.group(1).upper()] = str(v).strip()

        for idx in range(len(options)):
            letter = chr(ord("A") + idx)
            is_corr = letter in correct_letters
            status_label = "Correct" if is_corr else "Incorrect"
            raw_reason = norm_analysis.get(letter, "")
            # Strip redundant leading "Correct." or "Incorrect." from reason
            raw_reason = re.sub(
                r"^(?:Correct|Incorrect)\s*[:.\-\s]*",
                "",
                raw_reason,
                flags=re.IGNORECASE,
            ).strip()
            if not raw_reason:
                raw_reason = (
                    "This option satisfies all conditions derived above."
                    if is_corr
                    else "This option does not match the derived result."
                )
            reason_latex = _markdown_block_to_exam_latex(raw_reason)
            items.append(
                rf"    \item \textbf{{Option {letter} ({status_label}):}} {reason_latex}"
            )

    if not items:
        items.append(r"    \item Verified per course syllabus and answer key.")

    return (
        "\\begin{solution}\n"
        "\\begin{itemize}\n"
        + "\n".join(items)
        + "\n\\end{itemize}\n"
        "\\end{solution}"
    )


def _parse_qp_and_answers_from_markdown(
    qp_md_path: Path, ans_md_path: Path | None
) -> tuple[dict, dict[int, dict]]:
    """Fallback parser when JSON sidecars are absent: parse questions from -qp.md and solutions from -answer-key.md."""
    qp_text = qp_md_path.read_text(encoding="utf-8")
    title_m = re.search(r"^#\s+(.+)$", qp_text, re.MULTILINE)
    title = title_m.group(1).strip() if title_m else qp_md_path.stem
    marks_m = re.search(r"\*\*Total Marks:\*\*\s*(\d+)", qp_text)
    total_marks = int(marks_m.group(1)) if marks_m else 0

    q_header_re = re.compile(
        r"^###\s+Question\s+(\d+)\s*(?:\(([^)]*)\))?\s*\[([A-Za-z]+)\](?:\s*\[Marks:\s*(\d+)\])?",
        re.MULTILINE,
    )
    matches = list(q_header_re.finditer(qp_text))
    questions: list[dict] = []
    for idx, m in enumerate(matches):
        qnum = int(m.group(1))
        qcode = (m.group(2) or "").strip()
        qtype = (m.group(3) or "MCQ").strip().upper()
        qmarks = int(m.group(4)) if m.group(4) else 1
        week_m = re.search(r"W(\d+)", qcode, re.IGNORECASE)
        week = int(week_m.group(1)) if week_m else None

        start = m.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(qp_text)
        block = qp_text[start:end].strip()
        block = re.sub(r"\n---\s*$", "", block).strip()

        # Extract options at the end of block
        opt_re = re.compile(r"^(?:\([a-fA-F]\)|[a-fA-F][.)])\s+(.+)$", re.MULTILINE)
        opt_matches = list(opt_re.finditer(block))
        options: list[str] = []
        stem_body = block
        if qtype in {"MCQ", "MSQ"} and opt_matches:
            first_opt_start = opt_matches[0].start()
            stem_body = block[:first_opt_start].strip()
            options = [om.group(1).strip() for om in opt_matches]

        questions.append(
            {
                "question_number": qnum,
                "question_code": qcode,
                "question_type": qtype,
                "marks": qmarks,
                "week": week,
                "topic": "",
                "comprehension_stem": None,
                "diagram_markdown": None,
                "stem": stem_body,
                "options": options,
            }
        )

    solutions_by_qnum: dict[int, dict] = {}
    if ans_md_path and ans_md_path.exists():
        ans_text = ans_md_path.read_text(encoding="utf-8")
        s_header_re = re.compile(
            r"^###\s+Question\s+(\d+)\s*(?:\[([A-Za-z]+)\])?",
            re.MULTILINE,
        )
        s_matches = list(s_header_re.finditer(ans_text))
        for idx, sm in enumerate(s_matches):
            qnum = int(sm.group(1))
            qtype = (sm.group(2) or "MCQ").strip().upper()
            start = sm.end()
            end = s_matches[idx + 1].start() if idx + 1 < len(s_matches) else len(ans_text)
            s_block = ans_text[start:end].strip()

            ca_m = re.search(r"-\s*\*\*Correct Answer:\*\*\s*(.+)", s_block)
            correct_answer = ca_m.group(1).strip() if ca_m else ""

            distractor_analysis: dict[str, str] = {}
            for dm in re.finditer(
                r"^\s+-\s*\*\*(\(?[a-fA-F]\)?)\*\*:\s*(.+)$",
                s_block,
                re.MULTILINE,
            ):
                distractor_analysis[dm.group(1).strip()] = dm.group(2).strip()

            # Extract derivation between Walkthrough/Derivation heading and Option/Distractor heading
            deriv_m = re.search(
                r"-\s*\*\*(?:Execution Trace|Conceptual Rationale|Step-by-Step)[^*]*\*\*\s*(.*?)(?=\n-\s*\*\*Option\s*/\s*Distractor|\Z)",
                s_block,
                re.DOTALL,
            )
            derivation = deriv_m.group(1).strip() if deriv_m else ""

            solutions_by_qnum[qnum] = {
                "question_number": qnum,
                "question_type": qtype,
                "correct_answer": correct_answer,
                "derivation": derivation,
                "distractor_analysis": distractor_analysis,
            }

    qp_dict = {
        "title": title,
        "total_marks": total_marks or sum(q["marks"] for q in questions),
        "questions": questions,
    }
    return qp_dict, solutions_by_qnum


def render_combined_exam_latex(
    subject: str,
    source_filename: str,
    title_override: str | None = None,
) -> Path:
    """Render a Question Paper and its paired Answer Key into a single unified
    Overleaf `exam` documentclass `.tex` file matching `sample_latex_format.tex`.
    """
    import json
    from klyra.services.skill_service import load_subject_skill
    from klyra.utils.format_renderer import strip_option_prefix

    gen_dir = fs.generated_dir(subject)
    prefix, is_revised = _resolve_qp_prefix_and_revision(source_filename)
    rev_suffix = "-revised" if is_revised else ""

    # Locate QP (.json and .md) and Answer Key (.json and .md)
    qp_json_path = gen_dir / f"{prefix}-qp{rev_suffix}.json"
    qp_md_path = gen_dir / f"{prefix}-qp{rev_suffix}.md"
    if not qp_json_path.exists() and not qp_md_path.exists():
        # Fallback between revised and unrevised if only one exists
        alt_suffix = "" if is_revised else "-revised"
        if (gen_dir / f"{prefix}-qp{alt_suffix}.json").exists() or (
            gen_dir / f"{prefix}-qp{alt_suffix}.md"
        ).exists():
            rev_suffix = alt_suffix
            qp_json_path = gen_dir / f"{prefix}-qp{rev_suffix}.json"
            qp_md_path = gen_dir / f"{prefix}-qp{rev_suffix}.md"

    ans_json_path = gen_dir / f"{prefix}-answer-key{rev_suffix}.json"
    ans_md_path = gen_dir / f"{prefix}-answer-key{rev_suffix}.md"
    if not ans_json_path.exists() and not ans_md_path.exists():
        alt_ans_suffix = "" if rev_suffix == "-revised" else "-revised"
        if (gen_dir / f"{prefix}-answer-key{alt_ans_suffix}.json").exists():
            ans_json_path = gen_dir / f"{prefix}-answer-key{alt_ans_suffix}.json"
        if (gen_dir / f"{prefix}-answer-key{alt_ans_suffix}.md").exists():
            ans_md_path = gen_dir / f"{prefix}-answer-key{alt_ans_suffix}.md"

    # Load QP data and Solutions data
    qp_data: dict = {}
    solutions_by_qnum: dict[int, dict] = {}

    if qp_json_path.exists():
        qp_data = json.loads(qp_json_path.read_text(encoding="utf-8"))
        # If qp_md_path also exists and has markdown image links added via replace_in_generated_file,
        # merge any diagram_markdown from qp_md_path
        if qp_md_path.exists():
            md_qp_data, _ = _parse_qp_and_answers_from_markdown(qp_md_path, None)
            md_by_qnum = {q["question_number"]: q for q in md_qp_data.get("questions", [])}
            for q in qp_data.get("questions", []):
                qnum = q.get("question_number")
                md_q = md_by_qnum.get(qnum)
                if md_q and "![" in (md_q.get("stem") or "") and not q.get("diagram_markdown"):
                    img_m = re.search(r"(!\[[^\]]*\]\([^)]+\))", md_q["stem"])
                    if img_m:
                        q["diagram_markdown"] = img_m.group(1)
    elif qp_md_path.exists():
        qp_data, solutions_by_qnum = _parse_qp_and_answers_from_markdown(qp_md_path, ans_md_path)
    else:
        raise FileNotFoundError(
            f"Could not find Question Paper for '{prefix}' in {gen_dir}"
        )

    if ans_json_path.exists():
        ans_data = json.loads(ans_json_path.read_text(encoding="utf-8"))
        for sol in ans_data.get("solutions", []):
            qnum = int(sol.get("question_number", 0))
            if qnum:
                solutions_by_qnum[qnum] = sol
    elif not solutions_by_qnum and ans_md_path.exists() and qp_md_path.exists():
        _, solutions_by_qnum = _parse_qp_and_answers_from_markdown(qp_md_path, ans_md_path)

    try:
        skill = load_subject_skill(subject)
        subject_display = skill.display_name
    except Exception:
        subject_display = subject.upper()

    questions = qp_data.get("questions", [])
    total_marks = qp_data.get("total_marks") or sum(
        int(q.get("marks") or 1) for q in questions
    )
    paper_title = (
        title_override
        or qp_data.get("title")
        or prefix.replace("-", " ").title()
    )

    preamble = (
        "\\documentclass[12pt, answers]{exam}\n"
        "\\IfFileExists{Templates/packages.tex}{\\input{Templates/packages}}{\\IfFileExists{Templates/packages}{\\input{Templates/packages}}{\\usepackage{amsmath,amssymb,graphicx,array,booktabs,xcolor}}}\n"
        "\\IfFileExists{tfrupee.sty}{\\usepackage{tfrupee}}{\\providecommand{\\rupee}{Rs.~}}\n"
        "\\usepackage{caption}\n"
        "\\usepackage{float}\n"
        "\\usepackage{tikz}\n"
        "\\usetikzlibrary{shapes.geometric, arrows.meta, calc, positioning} \n"
        "\\usepackage[normalem]{ulem}\n\n"
        "\\def\\ojoin{\\setbox0=\\hbox{$\\bowtie$}%\n"
        "  \\rule[-.02ex]{.25em}{.4pt}\\llap{\\rule[\\ht0]{.25em}{.4pt}}}\n"
        "\\def\\leftouterjoin{\\mathbin{\\ojoin\\mkern-5.8mu\\bowtie}}\n"
        "\\def\\rightouterjoin{\\mathbin{\\bowtie\\mkern-5.8mu\\ojoin}}\n"
        "\\def\\fullouterjoin{\\mathbin{\\ojoin\\mkern-5.8mu\\bowtie\\mkern-5.8mu\\ojoin}}\n\n"
        "\\begin{document}\n"
        "%Title starts here\n"
        "\\begin{center}\n"
        "\\fbox{\\fbox{\\parbox{5.5in}{\\centering\n"
        f"\\large {_escape_latex_prose(subject_display)} \\\\\n"
        f"{_escape_latex_prose(paper_title)}  \\\\\n"
        f"Total Marks: {total_marks}\n"
        " \\\\\n"
        "}}}\n"
        "\\end{center}\n"
        "\\CorrectChoiceEmphasis{\\color{black}}\n\n"
        "\\begin{questions}\n"
    )

    question_blocks: list[str] = []
    for q in questions:
        qnum = int(q.get("question_number", 0))
        qtype = str(q.get("question_type") or "MCQ").upper()
        marks = int(q.get("marks") or 1)
        week = q.get("week")
        if week is None and q.get("question_code"):
            wm = re.search(r"W(\d+)", str(q.get("question_code")), re.IGNORECASE)
            if wm:
                week = int(wm.group(1))

        point_word = "point" if marks == 1 else "points"
        if week is not None:
            hfill_tag = f"[Week {week}: {qtype}: {marks} {point_word}]"
        else:
            hfill_tag = f"[{qtype}: {marks} {point_word}]"

        # Combine comprehension_stem, diagram_markdown, and stem
        stem_segments: list[str] = []
        if q.get("comprehension_stem") and str(q["comprehension_stem"]).strip():
            stem_segments.append(str(q["comprehension_stem"]).strip())
        if q.get("diagram_markdown") and str(q["diagram_markdown"]).strip():
            stem_segments.append(str(q["diagram_markdown"]).strip())
        if q.get("stem") and str(q["stem"]).strip():
            stem_segments.append(str(q["stem"]).strip())

        full_stem_md = "\n\n".join(stem_segments).strip()
        # Split first paragraph vs rest so \hfill [...] sits cleanly on the \question line
        stem_paras = re.split(r"\n\s*\n", full_stem_md, maxsplit=1)
        first_para_latex = _markdown_block_to_exam_latex(stem_paras[0]) if stem_paras else ""
        rest_stem_latex = (
            _markdown_block_to_exam_latex(stem_paras[1])
            if len(stem_paras) > 1 and stem_paras[1].strip()
            else ""
        )

        q_lines: list[str] = [
            "%***************************************************",
            f"\\question {first_para_latex} \\hfill {hfill_tag}",
        ]
        if rest_stem_latex:
            q_lines.append("")
            q_lines.append(rest_stem_latex)

        raw_options = [strip_option_prefix(str(opt)) for opt in (q.get("options") or [])]
        sol = solutions_by_qnum.get(qnum, {})
        raw_correct = str(sol.get("correct_answer") or "").strip()
        correct_letters = _extract_correct_option_letters(raw_correct, len(raw_options))

        if qtype in {"MCQ", "MSQ"} and raw_options:
            q_lines.append("")
            q_lines.append("\\begin{checkboxes}")
            for idx, opt_text in enumerate(raw_options):
                letter = chr(ord("A") + idx)
                opt_latex = _markdown_block_to_exam_latex(opt_text)
                choice_cmd = "\\CorrectChoice" if letter in correct_letters else "\\choice"
                q_lines.append(f"    {choice_cmd} {opt_latex}")
            q_lines.append("\\end{checkboxes}")
        else:
            # NAT / short answer question
            if "\\underline" not in first_para_latex and "\\underline" not in rest_stem_latex:
                q_lines.append("")
                q_lines.append("Answer: \\underline{\\hspace{2cm}}")

        q_lines.append("")
        q_lines.append(_format_answer_line(qtype, correct_letters, raw_correct or "N/A"))
        q_lines.append("")
        q_lines.append(
            _format_solution_environment(
                qtype=qtype,
                options=raw_options,
                correct_letters=correct_letters,
                derivation=str(sol.get("derivation") or ""),
                distractor_analysis=sol.get("distractor_analysis") or {},
            )
        )
        q_lines.append("")
        q_lines.append("\\newpage")
        question_blocks.append("\n".join(q_lines))

    postamble = (
        "\n%********************************************************\n"
        "\\end{questions}\n"
        "\\end{document}\n"
    )

    final_latex = preamble + "\n" + "\n\n".join(question_blocks) + postamble
    output_tex_path = gen_dir / f"{prefix}-qp{rev_suffix}.tex"
    output_tex_path.write_text(final_latex, encoding="utf-8")

    # Remove any separate/disconnected -answer-key.tex so questions & solutions always live in one file
    stale_ans_tex = gen_dir / f"{prefix}-answer-key{rev_suffix}.tex"
    if stale_ans_tex.exists() and stale_ans_tex != output_tex_path:
        try:
            stale_ans_tex.unlink()
        except OSError:
            pass

    return output_tex_path


class FormatLatexAsExamInput(BaseModel):
    subject: str
    filename: str = Field(..., description="The .tex or .md filename to format (e.g. 'et2-qp.tex')")


class FormatLatexAsExamTool(BaseTool):
    name: str = "format_latex_as_exam"
    description: str = (
        "Formats a generated question paper and its paired answer key into a single "
        "unified Overleaf 'exam' class .tex file (matching sample_latex_format.tex) "
        "containing \\question, \\hfill metadata, \\begin{checkboxes} with \\CorrectChoice, "
        "\\textbf{Answer:}, and \\begin{solution}...\\end{solution} in the same file."
    )
    args_schema: Type[BaseModel] = FormatLatexAsExamInput

    def _run(self, subject: str, filename: str) -> str:
        try:
            out_path = render_combined_exam_latex(
                subject=subject,
                source_filename=filename,
            )
            return (
                f"Formatted {out_path} as unified Overleaf Exam class "
                f"(questions + solutions in same file)."
            )
        except Exception as exc:  # noqa: BLE001
            return f"Failed to format {filename} as exam LaTeX: {exc}"
