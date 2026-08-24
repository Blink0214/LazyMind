#!/usr/bin/env bash
# Writer 测试 orchestrator（perf / func / both）
#
# 两套用例：
#   perf：cases/writer_perf_cases.yaml 的 P01..P05（每场景 5 个 case），
#         API-only + 三阶段性能统计；
#   func：cases/writer_func_cases.yaml 的 C01..C07 / I01..I02（默认 Playwright
#         UI 或 --no-ui），机械检查 + LLM 判定。
#   both：先 perf 场景，再功能场景。
#
# 用法:
#   bash tests/e2e/writer-test/scripts/full_run.sh            # perf P01..P05
#   bash tests/e2e/writer-test/scripts/full_run.sh P02 --cases 1,2
#   bash tests/e2e/writer-test/scripts/full_run.sh --mode func C03
#
# 输出:
#   reports/func/<时间戳>/<场景>/     功能测试
#   reports/perf/<时间戳>/<场景>/     性能测试（both 模式两套共用同一时间戳）
#     ├── case_<N>/run_N.json          单 case 摘要
#     ├── case_<N>/trace.json         Langfuse/local trace（若 fetch 成功）
#     ├── case_<N>/final.md           落地后的最终 markdown
#     ├── traces/case_N.json          trace 拷贝
#     └── stats.md / stats.json       场景级聚合（仅 perf）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TEST_DIR="$(dirname "$SCRIPT_DIR")"
CASES_DIR="${CASES_DIR:-$TEST_DIR/cases}"
REPORTS_DIR="${TEST_DIR}/reports"
BASE_URL="${BASE_URL:-http://localhost:8090}"
VENV_PY="${VENV_PY:-}"
CASES=""
TRACE_MODE="auto"
COMPUTE_STATS=true
RECIPE="text_intent"
MODE="perf"   # perf | func | both
TIMEOUT_S="${TIMEOUT_S:-600}"   # 单用例上限（秒），与 SKILL 的 10 分钟一致
SCENARIOS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cases) CASES="$2"; shift 2 ;;
        --base-url) BASE_URL="$2"; shift 2 ;;
        --cases-root) CASES_DIR="$2"; shift 2 ;;
        --no-trace) TRACE_MODE="none"; shift ;;
        --no-stats) COMPUTE_STATS=false; shift ;;
        --recipe) RECIPE="$2"; shift 2 ;;
        --mode) MODE="$2"; shift 2 ;;
        --venv) VENV_PY="$2"; shift 2 ;;
        --*) echo "未知参数: $1" >&2; exit 2 ;;
        *) SCENARIOS+=("$1"); shift ;;
    esac
done

if [[ -z "$VENV_PY" ]]; then
    if [[ -x "/Users/chensiyu2/Code/LazyMind/.venv/bin/python" ]]; then
        VENV_PY="/Users/chensiyu2/Code/LazyMind/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        VENV_PY="$(command -v python3)"
    fi
fi

# 按模式选择默认场景集；命令行显式传入的场景优先保留。
if [[ ${#SCENARIOS[@]} -eq 0 ]]; then
    if [[ "$MODE" == "func" ]]; then
        SCENARIOS=("C01" "C02" "C03" "C04" "C05" "C06" "C07"
                   "I01" "I02" "X01" "X02" "X03")
    elif [[ "$MODE" == "both" ]]; then
        SCENARIOS=("P01" "P02" "P03" "P04" "P05"
                   "C01" "C02" "C03" "C04" "C05" "C06" "C07"
                   "I01" "I02" "X01" "X02" "X03")
    else
        SCENARIOS=("P01" "P02" "P03" "P04" "P05")
    fi
fi

mkdir -p "$REPORTS_DIR/func" "$REPORTS_DIR/perf"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
echo "=== 运行时间戳: $TIMESTAMP ==="
echo "=== 输出目录: $REPORTS_DIR/func/$TIMESTAMP, $REPORTS_DIR/perf/$TIMESTAMP ==="
echo "=== cases root: $CASES_DIR ==="
echo "=== mode: $MODE ==="

scenario_sub() {  # $1=scenario id -> func | perf
    if [[ "$MODE" == "func" ]]; then
        echo "func"
    elif [[ "$MODE" == "perf" ]]; then
        echo "perf"
    elif [[ "$1" =~ ^P[0-9]{2}$ ]]; then
        echo "perf"
    else
        echo "func"
    fi
}

run_runner() {  # $1=runner script, rest=args
    local runner="$1"; shift
    if "$VENV_PY" "$runner" "$@" > "$CASE_OUT/run_N.stdout" 2> "$CASE_OUT/run_N.stderr"; then
        rc=0
    else
        rc=$?
    fi
    mv "$CASE_OUT/run_N.stdout" "$CASE_OUT/run_N.json" 2>/dev/null || true
    if [[ -s "$CASE_OUT/run_N.json" ]]; then
        ELAPSED=$(grep -o '"elapsed_s": [0-9.]*' "$CASE_OUT/run_N.json" | head -1 | cut -d' ' -f2 || echo "n/a")
        STATUS=$(grep -o '"writer_status": "[^"]*"' "$CASE_OUT/run_N.json" | head -1 | cut -d'"' -f4 || echo "n/a")
        echo "[$SCENARIO case ${CASE_NUM:-1}] rc=$rc elapsed=${ELAPSED}s status=${STATUS}"
    else
        echo "[$SCENARIO case ${CASE_NUM:-1}] rc=$rc 无 run_N.json（runner 失败）" >&2
    fi
    if [[ -f "$CASE_OUT/trace.json" ]]; then
        cp "$CASE_OUT/trace.json" "$OUT_DIR/traces/case_${CASE_NUM:-1}.json"
    fi
}

aggregate_stats() {  # $1=scenario id
    local scenario="$1"
    local trace_files=()
    for f in "$OUT_DIR"/traces/case_*.json; do
        [[ -f "$f" ]] && trace_files+=("$f")
    done
    if [[ ${#trace_files[@]} -eq 0 ]]; then
        return
    fi
    TRACE_JSON=$(printf '%s\n' "${trace_files[@]}" | python3 -c "import sys, json; print(json.dumps([l.strip() for l in sys.stdin if l.strip()]))")
    WRITER_TRACE_PATHS="$TRACE_JSON" "$VENV_PY" -c "
import os, sys, json
sys.path.insert(0, '$TEST_DIR/analyzers')
from analyze_perf import avg_traces, render_table
paths = json.loads(os.environ['WRITER_TRACE_PATHS'])
traces = [json.loads(open(p, encoding='utf-8').read()) for p in paths]
print('traces_loaded:', len(traces))
doc_stats = []
for p in paths:
    m = __import__('re').search(r'case_(\d+)\.json$', p)
    f = os.path.join('$OUT_DIR', 'case_%s' % (m.group(1) if m else ''), 'document_stats.json')
    doc_stats.append(json.load(open(f, encoding='utf-8')) if os.path.isfile(f) else None)
stats = avg_traces(traces, scenario='$scenario', doc_stats=doc_stats)
json.dump(stats, open('$OUT_DIR/stats.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
open('$OUT_DIR/stats.md', 'w', encoding='utf-8').write(render_table(stats))
" || true
    if [[ -f "$OUT_DIR/stats.md" ]]; then
        echo "[stats] $OUT_DIR/stats.md"
    fi
}

for SCENARIO in "${SCENARIOS[@]}"; do
    RUN_DIR="$REPORTS_DIR/$(scenario_sub "$SCENARIO")/$TIMESTAMP"
    mkdir -p "$RUN_DIR"
    OUT_DIR="$RUN_DIR/$SCENARIO"
    mkdir -p "$OUT_DIR/traces"
    echo ""
    echo "=== 场景: $SCENARIO ==="

    if [[ -d "$CASES_DIR/$SCENARIO" ]]; then
        # 兼容旧目录布局（性能案例）。
        SCENARIO_DIR="$CASES_DIR/$SCENARIO"
        RUNNER_SCRIPT="$TEST_DIR/runners/perf_run.py"
        [[ "$MODE" == "func" ]] && RUNNER_SCRIPT="$TEST_DIR/runners/func_run.py"
        PROMPTS=()
        if [[ -n "$CASES" ]]; then
            for CASE_NUM in ${CASES//,/ }; do
                [[ -f "$SCENARIO_DIR/prompt_${CASE_NUM}.md" ]] && PROMPTS+=("$CASE_NUM")
            done
        else
            for prompt_file in "$SCENARIO_DIR"/prompt_*.md; do
                [[ -f "$prompt_file" ]] || continue
                n=$(basename "$prompt_file" .md | sed 's/^prompt_//')
                PROMPTS+=("$n")
            done
        fi
        for CASE_NUM in "${PROMPTS[@]}"; do
            prompt_path="$SCENARIO_DIR/prompt_${CASE_NUM}.md"
            [[ -s "$prompt_path" ]] || { echo "[Case $CASE_NUM] 跳过（提示词空）"; continue; }
            CASE_OUT="$OUT_DIR/case_${CASE_NUM}"
            mkdir -p "$CASE_OUT"
            RUNNER_ARGS=(--cases-root "$CASES_DIR" --scenario "$SCENARIO" --case "$CASE_NUM"
                         --base-url "$BASE_URL" --output-dir "$CASE_OUT" --recipe "$RECIPE"
                         --timeout "$TIMEOUT_S")
            [[ "$TRACE_MODE" == "none" ]] && RUNNER_ARGS+=(--no-trace)
            if [[ "$MODE" == "func" ]]; then
                $COMPUTE_STATS || RUNNER_ARGS+=(--no-checks)
            else
                $COMPUTE_STATS || RUNNER_ARGS+=(--no-stats)
            fi
            run_runner "$RUNNER_SCRIPT" "${RUNNER_ARGS[@]}"
        done
        [[ "$MODE" != "func" ]] && aggregate_stats "$SCENARIO"
    elif [[ "$SCENARIO" =~ ^P[0-9]{2}$ ]]; then
        # 性能场景（writer_perf_cases.yaml）：P01..P05，每场景 5 个 case。
        CASE_NUMS=()
        if [[ -n "$CASES" ]]; then
            for CASE_NUM in ${CASES//,/ }; do CASE_NUMS+=("$CASE_NUM"); done
        else
            CASE_NUMS=(1 2 3 4 5)
        fi
        for CASE_NUM in "${CASE_NUMS[@]}"; do
            CASE_OUT="$OUT_DIR/case_${CASE_NUM}"
            mkdir -p "$CASE_OUT"
            RUNNER_ARGS=(--cases-root "$CASES_DIR" --scenario "$SCENARIO" --case "$CASE_NUM"
                         --base-url "$BASE_URL" --output-dir "$CASE_OUT" --recipe "$RECIPE"
                         --timeout "$TIMEOUT_S")
            [[ "$TRACE_MODE" == "none" ]] && RUNNER_ARGS+=(--no-trace)
            $COMPUTE_STATS || RUNNER_ARGS+=(--no-stats)
            run_runner "$TEST_DIR/runners/perf_run.py" "${RUNNER_ARGS[@]}"
        done
        aggregate_stats "$SCENARIO"
    else
        # 功能场景（writer_func_cases.yaml C0X/I0X）。
        CASE_NUM=1
        CASE_OUT="$OUT_DIR/case_1"
        mkdir -p "$CASE_OUT"
        RUNNER_ARGS=(--cases-root "$CASES_DIR" --scenario "$SCENARIO"
                     --base-url "$BASE_URL" --output-dir "$CASE_OUT" --recipe "$RECIPE"
                     --timeout "$TIMEOUT_S")
        [[ "$TRACE_MODE" == "none" ]] && RUNNER_ARGS+=(--no-trace)
        $COMPUTE_STATS || RUNNER_ARGS+=(--no-checks)
        run_runner "$TEST_DIR/runners/func_run.py" "${RUNNER_ARGS[@]}"
    fi
done

echo ""
echo "运行完成；报告位于 $RUN_DIR"
