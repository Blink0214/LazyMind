"""Validate remote LLM payloads without changing observations or exposing bodies."""
import hashlib
import json
import math


def model_calls(trace):
    for obs in trace.get("observations") or []:
        attrs = (obs.get("metadata") or {}).get("attributes") or {}
        kind = str(attrs.get("lazyllm.entity.config.type") or "").lower()
        if (obs.get("type") == "GENERATION" or obs.get("name") == "llm"
                or attrs.get("lazyllm.semantic_type") == "llm"
                or kind in {"llm", "chat", "vlm"}):
            yield obs


def classification_errors(trace):
    return [f"{o.get('id')}: model call is not GENERATION/name=llm"
            for o in model_calls(trace)
            if o.get("type") != "GENERATION" or o.get("name") != "llm"]


def validate_diagnostics(trace):
    errors = classification_errors(trace)
    calls = list(model_calls(trace))
    diagnosed = 0
    attempts = 0

    def payload(value, label):
        if not isinstance(value, dict) or not isinstance(value.get("json"), str):
            errors.append(f"{label}: missing payload")
            return
        body = value["json"]
        if (value.get("truncated") is not False
                or value.get("original_chars") != len(body)
                or value.get("captured_chars") != len(body)
                or value.get("sha256") != hashlib.sha256(body.encode()).hexdigest()):
            errors.append(f"{label}: payload integrity mismatch")
        try:
            json.loads(body)
        except ValueError:
            errors.append(f"{label}: invalid payload JSON")

    def timing(value, keys, label):
        for key in keys:
            v = value.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0:
                errors.append(f"{label}: missing/invalid {key}")

    if not calls:
        errors.append("no model calls")
    for obs in calls:
        oid = str(obs.get("id"))
        attrs = (obs.get("metadata") or {}).get("attributes") or {}
        diag = attrs.get("lazyllm.diagnostics.llm")
        if isinstance(diag, str):
            try:
                diag = json.loads(diag)
            except ValueError:
                diag = None
        if not isinstance(diag, dict):
            errors.append(f"{oid}: missing/invalid diagnostics")
            continue
        diagnosed += 1
        timing(diag, ["request_preparation_ms"], oid)
        http = diag.get("attempts")
        if not isinstance(http, list) or not http:
            errors.append(f"{oid}: missing HTTP attempts")
            http = []
        for i, attempt in enumerate(http):
            label = f"{oid}.attempt[{i}]"
            if not isinstance(attempt, dict):
                errors.append(f"{label}: invalid attempt")
                continue
            attempts += 1
            payload(attempt.get("request"), label)
            keys = ["capture_ms", "started_offset_ms", "duration_ms"]
            if not attempt.get("error_type"):
                keys.append("headers_ms")
                if attempt.get("streaming"):
                    keys.append("first_body_chunk_ms")
            timing(attempt, keys, label)
            # Semantic chunk timing can be null for unsupported parsers/non-streaming.
            if attempt.get("first_semantic_chunk_ms") is not None:
                timing(attempt, ["first_semantic_chunk_ms"], label)
        if obs.get("level") != "ERROR":
            payload(diag.get("output"), oid + ".output")
    return {"status": "BLOCKED" if errors else "PASS", "model_calls": len(calls),
            "diagnosed_calls": diagnosed, "http_attempts": attempts, "errors": errors}
