"""Identify sections and clause-level context without changing source wording."""

import re
from dataclasses import dataclass

from job_market.domain.enums import (
    MentionContext,
    QualificationLevel,
    RequirementContext,
    SectionKind,
)
from job_market.parsing.normalization import Span, Text, term_pattern, trim
from job_market.parsing.taxonomy import Taxonomy

_DECORATION = re.compile(r"^[^\w#]+")
_CONTACT = re.compile(
    r"^(?:[@]|https?://|(?:contact|e-?mail|контакт(?:на особа)?|контактное лицо) *:|"
    r"send (?:us )?(?:your )?(?:cv|resume)|apply (?:here|now)|"
    r"відгуки?\b|для відгуку\b|надішліть\b|отклик(?:и|нуться)?\b|"
    r"присылайте (?:резюме|cv)|резюме (?:на|в)\b)",
    re.I,
)
_CLAUSE = re.compile(
    r";+|(?<=[.!?]) +(?=[^\W\d_])|,? +(?:but|але|но) +|"
    r"(?<=required), +|(?<=a plus), +|(?<=обязательно), +|(?<=обов'язково), +|"
    r", +(?=(?:understanding|practical experience|proficiency|розуміння|"
    r"практичний досвід|володіння|понимание|практический опыт|владение)\b)",
    re.I,
)
_REQUIRED = re.compile(r"\b(?:must have|mandatory|required|обов'язков[оі]|обязательно)\b", re.I)
_OPTIONAL = re.compile(
    r"\b(?:not required|not mandatory|optional|необов'язково|необязательно|не обязательно)\b",
    re.I,
)
_PREFERRED = re.compile(
    r"\b(?:is preferred|preferred experience|would be a plus|will be a plus|"
    r"бажано|предпочтительно)\b|(?<!not )\bpreferred[.!]*$",
    re.I,
)
_NO_EXPERIENCE = re.compile(
    r"\b(?:no (?:prior |previous )?experience\b|without (?:prior )?experience\b|"
    r"experience\b[^;]{0,50}\b(?:not (?:required|needed)|is optional)\b|"
    r"без (?:попереднього )?досвіду\b|без опыта\b|"
    r"(?:досвід|опыт)\b[^;]{0,55}\b(?:не (?:потрібен|потрібний|вимагається|"
    r"требуется|обязателен))\b)",
    re.I,
)


@dataclass(frozen=True)
class Line:
    span: Span
    kind: SectionKind | None
    label: str | None


def undecorated(text: Text, span: Span) -> Span:
    match = _DECORATION.match(text.value(span))
    return span.subspan(match.end(), span.end - span.start) if match else span


def split_sections(text: Text, taxonomy: Taxonomy) -> tuple[Line, ...]:
    lines = []
    kind = None
    label = None
    for row in re.finditer(r"[^\n]+", text.normalized):
        span = trim(text, Span(*row.span()))
        if span.start == span.end:
            continue
        value = text.value(span)
        # Keep handles/links visible to the contact check.
        heading = undecorated(text, span) if not value.startswith("@") else span
        head = text.value(heading)
        recognized = False
        for section in taxonomy.sections:
            for term in section.headings:
                match = term_pattern(term).match(head)
                if not match:
                    continue
                tail = head[match.end() :]
                ending = re.match(r" *[*_]* *(?:: *[*_]* *|$)", tail)
                if ending is None:
                    continue
                kind = section.kind
                label = text.evidence(heading.subspan(*match.span())).text
                body = trim(text, heading.subspan(match.end() + ending.end(), len(head)))
                if body.start < body.end:
                    lines.append(Line(body, kind, label))
                recognized = True
                break
            if recognized:
                break
        if recognized:
            continue
        if _CONTACT.match(value) or _CONTACT.match(head):
            kind = SectionKind.CONTACTS
            label = None
        lines.append(Line(span, kind, label))
    return tuple(lines)


def clauses(text: Text, line: Line):
    value = text.value(line.span)
    start = 0
    for separator in _CLAUSE.finditer(value):
        part = trim(text, line.span.subspan(start, separator.start()))
        if part.start < part.end:
            yield part
        start = separator.end()
    part = trim(text, line.span.subspan(start, len(value)))
    if part.start < part.end:
        yield part


def context_for(value: str, line: Line, taxonomy: Taxonomy) -> MentionContext:
    if line.kind in {SectionKind.OFFER, SectionKind.CONTACTS}:
        return MentionContext.BACKGROUND_UNSPECIFIED
    if _PREFERRED.search(value) or any(
        term_pattern(cue).search(value) for cue in taxonomy.preference_cues
    ):
        return MentionContext.PREFERRED
    if _OPTIONAL.search(value):
        return MentionContext.BACKGROUND_UNSPECIFIED
    if _REQUIRED.search(value) and not _NO_EXPERIENCE.search(value):
        return MentionContext.REQUIRED
    return {
        SectionKind.REQUIRED: MentionContext.REQUIRED,
        SectionKind.PREFERRED: MentionContext.PREFERRED,
        SectionKind.RESPONSIBILITIES: MentionContext.RESPONSIBILITY,
    }.get(line.kind, MentionContext.BACKGROUND_UNSPECIFIED)


def requirement_context(context: MentionContext) -> RequirementContext:
    if context in {MentionContext.REQUIRED, MentionContext.PREFERRED}:
        return RequirementContext(context.value)
    return RequirementContext.UNSPECIFIED


def qualification_for(value: str, taxonomy: Taxonomy) -> QualificationLevel:
    matches = []
    for cue in taxonomy.qualification_cues:
        for term in cue.terms:
            if match := term_pattern(term).search(value):
                matches.append((match.start(), -len(match[0]), cue.level))
    if matches:
        return min(matches)[2]
    if re.search(r"\bfluent\b|\bвільн[оа]\b|\bсвободно\b", value, re.I):
        return QualificationLevel.PROFICIENCY
    if re.search(r"\b(?:experience (?:with|in|of)|досвід|опыт)\b", value, re.I):
        return QualificationLevel.PRACTICAL_EXPERIENCE
    return QualificationLevel.UNSPECIFIED


def no_experience(value: str) -> bool:
    return _NO_EXPERIENCE.search(value) is not None
