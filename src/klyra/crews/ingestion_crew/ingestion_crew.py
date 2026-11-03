"""Ingestion phase: Document Parsing Agent -> Syllabus Indexer Agent.

Decoupled from the generation phase (see generation_crew.py): this crew runs
whenever source docs change, not per QP request. See
.cursor/rules/klyra.mdc for the full spec.
"""
from __future__ import annotations

from typing import List

from crewai import Agent, Crew, Process, Task
from crewai.agents.agent_builder.base_agent import BaseAgent
from crewai.project import CrewBase, agent, crew, task

from klyra.crews.base_task import KlyraTask
from klyra.llm_config import default_llm, default_embedder
from klyra.models.task_outputs import (
    DocumentParseReportOutput,
    SyllabusIndexReportOutput,
)
from klyra.tools.document_parser_tool import DocumentParserTool
from klyra.tools.syllabus_tools import (
    FetchParsedChunksTool,
    ReportSyllabusGapsTool,
    WriteSyllabusEntriesTool,
)
from klyra.utils.task_guardrails import (
    ingestion_index_guardrail,
    ingestion_parse_guardrail,
)


@CrewBase
class IngestionCrew:
    """Parses raw subject docs and builds the per-week syllabus index."""

    agents: List[BaseAgent]
    tasks: List[Task]

    agents_config = "config/agents.yaml"
    tasks_config = "config/tasks.yaml"

    @agent
    def document_parser(self) -> Agent:
        return Agent(
            config=self.agents_config["document_parser"],  # type: ignore[index]
            tools=[DocumentParserTool()],
            llm=default_llm(),
            verbose=True,
        )

    @agent
    def syllabus_indexer(self) -> Agent:
        return Agent(
            config=self.agents_config["syllabus_indexer"],  # type: ignore[index]
            tools=[FetchParsedChunksTool(), WriteSyllabusEntriesTool(), ReportSyllabusGapsTool()],
            llm=default_llm(),
            verbose=True,
        )

    @task
    def parse_documents_task(self) -> Task:
        return KlyraTask(
            config=self.tasks_config["parse_documents_task"],  # type: ignore[index]
            output_pydantic=DocumentParseReportOutput,
            guardrail=ingestion_parse_guardrail,
        )

    @task
    def index_syllabus_task(self) -> Task:
        # No explicit context needed: Process.sequential already relays the
        # immediately-preceding task's output, and there are only 2 tasks here.
        return KlyraTask(
            config=self.tasks_config["index_syllabus_task"],  # type: ignore[index]
            output_pydantic=SyllabusIndexReportOutput,
            guardrail=ingestion_index_guardrail,
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
