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


class FormatLatexAsExamInput(BaseModel):
    subject: str
    filename: str = Field(..., description="The .tex filename to format (e.g. 'et2-qp.tex')")


class FormatLatexAsExamTool(BaseTool):
    name: str = "format_latex_as_exam"
    description: str = (
        "Post-processes a Pandoc-generated .tex question paper to use the "
        "Overleaf 'exam' class format. Converts sections to \\question, "
        "extracts metadata to \\hfill, and converts lists to \\begin{checkboxes}."
    )
    args_schema: Type[BaseModel] = FormatLatexAsExamInput

    def _run(self, subject: str, filename: str) -> str:
        tex_path = fs.generated_dir(subject) / filename
        if not tex_path.exists():
            return f"No such file: {tex_path}"
            
        content = tex_path.read_text()
        
        doc_match = re.search(r"\\begin\{document\}(.*?)\\end\{document\}", content, flags=re.DOTALL)
        if not doc_match:
            return "Could not find \\begin{document} in the file."
        body = doc_match.group(1)
        
        body = re.sub(r"\\section\{.*?\}", "", body)
        body = re.sub(r"\\label\{.*?\}", "", body)
        body = re.sub(r"\\maketitle", "", body)
        body = re.sub(r"\\subsubsection\{Question \d+\}", r"\\question", body)
        
        # Split by \question to process each question block individually
        parts = body.split(r"\question")
        for i in range(1, len(parts)):
            q_block = parts[i]
            
            q_type = re.search(r"\\textbf\{Type:\}\s*(.*?)(?:\\\\|\n)", q_block)
            q_week = re.search(r"\\textbf\{Week:\}\s*(.*?)(?:\\\\|\n)", q_block)
            q_marks = re.search(r"\\textbf\{Marks:\}\s*(.*?)(?:\\\\|\n)", q_block)
            
            type_str = q_type.group(1).strip() if q_type else ""
            week_str = q_week.group(1).strip() if q_week else ""
            marks_str = q_marks.group(1).strip() if q_marks else ""
            
            hfill = f" \\hfill [Pravin: Week {week_str}: {type_str}: {marks_str} point]"
            
            q_start = re.search(r"\\textbf\{Question:\}(?:\\\\)?\s*", q_block)
            if q_start:
                q_content = q_block[q_start.end():]
                
                # Clean up dividers first
                q_content = re.sub(r"\\begin\{center\}\\rule.*?\\end\{center\}", "", q_content, flags=re.DOTALL)
                
                # Find the last itemize (which should be the choices block)
                itemize_idx = q_content.rfind(r"\begin{itemize}")
                if itemize_idx != -1:
                    q_text = q_content[:itemize_idx].rstrip()
                    q_rest = q_content[itemize_idx:]
                    parts[i] = f" \n{q_text}{hfill}\n\n{q_rest}"
                else:
                    parts[i] = f" \n{q_content.rstrip()}{hfill}\n\n"
            else:
                # If no question text is found, just clean up dividers
                q_content = re.sub(r"\\begin\{center\}\\rule.*?\\end\{center\}", "", q_block, flags=re.DOTALL)
                parts[i] = f" \n{q_content}"
                
        body = r"\question".join(parts)
        
        def itemize_replacer(match):
            list_content = match.group(1)
            if "⃝" in list_content or "\\bigcirc" in list_content:
                list_content = list_content.replace("\\tightlist", "")
                list_content = re.sub(r"\\item\s*(?:⃝|\\bigcirc)", r"\\choice ", list_content)
                return f"\\begin{{checkboxes}}\n{list_content}\n\\end{{checkboxes}}"
            return match.group(0)
            
        body = re.sub(r"\\begin\{itemize\}(.*?)\\end\{itemize\}", itemize_replacer, body, flags=re.DOTALL)
        
        # Determine readable names for the header
        qp_name = filename.replace("-qp.tex", "").replace(".tex", "").replace("-", " ").title()
        subject_name = subject.upper()
        
        preamble = f'''\\documentclass[12pt, answers]{{exam}}
\\input{{Templates/packages}}
\\usepackage{{tfrupee}}
\\usepackage{{caption}}
\\usepackage{{float}}
\\usepackage{{tikz}}
\\usetikzlibrary{{shapes.geometric, arrows.meta, calc, positioning}} 
\\usepackage[normalem]{{ulem}}

\\def\\ojoin{{\\setbox0=\\hbox{{$\\bowtie$}}%
  \\rule[-.02ex]{{.25em}}{{.4pt}}\\llap{{\\rule[\\ht0]{{.25em}}{{.4pt}}}}}}
\\def\\leftouterjoin{{\\mathbin{{\\ojoin\\mkern-5.8mu\\bowtie}}}}
\\def\\rightouterjoin{{\\mathbin{{\\bowtie\\mkern-5.8mu\\ojoin}}}}
\\def\\fullouterjoin{{\\mathbin{{\\ojoin\\mkern-5.8mu\\bowtie\\mkern-5.8mu\\ojoin}}}}

\\begin{{document}}
%Title starts here
\\begin{{center}}
\\fbox{{\\fbox{{\\parbox{{5.5in}}{{\\centering
\\large {subject_name} \\\\
{qp_name}  \\\\
}}}}}}
\\end{{center}}
\\CorrectChoiceEmphasis{{\\color{{black}}}}

\\begin{{questions}}
'''
        postamble = r'''
\end{questions}
\end{document}
'''
        final_latex = preamble + body + postamble
        tex_path.write_text(final_latex)
        return f"Formatted {tex_path} as Overleaf Exam class."
