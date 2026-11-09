"""Current evidence-only functional summaries retain observations without verdicts."""
import sys
from pathlib import Path
sys.path[:0] = [str(Path(__file__).resolve().parents[2]), str(Path(__file__).resolve().parents[1] / 'analyzers')]
from analyze_func import functional_facts
from shared.observability import StreamRecord


def test_functional_facts_keep_incomplete_streams_and_no_verdict():
    stream = StreamRecord('s', events=['artifact_stream_start', 'artifact_stream_delta'], text='partial')
    result = functional_facts('body', revision=2, streams=[stream], ui={'final_panel_visible': False}, use_ui=True)
    assert result['draft']['characters'] == 4
    assert result['draft']['revision'] == 2
    assert result['streams'][0]['characters'] == 7
    assert result['streams'][0]['events'] == stream.events
    assert result['ui']['final_panel_visible'] is False
    assert 'status' not in result and 'verdict' not in result
