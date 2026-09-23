"""Analyzer modules that turn Writer trace JSON + case assertions into verdicts.

Each module in this package consumes a Langfuse-style trace (or a local OTel
export that ``shared.observability`` normalizes to the same shape) plus an optional
assertion dict (the contents of a ``case_N.yaml`` sidecar) and produces a
PASS/FAIL report.

The split between modules mirrors the runtime modes:

* ``analyze_common``  — trace → tools / workspace / write-back / model facts.
                       Used by BOTH perf and func modes; session-owned facts
                       are merged by the functional runner.
* ``analyze_perf``    — trace → mutual-exclusive token / latency buckets.
                       Used by perf mode.
* ``analyze_func``    — trace + assertions + SSE stream → PASS/FAIL checklist.
                       Used by func mode.
"""
