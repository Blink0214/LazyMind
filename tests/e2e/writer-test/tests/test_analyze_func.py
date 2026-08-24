"""Unit tests for analyze_func mechanical checks."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path("/Users/chensiyu2/Code/LazyMind")
sys.path.insert(0, str(REPO_ROOT / "tests/e2e"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test"))
sys.path.insert(0, str(REPO_ROOT / "tests/e2e/writer-test/analyzers"))

from analyze_func import (   # type: ignore
    check_cross_references,
    check_feishu_revision,
    check_media,
    check_numbering_correctness,
    check_reference_integrity,
    check_reference_number_consistency,
    check_sse_streaming_effect,
    collect_markdown_reference_targets,
)


class CrossReferenceCheckTests(unittest.TestCase):
    def test_markdown_link_count(self):
        md = ("见[第一节](#block-sec-001)与[第二节](#block-sec-002)。"
              '<a id="block-sec-001"></a>')
        self.assertEqual(check_cross_references(md, {"min_links": 2}).status, "PASS")
        bad = check_cross_references("没有任何链接", {"min_links": 1})
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("markdown links=0", bad.detail)

    def test_ir_internal_ref_count(self):
        doc = {
            "document_id": "d", "stage": "draft", "title": "t",
            "blocks": [
                {"node_id": "b1", "type": "paragraph", "spans": [
                    {"text": "见", "style": {}},
                    {"text": "图1", "style": {
                        "link": {"type": "internal_ref",
                                 "target_node_id": "visual-sec-001-1"}}},
                ]},
                {"node_id": "b2", "type": "paragraph", "spans": []},
            ],
        }
        ok = check_cross_references(doc, {"min_links": 1})
        self.assertEqual(ok.status, "PASS")
        self.assertIn("ir links=1", ok.detail)

    def test_preserve_from_source(self):
        source = "见[第一节](#block-sec-001)和[第二节](#block-sec-002)。"
        final_ok = "修改后仍见[第一节](#block-sec-001)和[第二节](#block-sec-002)。"
        final_dropped = "修改后只保留[第一节](#block-sec-001)。"
        self.assertEqual(
            check_cross_references(
                final_ok, {"min_links": 2, "preserve_from_source": True}, source,
            ).status,
            "PASS")
        bad = check_cross_references(
            final_dropped, {"min_links": 2, "preserve_from_source": True}, source)
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("block-sec-002", bad.detail)


class ReferenceIntegrityTests(unittest.TestCase):
    def test_ir_all_references_resolve(self):
        doc = {
            "document_id": "d", "stage": "final", "title": "t",
            "blocks": [
                {"node_id": "sec-001", "type": "heading",
                 "numbering": {"level": 1}, "spans": []},
                {"node_id": "visual-sec-001-1", "type": "image",
                 "numbering": {"level": 1},
                 "references": [{"type": "media_asset", "id": "a1"}],
                 "spans": []},
                {"node_id": "p-1", "type": "paragraph", "spans": [
                    {"text": "见", "style": {}},
                    {"text": "图", "style": {"link": {
                        "type": "internal_ref",
                        "target_node_id": "visual-sec-001-1"}}},
                ]},
            ],
        }
        ok = check_reference_integrity(doc, require_numbering=True)
        self.assertEqual(ok.status, "PASS")
        self.assertIn("ir targets=2 refs=1", ok.detail)

    def test_ir_dangling_reference_fails(self):
        doc = {
            "document_id": "d", "stage": "final", "title": "t",
            "blocks": [
                {"node_id": "sec-001", "type": "heading",
                 "numbering": {"level": 1}, "spans": []},
                {"node_id": "p-1", "type": "paragraph", "spans": [
                    {"text": "x", "style": {"link": {
                        "type": "internal_ref", "target_node_id": "missing-node"}}},
                ]},
            ],
        }
        bad = check_reference_integrity(doc)
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("missing-node", bad.detail)

    def test_markdown_anchor_coverage(self):
        good = ('<a id="block-sec-001"></a>\n## 1 准备\n\n'
                "见[准备](#block-sec-001)。\n")
        self.assertEqual(check_reference_integrity(good).status, "PASS")
        missing_anchor = "## 1 准备\n\n[准备](#block-sec-001)。\n"
        self.assertEqual(check_reference_integrity(missing_anchor).status, "FAIL")


class NumberingCorrectnessTests(unittest.TestCase):
    def test_ir_numbering_sequence(self):
        doc = {
            "document_id": "d", "stage": "final", "title": "t",
            "blocks": [
                {"node_id": "s1", "type": "heading",
                 "numbering": {"level": 1}, "content": "1. 准备",
                 "spans": [{"text": "1. 准备", "style": {}}]},
                {"node_id": "img1", "type": "image",
                 "numbering": {}, "content": "图1 配图", "spans": []},
                {"node_id": "s2", "type": "heading",
                 "numbering": {"level": 1}, "content": "2. 措施",
                 "spans": [{"text": "2. 措施", "style": {}}]},
                {"node_id": "t1", "type": "table",
                 "numbering": {}, "content": "表1 清单", "spans": []},
            ],
        }
        self.assertEqual(check_numbering_correctness(doc, require=True).status, "PASS")
        bad = dict(doc)
        bad["blocks"][1]["content"] = "图2 配图"
        failed = check_numbering_correctness(bad, require=True)
        self.assertEqual(failed.status, "FAIL")
        self.assertIn("figure", failed.detail)

    def test_markdown_numbering_sequence(self):
        good = "## 1 准备\n\n## 2 措施\n\n图1：配图\n表1：清单\n"
        self.assertEqual(check_numbering_correctness(good, require=True).status, "PASS")
        moved = "## 1 准备\n\n## 3 过境后\n\n## 2 措施\n"
        bad = check_numbering_correctness(moved, require=True)
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("(3,) != expected (2,)", bad.detail)

    def test_number_first_caption_before_table(self):
        """新物化格式：数字在前、题注在表格前，仍应计入题注/编号。"""
        md = (
            '<a id="block-tbl-001"></a>\n'
            "1 应急物资清单\n"
            "| 物品 | 数量 |\n"
            "|---|---|\n"
            "| 水 | 2 瓶 |\n"
        )
        parsed = collect_markdown_reference_targets(md)
        self.assertEqual(parsed["elements"]["table"]["total"], 1)
        self.assertEqual(parsed["elements"]["table"]["captioned"], 1)
        self.assertEqual(parsed["elements"]["table"]["numbered"], 1)
        self.assertTrue(parsed["targets"]["block-tbl-001"]["has_numbering"])
        self.assertEqual(check_reference_integrity(md).status, "PASS")

    def test_number_first_code_caption_before_fence(self):
        """新物化格式：代码块题注为单独数字行（围栏前），应计入编号。"""
        md = "1\n```python\nprint(1)\n```\n"
        parsed = collect_markdown_reference_targets(md)
        self.assertEqual(parsed["elements"]["code"]["total"], 1)
        self.assertEqual(parsed["elements"]["code"]["numbered"], 1)

    def test_old_prefix_caption_after_table_still_works(self):
        """旧格式：表 N 题注在表格后，仍应计入。"""
        md = (
            "| 物品 | 数量 |\n"
            "|---|---|\n"
            "| 水 | 2 瓶 |\n"
            "表 1：应急物资清单\n"
        )
        parsed = collect_markdown_reference_targets(md)
        self.assertEqual(parsed["elements"]["table"]["numbered"], 1)


class ReferenceNumberConsistencyTests(unittest.TestCase):
    def test_markdown_link_text_matches_target_number(self):
        good = ('<a id="block-sec-001"></a>\n## 1 准备\n\n'
                "见[第1节](#block-sec-001)。\n")
        self.assertEqual(check_reference_number_consistency(good).status, "PASS")
        stale = ('<a id="block-sec-001"></a>\n## 1 准备\n\n'
                 "见[第2节](#block-sec-001)。\n")
        bad = check_reference_number_consistency(stale)
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("link text number 2 != target block-sec-001 current 1",
                      bad.detail)


class BaseCheckSmokeTests(unittest.TestCase):
    def test_sse_streaming_effect_passes_with_deltas(self):
        from shared.observability import StreamRecord

        rec = StreamRecord(
            stream_id="s1",
            events=["artifact_stream_start", "artifact_stream_delta",
                    "artifact_stream_delta", "artifact_stream_end"],
            text="第一段第二段",
            slots={"draft_document"},
            content_types={"text/markdown"},
        )
        chk = check_sse_streaming_effect([rec])
        self.assertEqual(chk.status, "PASS")
        self.assertIn("deltas=2", chk.detail)

    def test_sse_streaming_effect_fails_on_single_delta(self):
        from shared.observability import StreamRecord

        rec = StreamRecord(
            stream_id="s1",
            events=["artifact_stream_start", "artifact_stream_delta",
                    "artifact_stream_end"],
            text="全文一次性到达",
            slots={"draft_document"},
            content_types={"text/markdown"},
        )
        chk = check_sse_streaming_effect([rec])
        self.assertEqual(chk.status, "FAIL")
        self.assertIn("deltas=1", chk.detail)

    def test_sse_streaming_effect_warns_without_capture(self):
        self.assertEqual(check_sse_streaming_effect([]).status, "WARN")

    def test_feishu_revision_increase(self):
        ok = check_feishu_revision("feishu", 10, 12, rule="increase")
        self.assertEqual(ok.status, "PASS")
        self.assertEqual(
            check_feishu_revision("feishu", 12, 10, rule="increase").status, "FAIL")
        self.assertEqual(
            check_feishu_revision("local", 1, 2, rule="increase").status, "PASS")

    def test_media_no_rule_passes(self):
        self.assertEqual(check_media("正文", None, 0).status, "PASS")

    def test_media_body_images_pass_without_generated_asset(self):
        """放松来源后：正文有配图即通过，不再强制生成图片资产。"""
        md = "![图1](media/a.png) ![图2](media/b.png)"
        chk = check_media(md, {"assets": {}}, 2)
        self.assertEqual(chk.status, "PASS")
        self.assertIn("未检测到生成图片资产", chk.detail)
        placeholder = check_media("![x](media-placeholder://need-1)", {"assets": {}}, 1)
        self.assertEqual(placeholder.status, "FAIL")


if __name__ == "__main__":
    unittest.main(verbosity=2)
