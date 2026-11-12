"""Offline tests of the public parser with sanitized and synthetic posts."""

from pathlib import Path

import pytest

from job_market.domain.contracts import ExtractionResult
from job_market.parsing.pipeline import parse_vacancy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.mark.parametrize("name", ["it_recruiter", "business_development_lead", "ua_manager"])
def test_sanitized_reference_post(name):
    text = (FIXTURES / "posts" / f"{name}.txt").read_bytes().decode("utf-8")
    expected = ExtractionResult.model_validate_json(
        (FIXTURES / "expected" / f"{name}.json").read_bytes()
    )
    result = parse_vacancy(text)
    assert result.classification == expected.classification
    result.validate_source(text)
    actual, wanted = result.posting, expected.posting
    for field in (
        "title_original",
        "role_normalized",
        "role_family_code",
        "seniority_original",
        "seniority_code",
        "work_arrangement",
        "employment_schedule",
        "organizational_arrangement",
        "fixed_pay_stated",
        "bonus_stated",
        "commission_stated",
    ):
        assert getattr(actual, field) == getattr(wanted, field), field
    assert len(actual.salary_offers) == len(wanted.salary_offers)
    for observed, annotated in zip(actual.salary_offers, wanted.salary_offers, strict=True):
        for field in (
            "minimum_amount_scaled",
            "maximum_amount_scaled",
            "bound_type",
            "currency_symbol",
            "currency_code",
            "tax_basis",
            "payment_period",
            "compensation_component",
        ):
            assert getattr(observed, field) == getattr(annotated, field), field
        assert len(observed.evidence) == len(annotated.evidence)
    (requirement,) = wanted.experience_requirements
    assert any(
        row.scope_type == requirement.scope_type
        and row.minimum_months == requirement.minimum_months
        and row.maximum_months == requirement.maximum_months
        and row.scope_text == requirement.scope_text
        and row.requirement_context == requirement.requirement_context
        for row in actual.experience_requirements
    )
    actual_contexts = {(m.keyword_code, m.context) for m in actual.keyword_mentions}
    assert {(m.keyword_code, m.context) for m in wanted.keyword_mentions} <= actual_contexts
    if name == "it_recruiter":
        assert all(row.minimum_months is None for row in actual.experience_requirements)
    elif name == "ua_manager":
        assert {context for code, context in actual_contexts if code == "tiktok_ads"} == {
            "responsibility",
            "preferred",
        }
        assert not {"google_ads", "aso"} & {code for code, _ in actual_contexts}
        assert {
            mention.qualification_level
            for mention in actual.keyword_mentions
            if mention.keyword_code in {"appsflyer", "adapty"}
        } == {"understanding"}


def _vacancy(body):
    return "We're hiring a QA Engineer\n" + body


@pytest.mark.parametrize(
    ("text", "classification"),
    [
        ("#vacancy #QA #Python", "uncertain"),
        ("QA Engineer", "uncertain"),
        ("Python JavaScript SQL", "other"),
        ("Ищу работу QA Engineer\nОпыт работы 3 года", "candidate"),
        ("Open to work: QA Engineer\nRequirements: Python", "candidate"),
        ("Курс Python для QA Engineer\nЗаписывайтесь на вебинар", "other"),
        ("Відкриті вакансії:\n1. QA Engineer\n2. Data Analyst", "multiple_vacancies"),
        ("Hiring a QA Engineer and Data Analyst", "multiple_vacancies"),
        ("QA Engineer\nWe are looking for a Data Analyst\nRequirements: SQL", "uncertain"),
        ("Hiring a QA Engineer\nVacancy: QA Engineer\nRequirements: Python", "single_vacancy"),
        ("Vacancy: QA Engineer\nRequirements: Python\nVacancy: Data Analyst", "multiple_vacancies"),
    ],
)
def test_synthetic_classification_does_not_invent_a_posting(text, classification):
    result = parse_vacancy(text)
    assert result.classification == classification
    assert (result.posting is not None) == (classification == "single_vacancy")
    if classification == "uncertain":
        assert any(issue.impact == "blocks_posting" for issue in result.issues)


def test_alternative_titles_are_not_a_digest_or_guessed_role():
    result = parse_vacancy("Vacancy: QA Engineer / QA Automation Engineer\nRequirements: Python")
    posting = result.posting
    assert result.classification == "single_vacancy"
    assert posting.title_original == "QA Engineer / QA Automation Engineer"
    assert posting.role_family_code == "quality_assurance"
    assert posting.role_normalized is None
    assert posting.field_states["role_normalized"] == "ambiguous"


def test_explicit_title_outranks_hashtag_and_context_guards_ua():
    posting = parse_vacancy("#software_engineering\nHiring a UA Manager\nRequirements: CRM").posting
    assert posting.title_original == "UA Manager"
    assert posting.role_family_code == "unclassified"
    assert posting.role_normalized is None and posting.seniority_code is None
    disguised = parse_vacancy("Hiring a UA Manager\nBenefits: mobile subscription").posting
    assert disguised.role_normalized is None


def test_synthetic_russian_caption_preserves_scope_and_pay():
    text = (
        "Требуется Аналитик данных\n"
        "Требования:\n"
        "• от 2 лет опыта с PostgreSQL\n"
        "• Практический опыт работы с SQL\n"
        "Условия:\n"
        "• Формат работы: полностью удалённая работа\n"
        "• Зарплата: от 250 000 RUB в месяц до налогов"
    )
    result = parse_vacancy(text, content_kind="caption")
    posting = result.posting
    assert posting.title_original == "Аналитик данных"
    assert posting.role_family_code == "data_ai"
    assert posting.work_arrangement == "remote"
    duration = next(
        row for row in posting.experience_requirements if row.minimum_months is not None
    )
    assert (duration.minimum_months, duration.maximum_months) == (24, None)
    assert duration.scope_type == "technology_specific"
    assert duration.keyword_code == "postgresql"
    (offer,) = posting.salary_offers
    assert (offer.minimum_amount_scaled, offer.maximum_amount_scaled) == (2_500_000_000, None)
    assert (offer.currency_code, offer.payment_period, offer.tax_basis) == ("RUB", "month", "gross")
    result.validate_source(text)


def test_synthetic_mixed_unicode_evidence_and_keyword_boundaries():
    text = (
        "📈 Вакансія: Senior QA Engineer\r\n"
        "Що важливо:\r\n"
        "• Практичний досвід: Ｃ＋＋, C#, .NET, Node.js, JavaScript; "
        "понимание CRM / SQL\r\n"
        "• Fluent English; additional European languages are a plus\r\n"
        "• Go to the team; R&D, Google, JavaScriptish, https://example.net/node.js\r\n"
        "• Go programming with gRPC and R language\r\n"
        "• Python required, Docker is a plus\r\n"
        "• Java is not required\r\n"
        "• без досвіду з AWS\r\n"
        "Обязанности: TikTok Ads\r\n"
        "Умови: Figma bonus points\r\n"
        "Для відгуку: @synthetic_kubernetes\r\n"
    )
    result = parse_vacancy(text)
    posting = result.posting
    assert posting.seniority_code == "senior"
    mentions = {(m.keyword_code, m.context) for m in posting.keyword_mentions}
    for code in (
        "cplusplus",
        "csharp",
        "dotnet",
        "nodejs",
        "javascript",
        "crm",
        "sql",
        "go",
        "r",
        "python",
    ):
        assert (code, "required") in mentions
    assert {
        ("english", "required"),
        ("european_languages", "preferred"),
        ("tiktok_ads", "responsibility"),
        ("docker", "preferred"),
    } <= mentions
    assert ("java", "required") not in mentions
    assert not {"google_ads", "salesforce", "figma", "kubernetes", "aws"} & {
        m.keyword_code for m in posting.keyword_mentions
    }
    assert sum(m.keyword_code == "go" for m in posting.keyword_mentions) == 1
    assert sum(m.keyword_code == "nodejs" for m in posting.keyword_mentions) == 1
    assert (
        next(m for m in posting.keyword_mentions if m.keyword_code == "cplusplus").original_term
        == "Ｃ＋＋"
    )
    assert (
        next(m for m in posting.keyword_mentions if m.keyword_code == "crm").qualification_level
        == "understanding"
    )
    result.validate_source(text)


@pytest.mark.parametrize(
    ("line", "min_months", "max_months"),
    [
        ("2+ years of experience", 24, None),
        ("2+ роки досвіду", 24, None),
        ("від 2 років досвіду", 24, None),
        ("от 2 лет опыта", 24, None),
        ("3–6 years of experience", 36, 72),
        ("1,5 року досвіду", 18, None),
        ("up to 6 months of experience", None, 6),
    ],
)
def test_exact_experience_units_and_open_bounds(line, min_months, max_months):
    (requirement,) = parse_vacancy(
        _vacancy("Requirements:\n" + line)
    ).posting.experience_requirements
    assert (requirement.minimum_months, requirement.maximum_months) == (min_months, max_months)
    assert requirement.scope_type == "overall"


def test_scoped_duration_and_explicit_no_experience_keep_unknowns():
    result = parse_vacancy(
        _vacancy(
            "Requirements:\n"
            "2+ years of experience in Python / Java\n"
            "No prior experience required with Kubernetes\n"
            "Nice to have:\n"
            "Experience with CRM\n"
            "Responsibilities:\n"
            "Analyze the last 3 years of experience data"
        )
    )
    scoped, no_prior, preferred = result.posting.experience_requirements
    assert scoped.keyword_code is None and scoped.scope_text == "Python / Java"
    assert no_prior.prior_experience_policy == "explicitly_not_required"
    assert no_prior.minimum_months is no_prior.maximum_months is None
    assert preferred.requirement_context == "preferred"
    assert preferred.minimum_months is preferred.maximum_months is None


@pytest.mark.parametrize(
    "line", ["6–3 years of experience", "2--3 years of experience", "1.1 years of experience"]
)
def test_malformed_experience_abstains_with_a_metric_issue(line):
    result = parse_vacancy(_vacancy("Requirements:\n" + line))
    assert result.classification == "single_vacancy"
    assert not any(row.minimum_months is not None for row in result.posting.experience_requirements)
    assert any(issue.affected_metric == "experience" for issue in result.issues)


@pytest.mark.parametrize(
    ("line", "lo", "hi", "code", "period"),
    [
        ("Salary range: 2–3k EUR per month", 20_000_000, 30_000_000, "EUR", "month"),
        ("Salary: €2.500,50/month", 25_005_000, 25_005_000, "EUR", "month"),
        ("Оплата: до 2 500 USD", None, 25_000_000, "USD", "unspecified"),
        ("Salary: from $2,500", 25_000_000, None, None, "unspecified"),
        ("Salary: 35.1250 USD per hour", 351_250, 351_250, "USD", "hour"),
    ],
)
def test_salary_bounds_separators_and_units(line, lo, hi, code, period):
    (offer,) = parse_vacancy(_vacancy("What we offer:\n" + line)).posting.salary_offers
    assert (offer.minimum_amount_scaled, offer.maximum_amount_scaled) == (lo, hi)
    assert (offer.currency_code, offer.payment_period) == (code, period)


@pytest.mark.parametrize(
    "line",
    [
        "Salary: 3–2k EUR",
        "Salary: €2.500",
        "Salary: 2500 USD–3000 EUR",
        "Salary: 2500 USD\nSalary: 3000 USD",
        "Salary: 2500 USD–",
        "Salary: $2,000 or $3,000",
        "Salary: 12.12345 USD",
        "Salary: 1000000000000000 USD",
        "Salary: -1000 USD",
    ],
)
def test_invalid_salary_blocks_salary_only_and_preserves_evidence(line):
    result = parse_vacancy(_vacancy("What we offer:\n" + line))
    assert result.classification == "single_vacancy"
    assert result.posting.salary_offers == ()
    assert result.posting.compensation_text is not None
    (issue,) = result.issues
    assert issue.affected_metric == "salary"
    assert issue.impact == "blocks_metric"


def test_salary_and_role_do_not_guess_from_requirements_or_teams():
    posting = parse_vacancy(
        _vacancy(
            "Requirements: Managed a marketing budget of $20,000\n"
            "What we offer:\n"
            "Advertising budget: $4,000\n"
            "An in-house development team\n"
            "fixed pay + bonuses + commissions\n"
            "Part-time / hybrid"
        )
    ).posting
    assert posting.salary_offers == ()
    assert posting.fixed_pay_stated and posting.bonus_stated and posting.commission_stated
    assert posting.organizational_arrangement is None
    assert posting.employment_schedule == "part_time"
    assert posting.work_arrangement == "hybrid"


@pytest.mark.parametrize("kind", ["empty", "unsupported"])
def test_unsupported_and_empty_content_never_produce_vacancies(kind):
    assert parse_vacancy("Vacancy: QA Engineer", content_kind=kind).classification == "other"
    assert parse_vacancy(" \n").posting is None


def test_punctuated_technology_is_preserved_as_experience_scope():
    (requirement,) = parse_vacancy(
        _vacancy("Requirements: 2 years of experience with .NET")
    ).posting.experience_requirements
    assert requirement.scope_text == ".NET"
    assert requirement.scope_type == "technology_specific"
    assert requirement.keyword_code == "dotnet"


def test_incompatible_currency_evidence_cannot_be_consolidated():
    result = parse_vacancy(_vacancy("Salary: $2,500\nSalary: 2500 EUR"))
    assert result.posting.salary_offers == ()
    assert any(issue.affected_metric == "salary" for issue in result.issues)


def test_ambiguous_or_negative_salary_is_never_an_exact_positive_offer():
    for line in ("Salary: around $3,000", "-$3000", "Salary: approx. 3000 USD"):
        result = parse_vacancy(_vacancy("What we offer:\n" + line))
        assert not result.posting.salary_offers
        assert any(issue.affected_metric == "salary" for issue in result.issues)


def test_education_budget_and_remote_work_stipend_do_not_establish_pay_or_mode():
    posting = parse_vacancy(_vacancy("Benefits:\n$3000 for education\nRemote work stipend")).posting
    assert posting.salary_offers == ()
    assert posting.work_arrangement is None


def test_explicit_maximum_after_salary_amount_does_not_become_exact():
    (offer,) = parse_vacancy(_vacancy("Salary: $3k maximum")).posting.salary_offers
    assert offer.bound_type == "upper_only"
    assert offer.minimum_amount_scaled is None and offer.maximum_amount_scaled == 30_000_000


def test_contact_and_bare_domain_text_are_not_skill_requirements():
    result = parse_vacancy(_vacancy("Requirements: node.js.com\nContact: LinkedIn"))
    assert result.posting.keyword_mentions == ()


def test_unknown_role_can_keep_explicit_title_and_seniority():
    posting = parse_vacancy("Vacancy — Senior Accessibility Specialist").posting
    assert posting.title_original == "Senior Accessibility Specialist"
    assert posting.role_family_code == "unclassified"
    assert posting.role_normalized is None
    assert posting.seniority_code == "senior"


def test_conflicting_work_modes_do_not_drop_the_posting():
    result = parse_vacancy(_vacancy("Work arrangement: remote or hybrid"))
    assert result.classification == "single_vacancy"
    assert result.posting.work_arrangement is None
    assert result.posting.field_states["work_arrangement"] == "conflicting"
    assert any(issue.affected_metric == "work_arrangement" for issue in result.issues)


def test_numbered_digest_keeps_second_role_after_first_requirements():
    result = parse_vacancy(
        "Open roles:\n1. QA Engineer\nRequirements: Python\n2. Data Analyst\nRequirements: SQL"
    )
    assert result.classification == "multiple_vacancies"
    assert result.posting is None


def test_unmapped_alternative_does_not_get_dropped_from_title():
    result = parse_vacancy("Vacancy: QA Engineer / Testing Team Lead\nRequirements: Python")
    assert result.classification == "single_vacancy"
    assert result.posting.title_original == "QA Engineer / Testing Team Lead"
    assert result.posting.role_normalized is None


def test_repeated_explicit_label_for_same_role_is_not_a_digest():
    result = parse_vacancy("Vacancy: QA Engineer\nJob title: QA Engineer\nRequirements: Python")
    assert result.classification == "single_vacancy"
