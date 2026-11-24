#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""量比预算耗尽时，「没算过」不能印成「量比1.0」。

## 实测缺陷

`[实测]` 2026-11-24 15:09「收盘定局」的生产日志与推送**逐字对读**：

    日志：量比预算 80s 用尽，已算 0/32 个，其余按基线 1.0 处理（下轮续算）
    推送：文娱用品 +3.04% 60日位置未知 **量比1.0** 主力占比+7.3%

`1.0` 是 `_num("-", 1.0)` 的兜底常数 —— 读者会把它读成「成交正常」。

## 两处成因，都是「判据在、没生效」

① **`_vr_ok` 压根没写**。它只在走完 `kline_stats` 那一趟时才赋值；而
   预算一耗尽，`for r in head` 一进门就 `break`（`vr_done=0`），整批 head
   行**没有这个键**。渲染层写的是 `r.get('_vr_ok', True)`（默认**真**），
   于是「没有这个键」= 「量比可信」。

② **补标记的循环是空操作**：

       for r in head[vr_done:]:
           if "vr" not in r:        # ← 恒为假
               r["vr"] = 1.0
               r["_vr_ok"] = False

   `vr` 在**构造时**就写进每一行了（`vr=_num(x.get("f10"), 1.0)`），
   所以条件永远不成立，循环一次都没执行过。一道写着「不能冒充真实 1.0」
   的守卫，实际什么都不做 —— 正是本仓库反复修的
   「判据写在注释里、没写在代码里」。

## 修法（两层，缺一不可）

· 构造时 `_vr_ok=False` —— 诚实的默认值：**没算过就是没算过**；
· 渲染默认值 `.get('_vr_ok', False)` —— 纵深防御：即使某行丢了键
  （旧缓存、上游换实现），也退到「未知」而不是「可信」。

## 本文件锁住什么

① 预算耗尽 → head 行有 `_vr_ok`，且为 False
② 预算充足 → 算出值的行 `_vr_ok` 为 True（不能一刀切全标未知）
③ 空守卫 `if "vr" not in r:` 不复存在
④ **行为级**：`build_brief` 对 `_vr_ok=False` 的行印「量比未知」
⑤ **行为级**：行**完全没有** `_vr_ok` 键时，也必须印「量比未知」
   （这条专门钉住渲染默认值 —— 改回 True 就红）
"""
import os
import sys
import tempfile

os.environ.setdefault("MW_DIR", tempfile.mkdtemp(prefix="mw_t_vrok_"))

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _SRC_DIR)

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


def _strip_comments(src):
    out = []
    for ln in src.split("\n"):
        out.append(ln.split("#", 1)[0])
    return "\n".join(out)


# ---------- ③ 空守卫必须消失 ----------
_src_hot = open(os.path.join(_SRC_DIR, "hot.py"), encoding="utf-8").read()
_code_hot = _strip_comments(_src_hot)
check('if "vr" not in r:' not in _code_hot,
      "`head[vr_done:]` 的补标记循环仍在用 `if \"vr\" not in r:` —— "
      "`vr` 在构造时就写进每一行了，这个条件**恒为假**，"
      "整段等于空操作（预算耗尽时 `_vr_ok` 永远补不上）")
check('r["_vr_ok"] = False' in _code_hot,
      "补标记循环里没有 `r[\"_vr_ok\"] = False` —— 没算到的行仍会被当成可信")

# ---------- ① / ② 行为级：真跑 hot.analyze ----------
import hot  # noqa: E402

_RAW = [{"f12": "BK%04d" % i, "f14": "板块%d" % i, "f3": 1.0, "f24": -20.0,
         "f6": 5e8, "f184": 3.0, "f69": 1.0, "f8": 1.0, "f62": 1e8,
         "f104": 50, "f105": 10, "f128": "龙头", "f134": 100, "_tag": "ind"}
        for i in range(40)]

_sv = (hot.fetch_sectors, hot.members, M.data_cap, hot.kline_stats)
hot.fetch_sectors = lambda: [dict(x) for x in _RAW]
hot.members = lambda c: set(range(20))
try:
    # ① 预算一进门就耗尽
    M.data_cap = lambda own, what="": -9
    _rA = hot.analyze(top=8)
    check(bool(_rA["strong"]), "预算耗尽时 strong 为空 —— 探针没跑起来")
    for _r in _rA["strong"][:3] + _rA["top_up"][:3] + _rA["top_dn"][:3]:
        check("_vr_ok" in _r,
              f"预算耗尽时 {_r['name']!r} **没有** `_vr_ok` 键 —— "
              f"渲染层 `r.get('_vr_ok', True)` 会把它印成实测值")
        check(_r["_vr_ok"] is False,
              f"预算耗尽时 {_r['name']!r} 的 `_vr_ok` 是 {_r['_vr_ok']!r} —— "
              f"应为 False（vr={_r['vr']} 是 `_num('-', 1.0)` 的兜底常数）")

    # ② 预算充足：算出值的行必须 True（不能一刀切全标未知）
    M.data_cap = lambda own, what="": own
    hot.kline_stats = lambda c, a, now=None, budget_s=None: (1.7, -25.0)
    _rB = hot.analyze(top=8)
    check(bool(_rB["strong"]), "预算充足时 strong 为空 —— 探针没跑起来")
    check(_rB["strong"][0]["_vr_ok"] is True,
          f"预算充足时 `_vr_ok` 是 {_rB['strong'][0]['_vr_ok']!r} —— "
          f"应为 True（kline_stats 返回了 1.7，这是真算出来的）")
    check(abs(_rB["strong"][0]["vr"] - 1.7) < 1e-9,
          f"预算充足时 vr={_rB['strong'][0]['vr']} —— 应为 1.7")
finally:
    (hot.fetch_sectors, hot.members, M.data_cap, hot.kline_stats) = _sv

# ---------- ④ / ⑤ 行为级：渲染 ----------
_src_an = open(os.path.join(_SRC_DIR, "analyze.py"), encoding="utf-8").read()
_code_an = _strip_comments(_src_an)
check("r.get('_vr_ok', True)" not in _code_an,
      "渲染层仍在用 `r.get('_vr_ok', True)` —— 缺键（旧缓存/上游换实现）"
      "时会把未知当成可信")

import datetime as _dt  # noqa: E402

import analyze as A  # noqa: E402


def _brief(row):
    _h = {"strong": [row], "start": [], "top_up": [], "top_dn": [],
          "pool": 100, "dedup": 90}
    _now = _dt.datetime(2026, 11, 24, 15, 0)
    return A.build_brief({}, None, [], _now, hot=_h, slot="15:00")


def _mkrow(**over):
    r = dict(tag="ind", code="BK1320", name="文娱用品", n=20, mem={1, 2},
             chg=3.04, f24=-11.2, pct=7.3, xlpct=1.0, vr=1.0, _vr_ok=False,
             turn=1.0, inflow=1e8, amt=2.3e9, f134=100, up=50, dn=10,
             breadth=0.83, lead="创源股份", score=1.0, stage="延续")
    r.update(over)
    return r


_b1 = _brief(_mkrow())
check("量比未知" in _b1,
      f"`_vr_ok=False` 的行没印「量比未知」：\n"
      f"      {[x for x in _b1.split(chr(10)) if '文娱用品' in x][:1]!r}")
check("量比1.0" not in _b1,
      "`_vr_ok=False` 的行仍印出了「量比1.0」—— 没算过却当成实测值")

# ⑤ 完全没有 `_vr_ok` 键（旧缓存 / 上游换实现）-> 必须印「未知」
_b2 = _brief({k: v for k, v in _mkrow().items() if k != "_vr_ok"})
check("量比未知" in _b2,
      f"行**缺少** `_vr_ok` 键时印的不是「量比未知」：\n"
      f"      {[x for x in _b2.split(chr(10)) if '文娱用品' in x][:1]!r}\n"
      f"      这条专门钉渲染默认值 —— 改回 `.get('_vr_ok', True)` 就红")

# 真算出来的行仍要显示数字（不能一刀切全变「未知」）
_b3 = _brief(_mkrow(_vr_ok=True, vr=1.7))
check("量比1.7" in _b3,
      f"`_vr_ok=True` 的行没印出「量比1.7」：\n"
      f"      {[x for x in _b3.split(chr(10)) if '文娱用品' in x][:1]!r}")

# ---------- ⑥ 构造时就要有默认值 ----------
_i = _code_hot.find("_vr_ok=False,")
check(_i > 0,
      "行构造处没有 `_vr_ok=False` —— 默认值仍靠「走完 kline_stats 才有」，"
      "预算耗尽时整批行没有这个键")

print(f"ALL PASS (量比「没算过」不得印成 1.0 {_ok} 个断言)")
