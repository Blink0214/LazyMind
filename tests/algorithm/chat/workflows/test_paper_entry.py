"""Direct manuscript entry: real files and readers, without a model replacement."""
import json

import pytest

from lazymind.document_tools.paper import workflow
from lazymind.document_tools.paper.entry import read_manuscript


def test_text_upload_initializes_without_pool_or_json(tmp_path):
    source = tmp_path / 'paper.md'
    source.write_text('# My paper\n## Results\n12 of 20 questions were correct.')
    pool = workflow.initialize_pool({'manuscript_file': str(source)}, 'Review this paper')
    assert pool['topic'] == 'My paper'
    assert workflow.material(pool, 'manuscript')['content'] == source.read_text()
    assert pool['history'] == []
    assert not workflow.material(pool, 'manuscript')['mock']
    workflow.contracts.validate_input('peer_review', workflow.business_input('peer_review', pool))


def test_plaintext_body_and_requirements_without_business_json(tmp_path):
    body = tmp_path / 'body'
    body.write_text('# Title\nFull manuscript')
    request = tmp_path / 'request'
    request.write_text('Only check evidence')
    pool = workflow.initialize_pool({'manuscript_text': str(body), 'review_request': str(request)}, 'launch command')
    assert pool['request'] == 'Only check evidence'
    assert pool['topic'] == 'Title'


def test_latest_pool_wins_over_original_upload(tmp_path):
    pool = workflow.pool_from_business({'topic': 'T', 'manuscript': 'Latest approved manuscript'})
    saved = tmp_path / 'saved.json'
    saved.write_text(json.dumps(pool))
    result = workflow.initialize_pool({'write_document_pool': str(saved), 'manuscript_file': '/missing/original.pdf'})
    assert workflow.material(result, 'manuscript')['content'] == 'Latest approved manuscript'


def test_conflicting_manuscript_sources_rejected(tmp_path):
    source = tmp_path / 'paper.md'
    source.write_text('# Manuscript')
    with pytest.raises(ValueError, match='同时提供'):
        workflow.initialize_pool({'manuscript_file': str(source), 'manuscript_text': str(source)})


def test_missing_manuscript_cannot_enter_review():
    pool = workflow.initialize_pool({}, 'Review this paper')
    with pytest.raises(workflow.MissingMaterials, match='manuscript'):
        workflow.contracts.validate_input('peer_review', workflow.business_input('peer_review', pool))


@pytest.mark.parametrize('name,content', [('empty.txt', ''), ('paper.exe', 'binary')])
def test_unusable_upload_is_rejected(tmp_path, name, content):
    source = tmp_path / name
    source.write_text(content)
    with pytest.raises(ValueError):
        read_manuscript(str(source))


def test_docx_reader_preserves_body(tmp_path):
    from docx import Document
    source = tmp_path / 'paper.docx'
    document = Document()
    document.add_heading('My manuscript', 0)
    document.add_paragraph('The method answered 15 of 20 questions correctly.')
    document.save(source)
    content = read_manuscript(str(source))
    assert 'My manuscript' in content
    assert '15 of 20' in content


def test_pdf_reader_preserves_body(tmp_path):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    source = tmp_path / 'paper.pdf'
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
                             NameObject('/Subtype'): NameObject('/Type1'),
                             NameObject('/BaseFont'): NameObject('/Helvetica')})
    page[NameObject('/Resources')] = DictionaryObject({
        NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b'BT /F1 12 Tf 50 700 Td (My paper: 15 of 20 questions correct.) Tj ET')
    page[NameObject('/Contents')] = writer._add_object(stream)
    with source.open('wb') as output:
        writer.write(output)
    assert '15 of 20' in read_manuscript(str(source))


def test_image_only_pdf_does_not_silently_review_empty_text(tmp_path):
    from pypdf import PdfWriter
    source = tmp_path / 'scan.pdf'
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open('wb') as output:
        writer.write(output)
    with pytest.raises(ValueError, match='提取文字'):
        read_manuscript(str(source))
