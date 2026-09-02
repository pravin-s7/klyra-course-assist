"""Independent utility crew: converts generated documents between Markdown
and LaTeX. Not part of the core QP ingestion/generation pipeline - see
.cursor/rules/klyra.mdc for how this fits alongside the core agents.
"""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.llm_config import default_llm, default_embedder
from klyra.tools.format_conversion import (
    ConvertLatexToMarkdownTool,
    ConvertMarkdownToLatexTool,
    ReplaceInGeneratedFileTool,
    FormatLatexAsExamTool,
)
from klyra.tools.image_tools import SearchImagesTool
from klyra.tools.latex_diagram_tool import GenerateDiagramTool, GenerateGraphTool


@CrewBase
class FormatConverterCrew:
    """Converts documents between Markdown and LaTeX via pandoc."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def format_converter(self) -> Agent:
        return Agent(
            config=self.agents_config["format_converter"],  # type: ignore[index]
            tools=[
                ConvertMarkdownToLatexTool(),
                ConvertLatexToMarkdownTool(),
                SearchImagesTool(),
                GenerateDiagramTool(), GenerateGraphTool(),
                ReplaceInGeneratedFileTool(),
                FormatLatexAsExamTool(),
            ],
            llm=default_llm(),
            verbose=True,
        )

    @task
    def convert_documents_task(self) -> Task:
        return Task(config=self.tasks_config["convert_documents_task"])  # type: ignore[index]

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
