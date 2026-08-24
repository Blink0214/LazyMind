"""Unit tests for the func runner (dataclass / CLI / case loader)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path("/Users/chensiyu2/Code/LazyMind")
for p in ("tests/e2e", "tests/e2e/writer-test", "tests/e2e/writer-test/runners"):
    sys.path.insert(0, str(REPO_ROOT / p))

from func_run import FuncResult, _parser  # type: ignore


class FuncResultDataclassTests(unittest.TestCase):
    def _result(self):
        return FuncResult(
            conversation_id="cid-1", session_id="sid-1", task_id="",
            started_at=1.0, finished_at=2.0, elapsed_s=1.0,
            finish_reason="FINISH_REASON_STOP", writer_status="completed",
            approvals=0, retry_events=[], retry_count=0,
            models=[], providers=[], llm_call_count=0, image_call_count=0,
            final_artifact_path=None, final_artifact_text=None,
            trace_id=None, trace_path=None, sse_capture=None, checks=[],
        )

    def test_round_trip_json(self):
        d = json.loads(json.dumps(self._result().__dict__, ensure_ascii=False))
        self.assertEqual(d["conversation_id"], "cid-1")
        self.assertEqual(d["writer_status"], "completed")

    def test_report_skeleton_has_machine_and_llm_sections(self):
        from func_run import _write_report
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "report.md"
            _write_report(p, self._result())
            text = p.read_text(encoding="utf-8")
            self.assertIn("## 机械检查（脚本比对）", text)
            self.assertIn("## 异常与说明（LLM 分析）", text)
            self.assertIn("## 最终结论（LLM 判定）", text)
            self.assertIn("models:", text)


class FuncRunCliTests(unittest.TestCase):
    def test_minimum_args_parse(self):
        ns = _parser().parse_args([
            "--cases-root", "/tmp/x", "--scenario", "C01", "--case", "1",
            "--output-dir", "/tmp/y"])
        self.assertEqual(ns.scenario, "C01")
        self.assertEqual(ns.output_dir, "/tmp/y")
        self.assertFalse(ns.no_ui)

    def test_recipe_choice(self):
        ns = _parser().parse_args([
            "--cases-root", "/tmp/x", "--scenario", "C01", "--case", "1",
            "--output-dir", "/tmp/y", "--recipe", "explicit_mention"])
        self.assertEqual(ns.recipe, "explicit_mention")
        with self.assertRaises(SystemExit):
            _parser().parse_args([
                "--cases-root", "/tmp/x", "--scenario", "C01", "--case", "1",
                "--output-dir", "/tmp/y", "--recipe", "junk"])

    def test_feishu_revision_flag(self):
        ns = _parser().parse_args([
            "--cases-root", "/tmp/x", "--scenario", "C01", "--case", "1",
            "--output-dir", "/tmp/y", "--feishu-revision-before", "5"])
        self.assertEqual(ns.feishu_revision_before, 5)

    def test_writer_e2e_scenario_loader(self):
        from shared.case_loader import load_writer_e2e_scenario
        case = load_writer_e2e_scenario("C01")
        self.assertEqual(case.scenario, "C01")
        self.assertIn("台风天", case.prompt_text)
        self.assertEqual(case.assertions["route"], "create")
        self.assertEqual(case.assertions["steps"],
                         ["prepare", "outline", "write_document"])
        self.assertIn("writer_prepare_workspace", case.assertions["tools_required"])
        self.assertIn("writer_draft_workspace", case.assertions["tools_required"])
        self.assertEqual(case.assertions["write_back"]["calls"], 0)
        self.assertIs(case.assertions.get("new_revision"), True)

    def test_feishu_placeholder_resolved_from_registry(self):
        from shared.case_loader import load_writer_e2e_scenario
        case = load_writer_e2e_scenario("C03")
        self.assertIn("FgOfw3gh8ivcajk8FPyc086Knvg", case.prompt_text)
        self.assertEqual(
            case.extras["feishu_reference"],
            "https://sensetime.feishu.cn/wiki/FgOfw3gh8ivcajk8FPyc086Knvg")
        self.assertEqual(case.extras["feishu_history_version_id"], "32768")
        self.assertEqual(case.extras["feishu_baseline_revision_id"], 78)
        self.assertNotIn("${outline}", case.prompt_text)
        self.assertEqual(case.extras["feishu_doc"], "outline")

    def test_func_markdown_fixture_uses_new_path(self):
        from shared.case_loader import load_writer_e2e_scenario
        case = load_writer_e2e_scenario("C02")
        self.assertIsNotNone(case.attachment_path)
        self.assertTrue(case.attachment_path.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
