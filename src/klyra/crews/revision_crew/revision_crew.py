"""On-demand revision: takes an already-generated QP + its own review
findings and fixes exactly what was flagged.

Deliberately a separate crew from GenerationCrew (not a 5th auto-run task)
so revision is an explicit, on-demand action - see .cursor/rules/klyra.mdc.
Same agent role as QP Creation Agent (Agent 3), just invoked separately.
"""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import default_llm, default_embedder
from klyra.models.task_outputs import (
    AnswerKeyOutput,
    DiscrepancyReportOutput,
    FinalReviewSummaryOutput,
    RevisedQuestionPaperOutput,
)
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool, GenerateGraphTool
from klyra.tools.syllabus_tools import (
    FetchPyqChunksTool,
    ListSourceFilesTool,
    PyqSearchTool,
    SyllabusSearchTool,
)
from klyra.utils.task_guardrails import (
    answer_key_guardrail,
    discrepancy_report_guardrail,
    final_summary_guardrail,
    revised_qp_output_guardrail,
)


@CrewBase
class RevisionCrew:
    """Fixes a generated question paper using its own discrepancy report,
    answer key, and final summary as input."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def qp_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["qp_reviser"],  # type: ignore[index]
            tools=[
                SyllabusSearchTool(),
                PyqSearchTool(),
                FetchPyqChunksTool(),
                ListSourceFilesTool(),
                SearchImagesTool(),
                GenerateDiagramTool(), GenerateGraphTool(),
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

    @task
    def revise_qp_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["revise_qp_task"],  # type: ignore[index]
            output_pydantic=RevisedQuestionPaperOutput,
            guardrail=revised_qp_output_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-qp-revised.md",
        )

    @task
    def solve_verify_revised_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["solve_verify_revised_task"],  # type: ignore[index]
            context=[self.revise_qp_task()],
            async_execution=True,
            output_pydantic=AnswerKeyOutput,
            guardrail=answer_key_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-answer-key-revised.md",
        )

    @task
    def discrepancy_review_revised_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["discrepancy_review_revised_task"],  # type: ignore[index]
            context=[self.revise_qp_task()],
            async_execution=True,
            output_pydantic=DiscrepancyReportOutput,
            guardrail=discrepancy_report_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-discrepancy-report-revised.md",
        )

    @task
    def finalize_revised_review_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["finalize_revised_review_task"],  # type: ignore[index]
            context=[self.discrepancy_review_revised_task(), self.solve_verify_revised_task()],
            output_pydantic=FinalReviewSummaryOutput,
            guardrail=final_summary_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-final-summary-revised.md",
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
