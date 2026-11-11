"""Source-bound validation and exact units without persistence or Telegram."""

from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal, localcontext

import pytest
from pydantic import ValidationError

from job_market.domain.contracts import (
    Evidence,
    ExperienceRequirement,
    ExtractionResult,
    JobPosting,
    KeywordMention,
    MessageObservation,
    ReviewIssue,
    SalaryOffer,
)
from job_market.domain.enums import (
    Classification,
    ExperienceUnit,
    PaymentPeriod,
    PriorExperiencePolicy,
    RoleFamily,
    TaxBasis,
)
from job_market.domain.validation import (
    INT64_MAX,
    duration_to_months,
    scale_money,
    unscale_money,
)


@pytest.mark.parametrize(
    ("amount", "scaled"),
    [
        (0, 0),
        ("2500", 25_000_000),
        (Decimal("1.234500"), 12_345),
        ("0.0001", 1),
        ("922337203685477.5807", INT64_MAX),
    ],
)
def test_money_is_exact_even_under_a_low_precision_decimal_context(amount, scaled):
    with localcontext() as context:
        context.prec = 3
        assert scale_money(amount) == scaled
        assert unscale_money(scaled) == Decimal(amount)


@pytest.mark.parametrize(
    "value",
    [
        2.5,
        True,
        "-0.01",
        "0.00001",
        "1e-1000000000",
        "922337203685477.5808",
        "1e1000000000",
        "NaN",
        "Infinity",
        "2,500",
    ],
)
def test_invalid_or_inexact_money_is_rejected(value):
    with pytest.raises(ValueError):
        scale_money(value)


@pytest.mark.parametrize("value", [-1, INT64_MAX + 1, False, 1.0])
def test_unscale_money_rejects_invalid_stored_integers(value):
    with pytest.raises(ValueError):
        unscale_money(value)


def test_duration_units_preserve_exact_whole_months():
    assert duration_to_months("1.5", ExperienceUnit.YEAR) == 18
    assert duration_to_months(3, ExperienceUnit.YEAR) == 36
    assert duration_to_months(6, ExperienceUnit.YEAR) == 72
    assert duration_to_months(2, ExperienceUnit.YEAR) == 24
    assert duration_to_months(24, ExperienceUnit.MONTH) == 24


@pytest.mark.parametrize(
    ("value", "unit"),
    [("1.1", "year"), ("2.5", "month"), (2, "week"), (2.0, "year"), (-1, "month")],
)
def test_invalid_or_inexact_experience_units_are_rejected(value, unit):
    with pytest.raises(ValueError):
        duration_to_months(value, unit)


def experience(**kwargs):
    return ExperienceRequirement(
        scope_type="role_specific",
        scope_text="technical recruiting",
        original_text="Experience in technical recruiting",
        **kwargs,
    )


def test_unknown_and_explicit_no_experience_do_not_become_zero_months():
    unspecified = experience(requirement_context="required")
    not_required = ExperienceRequirement(
        scope_type="overall",
        scope_text="prior experience",
        original_text="No prior experience required",
        prior_experience_policy="explicitly_not_required",
    )
    for requirement in (unspecified, not_required):
        assert requirement.minimum_months is None
        assert requirement.maximum_months is None
    assert unspecified.prior_experience_policy == PriorExperiencePolicy.UNSPECIFIED
    assert not_required.prior_experience_policy == PriorExperiencePolicy.EXPLICITLY_NOT_REQUIRED


@pytest.mark.parametrize(
    "fields",
    [
        {"minimum_months": -1},
        {"minimum_months": False},
        {"maximum_months": 1.5},
        {"minimum_months": 36, "maximum_months": 24},
        {"minimum_months": 0, "prior_experience_policy": "explicitly_not_required"},
        {"field_states": {"minimum_months": "stated"}},
        {"minimum_months": 12, "field_states": {"minimum_months": "ambiguous"}},
    ],
)
def test_invalid_experience_is_rejected(fields):
    with pytest.raises(ValidationError):
        experience(**fields)


@pytest.mark.parametrize(
    ("bound", "lo", "hi"),
    [("exact", 0, 0), ("range", 1, 2), ("lower_only", 1, None), ("upper_only", None, 2)],
)
def test_salary_accepts_supported_bound_shapes(bound, lo, hi):
    offer = SalaryOffer(
        bound_type=bound,
        minimum_amount_scaled=lo,
        maximum_amount_scaled=hi,
        currency_symbol="$",
        evidence=[{"text": "Advertised salary"}],
    )
    assert offer.minimum_amount_scaled == lo
    assert offer.maximum_amount_scaled == hi
    assert offer.currency_code is None
    assert offer.payment_period == PaymentPeriod.UNSPECIFIED
    assert offer.tax_basis == TaxBasis.UNSPECIFIED


@pytest.mark.parametrize(
    ("bound", "lo", "hi"),
    [
        ("exact", 1, 2),
        ("exact", None, None),
        ("range", None, 2),
        ("range", 2, 1),
        ("lower_only", 1, 2),
        ("lower_only", -1, None),
        ("lower_only", True, None),
        ("lower_only", 2.0, None),
        ("upper_only", None, INT64_MAX + 1),
        ("upper_only", 1, None),
    ],
)
def test_salary_rejects_invalid_bound_shapes_and_amount_types(bound, lo, hi):
    with pytest.raises(ValidationError):
        SalaryOffer(
            bound_type=bound,
            minimum_amount_scaled=lo,
            maximum_amount_scaled=hi,
            evidence=[{"text": "Advertised salary"}],
        )


@pytest.mark.parametrize("code", ["usd", "US", "$", "USDD", "ＵＳＤ", "USD\n"])
def test_currency_code_is_three_uppercase_ascii_letters(code):
    with pytest.raises(ValidationError):
        SalaryOffer(
            bound_type="upper_only",
            maximum_amount_scaled=2,
            currency_code=code,
            evidence=[{"text": "Advertised salary"}],
        )


def result(**fields):
    defaults = dict(parser_version="rules-1", taxonomy_version="1.0.0", classification="other")
    return ExtractionResult(**(defaults | fields))


def test_only_single_vacancy_can_carry_one_posting():
    evidence = [{"text": "We are hiring"}]
    single = result(
        classification="single_vacancy",
        posting={},
        classification_evidence=evidence,
    )
    assert single.posting.posting_number == 1
    assert single.posting.role_family_code == RoleFamily.UNCLASSIFIED
    assert single.posting.seniority_code is None
    assert single.posting.salary_offers == ()
    assert single.issues == ()
    with pytest.raises(ValidationError):
        result(classification="single_vacancy", classification_evidence=evidence)
    with pytest.raises(ValidationError):
        result(classification="single_vacancy", posting={})
    for classification in ("other", "candidate", "multiple_vacancies", "uncertain"):
        with pytest.raises(ValidationError):
            result(classification=classification, posting={}, classification_evidence=evidence)
    for number in (True, 1.0, 2):
        with pytest.raises(ValidationError):
            JobPosting(posting_number=number)


def test_facts_need_source_evidence_and_inferred_role_needs_mapping():
    with pytest.raises(ValidationError, match="work_arrangement requires evidence"):
        JobPosting(work_arrangement="remote")
    with pytest.raises(ValidationError, match="bonus_stated requires evidence"):
        JobPosting(bonus_stated=True)
    with pytest.raises(ValidationError, match="explicit taxonomy mapping"):
        JobPosting(
            role_normalized="User Acquisition Manager",
            field_evidence={"role_normalized": [{"text": "UA Manager"}]},
        )
    with pytest.raises(ValidationError, match="original wording"):
        JobPosting(title_original="Senior QA", field_evidence={"title_original": [{"text": "QA"}]})


def test_evidence_uses_original_character_offsets_and_requires_exact_quotes():
    source = "📈 Потрібен QA — C++"
    quote = "C++"
    start = source.index(quote)
    Evidence(text=quote, start_offset=start, end_offset=start + len(quote)).validate_source(source)
    Evidence(text="QA —").validate_source(source)
    with pytest.raises(ValueError, match="original text"):
        Evidence(text="QA -").validate_source(source)
    with pytest.raises(ValueError, match="does not match"):
        Evidence(
            text=quote, start_offset=start - 1, end_offset=start + len(quote) - 1
        ).validate_source(source)
    with pytest.raises(ValidationError, match="both evidence offsets"):
        Evidence(text=quote, start_offset=start)
    with pytest.raises(ValidationError, match="Unicode characters"):
        Evidence(text=quote, start_offset=start, end_offset=start + len(quote) + 1)


def test_result_checks_child_evidence_even_without_offsets():
    extraction = result(
        classification="single_vacancy",
        classification_evidence=[{"text": "We are hiring"}],
        posting={
            "keyword_mentions": [
                {
                    "keyword_code": "cplusplus",
                    "original_term": "C++",
                    "evidence_text": "Required: C++",
                }
            ]
        },
    )
    extraction.validate_source("We are hiring\nRequired: C++")
    with pytest.raises(ValueError, match="original text"):
        extraction.validate_source("We are hiring\nRequired: C#")
    with pytest.raises(ValidationError, match="original term"):
        KeywordMention(keyword_code="java", original_term="Java", evidence_text="Kotlin")


def issue(path="classification", **fields):
    return ReviewIssue(
        **(
            dict(
                field_path=path,
                issue_code="ambiguous_fact",
                description="Check the evidence",
                impact="blocks_posting",
            )
            | fields
        )
    )


def test_uncertain_result_requires_blocking_issue_and_has_no_posting():
    with pytest.raises(ValidationError, match="posting-blocking issue"):
        result(classification="uncertain", issues=[issue(impact="informational")])
    extraction = result(classification="uncertain", issues=[issue()])
    assert extraction.classification == Classification.UNCERTAIN
    assert extraction.posting is None


@pytest.mark.parametrize(
    "path",
    [
        "posting.salary_offers[-1]",
        "posting.salary_offers[00]",
        "posting.salary_offers[0].__dict__",
        "posting.salary_offers[0].bogus",
        "posting.salary_offers.currency_code",
        "posting.title_original[0]",
        "posting.id",
        "extraction_run.id",
        "posting.keyword_mentions[0].evidence_text",
    ],
)
def test_review_path_rejects_arbitrary_attributes(path):
    with pytest.raises(ValidationError, match="outside the extraction contract"):
        issue(path)


def test_review_target_and_metric_must_exist():
    with pytest.raises(ValidationError, match="metric is required"):
        issue("posting.salary_offers", impact="blocks_metric")
    with pytest.raises(ValidationError, match="requires a posting"):
        result(issues=[issue("posting.role_normalized")])
    evidence = [{"text": "We are hiring"}]
    with pytest.raises(ValidationError, match="absent record"):
        result(
            classification="single_vacancy",
            posting={},
            classification_evidence=evidence,
            issues=[
                issue(
                    "posting.salary_offers[0].currency_code",
                    impact="blocks_metric",
                    affected_metric="salary",
                )
            ],
        )
    good = result(
        classification="single_vacancy",
        posting={},
        classification_evidence=evidence,
        issues=[issue("posting.salary_offers", impact="blocks_metric", affected_metric="salary")],
    )
    assert good.posting.salary_offers == ()


def test_observation_normalizes_timestamps_preserving_original_text():
    plus_two = timezone(timedelta(hours=2))
    data = dict(
        telegram_chat_id=-1001234567890,
        telegram_message_id=10,
        published_at=datetime(2026, 1, 2, 12, tzinfo=plus_two),
        observed_at=datetime(2026, 1, 2, 14, tzinfo=plus_two),
        full_text="\n📈 Original\r\n",
        content_kind="caption",
    )
    observation = MessageObservation(**data)
    assert observation.published_at == datetime(2026, 1, 2, 10, tzinfo=UTC)
    assert observation.published_at.tzinfo is UTC
    assert observation.full_text == data["full_text"]
    assert MessageObservation.model_validate_json(observation.model_dump_json()) == observation
    with pytest.raises(ValidationError, match="timezone-aware"):
        MessageObservation(**(data | {"published_at": datetime(2026, 1, 2)}))
    with pytest.raises(ValidationError):
        MessageObservation(**(data | {"telegram_message_id": 0}))
