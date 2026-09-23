#!/usr/bin/env python3
"""从归一化 Writer trace 生成三阶段（prepare/outline/write_document）性能统计与 Markdown 报告。

统计维度对齐《写作性能优化》文档：每个阶段合并统计
阶段总耗时、LLM 推理轮数与累计耗时、Agent 工具调用次数、工具实际执行时间、
LLM 累计输入/输出（token 与原始载荷字符）；write_document 额外统计
draft 章节数与生成/修改文章字数。

阶段只统计 advance_step 的完整执行子树；步骤外发起决策只计全流程。
workspace 用于识别阶段，不是计时边界；不使用前端状态或相邻阶段时间窗。
缺失或歧义的父链直接报错；缺少决策关联证据的调用保留在全流程并单独列出。

本模块是 ``writer-benchmark/scripts/compute_stats.py`` 在统一测试栈中的移植
（writer-test 自包含，不依赖旧目录）；文档度量复用 ``shared/document_metrics.py``。
"""

import argparse
from validate_diagnostics import classification_errors
import json
import math
import re
import sys
from collections import Counter
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
from analyze_common import (  # noqa: E402
    WRITER_PHASE_FUNCTIONS as PHASE_FUNCTIONS,
    WRITER_PHASES as PHASES,
)


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

RETRY_SENSITIVE_STEPS = {
    name for names in PHASE_FUNCTIONS.values() for name in names
}


def r1(value):
    return int(math.floor(value + 0.5))


def _llm_candidates(observations):
    """Current OTel/Langfuse LLM observations; each span represents one call."""
    return [o for o in observations if o.get("type") == "GENERATION"
            and o.get("name") == "llm"]

def _normalize_llm_usage(observations, semantics):
    """Normalize explicitly declared cumulative counters before retry filtering.

    Never infer cumulative semantics merely from increasing token values. The
    cumulative contract requires a complete run from newly created modules.
    Source observations remain untouched; extract works on shallow copies.
    """
    if semantics == "per_call":
        return []
    if semantics != "cumulative_entity":
        raise ValueError(f"unsupported LLM usage semantics: {semantics}")
    groups = {}
    for obs in _llm_candidates(observations):
        attrs = (obs.get("metadata") or {}).get("attributes") or {}
        entity = attrs.get("lazyllm.entity.id")
        if not entity:
            raise ValueError(f"cumulative usage missing entity ID: {obs['id']}")
        groups.setdefault((obs.get("traceId"), entity), []).append(obs)
    corrections = []
    for (_, entity), items in groups.items():
        previous = {"input": 0, "output": 0}
        previous_end = None
        unknown = False
        for obs in sorted(items, key=lambda o: _interval(o)):
            lo, hi = _interval(obs)
            if previous_end is not None and lo < previous_end:
                raise ValueError(f"overlapping cumulative usage for entity {entity}")
            previous_end = hi
            raw = obs.get("usageDetails") or {}
            if any(raw.get(k) is None for k in previous):
                if obs.get("level") != "ERROR":
                    raise ValueError(f"cumulative usage missing tokens: {obs['id']}")
                unknown = True
                continue
            if unknown:
                raise ValueError(f"unknown cumulative baseline after ERROR: {obs['id']}")
            current = {k: int(raw[k]) for k in previous}
            delta = {k: current[k] - previous[k] for k in previous}
            if any(v < 0 for v in delta.values()):
                raise ValueError(f"cumulative usage counter reset: {obs['id']}")
            if current != delta:
                corrections.append({"observation_id": obs["id"], "entity_id": entity,
                                    "raw": current, "per_call": delta})
            obs["usageDetails"] = {**delta, "total": sum(delta.values())}
            previous = current
    return corrections


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
    """已采集的原始 IO 载荷长度；截断片段按原样计数，未采集返回 None。"""
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


def _ancestors(observation, index):
    seen = {observation.get("id")}
    parent_id = observation.get("parentObservationId")
    while parent_id in index:
        if parent_id in seen:
            raise ValueError(f"cyclic trace parent chain: {parent_id}")
        seen.add(parent_id)
        parent = index[parent_id]
        yield parent
        parent_id = parent.get("parentObservationId")


def _is_descendant(observation, root_id, index):
    return any(parent.get("id") == root_id for parent in _ancestors(observation, index))


def _nearest_step(observation, index):
    if observation.get("name") == "advance_step":
        return observation
    return next((p for p in _ancestors(observation, index)
                 if p.get("name") == "advance_step"), None)


def _interval(observation):
    start, end = _epoch(observation.get("startTime")), _epoch(observation.get("endTime"))
    if start is None or end is None or end < start:
        raise ValueError(f"incomplete trace span: {observation.get('name')} ({observation.get('id')})")
    return start, end


def _union_seconds(intervals):
    end = float("-inf")
    total = 0.0
    for lo, hi in sorted(intervals):
        total += max(0.0, hi - max(lo, end))
        end = max(end, hi)
    return total


def _exclusive_seconds(removed, protected):
    """Measure removed time outside retained work, without double subtraction."""
    return max(0.0, _union_seconds(removed + protected) - _union_seconds(protected))


def _dispatch_decision(target, observations, index, phase=None):
    """Resolve one serial ReactAgent round: generation, then one target tool.

    FunctionCall executes its model before its tools and waits before the next
    round. Validate that structure across the whole round, not just proximity.
    Payload capture is optional; available payload must not contradict the link.
    """
    parent = target.get("parentObservationId")
    if index.get(parent, {}).get("name") != "ReactAgent":
        return None
    start = _interval(target)[0]
    siblings = [o for o in observations if o.get("parentObservationId") == parent
                and o["id"] != target["id"] and _interval(o)[0] < start]
    if not siblings:
        return None
    candidate = max(siblings, key=lambda o: _interval(o)[0])
    if (candidate.get("name") != "llm" or candidate.get("level") == "ERROR"
            or _interval(candidate)[1] > start):
        return None
    if (candidate.get("traceId") and target.get("traceId")
            and candidate["traceId"] != target["traceId"]):
        return None
    following = sorted((o for o in observations
                        if o.get("parentObservationId") == parent
                        and _interval(o)[0] > _interval(candidate)[0]), key=_interval)
    next_llm = next((o for o in following if o.get("name") == "llm"), None)
    round_tools = [o for o in following
                   if next_llm is None or _interval(o)[0] < _interval(next_llm)[0]]
    if [o["id"] for o in round_tools] != [target["id"]]:
        return None
    if next_llm is not None and _interval(next_llm)[0] < _interval(target)[1]:
        return None
    output = candidate.get("output")
    if output is None:
        return candidate
    if not isinstance(output, dict):
        return None
    calls = output.get("tool_calls") or []
    if len(calls) != 1 or not isinstance(calls[0], dict):
        return None
    function = calls[0].get("function") or {}
    if function.get("name") != target.get("name"):
        return None
    if phase is not None:
        args = function.get("arguments")
        try:
            args = json.loads(args) if isinstance(args, str) else args
        except ValueError:
            return None
        if not isinstance(args, dict) or args.get("step_ids") != [phase]:
            return None
    return candidate


def _resolve_attempt_boundaries(observations, index, groups):
    links = []
    unresolved = []

    def record_unresolved(target, phase):
        parent = target.get("parentObservationId")
        if index.get(parent, {}).get("name") != "ReactAgent":
            return
        preceding = [o for o in observations if o.get("parentObservationId") == parent
                     and o.get("name") == "llm"
                     and _interval(o)[0] < _interval(target)[0]]
        if preceding:
            previous = max(preceding, key=lambda o: _interval(o)[0])
            if previous.get("name") == "llm":
                if target.get("name") in WORKSPACE_TOOLS:
                    previous["_unresolved_retry_dispatch"] = True
                unresolved.append({"phase": phase, "target_id": target["id"],
                                   "observation_id": previous["id"],
                                   "reason": "ambiguous_or_conflicting_agent_round"})
    for group in groups.values():
        root = group["root"]
        decision = _dispatch_decision(root, observations, index, group["phase"])
        if decision is not None:
            # Retry filtering only: this does not assign the decision to a phase.
            decision["_dispatch_step_id"] = root["id"]
            group["dispatch"] = decision
            links.append({"observation_id": decision["id"], "target_id": root["id"],
                          "phase": group["phase"], "basis": "same_agent_serial_round",
                          "scope": "full_flow_only",
                          "payload_verified": decision.get("output") is not None})
        elif (len(group["workspaces"]) > 1 or not _step_succeeded(group, index)
              or any(g["phase"] == group["phase"] and _interval(g["root"])[0] > _interval(root)[0]
                     for g in groups.values())):
            # Only failed/prior attempts need this association for exclusion.
            record_unresolved(root, group["phase"])
        workspaces = group["workspaces"]
        retry_decision = (_dispatch_decision(workspaces[-1], observations, index)
                          if len(workspaces) > 1 else None)
        if retry_decision is not None:
            links.append({"observation_id": retry_decision["id"],
                          "target_id": workspaces[-1]["id"], "phase": group["phase"],
                          "basis": "same_agent_serial_retry_round",
                          "payload_verified": retry_decision.get("output") is not None})
        elif len(workspaces) > 1:
            record_unresolved(workspaces[-1], group["phase"])
        group["normal_start"] = (_interval(retry_decision or workspaces[-1])[0]
                                 if len(workspaces) > 1 else
                                 _interval(root)[0])
    for group in groups.values():
        members = [o for o in observations if _nearest_step(o, index) is group["root"]]
        group["interval"] = (_interval(group["root"])[0],
                             max(_interval(o)[1] for o in members))
        group["full_attempt_interval"] = (
            _interval(group.get("dispatch", group["root"]))[0], group["interval"][1])
    return links, unresolved


def _link_remote_steps(observations, index, workflow_session_id):
    """Associate detached workflow subagents; never modify the source trace.

    Remote execution starts a new trace, so no OTel parent edge exists. Only
    accept an explicitly identified workflow subagent wholly enclosed by one
    cross-trace advance_step. The inferred edges are exposed in the report.
    """
    links = []
    for workspace in observations:
        if workspace.get("name") not in WORKSPACE_TOOLS or _nearest_step(workspace, index):
            continue
        ancestors = list(_ancestors(workspace, index))
        root = ancestors[-1] if ancestors else workspace
        if root["id"] in {link["agent_id"] for link in links}:
            continue
        attrs = (root.get("metadata") or {}).get("attributes") or {}
        tags = attrs.get("lazyllm.trace.tags", attrs.get("langfuse.trace.tags", []))
        if isinstance(tags, str):
            try:
                tags = json.loads(tags)
            except ValueError:
                tags = []
        session_id = attrs.get("session.id") or attrs.get("gen_ai.conversation.id")
        if (root.get("name") != "ReactAgent" or root.get("parentObservationId")
                or not workflow_session_id or session_id != workflow_session_id
                or not isinstance(tags, list) or "subagent" not in tags
                or not root.get("traceId")):
            raise ValueError(f"unidentified detached Writer subagent: {workspace['id']}")
        lo, hi = _interval(root)
        candidates = [step for step in observations
                      if step.get("name") == "advance_step" and step.get("traceId")
                      and step["traceId"] != root["traceId"]
                      and _interval(step)[0] <= lo and hi <= _interval(step)[1]]
        if len(candidates) != 1:
            raise ValueError(f"detached Writer subagent {root['id']} has {len(candidates)} enclosing steps")
        step = candidates[0]
        root["parentObservationId"] = step["id"]
        links.append({"agent_id": root["id"], "step_id": step["id"],
                      "workflow_session_id": workflow_session_id,
                      "basis": "workflow_subagent_unique_enclosing_step"})
    return links


def _phase_attempts(observations, index):
    """Resolve current workspace calls to their actual enclosing step executions."""
    groups = {}
    for obs in observations:
        if obs.get("name") not in WORKSPACE_TOOLS:
            continue
        phase = next(p for p in PHASES if obs["name"] == _phase_tool(p))
        root = _nearest_step(obs, index)
        if root is None:
            raise ValueError(f"workspace has no advance_step ancestor: {obs.get('id')}")
        group = groups.setdefault(root["id"], {"root": root, "phase": phase, "workspaces": []})
        if group["phase"] != phase:
            raise ValueError(f"multiple Writer phases under advance_step {root['id']}")
        group["workspaces"].append(obs)
    if not groups:
        raise ValueError("no Writer workspace/advance_step parent chains in trace")
    for obs in observations:
        _interval(obs)
        root = _nearest_step(obs, index)
        if root is not None and root["id"] not in groups:
            raise ValueError(f"advance_step has no supported Writer workspace: {root['id']}")
    for group in groups.values():
        group["workspaces"].sort(key=lambda o: _interval(o)[0])
    return groups


def _tool_calls(observations):
    """Agent-invoked tools only; exclude engine, agent and internal helper spans."""
    index = {o["id"]: o for o in observations}
    return [o for o in observations
            if o.get("type") != "GENERATION"
            and o.get("name") not in ENGINE_SPANS | CONTAINER_SPANS
            and index.get(o.get("parentObservationId"), {}).get("name") == "ReactAgent"]


def _has_error(observation, index):
    return any(o.get("level") == "ERROR"
               for o in (observation, *_ancestors(observation, index)))

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
            if llm.get("id") == error.get("id") or _is_descendant(llm, error.get("id"), index)
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


def _tool_agg(wrappers, llms, index):
    total = 0.0
    for wrapper in wrappers:
        lo, hi = _interval(wrapper)
        intervals = []
        for llm in llms:
            if _is_descendant(llm, wrapper["id"], index):
                start, end = _interval(llm)
                intervals.append((max(lo, start), min(hi, end)))
        # Parallel model calls overlap: subtract their union, not their sum.
        total += max(0.0, hi - lo - _union_seconds(intervals))
    return len(wrappers), total

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


def _step_succeeded(group, index):
    last_succeeded = not _has_error(group["workspaces"][-1], index)
    outcome = group["root"].get("output")
    if isinstance(outcome, dict):
        statuses = [v.get("status") for v in outcome.get("attempt_results", [])
                    if isinstance(v, dict) and v.get("step_id") == group["phase"]]
        if statuses and statuses[-1] != "succeeded":
            last_succeeded = False
    return last_succeeded


def _phase_aggregate(observations, index, raw_llms, groups, phase):
    attempts = sorted((g for g in groups.values() if g["phase"] == phase),
                      key=lambda g: _interval(g["root"])[0])
    roots = {g["root"]["id"] for g in attempts}
    last = attempts[-1]
    workspaces = last["workspaces"]
    last_succeeded = _step_succeeded(last, index)
    full = [o for o in observations
            if (root := _nearest_step(o, index)) is not None and root["id"] in roots]
    last_start, last_end = last["interval"]
    normal_start = last["normal_start"]
    normal = []
    for obs in full:
        root = _nearest_step(obs, index)
        if not last_succeeded or root["id"] != last["root"]["id"] or _has_error(obs, index):
            continue
        if len(workspaces) > 1 and obs["id"] != last["root"]["id"]:
            if _interval(obs)[0] < normal_start:
                continue
        normal.append(obs)
    normal_ids = {o["id"] for o in normal}
    abnormal = [o for o in full if o["id"] not in normal_ids]
    full_wall = _union_seconds([g["interval"] for g in attempts])
    # Failed parallel children can outlive their wrapper; do not extend the
    # successful attempt to their completion or erase overlapping normal work.
    normal_end = max([_interval(last["root"])[1]] + [_interval(o)[1] for o in normal])
    error_intervals = [(max(normal_start, lo), min(last_end, hi))
                       for o in abnormal if _nearest_step(o, index)["id"] == last["root"]["id"]
                       and o.get("name") not in ENGINE_SPANS | CONTAINER_SPANS
                       for lo, hi in [_interval(o)] if hi > normal_start and lo < last_end]
    protected = [_interval(o) for o in normal
                 if o.get("name") not in ENGINE_SPANS | CONTAINER_SPANS
                 and not any(_is_descendant(bad, o["id"], index) for bad in abnormal)]
    error_intervals = [(max(normal_start, lo), min(normal_end, hi))
                       for lo, hi in error_intervals if hi > normal_start and lo < normal_end]
    normal_wall = max(0.0, normal_end - normal_start -
                      _exclusive_seconds(error_intervals, protected)) if last_succeeded else 0.0
    llm_ids = {o["id"] for o in raw_llms}
    tool_ids = {o["id"] for o in _tool_calls(observations)}

    def bucket(items, wall):
        llms = [o for o in items if o["id"] in llm_ids]
        agg = _agg_llms(llms)
        tc, tt = _tool_agg([o for o in items if o["id"] in tool_ids], raw_llms, index)
        return {
            "wall": round(wall, 3), "llm_count": agg["count"],
            "llm_latency": agg["latency"], "tool_count": tc, "tool_time": round(tt, 3),
            "input": agg["input"], "output": agg["output"],
            "input_chars": agg["input_chars"], "output_chars": agg["output_chars"],
            "chars_present": agg["chars_present"],
            "chapters": _draft_chapters(items, index, llms),
        }
    result = bucket(full, full_wall)
    result["abnormal"] = bucket(abnormal, max(0.0, full_wall - normal_wall))
    result["normal_attempt"] = sum(len(g["workspaces"]) for g in attempts)
    result["normal_succeeded"] = last_succeeded
    result["normal"] = bucket(normal, normal_wall) if last_succeeded else None
    result["normal_interval"] = [normal_start, normal_end] if last_succeeded else None
    return result


def _doc_stats_for(doc_stats):
    """从 document_stats JSON 提取 write_document 字数指标。"""
    result = {"gen_chars": None, "rev_chars": None, "draft_sections": None}
    if not doc_stats:
        return result
    final = doc_stats.get("final") or {}
    if final.get("visible_characters") is not None:
        result["gen_chars"] = int(final["visible_characters"])
    revision = doc_stats.get("revision") or {}
    if revision.get("present") and revision.get("changed_final_characters") is not None:
        result["rev_chars"] = int(revision["changed_final_characters"])
    if doc_stats.get("draft_sections") is not None:
        result["draft_sections"] = int(doc_stats["draft_sections"])
    return result


def extract(trace_data, doc_stats=None):
    errors = classification_errors(trace_data)
    if errors:
        raise ValueError("unclassified model calls: " + "; ".join(errors))
    observations = [dict(o) for o in trace_data.get("observations") or []]
    index = {o.get("id"): o for o in observations if o.get("id")}
    if len(index) != len(observations):
        raise ValueError("trace contains missing or duplicate observation IDs")
    metadata = trace_data.get("metadata") or {}
    semantics = metadata.get("llm_usage_semantics", "per_call")
    usage_corrections = _normalize_llm_usage(observations, semantics)
    context = metadata.get("run_context") or {}
    links = _link_remote_steps(observations, index, context.get("workflow_session_id"))
    groups = _phase_attempts(observations, index)
    decision_links, unresolved_dispatches = _resolve_attempt_boundaries(observations, index, groups)
    raw_llms = _llm_candidates(observations)
    for llm in raw_llms:
        usage = llm.get("usageDetails") or {}
        if llm.get("level") != "ERROR" and any(usage.get(k) is None for k in ("input", "output")):
            raise ValueError(f"LLM token usage missing: {llm['id']}")
    valid_llms = [o for o in raw_llms if not _has_error(o, index)]
    phases = {phase: _phase_aggregate(observations, index, raw_llms, groups, phase)
              for phase in PHASES if any(g["phase"] == phase for g in groups.values())}
    if "write_document" in phases:
        doc = _doc_stats_for(doc_stats)
        for bucket in (phases["write_document"], phases["write_document"].get("normal")):
            if bucket is not None:
                bucket.update(gen_chars=doc["gen_chars"], rev_chars=doc["rev_chars"])
                if doc["draft_sections"] is not None:
                    bucket["chapters"] = doc["draft_sections"]
    first_step = min(_interval(g["root"])[0] for g in groups.values())
    pre_workflow = _pre_workflow_bucket(observations, first_step, index, valid_llms)
    terminal_end = max(_interval(g["root"])[1] for g in groups.values())
    # Full link includes errors/retries; clean_full contains only successful final attempts.
    full_link = _agg_llms(raw_llms)
    clean_full = _clean_full_flow(observations, index, raw_llms, groups, phases)
    retained_llm_ids = set(clean_full.pop("retained_llm_ids"))
    return {
        "wall": max(_interval(o)[1] for o in observations) - min(_interval(o)[0] for o in observations),
        "full_link": full_link, "pre_workflow": pre_workflow,
        "clean_full": clean_full, "phases": phases,
        "usage_semantics": semantics, "usage_corrections": usage_corrections,
        "attribution": "workspace_step_tree" if links else "workspace_parent_tree",
        "step_links": links, "model_info": _model_info(valid_llms),
        "decision_links": decision_links,
        "unresolved_dispatches": unresolved_dispatches,
        "phase_semantics": "advance_step_execution_tree",
        "unassigned_llm_ids": [o["id"] for o in valid_llms
                               if _nearest_step(o, index) is None
                               and o["id"] in retained_llm_ids],
        "exceptions": _detect_exceptions(observations, index, raw_llms),
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


def _pre_workflow_bucket(observations, first_step, index, valid_llms):
    """Keep successful routing before the first step, without its enclosing agent span."""
    valid_ids = {o["id"] for o in valid_llms}
    tool_ids = {o["id"] for o in _tool_calls(observations)}
    items = [o for o in observations if not _has_error(o, index)
             and _nearest_step(o, index) is None
             and (o["id"] in valid_ids or o["id"] in tool_ids or o.get("name") in TRIGGER_NAMES)
             and _interval(o)[1] <= first_step]
    agg = _agg_llms([o for o in items if o["id"] in valid_ids])
    return {**agg, "wall": round(_span_wall([(_interval(o)[0], o) for o in items]), 3),
            "present": 1 if items else 0}


def _clean_full_flow(observations, index, raw_llms, groups, phases):
    """Keep normal calls throughout the workflow, including inter-step routing.

    Preserve the existing terminal-step boundary. Remove prior step attempts,
    the failed prefix of an in-step retry, and ERROR subtrees. An observation
    without a phase is not evidence of a failed attempt.
    """
    terminal_end = max(_interval(g["root"])[1] for g in groups.values())
    start = min(_interval(o)[0] for o in observations)
    last_steps = {}
    removed_intervals = []
    for phase in phases:
        attempts = sorted((g for g in groups.values() if g["phase"] == phase),
                          key=lambda g: _interval(g["root"])[0])
        last = attempts[-1]
        lo, hi = last["interval"]
        normal_start = last["normal_start"]
        last_steps[last["root"]["id"]] = normal_start
        removed_intervals.extend(g["full_attempt_interval"] for g in attempts[:-1])
        if len(last["workspaces"]) > 1:
            removed_intervals.append((last["full_attempt_interval"][0], normal_start))
        if not phases[phase].get("normal_succeeded"):
            removed_intervals.append(last["full_attempt_interval"])
    removed_intervals.extend(_interval(o) for o in observations if o.get("level") == "ERROR")

    def retained(obs):
        if _has_error(obs, index) or _interval(obs)[0] > terminal_end:
            return False
        if obs.get("_unresolved_retry_dispatch"):
            # Lack of dispatch IO is not evidence that this successful LLM
            # belongs to the failed prefix. Export is blocked until resolved.
            return True
        dispatch_step = obs.get("_dispatch_step_id")
        if dispatch_step is not None:
            group = groups[dispatch_step]
            return (dispatch_step in last_steps and len(group["workspaces"]) == 1
                    and phases[group["phase"]].get("normal_succeeded"))
        step = _nearest_step(obs, index)
        if step is None:
            return True
        normal_start = last_steps.get(step["id"])
        return (normal_start is not None
                and phases[groups[step["id"]]["phase"]].get("normal_succeeded")
                and _interval(obs)[0] >= normal_start)

    llms = [o for o in raw_llms if retained(o)]
    result = _agg_llms(llms)
    tools = [o for o in _tool_calls(observations) if retained(o)]
    # Include late descendants of rejected attempts, not just ERROR wrappers.
    removed_intervals.extend(_interval(o) for o in observations
                             if not retained(o) and _interval(o)[0] <= terminal_end
                             and o.get("name") not in ENGINE_SPANS | CONTAINER_SPANS)
    tool_count, tool_time = _tool_agg(tools, raw_llms, index)
    rejected = [o for o in observations if not retained(o)]
    protected = [_interval(o) for o in llms + tools
                 if not any(_is_descendant(bad, o["id"], index) for bad in rejected)]
    excluded_wall = _exclusive_seconds(
        [(max(start, lo), min(terminal_end, hi)) for lo, hi in removed_intervals
         if hi > start and lo < terminal_end], protected)
    result.update(retained_llm_ids=[o["id"] for o in llms],
                  tool_count=tool_count, tool_time=round(tool_time, 3),
                  wall=round(max(0.0, terminal_end - start - excluded_wall), 3), present=1)
    return result


def avg_traces(traces_data, scenario, *, doc_stats=None):
    """Aggregate traces with document metrics aligned by case index."""
    n = len(traces_data)
    if not n:
        return {"scenario": scenario, "n_traces": 0}
    if doc_stats is not None and len(doc_stats) != n:
        raise ValueError("document stats must align with every trace")
    per_trace = [extract(trace, doc_stats=doc_stats[i] if doc_stats else None)
                 for i, trace in enumerate(traces_data)]

    successful = [t for t in per_trace
                  if all(p["normal_succeeded"] for p in t["phases"].values())]

    def average_metric(key):
        samples = successful if key == "clean_full" else per_trace
        count = len(samples)
        row = {
            "input": r1(sum(t[key]["input"] for t in samples) / (count or 1)),
            "output": r1(sum(t[key]["output"] for t in samples) / (count or 1)),
            "latency": round(sum(t[key]["latency"] for t in samples) / (count or 1), 3),
            "count": round(sum(t[key]["count"] for t in samples) / (count or 1), 1),
            "present": count,
        }
        if "input_chars" in per_trace[0][key]:
            for field in ("input_chars", "output_chars"):
                values = [t[key][field] for t in samples]
                row[field] = r1(sum(values) / count) if count and all(v is not None for v in values) else None
            row["chars_present"] = sum(t[key]["chars_present"] for t in samples)
        if key == "clean_full":
            for field in ("tool_count", "tool_time"):
                row[field] = round(sum(t[key][field] for t in samples) / (count or 1), 3)
        return row

    def average_phase(phase):
        rows = [t["phases"].get(phase) for t in per_trace if phase in t["phases"]]
        if not rows:
            return None
        out = _blank_phase()
        for key in ("wall", "llm_count", "llm_latency", "tool_count", "tool_time",
                    "input", "output", "input_chars", "output_chars", "chapters"):
            out[key] = round(sum(row.get(key, 0) or 0 for row in rows) / n, 3)
        for field in ("input_chars", "output_chars"):
            if any(row.get(field) is None for row in rows):
                out[field] = None
        out["chars_present"] = sum(row.get("chars_present", 0) for row in rows)
        out["present"] = len(rows)
        gen_rows = [row.get("gen_chars") for row in rows if row.get("gen_chars") is not None]
        rev_rows = [row.get("rev_chars") for row in rows if row.get("rev_chars") is not None]
        out["gen_chars"] = r1(sum(gen_rows) / len(gen_rows)) if gen_rows else None
        out["rev_chars"] = r1(sum(rev_rows) / len(rev_rows)) if rev_rows else None

        def sub_bucket(key):
            candidates = ([t["phases"][phase] for t in successful if phase in t["phases"]]
                          if key == "normal" else rows)
            sub_rows = [row.get(key) for row in candidates if row.get(key)]
            if not sub_rows:
                return None
            sub = _blank_phase()
            for k in ("wall", "llm_count", "llm_latency", "tool_count", "tool_time",
                      "input", "output", "input_chars", "output_chars", "chapters"):
                sub[k] = round(
                    sum((r.get(k) or 0) for r in sub_rows) / len(sub_rows), 3,
                )
            for field in ("input_chars", "output_chars"):
                if any(r.get(field) is None for r in sub_rows):
                    sub[field] = None
            sub["chars_present"] = sum(r.get("chars_present", 0) for r in sub_rows)
            sub["present"] = len(sub_rows)
            gen = [r.get("gen_chars") for r in sub_rows if r.get("gen_chars") is not None]
            rev = [r.get("rev_chars") for r in sub_rows if r.get("rev_chars") is not None]
            sub["gen_chars"] = r1(sum(gen) / len(gen)) if gen else None
            sub["rev_chars"] = r1(sum(rev) / len(rev)) if rev else None
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
            out["normal_present"] = normal["present"]
        out["normal_succeeded"] = normal is not None
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
        sum(t["clean_full"]["wall"] for t in successful) / (len(successful) or 1), 3,
    )
    attribution = Counter(t["attribution"] for t in per_trace).most_common(1)[0][0]
    suppliers = sorted({s for t in per_trace for s in t["model_info"]["suppliers"]})
    request_models = sorted({
        m for t in per_trace for m in t["model_info"]["request_model"]
    })
    return {
        "scenario": scenario,
        "n_traces": n,
        "n_successful_traces": len(successful),
        "failed_samples": [{"case_index": i, "phases": [p for p, row in t["phases"].items()
                                                        if not row["normal_succeeded"]]}
                           for i, t in enumerate(per_trace, 1) if t not in successful],
        "full_link": full_link,
        "clean_full": clean_full,
        "phases": phases,
        "exceptions": exception_cases,
        "abnormal_cases": abnormal_cases,
        "attribution": attribution,
        "phase_semantics": "advance_step_execution_tree",
        "decision_links": [{"case_index": i, **item}
                           for i, t in enumerate(per_trace, 1) for item in t["decision_links"]],
        "unresolved_dispatches": [{"case_index": i, **item}
                                  for i, t in enumerate(per_trace, 1)
                                  if t in successful for item in t["unresolved_dispatches"]],
        "unassigned_llm_ids": [{"case_index": i, "observation_id": oid}
                               for i, t in enumerate(per_trace, 1) for oid in t["unassigned_llm_ids"]],
        "step_links": [{"case_index": i, **link}
                       for i, t in enumerate(per_trace, 1) for link in t["step_links"]],
        "usage_semantics": sorted({t["usage_semantics"] for t in per_trace}),
        "usage_corrections": [{"case_index": i, **item}
                              for i, t in enumerate(per_trace, 1) for item in t["usage_corrections"]],
        "model_info": {"suppliers": suppliers, "request_model": request_models},
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
        ("Agent 工具调用次数", _fmt((full_link or {}).get("tool_count")),
         *(f"{_fmt(getp(p).get('tool_count'))}" if getp(p) else "—" for p in PHASES)),
        ("工具实际执行时间", _fmt((full_link or {}).get("tool_time"), "s"),
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
        f"> 业务口径：{stats.get('phase_semantics', 'unspecified')}；"
        f"未确认的发起决策：{len(stats.get('unresolved_dispatches') or [])}。",
        f"> 样本：完整过程 {n}；正常链路 {stats.get('n_successful_traces', 0)}。",
        "> 字符数按 trace IO 载荷计；载荷缺失或截断记为 —，不记 0，也不影响其他指标。",
        "",
    ]
    if stats.get("unresolved_dispatches"):
        lines += ["> 阶段调用归属不完整：以下为已确认部分，禁止导出正式阶段数据。"
                  "正常未归属调用仍保留在全流程；详见 stats.json 的 unresolved_dispatches。", ""]
    lines += _render_metric_table(
        phases, stats.get("clean_full") if stats.get("n_successful_traces") else None, "normal",
        "正常链路统计（导出使用）",
        "仅聚合成功样本，保留最后成功尝试及其发起决策。全流程还包含其他正常协调调用；"
        "阶段之和不要求等于全流程。完整过程和排除的异常另列如下。",
    )
    lines += _render_metric_table(
        phases, full, None,
        "一、完整过程统计主表（真实数据）",
        "全链路包含所有尝试、路由、阶段间决策和尾迹；阶段仅统计 advance_step 完整执行子树，包含内部 Agent 决策和重试；步骤外发起决策只计全流程。"
        "LLM 耗时为调用累计，阶段墙钟为业务尝试区间并集。工具实际执行时间为"
        "工具 span 墙钟扣除其中 LLM 时间区间并集。",
    )
    lines += _render_metric_table(
        phases, None, "abnormal",
        "二、异常/尾迹时间统计（同维度）",
        "阶段异常包含先前尝试及其发起决策和错误；未能关联阶段的正常调用保留在全流程。"
        "正常链路（每个阶段/工具调用的"
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


def _full_flow_row(stats, batch_id, scenario, phases):
    """构造“全流程”阶段行（键 `批次ID|场景|全流程`）。

    口径：墙钟 / LLM / token 取 clean_full，保留触发前和阶段之间的正常调用，
    剔除阶段先前尝试、失败前缀与 ERROR 子树；工具统计同样保留正常调用。
    章节数与生成/修改字数取
    write_document 阶段；修订场景
    （有 rev_chars）无 draft 章节数，章节数输出 “—”。
    """
    fl = stats.get("clean_full") or {}
    tool_count = round(float(fl.get("tool_count") or 0), 1)
    tool_time = round(float(fl.get("tool_time") or 0), 3)
    wd = phases.get("write_document") or {}
    wd_n = wd.get("normal") or {}
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
        int(fl.get("input") or 0), (int(fl["input_chars"]) if fl.get("input_chars") is not None else "—"),
        int(fl.get("output") or 0), (int(fl["output_chars"]) if fl.get("output_chars") is not None else "—"),
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
    if stats.get("unresolved_dispatches"):
        raise ValueError("incomplete phase attribution: unresolved dispatch decisions; "
                         "inspect unresolved_dispatches before exporting")
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
        n = row["normal"]
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
            int(n.get("input") or 0), (int(n["input_chars"]) if n.get("input_chars") is not None else "—"),
            int(n.get("output") or 0), (int(n["output_chars"]) if n.get("output_chars") is not None else "—"),
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
            reasons.append("资源分析及准备步骤内的 Agent 决策集中在准备阶段")
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
                notes.append("准备步骤内的决策与资源/上下文读取通常是主要来源")
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
                        help="document_stats.json，与输入 trace 顺序一一对应")
    args = parser.parse_args()
    traces = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.traces]
    for trace, path in zip(traces, args.traces):
        trace.setdefault("source", path)
    docs = [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.doc_stats] if args.doc_stats else None
    stats = avg_traces(traces, args.scenario, doc_stats=docs)
    if args.table:
        output = render_table(stats)
        if args.analysis:
            output += "\n" + render_analysis(stats)
        print(output, end="")
    else:
        print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
