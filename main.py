"""
main.py — SAR Memory Assistant CLI entry point.

Phase 04 of the Claude API Learning Series.
Concepts: Memory tool (memory_20250818), prompt caching, cross-session persistence.

Usage
-----
Ingest Phase 03 outputs:
  python main.py --target KRAS \
      --ingest data/compounds.json data/sar_trends.json \
      --source "Example Paper 2026"

Query the memory store (streams answer):
  python main.py --target KRAS \
      --query "What structural features improve potency?" --verbose

Both modes in one invocation (ingest then query):
  python main.py --target KRAS \
      --ingest data/compounds.json data/sar_trends.json \
      --query "Summarise the potency trend findings"
"""
from __future__ import annotations

import sys

# ── Windows UTF-8 wrapper ─────────────────────────────────────────────────────
# Must be first — before any other imports — so all output is UTF-8 encoded.
if sys.platform == "win32":
    import io

    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── stdlib .env loader ────────────────────────────────────────────────────────
# Reads KEY=VALUE pairs from .env; environment variables always take precedence
# (os.environ.setdefault only sets if the key is absent).
# No external dependency — pure stdlib.

import os


def _load_dotenv(path: str = ".env") -> None:
    """Load KEY=VALUE lines from *path*; skip if the key is already set."""
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip surrounding quotes (both single and double)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key, value)


_load_dotenv()

# ── remaining imports (after .env is loaded) ──────────────────────────────────

import argparse
import json
from datetime import datetime
from pathlib import Path

from src.ingestor import run_ingest
from src.models import IngestInput
from src.querier import run_query


# ── CLI parser ────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sar-memory-assistant",
        description=(
            "SAR Memory Assistant — accumulates Structure-Activity Relationship\n"
            "knowledge across sessions for a given drug target.\n\n"
            "Modes:\n"
            "  --ingest  Read Phase 03 JSON outputs and update the memory store.\n"
            "  --query   Synthesise an answer from the memory store.\n\n"
            "Both modes may be combined in one invocation (ingest runs first)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # ── required ──────────────────────────────────────────────────────────────
    parser.add_argument(
        "--target",
        required=True,
        metavar="TARGET",
        help=(
            "Drug target name used as the memory namespace "
            "(e.g. KRAS, CETP, EGFR).  Special characters are normalised to "
            "underscores when creating the memory directory."
        ),
    )

    # ── ingest options ────────────────────────────────────────────────────────
    ingest_group = parser.add_argument_group("ingest options")
    ingest_group.add_argument(
        "--ingest",
        nargs="+",
        metavar="JSON_PATH",
        help=(
            "One or more JSON files to ingest (compounds.json and/or "
            "sar_trends.json from Phase 03).  Both files may be passed together."
        ),
    )
    ingest_group.add_argument(
        "--source",
        default=None,
        metavar="LABEL",
        help=(
            "Human-readable source label written into MEMORY.md for provenance "
            "(e.g. 'JMedChem 2026').  Defaults to the ingested filenames."
        ),
    )

    # ── query options ─────────────────────────────────────────────────────────
    query_group = parser.add_argument_group("query options")
    query_group.add_argument(
        "--query",
        default=None,
        metavar="QUESTION",
        help="Natural language question to synthesise against the memory store.",
    )
    query_group.add_argument(
        "--no-stream",
        action="store_true",
        default=False,
        help="Disable streaming; print the full answer at once.",
    )
    query_group.add_argument(
        "--output",
        default=None,
        metavar="PATH",
        help=(
            "Write the query answer to this file path (Markdown format). "
            "Parent directories are created automatically."
        ),
    )

    # ── model options ─────────────────────────────────────────────────────────
    model_group = parser.add_argument_group("model options")
    model_group.add_argument(
        "--model-ingest",
        default="claude-sonnet-4-6",
        metavar="MODEL_ID",
        help="Claude model ID for ingest mode. (default: claude-sonnet-4-6)",
    )
    model_group.add_argument(
        "--model-query",
        default="claude-sonnet-4-6",
        metavar="MODEL_ID",
        help="Claude model ID for query mode. (default: claude-sonnet-4-6)",
    )

    # ── storage ───────────────────────────────────────────────────────────────
    parser.add_argument(
        "--memory-dir",
        default="memory",
        metavar="DIR",
        help="Base directory for all per-target memory files. (default: memory/)",
    )

    # ── flags ─────────────────────────────────────────────────────────────────
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help=(
            "Print extra diagnostic output: tool call trace during ingest, "
            "assistant reasoning snippets, and cache hit statistics during queries."
        ),
    )

    return parser


# ── JSON file loader ──────────────────────────────────────────────────────────


def _load_json_file(path: str) -> dict[str, list[dict]]:
    """
    Load a JSON file and return a dict with 'compounds' and/or 'sar_trends' keys.

    Infers the data type from the filename, then from record keys, and finally
    defaults to 'compounds' with a warning if both heuristics fail.
    """
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)

    filename = Path(path).stem.lower()

    if isinstance(data, list):
        # 1. Filename heuristic
        if "compound" in filename:
            return {"compounds": data}
        if "sar" in filename or "trend" in filename:
            return {"sar_trends": data}

        # 2. Record key heuristic
        if data and isinstance(data[0], dict):
            keys = set(data[0].keys())
            if "activity_value" in keys or "compound_name" in keys:
                return {"compounds": data}
            if "structural_feature" in keys or "finding" in keys:
                return {"sar_trends": data}

        # 3. Fallback
        print(
            f"  Warning: cannot infer data type of {path!r} from filename or "
            "record keys; treating as compounds.  "
            "Rename the file to 'compounds.json' or 'sar_trends.json' for "
            "reliable auto-detection.",
            file=sys.stderr,
        )
        return {"compounds": data}

    if isinstance(data, dict):
        # Already has the expected top-level keys
        return data

    raise ValueError(
        f"Unexpected JSON structure in {path!r}: "
        f"expected a list or a dict, got {type(data).__name__}."
    )


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    # Must specify at least one action
    if not args.ingest and not args.query:
        parser.error("Specify at least one of --ingest or --query.")

    # API key check — fail early before any network calls
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "Error: ANTHROPIC_API_KEY is not set.\n\n"
            "Set it in your shell:\n"
            "  export ANTHROPIC_API_KEY=sk-ant-...\n\n"
            "Or create a .env file in the project root:\n"
            "  ANTHROPIC_API_KEY=sk-ant-...",
            file=sys.stderr,
        )
        sys.exit(1)

    # ── INGEST MODE ───────────────────────────────────────────────────────────
    if args.ingest:
        all_compounds: list[dict] = []
        all_sar_trends: list[dict] = []
        loaded_names: list[str] = []

        for fpath in args.ingest:
            if not os.path.isfile(fpath):
                print(f"Error: file not found: {fpath!r}", file=sys.stderr)
                sys.exit(1)

            try:
                data = _load_json_file(fpath)
            except (json.JSONDecodeError, ValueError) as exc:
                print(f"Error reading {fpath!r}: {exc}", file=sys.stderr)
                sys.exit(1)

            all_compounds.extend(data.get("compounds", []))
            all_sar_trends.extend(data.get("sar_trends", []))
            loaded_names.append(Path(fpath).name)

        source_label = args.source or ", ".join(loaded_names)
        ingest_date = datetime.now().strftime("%Y-%m-%d")

        ingest_input = IngestInput(
            compounds=all_compounds,
            sar_trends=all_sar_trends,
            source_label=source_label,
            ingest_date=ingest_date,
        )

        run_ingest(
            target=args.target,
            ingest_input=ingest_input,
            model=args.model_ingest,
            memory_base=args.memory_dir,
            verbose=args.verbose,
        )

    # ── QUERY MODE ────────────────────────────────────────────────────────────
    if args.query:
        if args.ingest:
            print()  # blank line between ingest output and query output

        print(f"Querying memory for target '{args.target}':")
        print(f"Q: {args.query}")
        print()

        answer = run_query(
            target=args.target,
            question=args.query,
            model=args.model_query,
            memory_base=args.memory_dir,
            stream=not args.no_stream,
            verbose=args.verbose,
        )

        # Optionally write answer to a file
        if args.output and answer:
            output_path = Path(args.output)
            output_path.parent.mkdir(parents=True, exist_ok=True)

            report_lines = [
                f"# SAR Query Report — {args.target}",
                "",
                f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"**Question:** {args.query}",
                "",
                "---",
                "",
                answer,
            ]
            output_path.write_text("\n".join(report_lines), encoding="utf-8")
            print(f"\n  Report written to: {output_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
