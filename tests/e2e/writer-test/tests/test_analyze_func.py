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
    CheckContext,
    check_content_contract,
    check_feishu_content_consistency,
    check_cross_references,
    check_feishu_materialization,
    check_feishu_revision,
    check_media,
    check_ir_span_consistency,
    check_numbering_correctness,
    check_reference_integrity,
    check_reference_number_consistency,
    check_sse_streaming_effect,
    collect_markdown_reference_targets,
    run_func_checks,
)


class FeishuMaterializationCheckTests(unittest.TestCase):
    def test_native_blocks_verify_heading_and_figure_numbering(self):
        xml = (
            '<h1 id="heading-1">1. 准备</h1>'
            '<p id="paragraph-1">见<a href="https://example.test/doc#heading-2">第2章</a></p>'
            '<img id="image-1" caption="图1 准备示意图" src="secret-media-token"/>'
            '<h1 id="heading-2">2. 避险</h1>'
            '<img id="image-2" caption="图2 避险示意图" src="secret-media-token-2"/>'
        )
        integrity, numbering, summary = check_feishu_materialization(xml)
        self.assertEqual(integrity.status, "PASS")
        self.assertEqual(numbering.status, "PASS")
        self.assertEqual(summary["visible_internal_links"]["count"], 1)
        self.assertNotIn("secret-media-token", str(summary))

    def test_native_blocks_report_missing_caption_and_dangling_link(self):
        xml = (
            '<h1 id="heading-1">1. 准备</h1>'
            '<p><a href="#missing-heading">缺失章节</a></p>'
            '<img id="image-1" caption=""/>'
        )
        integrity, numbering, summary = check_feishu_materialization(xml)
        self.assertEqual(integrity.status, "FAIL")
        self.assertEqual(numbering.status, "FAIL")
        self.assertEqual(summary["result"], "FAIL")

    def test_body_links_need_no_numbers_but_destinations_still_do(self):
        xml = ('<h2 id="s1">1. 准备</h2><h3 id="s11">1.1. 物资</h3>'
               '<h2 id="s2">2. 避险</h2>'
               '<img id="img1" caption="图1 避险示意"/>'
               '<p><a href="#s2">避险章节</a><a href="#img1">配图</a></p>')
        for body in (xml, xml.replace("避险章节", "第99章")):
            integrity, numbering, _ = check_feishu_materialization(body)
            self.assertEqual(integrity.status, "PASS")
            self.assertEqual(numbering.status, "PASS")
        integrity, _, _ = check_feishu_materialization(xml.replace('href="#s2"', 'href="#gone"'))
        self.assertEqual(integrity.status, "FAIL")
        for bad in (xml.replace("2. 避险", "避险"),
                    xml.replace("图1 避险示意", "避险示意"),
                    xml.replace("2. 避险", "3. 避险")):
            self.assertEqual(check_feishu_materialization(bad)[1].status, "FAIL")

    def test_markdown_plain_reference_label_keeps_numbered_target_contract(self):
        md = ('# 文章\n<a id="block-sec1"></a>\n## 1. 准备\n'
              '参见[避险章节](#block-sec2)\n'
              '<a id="block-sec2"></a>\n## 2. 避险\n')
        self.assertEqual(check_reference_integrity(md, require_numbering=True).status, "PASS")
        self.assertEqual(check_numbering_correctness(md, require=True).status, "PASS")
        self.assertEqual(check_reference_integrity(md.replace('](#block-sec2)', '](#block-missing)')).status, "FAIL")

    def test_provider_content_detects_dropped_ir_block_text(self):
        artifact = {"data": {"blocks": [{
            "node_id": "p1", "type": "paragraph",
            "content": "这是必须完整写入飞书的一整段正文",
            "spans": [{"text": "这是", "style": {}}],
        }]}}
        check, summary = check_feishu_content_consistency(
            '<p id="p1">这是</p>', artifact,
        )
        self.assertEqual(check.status, "FAIL")
        self.assertEqual(summary["missing_blocks"][0]["node_id"], "p1")

    def test_ir_span_consistency_detects_sparse_spans(self):
        artifact = {"data": {"blocks": [{
            "node_id": "p1", "type": "paragraph", "content": "完整正文",
            "spans": [{"text": "正文", "style": {"bold": True}}],
        }]}}
        self.assertEqual(check_ir_span_consistency(artifact).status, "FAIL")


class ContentContractTests(unittest.TestCase):
    def test_required_forbidden_and_ordered_text(self):
        final = "# 指南\n\n## 台风过后的注意事项\n应急物资准备\n\n## 台风期间的安全措施"
        check = check_content_contract(final, {
            "required_text": ["应急物资准备"],
            "forbidden_text": ["地下通道、地下车库及下沉式建筑"],
            "ordered_text": ["台风过后的注意事项", "台风期间的安全措施"],
        })
        self.assertEqual(check.status, "PASS")

    def test_heading_contract_ignores_body_mentions_and_checks_structure(self):
        rule = {
            "required_headings": ["应急物资准备"],
            "ordered_headings": ["台风过后的注意事项", "台风期间的安全措施"],
        }
        body_only = (
            "# 指南\n\n目录：应急物资准备、台风过后的注意事项、"
            "台风期间的安全措施\n\n"
            "## 台风期间的安全措施\n正文\n\n"
            "## 台风过后的注意事项\n正文\n"
        )
        failed = check_content_contract(body_only, rule)
        self.assertEqual(failed.status, "FAIL")
        self.assertIn("required heading missing", failed.detail)
        self.assertIn("heading order mismatch", failed.detail)

        markdown = (
            "# 指南\n\n## 1. 应急物资准备\n正文\n\n"
            "## 2. 台风过后的注意事项\n正文\n\n"
            "## 3. 台风期间的安全措施\n正文\n"
        )
        self.assertEqual(check_content_contract(markdown, rule).status, "PASS")

        ir = {"data": {"blocks": [
            {"type": "heading", "content": "1. 应急物资准备"},
            {"type": "heading", "content": "2. 台风过后的注意事项"},
            {"type": "heading", "content": "3. 台风期间的安全措施"},
        ]}}
        self.assertEqual(check_content_contract(ir, rule).status, "PASS")


class CrossReferenceCheckTests(unittest.TestCase):
    def test_markdown_link_count(self):
        md = ("见[第一节](#block-sec-001)与[第二节](#block-sec-002)。"
              '<a id="block-sec-001"></a>')
        self.assertEqual(check_cross_references(md, {"min_links": 2}).status, "PASS")
        bad = check_cross_references("没有任何链接", {"min_links": 1})
        self.assertEqual(bad.status, "FAIL")
        self.assertIn("markdown links=0", bad.detail)

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

    def test_aggregate_uses_materialized_export_for_integrity_and_numbering(self):
        canonical = '<a id="block-sec-1"></a>\n## 章节\n\n见[本章](#block-sec-1)。\n'
        exported = '<a id="block-sec-1"></a>\n## 1. 章节\n\n见[第1章](#block-sec-1)。\n'
        checks = run_func_checks(CheckContext(
            common_checks=[], streams_for_draft=None,
            final_artifact=canonical, materialized_artifact=exported,
            expected={
                "cross_ref": {"min_links": 1},
                "ref_integrity": True,
                "numbering": {"require": True},
                "ref_number_consistency": True,
            },
        ))
        selected = {
            check.name: check for check in checks
            if check.name.startswith("final.")
        }
        self.assertEqual(selected["final.cross_references"].status, "PASS")
        self.assertEqual(selected["final.reference_integrity"].status, "PASS")
        self.assertEqual(selected["final.numbering"].status, "PASS")
        self.assertEqual(
            selected["final.reference_number_consistency"].status, "PASS")


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
    def test_sse_contract_boundaries(self):
        from shared.observability import StreamRecord

        self.assertEqual(run_func_checks(CheckContext(
            common_checks=[], streams_for_draft=[], final_artifact="正文", expected={},
        )), [])
        self.assertEqual(check_sse_streaming_effect([]).status, "WARN")

        cases = [
            (["artifact_stream_start", "artifact_stream_delta",
              "artifact_stream_delta", "artifact_stream_end"], "PASS", None),
            (["artifact_stream_start", "artifact_stream_delta",
              "artifact_stream_end"], "FAIL", "deltas=1"),
        ]
        for events, status, detail in cases:
            with self.subTest(events=events):
                rec = StreamRecord(stream_id="s", events=events, text="正文",
                                   slots={"draft_document"},
                                   content_types={"text/markdown"})
                check = check_sse_streaming_effect([rec])
                self.assertEqual(check.status, status)
                if detail:
                    self.assertIn(detail, check.detail)

        incomplete = StreamRecord(
            stream_id="incomplete",
            events=["artifact_stream_start", "artifact_stream_delta"],
            text="未结束正文", slots={"draft_document"},
            content_types={"text/markdown"},
        )
        checks = run_func_checks(CheckContext(
            common_checks=[], streams_for_draft=[incomplete],
            final_artifact=incomplete.text, sse_text=incomplete.text,
            expected={"sse": {"required": True, "min_deltas": 1}},
        ))
        self.assertEqual(checks[1].name, "sse.protocol")
        self.assertEqual(checks[1].status, "FAIL")
        self.assertIn("end=0", checks[1].detail)

    def test_provider_revision_and_media_boundaries(self):
        self.assertEqual(check_feishu_revision("feishu", 10, 12, "increase").status, "PASS")
        self.assertEqual(check_feishu_revision("feishu", 12, 10, "increase").status, "FAIL")
        self.assertEqual(check_feishu_revision("local", 1, 2, "increase").status, "PASS")

        self.assertEqual(check_media("正文", None, 0).status, "PASS")
        markdown = "![图1](media/a.png) ![图2](media/b.png)"
        self.assertEqual(check_media(markdown, {"assets": {}}, 2).status, "PASS")
        self.assertEqual(
            check_media("![x](media-placeholder://need-1)", {"assets": {}}, 1).status,
            "FAIL",
        )
        ir = {"data": {"blocks": [{
            "node_id": "img-1", "type": "image", "content": "",
            "references": [{"type": "media_asset", "id": "missing"}],
        }]}}
        failed = check_media(ir, {"data": {"assets": {}}}, 1)
        self.assertEqual(failed.status, "FAIL")
        self.assertIn("unresolved media asset", failed.detail)


if __name__ == "__main__":
    unittest.main(verbosity=2)
