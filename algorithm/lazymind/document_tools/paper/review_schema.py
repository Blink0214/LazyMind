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
    location: NonEmptyText = Field(description='原文章节路径；全文或缺失内容可标全文')
    quote: NonEmptyText | None = Field(description='逐字引用原文；全文或缺失内容建议为 null')
    problem: NonEmptyText = Field(description='问题及其可观察依据，区分错误、待澄清和无法判断')
    suggestion: NonEmptyText = Field(description='最小可执行修改建议，不虚构材料或实验')
    action_type: Literal['text_revision', 'needs_material', 'needs_experiment']


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    summary: NonEmptyText
    issues: list[ReviewIssue]
    limitations: list[NonEmptyText]


class ReviewRepair(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    replacements: dict[str, Any] = Field(description='仅返回要求修复的路径及替换值，如 issues.2 对应一条完整意见')


class QuoteLines(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class RepairedIssue(ReviewIssue):
    quote: QuoteLines | None = Field(description='原稿连续行的起止行号，由程序复制原文；仅全文/缺失内容可为空')


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
