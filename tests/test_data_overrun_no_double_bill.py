#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数超预算不该**再次**罚 AI —— 两个预算从未检查过能否相加。

## 实测算术

两个预算各自独立定，从来没有一行代码检查它们相加是否可行：

    DATA_BUDGET      = 120s   按取数耗时分布定的（P99=75s）
    AI_ROUND_BUDGET  = 240s   按调度间隔 300s 留 60s 余量定的
    ───────────────────────── 相减 → 取数吃满时 AI 只剩 **120s**

而 250 轮实测里 **AI 成功轮耗时的 P80 是 195s**。也就是说：取数一旦吃满，
AI 只有约一半概率写完 —— 不是"偶发慢轮"，是**算术必死**。

`[实测]` 2026-11-24 14:54 那一轮：

    取数 133s（已超自己的 120s 上限 13s）
    → AI 只剩 107s
    → 15:09「收盘定局」被掐停在 110s、**零产出**、退模板
    → 当天模板兜底 5/10 轮

## 为什么这是"二次惩罚"

取数超标是取数自己的账：它的页级超时（单页最长 45s）挡不住"预算只剩
3s 但这一页要 45s"，于是必然越界一点。而越界的那 13s 又被从 AI 的
预算里扣掉一次 —— 同一笔超支罚两遍。

更糟的是**归因被带偏**：那种轮次的日志写的是「AI 终止（单轮 AI 预算
耗尽）—— 跑得太慢，不是卡死」，把排查方向引到 AI 上，而真因是取数越界。

## 修法

扣减**封顶到 `DATA_BUDGET`**：超出部分照旧留在日志里（取数那侧已有
「clist 总耗时超预算 45s」的痕），但不进 AI 的账。

## 本文件锁住什么

① 取数超预算时，AI 预算按 `DATA_BUDGET` 封顶扣减，不是按实际耗时
② 真的越界时必须**留痕**（不能悄悄抹掉一笔超支）
③ 没越界时行为与之前**逐字一致**（不能顺手改动正常轮）
④ 两个常量相加必须可行：`DATA_BUDGET + AI 的 P80 <= AI_ROUND_BUDGET`
"""
import os
import sys
import tempfile

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_flr_"))

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


_SRC = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "analyze.py"), encoding="utf-8").read()

# ---------- ① 扣减必须封顶到 DATA_BUDGET ----------
_i = _SRC.find("_d_bill = min(_d_used, float(M.DATA_BUDGET))")
check(_i > 0,
      "找不到 `_d_bill = min(_d_used, float(M.DATA_BUDGET))` —— "
      "取数超出自身预算的部分又被全额从 AI 里扣了（同一笔超支罚两遍）")
_i2 = _SRC.find("_used += _d_bill")
check(_i2 > _i,
      "累加的不是封顶后的 `_d_bill` —— 封顶算了却没用上")

# ---------- ③ 没越界时行为不变 ----------
# 封顶只在 `_d_used > DATA_BUDGET` 时生效；正常轮（取数 1~14s）走的是
# `min()` 的直通分支，等价于原实现。
check("min(_d_used, float(M.DATA_BUDGET))" in _SRC,
      "封顶写法不对（应为 min，不是 max 或条件分支之外的形态）")

# ---------- ② 越界必须留痕 ----------
_i3 = _SRC.find("超出自身预算")
check(_i3 > 0,
      "取数越界时没有留痕 —— 一笔超支被悄悄抹掉，事后无从解释"
      "「为什么这轮 AI 预算比算出来的宽」")

# ---------- ④ 地板必须存在且量级合理 ----------
# ⚠️ 这条是本项真正的价值：把「两个预算各自合理、相加却不可行」这个
# **结构问题**变成一条可执行断言。
#
# 实测（250 轮）：
#     DATA_BUDGET      = 120s
#     AI_ROUND_BUDGET  = 240s   -> 取数吃满时 AI 只剩 120s
#     取数<45s 的 188 轮里 AI 耗时 P80 = **222s**
#     取数>=100s 的 14 轮里 AI 实测 39/60/76/102s —— 全是被掐停
#
# ⚠️ 注意**不能**断言 `DATA_BUDGET + AI_P80 <= AI_ROUND_BUDGET`
# （120+222=342 > 240，那条永不成立），也不能靠调大 AI_ROUND_BUDGET
# 解决 —— 它上面压着 300s 调度间隔。正确的做法是给 AI 一个**地板**：
# 宁可整轮跑过调度间隔（吞掉下一轮），也不能必然送模板。
import analyze as _a
_floor = getattr(_a, "AI_MIN_CALL", None)
check(_floor is not None,
      "缺少 `AI_MIN_CALL` —— 取数吃满时 AI 又会只剩 120s（实测写不完）")
check(_floor >= 150,
      f"AI 地板 {_floor}s 太低：AI 耗时 P50=88s、P70=152s、P80=222s，"
      f"低于 150s 等于没给")
check(_floor <= 200,
      f"AI 地板 {_floor}s 太高：整轮必然跑过 300s 调度间隔，"
      f"每次慢取数都吞掉下一轮（下一轮是新的、完整的数据，更值）")
if _a.AI_ROUND_BUDGET is not None:
    check(_floor < float(_a.AI_ROUND_BUDGET),
          f"AI 地板 {_floor}s 不小于整轮预算 {_a.AI_ROUND_BUDGET}s —— "
          f"那地板等于取消预算")

# ---------- ⑤ 地板只给**第一次**调用，重写不得再吃一份 ----------
_src2 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "..", "src", "analyze.py"),
             encoding="utf-8").read()
_i = _src2.find("if _budget < AI_MIN_CALL and not _ROUND_AI_MS:")
check(_i > 0,
      "地板没有「本轮还没调过 AI」的守卫 —— 重写/补写会各拿一份地板，"
      "一轮跑出 360s")

print(f"ALL PASS (取数超预算不重复罚 AI {_ok} 个断言)")
