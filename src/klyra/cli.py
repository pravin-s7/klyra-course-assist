"""CLI entry point for Klyra (local/dev use).

Usage:
  klyra ingest --subject data-structures [--week 3] [--force]

  klyra generate --subject data-structures --qp-name midterm \\
      --scope "week_1-week_4" --difficulty medium --pyq-similar 3 \\
      --type-counts "MCQ:5,MSQ:3,NAT:2" \\
      --concepts "arrays,linked lists" \\
      --source "Lecture 1.5" \\
      --notes "make B-tree questions hard, keep normalization easy"

  klyra revise --subject data-structures --qp-name midterm \\
      --notes "reformat Q1 as a table; replace Q3 with a question about hashing"

  klyra transcript "https://youtu.be/VIDEO_ID" --subject data-structures \\
      [--week 5] [--title "Lecture 1"]

  klyra convert --subject data-structures \\
      --request "convert midterm-qp.md and midterm-answer-key.md to LaTeX"

  klyra teach --subject data-structures --session-name week-4-live \\
      --scope week_4 --notes "45 min, focus on intuition before formalism"

  klyra revise-teach --subject data-structures --session-name week-4-live \\
      --notes "add a worked example for recursion before the formal definition"

  klyra code --subject data-structures --name lab-3 --language python \\
      --scope week_3 --difficulty medium --num-questions 5 \\
      --public-tests 3 --private-tests 5 \\
      --pyq-similar 1 --leetcode-similar \\
      --notes "focus on recursion, make the linked-list one hard"

  klyra revise-code --subject data-structures --name lab-3 --language python \\
      --notes "problem 2's constraints are unclear about negative numbers"

See README.md for the full option reference and more examples. For the HTTP
API (same underlying services/, meant for production/frontend use), run
`uvicorn klyra.api.main:app` instead - see PRODUCTION.md.
"""
from __future__ import annotations

import argparse
import sys
import warnings
import logging
from pathlib import Path

from dotenv import load_dotenv

warnings.filterwarnings("ignore", category=FutureWarning)
logging.getLogger("google_genai._api_client").setLevel(logging.ERROR)
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

# Kept as a plain string (not imported from code_execution_tool.LANGUAGE_RUNNERS) so
# building --help doesn't pull in crewai/pydantic just to print this list.
_SUPPORTED_LANGUAGES_HELP = "python, java, c, cpp, bash"


def _cmd_ingest(args: argparse.Namespace) -> int:
    from klyra.services.ingestion_service import run_ingestion

    run_ingestion(subject=args.subject, week=args.week, force=args.force)
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    from klyra.services.generation_service import GenerationInputError, run_generation

    try:
        run_generation(
            subject=args.subject,
            qp_name=args.qp_name,
            scope=args.scope,
            difficulty=args.difficulty,
            source=args.source,
            pyq_similar=args.pyq_similar,
            concepts=args.concepts,
            type_counts=args.type_counts,
            types=args.types,
            num_questions=args.num_questions,
            notes=args.notes,
        )
    except GenerationInputError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def _cmd_revise(args: argparse.Namespace) -> int:
    from klyra.services.revision_service import run_revision

    run_revision(subject=args.subject, qp_name=args.qp_name, notes=args.notes)
    return 0


def _cmd_convert(args: argparse.Namespace) -> int:
    from klyra.services.conversion_service import run_conversion

    run_conversion(subject=args.subject, request=args.request)
    return 0


def _cmd_teach(args: argparse.Namespace) -> int:
    from klyra.services.teaching_material_service import run_teaching_material

    run_teaching_material(
        subject=args.subject,
        session_name=args.session_name,
        scope=args.scope,
        concepts=args.concepts,
        notes=args.notes,
    )
    return 0


def _cmd_transcript(args: argparse.Namespace) -> int:
    from klyra.services.transcript_service import run_transcript

    path = run_transcript(url=args.url, subject=args.subject, week=args.week, title=args.title)
    print(f"Saved transcript to {path}")
    return 0


def _cmd_revise_teach(args: argparse.Namespace) -> int:
    from klyra.services.teaching_material_service import run_teaching_revision

    run_teaching_revision(subject=args.subject, session_name=args.session_name, notes=args.notes)
    return 0


def _cmd_code(args: argparse.Namespace) -> int:
    from klyra.services.coding_problem_service import CodingInputError, run_coding_generation

    try:
        run_coding_generation(
            subject=args.subject,
            name=args.name,
            language=args.language,
            scope=args.scope,
            difficulty=args.difficulty,
            num_questions=args.num_questions,
            public_tests=args.public_tests,
            private_tests=args.private_tests,
            source=args.source,
            pyq_similar=args.pyq_similar,
            leetcode_similar=args.leetcode_similar,
            include_reference=args.include_reference,
            concepts=args.concepts,
            notes=args.notes,
        )
    except CodingInputError as exc:
        raise SystemExit(str(exc)) from exc
    return 0


def _cmd_revise_code(args: argparse.Namespace) -> int:
    from klyra.services.coding_problem_service import run_coding_revision

    run_coding_revision(subject=args.subject, name=args.name, language=args.language, notes=args.notes)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("klyra.api.main:app", host=args.host, port=args.port, reload=args.reload)
    return 0


def _cmd_delete_index(args: argparse.Namespace) -> int:
    from klyra.tools import vector_store

    where = {}
    if args.source_file:
        where["source_file"] = args.source_file
    
    if not where:
        print("Error: Must specify at least one condition (e.g. --source-file)")
        return 1

    for name_func in [
        vector_store.parsed_collection_name,
        vector_store.syllabus_collection_name,
        vector_store.pyq_collection_name,
        vector_store.images_collection_name,
    ]:
        name = name_func(args.subject)
        vector_store.delete_by_metadata(name, where)
        
    print(f"Deleted entries from {args.subject} collections where {where}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="klyra")
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="Parse + index a subject's source docs (ingestion phase)")
    p_ingest.add_argument("--subject", required=True)
    p_ingest.add_argument("--week", type=int, default=None)
    p_ingest.add_argument("--force", action="store_true")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_generate = sub.add_parser(
        "generate", help="Create + review + solve a question paper (generation phase)"
    )
    p_generate.add_argument("--subject", required=True)
    p_generate.add_argument("--qp-name", required=True)
    p_generate.add_argument(
        "--scope",
        required=True,
        help='Which weeks: "all", "week_3", "week_1-week_4", "1-4", or "1,3,5" (mix allowed: "1-3,5")',
    )
    p_generate.add_argument(
        "--source",
        default=None,
        help='Optional: restrict to one document, e.g. "Lecture 1.5" (fuzzy match, resolved by the agent)',
    )
    p_generate.add_argument(
        "--difficulty", required=True, help='e.g. "medium" or "40%% easy, 60%% hard" (argparse needs %%)'
    )
    p_generate.add_argument(
        "--pyq-similar",
        type=int,
        default=0,
        dest="pyq_similar",
        help="How many questions should be similar in concept/style to a real PYQ (never a verbatim copy)",
    )
    p_generate.add_argument("--concepts", default=None, help='Optional: specific topics, e.g. "arrays,linked lists"')
    p_generate.add_argument(
        "--type-counts",
        default=None,
        dest="type_counts",
        help='Precise per-type breakdown, e.g. "MCQ:5,MSQ:3,NAT:2" (preferred - overrides --types/--num-questions)',
    )
    p_generate.add_argument(
        "--types", default=None, help="Comma-separated types if not using --type-counts, e.g. MCQ,MSQ,NAT"
    )
    p_generate.add_argument(
        "--num-questions", type=int, default=None, dest="num_questions", help="Total, if not using --type-counts"
    )
    p_generate.add_argument(
        "--notes",
        default=None,
        help='Free-form guidance, e.g. "make B-tree questions hard, keep normalization easy"',
    )
    p_generate.set_defaults(func=_cmd_generate)

    p_revise = sub.add_parser(
        "revise", help="Fix a generated QP's flagged issues using its own review + answer key"
    )
    p_revise.add_argument("--subject", required=True)
    p_revise.add_argument("--qp-name", required=True)
    p_revise.add_argument(
        "--notes",
        default=None,
        help='Specific manual edits, e.g. "reformat Q1 as a table; rephrase option B of Q2 to say X; replace Q3 with a question about Y"',
    )
    p_revise.set_defaults(func=_cmd_revise)

    p_transcript = sub.add_parser("transcript", help="Manually generate a transcript from a YouTube video")
    p_transcript.add_argument("url", help="YouTube URL or video ID")
    p_transcript.add_argument("--subject", required=True)
    p_transcript.add_argument("--week", type=int, default=None)
    p_transcript.add_argument("--title", default=None)
    p_transcript.set_defaults(func=_cmd_transcript)

    p_convert = sub.add_parser(
        "convert", help="Convert generated documents between Markdown and LaTeX (independent agent, via pandoc)"
    )
    p_convert.add_argument("--subject", required=True)
    p_convert.add_argument(
        "--request",
        required=True,
        help='e.g. "convert quiz-1-qp.md and quiz-1-answer-key.md to LaTeX"',
    )
    p_convert.set_defaults(func=_cmd_convert)

    p_teach = sub.add_parser(
        "teach", help="Prepare teaching material for a live session (independent agent)"
    )
    p_teach.add_argument("--subject", required=True)
    p_teach.add_argument("--session-name", required=True, dest="session_name")
    p_teach.add_argument("--scope", required=True, help="Which week(s) - same syntax as generate --scope")
    p_teach.add_argument("--concepts", default=None, help='Optional: specific concepts to focus on')
    p_teach.add_argument(
        "--notes",
        default=None,
        help='What you want covered/emphasized, duration, format, etc., e.g. "45 min, focus on intuition before formalism, 2 worked examples"',
    )
    p_teach.set_defaults(func=_cmd_teach)

    p_revise_teach = sub.add_parser(
        "revise-teach", help="Revise previously prepared teaching material per new instructor notes"
    )
    p_revise_teach.add_argument("--subject", required=True)
    p_revise_teach.add_argument("--session-name", required=True, dest="session_name")
    p_revise_teach.add_argument(
        "--notes",
        required=True,
        help='Required (unlike revise/revise-code): teaching material has no automated review to '
        'fall back on, e.g. "add a worked example for recursion before the formal definition"',
    )
    p_revise_teach.set_defaults(func=_cmd_revise_teach)

    p_code = sub.add_parser(
        "code", help="Generate coding problems with test cases, independently validated by actually running them"
    )
    p_code.add_argument("--subject", required=True)
    p_code.add_argument("--name", required=True, help="Name for the output files, e.g. lab-3")
    p_code.add_argument(
        "--language", required=True, help=f"One of: {_SUPPORTED_LANGUAGES_HELP}"
    )
    p_code.add_argument("--scope", required=True, help="Which week(s) - same syntax as generate --scope")
    p_code.add_argument(
        "--source", default=None, help='Optional: restrict to one document, e.g. "Lab 3 handout"'
    )
    p_code.add_argument("--difficulty", required=True, help='e.g. "medium" or "40%% easy, 60%% hard"')
    p_code.add_argument("--num-questions", type=int, required=True, dest="num_questions")
    p_code.add_argument(
        "--public-tests", type=int, required=True, dest="public_tests", help="Public test cases PER problem"
    )
    p_code.add_argument(
        "--private-tests", type=int, required=True, dest="private_tests", help="Private (hidden) test cases PER problem"
    )
    p_code.add_argument(
        "--pyq-similar",
        type=int,
        default=0,
        dest="pyq_similar",
        help="How many problems should be similar in concept/style to a real PYQ (never a verbatim copy)",
    )
    p_code.add_argument(
        "--leetcode-similar",
        action="store_true",
        dest="leetcode_similar",
        help="Look up related LeetCode problems (by --concepts, subject, or wording in --notes) "
        "and write original LeetCode-style problems inspired by them - never copies",
    )
    p_code.add_argument(
        "--include-reference",
        action="store_true",
        dest="include_reference",
        help="Also write a reference solution into the student-facing problems file (off by default - "
        "this tests coding concepts; the validator still writes its own solution into testcases.md)",
    )
    p_code.add_argument("--concepts", default=None, help='Optional: specific topics, e.g. "recursion,file I/O"')
    p_code.add_argument(
        "--notes",
        default=None,
        help='Free-form guidance, e.g. "focus on recursion, make the linked-list one hard"',
    )
    p_code.set_defaults(func=_cmd_code)

    p_revise_code = sub.add_parser(
        "revise-code", help="Fix a generated coding-problem set's flagged issues using its own review + validation"
    )
    p_revise_code.add_argument("--subject", required=True)
    p_revise_code.add_argument("--name", required=True)
    p_revise_code.add_argument("--language", required=True, help=f"One of: {_SUPPORTED_LANGUAGES_HELP}")
    p_revise_code.add_argument(
        "--notes",
        default=None,
        help='Specific manual edits, e.g. "problem 2\'s constraints are unclear about negative numbers"',
    )
    p_revise_code.set_defaults(func=_cmd_revise_code)

    p_serve = sub.add_parser("serve", help="Run the HTTP API (uvicorn) instead of a one-shot CLI command")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8000)
    p_serve.add_argument("--reload", action="store_true", help="Auto-reload on code changes (dev only)")
    p_serve.set_defaults(func=_cmd_serve)

    p_delete_index = sub.add_parser("delete-index", help="Delete indexed entries by metadata (e.g. by file path)")
    p_delete_index.add_argument("--subject", required=True)
    p_delete_index.add_argument("--source-file", required=True, help="Exact path of the source file to delete from index")
    p_delete_index.set_defaults(func=_cmd_delete_index)

    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path.cwd() / ".env")
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
