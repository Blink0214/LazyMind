import json

import pytest
from lazyllm.tools.agent import ToolExecutionError
from lazyllm.tools.writer.data_models import WriterBlock, WriterDocument
from lazyllm.tools.writer.provider import WriterProviderDocument

from lazymind.chat.engine.tools.writer import (
    WriterCreateToolkit as LegacyWriterCreateToolkit,
)
from lazymind.chat.engine.tools import (
    WriterRevisionToolkit as RegisteredWriterRevisionToolkit,
)
from lazymind.document_tools import (
    DocumentResourceToolkit,
    DocumentRevisionToolkit,
    DocumentWritingToolkit,
    WriterCreateToolkit,
    WriterResourceToolkit,
    WriterRevisionToolkit,
    document_action_names,
    get_document_action,
    register_document_action,
)
from lazymind.document_tools.artifacts import (
    WriterArtifactCapabilities,
    _normalize_streamed_markdown_section,
    lmd_to_markdown,
    markdown_to_lmd,
    markdown_to_writer_document,
)
from lazymind.document_tools.resources import WriterResourceCapabilities
from lazymind.document_tools import resources as document_resources
from lazymind.document_tools.references import bind_cross_reference_targets
from lazymind.document_tools.revision import WriterRevisionCapabilities
from lazymind.document_tools.toolkits import WriterToolkitBase
from lazymind.document_tools.writing import WriterWritingCapabilities


WRITING_CHAT_APIS = {
    "build_writing_task",
    "build_resources",
    "profile_resources",
    "create_writing_context",
    "prepare_outline",
    "generate_outline",
    "generate_rewrite_outline",
    "generate_rewrite_section_instructions",
    "generate_section_instructions",
    "generate_draft_section",
    "generate_draft_section_markdown",
    "generate_draft_blocks",
    "generate_draft_blocks_markdown",
    "generate_draft_document",
    "generate_draft_document_markdown",
    "update_writing_context",
    "check_consistency",
    "generate_final_document",
    "render_markdown",
}
REVISION_CHAT_APIS = {
    "build_revise_task",
    "build_revision_task",
    "locate_revision_target",
    "generate_modify_plan",
    "build_revision_visual_plan",
    "generate_patch_set",
    "generate_string_replace_set",
    "plan_revision",
    "validate_patch_set",
    "apply_patch",
    "apply_string_replace",
    "apply_revision",
}
RESOURCE_CHAT_APIS = {
    "load_document",
    "create_document",
    "publish_revision",
    "convert_document",
    "write_document",
}
WORKFLOW_ONLY_APIS = {
    "collect_available_media",
    "resolve_visual_needs",
    "materialize_acquired_media",
    "stream_outline",
    "execute_writing_subtasks",
    "stream_draft_blocks_ir",
    "stream_draft_blocks_markdown",
    "resolve_create_target",
    "prepare_markdown_for_editor",
}


def test_legacy_writer_import_uses_shared_document_toolkit():
    assert LegacyWriterCreateToolkit is WriterCreateToolkit
    assert DocumentWritingToolkit is WriterCreateToolkit
    assert DocumentRevisionToolkit is WriterRevisionToolkit
    assert DocumentResourceToolkit is WriterResourceToolkit
    assert RegisteredWriterRevisionToolkit is WriterRevisionToolkit


def test_legacy_writer_import_retains_provider_integration_symbols():
    from lazymind.chat.engine.tools.writer import (
        ToolExecutionError as LegacyToolExecutionError,
        WriterResourceTools as LegacyWriterResourceTools,
        _prepare_wechat_cover,
        _published_link,
    )

    assert LegacyToolExecutionError is ToolExecutionError
    assert LegacyWriterResourceTools.__name__ == "WriterResourceTools"
    assert callable(_prepare_wechat_cover)
    assert callable(_published_link)


def test_document_action_registry_is_explicit_and_deterministic():
    def preview():
        return "preview"

    register_document_action("test_document_action", "preview", preview)

    assert get_document_action("test_document_action", "preview") is preview
    assert "test_document_action" in document_action_names()

    with pytest.raises(ValueError, match="already registered"):
        register_document_action("test_document_action", "preview", preview)

    with pytest.raises(ValueError, match="unsupported document action phase"):
        register_document_action("test_document_action", "apply", preview)


def test_concrete_toolkits_own_disjoint_capability_sets():
    assert issubclass(WriterCreateToolkit, WriterWritingCapabilities)
    assert issubclass(WriterCreateToolkit, WriterArtifactCapabilities)
    assert not issubclass(WriterCreateToolkit, WriterRevisionCapabilities)
    assert not issubclass(WriterCreateToolkit, WriterResourceCapabilities)

    assert issubclass(WriterRevisionToolkit, WriterRevisionCapabilities)
    assert not issubclass(WriterRevisionToolkit, WriterWritingCapabilities)
    assert not issubclass(WriterRevisionToolkit, WriterResourceCapabilities)

    assert issubclass(WriterResourceToolkit, WriterResourceCapabilities)
    assert not issubclass(WriterResourceToolkit, WriterWritingCapabilities)
    assert not issubclass(WriterResourceToolkit, WriterRevisionCapabilities)


def test_all_45_capabilities_have_one_physical_owner():
    owners = (
        WriterWritingCapabilities,
        WriterArtifactCapabilities,
        WriterRevisionCapabilities,
        WriterResourceCapabilities,
    )
    owned = {
        owner: {
            name
            for name, value in vars(owner).items()
            if not name.startswith("_") and callable(value)
        }
        for owner in owners
    }

    resource_workflow_apis = {
        "resolve_create_target",
        "prepare_markdown_for_editor",
    }
    assert owned[WriterWritingCapabilities] == (
        WRITING_CHAT_APIS - {"render_markdown"}
    ) | (WORKFLOW_ONLY_APIS - resource_workflow_apis)
    assert owned[WriterArtifactCapabilities] == {"render_markdown"}
    assert owned[WriterRevisionCapabilities] == REVISION_CHAT_APIS
    assert owned[WriterResourceCapabilities] == (
        RESOURCE_CHAT_APIS | resource_workflow_apis
    )
    assert sum(len(names) for names in owned.values()) == 45


def test_chat_toolkit_exposure_remains_the_36_tool_snapshot():
    assert set(WriterCreateToolkit.__public_apis__) == WRITING_CHAT_APIS
    assert set(WriterRevisionToolkit.__public_apis__) == REVISION_CHAT_APIS
    assert set(WriterResourceToolkit.__public_apis__) == RESOURCE_CHAT_APIS
    assert (
        sum(
            map(
                len,
                (
                    WriterCreateToolkit.__public_apis__,
                    WriterRevisionToolkit.__public_apis__,
                    WriterResourceToolkit.__public_apis__,
                ),
            )
        )
        == 36
    )
    assert WORKFLOW_ONLY_APIS.isdisjoint(WriterCreateToolkit.__public_apis__)
    assert WORKFLOW_ONLY_APIS.isdisjoint(WriterResourceToolkit.__public_apis__)


def test_legacy_aggregate_retains_all_45_capabilities():
    expected = (
        WRITING_CHAT_APIS | REVISION_CHAT_APIS | RESOURCE_CHAT_APIS | WORKFLOW_ONLY_APIS
    )
    capabilities = {
        name
        for name in dir(WriterToolkitBase)
        if not name.startswith("_") and callable(getattr(WriterToolkitBase, name))
    }
    assert capabilities == expected
    assert len(capabilities) == 45


def test_markdown_heading_normalization_accepts_tab_separator():
    instruction = type("Instruction", (), {"section_title": "标题"})()
    assert (
        _normalize_streamed_markdown_section("##\t标题\n\n正文", instruction)
        == "## 标题\n\n正文"
    )


def test_provider_locator_stops_at_ascii_whitespace(monkeypatch):
    captured = []

    class FakeProvider:
        def resolve(self, locator):
            captured.append(locator)
            return type("Target", (), {"uri": locator, "adapter": "fake", "meta": {}})()

    monkeypatch.setattr(
        document_resources, "match_writer_provider", lambda _locator: FakeProvider()
    )

    targets = document_resources.resolve_provider_targets(
        "读取 fake://document-id 然后继续写作"
    )

    assert captured == ["fake://document-id"]
    assert len(targets) == 1


def test_markdown_lmd_conversion_uses_existing_writer_rules():
    markdown = "# 标题\n\n## 第一节\n\n正文。\n"

    document = markdown_to_writer_document(
        markdown,
        document_id="document-1",
        stage="draft",
    )
    lmd = markdown_to_lmd(
        markdown,
        document_id="document-1",
        stage="draft",
    )
    envelope = json.loads(lmd)

    assert document.document_id == "document-1"
    assert document.stage == "draft"
    assert document.title == "标题"
    assert {key: value for key, value in envelope.items() if key != "meta"} == {
        "schema": "lazyllm.tools.writer.data_models.writer_ir.WriterDocument",
        "schema_version": "0.1",
        "data": {
            "document_id": "document-1",
            "title": "标题",
            "blocks": [
                {
                    "node_id": "document-1-heading-1",
                    "type": "heading",
                    "numbering": {"level": 1},
                    "content": "第一节",
                    "children": [
                        {
                            "node_id": "document-1-paragraph-2",
                            "type": "paragraph",
                            "content": "正文。",
                            "spans": [{"text": "正文。"}],
                        }
                    ],
                }
            ],
            "metadata": {
                "source": "parse_document_markdown",
                "outline_id": None,
            },
        },
    }
    assert envelope["meta"]["created_by"] == "lazyllm-writer-conversion"
    assert lmd_to_markdown(lmd) == (
        '# 标题\n\n<a id="block-document-1-heading-1"></a>\n'
        "## 1\\. 第一节\n\n正文。\n"
    )


def test_cross_reference_binding_discovers_and_refreshes_all_targets():
    instructions = [
        {
            "meta": {
                "outline_node_id": "section-1",
                "cross_references": [{"target": "section-2"}],
                "cross_reference_targets": ["stale"],
            }
        },
        {"meta": {"outline_node_id": "section-2"}},
    ]

    bind_cross_reference_targets(instructions)

    assert instructions[0]["meta"]["cross_reference_targets"] == [
        "section-1",
        "section-2",
    ]
    assert instructions[1]["meta"]["cross_reference_targets"] == [
        "section-1",
        "section-2",
    ]


def _bound_document(provider="fake"):
    return WriterDocument(
        document_id="local-1",
        title="Draft",
        stage="final",
        revision="remote-1",
        provider_binding={
            "provider": provider,
            "document_id": "remote-1",
            "uri": f"{provider}:/remote-1",
        },
        blocks=[WriterBlock(
            node_id="body",
            type="paragraph",
            content="before",
            provider_binding={"provider": provider, "block_id": "block-1"},
            provider_payload={"remote": True},
        )],
    )


def test_conversion_is_pure_and_cross_provider_content_is_detached(monkeypatch):
    calls = []
    class FakeProvider:
        def convert_document(self, content, *, target=None, media_assets=None):
            calls.append((content, target, media_assets))
            return WriterProviderDocument(
                provider="destination",
                format="fake_blocks",
                content=[{"text": content.blocks[0].content}],
                source_document=content,
            )

    monkeypatch.setattr(
        document_resources, "get_writer_provider", lambda _provider: FakeProvider()
    )
    result = document_resources.convert_document(
        _bound_document("source").model_dump(), "destination"
    )

    detached = calls[0][0]
    assert detached.revision is None
    assert detached.provider_binding == {}
    assert detached.metadata.get("source") is None
    assert detached.blocks[0].provider_binding == {}
    assert detached.blocks[0].provider_payload == {}
    assert result["content"] == [{"text": "before"}]


def test_write_consumes_conversion_without_converting_again(monkeypatch):
    calls = []
    persisted = _bound_document("fake")
    converted = WriterProviderDocument(
        provider="fake",
        format="fake_blocks",
        content=[{"text": "before"}],
        source_document=persisted,
    )

    class FakeProvider:
        def require_capability(self, capability):
            calls.append(("require", capability))

        def write_document(self, document, target, *, media_assets=None, mode="replace"):
            calls.append(("write", document, target, media_assets, mode))
            return {
                "doc_id": "remote-1",
                "adapter": "fake",
                "locator": "https://example.test/remote-1",
                "persisted_document": persisted,
                "representation": "ir",
            }

    monkeypatch.setattr(
        document_resources, "get_writer_provider", lambda _provider: FakeProvider()
    )
    result = document_resources.write_document(
        converted.model_dump(),
        target_document={"adapter": "fake", "doc_id": "remote-1"},
    )

    assert [call[0] for call in calls] == ["require", "write"]
    assert calls[1][1].content == [{"text": "before"}]
    assert result["provider"] == "fake"
    assert result["persisted_document"]["provider_binding"]["provider"] == "fake"

    serialized = json.loads(WriterResourceCapabilities().write_document(
        converted_document_json=converted.model_dump_json(),
        target_document_json=json.dumps({"adapter": "fake", "doc_id": "remote-1"}),
    ))
    assert serialized["publish_result"]["persisted_document"]["document_id"] == "local-1"
