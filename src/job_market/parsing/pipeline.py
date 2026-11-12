"""Deterministic, offline extraction of at most one vacancy from a post."""

from job_market.domain.contracts import ExtractionResult, JobPosting, ReviewIssue
from job_market.domain.enums import ContentKind, FieldState, IssueImpact, MetricCode, RoleFamily
from job_market.parsing.classification import classify
from job_market.parsing.experience import extract_experience
from job_market.parsing.keywords import extract_keywords
from job_market.parsing.normalization import Text
from job_market.parsing.roles import Title, find_titles
from job_market.parsing.salary import extract_salary
from job_market.parsing.sections import split_sections
from job_market.parsing.taxonomy import load_taxonomy
from job_market.parsing.work_conditions import extract_conditions

PARSER_VERSION = "1.0.0"


def _role_fields(text: Text, title: Title, titles: tuple[Title, ...]):
    title_quote = text.evidence(title.span)
    fields = {"title_original": title_quote.text}
    evidence = {"title_original": (title_quote,)}
    states = {}
    issues = []
    definitions = [role.definition for role in title.roles]
    known = [role for role in title.roles if role.definition is not None]
    mappings = tuple(
        dict.fromkeys(
            (span, role.definition.code)
            for role in known
            for span in (role.span, role.context_span)
            if span is not None
        )
    )
    quotes = tuple(text.evidence(span, taxonomy_code=code) for span, code in mappings)
    if len(known) == len(title.roles):
        families = {definition.family for definition in definitions}
        codes = {definition.code for definition in definitions}
        if len(families) == 1:
            fields["role_family_code"] = next(iter(families))
            evidence["role_family_code"] = quotes
        if len(codes) == 1:
            fields["role_normalized"] = definitions[0].display_name
            evidence["role_normalized"] = quotes
    if len(title.roles) > 1 and "role_normalized" not in fields:
        states["role_normalized"] = FieldState.AMBIGUOUS
        role_known = "role_family_code" in fields
        if not role_known:
            states["role_family_code"] = FieldState.AMBIGUOUS
        issues.append(
            ReviewIssue(
                field_path="posting.role_normalized",
                issue_code="alternative_job_titles",
                description="Alternative titles do not establish one normalized role.",
                impact=IssueImpact.INFORMATIONAL if role_known else IssueImpact.BLOCKS_METRIC,
                affected_metric=None if role_known else MetricCode.ROLE,
            )
        )
    fields.setdefault("role_family_code", RoleFamily.UNCLASSIFIED)

    related = [candidate for candidate in titles if candidate.signature == title.signature]
    levels = {role.seniority for candidate in related for role in candidate.roles if role.seniority}
    selected_levels = {role.seniority for role in title.roles}
    if len(levels) > 1 or (len(title.roles) > 1 and None in selected_levels and levels):
        states["seniority_code"] = (
            FieldState.CONFLICTING if len(levels) > 1 else FieldState.AMBIGUOUS
        )
        issues.append(
            ReviewIssue(
                field_path="posting.seniority_code",
                issue_code="unclear_seniority",
                description="The title evidence does not establish one seniority.",
                impact=IssueImpact.BLOCKS_METRIC,
                affected_metric=MetricCode.SENIORITY,
            )
        )
    elif levels:
        role = next(role for candidate in related for role in candidate.roles if role.seniority)
        seniority = text.evidence(role.seniority_span)
        fields.update(seniority_code=role.seniority, seniority_original=seniority.text)
        evidence.update(seniority_code=(seniority,), seniority_original=(seniority,))
    return fields, evidence, states, issues


def parse_vacancy(
    original: str,
    *,
    content_kind: ContentKind = ContentKind.TEXT,
) -> ExtractionResult:
    """Parse one untouched source string and validate the returned evidence against it."""
    taxonomy = load_taxonomy()
    common = dict(parser_version=PARSER_VERSION, taxonomy_version=taxonomy.version)
    if not original.strip() or content_kind in {ContentKind.EMPTY, ContentKind.UNSUPPORTED}:
        return ExtractionResult(classification="other", **common)
    text = Text.from_source(original)
    lines = split_sections(text, taxonomy)
    titles = find_titles(text, lines, taxonomy)
    decision = classify(text, lines, titles)
    posting = None
    issues = list(decision.issues)
    if decision.title is not None:
        fields, evidence, states, role_issues = _role_fields(text, decision.title, titles)
        experience, experience_issues = extract_experience(text, lines, taxonomy)
        offers, salary_issues, salary_spans = extract_salary(text, lines)
        conditions, condition_issues, compensation_spans = extract_conditions(text, lines)
        for condition in conditions:
            if condition.value is not None:
                fields[condition.field] = condition.value
            if condition.state is not None:
                states[condition.field] = condition.state
            evidence[condition.field] = tuple(
                text.evidence(span) for span in dict.fromkeys(condition.spans)
            )
        spans = sorted(set((*salary_spans, *compensation_spans)), key=lambda span: span.start)
        if spans:
            quotes = tuple(text.evidence(span) for span in spans)
            fields["compensation_text"] = quotes[0].text
            evidence["compensation_text"] = quotes
        posting = JobPosting(
            field_evidence=evidence,
            field_states=states,
            salary_offers=offers,
            experience_requirements=experience,
            keyword_mentions=extract_keywords(text, lines, taxonomy),
            **fields,
        )
        issues.extend((*role_issues, *experience_issues, *salary_issues, *condition_issues))
    result = ExtractionResult(
        classification=decision.classification,
        classification_evidence=decision.evidence,
        posting=posting,
        issues=tuple(issues),
        **common,
    )
    result.validate_source(original)
    return result
