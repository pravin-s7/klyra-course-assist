"""Unified Coding Domain Crew (Coding Problem Generation & Revision)."""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import coding_llm, default_embedder
from klyra.models.task_outputs import (
    CodingDiscrepancyReportOutput,
    CodingFinalSummaryOutput,
    CodingProblemSetOutput,
    RevisedCodingProblemSetOutput,
    TestCaseValidationOutput,
)
from klyra.tools.code_execution_tool import RunCodeTool
from klyra.tools.coding_source_tool import SearchCodingSourcesTool
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool, GenerateGraphTool
from klyra.tools.leetcode_tool import SearchLeetCodeTool
from klyra.tools.subject_fs_tool import WriteCodingPackageTool, WriteSubjectFileTool
from klyra.tools.syllabus_tools import PyqSearchTool
from klyra.utils.task_guardrails import (
    coding_discrepancy_guardrail,
    coding_final_summary_guardrail,
    coding_problems_guardrail,
    coding_test_cases_guardrail,
    revised_coding_problems_guardrail,
)


def _coding_creator_tools(include_runner: bool = False) -> list:
    tools = [
        SearchCodingSourcesTool(),
        SearchLeetCodeTool(),
        PyqSearchTool(),
        SearchImagesTool(),
        GenerateDiagramTool(),
        GenerateGraphTool(),
    ]
    if include_runner:
        tools.append(RunCodeTool())
    tools.extend([WriteCodingPackageTool(), WriteSubjectFileTool()])
    return tools


def _coding_reviewer_tools() -> list:
    return [
        SearchCodingSourcesTool(),
        SearchLeetCodeTool(),
        PyqSearchTool(),
        SearchImagesTool(),
    ]


def _coding_validator_tools() -> list:
    return [RunCodeTool(), WriteCodingPackageTool(), WriteSubjectFileTool()]


@CrewBase
class CodingProblemCrew:
    """Creates coding problems, then reviews wording/scope and independently
    validates every test case by actually running code."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "coding_problem_crew/config/agents.yaml"
    tasks_config = "coding_problem_crew/config/tasks.yaml"

    @agent
    def coding_problem_creator(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_problem_creator"],  # type: ignore[index]
            tools=_coding_creator_tools(include_runner=False),
            llm=coding_llm(),
            verbose=True,
        )

    @agent
    def coding_discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_discrepancy_reviewer"],  # type: ignore[index]
            tools=_coding_reviewer_tools(),
            llm=coding_llm(),
            verbose=True,
        )

    @agent
    def test_case_validator(self) -> Agent:
        return Agent(
            config=self.agents_config["test_case_validator"],  # type: ignore[index]
            tools=_coding_validator_tools(),
            llm=coding_llm(),
            verbose=True,
        )

    @task
    def create_coding_problems_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["create_coding_problems_task"],  # type: ignore[index]
            output_pydantic=CodingProblemSetOutput,
            guardrail=coding_problems_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-problems.md",
        )

    @task
    def coding_discrepancy_review_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["coding_discrepancy_review_task"],  # type: ignore[index]
            context=[self.create_coding_problems_task()],
            async_execution=True,
            output_pydantic=CodingDiscrepancyReportOutput,
            guardrail=coding_discrepancy_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-coding-discrepancy-report.md",
        )

    @task
    def validate_test_cases_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["validate_test_cases_task"],  # type: ignore[index]
            context=[self.create_coding_problems_task()],
            async_execution=True,
            output_pydantic=TestCaseValidationOutput,
            guardrail=coding_test_cases_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-testcases.md",
        )

    @task
    def finalize_coding_review_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["finalize_coding_review_task"],  # type: ignore[index]
            context=[self.coding_discrepancy_review_task(), self.validate_test_cases_task()],
            output_pydantic=CodingFinalSummaryOutput,
            guardrail=coding_final_summary_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-final-summary.md",
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
class CodingRevisionCrew:
    """Fixes a generated coding-problem set using its own discrepancy
    report and test-case validation report as input."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "coding_revision_crew/config/agents.yaml"
    tasks_config = "coding_revision_crew/config/tasks.yaml"

    @agent
    def coding_problem_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_problem_reviser"],  # type: ignore[index]
            tools=_coding_creator_tools(include_runner=True),
            llm=coding_llm(),
            verbose=True,
        )

    @agent
    def test_case_validator(self) -> Agent:
        return Agent(
            config=self.agents_config["test_case_validator"],  # type: ignore[index]
            tools=_coding_validator_tools(),
            llm=coding_llm(),
            verbose=True,
        )

    @agent
    def coding_discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_discrepancy_reviewer"],  # type: ignore[index]
            tools=_coding_reviewer_tools(),
            llm=coding_llm(),
            verbose=True,
        )

    @task
    def revise_coding_problems_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["revise_coding_problems_task"],  # type: ignore[index]
            output_pydantic=RevisedCodingProblemSetOutput,
            guardrail=revised_coding_problems_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-problems-revised.md",
        )

    @task
    def validate_revised_test_cases_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["validate_revised_test_cases_task"],  # type: ignore[index]
            context=[self.revise_coding_problems_task()],
            async_execution=True,
            output_pydantic=TestCaseValidationOutput,
            guardrail=coding_test_cases_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-testcases-revised.md",
        )

    @task
    def coding_discrepancy_review_revised_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["coding_discrepancy_review_revised_task"],  # type: ignore[index]
            context=[self.revise_coding_problems_task()],
            async_execution=True,
            output_pydantic=CodingDiscrepancyReportOutput,
            guardrail=coding_discrepancy_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-coding-discrepancy-report-revised.md",
        )

    @task
    def finalize_revised_coding_review_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["finalize_revised_coding_review_task"],  # type: ignore[index]
            context=[self.coding_discrepancy_review_revised_task(), self.validate_revised_test_cases_task()],
            output_pydantic=CodingFinalSummaryOutput,
            guardrail=coding_final_summary_guardrail,
            output_file="docs/{subject}/generated/coding/{name}-final-summary-revised.md",
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


class CodingCrew:
    """Domain entry point for Coding Problem generation and revision crews."""

    @staticmethod
    def generation() -> Crew:
        return CodingProblemCrew().crew()

    @staticmethod
    def revision() -> Crew:
        return CodingRevisionCrew().crew()

