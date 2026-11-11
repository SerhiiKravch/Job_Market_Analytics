"""Installed rule resources and contract-shaped development annotations."""

import json
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import ValidationError

from job_market.domain.contracts import ExtractionResult
from job_market.domain.enums import (
    EmploymentSchedule,
    ExperienceScope,
    KeywordCategory,
    MentionContext,
    OrganizationalArrangement,
    PaymentPeriod,
    QualificationLevel,
    RoleFamily,
    SalaryBound,
    SectionKind,
    Seniority,
    TaxBasis,
    WorkArrangement,
)
from job_market.parsing import taxonomy as taxonomy_module
from job_market.parsing.taxonomy import RoleResource, load_taxonomy

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def test_taxonomy_loads_from_package_outside_project_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    load_taxonomy.cache_clear()
    taxonomy = load_taxonomy()

    assert taxonomy.version == "1.0.0"
    assert {role.family for role in taxonomy.roles} == set(RoleFamily) - {RoleFamily.UNCLASSIFIED}
    assert {word.category_code for word in taxonomy.keywords} == set(KeywordCategory)

    sections = {section.kind: section.headings for section in taxonomy.sections}
    assert {"Requirements", "Основні вимоги", "Що важливо", "Требования"} <= set(
        sections[SectionKind.REQUIRED]
    )
    assert {"Nice to have", "Буде значною перевагою", "Будет плюсом"} <= set(
        sections[SectionKind.PREFERRED]
    )
    assert {"Що робити", "Responsibilities", "Обязанности"} <= set(
        sections[SectionKind.RESPONSIBILITIES]
    )
    assert {"Умови", "Що даємо", "Условия"} <= set(sections[SectionKind.OFFER])
    assert "are a plus" in taxonomy.preference_cues


def test_ambiguous_aliases_require_context_and_vocabularies_do_not_invent_brands():
    taxonomy = load_taxonomy()
    roles = {role.code: role for role in taxonomy.roles}
    keywords = {word.canonical_code: word for word in taxonomy.keywords}

    role = roles["user_acquisition_manager"]
    assert role.family == RoleFamily.MARKETING_GROWTH
    assert "UA Manager" not in role.aliases
    assert any(
        alias.term == "UA Manager" and "mobile subscription" in alias.requires_any
        for alias in role.contextual_aliases
    )
    for code, term in [("go", "Go"), ("r", "R")]:
        assert term not in keywords[code].aliases
        assert any(
            alias.term == term and alias.case_sensitive and alias.requires_any
            for alias in keywords[code].contextual_aliases
        )
    assert "Google" not in keywords["google_ads"].aliases
    assert "CRM" not in keywords["salesforce"].aliases
    assert keywords["crm"].category_code == KeywordCategory.GENERIC_TOOL_CATEGORY
    assert keywords["linkedin"].category_code == KeywordCategory.TOOL_PLATFORM
    assert keywords["boolean_search"].category_code == KeywordCategory.SKILL_METHOD
    assert "C++" in keywords["cplusplus"].aliases
    assert "C#" in keywords["csharp"].aliases
    assert ".NET" in keywords["dotnet"].aliases
    assert "Node.js" in keywords["nodejs"].aliases
    assert "JavaScript" not in keywords["java"].aliases


def test_alias_collision_is_rejected_after_unicode_normalization():
    with pytest.raises(ValidationError, match="duplicate role alias"):
        RoleResource(
            version="1.0.0",
            roles=[
                {
                    "code": "first",
                    "display_name": "First role",
                    "family": "quality_assurance",
                    "aliases": ["ＱＡ"],
                },
                {
                    "code": "second",
                    "display_name": "Second role",
                    "family": "software_engineering",
                    "aliases": ["qa"],
                },
            ],
        )


def test_resources_with_mixed_versions_cannot_be_loaded(tmp_path, monkeypatch):
    original = files("job_market.parsing").joinpath("resources")
    resources = tmp_path / "resources"
    resources.mkdir()
    for name in ("roles.json", "keywords.json", "sections.json"):
        document = json.loads(original.joinpath(name).read_bytes())
        if name == "keywords.json":
            document["version"] = "1.0.1"
        (resources / name).write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(taxonomy_module, "files", lambda _: tmp_path)
    load_taxonomy.cache_clear()
    with pytest.raises(ValueError, match="versions must match"):
        load_taxonomy()


def load_fixture(name):
    text = (FIXTURES / "posts" / f"{name}.txt").read_bytes().decode("utf-8")
    expected = ExtractionResult.model_validate_json(
        (FIXTURES / "expected" / f"{name}.json").read_bytes()
    )
    expected.validate_source(text)
    return expected


@pytest.mark.parametrize("name", ["it_recruiter", "business_development_lead", "ua_manager"])
def test_reference_annotations_preserve_source_evidence_and_taxonomy_identities(name):
    expected = load_fixture(name)
    taxonomy = load_taxonomy()
    assert expected.taxonomy_version == taxonomy.version
    assert expected.parser_version == "reference-1"
    posting = expected.posting
    assert posting is not None
    roles = {role.code: role for role in taxonomy.roles}
    mapping = roles[posting.field_evidence["role_normalized"][0].taxonomy_code]
    assert mapping.display_name == posting.role_normalized
    assert mapping.family == posting.role_family_code
    keywords = {word.canonical_code for word in taxonomy.keywords}
    assert {mention.keyword_code for mention in posting.keyword_mentions} <= keywords


def test_recruiter_annotations_keep_unknown_units_and_consolidate_evidence():
    posting = load_fixture("it_recruiter").posting
    assert posting.role_family_code == RoleFamily.RECRUITMENT_HR
    assert posting.work_arrangement == WorkArrangement.REMOTE
    assert posting.seniority_code is None
    (requirement,) = posting.experience_requirements
    assert requirement.scope_type == ExperienceScope.ROLE_SPECIFIC
    assert requirement.minimum_months is requirement.maximum_months is None
    (salary,) = posting.salary_offers
    assert salary.bound_type == SalaryBound.UPPER_ONLY
    assert salary.minimum_amount_scaled is None
    assert salary.maximum_amount_scaled == 25_000_000
    assert salary.tax_basis == TaxBasis.NET
    assert salary.currency_symbol == "$"
    assert salary.currency_code is None
    assert salary.payment_period == PaymentPeriod.UNSPECIFIED
    assert len({quote.start_offset for quote in salary.evidence}) == 2
    assert {
        m.keyword_code for m in posting.keyword_mentions if m.context == MentionContext.PREFERRED
    } == {"fintech", "gambling", "igaming"}


def test_business_development_annotations_keep_in_house_separate_from_work_mode():
    posting = load_fixture("business_development_lead").posting
    assert posting.role_family_code == RoleFamily.SALES_BUSINESS_DEVELOPMENT
    assert posting.seniority_code == Seniority.LEAD
    assert posting.organizational_arrangement == OrganizationalArrangement.IN_HOUSE
    assert posting.work_arrangement is None
    assert posting.salary_offers == ()
    (requirement,) = posting.experience_requirements
    assert requirement.scope_type == ExperienceScope.ROLE_SPECIFIC
    assert (requirement.minimum_months, requirement.maximum_months) == (36, 72)
    (languages,) = [m for m in posting.keyword_mentions if m.keyword_code == "european_languages"]
    assert languages.section_label == "Requirements"
    assert languages.context == MentionContext.PREFERRED


def test_acquisition_annotations_preserve_context_and_do_not_invent_salary_or_skills():
    posting = load_fixture("ua_manager").posting
    assert posting.role_family_code == RoleFamily.MARKETING_GROWTH
    assert posting.seniority_code is None
    assert posting.work_arrangement == WorkArrangement.REMOTE
    assert posting.employment_schedule == EmploymentSchedule.FULL_TIME
    assert posting.organizational_arrangement is None
    assert posting.fixed_pay_stated and posting.bonus_stated
    assert posting.salary_offers == ()
    (requirement,) = posting.experience_requirements
    assert requirement.scope_type == ExperienceScope.ROLE_SPECIFIC
    assert requirement.minimum_months == 24
    assert requirement.maximum_months is None
    assert {m.context for m in posting.keyword_mentions if m.keyword_code == "tiktok_ads"} == {
        MentionContext.PREFERRED,
        MentionContext.RESPONSIBILITY,
    }
    assert {
        m.qualification_level
        for m in posting.keyword_mentions
        if m.keyword_code in {"appsflyer", "adapty"}
    } == {QualificationLevel.UNDERSTANDING}
    assert not {m.keyword_code for m in posting.keyword_mentions} & {"google_ads", "aso"}
