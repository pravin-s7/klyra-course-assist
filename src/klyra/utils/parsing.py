"""CLI-facing parsers that turn short, easy-to-type flags into unambiguous
instructions for the agents - so the LLM never has to guess what a range or
shorthand means.
"""
from __future__ import annotations


def parse_week_numbers(scope: str | None) -> list[int] | None:
    """Week numbers implied by a scope string. None means all weeks 1-12.

    Accepts: "all", "week_3", "3", "week_1-week_4", "1-4", "1,3,5",
    "1-3,5,7-9" (any mix of single weeks, ranges, and commas).
    """
    if scope is None:
        return None
        
    scope = scope.strip()
    if scope.lower() == "all":
        return None

    weeks: set[int] = set()
    for part in scope.split(","):
        part = part.strip().lower().replace("week_", "").replace("week", "").strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            start, end = int(start_s.strip()), int(end_s.strip())
            weeks.update(range(min(start, end), max(start, end) + 1))
        else:
            weeks.add(int(part))

    if not weeks:
        raise ValueError(
            f"Could not parse scope {scope!r}. Use e.g. 'all', 'week_3', 'week_1-week_4', '1,3,5'."
        )
    return sorted(weeks)


def parse_week_scope(scope: str) -> str:
    """Normalize a week scope into an explicit instruction string."""
    weeks = parse_week_numbers(scope)
    if weeks is None:
        return "all weeks (1-12)"
    if len(weeks) == 1:
        return f"week {weeks[0]}"
    return "weeks " + ", ".join(str(w) for w in weeks)


def parse_type_counts(spec: str) -> dict[str, int]:
    """'MCQ:3,MSQ:4,NAT:3' -> {'MCQ': 3, 'MSQ': 4, 'NAT': 3}"""
    counts: dict[str, int] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"Invalid --type-counts entry {part!r}, expected TYPE:COUNT (e.g. MCQ:3)")
        qtype, count_s = part.split(":", 1)
        qtype = qtype.strip().upper()
        count = int(count_s.strip())
        if count < 0:
            raise ValueError(f"Count for {qtype} must be >= 0, got {count}")
        counts[qtype] = count

    if not counts:
        raise ValueError("No question types found in --type-counts")
    return counts
