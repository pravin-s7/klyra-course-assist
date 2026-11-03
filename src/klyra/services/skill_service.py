"""Dynamic per-subject `skill.md` loader, generator, and prompt-input builder."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from klyra.models.subject_skill import SubjectOutputFormats, SubjectSkillConfig
from klyra.utils.fs import DOCS_ROOT, subject_dir, subject_skill_path, validate_subject

_REPO_DOCS_ROOT = Path(__file__).resolve().parents[3] / "docs"
_FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---\s*(?:\r?\n|$)(.*)\Z", re.DOTALL)
_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)


def _extract_markdown_sections(markdown_body: str) -> dict[str, str]:
    """Split a Markdown document by level-2 headings (`## Heading`)."""
    sections: dict[str, str] = {}
    matches = list(_HEADING_RE.finditer(markdown_body))
    for idx, match in enumerate(matches):
        title = match.group(1).strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown_body)
        sections[title] = markdown_body[start:end].strip()
    return sections


def _generic_subject_skill(subject: str) -> SubjectSkillConfig:
    """Create a generic fallback `SubjectSkillConfig` for an unknown subject."""
    clean = validate_subject(subject)
    display_name = clean.replace("_", " ").replace("-", " ").title()
    return SubjectSkillConfig(
        subject_id=clean,
        display_name=display_name,
        domain="general",
        allowed_question_types=["MCQ", "MSQ", "NAT"],
        default_type_counts="MCQ:5,MSQ:3,NAT:2",
        coding_languages=["python"],
        default_coding_language="python",
        diagram_style="flowchart",
        strict_syllabus_grounding=True,
        output_formats=SubjectOutputFormats(),
        domain_pedagogical_rules=(
            f"- Ground all content strictly in the indexed syllabus for **{display_name}**.\n"
            "- Ensure questions test conceptual understanding and analytical application rather than rote recall.\n"
            "- Distractors in MCQ/MSQ items must correspond to plausible student misconceptions."
        ),
        diagram_visual_conventions=(
            "- Use clean, high-contrast diagrams with explicit node labels and legends where helpful."
        ),
        qp_output_format_rules=(
            "- Format every question with `### Question N (WNN-QNN) [TYPE] [Marks: M]`.\n"
            "- List options on separate lines using `(a)`, `(b)`, `(c)`, `(d)`.\n"
            "- Never include answers, hints, or solutions in the Question Paper."
        ),
        answer_key_output_format_rules=(
            "- Provide the exact answer token/value first, followed by a step-by-step derivation and per-option distractor analysis."
        ),
        coding_output_format_rules=(
            "- Include `Problem Statement`, `Input Format`, `Output Format`, `Constraints`, and `Sample Test Cases` in `README.md`."
        ),
        teaching_output_format_rules=(
            "- Structure study material into `Learning Objectives`, `Concept Breakdown`, `Worked Examples`, `Common Pitfalls`, and `Practice Questions`."
        ),
    )


def default_subject_skill(subject: str) -> SubjectSkillConfig:
    """Create a `SubjectSkillConfig` for `subject`, using bundled repo presets when available."""
    clean = validate_subject(subject)
    preset_path = _REPO_DOCS_ROOT / clean / "skill.md"
    if preset_path.exists():
        return parse_subject_skill(preset_path, fallback_subject=clean, _use_preset=False)
    return _generic_subject_skill(clean)


def render_subject_skill_markdown(config: SubjectSkillConfig) -> str:
    """Render a `SubjectSkillConfig` into a `docs/<subject>/skill.md` document."""
    frontmatter_data: dict[str, Any] = {
        "subject_id": config.subject_id,
        "display_name": config.display_name,
        "domain": config.domain,
        "allowed_question_types": config.allowed_question_types,
        "default_type_counts": config.default_type_counts,
        "coding_languages": config.coding_languages,
        "default_coding_language": config.default_coding_language,
        "diagram_style": config.diagram_style,
        "strict_syllabus_grounding": config.strict_syllabus_grounding,
        "output_formats": config.output_formats.model_dump(),
    }
    yaml_block = yaml.safe_dump(
        frontmatter_data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    ).strip()

    body = (
        f"# {config.display_name} — Subject Skill & Output Format Specification\n\n"
        f"## Domain & Pedagogical Rules\n{config.domain_pedagogical_rules.strip()}\n\n"
        f"## Diagram & Visual Conventions\n{config.diagram_visual_conventions.strip()}\n\n"
        f"## Question Paper Output Format\n{config.qp_output_format_rules.strip()}\n\n"
        f"## Answer Key Output Format\n{config.answer_key_output_format_rules.strip()}\n\n"
        f"## Coding Problem Output Format\n{config.coding_output_format_rules.strip()}\n\n"
        f"## Teaching Material Output Format\n{config.teaching_output_format_rules.strip()}\n"
    )
    return f"---\n{yaml_block}\n---\n\n{body}"


def parse_subject_skill(
    source: Path | str,
    fallback_subject: str | None = None,
    _use_preset: bool = True,
) -> SubjectSkillConfig:
    """Parse a `skill.md` file (or raw markdown string) with YAML frontmatter and Markdown sections."""
    if isinstance(source, Path):
        raw_text = source.read_text(encoding="utf-8")
        subject_id = fallback_subject or source.parent.name
    else:
        raw_text = source
        subject_id = fallback_subject or "general"

    default_cfg = (
        default_subject_skill(subject_id)
        if _use_preset
        else _generic_subject_skill(subject_id)
    )

    frontmatter: dict[str, Any] = {}
    markdown_body = raw_text

    fm_match = _FRONTMATTER_RE.match(raw_text)
    if fm_match:
        fm_raw, markdown_body = fm_match.group(1), fm_match.group(2)
        loaded = yaml.safe_load(fm_raw)
        if isinstance(loaded, dict):
            frontmatter = loaded

    sections = _extract_markdown_sections(markdown_body)

    def _find_section(*keywords: str, default: str = "") -> str:
        for title, content in sections.items():
            if all(k in title for k in keywords):
                return content
        for title, content in sections.items():
            if any(k in title for k in keywords):
                return content
        return default

    output_formats_raw = frontmatter.get("output_formats")
    output_formats = (
        SubjectOutputFormats.model_validate(output_formats_raw)
        if isinstance(output_formats_raw, dict)
        else default_cfg.output_formats
    )

    return SubjectSkillConfig(
        subject_id=str(frontmatter.get("subject_id") or default_cfg.subject_id),
        display_name=str(frontmatter.get("display_name") or default_cfg.display_name),
        domain=str(frontmatter.get("domain") or default_cfg.domain),
        allowed_question_types=list(
            frontmatter.get("allowed_question_types")
            or default_cfg.allowed_question_types
        ),
        default_type_counts=str(
            frontmatter.get("default_type_counts") or default_cfg.default_type_counts
        ),
        coding_languages=list(
            frontmatter.get("coding_languages") or default_cfg.coding_languages
        ),
        default_coding_language=str(
            frontmatter.get("default_coding_language")
            or default_cfg.default_coding_language
        ),
        diagram_style=str(
            frontmatter.get("diagram_style") or default_cfg.diagram_style
        ),
        strict_syllabus_grounding=bool(
            frontmatter.get(
                "strict_syllabus_grounding", default_cfg.strict_syllabus_grounding
            )
        ),
        output_formats=output_formats,
        domain_pedagogical_rules=_find_section(
            "domain", "pedagogical", default=default_cfg.domain_pedagogical_rules
        ),
        diagram_visual_conventions=_find_section(
            "diagram", "visual", default=default_cfg.diagram_visual_conventions
        ),
        qp_output_format_rules=_find_section(
            "question paper", default=default_cfg.qp_output_format_rules
        ),
        answer_key_output_format_rules=_find_section(
            "answer key", default=default_cfg.answer_key_output_format_rules
        ),
        coding_output_format_rules=_find_section(
            "coding", default=default_cfg.coding_output_format_rules
        ),
        teaching_output_format_rules=_find_section(
            "teaching", default=default_cfg.teaching_output_format_rules
        ),
        raw_markdown=raw_text,
    )


def ensure_subject_skill(
    subject: str,
    root: Path = DOCS_ROOT,
    force: bool = False,
    overwrite: bool = False,
) -> SubjectSkillConfig:
    """Ensure `docs/<subject>/skill.md` exists on disk and return its parsed config."""
    path = subject_skill_path(subject, root=root)
    if path.exists() and not (force or overwrite):
        return parse_subject_skill(path, fallback_subject=subject)

    cfg = default_subject_skill(subject)
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = render_subject_skill_markdown(cfg)
    path.write_text(rendered, encoding="utf-8")
    cfg.raw_markdown = rendered
    return cfg


def load_subject_skill(subject: str, root: Path = DOCS_ROOT) -> SubjectSkillConfig:
    """Load `docs/<subject>/skill.md` if present, auto-creating it if the subject dir exists."""
    path = subject_skill_path(subject, root=root)
    if path.exists():
        return parse_subject_skill(path, fallback_subject=subject)
    sdir = subject_dir(subject, root=root)
    if sdir.exists() and sdir.is_dir():
        return ensure_subject_skill(subject, root=root, force=False)
    return default_subject_skill(subject)


def build_skill_prompt_inputs(subject: str, root: Path = DOCS_ROOT) -> dict[str, str]:
    """Build CrewAI prompt interpolation variables from the subject's `skill.md`."""
    skill = load_subject_skill(subject, root=root)
    fmt = skill.output_formats

    marks_str = ", ".join(f"{k}:{v}" for k, v in fmt.default_marks_by_type.items())
    req_blocks = (
        ", ".join(fmt.required_question_blocks)
        if fmt.required_question_blocks
        else "None mandatory"
    )

    subject_skill_rules = (
        f"Subject: {skill.display_name} ({skill.subject_id}) | Domain: {skill.domain}\n"
        f"Allowed Question Types: {', '.join(skill.allowed_question_types)}\n"
        f"Strict Syllabus Grounding: {skill.strict_syllabus_grounding}\n"
        f"Domain & Pedagogical Rules:\n{skill.domain_pedagogical_rules}"
    )

    subject_diagram_rules = (
        f"Preferred Diagram Style: {skill.diagram_style}\n"
        f"{skill.diagram_visual_conventions}"
    )

    subject_qp_format = (
        f"QP Style: {fmt.qp_style} | Option Labels: {fmt.option_label_style} | "
        f"Group By Section: {fmt.group_by_section} | "
        f"Include Marks: {fmt.include_marks_per_question} ({marks_str}) | "
        f"Required Context Blocks: {req_blocks}\n"
        f"{skill.qp_output_format_rules}"
    )

    subject_answer_key_format = (
        f"Answer Key Style: {fmt.answer_key_style}\n"
        f"{skill.answer_key_output_format_rules}"
    )

    subject_coding_format = (
        f"Allowed Coding Languages: {', '.join(skill.coding_languages)} "
        f"(default: {skill.default_coding_language})\n"
        f"Required README Sections: {', '.join(fmt.coding_readme_sections)}\n"
        f"{skill.coding_output_format_rules}"
    )

    subject_teaching_format = (
        f"Required Teaching Sections: {', '.join(fmt.teaching_sections)}\n"
        f"{skill.teaching_output_format_rules}"
    )

    return {
        "subject_display_name": skill.display_name,
        "subject_domain": skill.domain,
        "subject_allowed_types": ", ".join(skill.allowed_question_types),
        "subject_skill_rules": subject_skill_rules,
        "subject_diagram_rules": subject_diagram_rules,
        "subject_qp_format": subject_qp_format,
        "subject_answer_key_format": subject_answer_key_format,
        "subject_coding_format": subject_coding_format,
        "subject_teaching_format": subject_teaching_format,
    }
