#!/bin/bash
# 统一测试运行器 —— 2026-11-08 加，2026-11-17 加并行执行。
#
# ## 它解决什么问题
#
# 原先我是临时的 for 循环：
#
#     for t in tests/*.py; do
#       MW_DIR=$(mktemp -d) python3 "$t"; rc=$?
#       [ $rc -ne 0 ] && echo "❌ $t"
#     done
#
# 两个真实的坑：
#
# 1. **退出码 0 不等于通过**。`tests/test_agent_summary_market_terms.py`
#    在系统解释器下会打印「请用 agent/.venv/bin/python 运行本文件」然后
#    **exit 0** —— 上面的循环把它显示成 ✅。也就是说「24 个全过」里
#    有一个根本没跑。
# 2. **输出可能读错**。多个后台任务并行时，我对着 tail 的几行下结论，
#    而那段输出未必属于我刚启动的那次。
#
# 修法：判据改成「rc==0 **且** 输出里出现 PASS/SKIP 标记」，并且
# 逐文件打印，最后给明确的计数。SKIP 单独计，不算通过。
#
# ## 并行执行（2026-11-17）
#
# 每个测试用独立的 `MW_DIR` 临时目录、独立子进程，状态互不干扰，
# 所以可以并发跑。并发度**动态决定**（2026-11-17）：
#
#     bash tests/run_all.sh                 # 自动：按机器负载与实测吞吐
#     MW_TEST_JOBS=4 bash tests/run_all.sh  # 显式指定（环境变量永远优先）
#     MW_TEST_JOBS=1 bash tests/run_all.sh  # 退回串行（排查竞态时用）
#
# ⚠️ 为什么不再写死 4 路：这台机器的常态是 load 60~100、CPU 只剩 2~5% 空闲
# （一台常驻虚拟机吃掉 400%+ CPU）。写死 4 路在空闲时太保守、在拥挤时又不
# 肯退让。详见 `src/concurrency.py` 的模块 docstring。
#
# ⚠️ 关键：**输出必须归属到各自的测试文件**。做法是每个测试把 stdout 写进
# 自己的临时文件（`$_TMPDIR/<name>.out` + `.rc`），全部跑完后再逐个读取判定 ——
# 绝不能让多个后台进程往同一个 stdout 写（那正是坑 2 的翻版）。
#
# 需要一个**干净的** MW_DIR / 进程环境；本脚本给每个测试单独建沙箱。
set -u

cd "$(dirname "$0")/.." || exit 1

# ⚠⚠ 2026-11-18（CI 实测发现）：**不能硬编码解释器路径**。
#
# 原写死 `_SYS_PY=/usr/local/bin/python3` —— 那是 macOS Homebrew 的位置。
# 在 Linux（GitHub Actions runner）上它**不存在**，于是每个测试都被判成
# 「解释器缺失」而跳过。`[实测]` 第一次 CI 跑出来是：
#
#     通过 0 / 跳过 48 / 失败 0     ← 48 个测试一个都没执行，却显示"成功"
#
# 这比"跑失败"更糟：它给了一个**全绿但什么都没验**的结论。正是本文件
# 文件头"坑 1"批评的那种元层面失败，只是这次藏在解释器选择里。
#
# 现在按优先级探测，保留 `/usr/local/bin/python3` 优先（macOS 上用户装的
# 3.14 比系统的 3.9 新，本项目要 3.11+），找不到再退回 PATH 里的 `python3`。
# 探测失败时**显式报错退出**，而不是让所有测试静默跳过。
_SYS_PY=""
for _cand in /usr/local/bin/python3 /opt/homebrew/bin/python3 python3 python; do
    # ⚠️ 必须取**绝对路径**：下面 `_run_one` 用 `[ ! -x "$_py" ]` 判断可用性，
    # 而 `command -v python3` 只返回 `python3`（PATH 名），`-x python3` 在当前
    # 目录下找会失败 → 又变成"解释器缺失"。`[实测]` 第一版就是这么漏的：
    # 改了探测逻辑，CI 里仍然 48 个全跳过。
    _abs=$(command -v "$_cand" 2>/dev/null) || continue
    case "$_abs" in /*) ;; *) _abs=$(cd "$(dirname "$_abs")" && pwd)/$(basename "$_abs") ;; esac
    if "$_abs" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
        _SYS_PY=$_abs; break
    fi
done
if [ -z "$_SYS_PY" ]; then
    echo "❌ 找不到 Python 3.11+（试过 /usr/local/bin、/opt/homebrew/bin、PATH）" >&2
    echo "   本项目需要 3.11+（使用了 typing.NotRequired）。" >&2
    exit 3
fi
_VENV_PY=agent/.venv/bin/python
# 动态并发：环境变量优先，否则按「有效核数 - 当前负载」给初值。
# `concurrency.py` 读不到负载时返回下限（不冒进）。
_JOBS=$(cd "$(dirname "$0")/.." && "$_SYS_PY" -c '
import sys; sys.path.insert(0, "src")
from concurrency import resolve_workers
print(resolve_workers("MW_TEST_JOBS", min_workers=2, max_workers=12))
' 2>/dev/null || echo 4)
case "$_JOBS" in ''|*[!0-9]*) _JOBS=4 ;; esac
[ "$_JOBS" -lt 1 ] && _JOBS=1

# 每个测试的临时产物集中在一个总目录，便于统一清理与归属
_TMPDIR=$(mktemp -d "${TMPDIR:-/tmp}/mw_runall_XXXXXX") || exit 1
trap 'rm -rf "$_TMPDIR"' EXIT

# 启动一个测试：写 <name>.out 与 <name>.rc，返回即表示已派发
_run_one() {
    _t=$1; _base=$(basename "$_t")
    case "$_base" in
        test_agent_summary_*) _py=$_VENV_PY ;;
        *)                    _py=$_SYS_PY ;;
    esac
    if [ ! -x "$_py" ]; then
        # 解释器缺失是**环境缺失**，不是失败。具体场景：
        # test_agent_summary_* 需要 agent/.venv，而 agent/ 是独立仓库、
        # 已在本仓库 .gitignore 里 —— 别人克隆后必然没有。
        printf 'SKIPNOENV' > "$_TMPDIR/$_base.rc"
        return
    fi
    _d=$(mktemp -d "$_TMPDIR/sandbox_XXXXXX") || exit 1
    (
        MW_DIR="$_d" PYTHONDONTWRITEBYTECODE=1 "$_py" -B "$_t" > "$_TMPDIR/$_base.out" 2>&1
        printf '%s' "$?" > "$_TMPDIR/$_base.rc"
    ) &
}

# 派发全部测试：**滚动补位**，而不是按批等待。
#
# ⚠️ 2026-11-17（调度改进）：原先"每 N 个一批、等整批完成再派下一批"有个
# 真实的浪费 —— 批里跑完的槽位要等**最慢那个**跑完才能补新任务。
# `[实测]` 六个最慢的文件（13s/10s/9s/8s/4.6s/4.5s）恰好被排进同几批，
# 仅它们就贡献了约 40s 的关键路径，并行几乎白搭。
#
# 现在改成：始终维持 N 个在跑，任一跑完立刻补下一个。
# ⚠️ 不用 `wait -n` —— macOS 自带 bash 3.2 不支持（`wait: -n: invalid
# option`，实测会静默退化成等全部）。改用「轮询 + `jobs -pr` 数活跃数」，
# 3.2 可用；`sleep 0.05` 的轮询开销相对每个测试几百毫秒可忽略。
_QUEUE=$(ls tests/*.py)
_n_total=$(printf '%s\n' "$_QUEUE" | wc -l | tr -d ' ')
_n_dispatched=0
_n_done_prev=0
_t_round=$(date +%s)
_JOBS_START=$_JOBS

# ---------- 运行时自适应（2026-11-17） ----------
#
# 静态公式只能猜一个起点，真正的依据是**这一轮跑得多快**。
# 每 3 秒看一次完成数：
#   · 完成速度够快（≥ 1 个/秒·每 worker 的 1/4）→ 加一路
#   · 明显停滞（连续两轮零完成）               → 减一路
# 加减都只 ±1，避免在边界来回震荡。
#
# ⚠️ 只在**没有**显式设 `MW_TEST_JOBS` 时自适应 —— 使用者写了数字
# 就是要那个数字（排查竞态时常见），不能被"机器看起来很闲"改掉。
_ADAPT=1
[ -n "${MW_TEST_JOBS:-}" ] && _ADAPT=0
_stall=0

_adapt_tick() {
    [ "$_ADAPT" -eq 1 ] || return 0
    _now=$(date +%s)
    # 本轮完成数 = 当前派发数 - 仍在跑的数
    _running=$(jobs -pr | wc -l | tr -d ' ')
    _done=$((_n_dispatched - _running))
    _delta=$((_done - _n_done_prev))
    _el=$((_now - _t_round))
    [ "$_el" -lt 3 ] && return 0
    if [ "$_delta" -le 0 ]; then
        _stall=$((_stall + 1))
        if [ "$_stall" -ge 2 ] && [ "$_JOBS" -gt 2 ]; then
            _JOBS=$((_JOBS - 1)); _stall=0
        fi
    else
        _stall=0
        # 每 3 秒完成 ≥1 个，且当前不到上限 → 加一路
        if [ "$_JOBS" -lt 12 ]; then _JOBS=$((_JOBS + 1)); fi
    fi
    _n_done_prev=$_done
    _t_round=$_now
}

for _t in $_QUEUE; do
    while [ "$(jobs -pr | wc -l | tr -d ' ')" -ge "$_JOBS" ]; do
        _adapt_tick
        sleep 0.05
    done
    _run_one "$_t"
    _n_dispatched=$((_n_dispatched + 1))
done
wait   # 收尾：等最后 N 个跑完
if [ "$_ADAPT" -eq 1 ] && [ "$_JOBS" -ne "$_JOBS_START" ]; then
    echo "ℹ️  并发度 $_JOBS_START 路 → $_JOBS 路（运行时自适应）"
elif [ "$_ADAPT" -eq 1 ]; then
    echo "ℹ️  并发度 $_JOBS 路（自适应，MW_TEST_JOBS 可覆盖）"
fi

# 按文件名顺序汇总，保证输出稳定可读
_n_ok=0; _n_fail=0; _n_skip=0; _failed=""
for _t in tests/*.py; do
    _base=$(basename "$_t")
    _rc=$(cat "$_TMPDIR/$_base.rc" 2>/dev/null || echo 99)
    _out=$(cat "$_TMPDIR/$_base.out" 2>/dev/null)
    _last=$(printf '%s\n' "$_out" | tail -1)

    if [ "$_rc" = "SKIPNOENV" ]; then
        printf "  ⏭️  %-46s 跳过（解释器缺失）\n" "$_base"
        _n_skip=$((_n_skip + 1)); continue
    fi
    if [ "$_rc" -ne 0 ]; then
        printf "  ❌ %-46s rc=%s %s\n" "$_base" "$_rc" \
            "$(printf '%s\n' "$_out" | grep -E 'AssertionError|Error' | tail -1 | cut -c1-52)"
        _n_fail=$((_n_fail + 1)); _failed="$_failed $_base"
    elif printf '%s' "$_last" | grep -qE 'SKIP'; then
        printf "  ⏭️  %-46s %s\n" "$_base" "$(printf '%s' "$_last" | cut -c1-46)"
        _n_skip=$((_n_skip + 1))
    elif printf '%s' "$_out" | grep -qiE 'PASS|通过'; then
        printf "  ✅ %-46s %s\n" "$_base" "$(printf '%s' "$_last" | cut -c1-44)"
        _n_ok=$((_n_ok + 1))
    else
        # rc=0 但既没通过标记也没跳过标记 —— 最危险：看起来绿，实际没验
        printf "  ❓ %-46s rc=0 但输出无 PASS/SKIP 标记 —— 不计为通过\n" "$_base"
        _n_fail=$((_n_fail + 1)); _failed="$_failed $_base"
    fi
done

echo
echo "通过 $_n_ok / 跳过 $_n_skip / 失败 $_n_fail"
if [ -n "$_failed" ]; then
    echo "失败清单：$_failed"
    exit 1
fi
# 有 SKIP 也返回非零：跳过不算验证过，不能让 CI/脚本误以为全绿
if [ "$_n_skip" -ne 0 ]; then
    echo "⚠️  有 $_n_skip 个测试被跳过，未真正验证"
    exit 2
fi
exit 0
