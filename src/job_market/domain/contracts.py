"""Validated plain data at the ingestion and extraction boundaries."""

import re
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    model_validator,
)

from job_market.domain.enums import (
    Classification,
    CompensationComponent,
    ContentKind,
    EmploymentSchedule,
    ExperienceScope,
    FieldState,
    IssueImpact,
    MentionContext,
    MetricCode,
    OrganizationalArrangement,
    PaymentPeriod,
    PriorExperiencePolicy,
    QualificationLevel,
    RequirementContext,
    RoleFamily,
    SalaryBound,
    Seniority,
    TaxBasis,
    WorkArrangement,
)
from job_market.domain.validation import INT64_MAX, INT64_MIN, as_utc, nonblank, validate_quote

NonBlank = Annotated[str, StringConstraints(strict=True), AfterValidator(nonblank)]
CanonicalCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")]
UInt63 = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
PositiveId = Annotated[int, Field(strict=True, gt=0, le=INT64_MAX)]
SignedId = Annotated[int, Field(strict=True, ge=INT64_MIN, le=INT64_MAX)]
UtcTimestamp = Annotated[datetime, Field(strict=True), AfterValidator(as_utc)]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
PostingField = Literal[
    "title_original",
    "role_normalized",
    "role_family_code",
    "seniority_original",
    "seniority_code",
    "work_arrangement",
    "employment_schedule",
    "organizational_arrangement",
    "compensation_text",
    "fixed_pay_stated",
    "bonus_stated",
    "commission_stated",
]
ExperienceField = Literal[
    "scope_type",
    "scope_text",
    "keyword_code",
    "minimum_months",
    "maximum_months",
    "requirement_context",
    "prior_experience_policy",
]
SalaryField = Literal[
    "minimum_amount_scaled",
    "maximum_amount_scaled",
    "currency_symbol",
    "currency_code",
    "payment_period",
    "tax_basis",
    "compensation_component",
]


class Contract(BaseModel):
    """Reject extra attributes and accidental reassignment of model fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class MessageObservation(Contract):
    telegram_chat_id: SignedId
    telegram_message_id: PositiveId
    published_at: UtcTimestamp
    observed_at: UtcTimestamp
    telegram_edited_at: UtcTimestamp | None = None
    full_text: str
    content_kind: ContentKind
    message_url: NonBlank | None = None


def _check_offsets(quote: str, start: int | None, end: int | None) -> None:
    if (start is None) != (end is None):
        raise ValueError("supply both evidence offsets or neither")
    if start is not None and end is not None and end - start != len(quote):
        raise ValueError("evidence offsets must span the full quote in Unicode characters")


class Evidence(Contract):
    text: NonBlank
    start_offset: UInt63 | None = None
    end_offset: UInt63 | None = None
    taxonomy_code: CanonicalCode | None = None

    @model_validator(mode="after")
    def check_offsets(self) -> Self:
        _check_offsets(self.text, self.start_offset, self.end_offset)
        return self

    def validate_source(self, source: str) -> None:
        validate_quote(source, self.text, self.start_offset, self.end_offset)


def _check_field_states(states: dict, accepted: dict) -> None:
    for field, state in states.items():
        if accepted[field] != (state == FieldState.STATED):
            raise ValueError(f"field state does not match accepted value for {field}")


class ExperienceRequirement(Contract):
    scope_type: ExperienceScope
    scope_text: NonBlank
    keyword_code: CanonicalCode | None = None
    minimum_months: UInt63 | None = None
    maximum_months: UInt63 | None = None
    requirement_context: RequirementContext = RequirementContext.UNSPECIFIED
    prior_experience_policy: PriorExperiencePolicy = PriorExperiencePolicy.UNSPECIFIED
    original_text: NonBlank
    field_states: dict[ExperienceField, FieldState] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_requirement(self) -> Self:
        lo, hi = self.minimum_months, self.maximum_months
        if lo is not None and hi is not None and lo > hi:
            raise ValueError("minimum experience cannot exceed maximum experience")
        if self.prior_experience_policy == PriorExperiencePolicy.EXPLICITLY_NOT_REQUIRED and (
            lo is not None or hi is not None
        ):
            raise ValueError("no prior experience required is a policy, not a numeric duration")
        _check_field_states(
            self.field_states,
            {
                "scope_type": True,
                "scope_text": True,
                "keyword_code": self.keyword_code is not None,
                "minimum_months": lo is not None,
                "maximum_months": hi is not None,
                "requirement_context": self.requirement_context != RequirementContext.UNSPECIFIED,
                "prior_experience_policy": (
                    self.prior_experience_policy != PriorExperiencePolicy.UNSPECIFIED
                ),
            },
        )
        return self


class SalaryOffer(Contract):
    minimum_amount_scaled: UInt63 | None = None
    maximum_amount_scaled: UInt63 | None = None
    bound_type: SalaryBound
    currency_symbol: NonBlank | None = None
    currency_code: CurrencyCode | None = None
    payment_period: PaymentPeriod = PaymentPeriod.UNSPECIFIED
    tax_basis: TaxBasis = TaxBasis.UNSPECIFIED
    compensation_component: CompensationComponent = CompensationComponent.UNSPECIFIED
    evidence: Annotated[tuple[Evidence, ...], Field(min_length=1)]
    field_states: dict[SalaryField, FieldState] = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_offer(self) -> Self:
        lo, hi = self.minimum_amount_scaled, self.maximum_amount_scaled
        match self.bound_type:
            case SalaryBound.EXACT:
                valid = lo is not None and hi == lo
            case SalaryBound.RANGE:
                valid = lo is not None and hi is not None and lo <= hi
            case SalaryBound.LOWER_ONLY:
                valid = lo is not None and hi is None
            case SalaryBound.UPPER_ONLY:
                valid = lo is None and hi is not None
        if not valid:
            raise ValueError("salary bounds do not match the declared bound type")
        _check_field_states(
            self.field_states,
            {
                "minimum_amount_scaled": lo is not None,
                "maximum_amount_scaled": hi is not None,
                "currency_symbol": self.currency_symbol is not None,
                "currency_code": self.currency_code is not None,
                "payment_period": self.payment_period != PaymentPeriod.UNSPECIFIED,
                "tax_basis": self.tax_basis != TaxBasis.UNSPECIFIED,
                "compensation_component": (
                    self.compensation_component != CompensationComponent.UNSPECIFIED
                ),
            },
        )
        return self


class KeywordMention(Contract):
    keyword_code: CanonicalCode
    original_term: NonBlank
    context: MentionContext = MentionContext.BACKGROUND_UNSPECIFIED
    qualification_level: QualificationLevel = QualificationLevel.UNSPECIFIED
    section_label: NonBlank | None = None
    evidence_text: NonBlank
    start_offset: UInt63 | None = None
    end_offset: UInt63 | None = None

    @model_validator(mode="after")
    def check_evidence(self) -> Self:
        _check_offsets(self.evidence_text, self.start_offset, self.end_offset)
        if self.original_term not in self.evidence_text:
            raise ValueError("the original term must occur in the evidence quote")
        return self


class JobPosting(Contract):
    posting_number: Annotated[int, Field(strict=True, ge=1, le=1)] = 1
    title_original: NonBlank | None = None
    role_normalized: NonBlank | None = None
    role_family_code: RoleFamily = RoleFamily.UNCLASSIFIED
    seniority_original: NonBlank | None = None
    seniority_code: Seniority | None = None
    work_arrangement: WorkArrangement | None = None
    employment_schedule: EmploymentSchedule | None = None
    organizational_arrangement: OrganizationalArrangement | None = None
    compensation_text: NonBlank | None = None
    fixed_pay_stated: Annotated[bool, Field(strict=True)] = False
    bonus_stated: Annotated[bool, Field(strict=True)] = False
    commission_stated: Annotated[bool, Field(strict=True)] = False
    field_evidence: dict[PostingField, Annotated[tuple[Evidence, ...], Field(min_length=1)]] = (
        Field(default_factory=dict)
    )
    field_states: dict[PostingField, FieldState] = Field(default_factory=dict)
    experience_requirements: tuple[ExperienceRequirement, ...] = ()
    salary_offers: tuple[SalaryOffer, ...] = ()
    keyword_mentions: tuple[KeywordMention, ...] = ()

    @model_validator(mode="after")
    def check_evidence_and_states(self) -> Self:
        accepted = {
            "title_original": self.title_original is not None,
            "role_normalized": self.role_normalized is not None,
            "role_family_code": self.role_family_code != RoleFamily.UNCLASSIFIED,
            "seniority_original": self.seniority_original is not None,
            "seniority_code": self.seniority_code is not None,
            "work_arrangement": self.work_arrangement is not None,
            "employment_schedule": self.employment_schedule is not None,
            "organizational_arrangement": self.organizational_arrangement is not None,
            "compensation_text": self.compensation_text is not None,
            "fixed_pay_stated": self.fixed_pay_stated,
            "bonus_stated": self.bonus_stated,
            "commission_stated": self.commission_stated,
        }
        _check_field_states(self.field_states, accepted)
        for field, present in accepted.items():
            if present and field not in self.field_evidence:
                raise ValueError(f"accepted posting field {field} requires evidence")
        for field in ("title_original", "seniority_original", "compensation_text"):
            value = getattr(self, field)
            if value is not None and not any(
                value in quote.text for quote in self.field_evidence[field]
            ):
                raise ValueError(f"original wording for {field} must occur in its evidence")
        if self.role_normalized is not None and not any(
            self.role_normalized in quote.text or quote.taxonomy_code is not None
            for quote in self.field_evidence["role_normalized"]
        ):
            raise ValueError("a normalized role needs its wording or an explicit taxonomy mapping")
        return self


_COLLECTION_FIELDS = {
    "experience_requirements": frozenset(ExperienceRequirement.model_fields) - {"field_states"},
    "salary_offers": frozenset(SalaryOffer.model_fields) - {"evidence", "field_states"},
    "keyword_mentions": frozenset(KeywordMention.model_fields)
    - {
        "evidence_text",
        "start_offset",
        "end_offset",
    },
}
_POSTING_FIELDS = frozenset(JobPosting.model_fields) - {
    "posting_number",
    "field_states",
    "field_evidence",
    *_COLLECTION_FIELDS,
}
_POSTING_PATH = re.compile(
    r"posting\.(?P<field>[a-z_]+)(?:\[(?P<index>0|[1-9][0-9]*)\](?:\.(?P<child>[a-z_]+))?)?"
)


class ReviewIssue(Contract):
    """An issue draft. The storage layer supplies audited review metadata."""

    field_path: Annotated[str, StringConstraints(max_length=120)]
    issue_code: CanonicalCode
    description: NonBlank
    impact: IssueImpact
    affected_metric: MetricCode | None = None

    @model_validator(mode="after")
    def check_issue(self) -> Self:
        if (self.impact == IssueImpact.BLOCKS_METRIC) != (self.affected_metric is not None):
            raise ValueError("a metric is required only for a metric-blocking issue")
        if self.field_path in {"classification", "posting"}:
            return self
        match = _POSTING_PATH.fullmatch(self.field_path)
        if match is not None:
            field, index, child = match.groups()
            if field in _POSTING_FIELDS and index is None:
                return self
            if field in _COLLECTION_FIELDS and (
                child is None or child in _COLLECTION_FIELDS[field]
            ):
                return self
        raise ValueError("review field path is outside the extraction contract")

    def validate_target(self, posting: JobPosting | None) -> None:
        # 'posting' can refer to the missing posting as a whole, e.g. for a digest.
        match = _POSTING_PATH.fullmatch(self.field_path)
        if match is not None:
            if posting is None:
                raise ValueError("review field path requires a posting in this extraction")
            field, index, _ = match.groups()
            if index is not None and int(index) >= len(getattr(posting, field)):
                raise ValueError("review field path refers to an absent record")


class ExtractionResult(Contract):
    """One complete, source-bound interpretation; no persistence IDs."""

    parser_version: NonBlank
    taxonomy_version: NonBlank
    classification: Classification
    classification_evidence: tuple[Evidence, ...] = ()
    posting: JobPosting | None = None
    issues: tuple[ReviewIssue, ...] = ()

    @model_validator(mode="after")
    def check_result(self) -> Self:
        if (self.classification == Classification.SINGLE_VACANCY) != (self.posting is not None):
            raise ValueError("only a single vacancy must contain a posting")
        if (
            self.classification
            in {
                Classification.SINGLE_VACANCY,
                Classification.MULTIPLE_VACANCIES,
                Classification.CANDIDATE,
            }
            and not self.classification_evidence
        ):
            raise ValueError("classification requires source evidence")
        if self.classification == Classification.UNCERTAIN and not any(
            issue.impact == IssueImpact.BLOCKS_POSTING for issue in self.issues
        ):
            raise ValueError("uncertain classification requires a posting-blocking issue")
        for issue in self.issues:
            issue.validate_target(self.posting)
        return self

    def validate_source(self, source: str) -> None:
        """Check every quote against the exact revision before accepting the result."""
        for evidence in self.classification_evidence:
            evidence.validate_source(source)
        if self.posting is None:
            return
        for quotes in self.posting.field_evidence.values():
            for evidence in quotes:
                evidence.validate_source(source)
        for requirement in self.posting.experience_requirements:
            validate_quote(source, requirement.original_text, None, None)
        for offer in self.posting.salary_offers:
            for evidence in offer.evidence:
                evidence.validate_source(source)
        for mention in self.posting.keyword_mentions:
            validate_quote(source, mention.evidence_text, mention.start_offset, mention.end_offset)
