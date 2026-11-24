#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板块取数的预算分配：每个 fs 各给固定值 → 先跑的吃满、后跑的连排队都不够。

## 2026-11-24 实测

`hot.fetch_sectors()` 与 `alert._sector_rows()` 共用同一份板块名单后，
两个 fs（行业 t:2 / 概念 t:3）的取数**串行排队**：

    14:45:08  t:2 取到 494/496（用满自己那 45s）
    14:45:55  t:3 只取到 **100/554**（45s 全花在等配额上，只成了 1 页）

单页 firecrawl 要 **5~8 秒**，而配额窗口保证两次调用至少隔 6.7s ——
6 页的概念板块**光排队就要 40 秒**。两个 fs 合计 11 页 ≈ 73s，
**全局 `DATA_BUDGET`=120s 是够的**。

缺的不是总预算，是**分配**：每个 fs 各拿固定 45s，先跑的吃满了、
后跑的连排队都不够。改成用「全局剩余」（`data_left()`）后，
两个 fs 自然按实际需要分配、总和不超 120s。

## 顺带修的两个

① `data_cap` 的「被压到」日志有**亚秒差假告警**：
   `sector_rows` 先取 `data_left()` 当 own_s，`clist` 再取一次，
   中间隔几十毫秒 → 每次都「自身预算 120s 被压到 120s（已用 0s）」。
   一条**自相矛盾**的告警，每轮都刷 = 告警疲劳。加 1s 容差。

② `budget_s = data_left() or 60.0` —— 剩余**真的是 0** 时 `or` 会
   把它变成 60s，**凭空多给一分钟**（预算闸门被绕过）。
   只有"没有全局 deadline"（返回 None）才该退回 60s。

## 本文件锁住什么

① `sector_rows` 的预算默认值必须来自 `data_left()`，不是固定常数
② 剩余为 0 时不得退化成 60s（`or` 的陷阱）
③ `data_cap` 的日志容差：亚秒差不算"压"
④ 变异体 `data-budget-cap-silent` 的锚点与源码保持一致
"""
import os
import re
import sys
import tempfile
import time

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_budget_"))

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


_SRCPY = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "..", "src", "marketlib.py"),
              encoding="utf-8").read()

# ---------- ① 预算来自全局剩余，不是固定常数 ----------
_i = _SRCPY.find("def sector_rows(")
# ⚠️ 边界原来是 `def clist_extremes(` —— 那个函数已被删除（它在改用全量
# 共享名单后成了死代码），于是 `_j = -1`，整段断言变成「找不到定义」。
# 改成按**下一个顶层 def** 切：不依赖具体是哪个函数，删/加都不影响。
import re as _re
_m = _re.search(r"\ndef [a-zA-Z_(]", _SRCPY[_i + 10:])
_j = (_i + 10 + _m.start() + 1) if _m else len(_SRCPY)
check(_i > 0 and _j > _i, "找不到 sector_rows 的定义")
_body = _SRCPY[_i:_j]
check("data_left()" in _body,
      "`sector_rows` 没有用 `data_left()` —— 又是每个 fs 固定预算，"
      "先跑的吃满、后跑的连排队都不够（实测 t:3 只取到 100/554）")
# ⚠️ 「退回固定 45」要查的是**调用方**，不是 `sector_rows` 的默认值。
# 默认值改成 `None` 之后，调用方写 `budget_s=45` 会**覆盖**它，
# 而源码里看不出任何异常 —— 我第一版只查 `sector_rows` 函数体，
# 把 `alert.py` 改回 `budget_s=45` 时**测不出来**（实测确认）。
for _caller in ("alert.py", "hot.py"):
    _csrc = open(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src", _caller),
        encoding="utf-8").read()
    _cl = "\n".join(_ln for _ln in _csrc.splitlines()
                     if not _ln.lstrip().startswith("#"))
    check(not re.search(r"sector_rows\([^)]*budget_s\s*=\s*\d", _cl),
          f"{_caller} 调用 `sector_rows` 时传了**固定的** budget_s —— "
          f"会覆盖按全局剩余的分配，先跑的 fs 又吃满、后跑的连排队都不够")

# ---------- ② 剩余为 0 不得退化成 60s ----------
# ⚠️ 判据必须**跳过注释**：`data_left() or 60.0` 现在恰好出现在解释
# 「为什么不能用它」的注释里。判据直接找字符串，测的就是注释。
_body_code = "\n".join(
    _ln for _ln in _body.splitlines() if not _ln.lstrip().startswith("#"))
check("data_left() or 60.0" not in _body_code,
      "用了 `data_left() or 60.0`（**代码**，不是注释）—— 剩余真的是 0 时"
      "会变成 60s，凭空多给一分钟，预算闸门被绕过")

# 行为验证：真的把剩余设成 0，看预算是不是 0 而非 60
_orig_dl = M.data_left
try:
    M.data_left = lambda: 0.0
    _b = None
    _orig_clist = M.clist

    def _spy_clist(fs, fields, **kw):
        global _b
        _b = kw.get("budget_s")
        return ([], None)
    M.clist = _spy_clist
    try:
        M.sector_round_reset()
        M.sector_rows("m:90+t:2")
    finally:
        M.clist = _orig_clist
    check(_b is not None, "`sector_rows` 没把预算传给 `clist`")
    check(_b <= 1.5,
          f"剩余预算为 0 时，传给 clist 的 budget_s 是 {_b} —— "
          f"应被钳到最小（1s），不是退回 60s")
finally:
    M.data_left = _orig_dl
    M.sector_round_reset()

# ---------- ③ 亚秒差不算「被压」 ----------
M.set_data_deadline(time.time() + 100)
try:
    _logs = []
    _orig_log = M.log
    M.log = lambda m, tag="": _logs.append(str(m))
    try:
        # 剩 ~100s，自身预算 100s -> 亚秒差，不该报"被压"
        M.data_cap(100.0, "probe_subsec")
    finally:
        M.log = _orig_log
    check(not any("被压到" in x for x in _logs),
          f"亚秒差被报成「全局上限生效」：{_logs} —— "
          f"会刷出「自身预算 120s 被压到 120s」这种自相矛盾的告警")
    # 差距明显时必须报
    _logs2 = []
    M.log = lambda m, tag="": _logs2.append(str(m))
    try:
        M.data_cap(9999.0, "probe_big")
    finally:
        M.log = _orig_log
    check(any("被压到" in x for x in _logs2),
          f"真的被压掉很多却没留痕：{_logs2} —— "
          f"降级必须留痕，否则事后只能靠猜")
finally:
    M.set_data_deadline(None)
    M._DATA_CAPPED.clear() if hasattr(M, "_DATA_CAPPED") else None

# ---------- ④ 变异体锚点同步 ----------
_MUT = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "selfcheck_mutation.py"),
            encoding="utf-8").read()
_i = _MUT.find('("data-budget-cap-silent"')
check(_i > 0, "找不到变异体 data-budget-cap-silent")
_seg = _MUT[_i:_i + 400]
check('_cap < own_s - 1.0' in _seg,
      "变异体的注入锚还没跟源码的 1s 容差同步 —— "
      "锚点漂移会让这条变异体**永远注入不进去**，"
      "而它的失效形态是静默的（自检报 0 失配但实际没测）")

print(f"ALL PASS (取数预算按全局剩余分配 {_ok} 个断言)")
