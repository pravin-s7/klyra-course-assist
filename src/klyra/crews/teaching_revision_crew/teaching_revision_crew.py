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

from klyra.llm_config import default_llm, default_embedder
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool
from klyra.tools.syllabus_tools import FetchParsedChunksTool, SyllabusSearchTool


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
            tools=[FetchParsedChunksTool(), SyllabusSearchTool(), SearchImagesTool(), GenerateDiagramTool()],
            llm=default_llm(),
            verbose=True,
        )

    @task
    def revise_teaching_material_task(self) -> Task:
        return Task(config=self.tasks_config["revise_teaching_material_task"])  # type: ignore[index]

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
