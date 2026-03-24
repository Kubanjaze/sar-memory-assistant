"""
scripts/fetch_and_ingest_pmc.py
--------------------------------
End-to-end pipeline:
  1. Fetch JATS XML from NCBI EUtils for a PMC article
  2. Convert XML → PDF (same logic as paper-intelligence-agent/data/xml_to_pdf.py)
  3. Run Phase 03 extraction (extractor.py) to get compounds.json + sar_trends.json
  4. Run Phase 04 ingest to update the SAR memory store

Usage (from repo root):
    PYTHONUTF8=1 python scripts/fetch_and_ingest_pmc.py \
        --pmc PMC9583618 \
        --target "KRAS G12C" \
        --source "Lanman 2022 Accounts of Chemical Research"

    # Process multiple papers in one run:
    PYTHONUTF8=1 python scripts/fetch_and_ingest_pmc.py \
        --pmc PMC9583618 PMC10201555 PMC10577700 \
        --target "KRAS G12C"
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

# ── Windows UTF-8 fix ──────────────────────────────────────────────────────
if sys.platform == "win32":
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── .env loader ─────────────────────────────────────────────────────────────
_env = os.path.join(os.path.dirname(__file__), "..", ".env")
if os.path.isfile(_env):
    with open(_env) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())

# ── lazy imports (need venv) ─────────────────────────────────────────────────
import subprocess
import anthropic
import anyio
from fpdf import FPDF

# Phase 03 path (adjust if layout changes)
PHASE03_ROOT = Path(__file__).parent.parent.parent / "paper-intelligence-agent"
# Phase 04 root
PHASE04_ROOT = Path(__file__).parent.parent

NCBI_EFETCH = (
    "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    "?db=pmc&rettype=xml&id={pmc_id}"
)


# ── XML → PDF (from paper-intelligence-agent/data/xml_to_pdf.py) ────────────

def strip_ns(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def iter_text(elem, depth: int = 0) -> list[tuple[str, str]]:
    result = []
    tag = strip_ns(elem.tag)

    if elem.text and elem.text.strip():
        text = elem.text.strip()
        if tag == "article-title":
            result.append(("title", text))
        elif tag == "title":
            result.append(("heading", text))
        elif tag in ("label", "caption", "table-wrap-foot"):
            result.append(("caption", text))
        elif tag in ("p", "td", "th", "list-item", "def"):
            result.append(("body", text))
        elif tag not in (
            "ref", "element-citation", "mixed-citation",
            "pub-id", "year", "source", "volume", "fpage",
            "lpage", "issue", "person-group", "name", "surname", "given-names",
        ):
            result.append(("body", text))

    for child in elem:
        result.extend(iter_text(child, depth + 1))
        if child.tail and child.tail.strip():
            if depth == 0:
                result.append(("body", child.tail.strip()))

    return result


def build_pdf(segments: list[tuple[str, str]]) -> FPDF:
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    pdf.set_margins(20, 20, 20)
    seen: set[str] = set()

    for style, raw in segments:
        key = (style, raw[:80])
        if key in seen:
            continue
        seen.add(key)
        text = raw.encode("latin-1", errors="replace").decode("latin-1")

        if style == "title":
            pdf.set_font("Helvetica", "B", 14)
            pdf.multi_cell(0, 7, text)
            pdf.ln(3)
        elif style == "heading":
            pdf.set_font("Helvetica", "B", 11)
            pdf.multi_cell(0, 6, text)
            pdf.ln(2)
        elif style == "caption":
            pdf.set_font("Helvetica", "I", 9)
            pdf.multi_cell(0, 5, text)
            pdf.ln(1)
        else:
            pdf.set_font("Helvetica", "", 10)
            pdf.multi_cell(0, 5, text)
            pdf.ln(1)

    return pdf


def xml_to_pdf(xml_path: Path, pdf_path: Path) -> int:
    """Parse JATS XML and write PDF. Returns page count."""
    tree = ET.parse(str(xml_path))
    root = tree.getroot()
    segments = iter_text(root)
    pdf = build_pdf(segments)
    pdf.output(str(pdf_path))
    return pdf.page


# ── Python executable (this venv) ────────────────────────────────────────────

PYTHON = sys.executable


# ── Phase 03 extraction ──────────────────────────────────────────────────────

def run_phase03(pdf_path: Path, output_dir: Path, target: str, model: str) -> tuple[Path, Path]:
    """
    Run Phase 03 extraction via subprocess. Returns (compounds_path, sar_path).
    Using subprocess avoids sys.path conflicts between Phase 03 and Phase 04
    both having a src/ package.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    compounds_path = output_dir / "compounds.json"
    sar_path = output_dir / "sar_trends.json"

    cmd = [
        PYTHON, "main.py",
        "--paper", str(pdf_path),
        "--target", target,
        "--output", str(output_dir),
        "--model", model,
    ]
    result = subprocess.run(
        cmd,
        cwd=str(PHASE03_ROOT),
        capture_output=False,   # let stdout/stderr flow through
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    # Phase 03 may exit non-zero if the research agent (Phase B) fails —
    # e.g., Claude Code CLI not available. We only need Phase A output.
    # Succeed if the JSON extraction files exist, regardless of exit code.
    if not compounds_path.exists() or not sar_path.exists():
        raise RuntimeError(
            f"Phase 03 exited {result.returncode} and did not produce "
            "compounds.json / sar_trends.json"
        )
    if result.returncode != 0:
        print(f"        Note: Phase 03 exited {result.returncode} "
              "(research agent unavailable) — extraction files present, continuing.")
    return compounds_path, sar_path


# ── Phase 04 ingest ──────────────────────────────────────────────────────────

def run_phase04_ingest(
    compounds_path: Path,
    sar_path: Path,
    target: str,
    source: str,
    model: str,
    verbose: bool,
) -> None:
    """Run Phase 04 ingest via subprocess."""
    cmd = [
        PYTHON, "main.py",
        "--target", target,
        "--ingest", str(compounds_path), str(sar_path),
        "--source", source,
        "--model-ingest", model,
    ]
    if verbose:
        cmd.append("--verbose")

    result = subprocess.run(
        cmd,
        cwd=str(PHASE04_ROOT),
        capture_output=False,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Phase 04 ingest exited with code {result.returncode}")


# ── Main pipeline ────────────────────────────────────────────────────────────

def process_paper(
    pmc_id: str,
    target: str,
    source: str,
    model: str,
    verbose: bool,
    work_dir: Path,
    dry_run: bool = False,
) -> bool:
    """
    Full pipeline for one PMC paper.
    Returns True on success, False on error.
    """
    print(f"\n{'='*60}")
    print(f"Processing {pmc_id}")
    print(f"  Target : {target}")
    print(f"  Source : {source}")
    print(f"{'='*60}")

    paper_dir = work_dir / pmc_id
    paper_dir.mkdir(parents=True, exist_ok=True)

    xml_path = paper_dir / "paper_raw.xml"
    pdf_path = paper_dir / "paper.pdf"

    # ── Step 1: Download XML ──────────────────────────────────────────────
    url = NCBI_EFETCH.format(pmc_id=pmc_id)
    print(f"  [1/4] Downloading {url} ...")
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0 (research; mailto:research@example.com)"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            xml_bytes = resp.read()
        xml_path.write_bytes(xml_bytes)
        print(f"        Saved {len(xml_bytes)//1024} KB to {xml_path.name}")
    except Exception as exc:
        print(f"  ERROR downloading {pmc_id}: {exc}")
        return False

    # Quick sanity: check it looks like JATS XML
    if b"<article" not in xml_bytes[:2000]:
        print(f"  ERROR: response doesn't look like JATS XML — possible captcha/rate-limit")
        print(f"         First 200 chars: {xml_bytes[:200]}")
        return False

    # ── Step 2: XML → PDF ────────────────────────────────────────────────
    print(f"  [2/4] Converting XML → PDF ...")
    try:
        pages = xml_to_pdf(xml_path, pdf_path)
        size_kb = pdf_path.stat().st_size // 1024
        print(f"        Generated {pdf_path.name} ({size_kb} KB, {pages} pages)")
    except Exception as exc:
        print(f"  ERROR converting XML to PDF for {pmc_id}: {exc}")
        return False

    if dry_run:
        print("  [dry-run] Skipping Phase 03 + 04 steps.")
        return True

    # ── Step 3: Phase 03 extraction ──────────────────────────────────────
    print(f"  [3/4] Running Phase 03 extraction ...")
    output_dir = paper_dir / "extraction"
    try:
        compounds_path, sar_path = run_phase03(
            pdf_path=pdf_path,
            output_dir=output_dir,
            target=target,
            model=model,
        )
        print(f"        Wrote {compounds_path.name}, {sar_path.name}")
    except Exception as exc:
        print(f"  ERROR in Phase 03 extraction for {pmc_id}: {exc}")
        import traceback
        traceback.print_exc()
        return False

    # Check we got something useful
    try:
        compounds = json.loads(compounds_path.read_text())
        sar = json.loads(sar_path.read_text())
        if not compounds and not sar:
            print("  WARNING: extraction returned 0 compounds and 0 SAR trends — skipping ingest")
            return True
    except Exception:
        pass

    # ── Step 4: Phase 04 ingest ──────────────────────────────────────────
    print(f"  [4/4] Ingesting into Phase 04 memory (target={target}) ...")
    try:
        run_phase04_ingest(
            compounds_path=compounds_path,
            sar_path=sar_path,
            target=target,
            source=source,
            model=model,
            verbose=verbose,
        )
        print(f"        Ingest complete.")
    except Exception as exc:
        print(f"  ERROR in Phase 04 ingest for {pmc_id}: {exc}")
        import traceback
        traceback.print_exc()
        return False

    return True


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch PMC papers, extract SAR data, ingest into memory store",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--pmc", nargs="+", required=True,
                        help="One or more PMC IDs (e.g. PMC9583618)")
    parser.add_argument("--target", default="KRAS G12C",
                        help="Drug target name — used as memory namespace")
    parser.add_argument("--source", default=None,
                        help="Source label override. Defaults to PMC ID.")
    parser.add_argument("--model", default="claude-opus-4-6",
                        help="Claude model for extraction and ingest")
    parser.add_argument("--work-dir", default="scripts/pmc_work",
                        help="Directory to store downloaded XML, PDF, extraction outputs")
    parser.add_argument("--verbose", action="store_true",
                        help="Verbose tool call trace during ingest")
    parser.add_argument("--dry-run", action="store_true",
                        help="Download and convert XML/PDF only; skip API calls")
    parser.add_argument("--delay", type=float, default=3.0,
                        help="Seconds to wait between papers (NCBI rate limit courtesy)")
    args = parser.parse_args()

    if "ANTHROPIC_API_KEY" not in os.environ and not args.dry_run:
        print("ERROR: ANTHROPIC_API_KEY not set.", file=sys.stderr)
        sys.exit(1)

    work_dir = PHASE04_ROOT / args.work_dir
    work_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for i, pmc_id in enumerate(args.pmc):
        # Normalize
        pmc_id = pmc_id.strip()
        if not pmc_id.startswith("PMC"):
            pmc_id = "PMC" + pmc_id

        source = args.source or pmc_id

        if i > 0:
            print(f"\n  Waiting {args.delay}s (NCBI courtesy delay)...")
            time.sleep(args.delay)

        ok = process_paper(
            pmc_id=pmc_id,
            target=args.target,
            source=source,
            model=args.model,
            verbose=args.verbose,
            work_dir=work_dir,
            dry_run=args.dry_run,
        )
        results[pmc_id] = "OK" if ok else "FAILED"

    print(f"\n{'='*60}")
    print("Run summary:")
    for pmc_id, status in results.items():
        print(f"  {pmc_id}: {status}")
    print("="*60)

    if any(v == "FAILED" for v in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
