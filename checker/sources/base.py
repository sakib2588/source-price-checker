"""Base result type shared by all source extractors."""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SourceResult:
    url: str
    platform: str             # "ebay" | "amazon" | "unknown"
    title: Optional[str] = None
    price_gbp: Optional[float] = None
    available: Optional[bool] = None   # True=in-stock, False=OOS, None=unknown
    availability_text: str = ""
    error: Optional[str] = None
    note: str = ""            # how the price was chosen, e.g. "variant from link"
    state: str = ""           # "removed" | "ended" | "shell" when the page itself says so
    offers: list = field(default_factory=list)   # [{id, label, price_gbp, available}] per variation
    variation_label: Optional[str] = None        # the linked variation's name, e.g. "Pink / 8oz"

    @property
    def ok(self) -> bool:
        return self.error is None

    def to_dict(self) -> dict:
        return {
            "platform":          self.platform,
            "url":               self.url,
            "title":             self.title or "",
            "price_gbp":         self.price_gbp,
            "available":         self.available,
            "availability_text": self.availability_text,
            "error":             self.error or "",
        }
