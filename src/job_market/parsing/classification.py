"""Conservative classification from explicit hiring and candidate cues."""

import re
from dataclasses import dataclass

from job_market.domain.contracts import Evidence, ReviewIssue
from job_market.domain.enums import Classification, IssueImpact, SectionKind
from job_market.parsing.normalization import Text
from job_market.parsing.roles import Title, has_hiring_cue
from job_market.parsing.sections import Line, undecorated

_CANDIDATE = re.compile(
    r"^(?:open to work|(?:i(?: am|'m) +)?(?:looking for|seeking) (?:a |my next )?"
    r"(?:job|work|new role|position)\b|шукаю (?:роботу|позицію)|"
    r"розглядаю пропозиції|ищу (?:работу|позицию)|рассматриваю предложения|"
    r"#(?:opentowork|candidate|шукаюроботу|ищуработу)\b)",
    re.I,
)
_NON_JOB = re.compile(
    r"^(?:learn\b|enroll\b|register for\b|(?:free |new )?(?:course|bootcamp|webinar)\b|"
    r"(?:онлайн[- ]?)?(?:курс[иы]?|вебінар|вебинар)\b|навчальн\w+ курс\b|"
    r"набір на курс\b|записуйтесь\b|набор на курс\b|записывайтесь\b)",
    re.I,
)
_DIGEST = re.compile(
    r"^(?:open (?:roles|positions)|(?:job )?(?:digest|vacancies)|"
    r"відкриті вакансії|вакансії|подборка вакансий|открытые вакансии|вакансии)\b",
    re.I,
)
_JOB_TAG = re.compile(r"(?<!\w)#(?:vacancy|hiring|вакансія|вакансия)(?!\w)", re.I)


@dataclass(frozen=True)
class Decision:
    classification: Classification
    evidence: tuple[Evidence, ...] = ()
    title: Title | None = None
    issues: tuple[ReviewIssue, ...] = ()


def uncertain(evidence: tuple[Evidence, ...], code: str, description: str) -> Decision:
    return Decision(
        Classification.UNCERTAIN,
        evidence,
        issues=(
            ReviewIssue(
                field_path="classification",
                issue_code=code,
                description=description,
                impact=IssueImpact.BLOCKS_POSTING,
            ),
        ),
    )


def _identity(text: Text, title: Title) -> frozenset[str]:
    return title.signature or frozenset({text.value(title.span).casefold()})


def classify(text: Text, lines: tuple[Line, ...], titles: tuple[Title, ...]) -> Decision:
    intro = [
        (line, text.value(undecorated(text, line.span))) for line in lines if line.kind is None
    ]
    candidate = next((line for line, value in intro if _CANDIDATE.match(value)), None)
    non_job = next((line for line, value in intro if _NON_JOB.match(value)), None)
    hiring = next(
        (line for line, value in intro if has_hiring_cue(value) and not _CANDIDATE.match(value)),
        None,
    )
    if candidate is not None:
        if hiring is not None:
            return uncertain(
                (text.evidence(candidate.span), text.evidence(hiring.span)),
                "mixed_post_intent",
                "Candidate and hiring statements conflict.",
            )
        return Decision(Classification.CANDIDATE, (text.evidence(candidate.span),))
    if non_job is not None and hiring is None:
        return Decision(Classification.OTHER, (text.evidence(non_job.span),))

    digest = next((line for line, value in intro if _DIGEST.match(value)), None)
    labelled = tuple(
        {_identity(text, title): title for title in titles if title.mode == "label"}.values()
    )
    listed = tuple(title for title in titles if title.listed)
    independent = next((title for title in titles if title.independent), None)
    if independent or len(labelled) > 1 or len(listed) > 1 or (digest and len(titles) > 1):
        evidence = (text.evidence(digest.span),) if digest else ()
        selected = (
            (independent,) if independent else labelled if len(labelled) > 1 else listed or titles
        )
        return Decision(
            Classification.MULTIPLE_VACANCIES,
            evidence + tuple(text.evidence(title.line.span) for title in selected),
        )

    required = next((line for line in lines if line.kind == SectionKind.REQUIRED), None)
    explicit = tuple(title for title in titles if not title.listed) or titles
    if explicit:
        first = explicit[0]
        signature = _identity(text, first)
        incompatible = [title for title in explicit[1:] if _identity(text, title) != signature]
        if incompatible:
            return uncertain(
                tuple(text.evidence(title.line.span) for title in (first, *incompatible)),
                "conflicting_job_titles",
                "Explicit job titles identify different positions.",
            )
        if hiring or required or first.mode == "label":
            title = max(
                explicit,
                key=lambda title: (
                    len(title.roles) > 1,
                    any(role.seniority is not None for role in title.roles),
                    title.mode != "hiring",
                ),
            )
            spans = dict.fromkeys(
                line.span
                for line in (title.line, *(line for line in (hiring, required) if line is not None))
            )
            return Decision(
                Classification.SINGLE_VACANCY,
                tuple(text.evidence(span) for span in spans),
                title,
            )

    hint = next((line for line, value in intro if _JOB_TAG.search(value)), None)
    if clue := (hiring or digest or (explicit[0].line if explicit else None) or hint):
        return uncertain(
            (text.evidence(clue.span),),
            "insufficient_vacancy_evidence",
            "The available cues do not establish one advertised vacancy.",
        )
    return Decision(Classification.OTHER)
