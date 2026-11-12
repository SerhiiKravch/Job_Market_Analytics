"""Extract explicit arrangements and compensation structure with source evidence."""

import re
from dataclasses import dataclass

from job_market.domain.contracts import ReviewIssue
from job_market.domain.enums import (
    EmploymentSchedule,
    FieldState,
    IssueImpact,
    MetricCode,
    OrganizationalArrangement,
    SectionKind,
    WorkArrangement,
)
from job_market.parsing.normalization import Span, Text
from job_market.parsing.sections import Line, undecorated

_WORK = {
    WorkArrangement.REMOTE: (
        r"\b(?:fully remote|remote|віддален\w*|дистанційн\w*|удал[её]н\w*|дистанционн\w*)\b"
    ),
    WorkArrangement.HYBRID: r"\b(?:hybrid|гібридн\w*|гибридн\w*)\b",
    WorkArrangement.ON_SITE: (
        r"\b(?:on[- ]site|in[- ]office|in the office|в офісі|в офисе|офісн\w*|офисн\w*)\b"
    ),
}
_SCHEDULE = {
    EmploymentSchedule.FULL_TIME: (
        r"\b(?:full[- ]time|повна зайнятість|повний робочий день|"
        r"полная занятость|полный рабочий день)\b"
    ),
    EmploymentSchedule.PART_TIME: (
        r"\b(?:part[- ]time|часткова зайнятість|неповна зайнятість|"
        r"частичная занятость|неполная занятость)\b"
    ),
}
_ORG = {
    OrganizationalArrangement.IN_HOUSE: (
        r"\bin[- ]house\b|\b(?:у штаті|в штаті|в штате|штатна зайнятість)\b"
    ),
    OrganizationalArrangement.OUTSTAFF: r"\b(?:outstaff(?:ing)?|аутстаф\w*)\b",
    OrganizationalArrangement.OUTSOURCE: r"\b(?:outsourc(?:e|ing)|аутсорс\w*)\b",
}
_WORK_LABEL = re.compile(
    r"\b(?:work (?:mode|arrangement|format|location|schedule)|working hours|"
    r"формат р[ао]бот[иы]|режим работ[иы]|графік(?: роботи)?|график(?: работы)?|"
    r"employment schedule|employment type|зайнятість|занятость) *:",
    re.I,
)
_WORK_START = re.compile(
    r"^(?:work (?:remotely|from home)|remote (?:position|role)|"
    r"remote work(?: available| allowed| *$)|"
    r"hybrid (?:work|schedule)|працювати віддалено|можна працювати віддалено|"
    r"работа удал[её]нно|удал[её]нная работа|повністю віддален\w*|"
    r"полностью удал[её]н\w*)\b",
    re.I,
)
_ORG_LABEL = re.compile(
    r"\b(?:engagement|arrangement|team model|формат співпраці|формат сотрудничества) *:", re.I
)
_ORG_TAIL = re.compile(r"^[ *!‼️.]*$|^ *(?:\(|role\b|position\b|employment\b)", re.I)
_NEGATED = re.compile(r"\b(?:not|no|не|без) +$", re.I)
_COMP_FLAGS = {
    "fixed_pay_stated": (
        r"\b(?:fixed pay|fixed salary|base pay|base salary|фікс|фіксована ставка|"
        r"фикс|фиксированный оклад|оклад)\b"
    ),
    "bonus_stated": r"\b(?:bonuses?|бонус(?:и|ы|ів|ов)?|премі[яї]|преми[яи])\b",
    "commission_stated": (
        r"\b(?:commissions?|комісі[яїю]|комисси[яию]|відсоток від продажів|процент от продаж)\b"
    ),
}
_NON_PAY_BONUS = re.compile(
    r"\b(?:bonus points|bonus if|буде бонусом|будет бонусом|як бонус)\b", re.I
)


@dataclass(frozen=True)
class Condition:
    field: str
    value: str | bool | None
    spans: tuple[Span, ...]
    state: FieldState | None = None


def _matches(value: str, patterns: dict):
    for code, pattern in patterns.items():
        for match in re.finditer(pattern, value, re.I):
            if not _NEGATED.search(value[: match.start()]):
                yield code, match


def _standalone(value: str) -> bool:
    for pattern in (*_WORK.values(), *_SCHEDULE.values()):
        value = re.sub(pattern, "", value, flags=re.I)
    return (
        re.fullmatch(
            r"(?:[ /,|+\-!.*()]+|\b(?:or|and|або|та|или|и|повністю|полностью)\b)*", value, re.I
        )
        is not None
    )


def extract_conditions(
    text: Text,
    lines: tuple[Line, ...],
) -> tuple[tuple[Condition, ...], tuple[ReviewIssue, ...], tuple[Span, ...]]:
    candidates = {
        "work_arrangement": {},
        "employment_schedule": {},
        "organizational_arrangement": {},
    }
    flags = {}
    compensation = []
    for line in lines:
        if line.kind not in {None, SectionKind.OFFER}:
            continue
        normalized = text.value(line.span)
        clean = text.value(undecorated(text, line.span))
        if not clean or clean.startswith("#"):
            continue
        # A title suffix such as "QA Engineer | Remote" is a standalone cue.
        suffix = re.split(r" *[|] *| +- +", clean)[-1]
        if _WORK_LABEL.search(clean) or _WORK_START.match(clean) or _standalone(suffix):
            if re.search(r"\bwork (?:remotely|from home)\b", clean, re.I):
                candidates["work_arrangement"].setdefault(WorkArrangement.REMOTE, []).append(
                    line.span
                )
            for field, patterns in (
                ("work_arrangement", _WORK),
                ("employment_schedule", _SCHEDULE),
            ):
                for code, _ in _matches(normalized, patterns):
                    candidates[field].setdefault(code, []).append(line.span)
        for code, match in _matches(clean, _ORG):
            if _ORG_LABEL.search(clean) or (
                match.start() == 0 and _ORG_TAIL.match(clean[match.end() :])
            ):
                candidates["organizational_arrangement"].setdefault(code, []).append(line.span)
        if _NON_PAY_BONUS.search(clean):
            continue
        for field, pattern in _COMP_FLAGS.items():
            match = re.search(pattern, clean, re.I)
            if not match or _NEGATED.search(clean[: match.start()]):
                continue
            if line.kind != SectionKind.OFFER and not (
                match.start() == 0
                or re.search(r"\b(?:pay|salary|compensation|зарплата|оплата)\b", clean, re.I)
            ):
                continue
            flags.setdefault(field, []).append(line.span)
            compensation.append(line.span)

    conditions = [Condition(field, True, tuple(spans)) for field, spans in flags.items()]
    issues = []
    for field, values in candidates.items():
        if len(values) == 1:
            code, spans = next(iter(values.items()))
            conditions.append(Condition(field, code, tuple(spans)))
        elif len(values) > 1:
            conditions.append(
                Condition(
                    field,
                    None,
                    tuple(span for spans in values.values() for span in spans),
                    FieldState.CONFLICTING,
                )
            )
            metric = MetricCode.WORK_ARRANGEMENT if field == "work_arrangement" else None
            issues.append(
                ReviewIssue(
                    field_path=f"posting.{field}",
                    issue_code="conflicting_work_conditions",
                    description="Several conditions are stated without one established value.",
                    impact=IssueImpact.BLOCKS_METRIC if metric else IssueImpact.INFORMATIONAL,
                    affected_metric=metric,
                )
            )
    return tuple(conditions), tuple(issues), tuple(compensation)
