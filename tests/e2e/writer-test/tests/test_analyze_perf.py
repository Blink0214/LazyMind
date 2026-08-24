"""Unit tests for the perf analyzer (workspace-adapted phase stats)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path("/Users/chensiyu2/Code/LazyMind")
sys.path.insert(0, str(REPO_ROOT / "tests/e2e"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))

from analyze_perf import avg_traces, extract, sheet_rows  # type: ignore


def _advance(step_id, ts):
    return {
        "id": f"adv-{step_id}", "name": "advance_step",
        "startTime": ts, "endTime": ts, "level": "DEFAULT",
        "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
            "attempt_results": [{"step_id": step_id}]})}},
    }


def _llm(ts, model="MiniMax-M2.7-highspeed"):
    return {
        "id": f"llm-{ts}", "name": "llm", "type": "GENERATION",
        "startTime": ts, "endTime": ts, "level": "DEFAULT",
        "metadata": {"attributes": {
            "lazyllm.entity.config.suppliers": "[('minimax',)]",
            "lazyllm.io.input": json.dumps({
                "resolved_prompt": {"model": model, "messages": [{
                    "role": "system",
                    "content": "You are an intelligent assistant provided by Minimax.",
                }]}}),
        }},
        "usageDetails": {"input": 10, "output": 5},
    }


class PerfExtractTests(unittest.TestCase):
    def test_advance_step_boundaries_from_metadata(self):
        trace = {
            "id": "t", "latency": 60.0,
            "observations": [
                _advance("prepare", "2026-08-18T10:00:00Z"),
                _llm("2026-08-18T10:00:05Z"),
                _advance("outline", "2026-08-18T10:00:20Z"),
                _llm("2026-08-18T10:00:25Z"),
                _advance("write_document", "2026-08-18T10:00:40Z"),
                _llm("2026-08-18T10:00:45Z"),
                {
                    "id": "ws", "name": "writer_draft_workspace",
                    "startTime": "2026-08-18T10:00:45Z",
                    "endTime": "2026-08-18T10:00:55Z", "level": "DEFAULT",
                    "metadata": {"attributes": {
                        "lazyllm.io.output": json.dumps({
                            "operation": "generate", "draft_section_count": 4})}},
                },
            ],
        }
        out = extract(trace)
        self.assertEqual(out["attribution"], "advance_step")
        self.assertIn("prepare", out["phases"])
        self.assertIn("write_document", out["phases"])
        self.assertEqual(out["phases"]["write_document"]["chapters"], 4)
        self.assertIn("MiniMax-M2.7-highspeed", out["model_info"]["request_model"])
        self.assertIn("minimax", out["model_info"]["suppliers"])

    def test_fallback_attribution_without_advance_step(self):
        trace = {
            "id": "t", "latency": 10.0,
            "observations": [
                {"id": "p", "name": "writer_prepare_workspace",
                 "startTime": "2026-08-18T10:00:00Z",
                 "endTime": "2026-08-18T10:00:03Z", "level": "DEFAULT"},
                {"id": "d", "name": "writer_draft_workspace",
                 "startTime": "2026-08-18T10:00:05Z",
                 "endTime": "2026-08-18T10:00:09Z", "level": "DEFAULT",
                 "metadata": {"attributes": {
                     "lazyllm.io.output": json.dumps({"draft_section_count": 2})}}},
            ],
        }
        out = extract(trace)
        self.assertIn(out["attribution"], ("function_fallback", "missing"))
        self.assertIn("write_document", out["phases"])

    def test_avg_traces_scenario(self):
        stats = avg_traces([{
            "id": "t", "latency": 10.0, "observations": [
                _llm("2026-08-18T10:00:05Z"),
                {"id": "d", "name": "writer_draft_workspace",
                 "startTime": "2026-08-18T10:00:06Z",
                 "endTime": "2026-08-18T10:00:08Z", "level": "DEFAULT",
                 "metadata": {"attributes": {
                     "lazyllm.io.output": json.dumps({"draft_section_count": 3})}}},
            ],
        }], scenario="P01")
        self.assertEqual(stats["scenario"], "P01")
        self.assertEqual(stats["n_traces"], 1)

    def test_three_bucket_full_abnormal_normal(self):
        """三桶：full=完整过程（含异常），abnormal=非最后一次尝试，normal=最后一次成功尝试。"""
        def ws(oid, start, end, level="DEFAULT"):
            return {
                "id": oid, "name": "writer_draft_workspace", "type": "SPAN",
                "startTime": start, "endTime": end, "level": level,
                "metadata": {"attributes": {}},
            }

        def llm(oid, ts, parent):
            item = _llm(ts)
            item["id"] = oid
            item["parentObservationId"] = parent
            return item

        trace = {
            "id": "t", "latency": 120.0,
            "observations": [
                _advance("prepare", "2026-08-18T10:00:00Z"),
                _advance("write_document", "2026-08-18T10:00:10Z"),
                ws("ws-1", "2026-08-18T10:00:10Z", "2026-08-18T10:00:40Z"),
                llm("llm-1", "2026-08-18T10:00:15Z", "ws-1"),
                ws("ws-2", "2026-08-18T10:00:45Z", "2026-08-18T10:01:20Z"),
                llm("llm-2", "2026-08-18T10:00:50Z", "ws-2"),
            ],
        }
        out = extract(trace)
        wd = out["phases"]["write_document"]
        self.assertEqual(wd["llm_count"], 2)                 # full 含全部尝试
        self.assertEqual(wd["abnormal"]["llm_count"], 1)     # 首次尝试 → 异常
        self.assertEqual(wd["normal"]["llm_count"], 1)       # 最后一次尝试 → 正常
        self.assertEqual(wd["normal_attempt"], 2)
        self.assertIs(wd["normal_succeeded"], True)
        stats = avg_traces([trace], scenario="P05")
        wd_case = next(
            item for item in stats["abnormal_cases"]
            if item["phase"] == "write_document"
        )
        self.assertEqual(wd_case["attempts"], 2)
        self.assertIs(wd_case["normal_succeeded"], True)

    def test_last_attempt_failure_marks_case_failed(self):
        def ws(oid, start, end, level="DEFAULT"):
            return {
                "id": oid, "name": "writer_draft_workspace", "type": "SPAN",
                "startTime": start, "endTime": end, "level": level,
                "metadata": {"attributes": {}},
            }

        def llm(oid, ts, parent):
            item = _llm(ts)
            item["id"] = oid
            item["parentObservationId"] = parent
            return item

        trace = {
            "id": "t", "latency": 60.0,
            "observations": [
                _advance("prepare", "2026-08-18T10:00:00Z"),
                _advance("write_document", "2026-08-18T10:00:10Z"),
                ws("ws-1", "2026-08-18T10:00:10Z", "2026-08-18T10:00:30Z"),
                llm("llm-1", "2026-08-18T10:00:15Z", "ws-1"),
                ws("ws-2", "2026-08-18T10:00:35Z", "2026-08-18T10:00:50Z", level="ERROR"),
                llm("llm-2", "2026-08-18T10:00:40Z", "ws-2"),
            ],
        }
        out = extract(trace)
        wd = out["phases"]["write_document"]
        self.assertEqual(wd["normal_attempt"], 2)
        self.assertIs(wd["normal_succeeded"], False)
        self.assertIsNone(wd["normal"])                      # 末次失败 → 无正常链路
        self.assertEqual(wd["abnormal"]["llm_count"], 2)     # 全部尝试归异常
        self.assertEqual(wd["llm_count"], 2)                 # full 不变

    def test_doc_stats_aligned_to_gen_rev_chars(self):
        """document_stats 与数据表“生成/修改文章字数”列对齐。"""
        trace = {
            "id": "t", "latency": 60.0,
            "observations": [
                _advance("prepare", "2026-08-18T10:00:00Z"),
                _advance("write_document", "2026-08-18T10:00:10Z"),
                {
                    "id": "ws", "name": "writer_draft_workspace",
                    "startTime": "2026-08-18T10:00:10Z",
                    "endTime": "2026-08-18T10:00:20Z", "level": "DEFAULT",
                    "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                        "operation": "revise", "draft_section_count": None})}},
                },
            ],
        }
        doc = {
            "final": {"visible_characters": 6348, "markdown_characters": 7000},
            "revision": {"present": True, "changed_final_characters": 184},
        }
        stats = avg_traces([trace], scenario="P05", doc_stats=[doc])
        wd = stats["phases"]["write_document"]
        self.assertEqual(wd["gen_chars"], 6348)
        self.assertEqual(wd["rev_chars"], 184)

    def test_sheet_rows_uses_last_successful_attempt(self):
        """导出行取 normal（最后一次成功尝试）；末次失败标 failed 不导出。"""
        def ws(oid, start, end, level="DEFAULT"):
            return {
                "id": oid, "name": "writer_draft_workspace", "type": "SPAN",
                "startTime": start, "endTime": end, "level": level,
                "metadata": {"attributes": {}},
            }

        def llm(oid, ts, parent):
            item = _llm(ts)
            item["id"] = oid
            item["parentObservationId"] = parent
            return item

        ok_trace = {
            "id": "t", "latency": 60.0,
            "observations": [
                _advance("prepare", "2026-08-18T10:00:00Z"),
                _advance("write_document", "2026-08-18T10:00:10Z"),
                ws("ws-1", "2026-08-18T10:00:10Z", "2026-08-18T10:00:30Z"),
                llm("llm-1", "2026-08-18T10:00:15Z", "ws-1"),
                ws("ws-2", "2026-08-18T10:00:35Z", "2026-08-18T10:00:50Z"),
                llm("llm-2", "2026-08-18T10:00:40Z", "ws-2"),
            ],
        }
        rows, failed = sheet_rows(avg_traces([ok_trace], scenario="P05"), "PT0002")
        wd = next(r for r in rows if r[1] == "P05" and r[2] == "write_document")
        self.assertEqual(wd[0], "PT0002")
        self.assertEqual(wd[3], 15.0)          # 最后一次尝试（ws-2）墙钟
        self.assertFalse(failed)

        fail_trace = {
            "id": "t", "latency": 60.0,
            "observations": [
                _advance("prepare", "2026-08-18T10:00:00Z"),
                _advance("write_document", "2026-08-18T10:00:10Z"),
                ws("ws-1", "2026-08-18T10:00:10Z", "2026-08-18T10:00:30Z"),
                llm("llm-1", "2026-08-18T10:00:15Z", "ws-1"),
                ws("ws-2", "2026-08-18T10:00:35Z", "2026-08-18T10:00:50Z", level="ERROR"),
                llm("llm-2", "2026-08-18T10:00:40Z", "ws-2"),
            ],
        }
        rows2, failed2 = sheet_rows(avg_traces([fail_trace], scenario="P04"), "PT0002")
        self.assertEqual(failed2, [["P04", "write_document"]])
        # 任一阶段末次失败 → 行保留，指标列标 “—”。
        self.assertTrue(rows2)
        self.assertTrue(all(r[3] == "—" for r in rows2))
        self.assertIn("write_document", [r[2] for r in rows2])

    def test_full_flow_row_keeps_routing_excludes_retry(self):
        """全流程行：保留触发前/路由段，剔除重试与异常（clean_full）。"""
        def ws(oid, name, start, end, level="DEFAULT"):
            return {
                "id": oid, "name": name, "type": "SPAN",
                "startTime": start, "endTime": end, "level": level,
                "metadata": {"attributes": {}},
            }

        def llm(oid, ts, parent=None):
            item = _llm(ts)
            item["id"] = oid
            if parent:
                item["parentObservationId"] = parent
            return item

        trace = {
            "id": "t", "latency": 120.0,
            "observations": [
                llm("routing-1", "2026-08-18T10:00:00Z"),          # 触发前路由
                llm("routing-2", "2026-08-18T10:00:05Z"),
                _advance("prepare", "2026-08-18T10:00:10Z"),
                ws("prep-fail", "writer_prepare_workspace",
                   "2026-08-18T10:00:10Z", "2026-08-18T10:00:15Z", level="ERROR"),
                ws("prep-ok", "writer_prepare_workspace",
                   "2026-08-18T10:00:20Z", "2026-08-18T10:00:30Z"),
                llm("prep-llm", "2026-08-18T10:00:25Z", "prep-ok"),
                _advance("write_document", "2026-08-18T10:00:35Z"),
                ws("wd-ok", "writer_draft_workspace",
                   "2026-08-18T10:00:35Z", "2026-08-18T10:00:50Z"),
                llm("wd-llm", "2026-08-18T10:00:40Z", "wd-ok"),
            ],
        }
        stats = avg_traces([trace], scenario="P05")
        # 触发前 5s + prepare 正常 10s + write_document 正常 15s = 30s
        self.assertAlmostEqual(stats["clean_full"]["wall"], 30.0, places=1)
        self.assertAlmostEqual(stats["clean_full"]["count"], 4.0, places=1)
        self.assertEqual(stats["clean_full"]["input"], 40)
        self.assertEqual(stats["clean_full"]["output"], 20)
        # 重试与异常被剔除：clean_full 小于含异常的 trace 全链路
        self.assertLess(stats["clean_full"]["wall"], stats["full_link"]["wall"])

        rows, failed = sheet_rows(stats, "PT0002")
        self.assertFalse(failed)
        ff = next(r for r in rows if r[2] == "全流程")
        self.assertAlmostEqual(ff[3], 30.0, places=1)
        self.assertAlmostEqual(ff[4], 4.0, places=1)
        self.assertEqual(ff[8], 40)
        self.assertEqual(ff[10], 20)


if __name__ == "__main__":
    unittest.main(verbosity=2)
