"""
src/ingestor.py — Ingest mode: update memory files via the Memory tool loop.

Key API concepts demonstrated
------------------------------
- ``memory_20250818`` tool: Claude emits ``tool_use`` blocks with commands
  (view, create, str_replace, insert …).  This app executes them on local
  markdown files and returns ``tool_result`` blocks referencing the same
  ``tool_use_id``.  This is the foundational client-side tool loop pattern.

- ``betas=["context-management-2025-06-27"]``: enables server-side context
  compaction during long tool loops.  When a large ingest has many tool calls
  the message history grows; compaction summarises earlier turns so the context
  window does not overflow.

Two implementation paths
------------------------
A) Manual tool loop (always available — primary teaching implementation).
   Exposes every step: API call → inspect blocks → execute locally → append
   tool_result → loop.  MAX_TOOL_ITERATIONS guards against infinite loops.

B) tool_runner via BetaAbstractMemoryTool (SDK helper, if available).
   The SDK handles the loop automatically; you supply a subclass that routes
   each command to execute_memory_command.  The manual loop is kept as the
   default because it is more transparent for learning.

To switch to the tool_runner path, set USE_TOOL_RUNNER = True at module level
(requires anthropic >= 0.40 with BetaAbstractMemoryTool).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from anthropic import Anthropic

from src.deduplicator import get_dedup_rules
from src.memory_store import MEMORY_FILES, execute_memory_command, load_memory_state
from src.models import IngestInput

# ── tuneable constants ────────────────────────────────────────────────────────

# Hard cap on tool iterations per ingest session to prevent infinite loops.
MAX_TOOL_ITERATIONS: int = 30

# Set True to attempt the tool_runner SDK path (requires newer anthropic SDK).
# Falls back to manual loop automatically if BetaAbstractMemoryTool is absent.
USE_TOOL_RUNNER: bool = False

# ── optional SDK import ───────────────────────────────────────────────────────

try:
    from anthropic.lib.tools import BetaAbstractMemoryTool  # type: ignore[import]

    _TOOL_RUNNER_AVAILABLE = True
except ImportError:
    _TOOL_RUNNER_AVAILABLE = False
    BetaAbstractMemoryTool = object  # type: ignore[assignment,misc]

# ── system prompt ─────────────────────────────────────────────────────────────

_INGEST_SYSTEM_PROMPT_TEMPLATE = """\
You are a medicinal chemistry knowledge curator.  Your job is to maintain a
structured SAR (Structure-Activity Relationship) memory store for a specific
drug target.

You have access to four memory files:
  MEMORY.md       — index: sources ingested, record counts, last updated date
  compounds.md    — one "## compound_name" section per compound
  sar_trends.md   — one "## [direction] structural_feature" section per trend
  hypotheses.md   — inferred patterns, open questions, [INCONSISTENCY] notes

TRUTHFULNESS RULES — CRITICAL
------------------------------
- Write ONLY facts that appear in the provided JSON data or in the metadata
  fields (target, source_label, ingest_date).
- DO NOT invent, estimate, or infer properties that are absent from the input.
  If a field is missing (e.g. no SMILES, no species), OMIT that line entirely.
  Do not write "N/A", "unknown", or placeholder values for absent fields.
- Preserve source_quote and evidence_quote verbatim (quote them with > prefix).

CITATION FORMAT
---------------
End every compound/trend section with:
  Source: {{source_label}}, {{ingest_date}}, p.{{page_reference}}
Omit the page number if page_reference is absent from the input record.

MEMORY FILE FORMAT
------------------
compounds.md entry template (omit any line whose data is absent):
  ## compound_name
  - **Target:** assay_target
  - **Activity:** activity_value activity_unit (activity_type, assay_type)
  - **Species:** assay_species
  - **SMILES:** smiles
  - > "source_quote"
  - Source: source_label, ingest_date, p.page_reference

sar_trends.md entry template (omit magnitude if absent):
  ## [direction] structural_feature
  - **Direction:** direction
  - **Magnitude:** magnitude
  - > "evidence_quote"
  - Source: source_label, ingest_date, p.page_reference

WORKFLOW
--------
1. Use memory.view to read existing files before modifying them.
2. Apply the deduplication rules below.
3. Use memory.str_replace to update existing sections (preferred for edits).
4. Use memory.insert to add new sections at the end of a file.
5. Use memory.create to initialise a file that does not yet exist.
6. After all updates, always update MEMORY.md last with the new source and
   updated record counts.

{dedup_rules}
"""


# ── prompt builder ────────────────────────────────────────────────────────────


def _build_ingest_prompt(
    target: str,
    ingest_input: IngestInput,
    current_memory: dict[str, str],
) -> str:
    """
    Build the user-turn message for an ingest session.

    The current memory state is included inline so Claude can check for
    duplicates without spending extra tool calls on view commands.
    """
    lines: list[str] = [
        f"**Target:** {target}",
        f"**Source label:** {ingest_input.source_label}",
        f"**Ingest date:** {ingest_input.ingest_date}",
        "",
        f"## Compounds to ingest ({len(ingest_input.compounds)} records)",
        "```json",
        json.dumps(ingest_input.compounds, indent=2, ensure_ascii=False),
        "```",
        "",
        f"## SAR trends to ingest ({len(ingest_input.sar_trends)} records)",
        "```json",
        json.dumps(ingest_input.sar_trends, indent=2, ensure_ascii=False),
        "```",
        "",
        "## Current memory state (for deduplication reference)",
    ]

    for fname in MEMORY_FILES:
        content = current_memory.get(fname, "")
        lines.append(f"\n### {fname}")
        lines.append(content.strip() if content.strip() else "(empty — not yet initialised)")

    lines += [
        "",
        "Please update the memory files with the data above.",
        "Process all compounds first, then all SAR trends, then update hypotheses",
        "if patterns emerge, and finally update MEMORY.md with the new source.",
        "Remember: omit any field that is absent from the input records.",
    ]

    return "\n".join(lines)


# ── manual tool loop (Path A — primary) ──────────────────────────────────────


def _run_ingest_manual(
    target: str,
    ingest_input: IngestInput,
    model: str,
    memory_base: str,
    verbose: bool,
) -> dict[str, Any]:
    """
    Core ingest implementation using an explicit tool loop.

    Pattern:
      while not done:
          response = client.beta.messages.create(...)
          for each tool_use block with name=="memory":
              result = execute_memory_command(...)   # local file operation
              append tool_result referencing tool_use_id
          send tool_results back as next user turn
    """
    client = Anthropic()

    current_memory = load_memory_state(target, memory_base)

    system_prompt = _INGEST_SYSTEM_PROMPT_TEMPLATE.format(
        dedup_rules=get_dedup_rules()
    )
    user_prompt = _build_ingest_prompt(target, ingest_input, current_memory)

    messages: list[dict[str, Any]] = [{"role": "user", "content": user_prompt}]

    tool_call_count = 0
    files_modified: set[str] = set()
    iteration = 0

    while iteration < MAX_TOOL_ITERATIONS:
        iteration += 1

        response = client.beta.messages.create(
            betas=["context-management-2025-06-27"],
            model=model,
            max_tokens=4096,
            system=system_prompt,
            messages=messages,
            tools=[{"type": "memory_20250818", "name": "memory"}],
        )

        # Append assistant response to history (preserves compaction blocks)
        messages.append({"role": "assistant", "content": response.content})

        # Print any assistant text in verbose mode
        if verbose:
            for block in response.content:
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "").strip()
                    if text:
                        # Truncate long explanations in terminal output
                        preview = text[:300] + ("…" if len(text) > 300 else "")
                        print(f"    [assistant] {preview}", file=sys.stderr)

        if response.stop_reason == "end_turn":
            break

        # Collect memory tool_use blocks and execute them locally
        tool_results: list[dict[str, Any]] = []

        for block in response.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if getattr(block, "name", None) != "memory":
                continue

            tool_call_count += 1
            raw_input: dict[str, Any] = dict(block.input)  # type: ignore[arg-type]
            cmd = raw_input.get("command", "unknown")
            path = raw_input.get("path", "")

            if verbose:
                print(f"    [memory.{cmd}] {path}", file=sys.stderr)

            result = execute_memory_command(
                target=target,
                base=memory_base,
                **raw_input,
            )

            # Track which files were modified (write commands only)
            if cmd in ("create", "str_replace", "insert", "delete", "rename") and path:
                files_modified.add(Path(path).name)

            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,  # type: ignore[attr-defined]
                    "content": result,
                }
            )

        if not tool_results:
            # stop_reason was tool_use but no memory blocks were found — exit
            break

        messages.append({"role": "user", "content": tool_results})

    if iteration >= MAX_TOOL_ITERATIONS:
        print(
            f"  Warning: reached MAX_TOOL_ITERATIONS ({MAX_TOOL_ITERATIONS}). "
            "Some records may not have been written.",
            file=sys.stderr,
        )

    return {"tool_calls": tool_call_count, "files_modified": sorted(files_modified)}


# ── tool_runner path (Path B — optional) ─────────────────────────────────────

# This section is intentionally kept short.  To enable it, set
# USE_TOOL_RUNNER = True above and ensure anthropic >= 0.40 is installed.
#
# The BetaAbstractMemoryTool subclass below routes each SDK-dispatched command
# to execute_memory_command so the same safety logic applies.


class _TargetMemoryTool(BetaAbstractMemoryTool):  # type: ignore[misc]
    """Memory tool backend that routes commands to execute_memory_command."""

    def __init__(self, target: str, memory_base: str) -> None:
        self._target = target
        self._base = memory_base

    def _dispatch(self, command: str, **kwargs: Any) -> str:
        return execute_memory_command(
            target=self._target, base=self._base, command=command, **kwargs
        )

    def view(self, command: Any) -> str:  # type: ignore[override]
        return self._dispatch("view", **dict(command))

    def create(self, command: Any) -> str:  # type: ignore[override]
        return self._dispatch("create", **dict(command))

    def str_replace(self, command: Any) -> str:  # type: ignore[override]
        return self._dispatch("str_replace", **dict(command))

    def insert(self, command: Any) -> str:  # type: ignore[override]
        return self._dispatch("insert", **dict(command))

    def delete(self, command: Any) -> str:  # type: ignore[override]
        return self._dispatch("delete", **dict(command))

    def rename(self, command: Any) -> str:  # type: ignore[override]
        return self._dispatch("rename", **dict(command))


def _run_ingest_tool_runner(
    target: str,
    ingest_input: IngestInput,
    model: str,
    memory_base: str,
    verbose: bool,
) -> dict[str, Any]:
    """
    Ingest using the SDK tool_runner helper (Path B).

    Requires: anthropic >= 0.40 with BetaAbstractMemoryTool.
    The SDK manages the loop; we just iterate over messages.
    """
    client = Anthropic()
    current_memory = load_memory_state(target, memory_base)

    system_prompt = _INGEST_SYSTEM_PROMPT_TEMPLATE.format(
        dedup_rules=get_dedup_rules()
    )
    user_prompt = _build_ingest_prompt(target, ingest_input, current_memory)

    memory_tool = _TargetMemoryTool(target=target, memory_base=memory_base)

    runner = client.beta.messages.tool_runner(  # type: ignore[attr-defined]
        betas=["context-management-2025-06-27"],
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
        tools=[memory_tool],
    )

    msg_count = 0
    for message in runner:
        msg_count += 1
        if verbose:
            for block in message.content:
                if getattr(block, "type", None) == "text":
                    text = getattr(block, "text", "").strip()
                    if text:
                        print(f"    [assistant] {text[:300]}", file=sys.stderr)

    return {"tool_calls": msg_count, "files_modified": []}


# ── public entry point ────────────────────────────────────────────────────────


def run_ingest(
    target: str,
    ingest_input: IngestInput,
    model: str = "claude-sonnet-4-6",
    memory_base: str = "memory",
    verbose: bool = False,
) -> dict[str, Any]:
    """
    Run ingest mode: update the per-target memory files.

    Chooses between the manual loop (Path A) and the SDK tool_runner (Path B)
    based on USE_TOOL_RUNNER and SDK availability.

    Returns
    -------
    dict with keys:
      tool_calls      — number of tool_use blocks processed
      files_modified  — list of filenames that were written (manual loop only)
    """
    print(
        f"  Ingesting {len(ingest_input.compounds)} compounds and "
        f"{len(ingest_input.sar_trends)} SAR trends into '{target}' memory...",
        file=sys.stderr,
    )

    use_runner = USE_TOOL_RUNNER and _TOOL_RUNNER_AVAILABLE

    if use_runner:
        print("  [path B] Using SDK tool_runner.", file=sys.stderr)
        summary = _run_ingest_tool_runner(target, ingest_input, model, memory_base, verbose)
    else:
        if USE_TOOL_RUNNER and not _TOOL_RUNNER_AVAILABLE:
            print(
                "  [path B] BetaAbstractMemoryTool not found in installed SDK; "
                "falling back to manual loop.",
                file=sys.stderr,
            )
        print("  [path A] Using manual tool loop.", file=sys.stderr)
        summary = _run_ingest_manual(target, ingest_input, model, memory_base, verbose)

    modified = summary.get("files_modified") or []
    print(
        f"  Ingest complete. "
        f"Tool calls: {summary['tool_calls']}. "
        f"Files modified: {modified if modified else 'none (check verbose output)'}.",
        file=sys.stderr,
    )
    return summary
