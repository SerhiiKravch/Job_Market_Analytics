"""Extract evidenced keyword occurrences without inferring list relationships."""

import re
from dataclasses import dataclass

from job_market.domain.contracts import KeywordMention
from job_market.domain.enums import SectionKind
from job_market.parsing.normalization import Span, Text, term_pattern
from job_market.parsing.sections import Line, clauses, context_for, no_experience, qualification_for
from job_market.parsing.taxonomy import KeywordDefinition, Taxonomy

_NON_CONTENT = re.compile(
    r"https?://\S+|www\.\S+|\S+@\S+\.\S+|(?<!\w)[@#][\w.-]+|"
    r"(?<![\w.])(?:[\w-]+\.)+(?:com|org|net|io|dev|edu|ua|ru|pl)\b(?:/\S*)?",
    re.I,
)
_SHORT_SUFFIX = re.compile(r"^[-.&][^\W\d_]")
_NEGATED = re.compile(r"\b(?:no|not|без|не) +$", re.I)


@dataclass(frozen=True)
class KeywordHit:
    span: Span
    definition: KeywordDefinition


def keyword_hits(value: str, taxonomy: Taxonomy) -> tuple[KeywordHit, ...]:
    ignored = [match.span() for match in _NON_CONTENT.finditer(value)]
    found = []
    for keyword in taxonomy.keywords:
        if not keyword.active:
            continue
        aliases = [(alias, False, False) for alias in keyword.aliases]
        aliases.extend(
            (alias.term, alias.case_sensitive, len(alias.term) <= 2)
            for alias in keyword.contextual_aliases
            if any(term_pattern(cue).search(value) for cue in alias.requires_any)
        )
        for alias, case_sensitive, short in aliases:
            for match in term_pattern(alias, case_sensitive).finditer(value):
                lo, hi = match.span()
                if any(lo < end and hi > start for start, end in ignored):
                    continue
                if _NEGATED.search(value[:lo]):
                    continue
                if short and _SHORT_SUFFIX.match(value[hi:]):
                    continue
                found.append(KeywordHit(Span(lo, hi), keyword))
    end = 0
    matches = []
    for hit in sorted(found, key=lambda hit: (hit.span.start, -hit.span.end)):
        if hit.span.start >= end:
            matches.append(hit)
            end = hit.span.end
    return tuple(matches)


def extract_keywords(
    text: Text, lines: tuple[Line, ...], taxonomy: Taxonomy
) -> tuple[KeywordMention, ...]:
    result = []
    for line in lines:
        if line.kind in {SectionKind.OFFER, SectionKind.CONTACTS}:
            continue
        for clause in clauses(text, line):
            value = text.value(clause)
            if no_experience(value):
                continue
            evidence = text.evidence(clause)
            context = context_for(value, line, taxonomy)
            qualification = qualification_for(value, taxonomy)
            for hit in keyword_hits(value, taxonomy):
                term = text.evidence(clause.subspan(hit.span.start, hit.span.end))
                result.append(
                    KeywordMention(
                        keyword_code=hit.definition.canonical_code,
                        original_term=term.text,
                        context=context,
                        qualification_level=qualification,
                        section_label=line.label,
                        evidence_text=evidence.text,
                        start_offset=evidence.start_offset,
                        end_offset=evidence.end_offset,
                    )
                )
    return tuple(result)
