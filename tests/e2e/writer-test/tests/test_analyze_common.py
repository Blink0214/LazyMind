"""Synthetic-trace tests for analyze_common (workspace-centric route / facts)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))

from analyze_common import (   # type: ignore
    SlotRecord,
    TraceAnalysis,
    analyze,
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
        self.assertEqual(analyze(trace).route, "unknown")

    def test_create_without_source(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_outline_workspace",
             "writer_draft_workspace"],
            draft_op="generate")
        self.assertEqual(analyze(trace).route, "unknown")

    def test_rewrite_from_draft_workspace_operation(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_draft_workspace"],
            draft_op="rewrite")
        self.assertEqual(analyze(trace).route, "unknown")

    def test_workspace_write_back_infers_concrete_function_and_replace_mode(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_draft_workspace"],
            draft_op="rewrite")
        for obs in trace["observations"]:
            if obs["name"] == "writer_draft_workspace" and obs.get("metadata"):
                obs["metadata"]["attributes"]["lazyllm.io.output"] = json.dumps({
                    "operation": "rewrite",
                    "saved_artifact_keys": ["draft_document", "document_write_result"],
                })
        analysis = analyze(trace)
        self.assertEqual(analysis.write_back, {"writer_write_document": 1})
        self.assertEqual(analysis.write_back_modes, {"replace": 1})
        self.assertIn("writer_draft_workspace.output", analysis.write_back_evidence[0]["source"])

    def test_direct_write_document_span_reads_mode(self):
        trace = self._trace(["writer_write_document"])
        trace["observations"][0]["metadata"] = {"attributes": {
            "lazyllm.io.input": json.dumps({"kwargs": {"mode": "replace"}}),
        }}
        analysis = analyze(trace)
        self.assertEqual(analysis.write_back, {"writer_write_document": 1})
        self.assertEqual(analysis.write_back_modes, {"replace": 1})
        self.assertEqual(analysis.write_back_evidence[0]["source"], "direct_span")

    def test_revise_from_draft_workspace_operation(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_draft_workspace"],
            draft_op="revise")
        self.assertEqual(analyze(trace).route, "unknown")

    def test_outline_workspace_fallback(self):
        trace = self._trace(
            ["writer_prepare_workspace", "writer_outline_workspace"],
            prepare_source=True)
        self.assertEqual(analyze(trace).route, "unknown")
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
                         [])


    def test_workspace_facts_ignore_failed_attempt(self):
        trace = {"id": "t", "observations": [{
            "name": "writer_draft_workspace",
            "startTime": "2026-08-17T00:00:01Z",
            "level": "ERROR",
            "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                "operation": "rewrite", "control": {"next_step": "__end__"},
            })}},
        }]}
        self.assertEqual(analyze(trace).workspace_facts, {})


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
        self.assertEqual(analyze(trace).provider, "")

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
