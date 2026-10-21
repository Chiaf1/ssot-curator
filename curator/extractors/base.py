"""Normalized document produced by the extractors (recipe §10)."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Section:
    """A citable location. `anchor` follows the single convention:
    p<n> pages, s-<slug> sections, msg-<hash> messages, r-<sheet>-<row> table rows."""
    anchor: str
    label: str              # human readable: "p. 3", "sheet «Sheet1», row 12"
    text: str
    group: str = ""         # tables: sheet (chunks never mix sheets)
    header: str = ""        # tables: header row, repeated as context in chunks


@dataclass
class ExtractedDoc:
    source_type: str
    sections: list[Section]
    metadata: dict = field(default_factory=dict)
    confidence: str = "high"
    warnings: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n\n".join(s.text for s in self.sections)

    @property
    def has_text(self) -> bool:
        return any(s.text.strip() for s in self.sections)


class ExtractionError(Exception):
    pass
