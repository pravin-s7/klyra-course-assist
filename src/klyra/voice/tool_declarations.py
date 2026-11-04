"""Gemini Live API function declarations for all Klyra CLI features.

Each declaration tells Gemini what tool is available, what parameters it
needs, and what each parameter means — so the model can gather them
conversationally from the user before invoking the tool.

The declarations follow the ``google.genai.types.FunctionDeclaration``
schema used by ``LiveConnectConfig.tools``.
"""
from __future__ import annotations


def get_all_declarations() -> list[dict]:
    """Return all tool function declarations for the Gemini Live session."""
    decls = [
        _ingest(),
        _generate_question_paper(),
        _revise_question_paper(),
        _convert_documents(),
        _prepare_teaching_material(),
        _revise_teaching_material(),
        _fetch_transcript(),
        _generate_coding_problems(),
        _revise_coding_problems(),
        _delete_index(),
        _read_document(),
        _list_documents(),
        _get_subject_skill(),
        _update_subject_skill(),
        _verify_coding_package(),
    ]
    # Require confirmation for ALL tools
    for d in decls:
        if "required" not in d["parameters"]:
            d["parameters"]["required"] = []
        d["parameters"]["required"].append("user_has_confirmed")
        d["parameters"]["properties"]["user_has_confirmed"] = {
            "type": "BOOLEAN",
            "description": (
                "Set to true ONLY if you have explicitly asked the user for permission to run this tool in previous messages and they said yes. "
                "You MUST also ask them if they want to provide optional arguments (concepts/notes). "
                "If you haven't asked them yet, set it to false, and the tool will safely return an error so you can ask the user."
            ),
        }
            
    return decls


# ── Individual declarations ─────────────────────────────────────────────


def _list_documents() -> dict:
    return {
        "name": "list_documents",
        "description": (
            "List all files available in a subject's docs/ directory or a specific subfolder. "
            "Use this when the user asks you to read all files in a folder, or when you need "
            "to find out the exact filenames before calling read_document."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'dbms').",
                },
                "folder": {
                    "type": "STRING",
                    "description": "Optional subfolder to list (e.g. 'pyq', 'generated'). If omitted, lists all files in the subject's docs.",
                },
            },
            "required": ["subject"],
        },
    }


def _ingest() -> dict:
    return {
        "name": "ingest",
        "description": (
            "Parse and index a subject's source documents (PDFs, slides, "
            "transcripts, etc.) into the vector store so they can be used "
            "for question paper and coding problem generation. "
            "CRITICAL: Do NOT run this automatically. ONLY run this tool if "
            "the user explicitly asks to 'ingest', 'index', or 'upload' materials."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": (
                        "Subject identifier (e.g. 'data-structures', 'dbms'). "
                        "Must match a folder name under docs/."
                    ),
                },
                "week": {
                    "type": "INTEGER",
                    "description": (
                        "Optional: only ingest a specific week (1-12). "
                        "If omitted, all weeks and additional materials are ingested."
                    ),
                },
                "force": {
                    "type": "BOOLEAN",
                    "description": (
                        "If true, re-parse files even if they haven't changed "
                        "since the last ingestion. Default is false."
                    ),
                },
                "categories": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": (
                        "Optional: specific folders/categories to ingest (e.g. ['pyq', 'additional', 'transcripts']). "
                        "If omitted, all materials are ingested."
                    ),
                },
            },
            "required": ["subject"],
        },
    }


def _generate_question_paper() -> dict:
    return {
        "name": "generate_question_paper",
        "description": (
            "Create a complete question paper with review and answer key. "
            "The user must specify the subject, a name for the paper, which "
            "weeks/scope to cover, and the difficulty level. Either provide "
            "type_counts (e.g. 'MCQ:5,MSQ:3,NAT:2') for precise control, "
            "or provide both types and num_questions for a simpler spec."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'data-structures').",
                },
                "qp_name": {
                    "type": "STRING",
                    "description": (
                        "Name for the question paper (e.g. 'midterm', 'quiz-1'). "
                        "Used as the output filename."
                    ),
                },
                "scope": {
                    "type": "STRING",
                    "description": (
                        "Which weeks to cover. Examples: 'all', 'week_3', "
                        "'week_1-week_4', '1-4', '1,3,5', '1-3,5'."
                    ),
                },
                "difficulty": {
                    "type": "STRING",
                    "description": (
                        "Difficulty level. Examples: 'easy', 'medium', 'hard', "
                        "or a mix like '40% easy, 60% hard'."
                    ),
                },
                "type_counts": {
                    "type": "STRING",
                    "description": (
                        "Precise per-type question counts, e.g. 'MCQ:5,MSQ:3,NAT:2'. "
                        "Preferred over types+num_questions. If provided, types and "
                        "num_questions are ignored."
                    ),
                },
                "types": {
                    "type": "STRING",
                    "description": (
                        "Comma-separated question types (e.g. 'MCQ,MSQ,NAT'). "
                        "Only used if type_counts is not provided."
                    ),
                },
                "num_questions": {
                    "type": "INTEGER",
                    "description": (
                        "Total number of questions. Only used if type_counts "
                        "is not provided (paired with types)."
                    ),
                },
                "source": {
                    "type": "STRING",
                    "description": (
                        "Optional: restrict to one source document (fuzzy matched), "
                        "e.g. 'Lecture 1.5'."
                    ),
                },
                "pyq_similar": {
                    "type": "INTEGER",
                    "description": (
                        "How many questions should be similar in style to real "
                        "past-year questions (never verbatim copies). Default 0."
                    ),
                },
                "concepts": {
                    "type": "STRING",
                    "description": (
                        "Optional: specific topics to focus on, e.g. "
                        "'arrays, linked lists'."
                    ),
                },
                "notes": {
                    "type": "STRING",
                    "description": (
                        "Optional free-form guidance, e.g. 'make B-tree questions "
                        "hard, keep normalization easy'."
                    ),
                },
            },
            "required": ["subject", "qp_name", "scope", "difficulty", "types", "num_questions"],
        },
    }


def _revise_question_paper() -> dict:
    return {
        "name": "revise_question_paper",
        "description": (
            "Revise a previously generated question paper. Uses the paper's "
            "own review and answer key to fix flagged issues. Optionally "
            "accepts manual notes for specific edits."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "qp_name": {
                    "type": "STRING",
                    "description": "The base name of the question paper to revise (e.g. 'endterm-qp').",
                },
                "notes": {
                    "type": "STRING",
                    "description": (
                        "Optional specific edits, e.g. 'reformat Q1 as a table; "
                        "replace Q3 with a question about hashing'."
                    ),
                },
            },
            "required": ["subject", "qp_name"],
        },
    }


def _convert_documents() -> dict:
    return {
        "name": "convert_documents",
        "description": (
            "Convert generated documents between Markdown and LaTeX using "
            "an independent agent with pandoc."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "request": {
                    "type": "STRING",
                    "description": (
                        "What to convert, e.g. 'convert midterm-qp-qp.md and "
                        "midterm-qp-answer-key.md to LaTeX'. "
                        "CRITICAL: Do NOT guess filenames. If you don't know the exact filenames, "
                        "run list_documents first to find them."
                    ),
                },
            },
            "required": ["subject", "request"],
        },
    }


def _prepare_teaching_material() -> dict:
    return {
        "name": "prepare_teaching_material",
        "description": (
            "Prepare structured teaching material for a live classroom "
            "session. Generates a comprehensive session plan with content, "
            "worked examples, and activity suggestions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "session_name": {
                    "type": "STRING",
                    "description": (
                        "Name for the session, e.g. 'week-4-live'. "
                        "Used as the output filename."
                    ),
                },
                "scope": {
                    "type": "STRING",
                    "description": (
                        "Which weeks to cover. Same syntax as generate scope."
                    ),
                },
                "concepts": {
                    "type": "STRING",
                    "description": "Optional: specific concepts to focus on.",
                },
                "notes": {
                    "type": "STRING",
                    "description": (
                        "Optional guidance, e.g. '45 min, focus on intuition "
                        "before formalism, 2 worked examples'."
                    ),
                },
            },
            "required": ["subject", "session_name", "scope"],
        },
    }


def _revise_teaching_material() -> dict:
    return {
        "name": "revise_teaching_material",
        "description": (
            "Revise previously prepared teaching material based on "
            "instructor notes. Notes are required since teaching material "
            "has no automated review to fall back on."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "session_name": {
                    "type": "STRING",
                    "description": "Name of the teaching session to revise.",
                },
                "notes": {
                    "type": "STRING",
                    "description": (
                        "Required revision instructions, e.g. 'add a worked "
                        "example for recursion before the formal definition'."
                    ),
                },
            },
            "required": ["subject", "session_name", "notes"],
        },
    }


def _fetch_transcript() -> dict:
    return {
        "name": "fetch_transcript",
        "description": (
            "Download and save a transcript from a YouTube video. "
            "Useful for ingesting lecture recordings."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "url": {
                    "type": "STRING",
                    "description": "YouTube URL or video ID.",
                },
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "week": {
                    "type": "INTEGER",
                    "description": "Optional: which week this lecture belongs to.",
                },
                "title": {
                    "type": "STRING",
                    "description": "Optional: title for the transcript file.",
                },
            },
            "required": ["url", "subject"],
        },
    }


def _generate_coding_problems() -> dict:
    return {
        "name": "generate_coding_problems",
        "description": (
            "Generate a set of coding problems with test cases. Problems "
            "are independently validated by actually compiling and running "
            "reference solutions against the generated test cases in Docker."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "name": {
                    "type": "STRING",
                    "description": (
                        "Name for the problem set, e.g. 'lab-3'. "
                        "Used as the output filename."
                    ),
                },
                "language": {
                    "type": "STRING",
                    "description": (
                        "Programming language: python, java, c, cpp, bash, sed, awk, gawk, sql, postgresql, or python-db."
                    ),
                },
                "scope": {
                    "type": "STRING",
                    "description": "Which weeks to cover.",
                },
                "difficulty": {
                    "type": "STRING",
                    "description": (
                        "Difficulty level, e.g. 'medium' or '40% easy, 60% hard'."
                    ),
                },
                "num_questions": {
                    "type": "INTEGER",
                    "description": "Number of coding problems to generate.",
                },
                "public_tests": {
                    "type": "INTEGER",
                    "description": "Number of public (visible) test cases per problem.",
                },
                "private_tests": {
                    "type": "INTEGER",
                    "description": "Number of private (hidden) test cases per problem.",
                },
                "source": {
                    "type": "STRING",
                    "description": (
                        "Optional: restrict to one source document, e.g. "
                        "'Lab 3 handout'."
                    ),
                },
                "pyq_similar": {
                    "type": "INTEGER",
                    "description": (
                        "How many problems should be similar to real past-year "
                        "questions. Default 0."
                    ),
                },
                "leetcode_similar": {
                    "type": "BOOLEAN",
                    "description": (
                        "If true, look up related LeetCode problems and write "
                        "original problems inspired by them. Default false."
                    ),
                },
                "include_reference": {
                    "type": "BOOLEAN",
                    "description": (
                        "If true, include reference solutions in the student-facing "
                        "problems file. Default false."
                    ),
                },
                "concepts": {
                    "type": "STRING",
                    "description": (
                        "Optional: specific topics, e.g. 'recursion, file I/O'."
                    ),
                },
                "notes": {
                    "type": "STRING",
                    "description": (
                        "Optional free-form guidance, e.g. 'focus on recursion, "
                        "make the linked-list one hard'."
                    ),
                },
            },
            "required": [
                "subject", "name", "language", "scope", "difficulty",
                "num_questions", "public_tests", "private_tests",
            ],
        },
    }


def _revise_coding_problems() -> dict:
    return {
        "name": "revise_coding_problems",
        "description": (
            "Revise a previously generated coding problem set. Uses the "
            "automated review and validation results to fix flagged issues."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "name": {
                    "type": "STRING",
                    "description": "Name of the problem set to revise.",
                },
                "language": {
                    "type": "STRING",
                    "description": (
                        "Programming language: python, java, c, cpp, bash, sed, awk, gawk, sql, postgresql, or python-db."
                    ),
                },
                "notes": {
                    "type": "STRING",
                    "description": (
                        "Optional specific edits, e.g. 'problem 2's constraints "
                        "are unclear about negative numbers'."
                    ),
                },
            },
            "required": ["subject", "name", "language"],
        },
    }


def _delete_index() -> dict:
    return {
        "name": "delete_index",
        "description": (
            "Delete indexed entries from the vector store by metadata. "
            "For example, remove all chunks from a specific source file "
            "that was re-uploaded or is no longer needed."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier.",
                },
                "source_files": {
                    "type": "ARRAY",
                    "items": {"type": "STRING"},
                    "description": "Optional list of exact paths of the source files to remove from the index."
                },
                "folder": {
                    "type": "STRING",
                    "description": "Optional specific folder (e.g. 'pyq') to remove from the index."
                },
                "source_file": {
                    "type": "STRING",
                    "description": "Optional single exact path of the source file to remove from the index."
                },
            },
            "required": ["subject"],
        },
    }


def _read_document() -> dict:
    return {
        "name": "read_document",
        "description": (
            "Read the contents of a generated document or any file in the subject's docs/ directory. "
            "Use this when the user asks you to verify the actual content of a generated question paper, "
            "answer key, or other files instead of just relying on the tool success message."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'dbms').",
                },
                "filename": {
                    "type": "STRING",
                    "description": "Filename or path relative to the subject's docs folder (e.g. 'generated/end-term-qp.md').",
                },
            },
            "required": ["subject", "filename"],
        },
    }



def _generate_diagram() -> dict:
    return {
        "name": "generate_diagram",
        "description": (
            "Render a NEW diagram (tree, B-tree, graph, ER diagram, table, "
            "dependency diagram, etc.) via LaTeX/TikZ. "
            "Use this ONLY for academic purposes based on instructions. "
            "You MUST output the returned image markdown directly to the user."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'data-structures')."
                },
                "latex_body": {
                    "type": "STRING",
                    "description": (
                        "LaTeX for the diagram - only the content that goes inside "
                        "\\begin{document}...\\end{document}. "
                        "CRITICAL: Adapt your TikZ/LaTeX structures to the precise semantics of the "
                        "subject! For example, if CT, MUST be a DIRECTED graph."
                    )
                },
                "caption": {
                    "type": "STRING",
                    "description": "Short caption describing the diagram."
                },
                "ground_truth": {
                    "type": "STRING",
                    "description": "The FULL factual content of the diagram in plain text."
                }
            },
            "required": ["subject", "latex_body", "caption", "ground_truth"],
        },
    }


def _get_subject_skill() -> dict:
    return {
        "name": "get_subject_skill",
        "description": (
            "Read a subject's skill.md configuration (domain rules, allowed question types, "
            "coding languages, and custom output formats for question papers, answer keys, "
            "coding problems, and teaching material)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'dbms', 'ct', 'python').",
                },
            },
            "required": ["subject"],
        },
    }


def _update_subject_skill() -> dict:
    return {
        "name": "update_subject_skill",
        "description": (
            "Validate and update a subject's docs/<subject>/skill.md configuration file "
            "with custom YAML frontmatter and markdown formatting instructions."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'dbms').",
                },
                "markdown": {
                    "type": "STRING",
                    "description": "Complete skill.md content with YAML frontmatter and markdown sections.",
                },
            },
            "required": ["subject", "markdown"],
        },
    }


def _verify_coding_package() -> dict:
    return {
        "name": "verify_coding_package",
        "description": (
            "Execute all public and private test cases of an on-disk coding problem package "
            "against its reference solution in the isolated Docker sandbox and return the pass/fail report."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "subject": {
                    "type": "STRING",
                    "description": "Subject identifier (e.g. 'python', 'c_programming', 'dbms').",
                },
                "name": {
                    "type": "STRING",
                    "description": "Name of the coding problem set (e.g. 'lab-3').",
                },
                "language": {
                    "type": "STRING",
                    "description": (
                        "Optional language override: python, java, c, cpp, bash, sql, postgresql, or python-db."
                    ),
                },
            },
            "required": ["subject", "name"],
        },
    }
