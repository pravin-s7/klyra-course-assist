Instead:

                 klyra
                   │
          ┌────────┴────────┐
          │                 │
       generate            voice
          │                 │
          │          Gemini Live
          │                 │
          │          Voice Orchestrator
          │                 │
          └────────┬────────┘
                   ▼
              Klyra Core
                   │
                 CrewAI
                   │
             Qdrant / LLMs

Move the actual business logic out of the CLI command:

# klyra/core/generate.py

def generate_question_paper(spec):
    ...

Then:

# CLI

def generate_command(...):
    return generate_question_paper(spec)

and:

# Voice

def generate_question_paper_tool(...):
    return generate_question_paper(spec)

So CLI and voice share exactly the same execution code.

What the voice CLI could eventually feel like
$ klyra voice

Klyra: 🎙 Ready.

You: Create a question paper.

Klyra: Sure. What subject?

You: DBMS.

Klyra: Which units should I cover?

You: Two and three.

Klyra: What difficulty?

You: Medium.

Klyra: How many questions?

You: Ten.

Klyra:

  ┌───────────────────────────────────┐
  │ Question Paper                    │
  │                                   │
  │ Subject:     DBMS                 │
  │ Scope:       Units 2, 3           │
  │ Difficulty:  Medium               │
  │ Questions:   10                   │
  │                                   │
  │ Ready to generate?                │
  └───────────────────────────────────┘

You: Yes.

Klyra: Generating...

        ↓

      CrewAI
        ↓
      Qdrant
        ↓
    Generation
        ↓
    Validation

Klyra: Done. The question paper has
       been generated.

That's a much better UX than exposing 10 CLI flags.

And you can make it smarter later

Once the basic version works, the voice agent can understand:

"Make me something similar to last semester's DBMS paper, but a little harder and don't repeat any questions."

It could turn that into:

{
  "intent": "generate_question_paper",
  "reference": "last semester DBMS paper",
  "difficulty": "hard",
  "avoid_duplicates": true
}

Then your application resolves "last semester DBMS paper" through your own data/Qdrant layer.

The important architectural principle is:

LLM interprets the user's intent; Python decides whether the intent is sufficiently specified and whether execution is allowed.

That will also make your eventual web UI almost trivial. The same voice orchestrator can sit behind a WebSocket later; you won't have to redesign the CrewAI/Qdrant part.