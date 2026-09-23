"""Focused contracts for performance case loading and runner guards."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[4]
for p in ("tests/e2e", "tests/e2e/writer-test", "tests/e2e/writer-test/runners"):
    sys.path.insert(0, str(REPO_ROOT / p))

from perf_run import _build_arg_parser, _compute_document_stats, _exit_code, run_case  # type: ignore


class PerfRunTests(unittest.TestCase):
    def test_completed_workflow_without_metrics_is_not_a_successful_perf_run(self):
        result = SimpleNamespace(writer_status="completed", final_artifact={"path": "final.md"},
                                 final_artifact_error=None, trace_error=None,
                                 trace_path="trace.json", analysis={"stats": {}})
        self.assertEqual(_exit_code(result, fetch_trace_after=True, compute_stats=True), 0)
        result.analysis = None
        self.assertEqual(_exit_code(result, fetch_trace_after=True, compute_stats=True), 1)
        self.assertEqual(_exit_code(result, fetch_trace_after=True, compute_stats=False), 0)
        result.trace_error = "missing parent chain"
        self.assertEqual(_exit_code(result, fetch_trace_after=True, compute_stats=False), 1)
        self.assertEqual(_exit_code(result, fetch_trace_after=False, compute_stats=False), 0)
        result.final_artifact_error = "missing final document"
        self.assertEqual(_exit_code(result, fetch_trace_after=False, compute_stats=False), 1)

    def test_provider_reset_failure_aborts_before_chat(self):
        case = SimpleNamespace(
            scenario="P03", has_attachment=False, has_feishu_reference=True,
            extras={"feishu_reference": "https://example.invalid/wiki/doc",
                    "feishu_history_version_id": "1", "feishu_baseline_revision_id": 2},
        )
        with tempfile.TemporaryDirectory() as td, \
                patch("shared.execution.Session.login", return_value=SimpleNamespace(token="t", user_id="u")), \
                patch("shared.preflight.reset_feishu_document", side_effect=ValueError("reset failed")), \
                patch("perf_run._send") as send:
            with self.assertRaisesRegex(RuntimeError, "aborting before"):
                run_case(case, Path(td))
        send.assert_not_called()

    def test_cli_and_case_contracts(self):
        ns = _build_arg_parser().parse_args([
            "--scenario", "P01", "--case", "1", "--output-dir", "/tmp/y",
        ])
        self.assertEqual((ns.scenario, ns.case, ns.no_trace, ns.no_stats),
                         ("P01", 1, False, False))

        from shared.case_loader import load_perf_case
        expectations = {
            "P01": lambda c: not c.has_attachment,
            "P02": lambda c: c.has_attachment and c.attachment_path.is_file(),
            "P03": lambda c: c.has_feishu_reference and "${P3_FEISHU_OUTLINE_1}" not in c.prompt_text,
            "P04": lambda c: c.has_attachment and c.extras["constraints"] == "revise",
        }
        for scenario, contract in expectations.items():
            with self.subTest(scenario=scenario):
                case = load_perf_case(scenario, 1)
                self.assertTrue(contract(case))
                self.assertNotIn("最多重试 3 次", case.prompt_text)
                self.assertNotIn("writer_prepare_workspace", case.prompt_text)

    def test_loaders_do_not_append_execution_constraints(self):
        import yaml
        from shared.case_loader import load_perf_case, load_writer_e2e_scenario
        root = REPO_ROOT / "tests/e2e/writer-test/cases"
        perf = yaml.safe_load((root / "writer_perf_cases.yaml").read_text())
        func = yaml.safe_load((root / "writer_func_cases.yaml").read_text())
        with patch("shared.case_loader.inject_exec_constraints", side_effect=AssertionError("must not inject")):
            for scenario in perf["scenarios"]:
                for case in scenario["cases"]:
                    loaded = load_perf_case(scenario["id"], case["id"])
                    if "${" not in case["text"]:
                        self.assertEqual(loaded.prompt_text, case["text"])
            for scenario in func["scenarios"]:
                if "${" not in scenario["request"]["text"]:
                    loaded = load_writer_e2e_scenario(scenario["id"])
                    self.assertEqual(loaded.prompt_text, scenario["request"]["text"])

    def test_unknown_constraint_and_inline_markdown(self):
        from shared.api import load_slot
        from shared.case_loader import inject_exec_constraints
        with self.assertRaises(KeyError):
            inject_exec_constraints("hello", "not_a_template")

        session = {"slots": [{
            "slot_id": "draft_document", "selected": True, "revision": 1,
            "artifact_value": {"schema": "text/markdown", "data": "# 标题\n\n正文"},
        }]}
        data, ctype = load_slot("http://unused", "token", session, "draft_document")
        self.assertEqual((ctype, data), ("text/markdown", "# 标题\n\n正文"))

    def test_document_stats_and_final_slot_selection(self):
        case = SimpleNamespace(extras={}, has_attachment=False, has_feishu_reference=False)
        session = {"slots": [
            {"slot_id": "draft_blocks", "list_index": index, "selected": True}
            for index in (0, 1, 1, 2)
        ]}
        with tempfile.TemporaryDirectory() as td:
            final_path = Path(td) / "final.md"
            final_path.write_text("# 标题\n\n正文", encoding="utf-8")
            stats = _compute_document_stats(case, final_path, Path(td), session)
        self.assertEqual((stats["draft_sections"], stats["draft_sections_source"]),
                         (3, "session.list_index"))

        from shared.document_metrics import selected_final_slots
        slots = selected_final_slots({"slots": [{
            "slot_id": "draft_document", "slot": "opaque", "selected": True,
        }]})
        self.assertEqual(len(slots), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
