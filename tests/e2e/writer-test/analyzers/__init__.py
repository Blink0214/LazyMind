"""Analyzer modules that turn Writer trace JSON + case assertions into verdicts.

Each module in this package consumes a Langfuse-style trace (or a local OTel
export that ``shared.observability`` normalizes to the same shape) plus an optional
assertion dict (the contents of a ``case_N.yaml`` sidecar) and produces a
PASS/FAIL report.

The split between modules mirrors the runtime modes:

* ``analyze_common``  — trace → route / tools / step / slots / provider.
                       Used by BOTH perf and func modes.
* ``analyze_perf``    — trace → mutual-exclusive token / latency buckets.
                       Used by perf mode.
* ``analyze_func``    — trace + assertions + SSE stream → PASS/FAIL checklist.
                       Used by func mode.
"""
