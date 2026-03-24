"""
src/deduplicator.py — Deduplication rules injected into the ingest system prompt.

Claude handles synonym resolution and semantic deduplication because these are
language understanding problems that string-matching code cannot solve reliably.
For example, "GDC-6036", "divarasib", and "compound 10" all refer to the same
molecule — only a model with chemistry knowledge can recognise that.

This module provides a single public function that returns the rules text so it
can be consistently injected into any ingest system prompt.
"""
from __future__ import annotations


def get_dedup_rules() -> str:
    """
    Return the deduplication and merging rules for the ingest system prompt.

    The returned string is designed to be included verbatim as a section inside
    a larger system prompt.
    """
    return """\
DEDUPLICATION AND MERGING RULES
================================

Before creating any new entry, use memory.view to read the relevant file and
check whether the compound or SAR trend already exists.

1. SYNONYM DETECTION — Treat different names for the same entity as duplicates.
   - Compound synonyms include: IUPAC names, INN names, code numbers (e.g.
     "GDC-6036"), development codes ("compound 10"), and trade names.
   - If a synonym match is found, DO NOT create a new section.  Instead, update
     the existing section with any new non-redundant data.

2. DUPLICATE HANDLING — If an entry for this entity already exists:
   - Use str_replace to merge NEW data (e.g. a new activity value from a
     different assay, a new page reference) into the existing section.
   - DO NOT duplicate facts or quotes that are already present.
   - Add a secondary source citation if the same fact appears in a new source.

3. CONTRADICTORY DATA — If two sources report conflicting values for the same
   property (e.g. IC50 = 12 nM vs. 45 nM for the same compound/assay):
   - Record BOTH values with their respective source citations.
   - Do not silently discard either value.
   - Add an [INCONSISTENCY] note to hypotheses.md documenting the discrepancy,
     the two sources, and possible explanations (e.g. different assay conditions).

4. NOVEL ENTRY — If the entity is genuinely new (no match found after thorough
   synonym check):
   - Use memory.insert to add a new "## " section in the appropriate file.

5. SAR TREND DEDUP — If the same structural feature is reported with the same
   direction in a new source:
   - Add the new evidence_quote and source citation as a bullet under the
     existing trend section rather than creating a duplicate "## " heading.
"""
