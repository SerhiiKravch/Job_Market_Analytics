"""Stable text codes for domain records and portable database constraints."""

from enum import StrEnum


class ContentKind(StrEnum):
    TEXT = "text"
    CAPTION = "caption"
    EMPTY = "empty"
    UNSUPPORTED = "unsupported"


class Classification(StrEnum):
    SINGLE_VACANCY = "single_vacancy"
    MULTIPLE_VACANCIES = "multiple_vacancies"
    CANDIDATE = "candidate"
    OTHER = "other"
    UNCERTAIN = "uncertain"


class ExtractionOrigin(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class ExtractionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ReviewStatus(StrEnum):
    NOT_REQUIRED = "not_required"
    NEEDS_REVIEW = "needs_review"
    REVIEWED = "reviewed"


class RoleFamily(StrEnum):
    SOFTWARE_ENGINEERING = "software_engineering"
    QUALITY_ASSURANCE = "quality_assurance"
    INFRASTRUCTURE_OPERATIONS = "infrastructure_operations"
    DATA_AI = "data_ai"
    SECURITY = "security"
    PRODUCT_PROJECT_MANAGEMENT = "product_project_management"
    BUSINESS_ANALYSIS = "business_analysis"
    DESIGN_UX = "design_ux"
    RECRUITMENT_HR = "recruitment_hr"
    SALES_BUSINESS_DEVELOPMENT = "sales_business_development"
    MARKETING_GROWTH = "marketing_growth"
    CUSTOMER_SUCCESS_SUPPORT = "customer_success_support"
    OTHER_DIGITAL_BUSINESS = "other_digital_business"
    UNCLASSIFIED = "unclassified"


class Seniority(StrEnum):
    INTERN = "intern"
    TRAINEE = "trainee"
    JUNIOR = "junior"
    MIDDLE = "middle"
    SENIOR = "senior"
    LEAD = "lead"
    STAFF = "staff"
    PRINCIPAL = "principal"


class WorkArrangement(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ON_SITE = "on_site"


class EmploymentSchedule(StrEnum):
    FULL_TIME = "full_time"
    PART_TIME = "part_time"


class OrganizationalArrangement(StrEnum):
    IN_HOUSE = "in_house"
    OUTSTAFF = "outstaff"
    OUTSOURCE = "outsource"


class FieldState(StrEnum):
    STATED = "stated"
    NOT_STATED = "not_stated"
    AMBIGUOUS = "ambiguous"
    CONFLICTING = "conflicting"


class ExperienceScope(StrEnum):
    OVERALL = "overall"
    ROLE_SPECIFIC = "role_specific"
    TECHNOLOGY_SPECIFIC = "technology_specific"
    DOMAIN_SPECIFIC = "domain_specific"
    OTHER = "other"


class ExperienceUnit(StrEnum):
    MONTH = "month"
    YEAR = "year"


class RequirementContext(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    UNSPECIFIED = "unspecified"


class PriorExperiencePolicy(StrEnum):
    REQUIRED = "required"
    EXPLICITLY_NOT_REQUIRED = "explicitly_not_required"
    UNSPECIFIED = "unspecified"


class SalaryBound(StrEnum):
    EXACT = "exact"
    RANGE = "range"
    LOWER_ONLY = "lower_only"
    UPPER_ONLY = "upper_only"


class PaymentPeriod(StrEnum):
    HOUR = "hour"
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    YEAR = "year"
    UNSPECIFIED = "unspecified"


class TaxBasis(StrEnum):
    GROSS = "gross"
    NET = "net"
    UNSPECIFIED = "unspecified"


class CompensationComponent(StrEnum):
    BASE = "base"
    BONUS = "bonus"
    TOTAL = "total"
    UNSPECIFIED = "unspecified"


class KeywordCategory(StrEnum):
    TECHNOLOGY = "technology"
    TOOL_PLATFORM = "tool_platform"
    GENERIC_TOOL_CATEGORY = "generic_tool_category"
    SKILL_METHOD = "skill_method"
    METRIC = "metric"
    INDUSTRY_DOMAIN = "industry_domain"
    PRODUCT_SERVICE_CONTEXT = "product_service_context"
    COMMERCIAL_DELIVERY_MODEL = "commercial_delivery_model"


class MentionContext(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITY = "responsibility"
    BACKGROUND_UNSPECIFIED = "background_unspecified"


class QualificationLevel(StrEnum):
    UNDERSTANDING = "understanding"
    PRACTICAL_EXPERIENCE = "practical_experience"
    PROFICIENCY = "proficiency"
    UNSPECIFIED = "unspecified"


class SectionKind(StrEnum):
    REQUIRED = "required"
    PREFERRED = "preferred"
    RESPONSIBILITIES = "responsibilities"
    OFFER = "offer"
    CONTACTS = "contacts"


class IssueImpact(StrEnum):
    BLOCKS_POSTING = "blocks_posting"
    BLOCKS_METRIC = "blocks_metric"
    INFORMATIONAL = "informational"


class IssueStatus(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"


class MetricCode(StrEnum):
    ROLE = "role"
    KEYWORDS = "keywords"
    SENIORITY = "seniority"
    EXPERIENCE = "experience"
    COMPENSATION = "compensation"
    SALARY = "salary"
    WORK_ARRANGEMENT = "work_arrangement"


class DuplicateMethod(StrEnum):
    EXACT_CONTENT = "exact_content"
    MANUAL = "manual"


class DuplicateStatus(StrEnum):
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
