"""Lossless material adaptation through the current split Writer modules."""
import copy
import json
from pathlib import Path

import pytest
from lazyllm.tools.writer.data_models.context import WritingContext
from lazyllm.tools.writer.data_models.task import WritingTask
from lazymind.document_tools import artifacts, writing
from lazymind.document_tools.paper import contracts
from lazymind.document_tools.paper.writing import common


@pytest.fixture(autouse=True)
def isolated_builders(monkeypatch, tmp_path):
    monkeypatch.setattr(writing, '_temp_root', lambda: tmp_path)
    monkeypatch.setattr(artifacts, '_temp_root', lambda: tmp_path)
    monkeypatch.setattr(writing, 'AutoModel', lambda **kw: pytest.fail('Material builders must not call a model'))


def source(step='write_document'):
    raw = json.loads((Path(__file__).resolve().parents[4] /
        'tests/fixtures/paper-writing/write_document.json').read_text())['input']
    raw.update(analysis=None, charts=[], question='# Research question\nPreserve supplied hypotheses.',
               outline='# Paper\n\n## Methods', manuscript=None, review=None, review_data=None,
               selected_issue_ids=[], request='Use only supplied evidence')
    if step == 'revise_document':
        raw.update(mode='revise', manuscript='# Paper\n\n## Methods\nKeep this evidence.', outline=None)
    return contracts.validate_input(step, raw)


def facts(bundle):
    return {fact['key']: json.loads(fact['value']) for fact in bundle['writing_context']['facts']}


@pytest.mark.parametrize('step', ['write_document', 'revise_document'])
def test_real_writer_context_is_schema_compatible_and_lossless(step):
    value = source(step)
    value['papers'][0]['text'] += '\n' + 'Long evidence. ' * 1000
    before = copy.deepcopy(value)
    bundle = common.prepare_writer_context(value)
    WritingTask.model_validate(bundle['writing_task'])
    WritingContext.model_validate(bundle['writing_context'])
    key = 'write_document:paper:' + value['papers'][0]['id']
    assert facts(bundle)[key] == value['papers'][0]
    assert facts(bundle)['write_document:question'] == value['question']
    assert value == before
    assert bundle['writing_task']['task_type'] == ('revise' if step == 'revise_document' else 'write')


@pytest.mark.parametrize('language,target', [('zh-CN', None), ('zh-CN', 3000), ('en', 2000)])
def test_structured_constraints_override_incidental_numbers(language, target):
    value = source()
    value['request'] = 'The source mentions 100 words; follow structured constraints.'
    value['constraints'].update(paper_language=language, word_target=target)
    bundle = common.prepare_writer_inputs(value)
    constraints = bundle['writing_task']['constraints']
    assert all(constraints[key] == val for key, val in value['constraints'].items())
    if language == 'zh-CN' and target is not None:
        assert constraints['target_chars'] == target
        assert constraints['max_chars'] == int(target * 1.1)
    else:
        assert 'target_chars' not in constraints and 'max_chars' not in constraints


@pytest.mark.parametrize('material,step', [('outline', 'write_document'), ('manuscript', 'revise_document')])
def test_existing_document_only_preserves_exact_content(material, step):
    value = source(step)
    value[material] = '# Title\r\n\r\n## Methods\r\nKeep exact bytes.\r\n'
    bundle = common.prepare_writer_context(value)
    assert bundle[material] == value[material]
    assert facts(bundle)['write_document:' + material] == value[material]


def test_context_failure_propagates(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('context failed')
    monkeypatch.setattr(common.WriterCreateToolkit, 'create_writing_context', fail)
    with pytest.raises(RuntimeError, match='context failed'):
        common.prepare_writer_context(source())


def test_only_selected_review_issues_enter_writer_context():
    value = source('revise_document')
    value['selected_issue_ids'] = ['R2']
    value['review_data'] = {'issues': [{'id': 'R1', 'suggestion': 'UNSELECTED'},
                                     {'id': 'R2', 'suggestion': 'SELECTED'}], 'limitations': []}
    bundle = common.prepare_writer_context(value)
    assert facts(bundle)['write_document:review']['issues'] == [value['review_data']['issues'][1]]
    assert 'UNSELECTED' not in json.dumps(bundle['writing_context'])
