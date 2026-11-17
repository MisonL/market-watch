#!/bin/bash
# 统一测试运行器 —— 2026-11-08 加。
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
# ## 跑法
#
#     bash tests/run_all.sh
#
# 需要一个**干净的** MW_DIR / 进程环境；本脚本给每个测试单独建沙箱。
set -u

cd "$(dirname "$0")/.." || exit 1

_SYS_PY=/usr/local/bin/python3
_VENV_PY=agent/.venv/bin/python

_n_ok=0
_n_fail=0
_n_skip=0
_failed=""

for _t in tests/*.py; do
    _base=$(basename "$_t")
    # agent 摘要类测试需要 claude_agent_sdk，只有项目 venv 里有
    case "$_base" in
        test_agent_summary_*) _py=$_VENV_PY ;;
        *)                    _py=$_SYS_PY ;;
    esac
    if [ ! -x "$_py" ]; then
        # ⚠️ 2026-11-17（开源通用化）：解释器缺失是**环境缺失**，不是测试
        # 失败 —— 原先记进 `_failed`，于是别人克隆后跑 `run_all.sh` 会看到
        # 「失败 2」，而这 2 个测试压根没执行。分母不同、结论也不同。
        # 改成计入 `_n_skip`，与其它 SKIP 同口径。
        #
        # 具体场景：`test_agent_summary_*` 需要 `agent/.venv`，而 `agent/`
        # 是独立 git 仓库、已在本仓库的 .gitignore 里 —— 别人克隆后必然没有。
        printf "  ⏭️  %-46s 跳过（解释器缺失：%s）\n" "$_base" "$_py"
        _n_skip=$((_n_skip + 1))
        continue
    fi

    _d=$(mktemp -d "${TMPDIR:-/tmp}/mw_run_XXXXXX") || exit 1
    _out=$(MW_DIR="$_d" PYTHONDONTWRITEBYTECODE=1 "$_py" -B "$_t" 2>&1)
    _rc=$?
    rm -rf "$_d"

    _last=$(printf '%s\n' "$_out" | tail -1)
    # 判据必须是「rc 干净」**且**「输出里有明确的通过标记」——
    # 只判 rc 会被「提示语 + exit 0」骗过去（见文件头的坑 1）。
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
        # rc=0 但既没通过标记也没跳过标记 —— 这是**最危险**的一类：
        # 它看起来是绿的，实际上什么都没验（例如提前 return、空函数体）。
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
