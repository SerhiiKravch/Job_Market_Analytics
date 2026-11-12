"""Normalize matching text while retaining a map to original Unicode offsets."""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from job_market.domain.contracts import Evidence

_PUNCTUATION = str.maketrans(
    {
        **dict.fromkeys("‐‑‒–—―−", "-"),
        **dict.fromkeys("’‘ʼ", "'"),
    }
)


@dataclass(frozen=True)
class Span:
    start: int
    end: int

    def subspan(self, start: int, end: int) -> "Span":
        return Span(self.start + start, self.start + end)


@dataclass(frozen=True)
class Text:
    original: str = field(repr=False)
    normalized: str = field(repr=False)
    offsets: tuple[tuple[int, int], ...] = field(repr=False)

    @classmethod
    def from_source(cls, original: str) -> "Text":
        chars = []
        offsets = []
        i = 0
        while i < len(original):
            j = i + 1
            if original[i : j + 1] == "\r\n":
                j += 1
                value = "\n"
            elif original[i] in "\r\n\u2028\u2029":
                value = "\n"
            else:
                # Normalize a base character with its combining marks together.
                while j < len(original) and unicodedata.category(original[j]).startswith("M"):
                    j += 1
                value = unicodedata.normalize("NFKC", original[i:j]).translate(_PUNCTUATION)
                value = "".join(" " if c.isspace() else c for c in value)
            chars.append(value)
            offsets.extend([(i, j)] * len(value))
            i = j
        return cls(original, "".join(chars), tuple(offsets))

    def value(self, span: Span) -> str:
        return self.normalized[span.start : span.end]

    def evidence(self, span: Span, *, taxonomy_code: str | None = None) -> Evidence:
        start = self.offsets[span.start][0]
        end = self.offsets[span.end - 1][1]
        return Evidence(
            text=self.original[start:end],
            start_offset=start,
            end_offset=end,
            taxonomy_code=taxonomy_code,
        )


@lru_cache(maxsize=2048)
def term_pattern(term: str, case_sensitive: bool = False) -> re.Pattern[str]:
    normalized = Text.from_source(term).normalized
    body = r" +".join(map(re.escape, normalized.split()))
    # Avoid matching inside longer words, handles, or language tokens such as C++.
    return re.compile(r"(?<![\w@#])" + body + r"(?![\w+#])", 0 if case_sensitive else re.I)


def trim(text: Text, span: Span) -> Span:
    value = text.value(span)
    start = len(value) - len(value.lstrip())
    end = len(value.rstrip())
    return span.subspan(start, max(start, end))
