"""Unified Teaching Domain Crew (Teaching Material Preparation & Revision)."""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import default_embedder, default_llm
from klyra.models.task_outputs import (
    RevisedTeachingMaterialOutput,
    TeachingAnswersOutput,
    TeachingMaterialOutput,
)
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool, GenerateGraphTool
from klyra.tools.syllabus_tools import (
    FetchParsedChunksTool,
    ReportSyllabusGapsTool,
    SyllabusSearchTool,
)
from klyra.utils.task_guardrails import (
    revised_teaching_material_guardrail,
    teaching_answers_guardrail,
    teaching_material_guardrail,
)


@CrewBase
class TeachingMaterialCrew:
    """Prepares live-session teaching material grounded in indexed content."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "teaching_material_crew/config/agents.yaml"
    tasks_config = "teaching_material_crew/config/tasks.yaml"

    @agent
    def teaching_material_preparer(self) -> Agent:
        return Agent(
            config=self.agents_config["teaching_material_preparer"],  # type: ignore[index]
            tools=[
                FetchParsedChunksTool(),
                SyllabusSearchTool(),
                ReportSyllabusGapsTool(),
                SearchImagesTool(),
                GenerateDiagramTool(),
                GenerateGraphTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def teaching_answers_preparer(self) -> Agent:
        return Agent(
            config=self.agents_config["teaching_answers_preparer"],  # type: ignore[index]
            tools=[
                FetchParsedChunksTool(),
                SyllabusSearchTool(),
                SearchImagesTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @task
    def prepare_teaching_material_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["prepare_teaching_material_task"],  # type: ignore[index]
            output_pydantic=TeachingMaterialOutput,
            guardrail=teaching_material_guardrail,
            output_file="docs/{subject}/generated/teaching/{session_name}-teaching-material.md",
        )

    @task
    def prepare_teaching_answers_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["prepare_teaching_answers_task"],  # type: ignore[index]
            context=[self.prepare_teaching_material_task()],
            output_pydantic=TeachingAnswersOutput,
            guardrail=teaching_answers_guardrail,
            output_file="docs/{subject}/generated/teaching/{session_name}-teaching-answers.md",
        )

    @crew
    def crew(self) -> Crew:
        return Crew(
            cache=False,
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
            embedder=default_embedder(),
        )


@CrewBase
class TeachingRevisionCrew:
    """Revises previously prepared teaching material per instructor follow-up notes."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "teaching_revision_crew/config/agents.yaml"
    tasks_config = "teaching_revision_crew/config/tasks.yaml"

    @agent
    def teaching_material_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["teaching_material_reviser"],  # type: ignore[index]
            tools=[
                FetchParsedChunksTool(),
                SyllabusSearchTool(),
                SearchImagesTool(),
                GenerateDiagramTool(),
                GenerateGraphTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def teaching_answers_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["teaching_answers_reviser"],  # type: ignore[index]
            tools=[
                FetchParsedChunksTool(),
                SyllabusSearchTool(),
                SearchImagesTool(),
            ],
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


class TeachingCrew:
    """Domain entry point for Teaching Material preparation and revision crews."""

    @staticmethod
    def generation() -> Crew:
        return TeachingMaterialCrew().crew()

    @staticmethod
    def revision() -> Crew:
        return TeachingRevisionCrew().crew()

