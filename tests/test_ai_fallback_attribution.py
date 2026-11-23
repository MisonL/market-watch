#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""自检「模板兜底」的归因不能一律怪取数。

## 为什么（2026-11-23 生产实测）

当日 10 轮真调 AI，7 轮被掐停、读者全程拿到模板文案。自检原文写：

    掐停原因以**我方单轮预算**为主（budget×7）：AI 通道没坏，是
    「全轮预算 240s − 取数耗时」把它压没了
    处置：查该轮 `data_ms`（取数吃了多少）…… **不要去查网关**

但把 `runs.jsonl` 里那 7 轮的 `data_ms` 拉出来看：

    1.3s  3.2s  23.6s  31.9s  55.2s  74.2s  122.5s

**只有最后 1 轮（122.5s，占 51%）是真的被取数压的。** 前 5 轮取数只占
0.6%~23%，AI 手里有 185~239s 预算，**照样超时**。照那条提示去查 `data_ms`，
查到的是 1.3 秒 —— 什么线索都没有，而真正该看的「这一轮 AI 为什么跑了
240 秒还没写完」被彻底掩盖。

这正是本仓库反复修的「判据写在注释里、没写在代码里」：
注释里写着"预算 = 240 − 取数"，于是代码**假定**预算不够就是取数造成的，
从来没有回头看 `data_ms` 到底是多少。

## 本文件锁住什么

`_ai_budget_vs_data(data_ms_list, round_budget)` 把 budget 掐停的轮次再分成
`(n_data_bound, n_ai_bound, n_unknown)`：
  · 取数占预算 >= 50% -> data_bound（怪取数，公允）
  · 取数占预算 <  40% -> ai_bound  （怪 AI 自己，别再让人去查 data_ms）
  · 两者之间 / 无记录 / 脏值 -> unknown

并锁住自检**真的按这个分流改措辞**（接线断言），而不只是函数写对了。
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC)

import selfcheck as S

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


B = 240.0

# ---------- ①今日真实数据：6 轮该怪 AI，1 轮该怪取数 ----------
_d, _a, _u = S._ai_budget_vs_data(
    [1321, 3168, 23600, 31943, 55159, 74234, 122499], B)
check((_d, _a, _u) == (1, 6, 0),
      f"今日真实数据应判成 1 轮取数 + 6 轮 AI，实得 "
      f"取数={_d} AI={_a} 未知={_u}")
check(_a > _d,
      "今日的 `n_ai_bound` 必须大于 `n_data_bound` —— 否则自检仍会"
      "让人去查只有 1.3 秒的 `data_ms`")

# ---------- ②全场取数吃满 ----------
check(S._ai_budget_vs_data([130000, 150000], B) == (2, 0, 0),
      "取数 130s/150s（占 54%/62%）应全判 data_bound")

# ---------- ③全场 AI 自身慢 ----------
check(S._ai_budget_vs_data([1000, 2000, 3000], B) == (0, 3, 0),
      "取数 1~3s 却超时，应全判 ai_bound")

# ---------- ④边界：40% 与 50% 之间的灰区不强行归类 ----------
# 96s = 40%（不进 ai_bound）、120s = 50%（进 data_bound）、
# 中间的空档留给 unknown，避免卡在阈值上的轮次来回跳。
check(S._ai_budget_vs_data([96000, 120000], B) == (1, 0, 1),
      "40%/50% 的边界划分变了（灰区应归 unknown）")

# ---------- ⑤脏输入 ----------
check(S._ai_budget_vs_data([], B) == (0, 0, 0), "空输入应回 (0,0,0)")
check(S._ai_budget_vs_data(["x", None, -5], B) == (0, 0, 3),
      "脏值（字符串/None/负数）应全部计入 unknown，不能崩也不能瞎归")
check(S._ai_budget_vs_data([1000], None) == (0, 0, 1),
      "预算未知（None）时应归 unknown —— AI_ROUND_BUDGET 允许为 None")
check(S._ai_budget_vs_data(None, B) == (0, 0, 0),
      "list 为 None 时应回 (0,0,0)")

# ---------- ⑥接线断言：措辞必须真的分两套 ----------
# ⚠️ 只测函数不够 —— 本仓库的经典失败形态是「函数对、接线断」：
# 函数算出了 ai_bound，主流程却仍用旧文案，用户看到的还是"去查 data_ms"。
_src = open(os.path.join(_SRC, "selfcheck.py"), encoding="utf-8").read()
check("_ai_budget_vs_data(" in _src and
      _src.count("_ai_budget_vs_data(") >= 2,
      "`_ai_budget_vs_data` 只有定义、没有调用点 —— 判据没接进 [模板兜底]")
check("**取数不是主因**" in _src,
      "缺少「取数不是主因」这条分支的措辞 —— ai_bound 的结论没落到文案上")
check("**不要去查 `data_ms`" in _src,
      "ai_bound 分支没有明确劝阻去查 `data_ms` —— 那正是走错方向的那一步")
check("查该轮 `data_ms`（取数吃了多少）" in _src,
      "data_bound 分支原有的正确指引被删掉了 —— 不要把对的也一起改没")

# ---------- ⑦分流必须有真分支（不是恒走同一条） ----------
try:
    _tree = ast.parse(_src)
except SyntaxError as _e:
    check(False, f"selfcheck.py 语法错误：{_e}")
    raise SystemExit(1)
_found = False
for _node in ast.walk(_tree):
    if isinstance(_node, ast.If):
        _seg = ast.dump(_node.test)
        if "ai_budget_vs_data" in _seg or "_ab_dl" in _seg:
            _found = True
            break
check(_found,
      "没有找到 `if _ab_dl > _db_dl:` 这类真分支 —— 判据算出来了却没用于分流")

print(f"ALL PASS (模板兜底归因分流 {_ok} 个断言)")
