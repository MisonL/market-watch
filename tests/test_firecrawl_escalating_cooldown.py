#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""未分类的 firecrawl 失败不能一律套 900s —— 应按**连续失败次数**递增。

## 同一个缺陷出现三次，每次换一个入口

| 入口 | 原冷却 | 发现时间 | 修法 |
|---|---|---|---|
| 限流 | 900s | 11-23 | 固定 60s |
| 网关 5xx | 900s | 11-24 | 固定 120s |
| **调用超时** | **900s** | **11-24 13:09** | **本条** |

`[实测]` 13:09:00：

    firecrawl 调用异常: TimeoutExpired: ... timed out after 40 seconds

→ 900s 冷却 → 13:09~13:24 整整 **15 分钟**板块数据全无（13:14 那一轮
`clist 完全取不到`、两个 fs 的资金两端都返回 0 条）。
**一次 40 秒的超时罚 900 秒，是 22 倍过度惩罚。**

## 结构性问题不在「还有哪个入口没开小灶」，在默认值

所有未分类失败一律 900s。而观察到的失败形态**全都是瞬时的** ——
限流、网关抖动、超时、空输出，没有一个是「这台源真的挂了」。

全历史代价（`logs/watch.log`）：
    调用异常 12 次 + 无输出 31 次 + 非 JSON 21 次 = 64 次 × 900s ≈ 16 小时冷却

## 改成递增

· 第 1 次失败 → 120s（大概率已恢复）
· 连续失败 → 240 / 480 / 900 封顶
· **任一次成功即清零**（不清零的话，惩罚永不衰减：
  历史上连续失败过 8 次，以后每次失败都直接罚满 900s）

这样既不再过度惩罚瞬时抖动，又能把「真的挂了」隔离住 ——
而且**不用预先知道下一个入口长什么样**。

## 本文件锁住什么

① 递增序列正确、有封顶
② 成功真的清零（且**落盘**，不能只在内存 — 本进程每 300s 被重起）
③ 计数与 `until` 在**同一个文件**里（分两个文件会出现
   「计数清零了但冷却还在」的中间态）
④ 所有通用失败分支都走递增，不再有 `_fc_fail_set(time.time() + 900)` 残留
⑤ 限流 / 网关仍保留各自的固定短冷却（它们的时长是**已知**的）
"""
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_orig = M._FC_FAIL_FILE
_tmp = os.path.join(tempfile.mkdtemp(), "fc.json")
try:
    M._FC_FAIL_FILE = _tmp

    # ---------- ① 递增序列 ----------
    _seq = []
    for _ in range(6):
        _delay, _n = M._fc_fail_bump()
        _seq.append((_n, _delay))
    check([n for n, _ in _seq] == [1, 2, 3, 4, 5, 6],
          f"连续失败计数不是 1,2,3,4,5,6，实得 {[n for n, _ in _seq]} —— "
          f"递增冷却的前提就断了")
    check(_seq[0][1] == M.FC_ESCALATE_BASE,
          f"首次失败冷却 {_seq[0][1]}s，不等于 BASE={M.FC_ESCALATE_BASE}s")
    check(_seq[1][1] == M.FC_ESCALATE_BASE * 2,
          f"第二次失败冷却 {_seq[1][1]}s，不是翻倍 —— 说明没在递增")
    check(all(d <= M.FC_ESCALATE_MAX for _, d in _seq),
          f"冷却没有封顶，出现了 {[d for _, d in _seq]}（上限 "
          f"{M.FC_ESCALATE_MAX}s）")
    check(_seq[-1][1] == M.FC_ESCALATE_MAX,
          f"第 6 次仍是 {_seq[-1][1]}s，没到封顶 {M.FC_ESCALATE_MAX}s")
    # 首次绝不能还是 900s（那就是没修）
    check(_seq[0][1] < 300,
          f"首次失败仍冷却 {_seq[0][1]}s —— 一次 40 秒的超时被罚 "
          f"{_seq[0][1]} 秒，22 倍过度惩罚照旧")

    # ---------- ③ 计数与 until 同文件 ----------
    _rec = M._load_json(_tmp, {}) or {}
    check("until" in _rec and "n" in _rec,
          f"计数与冷却截止没写在同一个文件里（实得键 {sorted(_rec)}）—— "
          f"分开存会出现「计数清零但冷却还在」的中间态")

    # ---------- ② 成功真的清零，且落盘 ----------
    M._fc_ok()
    _rec2 = M._load_json(_tmp, {}) or {}
    check(_rec2.get("n") == 0,
          f"成功一次后盘上计数是 {_rec2.get('n')!r}，不是 0 —— "
          f"递增永不衰减，以后每次都罚满上限")
    _delay2, _n2 = M._fc_fail_bump()
    check(_n2 == 1,
          f"清零后再失败一次，计数是 {_n2} 不是 1 —— 没真清")
finally:
    M._FC_FAIL_FILE = _orig
    shutil.rmtree(os.path.dirname(_tmp), ignore_errors=True)

# ---------- ④ 所有通用分支都走递增 ----------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "marketlib.py"), encoding="utf-8").read()
check("_fc_fail_set(time.time() + FIRECRAWL_COOLDOWN)" not in _src,
      "仍有分支套用通用 900s 冷却 —— 下一个未分类入口会立刻重犯同一个错")
check(_src.count("_fc_fail_bump()") >= 3,
      f"只有 {_src.count('_fc_fail_bump()')} 处走递增，"
      f"而 firecrawl_get 的两处 + firecrawl_json 的两处都应走它")

# ---------- ⑤ 限流 / 网关保留固定短冷却 ----------
_get = M.__dict__["firecrawl_get"].__code__
_gsrc = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "src", "marketlib.py"), encoding="utf-8").read()
_i = _gsrc.index("def firecrawl_get")
_j = _gsrc.index("def firecrawl_json", _i)
_body = _gsrc[_i:_j]
check("FIRECRAWL_RATE_LIMIT_COOLDOWN" in _body,
      "限流分支丢了固定 60s 冷却（它的时长是**已知**的，不该跟着递增）")
check("FIRECRAWL_GATEWAY_COOLDOWN" in _body,
      "网关 5xx 分支丢了固定 120s 冷却")
check("_fc_ok()" in _body,
      "成功路径没有 `_fc_ok()` —— 递增冷却永不衰减")

print(f"ALL PASS (firecrawl 递增冷却 {_ok} 个断言)")
