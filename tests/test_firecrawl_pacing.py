#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firecrawl 必须节流：额度 12 req/min，而一轮扫描突发 11~19 次请求。

## 2026-11-24 实测的账

限流原文（`logs/watch.log` 里 firecrawl CLI 的 stderr）：

    Consumed (req/min): 11, Remaining (req/min): 0

而一轮完整扫描需要：

    行业 m:90+t:2   496 个 / pz=100 硬上限 = 5 页
    概念 m:90+t:3   554 个 / 100           = 6 页
    clist_extremes  两方向各 ≤2 页         = ≤8 页
    合计                                    11~19 次

这些请求是**背靠背**发出的 —— `firecrawl_get` 里一次 `time.sleep` 都没有。
于是第 12 次撞上限 → 套 60s 限流冷却 → 本轮剩下的分页全部在 0.0s 内空回。

当日后果：22 次限流、84 个 clist 分页被直接放弃，
板块列表长期停在 **200/496**（只取到前 2 页）。按 f62 降序时缺的正是
**排在最后的那些** —— 也就是流出最猛的板块。

## 关键判断

根因不是"总量太大"（300s 一轮 vs 12 req/min，总量绰绰有余），
而是**瞬间太密**。所以修法是**打散**，不是砍页数。

## 本文件锁住什么

① 间隔必须压在上限之下：`60 / FIRECRAWL_MIN_INTERVAL <= 12`
② 间隔不能大到把 DATA_BUDGET（120s）撑爆：11 次 ≤ 120s
③ **跨进程**有效（两个进程共享同一个时间戳文件）——
   生产里 marketwatch 与 marketcollect 是两条独立进程
④ 接线：`firecrawl_get` 真的调了节流，且**在可用性闸门之后**
   （冷却期与缺二进制的分支已提前返回，不该白等）
⑤ 只对真的走 firecrawl 的调用计费：直连成功不占额度、不用等
"""
import os
import subprocess
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


# ---------- ①② 间隔的上下界 ----------
_iv = M.FIRECRAWL_MIN_INTERVAL
_per_min = 60.0 / _iv
check(_per_min <= 11.5,
      f"间隔 {_iv}s 折算 {_per_min:.1f} req/min，超过 firecrawl 上限 12"
      f"（实测原文 Consumed 11 / Remaining 0）—— 仍会限流")
check(_per_min >= 6.0,
      f"间隔 {_iv}s 折算 {_per_min:.1f} req/min 太保守："
      f"11 页要等 {11 * _iv:.0f}s，会挤占取数预算")
# 完整扫描最坏情形（11 页全走兜底）必须仍在 DATA_BUDGET 之内
_DB = getattr(M, "DATA_BUDGET", 120)
check(11 * _iv <= _DB,
      f"11 页全走兜底需 {11 * _iv:.0f}s，超过 DATA_BUDGET={_DB}s —— "
      f"后半段必然被预算切断，等于没取全")

# ---------- ④ 接线 ----------
import inspect
_src = inspect.getsource(M.firecrawl_get)
check("_fc_pace()" in _src,
      "`firecrawl_get` 没有调用 `_fc_pace()` —— 写了不用等于没有")
_i_pace = _src.index("_fc_pace()")
_i_cool = _src.index("_fc_fail_get()")
_i_bin = _src.index("_FIRECRAWL_NODE")
check(_i_pace > _i_cool and _i_pace > _i_bin,
      "节流排在冷却/可用性闸门**之前** —— 冷却期里会白等每一页")

# ---------- ③ 跨进程真的生效 ----------
# ⚠️ 必须用**两个独立进程**验，同一进程内的两次调用只能证明进程内节流
# （而这正是 2026-11-10 在请求限流上踩过的坑：进程内变量各记各的，
# 合并后频率翻倍）。这里让两个子进程先后打同一个 pace 文件，量总间隔。
_pace = os.path.join(tempfile.mkdtemp(), "pace.json")
_open, _atomic = M._open_lockfile, M._atomic_write_json

CODE = r'''
import sys, time
sys.path.insert(0, {src!r})
import marketlib as M
M._FC_PACE_FILE = {pace!r}
M._fc_pace()
print(time.time(), flush=True)
'''

_t = []
for _ in range(2):
    _r = subprocess.run([sys.executable, "-c",
                         CODE.format(src=os.path.join(
                             os.path.dirname(os.path.abspath(__file__)),
                             "..", "src"), pace=_pace)],
                        capture_output=True, text=True, timeout=120)
    check(_r.returncode == 0,
          f"子进程跑节流失败：{(_r.stderr or '')[:120]}")
    _t.append(float(_r.stdout.strip()))

_gap = _t[1] - _t[0]
check(_gap >= M.FIRECRAWL_MIN_INTERVAL * 0.85,
      f"两个**独立进程**的 firecrawl 调用间隔只有 {_gap:.2f}s，"
      f"低于 {M.FIRECRAWL_MIN_INTERVAL}s —— 跨进程节流没生效"
      f"（进程内各记各的，合并后频率翻倍）")

print(f"ALL PASS (firecrawl 跨进程节流 {_ok} 个断言)")
