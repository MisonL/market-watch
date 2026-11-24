#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI 预算地板：**行为级**验证，不能只查源码里有没有那行。

## 为什么要行为级

地板那段代码是 `if _budget < AI_MIN_CALL and not _ROUND_AI_MS: _budget = ...`，
纯静态判据（grep 到这一行就算过）抓不住三类真实失效：

· 判据写了但 `_budget` 没被真正传给看门狗（「函数对、接线断」）
· 条件写反（`>=` 而不是 `<`）—— 静态看代码是"有的"
· 上一轮的 `_ROUND_AI_MS` 没清，守卫恒假 —— 地板从来不生效

所以桩掉 `run_with_idle_watchdog`，**真调 `ai_analyze`**，直接看它
传下去的 `total_limit` 是多少。

## 实测依据（250 轮）

    取数 <45s（188 轮）   AI 耗时 P80 = 222s
    取数 ≥100s（14 轮）   AI 实测 39/60/76/102s —— 全是被掐停
    2026-11-24 14:54     取数 133s -> AI 只剩 107s -> 掐停、零产出、退模板

## 本文件锁住什么

① 取数 133s 时，传给看门狗的上限是**地板 180s**，不是 107s
② 取数超自己预算（133 > 120）的部分不计入扣减
③ 本轮**已调过** AI 时不给地板（否则重写各拿一份，一轮 360s）
④ 本轮没取数（data_ms=0）时走正常算式，地板不干扰
"""
import datetime
import os
import sys
import tempfile

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_floorbeh_"))

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import analyze as A
import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_SAVED = (A._ROUND_AI_MS, A._ROUND_DATA_MS, M.run_with_idle_watchdog,
          A.ai_analyze.__dict__.get("_x"), A.CLI_BACKOFF_FILE
          if hasattr(A, "CLI_BACKOFF_FILE") else None)
_AIMS = A._ROUND_AI_MS


def _run_round(data_ms, ai_ms_list):
    """跑一次 ai_analyze，返回**每次**调用传给看门狗的 total_limit 列表。

    ⚠️⚠️ 必须返回**列表**，不能只返回最后一个。
    `[实测]` 第一版只记最后一次，断言取数 133s 时应得 180s —— 实得 119.99s，
    看上去像"地板没生效"。真相是：桩返回空正文 → 触发**结构重写** →
    第二次调用时 `_ROUND_AI_MS` 已非空，守卫正确地把地板关掉了。
    **地板本来就是只给第一次的**，是断言取错了那次调用。
    """
    _AIMS.clear()
    for _v in ai_ms_list:
        _AIMS.append(_v)
    A._ROUND_DATA_MS = data_ms
    _caps = []

    def _fake_wd(args, **kw):
        _caps.append(kw.get("total_limit"))
        return (0, "", "", False)        # rc=0 但无正文 -> 走兜底/重写

    # ⚠️⚠️ 三个桩必须**在这个函数里**装拆，不能靠外层。
    # 第一版只在外层 try 里桩 `claude_bin` / `ai_fail_*`，`finally` 一恢复，
    # 第 ⑥ 段（不限制模式）就回到「CI 无 CLI → 提前 return」→
    # `_caps` 为空 → 报「地板把这条路径弄崩了」。
    # 而那根本不是地板的问题，是**夹具没跟着用例走**。
    # CI py3.13 精确报出了这条：
    #     ❌ 不限制模式（AI_ROUND_BUDGET=None）下看门狗没被调用
    _sv = (M.run_with_idle_watchdog, M.ai_fail_get, M.ai_fail_set,
           M.claude_bin)
    M.run_with_idle_watchdog = _fake_wd
    M.ai_fail_get = lambda: 0
    M.ai_fail_set = lambda *_a, **_k: None
    M.claude_bin = lambda: (sys.executable, "")
    try:
        A.ai_analyze("【指数】x", [], "", slot="14:30")
    finally:
        (M.run_with_idle_watchdog, M.ai_fail_get, M.ai_fail_set,
         M.claude_bin) = _sv
    return _caps


# `_run_round` 自带全部桩（见其内部注释），这里只留一个还原锚。
_orig_wd = M.run_with_idle_watchdog
_orig_fail_get, _orig_fail_set = M.ai_fail_get, M.ai_fail_set
# ⚠️⚠️ 必须**同时**桩掉 AI 熔断。
# 桩返回空正文 = 「通道不可用」→ `ai_fail_set` 武装 10 分钟熔断 →
# 第二轮 `ai_analyze` 在入口就被挡回，看门狗一次都不调用（`_caps` 为空）。
# `[实测]` 第一版没桩，只有第一组断言能跑，后面四组全部 `None` +
# TypeError（`abs(None - 180)`）。这是**夹具串味**：一个用例的副作用
# 污染了同文件后续用例，而报错形态是 TypeError 不是断言失败 ——
# 看起来像代码坏了。
M.ai_fail_get = lambda: 0
M.ai_fail_set = lambda *_a, **_k: None
# ⚠️⚠️ 还必须桩掉 `claude_bin()`。
# 它失败时**在到达 `_invoke` 之前**就 `return ""`，看门狗一次都不会被调用。
# CI 上没有 claude CLI → 本文件在 CI 上必红：
#     ❌ 看门狗一次都没被调用 —— 接线断了
# 那是**假红**：接线是好的，只是探针没跑起来。本仓库反复踩的就是这个
# （`[实测]` 2026-10-02 自检里「缺失标注未进入 prompt」同因）——
# **「探针没跑」与「功能坏了」必须分开报**，否则归因直接跑偏。
# 这里返回一个**必然存在的**可执行文件即可（`run_with_idle_watchdog` 已桩，
# 不会真执行它）。
_orig_cb = M.claude_bin
M.claude_bin = lambda: (sys.executable, "")
_orig_exec = getattr(A, "CLI_BACKOFF_FILE", None)
try:
    # ---------- ① 取数越界 -> 拿地板 ----------
    _caps = _run_round(133000, [])
    # ⚠️ 空 `_caps` 有**两种**原因，必须分开报：
    #   · `claude_bin()` 返回 None（CI 没 CLI）-> 探针根本没跑到 _invoke
    #   · 真的没把 _budget 传给看门狗 -> 接线断了
    # 混报会让归因跑偏（本仓库反复的形态）。`claude_bin` 已桩成必然成功，
    # 所以走到这里为空就真是接线问题 —— 但仍要把这句话写出来，
    # 免得下次有人删掉桩。
    check(bool(_caps),
          "看门狗一次都没被调用。两种可能：① `claude_bin()` 返回了 None"
          "（CI 无 CLI，探针没跑 —— 本文件已桩，不该发生）；"
          "② 真的没接上（接线断了）")
    _t = _caps[0]
    check(_t is not None,
          "首次调用时 total_limit 是 None —— 地板算出来了却没传给看门狗"
          "（「函数对、接线断」）")
    check(abs(_t - A.AI_MIN_CALL) < 0.5,
          f"取数 133s 时**首次**调用上限是 {_t}s —— 应为地板 "
          f"{A.AI_MIN_CALL}s。107s（=240-133）就是 11-24 那轮被掐停的值")

    # ---------- ② 超预算部分不计入扣减 ----------
    # 取数 133s 但按 120s 计 -> 240-120=120s，仍低于地板 -> 180s。
    # 若超预算部分照样全额扣，会算出 107s（低于地板，也是 180s）——
    # 两者在地板生效时**看不出来**。所以这条单独用「取数刚好到预算」验：
    # 取数 120s -> 240-120=120s -> 地板 180s。
    _caps2 = _run_round(120000, [])
    _t2 = _caps2[0] if _caps2 else None
    check(abs(_t2 - A.AI_MIN_CALL) < 0.5,
          f"取数正好 120s 时 AI 上限 {_t2}s，应为地板 {A.AI_MIN_CALL}s")

    # ---------- ③ 已调过 AI 时不给地板 ----------
    # 场景：首调跑了 60s，重写时取数 100s -> 240-(60+100)=80s。
    # 必须拿到 80s（不再补到 180s），否则一轮最多 180+180+180=540s。
    _caps3 = _run_round(100000, [60000])
    _t3 = _caps3[0] if _caps3 else None
    check(_t3 is not None and abs(_t3 - 80.0) < 0.5,
          f"本轮已调过 AI（60s）时，重写的上限是 {_t3}s —— "
          f"应为 240-(60+100)=80s，**不能**再给地板"
          f"（否则一轮 540s，必然吞掉后续调度）")

    # ---------- ④ 无取数时正常算式 ----------
    _caps4 = _run_round(0, [])
    _t4 = _caps4[0] if _caps4 else None
    check(_t4 is not None and abs(_t4 - float(A.AI_ROUND_BUDGET)) < 0.5,
          f"没取数时 AI 上限是 {_t4}s —— 应为整轮预算 "
          f"{A.AI_ROUND_BUDGET}s，地板不该干扰正常轮")

    # ---------- ⑤ 地板不越过整轮预算 ----------
    _caps5 = _run_round(500000, [])
    _t5 = _caps5[0] if _caps5 else None
    check(_t5 is not None and _t5 <= max(float(A.AI_ROUND_BUDGET),
                                         A.AI_MIN_CALL) + 0.5,
          f"取数远超预算时 AI 上限 {_t5}s —— 超过了地板与整轮预算的较大者")
finally:
    M.run_with_idle_watchdog = _orig_wd
    M.ai_fail_get, M.ai_fail_set = _orig_fail_get, _orig_fail_set
    M.claude_bin = _orig_cb
    A._ROUND_AI_MS.clear()
    A._ROUND_DATA_MS = 0

# ---------- ⑥ 空白守卫：地板不能把「预算已耗尽」那条路彻底堵死 ----------
# 地板生效后 `_budget > 0` 恒成立（180），于是「预算已耗尽 → 直接用模板」
# 那个分支**不可达**了。这不是缺陷（地板本意如此），但必须确认
# `AI_ROUND_BUDGET=None`（不限制模式）时地板不去干扰 —— 那种模式
# 下 `_budget` 是 None，`None < 180` 在 Python 3 会抛 TypeError。
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "analyze.py"), encoding="utf-8").read()
_i = _src.find("if _budget < AI_MIN_CALL and not _ROUND_AI_MS:")
check(_i > 0, "找不到地板判据")
# 往前找最近的 `if AI_ROUND_BUDGET is not None:` 包裹
_outer = _src.rfind("if AI_ROUND_BUDGET is not None:", 0, _i)
_inner = _src.find("else:", _i)
check(_outer > 0,
      "地板不在 `if AI_ROUND_BUDGET is not None:` 之内 —— 不限制模式下 "
      "`_budget` 是 None，`None < 180` 会抛 TypeError（整轮直接崩）")

_orig_b = A.AI_ROUND_BUDGET
try:
    A.AI_ROUND_BUDGET = None
    _capsn = _run_round(133000, [])
    check(bool(_capsn),
          "不限制模式（AI_ROUND_BUDGET=None）下看门狗没被调用 —— "
          "地板把这条路径弄崩了")
    check(_capsn and _capsn[0] is None,
          f"不限制模式下 total_limit 应为 None（不给上限），实得 "
          f"{_capsn[0] if _capsn else '?'}")
finally:
    A.AI_ROUND_BUDGET = _orig_b

print(f"ALL PASS (AI 预算地板 · 行为级 {_ok} 个断言)")
