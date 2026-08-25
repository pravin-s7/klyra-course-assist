"""Generation phase: QP Creation -> (Discrepancy Review || Solve & Verify) -> finalize.

Runs per QP request after ingest has indexed syllabus and/or PYQ content
for the subject. Syllabus coverage is preferred; a PYQ-only subject is
enough to generate. See .cursor/rules/klyra.mdc for the full spec,
including why agents 4 and 5 run in parallel.
"""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.llm_config import default_llm, default_embedder
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool
from klyra.tools.syllabus_tools import (
    FetchPyqChunksTool,
    ListSourceFilesTool,
    PyqSearchTool,
    SyllabusSearchTool,
)


@CrewBase
class GenerationCrew:
    """Creates a question paper, then reviews and independently solves it."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def qp_creator(self) -> Agent:
        return Agent(
            config=self.agents_config["qp_creator"],  # type: ignore[index]
            tools=[
                SyllabusSearchTool(),
                PyqSearchTool(),
                FetchPyqChunksTool(),
                ListSourceFilesTool(),
                SearchImagesTool(),
                GenerateDiagramTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["discrepancy_reviewer"],  # type: ignore[index]
            tools=[
                SyllabusSearchTool(),
                PyqSearchTool(),
                FetchPyqChunksTool(),
                SearchImagesTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def solver_verifier(self) -> Agent:
        return Agent(
            config=self.agents_config["solver_verifier"],  # type: ignore[index]
            tools=[
                SyllabusSearchTool(),
                PyqSearchTool(),
                FetchPyqChunksTool(),
                SearchImagesTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @task
    def create_qp_task(self) -> Task:
        return Task(
            config=self.tasks_config["create_qp_task"],  # type: ignore[index]
            output_file="docs/{subject}/generated/{qp_name}-qp.md",
        )

    @task
    def discrepancy_review_task(self) -> Task:
        # Async + explicit context (not the YAML): it must depend on
        # create_qp_task specifically, not on whatever task precedes it in
        # the list, and it must run in parallel with solve_verify_task.
        return Task(
            config=self.tasks_config["discrepancy_review_task"],  # type: ignore[index]
            context=[self.create_qp_task()],
            async_execution=True,
            output_file="docs/{subject}/generated/{qp_name}-discrepancy-report.md",
        )

    @task
    def solve_verify_task(self) -> Task:
        return Task(
            config=self.tasks_config["solve_verify_task"],  # type: ignore[index]
            context=[self.create_qp_task()],
            async_execution=True,
            output_file="docs/{subject}/generated/{qp_name}-answer-key.md",
        )

    @task
    def finalize_review_task(self) -> Task:
        # Sync, and depends on BOTH async tasks above - this closes the
        # async barrier (CrewAI allows at most one trailing async task) and
        # is where the two independent reviews get cross-checked.
        return Task(
            config=self.tasks_config["finalize_review_task"],  # type: ignore[index]
            context=[self.discrepancy_review_task(), self.solve_verify_task()],
            output_file="docs/{subject}/generated/{qp_name}-final-summary.md",
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
