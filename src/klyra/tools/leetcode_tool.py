"""Look up public LeetCode problem metadata as inspiration for `klyra code`.

Returns titles, tags, difficulty, and (when available) a truncated public
statement so the creator can write a *new* similar problem. Never copy a
LeetCode title, statement, examples, or numbers into generated output.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from html.parser import HTMLParser
from typing import Type

from crewai.tools import BaseTool
from pydantic import BaseModel, Field

LEETCODE_GRAPHQL = "https://leetcode.com/graphql"
LEETCODE_ALL = "https://leetcode.com/api/problems/all/"
_HTTP_TIMEOUT = 8

# Public problem names + tags only (no statements). Used when the live
# LeetCode API is unreachable so the agent still has concept-matched inspiration.
_CATALOG: list[dict[str, object]] = [
    {"title": "Two Sum", "slug": "two-sum", "difficulty": "Easy", "tags": ["array", "hash-table"]},
    {"title": "Valid Anagram", "slug": "valid-anagram", "difficulty": "Easy", "tags": ["hash-table", "string", "sorting"]},
    {"title": "Contains Duplicate", "slug": "contains-duplicate", "difficulty": "Easy", "tags": ["array", "hash-table"]},
    {"title": "Group Anagrams", "slug": "group-anagrams", "difficulty": "Medium", "tags": ["array", "hash-table", "string"]},
    {"title": "Longest Substring Without Repeating Characters", "slug": "longest-substring-without-repeating-characters", "difficulty": "Medium", "tags": ["hash-table", "string", "sliding-window"]},
    {"title": "Maximum Subarray", "slug": "maximum-subarray", "difficulty": "Medium", "tags": ["array", "divide-and-conquer", "dynamic-programming"]},
    {"title": "Best Time to Buy and Sell Stock", "slug": "best-time-to-buy-and-sell-stock", "difficulty": "Easy", "tags": ["array", "dynamic-programming"]},
    {"title": "Valid Parentheses", "slug": "valid-parentheses", "difficulty": "Easy", "tags": ["string", "stack"]},
    {"title": "Merge Two Sorted Lists", "slug": "merge-two-sorted-lists", "difficulty": "Easy", "tags": ["linked-list", "recursion"]},
    {"title": "Reverse Linked List", "slug": "reverse-linked-list", "difficulty": "Easy", "tags": ["linked-list", "recursion"]},
    {"title": "Linked List Cycle", "slug": "linked-list-cycle", "difficulty": "Easy", "tags": ["hash-table", "linked-list", "two-pointers"]},
    {"title": "Middle of the Linked List", "slug": "middle-of-the-linked-list", "difficulty": "Easy", "tags": ["linked-list", "two-pointers"]},
    {"title": "Binary Search", "slug": "binary-search", "difficulty": "Easy", "tags": ["array", "binary-search"]},
    {"title": "Search Insert Position", "slug": "search-insert-position", "difficulty": "Easy", "tags": ["array", "binary-search"]},
    {"title": "Climbing Stairs", "slug": "climbing-stairs", "difficulty": "Easy", "tags": ["math", "dynamic-programming", "memoization"]},
    {"title": "Fibonacci Number", "slug": "fibonacci-number", "difficulty": "Easy", "tags": ["math", "dynamic-programming", "recursion", "memoization"]},
    {"title": "Pow(x, n)", "slug": "powx-n", "difficulty": "Medium", "tags": ["math", "recursion"]},
    {"title": "Subsets", "slug": "subsets", "difficulty": "Medium", "tags": ["array", "backtracking", "bit-manipulation"]},
    {"title": "Permutations", "slug": "permutations", "difficulty": "Medium", "tags": ["array", "backtracking"]},
    {"title": "Combination Sum", "slug": "combination-sum", "difficulty": "Medium", "tags": ["array", "backtracking"]},
    {"title": "Number of Islands", "slug": "number-of-islands", "difficulty": "Medium", "tags": ["array", "dfs", "bfs", "union-find", "matrix"]},
    {"title": "Invert Binary Tree", "slug": "invert-binary-tree", "difficulty": "Easy", "tags": ["tree", "dfs", "bfs", "binary-tree"]},
    {"title": "Maximum Depth of Binary Tree", "slug": "maximum-depth-of-binary-tree", "difficulty": "Easy", "tags": ["tree", "dfs", "bfs", "binary-tree"]},
    {"title": "Same Tree", "slug": "same-tree", "difficulty": "Easy", "tags": ["tree", "dfs", "bfs", "binary-tree"]},
    {"title": "Validate Binary Search Tree", "slug": "validate-binary-search-tree", "difficulty": "Medium", "tags": ["tree", "dfs", "binary-search-tree", "binary-tree"]},
    {"title": "Binary Tree Inorder Traversal", "slug": "binary-tree-inorder-traversal", "difficulty": "Easy", "tags": ["stack", "tree", "dfs", "binary-tree"]},
    {"title": "Kth Largest Element in an Array", "slug": "kth-largest-element-in-an-array", "difficulty": "Medium", "tags": ["array", "divide-and-conquer", "sorting", "heap"]},
    {"title": "Top K Frequent Elements", "slug": "top-k-frequent-elements", "difficulty": "Medium", "tags": ["array", "hash-table", "heap"]},
    {"title": "Move Zeroes", "slug": "move-zeroes", "difficulty": "Easy", "tags": ["array", "two-pointers"]},
    {"title": "Container With Most Water", "slug": "container-with-most-water", "difficulty": "Medium", "tags": ["array", "two-pointers", "greedy"]},
    {"title": "3Sum", "slug": "3sum", "difficulty": "Medium", "tags": ["array", "two-pointers", "sorting"]},
    {"title": "Trapping Rain Water", "slug": "trapping-rain-water", "difficulty": "Hard", "tags": ["array", "two-pointers", "dynamic-programming", "stack"]},
    {"title": "Min Stack", "slug": "min-stack", "difficulty": "Medium", "tags": ["stack", "design"]},
    {"title": "Implement Queue using Stacks", "slug": "implement-queue-using-stacks", "difficulty": "Easy", "tags": ["stack", "design", "queue"]},
    {"title": "Evaluate Reverse Polish Notation", "slug": "evaluate-reverse-polish-notation", "difficulty": "Medium", "tags": ["array", "math", "stack"]},
    {"title": "Word Search", "slug": "word-search", "difficulty": "Medium", "tags": ["array", "string", "backtracking", "matrix"]},
    {"title": "Coin Change", "slug": "coin-change", "difficulty": "Medium", "tags": ["array", "dynamic-programming", "bfs"]},
    {"title": "House Robber", "slug": "house-robber", "difficulty": "Medium", "tags": ["array", "dynamic-programming"]},
    {"title": "Unique Paths", "slug": "unique-paths", "difficulty": "Medium", "tags": ["math", "dynamic-programming", "combinatorics"]},
    {"title": "Edit Distance", "slug": "edit-distance", "difficulty": "Medium", "tags": ["string", "dynamic-programming"]},
    {"title": "Sort Colors", "slug": "sort-colors", "difficulty": "Medium", "tags": ["array", "two-pointers", "sorting"]},
    {"title": "Merge Intervals", "slug": "merge-intervals", "difficulty": "Medium", "tags": ["array", "sorting"]},
    {"title": "Insert Interval", "slug": "insert-interval", "difficulty": "Medium", "tags": ["array"]},
    {"title": "Rotate Array", "slug": "rotate-array", "difficulty": "Medium", "tags": ["array", "math", "two-pointers"]},
    {"title": "Product of Array Except Self", "slug": "product-of-array-except-self", "difficulty": "Medium", "tags": ["array", "prefix-sum"]},
    {"title": "Flatten Nested List Iterator", "slug": "flatten-nested-list-iterator", "difficulty": "Medium", "tags": ["stack", "tree", "depth-first-search", "design"]},
    {"title": "Pascal's Triangle", "slug": "pascals-triangle", "difficulty": "Easy", "tags": ["array", "dynamic-programming"]},
    {"title": "Reverse String", "slug": "reverse-string", "difficulty": "Easy", "tags": ["two-pointers", "string"]},
    {"title": "Valid Palindrome", "slug": "valid-palindrome", "difficulty": "Easy", "tags": ["two-pointers", "string"]},
]

_CONCEPT_TAGS = (
    ("hash map", "hash-table"),
    ("hashmap", "hash-table"),
    ("dictionary", "hash-table"),
    ("hash table", "hash-table"),
    ("hash", "hash-table"),
    ("linked list", "linked-list"),
    ("linked-list", "linked-list"),
    ("two pointer", "two-pointers"),
    ("two-pointer", "two-pointers"),
    ("sliding window", "sliding-window"),
    ("binary search", "binary-search"),
    ("dynamic programming", "dynamic-programming"),
    ("dynamic-programming", "dynamic-programming"),
    ("memoization", "memoization"),
    ("recursion", "recursion"),
    ("backtracking", "backtracking"),
    ("binary tree", "binary-tree"),
    ("binary search tree", "binary-search-tree"),
    ("bst", "binary-search-tree"),
    ("stack", "stack"),
    ("queue", "queue"),
    ("heap", "heap"),
    ("graph", "graph"),
    ("dfs", "dfs"),
    ("bfs", "bfs"),
    ("string", "string"),
    ("array", "array"),
    ("matrix", "matrix"),
    ("sorting", "sorting"),
    ("greedy", "greedy"),
    ("tree", "tree"),
)

_INSPIRATION_BANNER = (
    "INSPIRATION ONLY - write a NEW original problem. Never copy a LeetCode "
    "title, statement, examples, constraints, or numbers. Similar concept/"
    "pattern, different scenario."
)

_GRAPHQL_LIST = """
query problemsetQuestionList($categorySlug: String, $limit: Int, $skip: Int, $filters: QuestionListFilterInput) {
  problemsetQuestionList: questionList(
    categorySlug: $categorySlug
    limit: $limit
    skip: $skip
    filters: $filters
  ) {
    questions: data {
      title
      titleSlug
      difficulty
      topicTags { name slug }
    }
  }
}
"""

_GRAPHQL_ONE = """
query questionData($titleSlug: String!) {
  question(titleSlug: $titleSlug) {
    title
    titleSlug
    difficulty
    content
    topicTags { name slug }
  }
}
"""


class _StripHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self._parts)).strip()


def _strip_html(html: str) -> str:
    parser = _StripHTML()
    parser.feed(html)
    return parser.text()


def _http_json(url: str, payload: dict | None = None, timeout: int = _HTTP_TIMEOUT) -> dict:
    body = None if payload is None else json.dumps(payload).encode()
    headers = {
        "User-Agent": "Klyra/0.1 (educational problem-setter; +https://github.com)",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Referer": "https://leetcode.com/problemset/",
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _tags_from_query(query: str) -> list[str]:
    q = query.lower()
    tags: list[str] = []
    for phrase, tag in _CONCEPT_TAGS:
        if phrase in q and tag not in tags:
            tags.append(tag)
    return tags


def _format_problem(item: dict, statement: str | None = None) -> str:
    slug = item.get("slug") or item.get("titleSlug") or ""
    title = item.get("title") or slug
    difficulty = item.get("difficulty") or "?"
    tags = item.get("tags") or []
    if tags and isinstance(tags[0], dict):
        tags = [t.get("slug") or t.get("name") for t in tags]
    url = f"https://leetcode.com/problems/{slug}/" if slug else ""
    line = f"- {title} [{difficulty}] tags={', '.join(str(t) for t in tags)} {url}".strip()
    if statement:
        clipped = statement[:700] + ("…" if len(statement) > 700 else "")
        return f"{line}\n  excerpt: {clipped}"
    return line


def _catalog_search(query: str, difficulty: str | None, limit: int) -> list[dict]:
    tags = set(_tags_from_query(query))
    tokens = [t for t in re.split(r"[^a-z0-9]+", query.lower()) if len(t) > 2]
    scored: list[tuple[int, dict]] = []
    want_diff = (difficulty or "").capitalize() or None
    for item in _CATALOG:
        item_tags = {str(t) for t in item["tags"]}  # type: ignore[index]
        hay = f"{item['title']} {item['slug']} {' '.join(item_tags)}".lower()
        score = 3 * len(tags & item_tags) + sum(1 for tok in tokens if tok in hay)
        if want_diff and str(item["difficulty"]).lower() != want_diff.lower():
            score -= 2
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored[:limit]]


def search_leetcode_problems(
    query: str,
    difficulty: str | None = None,
    limit: int = 10,
    title_slug: str | None = None,
) -> str:
    """Look up LeetCode problems. Live API first, local catalog if that fails."""
    query = (query or "").strip()
    title_slug = (title_slug or "").strip() or None
    if title_slug:
        live = _fetch_one(title_slug)
        if live:
            return _INSPIRATION_BANNER + "\n" + live
        catalog = next((p for p in _CATALOG if p["slug"] == title_slug), None)
        if catalog:
            return _INSPIRATION_BANNER + "\n" + _format_problem(catalog)
        return f"No LeetCode problem found for slug {title_slug!r}."

    if not query:
        return "Pass concepts, a subject name, PYQ wording from --notes, or a title_slug."

    live = _fetch_list(query, difficulty, limit)
    if live:
        return _INSPIRATION_BANNER + "\n" + "\n".join(live)

    catalog = _catalog_search(query, difficulty, limit)
    if not catalog:
        return (
            f"No matching LeetCode problems for {query!r} "
            "(live lookup unavailable and catalog had no tag/keyword hit). "
            "Write an original problem on that concept anyway."
        )
    header = _INSPIRATION_BANNER + "\n(live LeetCode lookup unavailable; using local concept catalog)\n"
    return header + "\n".join(_format_problem(item) for item in catalog)


def _fetch_list(query: str, difficulty: str | None, limit: int) -> list[str] | None:
    tags = _tags_from_query(query)
    filters: dict = {"searchKeywords": query[:80]}
    if tags:
        filters["tags"] = tags[:3]
    if difficulty:
        filters["difficulty"] = difficulty.upper()
    try:
        data = _http_json(
            LEETCODE_GRAPHQL,
            {
                "query": _GRAPHQL_LIST,
                "variables": {
                    "categorySlug": "",
                    "skip": 0,
                    "limit": limit,
                    "filters": filters,
                },
            },
        )
        questions = (
            data.get("data", {}).get("problemsetQuestionList", {}).get("questions") or []
        )
        if not questions:
            return None
        out = []
        for q in questions[:limit]:
            out.append(
                _format_problem(
                    {
                        "title": q.get("title"),
                        "slug": q.get("titleSlug"),
                        "difficulty": q.get("difficulty"),
                        "tags": q.get("topicTags") or [],
                    }
                )
            )
        return out
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, OSError):
        return None


def _fetch_one(title_slug: str) -> str | None:
    try:
        data = _http_json(
            LEETCODE_GRAPHQL,
            {"query": _GRAPHQL_ONE, "variables": {"titleSlug": title_slug}},
        )
        q = (data.get("data") or {}).get("question")
        if not q:
            return None
        statement = _strip_html(q.get("content") or "")
        return _format_problem(
            {
                "title": q.get("title"),
                "slug": q.get("titleSlug") or title_slug,
                "difficulty": q.get("difficulty"),
                "tags": q.get("topicTags") or [],
            },
            statement=statement or None,
        )
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, KeyError, OSError):
        return None


class SearchLeetCodeInput(BaseModel):
    query: str = Field(
        default="",
        description="Concepts, subject name, or wording from --notes / a PYQ. "
        "Used to find related LeetCode problems.",
    )
    difficulty: str | None = Field(
        default=None, description="Optional: Easy, Medium, or Hard"
    )
    limit: int = Field(default=10, ge=1, le=20)
    title_slug: str | None = Field(
        default=None,
        description="Optional: a specific LeetCode slug (e.g. two-sum) to inspect more closely",
    )


class SearchLeetCodeTool(BaseTool):
    name: str = "search_leetcode"
    description: str = (
        "Look up public LeetCode problems matching a concept, subject, or "
        "wording from --notes / a PYQ. Use this when --leetcode-similar is on, "
        "or when notes ask for LeetCode-style problems. Results are inspiration "
        "only - write a NEW problem; never copy title, examples, or numbers. "
        "Pass title_slug to inspect one problem more closely."
    )
    args_schema: Type[BaseModel] = SearchLeetCodeInput

    def _run(
        self,
        query: str = "",
        difficulty: str | None = None,
        limit: int = 10,
        title_slug: str | None = None,
    ) -> str:
        return search_leetcode_problems(
            query=query, difficulty=difficulty, limit=limit, title_slug=title_slug
        )
