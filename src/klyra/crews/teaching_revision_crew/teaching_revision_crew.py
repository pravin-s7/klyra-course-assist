"""On-demand revision for teaching material: takes already-prepared
material plus the instructor's follow-up instructions and applies exactly
those changes.

Deliberately a separate crew from TeachingMaterialCrew (not an auto-run
second task), same relationship as revision_crew.py has to
generation_crew.py - see .cursor/rules/klyra.mdc. Same agent role as
Teaching Material Preparer Agent, just invoked separately, on demand.
"""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import default_llm, default_embedder
from klyra.models.task_outputs import (
    RevisedTeachingMaterialOutput,
    TeachingAnswersOutput,
)
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool, GenerateGraphTool
from klyra.tools.syllabus_tools import FetchParsedChunksTool, SyllabusSearchTool
from klyra.utils.task_guardrails import (
    revised_teaching_material_guardrail,
    teaching_answers_guardrail,
)


@CrewBase
class TeachingRevisionCrew:
    """Revises previously prepared teaching material per instructor follow-up notes."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def teaching_material_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["teaching_material_reviser"],  # type: ignore[index]
            tools=[FetchParsedChunksTool(), SyllabusSearchTool(), SearchImagesTool(), GenerateDiagramTool(), GenerateGraphTool()],
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def teaching_answers_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["teaching_answers_reviser"],  # type: ignore[index]
            tools=[FetchParsedChunksTool(), SyllabusSearchTool(), SearchImagesTool()],
            llm=default_llm(),
            verbose=True,
        )

    @task
    def revise_teaching_material_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["revise_teaching_material_task"],  # type: ignore[index]
            output_pydantic=RevisedTeachingMaterialOutput,
            guardrail=revised_teaching_material_guardrail,
            output_file="docs/{subject}/generated/teaching/{session_name}-teaching-material-revised.md",
        )

    @task
    def revise_teaching_answers_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["revise_teaching_answers_task"],  # type: ignore[index]
            context=[self.revise_teaching_material_task()],
            output_pydantic=TeachingAnswersOutput,
            guardrail=teaching_answers_guardrail,
            output_file="docs/{subject}/generated/teaching/{session_name}-teaching-answers-revised.md",
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            cache=False,
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
            memory=False,
            embedder=default_embedder(),
        )

