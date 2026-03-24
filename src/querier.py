"""
src/querier.py — Query mode: load memory, cache it, stream a synthesised answer.

Key API concepts demonstrated
------------------------------
- Prompt caching (cache_control): the large stable memory body is marked with
  ``cache_control: {type: "ephemeral"}``.  First query: full price + 25% write
  premium on the cached block.  Subsequent queries within 5 min: the cached
  block costs only 10% of normal input token rate.  As memory grows, savings
  compound significantly.

- Cache placement: ``cache_control`` goes on the *memory body* content block
  (the large, stable one), NOT on the system prompt (too small to matter) and
  NOT on the question block (changes every call).

- Streaming: ``client.messages.stream()`` with ``text_stream`` iterator for
  immediate terminal output.  ``get_final_message()`` is called inside the
  context manager to safely retrieve usage stats for cache hit reporting.

Query mode is strictly memory-grounded: no web search, no external lookups.
All answers must be supported by facts present in the memory files.
"""
from __future__ import annotations

import sys

from anthropic import Anthropic

from src.memory_store import MEMORY_FILES, load_memory_state

# ── system prompt ─────────────────────────────────────────────────────────────

_QUERY_SYSTEM_PROMPT = """\
You are a drug discovery analyst synthesising SAR knowledge from a structured
memory store.

Answer questions based ONLY on information present in the provided memory files.
Do not speculate, invent compounds, or add information that is not in the memory.
If the memory does not contain enough information to fully answer the question,
say so explicitly and describe what information is missing.

When citing evidence, reference the source label and page number as recorded
in the memory entries.
"""


# ── memory context builder ────────────────────────────────────────────────────


def _build_memory_context(target: str, memory_files: dict[str, str]) -> str:
    """
    Concatenate all four memory files into a single context block.

    A clear file-header separator makes it easy for the model to distinguish
    where each file's content begins and ends.
    """
    parts: list[str] = [f"# SAR Memory Store — {target}\n"]
    for fname in MEMORY_FILES:
        content = memory_files.get(fname, "").strip()
        parts.append(f"\n---\n## {fname}\n")
        parts.append(content if content else "(empty)")
    return "\n".join(parts)


# ── public entry point ────────────────────────────────────────────────────────


def run_query(
    target: str,
    question: str,
    model: str = "claude-sonnet-4-6",
    memory_base: str = "memory",
    stream: bool = True,
    verbose: bool = False,
) -> str:
    """
    Query the memory store and return the synthesised answer as a string.

    The full answer is also printed to stdout (streamed unless ``stream=False``).

    Parameters
    ----------
    target:       Drug target name — used to locate the memory directory.
    question:     Natural language question to answer from memory.
    model:        Claude model ID to use.
    memory_base:  Base directory for memory files.
    stream:       If True, stream tokens to stdout as they arrive.
    verbose:      If True, print cache hit statistics after the response.

    Returns
    -------
    The complete answer text (empty string if no memory was found).
    """
    client = Anthropic()

    memory_files = load_memory_state(target, memory_base)

    # Guard: friendly message if no memory exists for this target yet
    if all(v.strip() == "" for v in memory_files.values()):
        msg = (
            f"No memory found for target '{target}'. "
            "Run --ingest first to populate the memory store."
        )
        print(msg)
        return ""

    memory_body = _build_memory_context(target, memory_files)

    # ── message construction ──────────────────────────────────────────────────
    # Two content blocks in the user turn:
    #   Block 1 — memory body (large, stable) → CACHED
    #   Block 2 — the question (small, changes every call) → NOT cached
    #
    # Why not cache the system prompt?  It is only ~50 tokens; the savings
    # would be negligible.  The memory body grows with every ingest and
    # benefits most from caching.
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": memory_body,
                    "cache_control": {"type": "ephemeral"},  # ← cache breakpoint
                },
                {
                    "type": "text",
                    "text": f"Question: {question}",  # not cached — unique per call
                },
            ],
        }
    ]

    result_parts: list[str] = []

    # ── streaming path ────────────────────────────────────────────────────────
    if stream:
        with client.messages.stream(
            model=model,
            max_tokens=4096,
            system=_QUERY_SYSTEM_PROMPT,
            messages=messages,
        ) as s:
            for text in s.text_stream:
                print(text, end="", flush=True)
                result_parts.append(text)

            # get_final_message() must be called inside the context manager
            if verbose:
                final = s.get_final_message()
                _print_cache_stats(final.usage)

        print()  # trailing newline after streamed content

    # ── non-streaming path ────────────────────────────────────────────────────
    else:
        response = client.messages.create(
            model=model,
            max_tokens=4096,
            system=_QUERY_SYSTEM_PROMPT,
            messages=messages,
        )
        for block in response.content:
            if getattr(block, "type", None) == "text":
                text = getattr(block, "text", "")
                print(text)
                result_parts.append(text)

        if verbose:
            _print_cache_stats(response.usage)

    return "".join(result_parts)


# ── helpers ───────────────────────────────────────────────────────────────────


def _print_cache_stats(usage: object) -> None:
    """Print prompt-caching statistics to stderr if data is available."""
    total = getattr(usage, "input_tokens", 0) or 0
    cached = getattr(usage, "cache_read_input_tokens", 0) or 0
    written = getattr(usage, "cache_creation_input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0

    if total == 0:
        return

    hit_rate = cached / total if total > 0 else 0.0

    print(
        f"\n  [cache] input={total} tokens | "
        f"cache_read={cached} ({hit_rate:.0%}) | "
        f"cache_write={written} | "
        f"output={output}",
        file=sys.stderr,
    )
