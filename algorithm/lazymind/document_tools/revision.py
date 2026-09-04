"""AI rewrite and validated document patch capabilities."""

from __future__ import annotations
from typing import Any

from lazyllm import AutoModel
from lazyllm.tools.agent import ToolExecutionError
from lazyllm.tools.writer.data_models import (
    ContentRef,
    ModifyInstruction,
    ModifyPlan,
    PatchSet,
    StringReplaceSet,
    TargetDocument,
    VisualInstruction,
    VisualPlan,
    WriterDocument,
    WritingTask,
)
from lazyllm.tools.writer.tools import WriterQualityTools, WriterRevisionTools
from lazyllm.tools.writer.tools.revision_tools import apply_patch_to_ir
from lazyllm.tools.writer.utils import load_artifact_json
from .artifacts import (
    WRITER_BLOCK_SCHEMA,
    WRITER_IR_SCHEMA,
    _document_value,
    _json_dumps,
    _json_loads,
    _primary_data,
    _read_artifact_data,
    _set_document_editable,
    _temp_root,
    _write_document_input,
    _write_input_artifact,
    writer_schema,
)
from .resources import _target_from_document


def preview_selection_rewrite(
    document: str | dict,
    instruction: str,
    selection: dict,
    context: Any,
    *,
    artifact_store: str,
    flat_markdown: bool = False,
) -> dict[str, Any]:
    """Generate one selected-block rewrite candidate and its deterministic patch."""
    instruction = str(instruction or "").strip()
    if not instruction:
        raise ValueError("instruction must not be empty.")
    revision = WriterRevisionTools(
        llm=AutoModel(model="llm"), artifact_store=artifact_store
    )
    selection_type = str((selection or {}).get("type") or "")
    if isinstance(document, dict):
        source = WriterDocument.model_validate(document)
        if selection_type != "ir":
            raise ValueError("IR artifacts require selection.type='ir'.")
        node_id = str(selection.get("node_id") or "")
        target = source.block_by_id(node_id)
        if target is None:
            raise ValueError("The selected IR node no longer exists.")
        plan = ModifyPlan(
            scope="block",
            instructions=[
                ModifyInstruction(
                    instruction_id="rewrite-selection",
                    content_ref=ContentRef(node_id=node_id),
                    modify_type="update",
                    instruction=instruction,
                )
            ],
        )
        output = revision.generate_patch_set(source, plan, context)
        patch_set = load_artifact_json(output["artifact_path"], PatchSet)
        revised, _ = apply_patch_to_ir(source, patch_set)
        return {
            "representation": "ir",
            "target": {"type": "block", "block_type": target.type, "node_id": node_id},
            "preview": {
                "old_text": target.content,
                "new_text": revised.block_by_id(node_id).content,
            },
            "patch": {"type": "writer_ir_patch", "payload": patch_set.model_dump()},
            "revised_document": revised,
        }

    if selection_type != "markdown":
        raise ValueError("Markdown artifacts require selection.type='markdown'.")
    if flat_markdown:
        instruction += (
            "\nKeep the replacement as exactly one Markdown paragraph; do not split it."
        )
    replace_set = StringReplaceSet.model_validate(
        revision.build_selected_markdown_replace_set(
            document,
            instruction,
            str(selection.get("selected_text") or ""),
            context,
        )
    )
    replacement = replace_set.replacements[0]
    output = revision.apply_string_replace(document, replace_set, context)
    return {
        "representation": "markdown",
        "target": {"type": "block", "block_type": "paragraph"},
        "preview": {
            "old_text": replacement.old_string,
            "new_text": replacement.new_string,
        },
        "patch": {"type": "string_replace_set", "payload": replace_set.model_dump()},
        "revised_document_md": output["revised_document_md"],
    }


class WriterRevisionCapabilities:
    WRITER_IR_SCHEMA = WRITER_IR_SCHEMA
    WRITER_BLOCK_SCHEMA = WRITER_BLOCK_SCHEMA

    def build_revise_task(self, query: str, target_document_json: str = "") -> str:
        """Build a revise-type WritingTask from the user's revision request."""
        target_document = None
        if target_document_json:
            target_document = TargetDocument.model_validate(
                _json_loads(target_document_json, {}),
            )
        task = WritingTask(
            query=query,
            task_type="revise",
            scope="auto",
            target_document=target_document,
        )
        return _json_dumps(task.model_dump(exclude_defaults=True))

    def build_revision_task(
        self,
        query: str,
        writer_document_json: str,
        allow_outline: bool = True,
    ) -> str:
        """Build a revision task directly from its current document."""
        source = _document_value(writer_document_json)
        document = (
            WriterDocument.model_validate(source) if isinstance(source, dict) else None
        )
        if document and document.stage == "outline" and not allow_outline:
            raise ToolExecutionError(
                "A full-document revision cannot use an outline-stage document.",
            )
        target = _target_from_document(document) if document else None
        return self.build_revise_task(
            query=query,
            target_document_json=(
                _json_dumps(target.model_dump(exclude_defaults=True)) if target else ""
            ),
        )

    def validate_patch_set(
        self,
        patch_set_json: str,
        writing_context_json: str,
        writing_task_json: str,
    ) -> str:
        """Validate a PatchSet and return its audit result."""
        root = _temp_root()
        patch_set_path = _write_input_artifact(
            root,
            "patch_set.json",
            _json_loads(patch_set_json, {}),
            writer_schema("revision.PatchSet"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        result = WriterQualityTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).validate_patch_set(
            patch_set=patch_set_path,
            context=context_path,
            task=task_path,
        )
        return _json_dumps(
            {
                "patch_set_review": _primary_data(result),
                "patch_set_review_summary": result.get("summary") or "",
            }
        )

    def locate_revision_target(
        self,
        writing_task_json: str,
        writer_document_json: str,
        writing_context_json: str,
    ) -> str:
        """Locate the IR or Markdown content affected by a revision task."""
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        document_path = _write_document_input(
            root, "writer_document", writer_document_json
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterRevisionTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).locate_revision_target(
            task=task_path, document=document_path, context=context_path
        )
        return _json_dumps(_primary_data(result))

    def generate_modify_plan(
        self,
        writing_task_json: str,
        writer_document_json: str,
        locate_result_json: str,
        writing_context_json: str,
    ) -> str:
        """Generate a structured modification plan for the located targets."""
        root = _temp_root()
        task_path = _write_input_artifact(
            root,
            "writing_task.json",
            _json_loads(writing_task_json, {}),
            writer_schema("task.WritingTask"),
        )
        document_path = _write_document_input(
            root, "writer_document", writer_document_json
        )
        locate_path = _write_input_artifact(
            root,
            "locate_result.json",
            _json_loads(locate_result_json, {}),
            writer_schema("revision.LocateResult"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterRevisionTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).generate_modify_plan(
            task=task_path,
            document=document_path,
            locate_result=locate_path,
            context=context_path,
        )
        return _json_dumps(_primary_data(result))

    def build_revision_visual_plan(self, modify_plan_json: str) -> str:
        """Extract the explicit visual needs from a structured revision plan."""
        plan = ModifyPlan.model_validate(_json_loads(modify_plan_json, {}))
        instructions: list[VisualInstruction] = []
        for instruction in plan.instructions:
            visual = instruction.visual_instruction
            if visual is None:
                continue
            if instruction.modify_type != "create":
                raise ToolExecutionError(
                    "visual_instruction is only valid for create instructions."
                )
            if visual.visual_type not in {"image", "diagram", "chart", "table"}:
                raise ToolExecutionError(
                    "revision visual_instruction.visual_type must be image, diagram, chart, or table."
                )
            if visual.need_id != instruction.instruction_id:
                raise ToolExecutionError(
                    "visual_instruction.need_id must equal instruction_id."
                )
            if visual.content_ref != instruction.content_ref:
                raise ToolExecutionError(
                    "visual_instruction.content_ref must equal content_ref."
                )
            if not visual.purpose.strip() or not visual.required:
                raise ToolExecutionError(
                    "revision image visual_instruction must be required and non-empty."
                )
            allowed_strategies = {None, "image_generation"}
            if visual.visual_type in {"image", "diagram"}:
                allowed_strategies.add("web_search")
            if visual.preferred_strategy not in allowed_strategies:
                raise ToolExecutionError(
                    "revision visual preferred_strategy is not supported for its visual_type."
                )
            instructions.append(visual)
        return _json_dumps(
            VisualPlan(instructions=instructions).model_dump(exclude_defaults=True)
        )

    def generate_patch_set(
        self,
        writer_document_json: str,
        modify_plan_json: str,
        writing_context_json: str,
        media_assets_json: str = "",
    ) -> str:
        """Generate a WriterDocument patch set from a modification plan."""
        root = _temp_root()
        document_path = _write_input_artifact(
            root,
            "writer_document.json",
            _json_loads(writer_document_json, {}),
            self.WRITER_IR_SCHEMA,
        )
        plan_path = _write_input_artifact(
            root,
            "modify_plan.json",
            _json_loads(modify_plan_json, {}),
            writer_schema("revision.ModifyPlan"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        media_assets_path = ""
        if media_assets_json.strip():
            media_assets_path = _write_input_artifact(
                root,
                "media_assets.json",
                _json_loads(media_assets_json, {}),
                writer_schema("multimodal.MediaAssetLibrary"),
            )
        result = WriterRevisionTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).generate_patch_set(
            document=document_path,
            modify_plan=plan_path,
            context=context_path,
            media_assets=media_assets_path or None,
        )
        return _json_dumps(_primary_data(result))

    def generate_string_replace_set(
        self,
        markdown_document: str,
        modify_plan_json: str,
        writing_context_json: str,
    ) -> str:
        """Generate Markdown string replacements from a modification plan."""
        root = _temp_root()
        document_path = _write_document_input(root, "document", markdown_document)
        plan_path = _write_input_artifact(
            root,
            "modify_plan.json",
            _json_loads(modify_plan_json, {}),
            writer_schema("revision.ModifyPlan"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterRevisionTools(
            llm=AutoModel(model="llm"),
            artifact_store=str(root),
        ).generate_string_replace_set(
            document=document_path,
            modify_plan=plan_path,
            context=context_path,
        )
        return _json_dumps(_primary_data(result))

    def plan_revision(
        self,
        writing_task_json: str,
        writer_document_json: str,
        writing_context_json: str,
        media_assets_json: str = "",
    ) -> str:
        """Locate targets, build a modification plan, and generate a PatchSet."""
        located = self.locate_revision_target(
            writing_task_json=writing_task_json,
            writer_document_json=writer_document_json,
            writing_context_json=writing_context_json,
        )
        plan = self.generate_modify_plan(
            writing_task_json=writing_task_json,
            writer_document_json=writer_document_json,
            locate_result_json=located,
            writing_context_json=writing_context_json,
        )
        patch_set = self.generate_patch_set(
            writer_document_json=writer_document_json,
            modify_plan_json=plan,
            writing_context_json=writing_context_json,
            media_assets_json=media_assets_json,
        )
        return _json_dumps(
            {
                "locate_result": _json_loads(located, {}),
                "modify_plan": _json_loads(plan, {}),
                "patch_set": _json_loads(patch_set, {}),
            }
        )

    def apply_patch(
        self,
        writer_document_json: str,
        patch_set_json: str,
        writing_context_json: str,
        media_assets_json: str = "",
    ) -> str:
        """Apply a validated patch set and return the revised WriterDocument."""
        root = _temp_root()
        document_path = _write_input_artifact(
            root,
            "writer_document.json",
            _json_loads(writer_document_json, {}),
            self.WRITER_IR_SCHEMA,
        )
        patch_path = _write_input_artifact(
            root,
            "patch_set.json",
            _json_loads(patch_set_json, {}),
            writer_schema("revision.PatchSet"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        media_assets_path = ""
        if media_assets_json.strip():
            media_assets_path = _write_input_artifact(
                root,
                "media_assets.json",
                _json_loads(media_assets_json, {}),
                writer_schema("multimodal.MediaAssetLibrary"),
            )
        result = WriterRevisionTools(llm=None, artifact_store=str(root)).apply_patch(
            document=document_path,
            patch_set=patch_path,
            context=context_path,
            media_assets=media_assets_path or None,
        )
        artifact_paths = (result.get("metadata") or {}).get("artifact_paths") or {}
        revised_path = artifact_paths.get("revised_document", "")
        source = WriterDocument.model_validate(
            _json_loads(writer_document_json, {}),
        )
        revised = _set_document_editable(
            _read_artifact_data(revised_path) if revised_path else {},
            stage=source.stage,
        )
        return _json_dumps(
            {
                "patch_result": _primary_data(result),
                "revised_document": revised.model_dump(exclude_defaults=True),
            }
        )

    def apply_string_replace(
        self,
        markdown_document: str,
        string_replace_set_json: str,
        writing_context_json: str,
    ) -> str:
        """Apply replacements and return the revised Markdown document."""
        root = _temp_root()
        document_path = _write_document_input(root, "document", markdown_document)
        replace_path = _write_input_artifact(
            root,
            "string_replace_set.json",
            _json_loads(string_replace_set_json, {}),
            writer_schema("revision.StringReplaceSet"),
        )
        context_path = _write_input_artifact(
            root,
            "writing_context.json",
            _json_loads(writing_context_json, {}),
            writer_schema("context.WritingContext"),
        )
        result = WriterRevisionTools(
            llm=None, artifact_store=str(root)
        ).apply_string_replace(
            document=document_path,
            replace_set=replace_path,
            context=context_path,
        )
        artifact_paths = (result.get("metadata") or {}).get("artifact_paths") or {}
        revised_path = artifact_paths.get("revised_document_md", "")
        return _json_dumps(
            {
                "string_replace_result": _primary_data(result),
                "revised_document": _read_artifact_data(revised_path),
            }
        )

    def apply_revision(
        self,
        writer_document_json: str,
        patch_set_json: str,
        writing_context_json: str,
        sync_provider: bool = False,
        allow_outline: bool = True,
        media_assets_json: str = "",
    ) -> str:
        """Apply a local revision and optionally synchronize its bound provider."""
        source = WriterDocument.model_validate(
            _json_loads(writer_document_json, {}),
        )
        if source.stage == "outline" and not allow_outline:
            raise ToolExecutionError(
                "A full-document revision cannot use an outline-stage document.",
            )
        applied = _json_loads(
            self.apply_patch(
                writer_document_json=writer_document_json,
                patch_set_json=patch_set_json,
                writing_context_json=writing_context_json,
                media_assets_json=media_assets_json,
            ),
            {},
        )
        output = {
            "patch_result": applied.get("patch_result") or {},
            "revised_document": applied.get("revised_document") or {},
            "write_result": None,
        }
        if not sync_provider or _target_from_document(source) is None:
            return _json_dumps(output)

        from .toolkits import WriterResourceToolkit

        published = _json_loads(
            WriterResourceToolkit().publish_revision(
                source_document_json=writer_document_json,
                patch_set_json=patch_set_json,
                media_assets_json=media_assets_json,
            ),
            {},
        )
        output["revised_document"] = published.get("draft_document") or {}
        output["write_result"] = published.get("publish_result") or {}
        return _json_dumps(output)


from .resources import sync_writer_documents


__all__ = [
    "WriterRevisionCapabilities",
    "sync_writer_documents",
]
