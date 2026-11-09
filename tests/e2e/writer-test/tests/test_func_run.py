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


REPO_ROOT = Path(__file__).resolve().parents[4]
for p in ("tests/e2e", "tests/e2e/writer-test", "tests/e2e/writer-test/runners"):
    sys.path.insert(0, str(REPO_ROOT / p))

from func_run import (  # type: ignore
    _collect_provider_evidence, _correlate_workflow_session,
    _export_writer_document,
    _merge_session_slots, _merge_session_steps,
    _persist_execution, _run_result_payload, _safe_error_message, FuncResult,
    _run_api, _sse_from_bridge_payload, _wait_with_sse, run_case,
)
from shared.api import ChatResult
from shared.observability import (
    TaskCapture, WorkflowTaskCapture, collect_workflow_task_streams,
    task_capture_to_dict,
)


class FunctionalSafetyTests(unittest.TestCase):
    def test_bridge_sse_payload_reaches_draft_checks(self):
        capture, streams, text = _sse_from_bridge_payload({
            "sse_capture": {
                "task_id": "task",
                "total_artifact_events": 3,
                "events": [
                    {"type": "artifact_stream_start", "stream_id": "stream", "slot": "draft_document", "content_type": "text/markdown"},
                    {"type": "artifact_stream", "stream_id": "stream", "delta": "draft"},
                    {"type": "artifact_stream_end", "stream_id": "stream"},
                ],
            },
        })
        self.assertEqual(capture["total_artifact_events"], 3)
        self.assertEqual(len(streams), 1)
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
                case=SimpleNamespace(assertions={"route": "create"}, extras={}),
                source_artifact=None,
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

    def test_run_envelope_omits_evidence_duplicated_in_separate_files(self):
        result = FuncResult(
            conversation_id="conversation", session_id="session", task_id="task",
            started_at=1, finished_at=2, elapsed_s=1, finish_reason="STOP",
            writer_status="completed", mechanical_verdict="PASS", approvals=0,
            final_artifact_path="final.md", final_artifact_text="full body",
            trace_id="trace", trace_path="trace.json",
            sse_capture={"streams": []}, checks=[{"status": "PASS"}],
            retry_events=[{"type": "step_retried"}],
            evidence_path="evidence.json", report_path="report.md",
        )
        payload = _run_result_payload(result)
        self.assertEqual(payload["evidence_path"], "evidence.json")
        for duplicate in (
            "final_artifact_text", "sse_capture", "checks", "retry_events",
        ):
            self.assertNotIn(duplicate, payload)

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
            )
        record = analysis.slot_view["draft_blocks"]
        self.assertEqual(record.extension, ".md")
        self.assertEqual(record.properties["item_count"], 3)
        self.assertEqual(
            record.properties["item_count_source"], "session.list_index",
        )
        load_slot.assert_called_once()

    def test_ir_list_slot_counts_sections_not_blocks_in_one_section(self):
        analysis = SimpleNamespace(slot_view={})
        slots = [
            {
                "slot_id": "draft_blocks", "list_index": index,
                "revision": 1, "selected": True, "content_type": "file",
                "document": {"representation": "ir"},
                "artifact_value": {"filename": f"draft_block_{index + 1:04d}.lmd"},
            }
            for index in range(2)
        ]
        with patch(
            "func_run.load_slot",
            return_value=({"data": {"blocks": [{}, {}, {}]}}, "application/json"),
        ):
            _merge_session_slots(
                analysis, {"slots": slots}, "http://unused", "token",
            )
        record = analysis.slot_view["draft_blocks"]
        self.assertEqual(record.extension, ".lmd")
        self.assertEqual(record.properties["item_count"], 2)
        self.assertEqual(
            record.properties["item_count_source"], "session.list_index",
        )

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
            extras={"feishu_reference": "https://sensetime.feishu.cn/wiki/doc"},
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
        self.assertEqual(names, [])
        self.assertEqual(summary["content"], document.content)

if __name__ == "__main__":
    unittest.main(verbosity=2)
