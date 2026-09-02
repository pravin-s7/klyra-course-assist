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

from klyra.llm_config import default_llm, default_embedder
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool, GenerateGraphTool
from klyra.tools.syllabus_tools import (
    FetchPyqChunksTool,
    ListSourceFilesTool,
    PyqSearchTool,
    SyllabusSearchTool,
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

    @task
    def revise_qp_task(self) -> Task:
        return Task(config=self.tasks_config["revise_qp_task"])  # type: ignore[index]

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
