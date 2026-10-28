"""A short scientific outline, not an inventory of individual scalar calculations."""
from typing import Annotated

from pydantic import Field

from .schema import Object, Text, parse


class Plan(Object):
    questions: Annotated[list[Text], Field(min_length=1)]
    outline: Text = Field(description=(
        'Concise prose: methods, variables, grouping/comparison, preparation/missingness, '
        'required outputs and limitations. Do not enumerate scalar jobs or plan figures yet.'))


def validate_plan(value, inputs=None):
    return parse(Plan, value, 'analysis.plan', extra='ignore')


def questions(plan):
    return [dict(id=f'q{index}', question=question)
            for index, question in enumerate(plan['questions'], 1)]
