"""Unified Exam Domain Crew (Question Paper Generation & Revision)."""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import default_embedder, default_llm
from klyra.models.task_outputs import (
    AnswerKeyOutput,
    DiscrepancyReportOutput,
    FinalReviewSummaryOutput,
    QuestionPaperOutput,
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
    qp_output_guardrail,
    revised_qp_output_guardrail,
)


def _creator_tools() -> list:
    return [
        SyllabusSearchTool(),
        PyqSearchTool(),
        FetchPyqChunksTool(),
        ListSourceFilesTool(),
        SearchImagesTool(),
        GenerateDiagramTool(),
        GenerateGraphTool(),
    ]


def _reviewer_tools() -> list:
    return [
        SyllabusSearchTool(),
        PyqSearchTool(),
        FetchPyqChunksTool(),
        SearchImagesTool(),
    ]


@CrewBase
class GenerationCrew:
    """Creates a question paper, then reviews and independently solves it."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "generation_crew/config/agents.yaml"
    tasks_config = "generation_crew/config/tasks.yaml"

    @agent
    def qp_creator(self) -> Agent:
        return Agent(
            config=self.agents_config["qp_creator"],  # type: ignore[index]
            tools=_creator_tools(),
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["discrepancy_reviewer"],  # type: ignore[index]
            tools=_reviewer_tools(),
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def solver_verifier(self) -> Agent:
        return Agent(
            config=self.agents_config["solver_verifier"],  # type: ignore[index]
            tools=_reviewer_tools(),
            llm=default_llm(),
            verbose=True,
        )

    @task
    def create_qp_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["create_qp_task"],  # type: ignore[index]
            output_pydantic=QuestionPaperOutput,
            guardrail=qp_output_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-qp.md",
        )

    @task
    def discrepancy_review_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["discrepancy_review_task"],  # type: ignore[index]
            context=[self.create_qp_task()],
            async_execution=True,
            output_pydantic=DiscrepancyReportOutput,
            guardrail=discrepancy_report_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-discrepancy-report.md",
        )

    @task
    def solve_verify_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["solve_verify_task"],  # type: ignore[index]
            context=[self.create_qp_task()],
            async_execution=True,
            output_pydantic=AnswerKeyOutput,
            guardrail=answer_key_guardrail,
            output_file="docs/{subject}/generated/{qp_name}-answer-key.md",
        )

    @task
    def finalize_review_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["finalize_review_task"],  # type: ignore[index]
            context=[self.discrepancy_review_task(), self.solve_verify_task()],
            output_pydantic=FinalReviewSummaryOutput,
            guardrail=final_summary_guardrail,
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


@CrewBase
class RevisionCrew:
    """Fixes a generated question paper using its own discrepancy report,
    answer key, and final summary as input."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "revision_crew/config/agents.yaml"
    tasks_config = "revision_crew/config/tasks.yaml"

    @agent
    def qp_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["qp_reviser"],  # type: ignore[index]
            tools=_creator_tools(),
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def solver_verifier(self) -> Agent:
        return Agent(
            config=self.agents_config["solver_verifier"],  # type: ignore[index]
            tools=_reviewer_tools(),
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["discrepancy_reviewer"],  # type: ignore[index]
            tools=_reviewer_tools(),
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


class ExamCrew:
    """Domain entry point for Question Paper generation and revision crews."""

    @staticmethod
    def generation() -> Crew:
        return GenerationCrew().crew()

    @staticmethod
    def revision() -> Crew:
        return RevisionCrew().crew()

