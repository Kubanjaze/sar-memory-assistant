"""
src/memory_store.py — File I/O layer for the per-target memory store.

Security contract
-----------------
All file access is restricted to the target's memory directory.  Any path that
resolves outside that directory, or that references a filename not in
MEMORY_FILES, is rejected with a descriptive error string.  This prevents path
traversal attacks from malformed model outputs.

Supported memory tool commands
-------------------------------
  view        — return current file content (empty-file message if not present)
  create      — create / overwrite a file with supplied content
  str_replace  — atomic replace of one occurrence of old_str with new_str
  insert      — append new content after an anchor string, before an anchor
                string, or at end-of-file if no anchor is supplied
  delete      — remove a file from the memory directory
  rename      — rename a file within the memory directory
"""
from __future__ import annotations

import re
from pathlib import Path

# The only filenames that may be read or written.
MEMORY_FILES: list[str] = [
    "MEMORY.md",
    "compounds.md",
    "sar_trends.md",
    "hypotheses.md",
]


# ---------------------------------------------------------------------------
# Directory helpers
# ---------------------------------------------------------------------------


def normalize_target_dir(target: str) -> str:
    """
    Convert a free-form target name to a safe directory name.

    Rules: keep word characters, hyphens, and dots; collapse runs of
    underscores; strip leading/trailing underscores.

    Examples
    --------
    "KRAS G12C"  -> "KRAS_G12C"
    "CETP (HDL)" -> "CETP__HDL_"  -> "CETP_HDL"
    """
    safe = re.sub(r"[^\w\-\.]", "_", target)
    safe = re.sub(r"_+", "_", safe)
    return safe.strip("_") or "default"


def get_memory_dir(target: str, base: str = "memory") -> Path:
    """Return the resolved memory directory path for *target*, creating it if needed."""
    path = Path(base) / normalize_target_dir(target)
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def load_memory_state(target: str, base: str = "memory") -> dict[str, str]:
    """
    Load every file in MEMORY_FILES for *target*.

    Returns a dict mapping filename -> content (empty string if the file does
    not yet exist).
    """
    memory_dir = get_memory_dir(target, base)
    state: dict[str, str] = {}
    for fname in MEMORY_FILES:
        fpath = memory_dir / fname
        state[fname] = fpath.read_text(encoding="utf-8") if fpath.exists() else ""
    return state


def write_memory_file(
    target: str, filename: str, content: str, base: str = "memory"
) -> None:
    """Write *content* to *filename* in the target's memory directory."""
    if filename not in MEMORY_FILES:
        raise ValueError(
            f"Filename {filename!r} is not in MEMORY_FILES. "
            f"Allowed: {MEMORY_FILES}"
        )
    memory_dir = get_memory_dir(target, base)
    (memory_dir / filename).write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Internal safety check
# ---------------------------------------------------------------------------


def _resolve_safe_path(memory_dir: Path, path: str | None) -> Path:
    """
    Resolve *path* to an absolute Path inside *memory_dir*.

    Accepts paths like:
      - "compounds.md"
      - "/memories/compounds.md"   (leading /memories/ is stripped)
      - "./compounds.md"

    Raises ValueError if:
      - The extracted filename is not in MEMORY_FILES.
      - The resolved path escapes memory_dir (traversal).
    """
    if not path:
        raise ValueError("'path' is required but was empty or None.")

    # Strip any leading protocol-like prefix the model might add
    # e.g. "/memories/compounds.md" -> "compounds.md"
    cleaned = path.strip()
    for prefix in ("/memories/", "memories/", "./memories/"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break

    # Extract only the filename (discard any remaining directory components)
    fname = Path(cleaned).name

    if fname not in MEMORY_FILES:
        raise ValueError(
            f"File {fname!r} is not in the allowed list {MEMORY_FILES}. "
            "Only the four designated memory files may be accessed."
        )

    resolved = (memory_dir / fname).resolve()

    # Confirm we haven't escaped the memory directory
    try:
        resolved.relative_to(memory_dir)
    except ValueError:
        raise ValueError(
            f"Path traversal detected: {path!r} resolves outside the memory directory."
        )

    return resolved


# ---------------------------------------------------------------------------
# Memory tool command dispatcher
# ---------------------------------------------------------------------------


def execute_memory_command(
    target: str,
    base: str,
    command: str,
    path: str | None = None,
    **kwargs: object,
) -> str:
    """
    Execute a memory tool command on behalf of Claude.

    This is the client-side handler for the ``memory_20250818`` tool.  Claude
    emits ``tool_use`` blocks; the caller unpacks ``block.input`` into this
    function and returns the result string as a ``tool_result`` content.

    Parameters
    ----------
    target:  Drug target name (used to locate the memory directory).
    base:    Base directory for all memory files (default "memory").
    command: One of: view, create, str_replace, insert, delete, rename.
    path:    Target filename (e.g. "compounds.md").
    **kwargs: Command-specific arguments (see below per command).

    Returns
    -------
    A human-readable string describing the outcome, suitable for use as
    ``tool_result`` content.  Errors are returned as strings (not raised) so
    Claude can observe the error and retry.
    """
    memory_dir = get_memory_dir(target, base)

    try:
        # ------------------------------------------------------------------ #
        # view — return file content                                          #
        # ------------------------------------------------------------------ #
        if command == "view":
            fpath = _resolve_safe_path(memory_dir, path)
            if not fpath.exists():
                return (
                    f"(File {fpath.name!r} does not exist yet. "
                    "Use 'create' to initialise it.)"
                )
            return fpath.read_text(encoding="utf-8")

        # ------------------------------------------------------------------ #
        # create — create or overwrite a file                                 #
        # ------------------------------------------------------------------ #
        elif command == "create":
            fpath = _resolve_safe_path(memory_dir, path)
            content: str = str(kwargs.get("content", ""))
            fpath.write_text(content, encoding="utf-8")
            return f"Created {fpath.name} ({len(content)} chars)."

        # ------------------------------------------------------------------ #
        # str_replace — replace the first occurrence of old_str with new_str  #
        # ------------------------------------------------------------------ #
        elif command == "str_replace":
            fpath = _resolve_safe_path(memory_dir, path)
            if not fpath.exists():
                return (
                    f"Error: {fpath.name!r} does not exist. "
                    "Use 'create' to initialise it first."
                )
            old_str: str = str(kwargs.get("old_str", ""))
            new_str: str = str(kwargs.get("new_str", ""))
            if not old_str:
                return "Error: 'old_str' is required for str_replace and must not be empty."

            current = fpath.read_text(encoding="utf-8")
            if old_str not in current:
                # Return helpful context so Claude can self-correct
                lines_preview = current[:400].replace("\n", "\\n")
                return (
                    f"Error: old_str not found verbatim in {fpath.name!r}. "
                    "str_replace is whitespace-sensitive. "
                    f"File begins with: {lines_preview!r}... "
                    "Use 'view' to read the exact current content before retrying."
                )

            updated = current.replace(old_str, new_str, 1)
            fpath.write_text(updated, encoding="utf-8")
            return f"str_replace succeeded in {fpath.name!r}."

        # ------------------------------------------------------------------ #
        # insert — add content relative to an anchor string, or at end        #
        # ------------------------------------------------------------------ #
        elif command == "insert":
            fpath = _resolve_safe_path(memory_dir, path)
            # Auto-create empty file if it doesn't exist
            if not fpath.exists():
                fpath.write_text("", encoding="utf-8")

            insert_content: str = str(kwargs.get("content", ""))
            after: str | None = kwargs.get("after", None)   # type: ignore[assignment]
            before: str | None = kwargs.get("before", None)  # type: ignore[assignment]

            current = fpath.read_text(encoding="utf-8")

            if after is not None:
                after_str = str(after)
                if after_str not in current:
                    return (
                        f"Error: 'after' anchor {after_str!r} not found in {fpath.name!r}. "
                        "Use 'view' to check the current content."
                    )
                updated = current.replace(after_str, after_str + "\n" + insert_content, 1)

            elif before is not None:
                before_str = str(before)
                if before_str not in current:
                    return (
                        f"Error: 'before' anchor {before_str!r} not found in {fpath.name!r}. "
                        "Use 'view' to check the current content."
                    )
                updated = current.replace(before_str, insert_content + "\n" + before_str, 1)

            else:
                # Default: append at end-of-file
                updated = current.rstrip("\n") + "\n\n" + insert_content + "\n"

            fpath.write_text(updated, encoding="utf-8")
            return f"insert succeeded in {fpath.name!r}."

        # ------------------------------------------------------------------ #
        # delete — remove a file                                              #
        # ------------------------------------------------------------------ #
        elif command == "delete":
            fpath = _resolve_safe_path(memory_dir, path)
            if not fpath.exists():
                return f"File {fpath.name!r} does not exist; nothing to delete."
            fpath.unlink()
            return f"Deleted {fpath.name!r}."

        # ------------------------------------------------------------------ #
        # rename — rename within the same memory directory                    #
        # ------------------------------------------------------------------ #
        elif command == "rename":
            fpath = _resolve_safe_path(memory_dir, path)
            new_path_raw: str = str(kwargs.get("new_path", ""))
            if not new_path_raw:
                return "Error: 'new_path' is required for rename."
            new_fpath = _resolve_safe_path(memory_dir, new_path_raw)
            if not fpath.exists():
                return f"Error: {fpath.name!r} does not exist."
            fpath.rename(new_fpath)
            return f"Renamed {fpath.name!r} -> {new_fpath.name!r}."

        # ------------------------------------------------------------------ #
        # unknown command                                                      #
        # ------------------------------------------------------------------ #
        else:
            return (
                f"Error: Unknown command {command!r}. "
                "Supported commands: view, create, str_replace, insert, delete, rename."
            )

    except ValueError as exc:
        return f"Error (validation): {exc}"
    except OSError as exc:
        return f"Error (filesystem): {exc}"
