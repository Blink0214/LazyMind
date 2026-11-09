"""Supplemental no-image planning boundary regression."""
from lazymind.document_tools import execution


def test_short_no_image_plan_keeps_required_artifact(monkeypatch, tmp_path):
    saved = []
    monkeypatch.setattr(execution, '_read_json_file', lambda _: {
        'constraints': {'visual_policy': {'allow_visuals': False}}})
    monkeypatch.setattr(execution, '_run_root', lambda _: tmp_path)
    monkeypatch.setattr(execution, '_save_json_artifact',
                        lambda name, *a, **kw: saved.append(name) or str(tmp_path / (name + '.json')))
    result = execution._writer_generate_short_visual_plan('task', 'plan', 'context')
    assert result['visual_need_count'] == 0
    assert result['visual_plan'].endswith('visual_plan.json')
    assert saved == ['visual_plan']
