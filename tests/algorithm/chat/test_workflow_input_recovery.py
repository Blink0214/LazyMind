"""Regression coverage for trigger schema, empty queries and input recovery."""
import json
from unittest.mock import MagicMock, patch

import pytest
from lazyllm.tools.agent.toolsManager import ToolManager
from lazyllm.module.llms.onlinemodule.supplier.minimax import MinimaxChat
from lazymind.chat.workflow.workflow_manager import _workflow_trigger_tools, _safe_session_tools
from lazymind.workflow_sdk import WorkflowClientError


@pytest.fixture
def runtime():
    client, toolkit = MagicMock(), MagicMock()
    client.get_workflow.return_value.result = {
        'workflow_id': 'paper-writing', 'revision_id': 'pinned',
        'compiled_graph': {
            'material_types': {'paper_input': 'json'},
            'material_producers': {'paper_input': {'kind': 'external'}},
        },
    }
    client.get_state.return_value = {
        'session_id': 'session', 'state_version': 1,
        'projection': {'ready': ['write_document']},
    }
    toolkit.prepare_workflow.return_value = {'session_id': 'session'}
    toolkit.list_workflow_inputs.return_value = {'inputs': []}
    holder = {}
    resource = {'resource_id': 'resource', 'revision': 1, 'content_hash': 'sha256:materials'}
    with patch('lazymind.chat.workflow.workflow_manager._client', return_value=client), patch(
        'lazymind.chat.workflow.workflow_manager.HostWorkflowToolkit', return_value=toolkit,
    ), patch('lazymind.chat.workflow.workflow_manager._import_text_binding', return_value=resource) as importer:
        trigger = _workflow_trigger_tools([{
            'workflow_id': 'paper-writing', 'workflow_ref': 'builtin:paper-writing',
            'revision_id': 'pinned', 'tool_name': 'trigger_paper_writing_workflow',
        }], {'builtin:paper-writing'}, 'Write from the attachment', 'conversation', holder)[0]
        yield trigger, toolkit, holder, importer, resource


def test_minimax_wire_schema_and_correction_preserve_input(runtime):
    trigger, toolkit, holder, importer, resource = runtime
    manager = ToolManager(tools=[trigger])
    captured = {}

    class Captured(Exception):
        pass

    def capture(self, data, **kwargs):
        captured.update(data)
        raise Captured()

    with patch.object(MinimaxChat, '_forward_with_retry', capture), pytest.raises(Captured):
        MinimaxChat(api_key='no-network', model='MiniMax-M3').forward(
            'Write the paper', tools=manager.tools_description,
        )
    schema = captured['tools'][0]['function']['parameters']
    assert set(schema['properties']) == {'input_bindings', 'request_context'}
    assert schema['additionalProperties'] is False
    assert 'preserving all fields' in schema['properties']['input_bindings']['description']
    bindings = {'paper_input': json.dumps({'topic': 'Synthetic', 'request': 'Write', 'charts': []})}
    _, failure = manager._validate_tool(trigger.__name__, {'inputBindings': bindings})
    assert "Rename argument 'inputBindings' to 'input_bindings'" in failure['value']
    assert 'preserving its complete value' in failure['value']
    assert holder == {}
    toolkit.prepare_workflow.assert_not_called()
    arguments, failure = manager._validate_tool(trigger.__name__, {'input_bindings': bindings})
    assert failure is None
    trigger(**arguments)
    importer.assert_called_once_with('paper_input', bindings['paper_input'])
    assert toolkit.prepare_workflow.call_args.kwargs['input_bindings'] == {'paper_input': resource}


def test_queries_never_create_empty_session(runtime):
    trigger, toolkit, holder, _, _ = runtime
    initializer = MagicMock(side_effect=trigger)
    tools = _safe_session_tools(toolkit, lambda: holder.get('session_id', ''), initializer)
    for tool in tools:
        if tool.__name__ in {'get_workflow_state', 'get_ready_steps', 'list_workflow_inputs', 'list_artifacts'}:
            with pytest.raises(WorkflowClientError, match='Call the selected trigger'):
                tool()
    initializer.assert_not_called()
    toolkit.prepare_workflow.assert_not_called()


def test_existing_empty_session_accepts_inputs_and_repeated_input_is_idempotent(runtime):
    trigger, toolkit, holder, _, resource = runtime
    holder['session_id'] = 'session'
    bindings = {'paper_input': '{"topic":"Synthetic","request":"Write"}'}
    trigger(bindings)
    toolkit.prepare_workflow.assert_not_called()
    toolkit.bind_workflow_input.assert_called_once_with('session', 'paper_input', resource)
    # The public API currently serializes ORM binding fields using Go names.
    toolkit.list_workflow_inputs.return_value = {'inputs': [
        {'MaterialID': 'paper_input', 'ContentHash': resource['content_hash']},
    ]}
    trigger(bindings)
    assert toolkit.bind_workflow_input.call_count == 1
    toolkit.list_workflow_inputs.return_value['inputs'][0]['ContentHash'] = 'sha256:other'
    with pytest.raises(WorkflowClientError, match='different content'):
        trigger(bindings)
    assert toolkit.bind_workflow_input.call_count == 1


def test_later_chat_turn_can_supply_inputs_to_pinned_session(runtime):
    from lazymind.chat.workflow.workflow_manager import resolve_workflow_injection
    _, toolkit, _, importer, resource = runtime
    contribution = resolve_workflow_injection(
        {'session_id': 'session', 'workflow_id': 'paper-writing', 'revision_id': 'pinned'},
        conversation_id='conversation', current_query='Retry from the attachment',
    )
    repair = next(tool for tool in contribution.tools if getattr(tool, '__name__', '') == 'supply_workflow_inputs')
    repair({'paper_input': '{"topic":"Synthetic","request":"Write"}'})
    toolkit.bind_workflow_input.assert_called_once_with('session', 'paper_input', resource)
    toolkit.prepare_workflow.assert_not_called()
