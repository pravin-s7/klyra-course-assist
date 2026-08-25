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

from klyra.llm_config import default_llm, default_embedder
from klyra.tools.code_execution_tool import RunCodeTool
from klyra.tools.coding_source_tool import SearchCodingSourcesTool
from klyra.tools.leetcode_tool import SearchLeetCodeTool
from klyra.tools.subject_fs_tool import WriteCodingPackageTool, WriteSubjectFileTool
from klyra.tools.syllabus_tools import PyqSearchTool


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
            llm=default_llm(),
            verbose=True,
        )

    @task
    def revise_coding_problems_task(self) -> Task:
        return Task(config=self.tasks_config["revise_coding_problems_task"])  # type: ignore[index]

    @crew
    def crew(self) -> Crew:
        return Crew(
            cache=False,
            agents=self.agents,
            tasks=self.tasks,
            process=Process.sequential,
            verbose=True,
            memory=True,
            embedder=default_embedder(),
        )
