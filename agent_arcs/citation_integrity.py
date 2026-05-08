from __future__ import annotations

import re


REFERENCE_HEADING_RE = re.compile(r"^#{1,6}\s+References\s*$", re.IGNORECASE | re.MULTILINE)
REFERENCE_LINE_RE = re.compile(r"^(?:\[(\d+)\]|(\d+)\.)\s+", re.MULTILINE)
INLINE_CITATION_RE = re.compile(r"\[(\d+)\]")


def _format_ints(values: list[int], *, limit: int = 20) -> str:
    if len(values) <= limit:
        return ", ".join(str(value) for value in values)
    shown = ", ".join(str(value) for value in values[:limit])
    return f"{shown}, ... (+{len(values) - limit} more)"


def citation_integrity_failure(article_text: str) -> str | None:
    """Return a blocking citation-numbering issue, if one is detectable.

    This is intentionally structural only. FACT still handles substantive source
    validation; this gate catches deterministic numbering defects before eval.
    """
    references_match = REFERENCE_HEADING_RE.search(article_text)
    if not references_match:
        inline_numbers = [int(match) for match in INLINE_CITATION_RE.findall(article_text)]
        if inline_numbers:
            return "Report has inline numeric citations but no References section."
        return None

    body_text = article_text[: references_match.start()]
    references_text = article_text[references_match.end() :]
    inline_numbers = [int(match) for match in INLINE_CITATION_RE.findall(body_text)]
    reference_numbers = [int(bracketed or dotted) for bracketed, dotted in REFERENCE_LINE_RE.findall(references_text)]

    if inline_numbers and not reference_numbers:
        return "Report has inline numeric citations but no numbered references."
    if reference_numbers and not inline_numbers:
        return "Report has numbered references but no inline numeric citations before References."
    if not inline_numbers and not reference_numbers:
        return None

    seen: set[int] = set()
    duplicates: list[int] = []
    for number in reference_numbers:
        if number in seen and number not in duplicates:
            duplicates.append(number)
        seen.add(number)
    if duplicates:
        return f"Duplicate reference numbers in References section: [{_format_ints(sorted(duplicates))}]."

    max_reference = max(reference_numbers)
    missing = sorted(set(range(1, max_reference + 1)) - set(reference_numbers))
    if missing:
        return f"Missing reference numbers in References section: [{_format_ints(missing)}]."

    unresolved = sorted(set(inline_numbers) - set(reference_numbers))
    if unresolved:
        return f"Inline citations without matching references: [{_format_ints(unresolved)}]."

    return None
