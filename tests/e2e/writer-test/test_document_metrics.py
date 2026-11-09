import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from shared.document_metrics import revision_source_from_trace, writer_document_headings, writer_document_visible_text


def test_revision_source_requires_unambiguous_explicit_input():
    original = '# Original\nBody'
    trace = {'observations': [{'id': 'a', 'input': {'original_markdown': original}, 'output': '# Generated'}]}
    source, audit = revision_source_from_trace(trace)
    assert source == original and audit['status'] == 'recovered'
    trace['observations'].append({'input': {'original_markdown': '# Different'}})
    assert revision_source_from_trace(trace)[1]['status'] == 'ambiguous'
    assert revision_source_from_trace({'observations': [{'output': original}]})[0] is None


def test_wrapped_document_metrics_preserve_nested_headings():
    doc = {'data': {'blocks': [{'type': 'heading', 'content': 'Title', 'children': [
        {'type': 'heading', 'content': 'Nested'}, {'type': 'paragraph', 'content': 'Evidence'}]}]}}
    assert writer_document_headings(doc) == ['Title', 'Nested']
    assert 'Evidence' in writer_document_visible_text(doc)
