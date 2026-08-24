"""Unit tests for the perf runner (CLI / case loading)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path("/Users/chensiyu2/Code/LazyMind")
for p in ("tests/e2e", "tests/e2e/writer-test", "tests/e2e/writer-test/runners"):
    sys.path.insert(0, str(REPO_ROOT / p))

from perf_run import _build_arg_parser  # type: ignore


class PerfRunCliTests(unittest.TestCase):
    def test_required_args(self):
        ns = _build_arg_parser().parse_args([
            "--cases-root", "/tmp/x", "--scenario", "P01", "--case", "1",
            "--output-dir", "/tmp/y"])
        self.assertEqual(ns.scenario, "P01")
        self.assertEqual(ns.case, 1)
        self.assertFalse(ns.no_trace)
        self.assertFalse(ns.no_stats)

    def test_load_perf_case_p01(self):
        from shared.case_loader import load_perf_case
        case = load_perf_case("P01", 1)
        self.assertIn("AI Writer", case.prompt_text)
        self.assertIsNone(case.attachment_path)
        # 统一执行约束在测试开始时注入，且口径跟随新业务流程。
        self.assertIn("执行约束", case.prompt_text)
        self.assertIn("writer_prepare_workspace", case.prompt_text)
        self.assertIn("最多重试 3 次", case.prompt_text)
        self.assertNotIn("load→build_revision_task", case.prompt_text)

    def test_load_perf_case_p03_resolves_feishu(self):
        from shared.case_loader import load_perf_case
        case = load_perf_case("P03", 1)
        self.assertNotIn("${P3_FEISHU_OUTLINE_1}", case.prompt_text)
        self.assertIn("feishu.cn", case.prompt_text)
        self.assertTrue(case.has_feishu_reference)

    def test_load_perf_case_p02_has_attachment(self):
        from shared.case_loader import load_perf_case
        case = load_perf_case("P02", 1)
        self.assertTrue(case.has_attachment)
        self.assertTrue(case.attachment_path.is_file())

    def test_load_perf_case_p04_injects_revise_constraint(self):
        from shared.case_loader import load_perf_case
        case = load_perf_case("P04", 1)
        self.assertIn("writer_prepare_workspace 与 writer_draft_workspace",
                      case.prompt_text)
        self.assertIn("最多重试 3 次", case.prompt_text)
        self.assertNotIn("严格走 load", case.prompt_text)

    def test_load_exec_constraints_unknown_key_raises(self):
        from shared.case_loader import inject_exec_constraints
        with self.assertRaises(KeyError):
            inject_exec_constraints("hello", "not_a_template")

    def test_load_slot_inline_markdown_artifact(self):
        """新后端内联 Markdown 产物（schema=text/markdown + data）应按文本返回。"""
        from shared.api import load_slot
        session = {"slots": [{
            "slot_id": "draft_document",
            "artifact_value": {
                "schema": "text/markdown",
                "data": "# 标题\n\n正文内容",
                "meta": {"title": "标题"},
            },
            "selected": True,
            "revision": 1,
        }]}
        data, ctype = load_slot("http://unused", "token", session, "draft_document")
        self.assertEqual(ctype, "text/markdown")
        self.assertIn("# 标题", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
