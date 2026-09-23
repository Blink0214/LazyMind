"""Unit tests for the func runner (dataclass / CLI / case loader)."""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, MagicMock, patch


REPO_ROOT = Path("/Users/chensiyu2/Code/LazyMind")
for p in ("tests/e2e", "tests/e2e/writer-test", "tests/e2e/writer-test/runners"):
    sys.path.insert(0, str(REPO_ROOT / p))

from func_run import (  # type: ignore
    _collect_provider_evidence, _correlate_workflow_session,
    _export_writer_document, _mechanical_verdict,
    _merge_session_route, _merge_session_slots,
    _merge_session_steps,
    _persist_execution, _safe_error_message,
    _run_api, _sse_from_bridge_payload, _wait_with_sse, run_case,
)
from shared.api import ChatResult
from shared.observability import (
    TaskCapture, WorkflowTaskCapture, _consume_event,
    collect_workflow_task_streams, select_complete_streams, task_capture_to_dict,
)


class FunctionalSafetyTests(unittest.TestCase):
    def test_current_core_sse_protocol_is_normalized(self):
        capture = TaskCapture(task_id="task")
        for event in (
            {
                "type": "artifact_stream_start", "stream_id": "draft-1",
                "slot": "draft_document", "content_type": "text/markdown",
            },
            {
                "type": "artifact_stream", "stream_id": "draft-1",
                "delta": "# Current protocol\n",
            },
            {"type": "artifact_stream_end", "stream_id": "draft-1"},
        ):
            _consume_event(capture, event)

        records = select_complete_streams(
            capture, slot="draft_document", content_type="text/markdown",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].text, "# Current protocol\n")
        _consume_event(capture, {"type": "artifact", "value": {"ok": True}})
        self.assertEqual(capture.artifacts, [{"ok": True}])

    def test_writer_command_overrides_trace_route(self):
        analysis = SimpleNamespace(route="create")
        with patch("func_run.load_slot", return_value=(
            {"data": {"action": "use_outline"}}, "application/json",
        )):
            _merge_session_route(
                analysis, {"slots": [{"slot_id": "writer_command"}]},
                "http://example.test", "token",
            )
        self.assertEqual(analysis.route, "expand")

    def test_session_slot_provider_is_authoritative(self):
        analysis = SimpleNamespace(provider="", slot_view={})
        _merge_session_slots(analysis, {"slots": [{
            "slot_id": "draft_document", "selected": True,
            "provider": "feishu", "revision": 1,
            "content_type": "text/markdown", "artifact_value": "# draft",
        }]}, "http://unused", "token")
        self.assertEqual(analysis.provider, "feishu")
        self.assertEqual(analysis.slot_view["draft_document"].provider, "feishu")

    def test_source_and_target_provider_come_from_artifact_identity(self):
        for provider in ("feishu", "notion"):
            with self.subTest(provider=provider):
                analysis = SimpleNamespace(provider="", slot_view={})
                artifacts = {
                    "source_document": {"data": {"stage": "outline",
                        "provider_binding": {"provider": provider}}},
                    "target_document": {"data": {"adapter": provider}},
                }
                slots = [{"slot_id": key, "selected": True, "revision": 1,
                          "artifact_value": value, "content_type": "application/json"}
                         for key, value in artifacts.items()]
                with patch("func_run.load_slot", side_effect=lambda _b, _t, _s, sid:
                           (artifacts[sid], "application/json")) as loader:
                    _merge_session_slots(analysis, {"slots": slots}, "http://unused", "token",
                                         {key: {"provider": provider} for key in artifacts})
                self.assertEqual(loader.call_count, 2)
                for key in artifacts:
                    self.assertEqual(analysis.slot_view[key].provider, provider)
                self.assertEqual(analysis.slot_view["source_document"].stage, "outline")

        # Do not invent Feishu identity from the filename, URI, or expected rule.
        analysis = SimpleNamespace(provider="", slot_view={})
        with patch("func_run.load_slot", return_value=({"data": {
                "uri": "https://example.feishu.cn/docx/test"}}, "application/json")):
            _merge_session_slots(analysis, {"slots": [{"slot_id": "target_document",
                "revision": 1, "provider": "feishu"}]}, "http://unused", "token",
                {"target_document": {"provider": "feishu"}})
        self.assertEqual(analysis.slot_view["target_document"].provider, "")

    def test_revision_set_checks_actual_schema_not_nonexistent_patch_type(self):
        from analyze_common import check_required_artifacts
        schema = "lazyllm.tools.writer.data_models.revision.PatchSet"
        analysis = SimpleNamespace(provider="", slot_view={})
        with patch("func_run.load_slot", return_value=({"schema": schema,
                "data": {"target_doc_id": "doc", "hunks": [{"modify_type": "move"}]}},
                "application/json")):
            _merge_session_slots(analysis, {"slots": [{"slot_id": "document_revision_set",
                "revision": 1}]}, "http://unused", "token",
                {"document_revision_set": {"schema": schema}})
        self.assertEqual(check_required_artifacts(analysis,
            {"document_revision_set": {"schema": schema}})[0].status, "PASS")
        self.assertEqual(check_required_artifacts(analysis,
            {"document_revision_set": {"schema": "wrong.Schema"}})[0].status, "FAIL")

    def test_observer_cannot_repair_missing_browser_stream_end(self):
        start = {"type": "artifact_stream_start", "stream_id": "s",
                 "slot": "draft_document", "content_type": "text/markdown"}
        delta = {"type": "artifact_stream", "stream_id": "s", "delta": "head"}
        browser = {"events": [start, delta], "diagnostics": {"drain_reason": "timeout"}}
        observer = {"events": [start, delta,
            {"type": "artifact_stream", "stream_id": "s", "delta": "tail"},
            {"type": "artifact_stream_end", "stream_id": "s"}]}
        capture, streams, text = _sse_from_bridge_payload({
            "sse_capture": browser, "sse_observer_capture": observer})
        self.assertEqual(text, "head")
        self.assertFalse(streams[0].is_complete)
        self.assertEqual(capture["diagnostics"]["drain_reason"], "timeout")
        self.assertEqual(capture["observer"]["streams"]["s"]["text"], "headtail")

    def test_bridge_sse_payload_keeps_complete_and_incomplete_drafts(self):
        base = [
            {"type": "artifact_stream_start", "stream_id": "stream",
             "slot": "draft_document", "content_type": "text/markdown"},
            {"type": "artifact_stream", "stream_id": "stream", "delta": "draft"},
        ]
        for terminal, complete in [([], False),
                                   ([{"type": "artifact_stream_end", "stream_id": "stream"}], True)]:
            with self.subTest(complete=complete):
                events = base + terminal
                capture, streams, text = _sse_from_bridge_payload({
                    "sse_capture": {"task_id": "task",
                                    "total_artifact_events": len(events),
                                    "events": events},
                })
                self.assertEqual(capture["total_artifact_events"], len(events))
                self.assertEqual(len(streams), 1)
                self.assertEqual(streams[0].is_complete, complete)
                self.assertEqual(text, "draft")

    def test_functional_evidence_is_consolidated_without_markdown_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td)
            final_path = output_dir / "final.md"
            final_path.write_text("# final", encoding="utf-8")
            evidence = SimpleNamespace(
                output_dir=output_dir,
                final_artifact="# final",
                final_artifact_text="# final",
                final_md_path=final_path,
                materialized_artifact=None,
                render_evidence=None,
                feishu_materialization_summary=None,
                media_assets=None,
                materialization_error=None,
                case=SimpleNamespace(assertions={"route": "create"}),
                trace_error=None,
                trace_error_spans=[],
                session_payload={"steps": []},
                trace_json=None,
                chat_info={
                    "retry_events": [], "finish_reason": "STOP", "approvals": 0,
                },
                sse_capture={"task_id": "task", "streams": []},
                state={
                    "conversation_id": "conversation", "session_id": "session",
                    "task_id": "task", "writer_status": "completed",
                },
                started_at=1.0,
                finished_at=2.0,
                trace_id=None,
                trace_path=output_dir / "trace.json",
                use_ui=True,
                feishu_before=None,
                feishu_after=None,
            )
            outcome = SimpleNamespace(
                checks=[{"name": "route", "status": "PASS", "detail": "ok"}],
                facts={"route": "create"}, models=[], providers=[],
                llm_call_count=0, image_call_count=0,
            )
            _persist_execution(evidence, outcome)

            self.assertTrue((output_dir / "evidence.json").is_file())
            self.assertFalse((output_dir / "checks.json").exists())
            self.assertFalse((output_dir / "evidence").exists())
            self.assertFalse((output_dir / "draft_document.md").exists())
            persisted = json.loads((output_dir / "evidence.json").read_text())
            self.assertEqual(persisted["checks"], outcome.checks)
            self.assertEqual(persisted["sse"]["task_id"], "task")

    def test_workflow_session_must_match_captured_conversation(self):
        state = {"session_id": "session-ui"}
        _correlate_workflow_session(state, {"session_id": "session-ui"})
        with self.assertRaisesRegex(RuntimeError, "UI workflow session id missing"):
            _correlate_workflow_session({}, {"session_id": "session-core"})
        with self.assertRaisesRegex(RuntimeError, "Core workflow session id missing"):
            _correlate_workflow_session(state, {})
        with self.assertRaisesRegex(RuntimeError, "session mismatch"):
            _correlate_workflow_session(
                state, {"session_id": "session-from-another-conversation"},
            )

    def test_mechanical_verdict_is_separate_and_fail_closed(self):
        self.assertEqual(_mechanical_verdict(
            "completed", [{"status": "PASS"}], retry_count=0,
        ), "PASS")
        self.assertEqual(_mechanical_verdict(
            "completed", [{"status": "PASS"}], retry_count=1,
        ), "PASS_WITH_RETRY")
        self.assertEqual(_mechanical_verdict(
            "completed", [{"status": "WARN"}], retry_count=0,
        ), "INCONCLUSIVE")
        self.assertEqual(_mechanical_verdict(
            "completed", [{"status": "FAIL"}], retry_count=0,
        ), "FAIL")
        self.assertEqual(_mechanical_verdict(
            "failed", [{"status": "PASS"}], retry_count=0,
        ), "FAIL")

    def test_error_message_redacts_credentials(self):
        safe = _safe_error_message(ValueError(
            "Bearer abc.def URL?token=secret&x=1&signature=signed",
        ))
        self.assertNotIn("abc.def", safe)
        self.assertNotIn("secret", safe)
        self.assertNotIn("signed", safe)

    def test_session_steps_fill_missing_trace_outputs(self):
        analysis = SimpleNamespace(step_path=[])
        _merge_session_steps(analysis, {"steps": [
            {"step_id": "prepare", "status": "succeeded", "attempt": 1},
            {"step_id": "outline", "status": "succeeded", "attempt": 1},
            {"step_id": "write_document", "status": "succeeded", "attempt": 1},
        ]})
        self.assertEqual(
            analysis.step_path, ["prepare", "outline", "write_document"],
        )

    def test_list_slot_item_count_uses_distinct_session_indices(self):
        analysis = SimpleNamespace(slot_view={})
        slots = [
            {
                "slot_id": "draft_blocks", "list_index": index,
                "revision": 1, "selected": True, "content_type": "file",
                "document": {"representation": "markdown"},
                "artifact_value": {"filename": f"draft_section_{index + 1:04d}.md"},
            }
            for index in range(3)
        ]
        with patch("func_run.load_slot") as load_slot:
            _merge_session_slots(
                analysis, {"slots": slots}, "http://unused", "token",
                required_rules={"draft_blocks": {"min_items": 1}},
            )
        record = analysis.slot_view["draft_blocks"]
        self.assertEqual(record.extension, ".md")
        self.assertEqual(record.properties["item_count"], 3)
        self.assertEqual(
            record.properties["item_count_source"], "session.list_index",
        )
        load_slot.assert_not_called()

    def test_slot_representation_prefers_document_descriptor(self):
        analysis = SimpleNamespace(slot_view={})
        slot = {
            "slot_id": "draft_document", "revision": 1, "selected": True,
            "content_type": "json",
            "document": {
                "representation": "ir",
                "schema": "application/vnd.lazymind.writer+json",
            },
            "artifact_value": {"filename": "misleading.md"},
        }
        with patch(
            "func_run.load_slot",
            return_value=({"data": {"blocks": []}}, "application/json"),
        ):
            _merge_session_slots(
                analysis, {"slots": [slot]}, "http://unused", "token",
            )
        self.assertEqual(analysis.slot_view["draft_document"].extension, ".lmd")
        self.assertIsNone(analysis.slot_view["draft_document"].stage)

    def test_sse_evidence_does_not_persist_provider_payloads(self):
        capture = TaskCapture(
            task_id="task-1",
            tool_calls=[{"url": "https://example.test?a=1&signature=secret"}],
            results=[{"token": "secret"}],
            artifacts=[{"authorization": "Bearer secret"}],
            terminal={
                "type": "workflow.completed",
                "status": "completed",
                "payload": {"token": "secret"},
            },
        )
        persisted = task_capture_to_dict(capture, task_ids=["task-1"])
        encoded = json.dumps(persisted)
        self.assertNotIn("secret", encoded)
        self.assertEqual(persisted["tool_call_count"], 1)
        self.assertEqual(persisted["result_count"], 1)
        self.assertEqual(persisted["artifact_count"], 1)
        self.assertEqual(persisted["terminal"], {
            "type": "workflow.completed", "status": "completed",
        })


class FuncCaseContractTests(unittest.TestCase):
    def test_representative_scenario_contracts(self):
        from shared.case_loader import load_writer_e2e_scenario
        c01 = load_writer_e2e_scenario("C01")
        c02 = load_writer_e2e_scenario("C02")
        c03 = load_writer_e2e_scenario("C03")
        c06 = load_writer_e2e_scenario("C06")
        c07 = load_writer_e2e_scenario("C07")
        x02 = load_writer_e2e_scenario("X02")
        x03 = load_writer_e2e_scenario("X03")

        self.assertEqual(c01.assertions["route"], "create")
        self.assertTrue(c01.assertions["sse"]["required"])
        self.assertTrue(c02.attachment_path.is_file())
        self.assertNotIn("${outline}", c03.prompt_text)
        self.assertEqual(c03.assertions["write_back"]["mode"], "replace")
        self.assertEqual(
            c06.assertions["modify_types"],
            ["create", "move"],
        )
        self.assertEqual(
            c06.assertions["content"]["required_headings"], ["应急物资准备"],
        )
        self.assertEqual(
            c06.assertions["content"]["ordered_headings"],
            ["台风来临前的准备工作", "应急物资准备", "台风过后的注意事项", "台风期间的安全措施"],
        )
        self.assertEqual(
            c07.assertions["content"]["required_headings"], ["应急物资准备"],
        )
        self.assertEqual(
            c07.assertions["content"]["ordered_headings"],
            ["台风来临前的准备工作", "应急物资准备", "台风过后的注意事项", "台风期间的安全措施"],
        )
        self.assertTrue(x02.assertions["provider_materialization"])
        for case in (c06, c07):
            self.assertEqual(case.assertions["modify_types"], ["create", "move"])
            self.assertNotIn("forbidden_text", case.assertions["content"])
            self.assertEqual(len(case.assertions["content"]["required_text"]), 2)
        self.assertEqual(x02.assertions["image_calls"], 0)
        self.assertIs(x03.assertions["ref_number_consistency"], True)
        self.assertIs(x03.assertions["cross_ref"]["preserve_from_source"], True)


class FunctionalPreflightRegressionTests(unittest.TestCase):
    def test_markdown_export_uses_rendered_export_document(self):
        response = MagicMock()
        response.read.return_value = json.dumps({"data": {
                "representation": "markdown",
                "document": "## Section",
                "export_document": "## 1. Section",
                "numbering": {"entries": {"sec": {"label": "1."}}},
            }}).encode()
        response.headers = {"Content-Type": "application/json"}
        response.__enter__.return_value = response
        with patch("func_run.urllib.request.urlopen", return_value=response):
            exported, rendered = _export_writer_document(
                "http://example.test", "token", "session",
            )
        self.assertEqual(exported, "## 1. Section")
        self.assertEqual(rendered["document"], "## Section")

    def test_ir_export_uses_writer_download_conversion(self):
        render_response = MagicMock()
        render_response.read.return_value = json.dumps({"data": {
            "representation": "ir",
            "document": {
                "document_id": "doc-1", "title": "Document", "blocks": [],
            },
            "numbering": {"entries": {}},
        }}).encode()
        render_response.headers = {"Content-Type": "application/json"}
        render_response.__enter__.return_value = render_response
        conversion_response = MagicMock()
        conversion_response.read.return_value = b"# Document\n\n## 1. Section\n"
        conversion_response.headers = {"Content-Type": "text/markdown"}
        conversion_response.__enter__.return_value = conversion_response
        with patch(
            "func_run.urllib.request.urlopen",
            side_effect=[render_response, conversion_response],
        ) as urlopen:
            exported, rendered = _export_writer_document(
                "http://example.test", "token", "session",
            )
        self.assertEqual(exported, "# Document\n\n## 1. Section\n")
        self.assertEqual(rendered["representation"], "ir")
        self.assertEqual(urlopen.call_count, 2)

    def test_feishu_reset_failure_aborts_before_ui_request(self):
        case = SimpleNamespace(
            scenario="C03",
            has_attachment=False,
            has_feishu_reference=True,
            extras={
                "feishu_reference": "https://example.invalid/wiki/doc",
                "feishu_history_version_id": "1",
                "feishu_baseline_revision_id": 2,
            },
        )
        session = SimpleNamespace(token="token", user_id="user")
        with tempfile.TemporaryDirectory() as td, \
                patch("shared.execution.Session.login", return_value=session), \
                patch("shared.preflight.reset_feishu_document", side_effect=ValueError("reset failed")), \
                patch("func_run._run_ui_bridge") as ui_bridge:
            with self.assertRaisesRegex(RuntimeError, "aborting before"):
                run_case(case, Path(td))
        ui_bridge.assert_not_called()

    def test_no_ui_starts_sse_before_completion_wait_returns(self):
        running = {"status": "active", "session_id": "session-1"}
        terminal = {"status": "completed", "session_id": "session-1"}
        capture = TaskCapture(task_id="task-1")
        collector_started = threading.Event()

        def fake_collect_workflow(*args, **kwargs):
            collector_started.set()
            return WorkflowTaskCapture(
                task_ids=["task-1"], capture=capture, stream_connected=True,
            )

        def fake_wait(*args, **kwargs):
            kwargs["on_session"](running)
            self.assertTrue(collector_started.wait(1))
            return SimpleNamespace(session=terminal)

        with patch(
                "func_run.collect_workflow_task_streams",
                side_effect=fake_collect_workflow,
        ) as collect, \
                patch("func_run.wait_for_writer_completion", side_effect=fake_wait):
            result = _wait_with_sse(
                "http://example.test", "token", "user-1", "conversation-1", 30,
            )

        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(result["session"], terminal)
        self.assertEqual(result["sse_capture_dict"]["task_ids"], ["task-1"])
        collect.assert_called_once_with(
            "http://example.test", "token", "user-1", "session-1",
            timeout_s=30, stop_event=ANY,
        )

    def test_workflow_event_stream_replays_completed_and_captures_live_tasks(self):
        lines = [
            b"event: snapshot\n",
            b'data: {"attempt_history":{"prepare":[{"task_id":"done","status":"succeeded"}],"write":[{"task_id":"draft","status":"running"}]}}\n',
            b"\n",
            b"event: attempt.patch\n",
            b'data: {"entity_id":"failed-attempt","payload":{"status":"failed"}}\n',
            b"\n",
            b"event: attempt.patch\n",
            b'data: {"entity_id":"retry","payload":{"status":"queued"}}\n',
            b"\n",
            b"event: workflow.completed\n",
            b'data: {"payload":{"status":"completed"}}\n',
            b"\n",
        ]
        response = MagicMock()
        response.__enter__.return_value = lines
        response.__exit__.return_value = False

        def captured(_base, _token, task_id, _timeout):
            return TaskCapture(task_id=task_id)

        with patch(
                "shared.observability.urllib.request.urlopen",
                return_value=response,
        ) as urlopen, patch(
                "shared.observability._collect_best_effort",
                side_effect=captured,
        ):
            result = collect_workflow_task_streams(
                "http://example.test", "token", "user-1", "session-1",
                timeout_s=30,
            )

        self.assertEqual(result.task_ids, ["done", "draft", "retry"])
        self.assertTrue(result.stream_connected)
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-user-id"), "user-1")

    def test_no_ui_recipe_is_forwarded_to_send_chat(self):
        case = SimpleNamespace(prompt_text="hello")
        wait_result = {
            "session": {"session_id": "s", "status": "completed"},
            "task_id": "t", "approvals": 0, "sse_capture_dict": None,
            "sse_streams_for_draft": [], "sse_text": "",
        }
        with patch("func_run.send_case", return_value=ChatResult(
                conversation_id="c", elapsed_s=1, finish_reason="STOP",
        )) as send, patch("func_run._wait_with_sse", return_value=wait_result), \
                patch("func_run._fetch_final_text", return_value="done"):
            _run_api(
                case, "http://example.test", "token", "user", [], 30,
                "explicit_mention",
            )
        self.assertEqual(send.call_args.kwargs["recipe"], "explicit_mention")
        self.assertIs(send.call_args.args[0], case)

    def test_provider_evidence_builds_each_terminal_check_once(self):
        case = SimpleNamespace(
            prompt_text="请处理 https://sensetime.feishu.cn/wiki/doc",
            assertions={
                "write_back": {"provider_revision": "increase"},
                "provider_materialization": True,
                "numbering": {"require": True},
                "cross_ref": {"min_links": 1, "min_section_links": 1},
            },
        )
        artifact = {"data": {"blocks": [
            {"node_id": "heading-1", "type": "heading", "content": "准备"},
            {"node_id": "heading-2", "type": "heading", "content": "避险"},
        ]}}
        document = SimpleNamespace(
            revision_id=12,
            content=(
                '<h1 id="heading-1">1. 准备</h1>'
                '<p>见<a href="#heading-2">第2章</a></p>'
                '<h1 id="heading-2">2. 避险</h1>'
            ),
        )
        with patch("func_run.fetch_feishu_document", return_value=document):
            revision, summary, checks, error = _collect_provider_evidence(
                case, artifact, 0, 30,
            )
        names = [check.name for check in checks]
        self.assertEqual(revision, 12)
        self.assertIsNone(error)
        self.assertIsNotNone(summary)
        self.assertEqual(len(names), len(set(names)))
        self.assertIn("final.provider_content", names)
        self.assertIn("final.cross_references", names)

if __name__ == "__main__":
    unittest.main(verbosity=2)
