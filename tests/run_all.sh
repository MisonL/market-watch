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
# 所以可以并发跑。用 `MW_TEST_JOBS=N` 控制并发度：
#
#     bash tests/run_all.sh                 # 默认 4 路
#     MW_TEST_JOBS=1 bash tests/run_all.sh  # 退回串行（排查用）
#
# ⚠️ 关键：**输出必须归属到各自的测试文件**。做法是每个测试把 stdout 写进
# 自己的临时文件（`$_TMPDIR/<name>.out` + `.rc`），全部跑完后再逐个读取判定 ——
# 绝不能让多个后台进程往同一个 stdout 写（那正是坑 2 的翻版）。
#
# 需要一个**干净的** MW_DIR / 进程环境；本脚本给每个测试单独建沙箱。
set -u

cd "$(dirname "$0")/.." || exit 1

_SYS_PY=/usr/local/bin/python3
_VENV_PY=agent/.venv/bin/python
_JOBS=${MW_TEST_JOBS:-4}

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

# 派发全部测试，按批次控制并发度。
# ⚠️ 不用 `wait -n` —— macOS 自带 bash 3.2 不支持它（`wait: -n: invalid option`）。
# 改用"每批 N 个、等整批完成再派下一批"，虽不如线程池精细，但简单可靠：
# 输出归属靠各自的 .out/.rc 文件，与调度方式无关。
_n_running=0
for _t in tests/*.py; do
    _run_one "$_t"
    _n_running=$((_n_running + 1))
    if [ "$_n_running" -ge "$_JOBS" ]; then
        wait
        _n_running=0
    fi
done
wait   # 等最后不足一批的剩余测试

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
