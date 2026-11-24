#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""firecrawl 必须节流：额度 11 req/min，而一轮扫描突发 19 次请求。

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


# ---------- ①② 窗口配额的上下界 ----------
# ⚠️ 判据改过（2026-11-24 第二次修）：第一版是**固定间隔 5.5s**
# = 10.9 req/min，而上限就是 **11/min** —— 余量 0.1 次，等于没有。
# 上线后 12:58 仍然限流，板块列表照旧停在 200/496。
# 现在改成**滑动窗口**：60s 内最多 FC_MAX_PER_WINDOW 次。
_w = M.FC_WINDOW
_n = M.FC_MAX_PER_WINDOW
check(_w == 60.0,
      f"窗口长度 {_w}s 不是 60s —— firecrawl 的额度是**按分钟**算的，"
      f"窗口与它不同源就没有意义")
_cap_seen = 11          # 限流原文 Consumed 11 / Remaining 0
check(_n < _cap_seen,
      f"窗口上限 {_n}/min 不小于实测上限 {_cap_seen} —— 余量为零，"
      f"抖动或重试就会越界（这是第一版栽的地方）")
_margin = (_cap_seen - _n) / _cap_seen
check(_margin >= 0.15,
      f"余量只有 {_margin:.0%}，低于 15%：`feeds.py` 那边还有一条"
      f"**独立的** firecrawl_get 调用点，加上 clist 重试，15% 是下限")
check(_n >= 6,
      f"窗口上限 {_n}/min 太低：一轮最坏 19 页要等 "
      f"{(19 // _n + 1) * 60}s，会挤占 DATA_BUDGET")

# ---------- ③ 行为：**直接驱动生产代码**的 `_fc_pace()` ----------
# ⚠️⚠️ 这一段第一版写的是"在测试里重新实现一遍排票算法再模拟" ——
# 那是自欺：**改生产代码它不会红**。实测确认过，把 `_fc_pace` 换成
# 固定间隔后，模拟照样全绿。
# 现在改成把窗口压到亚秒级（0.6s / 2 次），**真的调用** `_fc_pace()`
# 并量每次的真实返回时刻。这样测的就是生产里跑的那份代码。
import tempfile as _tf
_pace2 = os.path.join(_tf.mkdtemp(), "pace_sim.json")
_orig_file = M._FC_PACE_FILE
_orig_win, _orig_max = M.FC_WINDOW, M.FC_MAX_PER_WINDOW
try:
    M._FC_PACE_FILE = _pace2
    M.FC_WINDOW = 0.6
    M.FC_MAX_PER_WINDOW = 2
    _real = []
    for _k in range(6):
        _t0 = time.monotonic()
        M._fc_pace()
        _real.append(time.monotonic() - _t0)      # 本次**实际等待**秒数

    # ① 前 2 次不得等待（滑动窗口相对固定间隔的核心优势）
    check(_real[0] < 0.15 and _real[1] < 0.15,
          f"前 2 次调用的等待是 {[round(x, 2) for x in _real[:2]]}，"
          f"未满窗口却在拖慢 —— 滑动窗口退化成了固定间隔")
    # ② 第 3 次必须等（窗口已满）
    check(_real[2] >= 0.3,
          f"窗口已满（2/0.6s）但第 3 次只等了 {_real[2]:.2f}s —— 没拦住，"
          f"真实上限 11/min 会被打穿")
    # ③ 任意 0.6s 窗口内不超过 2 次（用真实时刻验，不是模拟）
    _ts = M._load_json(_pace2, {}).get("ts") or []
    check(len(_ts) <= 2,
          f"排票后窗口里记着 {len(_ts)} 个时刻，超过上限 2 —— "
          f"文件本身就没守住")
finally:
    M._FC_PACE_FILE = _orig_file
    M.FC_WINDOW, M.FC_MAX_PER_WINDOW = _orig_win, _orig_max

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
# 合并后频率翻倍）。
# 窗口上限是 9/min，两个进程远没到上限 —— 所以这里验的不是"会等"，
# 而是**共享同一个窗口文件**：两个进程各调一次后，文件里必须记着 2 次。
_pace = os.path.join(tempfile.mkdtemp(), "pace.json")

CODE = r'''
import json, sys
sys.path.insert(0, {src!r})
import marketlib as M
M._FC_PACE_FILE = {pace!r}
M._fc_pace()
print(M._load_json({pace!r}, {{}}).get("ts"), flush=True)
'''

_seen = []
for _ in range(2):
    _r = subprocess.run([sys.executable, "-c",
                         CODE.format(src=os.path.join(
                             os.path.dirname(os.path.abspath(__file__)),
                             "..", "src"), pace=_pace)],
                        capture_output=True, text=True, timeout=120)
    check(_r.returncode == 0,
          f"子进程跑窗口配额失败：{(_r.stderr or '')[:120]}")
    _seen.append(len(M._load_json(_pace, {}) or {}))

check(_seen[-1] == 1, "窗口文件在子进程里写不出来 —— 跨进程配额无从谈起")
_ts = M._load_json(_pace, {}) or {}
_ts = _ts.get("ts")
check(isinstance(_ts, list) and len(_ts) == 2,
      f"两个独立进程各调一次后，窗口里应记着 2 次，实得 {_ts!r} —— "
      f"跨进程共享没生效（各记各的，合并后频率翻倍）")

print(f"ALL PASS (firecrawl 跨进程节流 {_ok} 个断言)")
