#!/usr/bin/env python3
"""AI 调用留痕（模型名/耗时/输出量）—— 2026-11-21。

## 为什么需要这个测试

`AI_MODEL = None` 时走 `claude` CLI 的**默认模型**，那个 id 只存在于上游。
于是"今天用的是哪个模型"这个问题，**日志里从来查不到**。

`[实测]` 2026-11-20 排查"AI 一天慢到 104s、兜底率 0%→23%"时，我只能手工
跑 `claude` CLI 才知道是 `claude-fable-5`；watch.log 里一个字都没有。
没有这条记录，"它变慢了"到底是「换了模型」还是「同一模型变慢」分不开 ——
而这两种处置完全不同（换回来 vs 查网关/改预算）。

## 判据要点

  · 模型名要从 stream-json 的 `system/init` 事件里取（那是流里第一个事件，
    所以**被闸门掐停、正文还没出来时**也拿得到 —— 正是最需要记录的场合）
  · 取不到时降级成 `AI_MODEL` / 「CLI 默认」，不能因此不打日志
  · 日志必须**真的打出来**（本轮反复出现的"代码在 ≠ 会执行"）

⚠️ 测试自身必须**不污染生产熔断文件** `state/cache/ai_fail.json`：
`ai_analyze` 在正文过不了结构闸门时会写熔断，阻塞下一轮真实 AI。
故本测试用**能过闸门**的正文，并在前后各删一次熔断文件。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

# ⚠️2026-11-22（P0写穿生产）：下面 `_FAIL`原先拼的是 `ROOT/state/cache/…`，
#而本文件会 `os.remove(_FAIL)` ——**直接删生产的 AI熔断文件**。
#必须先设 MW_DIR再 import paths；`_FAIL`改由 paths.p_cache解析。
if not os.environ.get("MW_DIR"):
    os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_aiobs_")
_SB = os.environ["MW_DIR"]
for _sub in ("state/history", "state/cache", "conf", "logs"):
    os.makedirs(os.path.join(_SB, _sub), exist_ok=True)

import marketlib as M                                  # noqa: E402
import analyze as A                                    # noqa: E402

_ok = [0]


def check(cond, msg):
    if cond:
        _ok[0] += 1
    else:
        print(f"  ❌ {msg}")
        raise SystemExit(1)


# ⚠️必须走 paths.p_cache（受 MW_DIR管辖），不能拼 ROOT ——
#拼 ROOT等于固定指向生产目录，MW_DIR沙箱对它无效。
from paths import p_cache as _p_cache # noqa: E402
_FAIL = _p_cache("ai_fail.json")

# 能过结构闸门的正文（够长、标签齐、有 > 引用行）。
BODY = (
    "**盘面** 上证 -1.23%（主力 -3.16%，涨159跌2176），成交 1.42 万亿放量。\n"
    "**触发** 整点播报 15:00\n"
    "**关注板块** 半导体 -6.86%（-71.6 亿）\n"
    "**建议**\n"
    "> 风险 放量下跌属主动抛压。\n"
    "> 机会 超跌方向存在修复窗口。\n"
)
INIT = ('{"type":"system","subtype":"init",'
        '"model":"claude-fable-5","session_id":"x"}')
ASSIST = ('{"type":"assistant","message":{"content":'
          '[{"type":"text","text":"' +
          BODY.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") +
          '"}]}}')

# ---------- ① ai_extract_model 的四种形态 ----------
check(M.ai_extract_model(INIT) == "claude-fable-5",
      f"init 事件里应取到模型 id，实得 {M.ai_extract_model(INIT)!r}")
check(M.ai_extract_model("") == "",
      "空输入应返回空串（调用方据此降级）")
check(M.ai_extract_model("not json") == "",
      "非 JSON 应返回空串，不得抛异常")
check(M.ai_extract_model('{"type":"system"}') == "",
      "无 model 字段应返回空串")
# 最关键的一条：只有 init、正文未出（被掐停的部分输出）也要能取到
check(M.ai_extract_model(INIT + "\n" +
                         '{"type":"stream_event","event":{"delta":'
                         '{"text":"半句"}}}') == "claude-fable-5",
      "部分输出（仅 init + delta）时必须仍能取到模型 —— "
      "被闸门掐停的轮次正是最需要记录模型的场合")

# ---------- ② 日志必须真的打出来，且带上模型名 ----------
try:
    os.path.exists(_FAIL) and os.remove(_FAIL)      # 先清熔断，免得挡住调用
except OSError:
    pass


def _run(raw, label):
    """跑一次真实 ai_analyze，返回 AI 级别的日志行。

    ⚠️⚠️ **必须同时桩 `claude_bin`**（2026-11-21 CI 实测坐实）。
    `ai_analyze()` 在调 `_invoke` 之前有一道闸：

        binp, bindir = M.claude_bin()
        if not binp:
            _log("claude CLI 未找到，跳过 AI 调用（将用兜底模板）")
            return ""

    本机装了 claude CLI 所以本地永远走得通；而 **CI 跑在 ubuntu 上，没有
    CLI** → 直接 return，`_invoke` 一次都没进 → 留痕日志当然一条不出 →
    断言"完整流下没有产生 AI 级别日志"报 ❌。
    这个测试因此在 GitHub Actions 上**连挂 4 次提交**，而我只看本地
    `run_all 56/56` 没看 CI —— 「本地绿 ≠ CI 绿」，环境差异正是判据的边界。
    """
    logs = []
    _sv_log = M.log
    _sv_wd = M.run_with_idle_watchdog
    _sv_cb = M.claude_bin
    M.log = lambda m, tag="": logs.append((tag, str(m)))
    A.M.log = M.log
    # 桩成"存在且可执行"的假路径：真正的执行被下面的 watchdog 桩拦掉，
    # 不会真的跑任何二进制。
    M.claude_bin = lambda: ("/bin/echo", "/bin")
    M.run_with_idle_watchdog = lambda *a, **k: (0, raw, "", False)
    try:
        A.ai_analyze(BODY, [], "# 计划", slot="15:00")
    finally:
        M.log = _sv_log
        M.run_with_idle_watchdog = _sv_wd
        M.claude_bin = _sv_cb
    return [m for t, m in logs if t == "AI"]


try:
    lines = _run(INIT + "\n" + ASSIST, "完整流")
    check(lines, "完整流下没有产生 AI 级别日志 —— 留痕代码在 ≠ 会执行")
    check("claude-fable-5" in lines[0],
          f"日志里没有实际模型名，实得 {lines[0][:120]!r} —— "
          f"这正是「日志查不到用的是哪个模型」的缺口")
    check("耗时" in lines[0] and "字符" in lines[0],
          f"日志缺耗时/输出量，实得 {lines[0][:120]!r}")

    # 取不到模型名时不得因此不打日志（降级成 CLI 默认）
    lines2 = _run(ASSIST, "text 模式")
    check(lines2, "取不到模型名时日志整条消失了 —— 必须先打日志、再降级显示")
    check("CLI 默认" in lines2[0] or "claude" in lines2[0].lower(),
          f"降级文案不对，实得 {lines2[0][:120]!r}")

    # 被掐停（超时标记）时也必须有日志，并标出掐停
    logs = []
    _sv_log = M.log
    _sv_wd = M.run_with_idle_watchdog
    _sv_cb3 = M.claude_bin
    M.log = lambda m, tag="": logs.append((tag, str(m)))
    A.M.log = M.log
    M.claude_bin = lambda: ("/bin/echo", "/bin")
    M.run_with_idle_watchdog = lambda *a, **k: (0, INIT + "\n" + ASSIST, "", True)
    try:
        A.ai_analyze(BODY, [], "# 计划", slot="15:00")
    finally:
        M.log = _sv_log
        M.run_with_idle_watchdog = _sv_wd
        M.claude_bin = _sv_cb3
    ai = [m for t, m in logs if t == "AI"]
    check(ai, "被掐停的调用没有日志 —— 那正是最需要看清模型的场合")
    check("掐停" in ai[0], f"掐停未在日志里标出，实得 {ai[0][:120]!r}")
finally:
    # ⚠️ 必须清掉本次可能写下的熔断文件（测试不得留下生产副作用）
    try:
        os.path.exists(_FAIL) and os.remove(_FAIL)
    except OSError:
        pass

print(f"ALL PASS (AI 调用留痕（模型名/耗时/输出量/掐停）{_ok[0]} 个断言)")