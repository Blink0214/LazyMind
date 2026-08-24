"""Synthetic-trace tests for analyze_common (workspace-centric route / facts)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path("/Users/chensiyu2/Code/LazyMind")
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))

from analyze_common import (   # type: ignore
    TraceAnalysis,
    analyze,
    check_modify_types,
    load_trace,
)


class TraceAnalysisTests(unittest.TestCase):
    def test_load_trace_accepts_dict(self):
        trace = load_trace({"id": "t", "observations": []})
        self.assertEqual(trace["id"], "t")

    def test_load_trace_missing_file(self):
        with self.assertRaises(FileNotFoundError):
            load_trace(Path("/tmp/definitely_missing_trace.json"))


class NewFlowRouteTests(unittest.TestCase):
    """Workspace 封装后的路由/步骤/工具/provider 推断。"""

    def _trace(self, tools, draft_op="", prepare_source=False, provider_obs=()):
        obs = [
            {"name": name, "startTime": f"2026-08-17T00:00:{index:02d}Z",
             "level": "DEFAULT"}
            for index, name in enumerate(tools)
        ]
        if draft_op:
            obs.append({
                "name": "writer_draft_workspace",
                "metadata": {"attributes": {
                    "lazyllm.io.output": json.dumps({"operation": draft_op}),
                }},
                "startTime": "2026-08-17T00:00:59Z",
            })
        if prepare_source:
            obs.append({
                "name": "writer_prepare_workspace",
                "metadata": {"attributes": {
                    "lazyllm.io.output": '{"source_document": "/tmp/source.md"}',
                }},
                "startTime": "2026-08-17T00:00:58Z",
            })
        obs.extend(provider_obs)
        return {"id": "t", "observations": obs}

    def test_expand_from_draft_workspace_generate_with_source(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_outline_workspace",
             "writer_draft_workspace"],
            draft_op="generate", prepare_source=True)
        self.assertEqual(analyze(trace).route, "expand")

    def test_create_without_source(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_outline_workspace",
             "writer_draft_workspace"],
            draft_op="generate")
        self.assertEqual(analyze(trace).route, "create")

    def test_rewrite_from_draft_workspace_operation(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_draft_workspace"],
            draft_op="rewrite")
        self.assertEqual(analyze(trace).route, "rewrite")

    def test_revise_from_draft_workspace_operation(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_draft_workspace"],
            draft_op="revise")
        self.assertEqual(analyze(trace).route, "revise")

    def test_outline_workspace_fallback(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_outline_workspace"],
            prepare_source=True)
        self.assertEqual(analyze(trace).route, "expand")
        self.assertEqual(analyze(self._trace(["writer_prepare_workspace"])).route,
                         "unknown")

    def test_steps_from_advance_step_attempt_results(self):
        obs = [
            {"name": "advance_step",
             "startTime": f"2026-08-17T00:00:0{i}Z", "level": "DEFAULT",
             "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                 "attempt_results": [{"step_id": step}]})}}}
            for i, step in enumerate(("prepare", "outline", "write_document"))
        ]
        self.assertEqual(analyze({"id": "t", "observations": obs}).step_path,
                         ["prepare", "outline", "write_document"])

    def test_modify_types_check_reports_missing(self):
        analysis = TraceAnalysis(
            trace_id="t", session_id="", latency_s=0.0,
            route="revise", step_path=[], tool_sequence=[],
            slot_view={}, provider="local", write_back={},
            modify_types=["create", "move"])
        c = check_modify_types(analysis, ["create", "update", "delete", "move"])
        self.assertEqual(c.status, "FAIL")
        self.assertIn("update", c.detail)
        self.assertIn("delete", c.detail)
        self.assertEqual(check_modify_types(analysis, ["create", "move"]).status,
                         "PASS")

    def test_provider_detected_from_prepare_workspace(self):
        trace = self._trace(
            ["writer_prepare_workspace"],
            provider_obs=[{
                "name": "writer_prepare_workspace",
                "metadata": {"attributes": {
                    "lazyllm.io.input":
                        "请根据这份飞书大纲... https://sensetime.feishu.cn/wiki/abc",
                }},
            }])
        self.assertEqual(analyze(trace).provider, "feishu")

    def test_models_and_providers_extracted_from_llm_input(self):
        trace = {
            "id": "t",
            "observations": [
                {
                    "name": "llm",
                    "level": "DEFAULT",
                    "metadata": {"attributes": {"lazyllm.io.input": json.dumps({
                        "resolved_prompt": {
                            "model": "MiniMax-M2.7-highspeed",
                            "messages": [{"role": "system", "content":
                                "You are an intelligent assistant provided by Minimax. "
                                "You are a helpful assistant."}],
                        },
                    })}},
                },
                {"name": "image_generator", "level": "DEFAULT", "metadata": {}},
            ],
        }
        a = analyze(trace)
        self.assertEqual(a.models, ["MiniMax-M2.7-highspeed"])
        self.assertEqual(a.providers, ["Minimax"])
        self.assertEqual(a.llm_call_count, 1)
        self.assertEqual(a.image_call_count, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
