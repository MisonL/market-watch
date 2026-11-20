#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数阶段的**全局**时间上限（2026-11-20，P1）。

## 它治的那个结构性缺陷

`AI_ROUND_BUDGET`(240s) 是**减去取数耗时**再给 AI 的：

    _budget = AI_ROUND_BUDGET − (取数耗时 + 已用 AI 耗时)

而取数阶段原先**没有总数**，只有各数据源自己的预算，三者可叠加：

    hot.fetch_sectors 的 clist   60s
    hot.MEMBERS_BUDGET           40s
    hot.VR_BUDGET                80s
    ─────────────────────────── 180s

取数一旦吃到 172s，AI 只剩 68s —— 而成功轮 AI 耗时的 P90 是 140s、
最长 210s，**结构性必死**。`[实测]` 11-18 14:54（取数 172s / AI 上限
68s / 退模板）、11-19 09:23（108s / 132s / 退模板）都是这个形态。

近 20 天 986 轮 `data_ms`：P50=0.1s P90=13.6s P99=75s 最大 171.8s；
>60s 只占 1.6%。故 120s 作硬上界 —— 截掉长尾，P99 的正常轮不受影响。

## 本文件锁住什么

① `data_cap` 的四种组合（自身／全局／取小／都为 None）
② **默认不生效**：没调 `set_data_deadline` 时 `data_cap(60)` 仍是 60
   （否则等于给所有数据源平白加了 120s 上限，改了原有语义）
③ 钳到了必须**留痕**（每源每轮一次）—— 降级不可见是本仓库反复的缺陷
④ 三处接线真的接上了（clist / members / 量比），不是加了函数没人调
⑤ `analyze.run()` 每轮都会设截止，且用的是本轮起点 `_t0`
"""
import os
import sys
import time
import tempfile
import atexit
import shutil
import ast

_d = tempfile.mkdtemp(prefix="mw_t_dbud_")
os.environ["MW_DIR"] = _d
atexit.register(shutil.rmtree, _d, ignore_errors=True)
_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _src)

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_logs = []
M.log = lambda m, tag="": _logs.append(m)

# ---------- ② 默认不生效（最重要的一条）----------
M.set_data_deadline(None)
_ok_before = _ok
check(M.data_left() is None, "未设截止时 data_left() 应为 None")
check(M.data_cap(60) == 60,
      f"未设截止时 data_cap(60) 必须是 60（不限制），实得 {M.data_cap(60)}")
check(M.data_cap(None) is None, "未设截止时 data_cap(None) 应为 None")
check(not _logs, f"未设截止时不该记任何日志，实得 {_logs}")

# ---------- ① 四种组合 ----------
M.set_data_deadline(time.time() + 30)
check(abs(M.data_cap(60) - 30) < 2,
      f"全局 30s 更紧时应取 30，实得 {M.data_cap(60)}")
M.set_data_deadline(time.time() + 90)
check(abs(M.data_cap(60) - 60) < 2,
      f"自身 60s 更紧时应取 60，实得 {M.data_cap(60)}")
M.set_data_deadline(time.time() + 45)
check(abs(M.data_cap(None) - 45) < 2,
      f"自身 None 时应用全局 45，实得 {M.data_cap(None)}")
# 已超时：返回负数（刻意不钳到 0 —— 调用方自己加下限，
# 钳成 0 会让 budget_s=0 变成"一页都不翻"，连已到手的第一页都拿不到）
M.set_data_deadline(time.time() - 5)
check(M.data_cap(60) < 0,
      f"已超时时应返回负值（由调用方加下限），实得 {M.data_cap(60)}")

# ---------- ③ 钳到必须留痕，且每源每轮只记一次 ----------
_logs.clear()
M.set_data_deadline(time.time() + 10)
for _ in range(3):
    M.data_cap(80, "量比")
_cap_logs = [x for x in _logs if "量比" in x]
check(len(_cap_logs) == 1,
      f"同一数据源一轮内只该记 1 条降级日志，实得 {len(_cap_logs)}")
check("取数全局上限生效" in _cap_logs[0],
      f"降级日志要能被 grep（含「取数全局上限生效」），实得 {_cap_logs[0]!r}")
M.data_cap(80, "members")
check(len([x for x in _logs if "members" in x]) == 1,
      "不同数据源应各自留痕")
# 新一轮（重设截止）后去重集清空
_logs.clear()
M.set_data_deadline(time.time() + 10)
M.data_cap(80, "量比")
check(len(_logs) == 1, f"新轮次应重新留痕，实得 {_logs}")

# 没被钳（自身更紧）时**不该**留痕 —— 否则日志噪音淹没真降级
_logs.clear()
M.set_data_deadline(time.time() + 999)
M.data_cap(60, "clist")
check(not _logs, f"未被钳时不该留痕，实得 {_logs}")

M.set_data_deadline(None)

# ---------- ④ 三处接线（读生产源码，不是读副本）----------
_tree_ml = ast.parse(open(os.path.join(_src, "marketlib.py"),
                          encoding="utf-8").read())
_tree_hot = ast.parse(open(os.path.join(_src, "hot.py"),
                           encoding="utf-8").read())
_tree_an = ast.parse(open(os.path.join(_src, "analyze.py"),
                          encoding="utf-8").read())


def _calls_in(tree, names):
    """返回树里所有 `X.name(...)` / `name(...)` 的 attr/id 名单。"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute):
                out.append(n.func.attr)
            elif isinstance(n.func, ast.Name):
                out.append(n.func.id)
    return [x for x in out if x in names]


_ml_caps = _calls_in(_tree_ml, {"data_cap"})
_hot_caps = _calls_in(_tree_hot, {"data_cap"})
_an_sets = _calls_in(_tree_an, {"set_data_deadline"})

check(_ml_caps, "marketlib.clist 没接 data_cap —— 全局上限对板块列表无效")
check(len(_hot_caps) >= 2,
      f"hot.py 应有 ≥2 处 data_cap（members + 量比），实得 {len(_hot_caps)}")
check(_an_sets, "analyze.run() 没调 set_data_deadline —— 全局上限永不生效")

# ⑤ run() 设截止必须用 _t0（与本轮耗时同源），不能重新取时钟
_found_t0 = False
for _n in ast.walk(_tree_an):
    if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
            and _n.func.attr == "set_data_deadline"):
        _txt = ast.unparse(_n)
        if "_t0" in _txt and "DATA_BUDGET" in _txt:
            _found_t0 = True
check(_found_t0,
      "set_data_deadline 必须以 `_t0 + DATA_BUDGET` 调用（与 _ROUND_DATA_MS "
      "同源，否则「取数没超预算」这句话会自相矛盾）")

# ⑥ 常量本身要覆盖实测 P99（75s）又不至于形同废除
check(90 <= M.DATA_BUDGET <= 180,
      f"DATA_BUDGET={M.DATA_BUDGET} 不在合理区间 [90,180]："
      f"太小会截掉正常轮次（P99=75s），太大等于没设")

print(f"ALL PASS (取数全局上限 {_ok} 个断言)")
