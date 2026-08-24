#!/usr/bin/env python3
"""从归一化 Writer trace 生成三阶段（prepare/outline/write_document）性能统计与 Markdown 报告。

统计维度对齐《写作性能优化》文档：每个阶段合并统计
阶段总耗时、LLM 推理轮数与累计耗时、Agent 工具调用次数、工具实际执行时间、
LLM 累计输入/输出（token 与原始载荷字符）；write_document 额外统计
draft 章节数与生成/修改文章字数。

阶段归属优先使用 trace 中 advance_step 的 step_id（prepare/outline/write_document）
作为时间窗口边界；旧 trace 缺少 advance_step 时按 Writer 函数名回退。
每次 LLM/工具 span 只归入一个阶段，保证阶段之间互斥。

本模块是 ``writer-benchmark/scripts/compute_stats.py`` 在统一测试栈中的移植
（writer-test 自包含，不依赖旧目录）；文档度量复用 ``shared/document_metrics.py``。
"""

import argparse
import json
import math
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT / "tests/e2e") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "tests/e2e"))


from shared.document_metrics import (  # noqa: E402
    FINAL_SLOT_PRIORITY,
    document_stats,
    inline_artifact_to_markdown,
    is_writer_document,
    original_from_prompt,
    selected_final_slots,
    visible_text,
    writer_document_to_markdown,
)


# Back-compat alias used by tests and ad-hoc callers.
_visible_chars = visible_text


def document_metrics(final_path: Path | str,
                     original_path: Path | str | None = None,
                     original_prompt: str | None = None) -> dict:
    """File-based document metrics kept for perf_run (benchmark semantics)."""
    final_text = (
        Path(final_path).read_text(encoding="utf-8")
        if Path(final_path).is_file() else ""
    )
    out: dict = {
        "final_visible_chars": len(visible_text(final_text)),
        "final_md_chars": len(final_text),
        "final_md_lines": final_text.count("\n") + (1 if final_text else 0),
    }
    if original_path and Path(original_path).is_file():
        original = Path(original_path).read_text(encoding="utf-8")
        out["original_visible_chars"] = len(visible_text(original))
        out["original_md_chars"] = len(original)
        out["document_metrics_baseline"] = str(original_path)
    elif original_prompt:
        body = original_from_prompt(original_prompt) or original_prompt
        out["original_visible_chars"] = len(visible_text(body))
        out["original_md_chars"] = len(body)
    return out


PHASES = ("prepare", "outline", "write_document")
PHASE_LABELS = {
    "prepare": "prepare（准备）",
    "outline": "outline（大纲）",
    "write_document": "write_document（成稿）",
}

TRIGGER_NAMES = {"trigger_writer_workflow", "trigger_writer_plugin"}
# advance_step 是工作流引擎的步骤推进，不视为 Agent 工具调用。
ENGINE_SPANS = {"advance_step", *TRIGGER_NAMES}
# 容器 span：只用于聚合统计，不参与阶段墙钟与工具计数。
CONTAINER_SPANS = {"ReactAgent", "_indexed_call"}

# 回退口径：Writer 函数 -> 阶段。writer_update_writing_context 依时间先后归
# prepare（首个 outline span 前）或 write_document（其后）。
PHASE_FUNCTIONS = {
    "prepare": {
        "writer_prepare_workspace", "writer_build_writing_task",
        "writer_load_document", "writer_load_local_document",
        "writer_profile_resources", "writer_collect_available_media",
        "writer_create_writing_context",
    },
    "outline": {
        "writer_outline_workspace", "writer_prepare_outline",
        "writer_generate_outline", "writer_generate_section_instructions",
        "writer_generate_rewrite_outline", "writer_generate_rewrite_section_instructions",
    },
    "write_document": {
        "writer_draft_workspace",
        "writer_generate_draft_blocks", "writer_generate_draft_blocks_markdown",
        "writer_generate_draft_document", "writer_generate_draft_document_markdown",
        "writer_locate_revision_target", "writer_generate_modify_plan",
        "writer_generate_revision_set", "writer_apply_revision",
        "writer_publish_revision", "writer_replace_document",
        "writer_sync_document", "writer_append_document", "writer_create_document",
        "writer_preview_selection_rewrite", "writer_resolve_visual_media",
        "writer_resolve_revision_media", "writer_save_document",
        "writer_render_document", "writer_export_markdown",
        "writer_update_writing_context",
    },
}

DRAFT_SPANS = {"writer_generate_draft_blocks", "writer_generate_draft_blocks_markdown"}

RETRY_SENSITIVE_STEPS = {
    name for names in PHASE_FUNCTIONS.values() for name in names
}


def r1(value):
    return int(math.floor(value + 0.5))


def _llm_candidates(observations):
    """同一后端优先 OpenAIChat，否则使用 Langfuse generation=llm。"""
    openai = [o for o in observations if o.get("name") == "OpenAIChat"]
    if openai:
        return openai
    return [
        o for o in observations
        if o.get("type") == "GENERATION" and o.get("name") == "llm"
    ]


def _usage(observation):
    usage = observation.get("usageDetails") or {}
    if usage.get("input") is not None or usage.get("output") is not None:
        return {
            "input": int(usage.get("input") or 0),
            "output": int(usage.get("output") or 0),
        }
    return {
        "input": int(observation.get("promptTokens") or 0),
        "output": int(observation.get("completionTokens") or 0),
    }


def _epoch(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _span_latency(observation):
    if observation.get("latency") is not None:
        return float(observation.get("latency") or 0)
    start, end = _epoch(observation.get("startTime")), _epoch(observation.get("endTime"))
    return max(0.0, end - start) if start is not None and end is not None else 0.0


def _payload_chars(observation):
    """LLM 原始 io 载荷字符数（与参考文档字符口径对齐）；不可用时返回 None。"""
    attrs = (observation.get("metadata") or {}).get("attributes") or {}
    io_in = attrs.get("lazyllm.io.input")
    io_out = attrs.get("lazyllm.io.output")
    return {
        "input": len(str(io_in)) if io_in is not None else None,
        "output": len(str(io_out)) if io_out is not None else None,
    }


def _suppliers_from(observation):
    """从 trace 元数据解析供应商名（如 minimax）。"""
    attrs = (observation.get("metadata") or {}).get("attributes") or {}
    suppliers = attrs.get("lazyllm.entity.config.suppliers")
    if not suppliers:
        return set()
    return set(re.findall(r"\('([^']+)',", str(suppliers)))


def _model_info(valid_llms):
    suppliers = set()
    request_models = set()
    for llm in valid_llms:
        suppliers |= _suppliers_from(llm)
        attrs = (llm.get("metadata") or {}).get("attributes") or {}
        model = attrs.get("gen_ai.request.model")
        if model:
            request_models.add(str(model))
        # 具体模型名在 input.resolved_prompt.model（业务代码配置的默认对话模型）。
        raw = attrs.get("lazyllm.io.input") or ""
        try:
            payload = json.loads(raw)
            resolved = (payload or {}).get("resolved_prompt") or {}
            concrete = (resolved or {}).get("model")
            if concrete:
                request_models.add(str(concrete))
        except (json.JSONDecodeError, TypeError):
            pass
    return {
        "suppliers": sorted(suppliers),
        "request_model": sorted(request_models),
    }


def _agg_llms(items):
    result = {"input": 0, "output": 0, "latency": 0.0, "count": len(items),
              "input_chars": 0, "output_chars": 0, "chars_present": 0}
    for item in items:
        usage = _usage(item)
        result["input"] += usage["input"]
        result["output"] += usage["output"]
        result["latency"] += _span_latency(item)
        chars = _payload_chars(item)
        if chars["input"] is not None:
            result["input_chars"] += chars["input"]
            result["chars_present"] += 1
        if chars["output"] is not None:
            result["output_chars"] += chars["output"]
    return result


def _step_boundaries(observations):
    """从 advance_step 的 attempt_results 提取 (开始时间, 阶段) 边界。"""
    bounds = []
    for obs in observations:
        if obs.get("name") != "advance_step":
            continue
        output = obs.get("output") or {}
        results = output.get("attempt_results") or []
        step_id = results[0].get("step_id") if results else None
        if not step_id:
            # 新工作流：attempt_results 记录在 lazyllym.io.output 元数据中。
            attrs = (obs.get("metadata") or {}).get("attributes") or {}
            raw = attrs.get("lazyllm.io.output") or ""
            try:
                payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
            except json.JSONDecodeError:
                payload = {}
            results = (payload or {}).get("attempt_results") or []
            step_id = results[0].get("step_id") if results else None
        if step_id not in PHASES:
            continue
        start = _epoch(obs.get("startTime"))
        if start is not None:
            bounds.append((start, step_id))
    return sorted(bounds)


def _phase_windows(observations, bounds):
    """按 advance_step 边界构造 [start, end, phase) 时间窗。

    prepare 窗口从 trace 最早 span 开始，到第一个 outline 边界为止；
    outline / write_document 依次以各自边界为起点。未出现边界时
    返回空列表，由调用方走函数名回退。
    """
    starts = [_epoch(o.get("startTime")) for o in observations if _epoch(o.get("startTime")) is not None]
    ends = [_epoch(o.get("endTime")) for o in observations if _epoch(o.get("endTime")) is not None]
    if not starts or not ends or not bounds:
        return []
    t0 = min(starts)
    t_end = max(ends)
    windows = []
    for index, (ts, _phase) in enumerate(bounds):
        start = t0 if index == 0 else bounds[index - 1][0]
        end = bounds[index][0]
        if end <= start:
            continue
        windows.append((start, end, bounds[index - 1][1] if index else _phase))
    last = (bounds[-1][0], t_end + 1e-6, bounds[-1][1])
    windows.append(last)
    # 简化：去掉 prepare 自身 advance_step 前的重复窗口
    return windows


def _phase_for(windows, ts):
    for start, end, phase in windows:
        if start <= ts < end:
            return phase
    return windows[-1][2] if windows else None


def _fallback_phase_map(observations):
    """旧 trace 缺少 advance_step 时的 Writer 函数 -> 阶段映射（按 span 时间）。"""
    spans = []
    outline_first = None
    for obs in observations:
        name = str(obs.get("name") or "")
        if not name.startswith("writer_"):
            continue
        start = _epoch(obs.get("startTime"))
        if start is None:
            continue
        spans.append((start, obs))
        if name in PHASE_FUNCTIONS["outline"]:
            outline_first = min(outline_first or start, start)
    mapping = {}
    for start, obs in spans:
        name = str(obs.get("name") or "")
        if name == "writer_update_writing_context":
            mapping[obs.get("id")] = (
                "prepare" if outline_first is None or start < outline_first
                else "write_document"
            )
            continue
        for phase, names in PHASE_FUNCTIONS.items():
            if name in names:
                mapping[obs.get("id")] = phase
                break
    return mapping


def _fallback_windows(observations, mapping):
    """按 Writer span 的 phase 区间构造回退时间窗，相邻阶段以中点分界。"""
    by_phase = defaultdict(list)
    all_ts = []
    for obs in observations:
        start = _epoch(obs.get("startTime"))
        end = _epoch(obs.get("endTime"))
        if start is None:
            continue
        all_ts.append(start)
        if end is not None:
            all_ts.append(end)
        phase = mapping.get(obs.get("id"))
        if phase:
            by_phase[phase].append((start, end or start))
    if not all_ts:
        return []
    intervals = {}
    for phase in PHASES:
        if phase not in by_phase:
            continue
        starts = [s for s, _ in by_phase[phase]]
        ends = [e for _, e in by_phase[phase]]
        intervals[phase] = (min(starts), max(ends))
    if not intervals:
        return []
    order = [p for p in PHASES if p in intervals]
    windows = []
    prev_end = None
    for phase in order:
        start, end = intervals[phase]
        if prev_end is not None:
            start = (prev_end + start) / 2
        windows.append((start, end, phase))
        prev_end = end
    # 第一个 phase 窗口之前的时间归 prepare（触发/外层决策）。
    first_start, _, first_phase = windows[0]
    if first_phase != "prepare":
        windows.insert(0, (min(all_ts), first_start, "prepare"))
    return windows


def _tool_calls(observations):
    """工具调用包装 span：优先 _indexed_call，否则取非 LLM/非引擎的工具 span。"""
    indexed = [o for o in observations if o.get("name") == "_indexed_call"]
    if indexed:
        return indexed
    return [
        o for o in observations
        if str(o.get("name") or "")
        and o.get("name") not in ENGINE_SPANS
        and o.get("type") != "GENERATION"
        and not str(o.get("name") or "").startswith("writer_")
    ]


def _is_descendant(observation, root_id, index):
    parent = index.get(observation.get("parentObservationId"))
    while parent:
        if parent.get("id") == root_id:
            return True
        parent = index.get(parent.get("parentObservationId"))
    return False


def _detect_exceptions(observations, index, raw_llms):
    """异常附表：ERROR span、关键 Writer 步骤重复、失败的 artifact patch。"""
    errors = [o for o in observations if o.get("level", "DEFAULT") == "ERROR"]
    writer_counts = Counter(
        o.get("name") for o in observations
        if str(o.get("name") or "").startswith("writer_")
    )
    repeated = {
        name: count for name, count in writer_counts.items()
        if name in RETRY_SENSITIVE_STEPS and count > 1
    }
    failed_patches = [
        o for o in observations
        if o.get("name") == "patch_artifact"
        and isinstance(o.get("output"), dict)
        and ((o["output"].get("result") or {}).get("status") == "error")
    ]
    items = []
    for error in errors:
        start = _epoch(error.get("startTime"))
        end = _epoch(error.get("endTime"))
        latency = _span_latency(error)
        inside = [
            llm for llm in raw_llms
            if _is_descendant(llm, error.get("id"), index)
        ]
        llm_agg = _agg_llms(inside)
        name = str(error.get("name") or "unknown")
        items.append({
            "type": "error_observation",
            "name": name,
            "start_time": error.get("startTime") or "",
            "end_time": error.get("endTime") or "",
            "latency": latency,
            "llm_count": llm_agg["count"],
            "llm_input": llm_agg["input"],
            "llm_output": llm_agg["output"],
            "llm_latency": llm_agg["latency"],
            "retried": writer_counts.get(name, 0) > 1,
            "attempts": writer_counts.get(name, 0),
        })
    for name, count in sorted(repeated.items()):
        items.append({
            "type": "repeated_writer_steps", "name": name, "count": count,
        })
    if failed_patches:
        items.append({
            "type": "failed_patch_retry", "name": "patch_artifact",
            "count": len(failed_patches),
        })
    return items


WORKSPACE_TOOLS = ("writer_prepare_workspace", "writer_outline_workspace",
                   "writer_draft_workspace")


def _workspace_attempts(observations):
    """按时间排序的 workspace 工具调用序列；下标即尝试序号（0 = 首次/正常链路）。"""
    by_tool: dict[str, list] = {}
    for obs in observations:
        name = str(obs.get("name") or "")
        if name in WORKSPACE_TOOLS:
            by_tool.setdefault(name, []).append(obs)
    for spans in by_tool.values():
        spans.sort(key=lambda o: str(o.get("startTime") or ""))
    return by_tool


def _terminal_ts(observations):
    """工作流终态时间戳：advance_step / 工作流触发 / writer_* span 的最晚结束时间。"""
    ends = []
    for obs in observations:
        name = str(obs.get("name") or "")
        if name == "advance_step" or name == "trigger_writer_workflow" \
                or name.startswith("writer_"):
            end = _epoch(obs.get("endTime") or obs.get("startTime"))
            if end is not None:
                ends.append(end)
    return max(ends) if ends else None


def _tail_info(observations, terminal_end, index, raw_llms):
    """终态后的会话尾迹信息（工作流结束后的观测，不计入正常链路）。"""
    if terminal_end is None:
        return {"present": False, "wall": 0.0, "llm_count": 0,
                "start_time": "", "end_time": ""}
    tail = [
        (start, obs)
        for obs in observations
        if (start := _epoch(obs.get("startTime"))) is not None
        and start > terminal_end
        and obs.get("level", "DEFAULT") != "ERROR"
    ]
    if not tail:
        return {"present": False, "wall": 0.0, "llm_count": 0,
                "start_time": "", "end_time": ""}
    llm_count = sum(
        1 for _, obs in tail if obs.get("id") in {o.get("id") for o in raw_llms}
    )
    from datetime import datetime, timezone
    start_iso = datetime.fromtimestamp(min(s for s, _ in tail),
                                       tz=timezone.utc).isoformat()
    end_iso = datetime.fromtimestamp(max(
        (_epoch(o.get("endTime") or o.get("startTime")) or s) for s, o in tail
    ), tz=timezone.utc).isoformat()
    return {
        "present": True,
        "wall": round(_span_wall(tail), 3),
        "llm_count": llm_count,
        "start_time": start_iso,
        "end_time": end_iso,
    }


def _phase_tool(phase):
    """阶段对应的 workspace 顶层工具名（如 write_document -> writer_draft_workspace）。"""
    for name in WORKSPACE_TOOLS:
        if name in PHASE_FUNCTIONS.get(phase, ()):
            return name
    return None


def _tool_agg(wrappers, valid_llms, index):
    count = 0
    total = 0.0
    for wrapper in wrappers:
        count += 1
        llm_inside = sum(
            _span_latency(llm) for llm in valid_llms
            if _is_descendant(llm, wrapper.get("id"), index)
        )
        total += max(0.0, _span_latency(wrapper) - llm_inside)
    return count, total


def _span_wall(spans, exclude_engine=False):
    work = [
        (start, obs) for start, obs in spans
        if str(obs.get("name") or "") not in CONTAINER_SPANS
        and not (exclude_engine and str(obs.get("name") or "") in ENGINE_SPANS)
    ]
    if not work:
        return 0.0
    return max(_span_latency(obs) + start for start, obs in work) - min(
        start for start, _ in work
    )


def _draft_chapters(writer_spans, index, valid_llms):
    chapters = 0
    for span in writer_spans:
        if span.get("name") not in DRAFT_SPANS:
            continue
        chapter_llms = [
            llm for llm in valid_llms
            if _is_descendant(llm, span.get("id"), index)
        ]
        chapters += len(chapter_llms) if chapter_llms else 1
    for span in writer_spans:
        if span.get("name") != "writer_draft_workspace":
            continue
        attrs = (span.get("metadata") or {}).get("attributes") or {}
        raw = attrs.get("lazyllm.io.output") or ""
        try:
            payload = json.loads(raw) if raw.strip().startswith(("{", "[")) else {}
        except json.JSONDecodeError:
            payload = {}
        count = (payload or {}).get("draft_section_count")
        if isinstance(count, int):
            chapters = max(chapters, count)
    return chapters


def _phase_bounds(windows, phase):
    """阶段时间窗边界 (min_start, max_end)；无窗口返回 None。"""
    segs = [(s, e) for s, e, p in (windows or []) if p == phase]
    if not segs:
        return None
    return min(s for s, _ in segs), max(e for _, e in segs)


def _attempt_intervals(spans, bounds):
    """每次尝试的归属时间区间。

    真实时长（end>start）按 [start, end]；零时长标记（prepare/outline 的
    workspace span）扩展为“上个尝试结束 → 下个尝试开始/阶段末”，单次尝试时
    覆盖整个阶段段，避免漏算阶段内工作。
    """
    intervals = []
    for i, span in enumerate(spans):
        start = _epoch(span.get("startTime"))
        end = _epoch(span.get("endTime") or span.get("startTime"))
        if start is None:
            continue
        if end is not None and end - start >= 1.0:
            intervals.append((start, end))
            continue
        lo = intervals[-1][1] if intervals else (bounds[0] if bounds else start)
        if i + 1 < len(spans):
            hi = _epoch(spans[i + 1].get("startTime"))
        else:
            hi = bounds[1] if bounds else (end or start)
        intervals.append((lo, hi if hi is not None else (end or start)))
    return intervals


def _phase_aggregate(observations, index, raw_llms, valid_llms, windows, phase,
                     attempts=None, terminal_end=None, normal_collector=None):
    """三桶聚合单个阶段指标（维度一致，供两张同格式大表 + 导出使用）。

    - ``full``（完整过程）：阶段窗口内全部观测，时间上包含异常/尾迹；
    - ``abnormal``（异常/尾迹）：非最后一次尝试的重试、触发前探测、阶段间决策
      与终态后尾迹等一切非正常链路的观测；
    - ``normal``（正常链路）：该阶段 workspace 工具**最后一次调用**的执行，
      仅当该次调用成功（level=DEFAULT）时有效；末次仍失败则 ``normal=None``，
      由调用方把本用例标记为失败。

    归属采用时间区间：真实时长（end>start）的调用按其 [start,end]；零时长标记
    （prepare/outline 的 workspace span）扩展到该阶段段，避免漏算阶段内工作。
    """
    attempts = attempts or {}
    raw_ids = {llm.get("id") for llm in raw_llms}
    valid_ids = {llm.get("id") for llm in valid_llms}
    tool = _phase_tool(phase)
    spans = (attempts.get(tool) or []) if tool else []
    bounds = _phase_bounds(windows, phase)
    intervals = _attempt_intervals(spans, bounds)
    last_span = spans[-1] if spans else None
    last_interval = intervals[-1] if intervals else None
    single_success = len(spans) == 1 and bool(
        last_span is not None
        and last_span.get("level", "DEFAULT") == "DEFAULT"
    )
    last_succeeded = bool(
        last_span is not None
        and last_span.get("level", "DEFAULT") == "DEFAULT"
    )

    def in_interval(ts):
        if last_interval is None:
            return False
        lo, hi = last_interval
        return lo - 0.5 <= ts <= hi + 0.5

    full_spans, abnormal_spans, normal_spans = [], [], []
    full_llms, abnormal_llms, normal_llms = [], [], []
    full_writers, abnormal_writers, normal_writers = [], [], []

    for obs in observations:
        if obs.get("level", "DEFAULT") == "ERROR":
            continue
        start = _epoch(obs.get("startTime"))
        if start is None:
            continue
        if windows:
            p = _phase_for(windows, start)
        else:
            p = fallback_mapping.get(obs.get("id"))
        if p != phase:
            continue
        name = str(obs.get("name") or "")
        full_spans.append((start, obs))
        if obs.get("id") in raw_ids:
            full_llms.append(obs)
        if name.startswith("writer_"):
            full_writers.append(obs)

        if spans and last_succeeded and (single_success or in_interval(start)):
            normal_spans.append((start, obs))
            if obs.get("id") in raw_ids:
                normal_llms.append(obs)
            if name.startswith("writer_"):
                normal_writers.append(obs)
        else:
            abnormal_spans.append((start, obs))
            if obs.get("id") in raw_ids:
                abnormal_llms.append(obs)
            if name.startswith("writer_"):
                abnormal_writers.append(obs)

    if normal_collector is not None:
        normal_collector.extend(obs for _, obs in normal_spans)

    tool_wrappers, abnormal_tool_wrappers, normal_tool_wrappers = [], [], []
    for obs in _tool_calls(observations):
        start = _epoch(obs.get("startTime"))
        if start is None:
            continue
        child = next(
            (c for c in observations if c.get("parentObservationId") == obs.get("id")),
            None,
        )
        child_name = str((child or {}).get("name") or "")
        if child_name in ENGINE_SPANS:
            continue
        if _phase_for(windows, start) != phase:
            continue
        tool_wrappers.append(obs)
        if spans and last_succeeded and (single_success or in_interval(start)):
            normal_tool_wrappers.append(obs)
        else:
            abnormal_tool_wrappers.append(obs)

    tool_count, tool_time = _tool_agg(tool_wrappers, valid_llms, index)
    abnormal_tool_count, abnormal_tool_time = _tool_agg(
        abnormal_tool_wrappers, valid_llms, index
    )
    normal_tool_count, normal_tool_time = _tool_agg(
        normal_tool_wrappers, valid_llms, index
    )

    full_llm_agg = _agg_llms(full_llms)
    abnormal_llm_agg = _agg_llms(abnormal_llms)
    normal_llm_agg = _agg_llms(normal_llms)

    def bucket(wall_spans, llm_agg, tc, tt, writers, exclude_engine=False):
        chapters = _draft_chapters(writers, index, valid_llms)
        return {
            "wall": _span_wall(wall_spans, exclude_engine=exclude_engine),
            "llm_count": llm_agg["count"],
            "llm_latency": llm_agg["latency"],
            "tool_count": tc,
            "tool_time": tt,
            "input": llm_agg["input"],
            "output": llm_agg["output"],
            "input_chars": llm_agg["input_chars"],
            "output_chars": llm_agg["output_chars"],
            "chars_present": llm_agg["chars_present"],
            "chapters": chapters,
        }

    result = bucket(full_spans, full_llm_agg, tool_count, tool_time, full_writers)
    result["abnormal"] = bucket(
        abnormal_spans, abnormal_llm_agg,
        abnormal_tool_count, abnormal_tool_time, abnormal_writers,
        exclude_engine=True,
    )
    result["normal_attempt"] = len(spans)
    result["normal_succeeded"] = last_succeeded
    result["normal"] = (
        bucket(normal_spans, normal_llm_agg,
               normal_tool_count, normal_tool_time, normal_writers)
        if last_succeeded else None
    )
    return result


def _doc_stats_for(doc_stats):
    """从 document_stats JSON 提取 write_document 字数指标。"""
    result = {"gen_chars": None, "rev_chars": None}
    if not doc_stats:
        return result
    final = doc_stats.get("final") or {}
    if final.get("visible_characters") is not None:
        result["gen_chars"] = int(final["visible_characters"])
    revision = doc_stats.get("revision") or {}
    if revision.get("present") and revision.get("changed_final_characters") is not None:
        result["rev_chars"] = int(revision["changed_final_characters"])
    return result


def extract(trace_data, doc_stats=None):
    observations = trace_data.get("observations") or []
    index = {o.get("id"): o for o in observations if o.get("id")}
    raw_llms = _llm_candidates(observations)
    valid_llms = [
        o for o in raw_llms
        if index.get(o.get("parentObservationId"), {}).get("level", "DEFAULT") != "ERROR"
    ]
    valid_ids = {o.get("id") for o in valid_llms}
    attempts = _workspace_attempts(observations)
    terminal_end = _terminal_ts(observations)

    bounds = _step_boundaries(observations)
    if bounds:
        windows = _phase_windows(observations, bounds)
        attribution = "advance_step"
    else:
        fallback_mapping = _fallback_phase_map(observations)
        windows = _fallback_windows(observations, fallback_mapping)
        attribution = "function_fallback" if fallback_mapping else "missing"

    normal_collector: list = []
    phases = {}
    for phase in PHASES:
        agg = _phase_aggregate(
            observations, index, raw_llms, valid_llms, windows, phase,
            attempts=attempts, terminal_end=terminal_end,
            normal_collector=normal_collector,
        )
        if windows:
            phases[phase] = agg
        if phase == "write_document" and phase in phases:
            doc = _doc_stats_for(doc_stats)
            phases[phase]["gen_chars"] = doc["gen_chars"]
            phases[phase]["rev_chars"] = doc["rev_chars"]
            if phases[phase].get("normal"):
                phases[phase]["normal"]["gen_chars"] = doc["gen_chars"]
                phases[phase]["normal"]["rev_chars"] = doc["rev_chars"]

    first_bound_ts = bounds[0][0] if bounds else None
    pre_workflow = _pre_workflow_bucket(
        observations, first_bound_ts, normal_collector, valid_ids,
    )
    clean_full = _clean_full_flow(pre_workflow, phases)
    full_link = _agg_llms(valid_llms)
    exceptions = _detect_exceptions(observations, index, raw_llms)
    return {
        "wall": float(trace_data.get("latency") or 0),
        "full_link": full_link,
        "pre_workflow": pre_workflow,
        "clean_full": clean_full,
        "phases": phases,
        "attribution": attribution,
        "model_info": _model_info(valid_llms),
        "exceptions": exceptions,
        "tail": _tail_info(observations, terminal_end, index, raw_llms),
    }


def _blank_phase():
    return {
        "wall": 0.0, "llm_count": 0.0, "llm_latency": 0.0,
        "tool_count": 0.0, "tool_time": 0.0,
        "input": 0, "output": 0, "input_chars": 0, "output_chars": 0,
        "chars_present": 0, "chapters": 0.0,
        "gen_chars": None, "rev_chars": None, "present": 0,
    }


def _pre_workflow_bucket(observations, first_bound_ts, normal_collector, valid_ids):
    """触发前/路由段：进入首个工作流步骤之前的观测（路由 LLM、trigger 等）。

    已计入各阶段 normal（最后一次成功尝试）的观测不重复计入：单次成功场景下
    normal 桶本身已包含触发前观测，避免全流程口径重复累加。
    """
    normal_ids = {o.get("id") for o in (normal_collector or []) if o.get("id")}
    spans, llms = [], []
    for obs in observations:
        start = _epoch(obs.get("startTime"))
        if start is None or first_bound_ts is None or start >= first_bound_ts:
            continue
        if obs.get("id") and obs.get("id") in normal_ids:
            continue
        if obs.get("level", "DEFAULT") == "ERROR":
            continue
        spans.append((start, obs))
        if obs.get("id") in valid_ids:
            llms.append(obs)
    agg = _agg_llms(llms)
    return {
        "wall": round(_span_wall(spans), 3),
        "count": agg["count"],
        "latency": round(agg["latency"], 3),
        "input": agg["input"], "output": agg["output"],
        "input_chars": agg["input_chars"], "output_chars": agg["output_chars"],
        "chars_present": agg["chars_present"],
        "present": 1 if spans else 0,
    }


def _clean_full_flow(pre_workflow, phases):
    """全流程干净链路：触发前/路由段 + 各阶段 normal（最后一次成功尝试）合计。

    用于飞书导出的“全流程”行：保留路由/触发前时间，剔除重试与异常（异常/尾迹
    单独在统计表中列示，不进入导出值）。
    """
    def normal_sum(key):
        return sum(
            (p.get("normal") or {}).get(key) or 0
            for p in phases.values()
        )

    return {
        "wall": round((pre_workflow.get("wall") or 0) + normal_sum("wall"), 3),
        "count": (pre_workflow.get("count") or 0) + normal_sum("llm_count"),
        "latency": round(
            (pre_workflow.get("latency") or 0) + normal_sum("llm_latency"), 3,
        ),
        "input": (pre_workflow.get("input") or 0) + normal_sum("input"),
        "output": (pre_workflow.get("output") or 0) + normal_sum("output"),
        "input_chars": (pre_workflow.get("input_chars") or 0)
        + normal_sum("input_chars"),
        "output_chars": (pre_workflow.get("output_chars") or 0)
        + normal_sum("output_chars"),
        "chars_present": (pre_workflow.get("chars_present") or 0)
        + normal_sum("chars_present"),
        "present": 1,
    }


def avg_traces(traces_data, scenario, doc_stats_files=None, doc_stats=None):
    """场景级聚合。

    ``doc_stats_files`` 为 ``<场景>/case_N_document_stats.json`` 形式的文件路径
    （旧布局）；``doc_stats`` 为与 ``traces_data`` 按下标对齐的文档度量 dict 列表
    （新布局，优先使用）。文档度量用于对齐数据表的“生成/修改文章字数”列。
    """
    n = len(traces_data)
    if not n:
        return {"scenario": scenario, "n_traces": 0}
    doc_map = {}
    if doc_stats_files:
        for path in doc_stats_files:
            match = re.search(r"([^/]+)/case_(\d+)_document_stats\.json$", str(path))
            if not match:
                continue
            try:
                doc_map[(match.group(1), int(match.group(2)))] = json.loads(
                    Path(path).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue

    per_trace = []
    for index, trace in enumerate(traces_data):
        item_stats = None
        if doc_stats and index < len(doc_stats) and doc_stats[index]:
            item_stats = doc_stats[index]
        else:
            match = re.search(
                r"([^/]+)/traces/case_(\d+)\.json$", str(trace.get("source") or "")
            )
            if match:
                item_stats = doc_map.get((match.group(1), int(match.group(2))))
        per_trace.append(extract(trace, doc_stats=item_stats))

    def average_metric(key):
        row = {
            "input": r1(sum(t[key]["input"] for t in per_trace) / n),
            "output": r1(sum(t[key]["output"] for t in per_trace) / n),
            "latency": round(sum(t[key]["latency"] for t in per_trace) / n, 3),
            "count": round(sum(t[key]["count"] for t in per_trace) / n, 1),
            "present": n,
        }
        if "input_chars" in per_trace[0][key]:
            row["input_chars"] = r1(sum(t[key]["input_chars"] for t in per_trace) / n)
            row["output_chars"] = r1(sum(t[key]["output_chars"] for t in per_trace) / n)
            row["chars_present"] = sum(t[key]["chars_present"] for t in per_trace)
        return row

    def average_phase(phase):
        rows = [t["phases"].get(phase) for t in per_trace if phase in t["phases"]]
        if not rows:
            return None
        out = _blank_phase()
        for key in ("wall", "llm_count", "llm_latency", "tool_count", "tool_time",
                    "input", "output", "input_chars", "output_chars", "chapters"):
            out[key] = round(sum(row.get(key, 0) or 0 for row in rows) / n, 3)
        out["chars_present"] = sum(row.get("chars_present", 0) for row in rows)
        out["present"] = len(rows)
        gen_rows = [row.get("gen_chars") for row in rows if row.get("gen_chars") is not None]
        rev_rows = [row.get("rev_chars") for row in rows if row.get("rev_chars") is not None]
        out["gen_chars"] = r1(sum(gen_rows) / n) if gen_rows else None
        out["rev_chars"] = r1(sum(rev_rows) / n) if rev_rows else None

        def sub_bucket(key):
            sub_rows = [row.get(key) for row in rows if row.get(key)]
            if not sub_rows:
                return None
            sub = _blank_phase()
            for k in ("wall", "llm_count", "llm_latency", "tool_count", "tool_time",
                      "input", "output", "input_chars", "output_chars", "chapters"):
                sub[k] = round(
                    sum((r.get(k) or 0) for r in sub_rows) / len(sub_rows), 3,
                )
            sub["chars_present"] = sum(r.get("chars_present", 0) for r in sub_rows)
            sub["present"] = len(sub_rows)
            gen = [r.get("gen_chars") for r in sub_rows if r.get("gen_chars") is not None]
            rev = [r.get("rev_chars") for r in sub_rows if r.get("rev_chars") is not None]
            sub["gen_chars"] = r1(sum(gen) / n) if gen else None
            sub["rev_chars"] = r1(sum(rev) / n) if rev else None
            return sub

        abnormal = sub_bucket("abnormal")
        if abnormal is not None:
            out["abnormal"] = abnormal
            out["abnormal_attempts"] = sum(
                max(0, int(row.get("normal_attempt") or 0) - 1)
                for row in rows
            )
            out["abnormal_present"] = len([row for row in rows if row.get("abnormal")])
        normal = sub_bucket("normal")
        if normal is not None:
            out["normal"] = normal
            out["normal_present"] = len([row for row in rows if row.get("normal")])
            out["normal_succeeded"] = all(
                bool(row.get("normal_succeeded")) for row in rows
            )
        out["normal_attempt"] = r1(
            sum(int(row.get("normal_attempt") or 0) for row in rows) / n
        )
        return out

    phases = {phase: average_phase(phase) for phase in PHASES}
    phases = {k: v for k, v in phases.items() if v is not None}

    exception_cases = []
    for index, trace in enumerate(per_trace, 1):
        if trace["exceptions"]:
            exception_cases.append({"case_index": index, "items": trace["exceptions"]})

    abnormal_cases = []
    for index, trace in enumerate(per_trace, 1):
        for phase in PHASES:
            row = trace["phases"].get(phase) or {}
            if not row or int(row.get("normal_attempt") or 0) == 0:
                continue
            abnormal_cases.append({
                "case_index": index,
                "phase": phase,
                "tool": _phase_tool(phase) or "",
                "attempts": int(row.get("normal_attempt") or 0),
                "normal_succeeded": bool(row.get("normal_succeeded")),
                "abnormal": (row.get("abnormal") or _blank_phase()),
                "tail": trace.get("tail") or {"present": False},
            })

    full_link = average_metric("full_link")
    full_link["wall"] = round(sum(t["wall"] for t in per_trace) / n, 3)
    clean_full = average_metric("clean_full")
    clean_full["wall"] = round(
        sum(t["clean_full"]["wall"] for t in per_trace) / n, 3,
    )
    attribution = Counter(t["attribution"] for t in per_trace).most_common(1)[0][0]
    suppliers = sorted({s for t in per_trace for s in t["model_info"]["suppliers"]})
    request_models = sorted({
        m for t in per_trace for m in t["model_info"]["request_model"]
    })
    return {
        "scenario": scenario,
        "n_traces": n,
        "full_link": full_link,
        "clean_full": clean_full,
        "phases": phases,
        "exceptions": exception_cases,
        "abnormal_cases": abnormal_cases,
        "attribution": attribution,
        "model_info": {"suppliers": suppliers, "request_model": request_models},
        # 兼容旧消费者。
        "wall": full_link["wall"],
    }


def _fmt(value, unit="", blank="—"):
    if value is None:
        return blank
    if isinstance(value, float) and value == int(value):
        value = int(value)
    if isinstance(value, int):
        return f"{value:,}"
    return f"{value:,.3f} {unit}".strip()


def _render_metric_table(phases, full_link, phase_key, title, note):
    """按统一维度渲染一张三阶段统计表；phase_key 为 None 表示完整过程（full）。"""
    def getp(p):
        row = phases.get(p) or {}
        return row if phase_key is None else (row.get(phase_key) or {})

    lines = [f"## {title}", "", f"> {note}", "",
             "| 指标 | 全链路 | prepare | outline | write_document |",
             "|---|---:|---:|---:|---:|"]
    full_value = (lambda v: v) if full_link is not None else (lambda v: "—")
    rows = [
        ("总耗时", full_value(f"{full_link['wall']:.3f} s（墙钟）" if full_link else "—"),
         *(f"{_fmt(getp(p).get('wall'))} s" if getp(p) else "—" for p in PHASES)),
        ("LLM 推理轮数", full_value(f"{full_link['count']:.1f}" if full_link else "—"),
         *(f"{_fmt(getp(p).get('llm_count'))}" if getp(p) else "—" for p in PHASES)),
        ("LLM 累计耗时", full_value(f"{full_link['latency']:.3f} s" if full_link else "—"),
         *(f"{_fmt(getp(p).get('llm_latency'))} s" if getp(p) else "—" for p in PHASES)),
        ("Agent 工具调用次数", "—",
         *(f"{_fmt(getp(p).get('tool_count'))}" if getp(p) else "—" for p in PHASES)),
        ("工具实际执行时间", "—",
         *(f"{_fmt(getp(p).get('tool_time'))} s" if getp(p) else "—" for p in PHASES)),
        ("LLM 累计输入 token", full_value(f"{full_link['input']:,}" if full_link else "—"),
         *(f"{_fmt(getp(p).get('input'))}" if getp(p) else "—" for p in PHASES)),
        ("LLM 累计输入字符", full_value(
            f"{full_link.get('input_chars', 0):,}" if full_link and full_link.get("input_chars") else "—"),
         *(f"{_fmt(getp(p).get('input_chars'))}" if getp(p) else "—" for p in PHASES)),
        ("LLM 累计输出 token", full_value(f"{full_link['output']:,}" if full_link else "—"),
         *(f"{_fmt(getp(p).get('output'))}" if getp(p) else "—" for p in PHASES)),
        ("LLM 累计输出字符", full_value(
            f"{full_link.get('output_chars', 0):,}" if full_link and full_link.get("output_chars") else "—"),
         *(f"{_fmt(getp(p).get('output_chars'))}" if getp(p) else "—" for p in PHASES)),
    ]
    for label, fv, p1, p2, p3 in rows:
        lines.append(f"| {label} | {fv} | {p1} | {p2} | {p3} |")
    write = getp("write_document")
    if write:
        lines.append(f"| draft 章节数 | — | — | — | {_fmt(write.get('chapters'))} |")
        lines.append(
            f"| 生成/修改文章字数 | — | — | — | 生成 {_fmt(write.get('gen_chars'))} / "
            f"修改 {_fmt(write.get('rev_chars'))} |"
        )
    return lines


def render_table(stats):
    n = stats["n_traces"]
    phases = stats.get("phases") or {}
    full = stats["full_link"]
    lines = [
        "## 性能统计（prepare / outline / write_document）",
        "",
        f"> 阶段归属口径：{stats.get('attribution')}。",
    ]
    lines += _render_metric_table(
        phases, full, None,
        "一、完整过程统计主表（真实数据）",
        "完整过程包含全部观测：所有尝试（含重试）、触发前探测、阶段间决策与终态后尾迹；"
        "时间上包含下表的异常/尾迹部分。耗时口径：墙钟 / LLM 累计耗时 / 工具实际执行时间"
        "（工具 span 墙钟扣除其中 LLM 耗时）。",
    )
    lines += _render_metric_table(
        phases, None, "abnormal",
        "二、异常/尾迹时间统计（同维度）",
        "异常/尾迹 = 完整过程中非正常链路的部分：非最后一次尝试的重试、触发前探测、"
        "阶段间决策 LLM、终态后尾迹；维度与完整过程表一致。正常链路（每个阶段/工具调用的"
        "最后一次、且成功）见 stats.json 的 phases.*.normal，供导出数据表使用。",
    )

    lines.extend(["", "## 三、异常来源说明", ""])
    exceptions = stats.get("exceptions") or []
    if not exceptions:
        lines.append("未观测到 ERROR span、关键 Writer 步骤重复或失败的 artifact patch。")
    else:
        lines.extend([
            "| 案例序号 | 异常类型 | span | 时间窗口 | 墙钟 | 阶段内 LLM | 明细 |",
            "|---:|---|---|---|---:|---|---|",
        ])
        for case in exceptions:
            for item in case["items"]:
                if item["type"] == "error_observation":
                    window = f"{item.get('start_time', '')} → {item.get('end_time', '')}"
                    llm = (
                        f"{item['llm_count']} 次 / {item['llm_input']:,} in / "
                        f"{item['llm_output']:,} out / {item['llm_latency']:.3f}s"
                    )
                    detail = (
                        f"共 {item.get('attempts', 1)} 次尝试（重试 "
                        f"{max(0, int(item.get('attempts', 1)) - 1)} 次）"
                        if item.get("retried") else "未观察到重试"
                    )
                    lines.append(
                        f"| {case['case_index']} | error_observation | {item['name']} | "
                        f"{window} | {item['latency']:.3f} s | {llm} | {detail} |"
                    )
                else:
                    lines.append(
                        f"| {case['case_index']} | {item['type']} | {item.get('name', '')} | "
                        f"— | — | — | ×{item.get('count', 1)} |"
                    )

    abnormal_cases = stats.get("abnormal_cases") or []
    if abnormal_cases:
        lines.extend([
            "",
            "| 案例序号 | 阶段 | 工具 | 调用次数 | 重试次数 | 末次是否成功 | "
            "终态后尾迹 | 异常/尾迹墙钟 | 异常/尾迹 LLM |",
            "|---:|---|---|---:|---:|---|---:|---:|---:|",
        ])
        for item in abnormal_cases:
            tail = item.get("tail") or {}
            tail_txt = (
                f"{str(tail.get('start_time', ''))[11:19]} → "
                f"{str(tail.get('end_time', ''))[11:19]}（{tail.get('wall', 0):.1f}s）"
                if tail.get("present") else "无"
            )
            ab = item.get("abnormal") or {}
            attempts = int(item.get("attempts") or 0)
            lines.append(
                f"| {item['case_index']} | "
                f"{PHASE_LABELS.get(item['phase'], item['phase'])} | "
                f"{item['tool']} | {attempts} | {max(0, attempts - 1)} | "
                f"{'成功' if item.get('normal_succeeded') else '失败'} | {tail_txt} | "
                f"{ab.get('wall', 0):.3f} s | {ab.get('llm_count', 0)} |"
            )
        lines.append(
            "> 末次成功与否决定用例是否可导出正常链路（normal）：末次仍失败则本用例判失败，"
            "不导出，记录到最终分析报告。"
        )
    return "\n".join(lines) + "\n"


FULL_FLOW_PHASE = "全流程"


def _pick(row, key, *, prefer_normal=True):
    """阶段指标取值：正常链路（normal）优先，旧统计无 normal 时回退完整过程。"""
    if prefer_normal and row.get("normal"):
        value = row["normal"].get(key)
    else:
        value = row.get(key)
    return value if value is not None else 0


def _full_flow_row(stats, batch_id, scenario, phases):
    """构造“全流程”阶段行（键 `批次ID|场景|全流程`）。

    口径：墙钟 / LLM / token 取“干净全链路”（clean_full = 触发前/路由段 + 各阶段
    最后一次成功尝试的 normal 合计），保留路由/触发前时间、剔除重试与异常；
    旧统计无 clean_full 时回退 trace 全链路（full_link）。工具次数与工具时间按
    各阶段正常链路合计；章节数与生成/修改字数取 write_document 阶段；修订场景
    （有 rev_chars）无 draft 章节数，章节数输出 “—”。
    """
    fl = stats.get("clean_full") or stats.get("full_link") or {}
    tool_count = round(sum(_pick(r, "tool_count") for r in phases.values()), 1)
    tool_time = round(sum(_pick(r, "tool_time") for r in phases.values()), 3)
    wd = phases.get("write_document") or {}
    wd_n = wd.get("normal") or wd
    is_revise = (wd_n.get("rev_chars") is not None) or (wd.get("rev_chars") is not None)
    chapters = wd_n.get("chapters")
    if is_revise or chapters is None:
        chapters = "—"
    return [
        batch_id, scenario, FULL_FLOW_PHASE,
        round(float(fl.get("wall") or 0), 3),
        float(fl.get("count") or 0),
        round(float(fl.get("latency") or 0), 3),
        tool_count, tool_time,
        int(fl.get("input") or 0), int(fl.get("input_chars") or 0),
        int(fl.get("output") or 0), int(fl.get("output_chars") or 0),
        chapters,
        wd_n.get("gen_chars") if wd_n.get("gen_chars") is not None else wd.get("gen_chars"),
        wd_n.get("rev_chars") if wd_n.get("rev_chars") is not None else wd.get("rev_chars"),
        f"{batch_id}|{scenario}|{FULL_FLOW_PHASE}",
    ]


def sheet_rows(stats, batch_id, *, with_full_flow=True):
    """按飞书“全数据”表维度生成导出行；正常链路取最后一次成功尝试（normal）。

    返回 ``(rows, failed)``：
    - rows：每阶段一行
      ``[批次ID, 场景, 阶段, 总耗时, LLM 轮数, LLM 耗时, 工具次数, 工具时间,
      in/out token, in/out 字符, 章节数, 生成字数, 修改字数, 键]``；失败用例
      （末次尝试失败）行保留，但指标列全部填 ``—``，由最终报告记录失败原因；
      另附一行阶段=“全流程”（``with_full_flow=True``），键
      ``批次ID|场景|全流程``，失败场景不生成全流程行；
    - failed：末次尝试失败的 ``[场景, 阶段]`` 列表。
    """
    rows, failed = [], []
    scenario = str(stats.get("scenario") or "")
    phases = stats.get("phases") or {}
    failed_cases = {
        sc for sc, _ in (
            (scenario, phase)
            for phase, row in phases.items()
            if int(row.get("normal_attempt") or 0) > 0
            and row.get("normal_succeeded") is not True
        )
    }
    for phase in PHASES:
        row = phases.get(phase) or {}
        if not row.get("wall") and not row.get("llm_count"):
            continue
        if int(row.get("normal_attempt") or 0) > 0 \
                and row.get("normal_succeeded") is not True:
            failed.append([scenario, phase])
        if scenario in failed_cases:
            rows.append([
                batch_id, scenario, phase, "—", "—", "—", "—", "—",
                "—", "—", "—", "—", "—", "—", "—",
                f"{batch_id}|{scenario}|{phase}",
            ])
            continue
        n = row.get("normal") or row
        chapters = n.get("chapters") if phase == "write_document" else "—"
        if phase == "write_document" and (
            n.get("rev_chars") is not None or row.get("rev_chars") is not None
        ):
            # 修订场景无 draft 章节数上报：章节数按 “—” 计
            chapters = "—"
        gen = n.get("gen_chars") if phase == "write_document" else "—"
        rev = n.get("rev_chars") if phase == "write_document" else "—"
        rows.append([
            batch_id, scenario, phase,
            round(n.get("wall") or 0, 3), n.get("llm_count"),
            round(n.get("llm_latency") or 0, 3), n.get("tool_count"),
            round(n.get("tool_time") or 0, 3),
            int(n.get("input") or 0), int(n.get("input_chars") or 0),
            int(n.get("output") or 0), int(n.get("output_chars") or 0),
            chapters, gen, rev,
            f"{batch_id}|{scenario}|{phase}",
        ])
    if with_full_flow and scenario not in failed_cases and phases:
        rows.append(_full_flow_row(stats, batch_id, scenario, phases))
    return rows, failed


def render_analysis(stats):
    """初步分析：长耗时/高 Token 阶段及原因，异常情况。"""
    n = stats["n_traces"]
    full = stats["full_link"]
    phases = stats.get("phases") or {}
    lines = ["## 分析结论（初步）", ""]

    def shares(metric):
        total = sum((phases[p].get(metric) or 0) for p in phases)
        return sorted(
            ((p, phases[p].get(metric) or 0) for p in phases),
            key=lambda kv: kv[1], reverse=True,
        ), total

    # 长耗时
    wall_rank, wall_total = shares("wall")
    if wall_total:
        top_wall = wall_rank[0]
        top_pct = top_wall[1] / wall_total * 100
        top = phases[top_wall[0]]
        llm_share = top["llm_latency"] / top_wall[1] if top_wall[1] else 0
        tool_share = top["tool_time"] / top_wall[1] if top_wall[1] else 0
        reasons = []
        if llm_share > 0.6:
            reasons.append(f"LLM 推理累计占该阶段 {llm_share:.0%}，以推理为主")
        if tool_share > 0.3:
            reasons.append(f"工具实际执行占该阶段 {tool_share:.0%}，工具往返/执行为主")
        if top_wall[0] == "write_document" and top.get("chapters", 0) > 1:
            reasons.append(
                f"章节数 {top.get('chapters')}，逐章生成且上下文累计时耗时随章节递增"
            )
        if top_wall[0] == "prepare":
            reasons.append("外层 Agent 触发选择与资源分析集中在准备阶段")
        lines.append(
            f"- 长耗时：{PHASE_LABELS.get(top_wall[0], top_wall[0])} 墙钟占比最高"
            f"（{top_pct:.1f}%，{top_wall[1]:.1f}s / 阶段合计 {wall_total:.1f}s）。"
            + ("；".join(reasons) or "无明显单一原因，需结合事件时间线进一步定位。")
        )
        gap = full["wall"] - wall_total
        if gap > 1:
            lines.append(
                f"- 阶段墙钟合计 {wall_total:.1f}s，与全链路墙钟 {full['wall']:.1f}s "
                f"相差 {gap:.1f}s（阶段间空隙、异常/尾迹、审批/轮询或未观测开销；"
                "异常/尾迹见异常/尾迹统计表）。"
            )

    # 高 Token
    for metric, label in (("input", "Input"), ("output", "Output")):
        rank, total = shares(metric)
        if not total:
            continue
        top = rank[0]
        top_pct = top[1] / total * 100
        notes = []
        if metric == "input":
            avg_per_llm = (
                phases[top[0]]["input"] / phases[top[0]]["llm_count"]
                if phases[top[0]].get("llm_count") else 0
            )
            notes.append(f"该阶段单次 LLM 平均输入约 {avg_per_llm:,.0f} token")
            if top[0] == "write_document" and phases[top[0]].get("chapters", 0) > 1:
                notes.append("逐章携带前文/上下文累计通常是主要来源")
            if top[0] == "prepare":
                notes.append("外层 Agent 决策与资源/上下文反复读取通常是主要来源")
        value_text = f"{round(top[1], 1):,}"
        total_text = f"{round(total, 1):,}"
        lines.append(
            f"- 高 Token（{label}）：{PHASE_LABELS.get(top[0], top[0])} 占比最高"
            f"（{top_pct:.1f}%，{value_text} / 阶段合计 {total_text}）。"
            + ("；".join(notes) if notes else "")
        )

    # 异常
    exceptions = stats.get("exceptions") or []
    abnormal_cases = stats.get("abnormal_cases") or []
    abnormal_wall = sum(
        (item.get("abnormal") or {}).get("wall", 0)
        for item in abnormal_cases
    )
    tail_cases = [item for item in abnormal_cases
                  if (item.get("tail") or {}).get("present")]
    if exceptions:
        total_wall = sum(
            item.get("latency", 0)
            for case in exceptions for item in case["items"]
            if item["type"] == "error_observation"
        )
        lines.append(
            f"- 异常：{len(exceptions)} 个案例存在 ERROR span，墙钟合计约 "
            f"{total_wall:.1f}s；异常/尾迹墙钟合计约 {abnormal_wall:.1f}s，"
            f"其中 {len(tail_cases)} 个案例存在终态后尾迹。"
        )
    elif abnormal_wall > 1 or tail_cases:
        lines.append(
            f"- 异常：未观测到 ERROR span，但异常/尾迹墙钟合计约 {abnormal_wall:.1f}s"
            f"（{len(tail_cases)} 个案例含终态后尾迹），见异常/尾迹统计表。"
        )
    else:
        lines.append("- 异常：未观测到异常阶段。")
    failed = [item for item in abnormal_cases if not item.get("normal_succeeded")]
    if failed:
        lines.append(
            "- 失败判定："
            + "、".join(
                f"{PHASE_LABELS.get(item['phase'], item['phase'])} 末次尝试失败"
                for item in failed
            )
            + "；本用例判失败，不导出正常链路数据。"
        )
    lines.append(
        f"- 口径说明：n={n}，{stats.get('attribution')} 归属；"
        "分项指标互斥，阶段合计可与全链路墙钟不一致（嵌套/并发/空隙）。"
    )
    missing = [
        (phase, row.get("present", 0))
        for phase, row in phases.items()
        if row.get("present", 0) < n
    ]
    if missing:
        details = "、".join(
            f"{PHASE_LABELS.get(p, p)} 仅出现在 {present}/{n} 案例"
            for p, present in missing
        )
        lines.append(
            f"- 覆盖提示：{details}，缺失案例按 0 计入平均（JSON 保留 present 供排查）。"
        )
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="计算 Writer 三阶段性能统计")
    parser.add_argument("traces", nargs="+", help="trace JSON 文件路径")
    parser.add_argument("--scenario", default="unnamed")
    parser.add_argument("--table", action="store_true", help="输出 Markdown 报告")
    parser.add_argument("--analysis", action="store_true", help="追加初步分析结论")
    parser.add_argument("--doc-stats", nargs="*", default=None,
                        help="case_N_document_stats.json，按 case 编号与 trace 匹配")
    args = parser.parse_args()
    traces = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.traces]
    for trace, path in zip(traces, args.traces):
        trace.setdefault("source", path)
    stats = avg_traces(traces, args.scenario, args.doc_stats)
    if args.table:
        output = render_table(stats)
        if args.analysis:
            output += "\n" + render_analysis(stats)
        print(output, end="")
    else:
        print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
