"""Focused contract tests for trace-owned Writer facts."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))

from analyze_common import (  # type: ignore
    SlotRecord,
    TraceAnalysis,
    analyze,
    check_image_calls,
    check_modify_types,
    check_required_artifacts,
    check_workspace_facts,
    load_trace,
)


class TraceAnalysisTests(unittest.TestCase):
    def test_trace_loading_and_artifact_rules(self):
        self.assertEqual(load_trace({"id": "t", "observations": []})["id"], "t")
        with self.assertRaises(FileNotFoundError):
            load_trace(Path("/tmp/definitely_missing_trace.json"))

        analysis = TraceAnalysis(
            trace_id="t", session_id="", latency_s=0.0, route="create",
            step_path=[], tool_sequence=[], provider="feishu", write_back={},
            slot_view={"result": SlotRecord(
                slot="result", extension=".json", revisions=[1], provider="local",
                properties={"success": False, "item_count": 0},
            )},
        )
        for rule, fragment in [
            ({"success": True, "min_items": 1}, "success=False"),
            ({"provider": "feishu"}, "provider"),
        ]:
            with self.subTest(rule=rule):
                check = check_required_artifacts(analysis, {"result": rule})[0]
                self.assertEqual(check.status, "FAIL")
                self.assertIn(fragment, check.detail)

        self.assertEqual(check_image_calls(analysis, 0).status, "PASS")
        analysis.image_call_count = 1
        self.assertEqual(check_image_calls(analysis, 0).status, "FAIL")

    def test_write_back_workspace_and_direct_modes(self):
        cases = [
            ({
                "name": "writer_draft_workspace",
                "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                    "operation": "rewrite",
                    "saved_artifact_keys": ["draft_document", "document_write_result"],
                })}},
            }, "writer_draft_workspace.output"),
            ({
                "name": "writer_write_document",
                "metadata": {"attributes": {"lazyllm.io.input": json.dumps({
                    "kwargs": {"mode": "replace"},
                })}},
            }, "direct_span"),
        ]
        for observation, source in cases:
            with self.subTest(source=source):
                analysis = analyze({"id": "t", "observations": [observation]})
                self.assertEqual(analysis.write_back, {"writer_write_document": 1})
                self.assertEqual(analysis.write_back_modes, {"replace": 1})
                self.assertIn(source, analysis.write_back_evidence[0]["source"])

    def test_workspace_facts_use_only_successful_outputs(self):
        observations = [
            {
                "name": "writer_prepare_workspace", "level": "DEFAULT",
                "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                    "structure_mode": "sectioned", "representation": "markdown",
                    "control": {"next_step": "outline"},
                })}},
            },
            {
                "name": "writer_outline_workspace", "level": "DEFAULT",
                "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                    "operation": "generate", "control": {"next_step": "write_document"},
                })}},
            },
            {
                "name": "writer_draft_workspace", "level": "DEFAULT",
                "metadata": {"attributes": {"lazyllm.io.output": json.dumps({
                    "operation": "generate", "representation": "markdown",
                    "control": {"next_step": "__end__"},
                })}},
            },
        ]
        analysis = analyze({"id": "t", "observations": observations})
        expected = {
            "prepare": {"structure_mode": "sectioned", "representation": "markdown",
                        "next_step": "outline"},
            "outline": {"operation": "generate", "next_step": "write_document"},
            "write_document": {"operation": "generate", "representation": "markdown",
                               "next_step": "__end__"},
        }
        self.assertTrue(all(c.status == "PASS" for c in check_workspace_facts(analysis, expected)))

        observations[-1]["level"] = "ERROR"
        self.assertNotIn("write_document", analyze({"id": "t", "observations": observations}).workspace_facts)

    def test_modify_model_and_provider_facts(self):
        analysis = TraceAnalysis(
            trace_id="t", session_id="", latency_s=0.0, route="revise",
            step_path=[], tool_sequence=[], slot_view={}, provider="local",
            write_back={}, modify_types=["create", "move"],
        )
        missing = check_modify_types(analysis, ["create", "update", "delete", "move"])
        self.assertEqual(missing.status, "FAIL")
        self.assertIn("update", missing.detail)
        self.assertEqual(check_modify_types(analysis, ["create", "move"]).status, "PASS")

        trace = {"id": "t", "observations": [
            {"name": "llm", "level": "DEFAULT", "metadata": {"attributes": {
                "lazyllm.io.input": json.dumps({"resolved_prompt": {
                    "model": "MiniMax-M2.7-highspeed",
                    "messages": [{"role": "system", "content":
                                  "You are an intelligent assistant provided by Minimax."}],
                }}),
            }}},
            {"name": "image_generator", "level": "DEFAULT", "metadata": {}},
        ]}
        facts = analyze(trace)
        self.assertEqual(facts.models, ["MiniMax-M2.7-highspeed"])
        self.assertEqual(facts.providers, ["Minimax"])
        self.assertEqual((facts.llm_call_count, facts.image_call_count), (1, 1))


if __name__ == "__main__":
    unittest.main(verbosity=2)
