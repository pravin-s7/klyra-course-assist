"""On-demand revision for coding problems: takes an already-generated set
of coding problems plus its own discrepancy report and test-case
validation report and fixes exactly what was flagged.

Deliberately a separate crew from CodingProblemCrew (not a 4th auto-run
task), same relationship as revision_crew.py has to generation_crew.py -
see .cursor/rules/klyra.mdc. Same agent role as Coding Problem Creation
Agent, just invoked separately, on demand.
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
    RevisedCodingProblemSetOutput,
    TestCaseValidationOutput,
)
from klyra.tools.code_execution_tool import RunCodeTool
from klyra.tools.coding_source_tool import SearchCodingSourcesTool
from klyra.tools.leetcode_tool import SearchLeetCodeTool
from klyra.tools.subject_fs_tool import WriteCodingPackageTool, WriteSubjectFileTool
from klyra.tools.syllabus_tools import PyqSearchTool
from klyra.utils.task_guardrails import (
    coding_discrepancy_guardrail,
    coding_final_summary_guardrail,
    coding_test_cases_guardrail,
    revised_coding_problems_guardrail,
)


@CrewBase
class CodingRevisionCrew:
    """Fixes a generated coding-problem set using its own discrepancy
    report and test-case validation report as input."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def coding_problem_reviser(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_problem_reviser"],  # type: ignore[index]
            tools=[
                SearchCodingSourcesTool(),
                SearchLeetCodeTool(),
                PyqSearchTool(),
                RunCodeTool(),
                WriteCodingPackageTool(),
                WriteSubjectFileTool(),
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

    @agent
    def coding_discrepancy_reviewer(self) -> Agent:
        return Agent(
            config=self.agents_config["coding_discrepancy_reviewer"],  # type: ignore[index]
            tools=[SearchCodingSourcesTool(), SearchLeetCodeTool(), PyqSearchTool()],
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
