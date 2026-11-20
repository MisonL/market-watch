#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 终止原因落盘 + 自检按原因分流归因（2026-11-20，P1 归因缺口）。

## 复现的那次误诊

2026-11-19 自检报：

    ❌ 当日模板兜底 5/10 轮（≥ 半数）—— AI 通道实质不可用，读者拿到的
       多为模板文案。查 watch.log 的「AI 终止」与 `docker logs new-api`
       的 stream disconnected

而 `watch.log` 自己在那 5 轮里有 **4 轮**写着「总时长闸门掐停且无产出 ——
**是我方预算上限，不是通道故障**」，只有 1 轮是首字节闸门（真挂起）：

    09:23 预算 132s←取数108s   14:39 预算 165s←取数75s
    10:04 预算 232s←取数 8s     15:08 预算 189s←取数51s
    09:45 首字节闸门 150s 零输出（真通道挂起）

流水 `runs.jsonl` 里这 5 轮全是 `ai_fallback=True`、`ai_stall` 键**不存在**，
于是判据只能合并成「查网关」。照那个提示去查 `docker logs new-api`，
方向从一开始就错了。

根因是这个仓库反复的那一类：**日志知道、统计不知道**。
`watch.log` 有完整归因（`_stall_desc`），但它产出的是给人读的长句，
从不进流水。

## 本文件锁住什么

① `analyze._stall_kind` 把 stderr 标记映射成短 token（与 `_stall_desc` 同源）
② `runlog.run(ai_stall=...)` 真的落盘，且只在非 None 时写
③ `ai_analyze` 被闸门掐停后，`_ROUND_AI_STALL` 记下**第一次**的原因
   （一轮内可能调 2~3 次，首记优先 —— 读者看到的模板是第一次失败导致的）
④ `selfcheck._ai_stall_split` 分成（我方预算, 其他）
⑤ `_ai_fallback_verdict` 的**判定**不因 stalls 改变（只改措辞）
⑥ 向后兼容：老流水没有 `ai_stall` 键时，行为与 2026-11-19 之前逐字一致
"""
import os
import sys
import json
import tempfile
import atexit
import shutil

_d = tempfile.mkdtemp(prefix="mw_t_stall_")
os.environ["MW_DIR"] = _d
atexit.register(shutil.rmtree, _d, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import analyze as A
import runlog
import selfcheck as S

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


# ---------- ① _stall_kind 的映射 ----------
# 用例取自生产真实 marker 形态（含 float 秒数、after-model-reject 变体）。
_cases = [
    # (stderr 标记, timed_out, 期望 token)
    ("[total-limit-gate 196.0s 触发，非 idle 闸门] ", False, "budget"),
    ("[total-limit-gate 132.0s 触发，非 idle 闸门] ", False, "budget"),
    ("[first-byte-gate 150s 触发，非 idle 闸门] ", False, "first-byte"),
    ("[first-byte-gate-after-model-reject 90s 触发] ", False, "model-reject"),
    ("[text-gate 600s 无正文] ", False, "text-gate"),
    ("[budget-exhausted 预算已耗尽] ", False, "budget-skip"),
    ("Popen 失败: [Errno 8] Exec format error", False, "launch"),
    ("", True, "idle"),
    ("", False, "exit"),
    (None, False, "exit"),
]
for _se, _to, _exp in _cases:
    _got = A._stall_kind(_se, _to)
    check(_got == _exp,
          f"_stall_kind({_se!r}, {_to}) 得 {_got!r}，期望 {_exp!r}")

# 顺序敏感：after-model-reject 是 first-byte 的变体，必须先判前者
check(A._stall_kind("[first-byte-gate-after-model-reject 1s]") == "model-reject",
      "after-model-reject 必须优先于 first-byte 判定（否则归因错成'零输出'）")

# ---------- ② runlog 落盘 ----------
_runs = os.path.join(_d, "state", "history", "runs.jsonl")
os.makedirs(os.path.dirname(_runs), exist_ok=True)
runlog.run("analyze", "push_ok", phase="end", ai_ms=1000,
           ai_fallback=True, ai_stall="budget")
runlog.run("analyze", "push_ok", phase="end", ai_ms=900)      # 正常轮
_recs = [json.loads(l) for l in open(_runs, encoding="utf-8") if l.strip()]
check(_recs[-2].get("ai_stall") == "budget",
      f"兜底轮未落 ai_stall: {_recs[-2]}")
check("ai_stall" not in _recs[-1],
      f"正常轮不该带 ai_stall 键: {_recs[-1]}")

# ---------- ③ _ROUND_AI_STALL 首记优先 ----------
A._ROUND_AI_STALL.clear()
A._stall_record("[total-limit-gate 100.0s x]", False)
A._stall_record("[first-byte-gate 150s x]", False)      # 第二次不得覆盖
check(A._ROUND_AI_STALL == ["budget"],
      f"必须首记优先（读者看到的模板是第一次失败导致的），实得 "
      f"{A._ROUND_AI_STALL}")

# 真实链路：ai_analyze 被总时长闸门掐停 → 记 budget
_saved = (A.M.claude_bin, A.M.run_with_idle_watchdog, A.M._AI_FAIL_FILE,
          A.AI_ROUND_BUDGET, list(A._ROUND_AI_MS))
_fail = os.path.join(_d, "ai_fail.json")
A.M._AI_FAIL_FILE = _fail
A.M.claude_bin = lambda: ("/bin/true", None)
A.M.run_with_idle_watchdog = lambda *a, **k: (
    0, "", "[total-limit-gate 196.0s 触发，非 idle 闸门] ", True)
A.AI_ROUND_BUDGET = 240
A._ROUND_AI_MS.clear()
A._ROUND_AI_STALL.clear()
try:
    A.ai_analyze("事实", [], "", slot="10:00")
    check(A._ROUND_AI_STALL == ["budget"],
          f"总时长闸门掐停后必须记 budget，实得 {A._ROUND_AI_STALL}")
finally:
    (A.M.claude_bin, A.M.run_with_idle_watchdog, A.M._AI_FAIL_FILE,
     A.AI_ROUND_BUDGET, _rms) = _saved
    A._ROUND_AI_MS[:] = _rms

# ---------- ④ _ai_stall_split 分类 ----------
for _st, _exp in [({"budget": 4, "first-byte": 1}, (4, 1)),
                  ({"budget": 3}, (3, 0)),
                  ({"first-byte": 2}, (0, 2)),
                  ({}, (0, 0)),
                  (None, (0, 0)),
                  ({"budget-skip": 2}, (2, 0)),
                  (["budget", "first-byte"], (1, 1)),
                  ({"budget": -1}, (0, 0)),
                  # 脏输入：字符串是 iterable，不显式拒绝会被逐字符计数
                  ("garbage", (0, 0)),
                  (b"xx", (0, 0))]:
    _got = S._ai_stall_split(_st)
    check(_got == _exp,
          f"_ai_stall_split({_st!r}) 得 {_got}，期望 {_exp}")

# ---------- ⑤ 判定不因 stalls 改变（只改措辞） ----------
for _fb, _ai in [(5, 10), (3, 8), (1, 8), (0, 8), (4, 8), (10, 10)]:
    _base = S._ai_fallback_verdict(_fb, _ai)
    for _st in ({"budget": _fb}, {"first-byte": _fb}, {"unknown": _fb}):
        _with = S._ai_fallback_verdict(_fb, _ai, _st)
        check(_with == _base,
              f"stalls={_st} 改变了判定（{_base} → {_with}）——"
              f"stalls 只该改措辞，不该改 ok/warn/fail")

# ---------- ⑥ 向后兼容：老流水（无 ai_stall）与不传参等价 ----------
check(S._ai_fallback_verdict(5, 10) ==
      S._ai_fallback_verdict(5, 10, {"unknown": 5}),
      "老流水（无 ai_stall → unknown）必须与不传 stalls 时判定一致")

# ---------- ⑦ 措辞分流：预算为主 vs 通道为主 ----------
# 判据函数本身不返回文案，措辞在 [投递结果] 段里。这里锁「分流条件」
# —— _bq > 0 且 _bq >= _oq 才算「以我方预算为主」。
_bq, _oq = S._ai_stall_split({"budget": 4, "first-byte": 1})
check(_bq > 0 and _bq >= _oq, "4 预算 + 1 通道 → 应判「预算为主」")
_bq, _oq = S._ai_stall_split({"budget": 1, "first-byte": 2})
check(not (_bq > 0 and _bq >= _oq), "1 预算 + 2 通道 → 不该判「预算为主」")
_bq, _oq = S._ai_stall_split({"first-byte": 3})
check(not (_bq > 0 and _bq >= _oq), "全通道 → 不该判「预算为主」")

# ---------- ⑧ 接线：run() 收尾必须真的把 _ROUND_AI_STALL 传给 runlog ----------
# ⚠️ 这条用 AST 读**生产源码**的接线，不是读副本/注释 ——
# 本仓库反复出现的缺陷是"判据在、调用没接上"：`_stall_record` 记了、
# runlog 能收，但 `run()` 收尾那句没传，流水里依然什么都没有。
import ast as _ast
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "analyze.py"), encoding="utf-8").read()
_tree = _ast.parse(_src)
_wired = []
for _node in _ast.walk(_tree):
    if (isinstance(_node, _ast.Call)
            and isinstance(_node.func, _ast.Attribute)
            and _node.func.attr == "run"):
        for _kw in _node.keywords:
            if _kw.arg == "ai_stall":
                _wired.append(_ast.unparse(_kw.value))
check(_wired, "analyze.py 里没有任何 `run(..., ai_stall=...)` 调用 —— "
              "归因记了却没落进流水（2026-11-19 误诊的形态）")
check(any("_ROUND_AI_STALL" in _w for _w in _wired),
      f"run(ai_stall=...) 没接 _ROUND_AI_STALL，实得 {_wired}")

print(f"ALL PASS (AI 终止归因 {_ok} 个断言)")
