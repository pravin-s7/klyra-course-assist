"""Generation phase for coding problems: Coding Problem Creation -> (Coding
Discrepancy Review || Test-Case Validation) -> finalize.

Runs per request. Does not require IngestionCrew coverage - coding
problems can be written from --concepts / --notes / search_leetcode /
search_pyq. When lab/assignment files exist under the week folders those
are preferred over lecture theory. See .cursor/rules/klyra.mdc.
"""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import default_llm, default_embedder, coding_llm
from klyra.models.task_outputs import (
    CodingDiscrepancyReportOutput,
    CodingFinalSummaryOutput,
    CodingProblemSetOutput,
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
)


@CrewBase
class CodingProblemCrew:
    """Creates coding problems, then reviews wording/scope and independently
    validates every test case by actually running code."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def coding_problem_creator(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_problem_creator"],  # type: ignore[index]
            tools=[
                SearchCodingSourcesTool(),
                SearchLeetCodeTool(),
                PyqSearchTool(),
                SearchImagesTool(),
                GenerateDiagramTool(),
                GenerateGraphTool(),
                WriteCodingPackageTool(),
                WriteSubjectFileTool(),
            ],
            llm=coding_llm(),
            verbose=True,
        )

    @agent
    def coding_discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_discrepancy_reviewer"],  # type: ignore[index]
            tools=[
                SearchCodingSourcesTool(),
                SearchLeetCodeTool(),
                PyqSearchTool(),
                SearchImagesTool(),
            ],
            llm=coding_llm(),
            verbose=True,
        )

    @agent
    def test_case_validator(self) -> Agent:
        return Agent(
            config=self.agents_config["test_case_validator"],  # type: ignore[index]
            tools=[RunCodeTool(), WriteCodingPackageTool(), WriteSubjectFileTool()],
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
        # Async + explicit context (not the YAML): must depend on
        # create_coding_problems_task specifically, and must run in
        # parallel with validate_test_cases_task - same pattern as
        # generation_crew's discrepancy_review_task/solve_verify_task.
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
        # Sync, depends on BOTH async tasks above - closes the async
        # barrier, same as generation_crew's finalize_review_task.
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
