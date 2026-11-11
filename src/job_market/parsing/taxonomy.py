"""Load and validate versioned rule data from the installed package."""

import unicodedata
from functools import cache
from importlib.resources import files
from typing import Annotated, Self

from pydantic import Field, StringConstraints, model_validator

from job_market.domain.contracts import CanonicalCode, Contract, NonBlank
from job_market.domain.enums import KeywordCategory, QualificationLevel, RoleFamily, SectionKind

Version = Annotated[str, StringConstraints(pattern=r"^[0-9]+\.[0-9]+\.[0-9]+$")]
Terms = Annotated[tuple[NonBlank, ...], Field(min_length=1)]


class ContextualAlias(Contract):
    term: NonBlank
    requires_any: Terms
    case_sensitive: bool = False


class RoleDefinition(Contract):
    code: CanonicalCode
    display_name: NonBlank
    family: RoleFamily
    aliases: Terms
    contextual_aliases: tuple[ContextualAlias, ...] = ()


class KeywordDefinition(Contract):
    canonical_code: CanonicalCode
    display_name: NonBlank
    category_code: KeywordCategory
    description: NonBlank | None = None
    active: bool = True
    aliases: Terms
    contextual_aliases: tuple[ContextualAlias, ...] = ()


class SectionDefinition(Contract):
    kind: SectionKind
    headings: Terms


def _check_unique(values: list[str], label: str) -> None:
    # Use a conservative collision key for validation, not a matching algorithm.
    keys = [" ".join(unicodedata.normalize("NFKC", value).casefold().split()) for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError(f"duplicate {label} in taxonomy")


class RoleResource(Contract):
    version: Version
    roles: tuple[RoleDefinition, ...]

    @model_validator(mode="after")
    def check_roles(self) -> Self:
        _check_unique([role.code for role in self.roles], "role code")
        _check_unique(
            [alias for role in self.roles for alias in role.aliases]
            + [alias.term for role in self.roles for alias in role.contextual_aliases],
            "role alias",
        )
        if {role.family for role in self.roles} != set(RoleFamily) - {RoleFamily.UNCLASSIFIED}:
            raise ValueError("every role family except unclassified must have a taxonomy entry")
        return self


class KeywordResource(Contract):
    version: Version
    keywords: tuple[KeywordDefinition, ...]

    @model_validator(mode="after")
    def check_keywords(self) -> Self:
        _check_unique([keyword.canonical_code for keyword in self.keywords], "keyword code")
        _check_unique(
            [alias for keyword in self.keywords for alias in keyword.aliases]
            + [alias.term for keyword in self.keywords for alias in keyword.contextual_aliases],
            "keyword alias",
        )
        if {keyword.category_code for keyword in self.keywords} != set(KeywordCategory):
            raise ValueError("every keyword category must have a taxonomy entry")
        return self


class QualificationCue(Contract):
    level: QualificationLevel
    terms: Terms


class SectionResource(Contract):
    version: Version
    sections: tuple[SectionDefinition, ...]
    preference_cues: Terms
    qualification_cues: tuple[QualificationCue, ...]

    @model_validator(mode="after")
    def check_sections(self) -> Self:
        _check_unique([section.kind for section in self.sections], "section kind")
        _check_unique([term for section in self.sections for term in section.headings], "heading")
        _check_unique([cue.level for cue in self.qualification_cues], "qualification level")
        if {section.kind for section in self.sections} != set(SectionKind):
            raise ValueError("every section kind must have a taxonomy entry")
        return self


class Taxonomy(Contract):
    version: Version
    roles: tuple[RoleDefinition, ...]
    keywords: tuple[KeywordDefinition, ...]
    sections: tuple[SectionDefinition, ...]
    preference_cues: Terms
    qualification_cues: tuple[QualificationCue, ...]


@cache
def load_taxonomy() -> Taxonomy:
    resource_dir = files("job_market.parsing").joinpath("resources")
    roles = RoleResource.model_validate_json(resource_dir.joinpath("roles.json").read_bytes())
    keywords = KeywordResource.model_validate_json(
        resource_dir.joinpath("keywords.json").read_bytes()
    )
    sections = SectionResource.model_validate_json(
        resource_dir.joinpath("sections.json").read_bytes()
    )
    if roles.version != keywords.version or roles.version != sections.version:
        raise ValueError("taxonomy resource versions must match")
    return Taxonomy(
        version=roles.version,
        roles=roles.roles,
        keywords=keywords.keywords,
        sections=sections.sections,
        preference_cues=sections.preference_cues,
        qualification_cues=sections.qualification_cues,
    )
