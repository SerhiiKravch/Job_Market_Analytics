"""Find explicit job titles and their supported role and seniority mappings."""

import re
from dataclasses import dataclass

from job_market.domain.enums import SectionKind, Seniority
from job_market.parsing.normalization import Span, Text, term_pattern, trim
from job_market.parsing.sections import Line, undecorated
from job_market.parsing.taxonomy import RoleDefinition, Taxonomy

_HIRING = re.compile(
    r"^(?:(?:we(?: are|'re)? +)?(?:hiring|looking (?:for|to hire)|seeking)|"
    r"we seek|join (?:our team|us) as|шукаємо|шукаєм|потріб(?:ен|на|ні)|"
    r"ищем|нанимаем|требу(?:ется|ются))\b *[:\-]? *",
    re.I,
)
_LABEL = re.compile(
    r"^(?:vacancy|job(?: title)?|role|position|вакансія|вакансия|посада) *[:-] *", re.I
)
_ITEM = re.compile(r"^(?:\d{1,3}[.)] *|[•▪◦] *)")
_LEADING_WORDS = re.compile(
    r"^(?:(?:a|an|for|our|experienced|talented|на посаду|на позицию|досвідчен(?:ого|у|ий|а)|"
    r"опытн(?:ого|ую|ый|ая)|сильн(?:ого|у|ую)) +)+",
    re.I,
)
_SENIORITY = {
    "intern": Seniority.INTERN,
    "стажер": Seniority.INTERN,
    "стажёр": Seniority.INTERN,
    "trainee": Seniority.TRAINEE,
    "junior": Seniority.JUNIOR,
    "jr": Seniority.JUNIOR,
    "middle": Seniority.MIDDLE,
    "mid-level": Seniority.MIDDLE,
    "senior": Seniority.SENIOR,
    "sr": Seniority.SENIOR,
    "lead": Seniority.LEAD,
    "staff": Seniority.STAFF,
    "principal": Seniority.PRINCIPAL,
    "провідний": Seniority.LEAD,
    "ведущий": Seniority.LEAD,
}
_SENIOR_TERM = "|".join(map(re.escape, sorted(_SENIORITY, key=len, reverse=True)))
_SENIOR_PREFIX = re.compile(rf"^(?P<level>{_SENIOR_TERM}) +", re.I)
_SENIOR_SUFFIX = re.compile(rf"^ +(?P<level>{_SENIOR_TERM})(?!\w)", re.I)
_SENIOR_END = re.compile(rf"\b(?P<level>{_SENIOR_TERM})$", re.I)
_ALTERNATIVE = re.compile(r"^ *(?:/|\bor\b|\bабо\b|\bили\b) *", re.I)
_SEPARATE = re.compile(r"^ *(?:,|\band\b|\bта\b|\bі\b|\bи\b) *", re.I)
_GENERIC_TITLE = re.compile(r"[\w+#.]+(?:[ \-]+[\w+#.]+){1,6}")
_JOB_NOUN = re.compile(
    r"\b(?:manager|developer|engineer|analyst|designer|specialist|consultant|lead|"
    r"менеджер\w*|розробник\w*|разработчик\w*|інженер\w*|инженер\w*|аналітик\w*|аналитик\w*)\b",
    re.I,
)
_TITLE_TAIL = re.compile(
    r"^ *(?:[*_.!‼️]*$|[|(–—\-]|(?:full[- ]time|part[- ]time|fully remote|remote|hybrid)\b)",
    re.I,
)


@dataclass(frozen=True)
class RoleHit:
    span: Span
    definition: RoleDefinition | None
    seniority: Seniority | None = None
    seniority_span: Span | None = None
    context_span: Span | None = None


@dataclass(frozen=True)
class Title:
    span: Span
    line: Line
    roles: tuple[RoleHit, ...]
    mode: str
    listed: bool
    independent: bool = False

    @property
    def signature(self) -> frozenset[str]:
        return frozenset(role.definition.code for role in self.roles if role.definition is not None)


def _support(text: Text, lines: tuple[Line, ...], cues: tuple[str, ...]) -> Span | None:
    for line in lines:
        if line.kind in {SectionKind.OFFER, SectionKind.CONTACTS}:
            continue
        if any(term_pattern(cue).search(text.value(line.span)) for cue in cues):
            return line.span
    return None


def _role_at(text: Text, span: Span, taxonomy: Taxonomy, lines: tuple[Line, ...]) -> RoleHit | None:
    value = text.value(span)
    prefix = _SENIOR_PREFIX.match(value)
    pos = prefix.end() if prefix else 0
    matches = []
    for role in taxonomy.roles:
        for alias in role.aliases:
            if match := term_pattern(alias).match(value, pos):
                matches.append((match.end(), role, None, True))
        for alias in role.contextual_aliases:
            if match := term_pattern(alias.term, alias.case_sensitive).match(value, pos):
                supporting = _support(text, lines, alias.requires_any)
                matches.append((match.end(), role, supporting, supporting is not None))
    if not matches:
        return None
    end, role, supporting, established = max(matches, key=lambda item: (item[0], item[3]))
    suffix = _SENIOR_SUFFIX.match(value[end:])
    internal = _SENIOR_END.search(value[pos:end])
    seniority = None
    seniority_span = None
    if prefix:
        seniority = _SENIORITY[prefix["level"].lower()]
        seniority_span = span.subspan(*prefix.span("level"))
    elif internal:
        seniority = _SENIORITY[internal["level"].lower()]
        seniority_span = span.subspan(pos + internal.start("level"), end)
    elif suffix:
        seniority = _SENIORITY[suffix["level"].lower()]
        seniority_span = span.subspan(end + suffix.start("level"), end + suffix.end("level"))
        end += suffix.end()
    return RoleHit(
        span.subspan(0, end),
        role if established else None,
        seniority,
        seniority_span,
        supporting,
    )


def _generic_hit(text: Text, span: Span) -> RoleHit | None:
    value = text.value(span).rstrip("!.")
    if not _GENERIC_TITLE.fullmatch(value) or not _JOB_NOUN.search(value):
        return None
    seniority = _SENIOR_PREFIX.match(value) or _SENIOR_END.search(value)
    return RoleHit(
        span.subspan(0, len(value)),
        None,
        _SENIORITY[seniority["level"].lower()] if seniority else None,
        span.subspan(*seniority.span("level")) if seniority else None,
    )


def find_titles(text: Text, lines: tuple[Line, ...], taxonomy: Taxonomy) -> tuple[Title, ...]:
    titles = []
    for line in lines:
        raw = text.value(line.span)
        bullet = raw.startswith(("•", "▪", "◦"))
        span = undecorated(text, line.span)
        value = text.value(span)
        item = _ITEM.match(value)
        if item:
            span = span.subspan(item.end(), len(value))
            value = text.value(span)
        label, hiring = _LABEL.match(value), _HIRING.match(value)
        follows_numbered_title = item and any(
            title.listed and title.line.kind is None for title in titles
        )
        if line.kind is not None and not (label or follows_numbered_title):
            continue
        mode = "label" if label else "hiring" if hiring else "heading"
        marker = label or hiring
        if marker:
            span = span.subspan(marker.end(), len(value))
            if lead := _LEADING_WORDS.match(text.value(span)):
                span = span.subspan(lead.end(), span.end - span.start)
        if span.start == span.end:
            continue

        first = _role_at(text, span, taxonomy, lines)
        if first is None and marker:
            first = _generic_hit(text, span)
        if first is None:
            continue

        roles = [first]
        end = first.span.end
        while separator := _ALTERNATIVE.match(text.normalized[end : span.end]):
            next_span = trim(text, Span(end + separator.end(), span.end))
            follow = _role_at(text, next_span, taxonomy, lines) or _generic_hit(text, next_span)
            if follow is None:
                break
            roles.append(follow)
            end = follow.span.end
        separate = _SEPARATE.match(text.normalized[end : span.end]) if marker else None
        independent = (
            separate is not None
            and _role_at(
                text,
                Span(end + separate.end(), span.end),
                taxonomy,
                lines,
            )
            is not None
        )
        if mode == "heading" and not _TITLE_TAIL.match(text.normalized[end : span.end]):
            continue
        titles.append(
            Title(
                Span(first.span.start, end),
                line,
                tuple(roles),
                mode,
                bool(item or bullet),
                independent,
            )
        )
    return tuple(titles)


def has_hiring_cue(value: str) -> bool:
    return _HIRING.match(value) is not None or _LABEL.match(value) is not None
