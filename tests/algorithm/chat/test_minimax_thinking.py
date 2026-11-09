import pytest

from lazyllm import OnlineChatModule
from lazyllm.module.llms.onlinemodule.chat import dynamic_chat_config
from lazyllm.module.llms.onlinemodule.supplier.minimax import MinimaxChat


@pytest.fixture
def requests_sent(monkeypatch):
    sent = []

    def capture(self, data, **kwargs):
        sent.append(data)
        return [{'choices': [{'message': {'content': 'ok'}, 'finish_reason': 'stop'}]}]

    monkeypatch.setattr(MinimaxChat, '_forward_with_retry', capture)
    return sent


@pytest.mark.parametrize('stream', [True, False])
def test_m3_enables_thinking_in_request(requests_sent, stream):
    model = OnlineChatModule(source='minimax', model='MiniMax-M3', api_key='no-network')
    model.forward('Write an article', stream=stream)
    assert requests_sent[-1]['thinking'] == {'type': 'adaptive'}
    assert requests_sent[-1]['stream'] is stream


@pytest.mark.parametrize('mode', ['disabled', 'adaptive'])
def test_explicit_thinking_is_preserved(requests_sent, mode):
    model = OnlineChatModule(source='minimax', model='MiniMax-M3', api_key='no-network')
    model.forward('Write an article', thinking={'type': mode})
    assert requests_sent[-1]['thinking'] == {'type': mode}


def test_dynamic_model_switch_uses_effective_model(requests_sent):
    model = OnlineChatModule(source='dynamic', skip_auth=True).share(stream=False)
    for name in ['MiniMax-M3', 'MiniMax-M2.7', 'MiniMax-M3.1-Flash', 'MiniMax-M3']:
        with dynamic_chat_config(source='minimax', model=name, skip_auth=True):
            model('Write an article')
        assert requests_sent[-1]['model'] == name
        if name == 'MiniMax-M3':
            assert requests_sent[-1]['thinking'] == {'type': 'adaptive'}
        else:
            assert 'thinking' not in requests_sent[-1]
