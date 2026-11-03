"""Custom CrewAI Task subclass (`KlyraTask`) for structured + subject-formatted outputs."""
from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

from crewai import Task
from pydantic import BaseModel, PrivateAttr

from klyra.services.skill_service import build_skill_prompt_inputs, load_subject_skill
from klyra.utils.format_renderer import render_task_output_markdown

_ACTIVE_CREW_INPUTS: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "_ACTIVE_CREW_INPUTS", default={}
)


def get_active_crew_inputs() -> dict[str, Any]:
    """Return the active crew inputs dictionary for the current task execution."""
    return dict(_ACTIVE_CREW_INPUTS.get() or {})


def set_active_crew_inputs(inputs: dict[str, Any]) -> None:
    """Set the active crew inputs dictionary for the current execution context."""
    _ACTIVE_CREW_INPUTS.set(dict(inputs or {}))


def _infer_subject_from_path(output_file: str | None) -> str:
    if not output_file:
        return ""
    parts = Path(output_file).parts
    # Expected pattern: docs/<subject>/generated/...
    for idx, part in enumerate(parts):
        if part == "docs" and idx + 1 < len(parts):
            return parts[idx + 1]
    return ""


class KlyraTask(Task):
    """CrewAI `Task` subclass that persists both structured JSON (`.json`) and
    per-subject formatted Markdown (`.md`) when `output_pydantic` is configured.
    """

    guardrail_max_retries: int = 3
    _crew_inputs: dict[str, Any] = PrivateAttr(default_factory=dict)

    def interpolate_inputs_and_add_conversation_history(
        self, inputs: dict[str, Any]
    ) -> None:
        merged = dict(inputs or {})
        subj = str(merged.get("subject") or _infer_subject_from_path(self.output_file) or "general")
        try:
            skill_inputs = build_skill_prompt_inputs(subj)
            for k, v in skill_inputs.items():
                merged.setdefault(k, v)
        except Exception:
            for k in (
                "subject_skill_rules",
                "subject_diagram_rules",
                "subject_qp_format",
                "subject_answer_key_format",
                "subject_coding_format",
                "subject_teaching_format",
            ):
                merged.setdefault(k, "")
        self._crew_inputs = merged
        set_active_crew_inputs(self._crew_inputs)
        super().interpolate_inputs_and_add_conversation_history(merged)

    def _resolve_subject(self, model: BaseModel | None = None) -> str:
        subj = (
            self._crew_inputs.get("subject")
            or get_active_crew_inputs().get("subject")
            or (getattr(model, "subject", "") if model is not None else "")
            or _infer_subject_from_path(self.output_file)
            or "general"
        )
        return str(subj)

    def _save_file(self, result: dict[str, Any] | str | Any) -> None:
        """Save structured `.json` sidecar and rendered subject-specific `.md`."""
        if self.output_file is None:
            raise ValueError("output_file is not set.")

        pydantic_model: BaseModel | None = None
        if self.output is not None and isinstance(self.output.pydantic, BaseModel):
            pydantic_model = self.output.pydantic
        elif isinstance(result, BaseModel):
            pydantic_model = result
        elif self.output_pydantic is not None and isinstance(result, str):
            try:
                pydantic_model = self.output_pydantic.model_validate_json(result)
            except Exception:
                pydantic_model = None

        out_path = Path(self.output_file).expanduser().resolve()

        if pydantic_model is not None and out_path.suffix.lower() == ".md":
            subj = self._resolve_subject(pydantic_model)
            try:
                skill = load_subject_skill(subj)
            except Exception:
                skill = None

            # 1. Write the structured JSON sidecar (`<stem>.json`)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            json_sidecar = out_path.with_suffix(".json")
            json_sidecar.write_text(
                pydantic_model.model_dump_json(indent=2), encoding="utf-8"
            )

            # 2. Render subject-formatted Markdown and write to `<stem>.md`
            context_md = render_task_output_markdown(
                pydantic_model, skill=skill, for_student_file=False
            )
            file_md = render_task_output_markdown(
                pydantic_model, skill=skill, for_student_file=True
            )
            if self.output is not None:
                self.output.raw = context_md
                self.output.pydantic = pydantic_model
            super()._save_file(file_md)
            return

        super()._save_file(result)
