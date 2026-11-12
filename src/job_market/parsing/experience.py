"""Extract exact whole-month duration and preserve the stated experience scope."""

import re

from job_market.domain.contracts import ExperienceRequirement, ReviewIssue
from job_market.domain.enums import (
    ExperienceScope,
    ExperienceUnit,
    IssueImpact,
    MentionContext,
    MetricCode,
    PriorExperiencePolicy,
    RequirementContext,
    SectionKind,
)
from job_market.domain.validation import duration_to_months
from job_market.parsing.keywords import keyword_hits
from job_market.parsing.normalization import Span, Text, trim
from job_market.parsing.sections import (
    Line,
    clauses,
    context_for,
    no_experience,
    requirement_context,
)
from job_market.parsing.taxonomy import Taxonomy

_NUMBER = r"[0-9]+(?:[.,][0-9]+)?"
_UNIT = (
    r"years?|yrs?|months?|mos?|рік|роки|років|року|місяць|місяці|місяців|"
    r"год(?:а|ов)?|лет|месяц(?:а|ев)?"
)
_DURATION = re.compile(
    rf"(?<![\w.,-])(?:(?P<qual>up to|at least|from|до|від|от|не менее) +)?"
    rf"(?P<lo>{_NUMBER}) *(?P<plus>\+)?"
    rf"(?: *(?:-|to|до) *(?P<hi>{_NUMBER}))? *(?P<unit>{_UNIT})(?!\w)",
    re.I,
)
_EXPERIENCE = re.compile(r"\b(?:experience|досвід\w*|опыт\w*)\b", re.I)
_ROLE_SCOPE = re.compile(
    r"\b(?:sell(?:ing)?|sales\b|recruit\w*|(?:it[- ]?)?рекрут\w*|продаж\w*|"
    r"ua\b|performance marketing|user acquisition|(?:backend|frontend) development|"
    r"as (?:an? )?\w+)",
    re.I,
)
_SCOPE_PREFIX = re.compile(
    r"^[ :,-]*(?:(?:of +)?(?:experience|досвід\w*|опыт\w*)\b[ :,-]*)?"
    r"(?:(?:in|with|of|using|в|у|з|із|с|по) +)?",
    re.I,
)
_EXPERIENCE_BEFORE = re.compile(
    r"\b(?:experience|досвід\w*|опыт\w*) +(?:(?:роботи|работы) +)?"
    r"(?:(?:required|не требуется|не потрібен) +)?"
    r"(?:in|with|of|using|в|у|з|із|с|по) +",
    re.I,
)
_DURATION_LIKE = re.compile(rf"[0-9][0-9., +\-]* *(?:{_UNIT})(?!\w)", re.I)
_GENERIC_SCOPE = re.compile(
    r"^(?:(?:relevant|prior|previous|professional|commercial|overall|work|"
    r"роботи|работы|попередній|загальний|комерційний|коммерческий|общий) +)*"
    r"(?:experience|досвід\w*|опыт\w*|роботи|работы)?$",
    re.I,
)


def _issue(code: str, description: str) -> ReviewIssue:
    return ReviewIssue(
        field_path="posting.experience_requirements",
        issue_code=code,
        description=description,
        impact=IssueImpact.BLOCKS_METRIC,
        affected_metric=MetricCode.EXPERIENCE,
    )


def _bounds(match: re.Match[str]) -> tuple[int | None, int | None]:
    unit = (
        ExperienceUnit.MONTH
        if match["unit"].lower().startswith(("mo", "мі", "ме"))
        else ExperienceUnit.YEAR
    )

    def months(raw: str) -> int:
        if re.fullmatch(r"[0-9]{1,3}[.,][0-9]{3}", raw):
            raise ValueError("Ambiguous experience duration separator.")
        return duration_to_months(raw.replace(",", "."), unit)

    lo = months(match["lo"])
    qualifier = (match["qual"] or "").lower()
    if match["hi"] is not None:
        hi = months(match["hi"])
        if lo > hi or match["plus"] or qualifier in {"до", "up to", "at least", "не менее"}:
            raise ValueError("Invalid experience range.")
        return lo, hi
    if qualifier in {"до", "up to"}:
        if match["plus"]:
            raise ValueError("Conflicting experience bounds.")
        return None, lo
    # A stated experience duration is a threshold unless an upper bound is explicit.
    return lo, None


def _scope(
    text: Text,
    clause: Span,
    match: re.Match[str] | None,
    taxonomy: Taxonomy,
) -> tuple[ExperienceScope, str, str | None]:
    value = text.value(clause)
    prior = _EXPERIENCE_BEFORE.search(value)
    start = end = 0
    if prior and (match is None or prior.end() < match.start()):
        start, end = prior.end(), match.start() if match else len(value)
    elif match:
        tail = value[match.end() :]
        start = match.end() + _SCOPE_PREFIX.match(tail).end()
        end = len(value)
        if not value[start:end].strip(" .,:!-"):
            before = re.sub(r"^[^\w]+", "", value[: match.start()]).rstrip(" :,-")
            if before and not _EXPERIENCE.search(before):
                start = value.find(before)
                end = start + len(before)
    elif not no_experience(value):
        generic = _EXPERIENCE.search(value)
        if generic:
            start = generic.end() + _SCOPE_PREFIX.match(value[generic.end() :]).end()
            end = len(value)
    raw_scope = re.sub(
        r" +(?:is required|required|is preferred|is not required|не требуется) *[.!]*$",
        "",
        value[start:end],
        flags=re.I,
    )
    leading = len(raw_scope) - len(raw_scope.lstrip(" :,-"))
    raw_scope = raw_scope.lstrip(" :,-").rstrip(" .,:!-")
    if not raw_scope or _GENERIC_SCOPE.fullmatch(raw_scope):
        return ExperienceScope.OVERALL, "overall experience", None
    span = clause.subspan(start + leading, start + leading + len(raw_scope))
    scope = text.evidence(span).text
    hits = keyword_hits(raw_scope, taxonomy)
    ids = {hit.definition.canonical_code for hit in hits}
    cats = {hit.definition.category_code.value for hit in hits}
    if _ROLE_SCOPE.search(raw_scope):
        return ExperienceScope.ROLE_SPECIFIC, scope, None
    if cats and cats <= {"industry_domain"}:
        return ExperienceScope.DOMAIN_SPECIFIC, scope, next(iter(ids)) if len(ids) == 1 else None
    if len(ids) == 1 and cats <= {"technology", "tool_platform", "generic_tool_category"}:
        return ExperienceScope.TECHNOLOGY_SPECIFIC, scope, next(iter(ids))
    return ExperienceScope.OTHER, scope, None


def extract_experience(
    text: Text,
    lines: tuple[Line, ...],
    taxonomy: Taxonomy,
) -> tuple[tuple[ExperienceRequirement, ...], tuple[ReviewIssue, ...]]:
    requirements = []
    issues = []
    for line in lines:
        if line.kind not in {None, SectionKind.REQUIRED, SectionKind.PREFERRED}:
            continue
        for part in clauses(text, line):
            clause = trim(text, part)
            value = text.value(clause)
            context = context_for(value, line, taxonomy)
            negated = no_experience(value)
            matches = list(_DURATION.finditer(value))
            experience = _EXPERIENCE.search(value)
            if not (
                negated
                or experience
                or (
                    matches
                    and context in {MentionContext.REQUIRED, MentionContext.PREFERRED}
                    and (
                        keyword_hits(value, taxonomy)
                        or value.lstrip("• -*").startswith(matches[0][0])
                    )
                )
            ):
                continue
            if len(matches) > 1 or (matches and negated):
                issues.append(
                    _issue(
                        "ambiguous_experience", "Several incompatible duration cues occur together."
                    )
                )
                continue
            match = matches[0] if matches else None
            if match is None and _DURATION_LIKE.search(value):
                issues.append(_issue("invalid_experience", "The duration has a malformed bound."))
                continue
            if match and re.search(r"[-–—] *$", value[: match.start()]):
                issues.append(_issue("invalid_experience", "The duration has a malformed bound."))
                continue
            try:
                lo, hi = _bounds(match) if match else (None, None)
            except ValueError:
                issues.append(
                    _issue(
                        "invalid_experience",
                        "The duration cannot be represented as ordered whole months.",
                    )
                )
                continue
            scope_type, scope_text, keyword = _scope(text, clause, match, taxonomy)
            context_code = requirement_context(context)
            policy = PriorExperiencePolicy.UNSPECIFIED
            if negated:
                policy = PriorExperiencePolicy.EXPLICITLY_NOT_REQUIRED
            elif context_code == RequirementContext.REQUIRED:
                policy = PriorExperiencePolicy.REQUIRED
            requirements.append(
                ExperienceRequirement(
                    scope_type=scope_type,
                    scope_text=scope_text,
                    keyword_code=keyword,
                    minimum_months=lo,
                    maximum_months=hi,
                    requirement_context=context_code,
                    prior_experience_policy=policy,
                    original_text=text.evidence(clause).text,
                )
            )
    return tuple(requirements), tuple(issues)
