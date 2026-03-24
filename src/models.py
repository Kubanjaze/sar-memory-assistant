"""
src/models.py — Pydantic schemas for SAR Memory Assistant.
"""
from __future__ import annotations

from pydantic import BaseModel


class IngestInput(BaseModel):
    """All data for a single ingest session."""

    compounds: list[dict]
    """Compound activity records loaded from Phase 03 compounds.json."""

    sar_trends: list[dict]
    """SAR trend records loaded from Phase 03 sar_trends.json."""

    source_label: str
    """Human-readable provenance string written into MEMORY.md (e.g. 'JMedChem 2026')."""

    ingest_date: str
    """ISO date of this ingest (YYYY-MM-DD), used in citations."""


class MemoryFileState(BaseModel):
    """Snapshot of all memory files for a target at a point in time."""

    target: str
    """Drug target name as supplied by the user (e.g. 'KRAS')."""

    memory_dir: str
    """Absolute path to the target's memory directory on disk."""

    files: dict[str, str]
    """Mapping of filename -> content for each file in MEMORY_FILES."""


class QueryRequest(BaseModel):
    """Input contract for the query mode."""

    target: str
    question: str
    memory_state: MemoryFileState
    model: str = "claude-sonnet-4-6"
