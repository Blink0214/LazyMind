"""Shared review contract for the reviewer and downstream revision."""
import hashlib
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

NonEmptyText = Annotated[str, Field(min_length=1, pattern=r'\S')]
Digest = Annotated[str, Field(pattern=r'^[0-9a-f]{64}$')]


class ReviewIssue(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    severity: Literal['major', 'minor', 'suggestion']
    category: Literal['method', 'evidence', 'analysis', 'structure', 'language', 'citation']
    location: NonEmptyText = Field(description='Manuscript section path; identify whole-document or missing content')
    quote: NonEmptyText | None = Field(description='Verbatim manuscript excerpt; null only for whole/missing content')
    problem: NonEmptyText = Field(
        description='Issue and observable evidence; distinguish errors, clarification, unknowns')
    suggestion: NonEmptyText = Field(
        description='Smallest actionable correction; do not invent materials or experiments')
    action_type: Literal['text_revision', 'needs_material', 'needs_experiment']


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    summary: NonEmptyText
    issues: list[ReviewIssue]
    limitations: list[NonEmptyText]


class ReviewRepair(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    replacements: dict[str, Any] = Field(
        description='Only requested paths and replacements; issues.2 is a complete issue')


class QuoteLines(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class RepairedIssue(ReviewIssue):
    quote: QuoteLines | None = Field(
        description='Contiguous source line range copied by code; null only for whole/missing content')


class SavedReviewIssue(ReviewIssue):
    id: Annotated[str, Field(pattern=r'^R[1-9][0-9]*$')]


class SavedReview(ReviewResult):
    issues: list[SavedReviewIssue]
    manuscript_sha256: Digest
    review_sha256: Digest

    @model_validator(mode='after')
    def unique_ids(self):
        ids = [issue.id for issue in self.issues]
        if len(ids) != len(set(ids)):
            raise ValueError('审稿问题编号不能重复')
        return self


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()
