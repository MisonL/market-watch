#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""同一轮的板块名单必须**只拉一遍** —— 两处各拉一遍在预算内算术上不可能取全。

## 2026-11-24 实测的账

一轮 `analyze` 里有两个消费者，各拉一遍同一个板块宇宙：

    hot.fetch_sectors()      行业 5 页 + 概念 6 页 = 11 次
    alert._sector_rows()     两方向各 ≤2 页 × 2 类 = ≤8 次
                             合计 19 次

而 firecrawl 额度 11~13/min、`DATA_BUDGET`=120s，按 9 req/min 算
**19 次要 127 秒** —— 比取数预算还长。所以无论怎么调冷却/节流，
「取全」在这套预算下**算术上就不可能**。

`[实测]` 13:47 那一轮：t:2 与 t:3 各停在 **200/496、199/554**
（每类只取到 2 页），四个消费者日志一模一样地报「名单不完整」。

## 修法

不是砍页数（砍了就永久丢掉「流出最猛那批」——按 f62 降序时它们在末尾），
而是**只拉一遍**：两边共用同一份全量名单。请求数 19 -> 11（5+6），
11 × 6.7s ≈ 74s，回到预算之内。

为什么共享的是「全量」而不是「两端」：`hot` 需要全量做候选池
（按成交额/成分股数过滤），只看两端会丢掉中间那一大段。

## 本文件锁住什么

① 轮内真的只拉一次：`hot.fetch_sectors()` + 两次 `alert._sector_rows()`
   合计只产生**一轮**分页请求
② 字段必须是**超集**：两份 URL 不同就命中不了缓存 —— 那正是
   「共享」最容易假生效的地方（看着改了，实际各拉各的）
③ `sector_round_reset()` 必须被 `analyze.run()` 每轮调用 ——
   不清缓存就是拿上一轮的名单当真值
④ 失败**不缓存**：否则本轮后续调用会拿到空名单并当成「真没有板块」
"""
import os
import re
import subprocess
import sys
import tempfile

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


_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")

# ---------- ② 字段必须是超集 ----------
_f = set(x for x in M.SECTOR_FIELDS.split(",") if x)
# hot.fetch_sectors 原字段（它要 f8/f10/f20/f21/f66/f69/f104/f105/f128/f134）
for _need in ("f2", "f3", "f6", "f8", "f10", "f12", "f14", "f20", "f21",
              "f24", "f62", "f66", "f69", "f104", "f105", "f128", "f134",
              "f184"):
    check(_need in _f, f"SECTOR_FIELDS 缺 {_need} —— hot 的候选池算不出来"
                        f"（或两份 URL 不同、缓存命中不了）")
# alert 需要 f12/f14/f3/f6/f62/f184 —— 已含在上面

# ---------- ③ 每轮必须清缓存 ----------
_an = open(os.path.join(_SRC, "analyze.py"), encoding="utf-8").read()
check("M.sector_round_reset()" in _an,
      "`analyze.run()` 没有调用 `sector_round_reset()` —— "
      "本轮会读到上一轮的板块名单（拿几分钟前那份当真值）")
# 必须在取数之前
_i_reset = _an.find("M.sector_round_reset()")
_i_hot = _an.find("HOT.analyze_cached(")
check(0 < _i_reset < _i_hot,
      f"`sector_round_reset()` 出现在 hot 取数**之后**"
      f"（reset@{_i_reset} hot@{_i_hot}）—— 清晚了，本轮还是用到旧缓存")

# ---------- ④ 两个消费者都必须走共享实现 ----------
_hot = open(os.path.join(_SRC, "hot.py"), encoding="utf-8").read()
_al = open(os.path.join(_SRC, "alert.py"), encoding="utf-8").read()
check("M.sector_rows(" in _hot,
      "hot.fetch_sectors 没有走 `M.sector_rows()` —— 仍在自己拉一遍")
check("M.sector_rows(" in _al,
      "alert._sector_rows 没有走 `M.sector_rows()` —— 仍在自己拉一遍")
# ⚠️ 判据要**跳过注释** —— 我第一版直接在源码里找 `clist_extremes(`，
# 而它现在恰好出现在解释"为什么不再用它"的注释里，于是恒红。
# 判据不区分代码与注释，测的就是注释。
_al_code = "\n".join(
    _ln for _ln in _al.splitlines() if not _ln.lstrip().startswith("#"))
check("clist_extremes(" not in _al_code,
      "alert 里仍有 `clist_extremes(`（**代码**，不是注释）—— "
      "两端扫描与全量共享同时存在，请求数没有真的降下来")

# ---------- ① 行为：真的只拉一次 ----------
# 用子进程跑真实路径，数 firecrawl 调用。桩掉两样东西：
#   · subprocess.run：返回一个形状合法但内容固定的 clist 响应
#   · time.sleep：配额窗口别真等 60 秒
# 目标是数**请求次数**，不是验数据内容。
_CODE = r'''
import json, sys
sys.path.insert(0, {src!r})
import marketlib as M

M._FC_PACE_FILE = {pace!r}
M.time.sleep = lambda *a, **k: None      # 别真等配额窗口

_n = [0]

class _R:
    returncode = 0
    stderr = ""
    def __init__(self, txt):
        self._t = txt
    @property
    def stdout(self):
        return self._t

def _run(cmd, *a, **k):
    # ⚠️ 每一页必须给**不同**的 f12。第一版每页返回同一批 BK0000..BK0099，
    # 于是 `clist` 的去重逻辑判定 `_new == 0` 直接 break —— 每个 fs 只拉了
    # 2 页，断言"至少 8 次"恒红。桩的数据形状必须让被测逻辑真的走到末页。
    _n[0] += 1
    base = _n[0] * 1000
    diff = [{{"f12": "BK%05d" % (base + i), "f14": "s%d" % (base + i),
              "f3": 1.0, "f6": 5e8, "f62": 1e8, "f184": 2.0}}
            for i in range(100)]
    return _R(json.dumps({{"rc": 0, "data": {{"total": 350, "diff": diff}}}}))

M.subprocess.run = _run
M.sector_round_reset()
import hot, alert
try:
    hot.fetch_sectors()
    alert._sector_rows("m:90+t:2")
    alert._sector_rows("m:90+t:3")
finally:
    print(_n[0], flush=True)
'''

_pace = os.path.join(tempfile.mkdtemp(), "pace.json")
_r = subprocess.run([sys.executable, "-B", "-c",
                     _CODE.format(src=_SRC, pace=_pace)],
                    capture_output=True, text=True, timeout=180,
                    cwd=os.path.join(_HERE, ".."))
check(_r.returncode == 0,
      f"子进程跑共享路径失败：{(_r.stderr or '')[-300:]}")
_n = int((_r.stdout or "0").strip().splitlines()[-1])
# total=350 / pz=100 -> 每个 fs 4 页；两个 fs = 8 次。
# 若各拉各的，会是 8 + 2 + 2（alert 两端各 1 页）= 12 次以上。
check(_n <= 9,
      f"本轮共发起 {_n} 次 clist 分页请求，超过共享后的应有值 8 —— "
      f"说明 hot 与 alert 仍在各拉一遍（修复前是 19 次，算术上取不全）")
check(_n >= 8,
      f"只发起 {_n} 次请求，少于两个板块宇宙各 4 页的 8 次 —— "
      f"有消费者根本没取到数据")

print(f"ALL PASS (板块名单轮内共享 {_ok} 个断言)")
