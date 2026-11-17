"""AI 兜底率判据 + 兜底轮次落盘（2026-11-03 修，P1 观测缺口）。

复现背景：AI 成功时**一行日志都不打**，只有失败才留痕。于是
`[实测]` 2026-11-03：7 轮真正调用 AI 的轮次里 3 轮零产出退模板
（09:24 首字节闸门 151s / 09:44 预算耗尽 238s / 10:45 预算耗尽 226s），
而 watch.log 里只能看到 3 条失败，**看不出分母**；自检也没有任何
"当日兜底率"的结果性判据 —— 读者收到的是模板文案，系统却说一切正常。

两半：
  ① `runlog.run(ai_fallback=True)` 必须把 `ai_fallback: true` 落进流水
     （兜底轮次可被事后统计；不写 = 分母永远不可见）
  ② `selfcheck._ai_fallback_verdict(n_fb, n_ai)` 纯函数三态判定
     （0 → ok；部分兜底 → warn；过半兜底 → fail）
"""
import os, sys, json, tempfile, atexit, shutil
_d = tempfile.mkdtemp(prefix="mw_t_")
os.environ["MW_DIR"] = _d
# ⚠️ 本文件无条件自建 MW_DIR（它要写 runs.jsonl 夹具），因此必须注册清理；
# 若被外部 MW_DIR 调用，本文件仍覆盖之（测试隔离优先），清理只删自己建的。
atexit.register(shutil.rmtree, _d, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import runlog

# ---------- ① 落盘契约 ----------
_runs = os.path.join(_d, "state", "history", "runs.jsonl")
os.makedirs(os.path.dirname(_runs), exist_ok=True)
runlog.run("analyze", "push_ok", phase="end", ai_ms=1200, ai_fallback=True)
runlog.run("analyze", "push_ok", phase="end", ai_ms=900)          # 正常轮
_recs = [json.loads(l) for l in open(_runs, encoding="utf-8") if l.strip()]
assert _recs[-2].get("ai_fallback") is True, f"兜底轮未落 ai_fallback: {_recs[-2]}"
assert "ai_fallback" not in _recs[-1], f"正常轮不该带 ai_fallback: {_recs[-1]}"
print("PASS 1 runlog writes ai_fallback")

# ---------- ② 判据三态 ----------
import selfcheck as S

_ok = S._ai_fallback_verdict(0, 8)
assert _ok == "ok", _ok
print("PASS 2 zero-fallback ok")

_w = S._ai_fallback_verdict(1, 8)          # 少数兜底 → warn，不置 ok_all
assert _w == "warn", _w
print("PASS 3 few-fallback warn")

# 边界：兜底 3 轮但只占 37.5%（3/8）→ warn（不是 AI 全挂）
assert S._ai_fallback_verdict(3, 8) == "warn", S._ai_fallback_verdict(3, 8)
print("PASS 4 3-of-8 warn")

# 过半兜底 → fail（AI 实质不可用；10-22 那种 10/10 全挂）
assert S._ai_fallback_verdict(10, 10) == "fail"
assert S._ai_fallback_verdict(5, 8) == "fail"
print("PASS 5 majority-fallback fail")

# 边界：恰好一半 4/8 → fail（过半含"达到一半"这一侧，宁可报）
assert S._ai_fallback_verdict(4, 8) == "fail", S._ai_fallback_verdict(4, 8)
print("PASS 6 half-is-fail boundary")

# 脏输入：分母为 0 / None / 负数 → ok（没有调 AI 的轮次，谈不上兜底率）
assert S._ai_fallback_verdict(0, 0) == "ok"
assert S._ai_fallback_verdict(None, None) == "ok"
assert S._ai_fallback_verdict(-1, -1) == "ok"
print("PASS 7 dirty-input ok")

# ⚠️ 2026-11-03 修的真实漏判：**有兜底轮、但一次 AI 都没真调**（分母 0）
# 必须 fail，不能 ok。可达路径：AI 熔断开启中 / claude CLI 缺失 /
# 首调就预算耗尽（取数耗时已 >240s）—— 这三种都不进 `_invoke`，
# 于是 `ai_ms` 永远不写、`ai_fallback` 照写。若判 ok，就等于
# 「AI 整天不可用、读者拿到的全是模板」时自检报一切正常 ——
# 正是本条判据存在的目的。`[实测]` 生产 09:44/10:45 两轮即此类
# （预算耗尽 238s/226s）。
assert S._ai_fallback_verdict(3, 0) == "fail", S._ai_fallback_verdict(3, 0)
assert S._ai_fallback_verdict(1, 0) == "fail", S._ai_fallback_verdict(1, 0)
print("PASS 7b fallback-without-ai-invoked is fail")

# 兜底数大于分母（数据不一致）不得除零/负比率
assert S._ai_fallback_verdict(5, 3) in ("fail", "warn"), \
    S._ai_fallback_verdict(5, 3)
print("PASS 8 fb-gt-ai no-crash")
print("ALL PASS")