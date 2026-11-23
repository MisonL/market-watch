#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clist() 分页：中间某一页瞬时失败时**不得静默返回残缺名单**。

## 为什么值得单测

`[实测]` 2026-11-23 同一分钟内对 `m:90+t:3`（概念 554 个）连做两次全量取数：

    max_pn=8  -> 6 页, 554 条, total=554   ← 完整
    max_pn=12 -> 5 页, 400 条, total=554   ← 缺 154 个

两次唯一的差别是第 5 页那一次 `cj` 撞上瞬时失败（firecrawl 偶发空包）。
原实现在那里直接 `break`，**把 400 条当成功返回**，而服务端声明的
`total=554` 明摆着还没取完。

危害不是"少几十个板块"这么轻：`clist` 按 `fid=f62` **降序**取，
被丢的正是排在后面的那批 —— 也就是当日**流出最猛**的板块。
`alert.check_sector_fund` 正是靠这份名单找「外资/融资盘整体撤离」，
于是最该报的紧急项永远看不见，而日志里一个字都没有。

## 本文件锁住什么

- total 已知且没取够时，失败的那一页必须**重试**（默认 2 次）
- 重试成功 -> 返回完整名单（554 条），且留下"重试成功"日志
- 重试仍失败 -> 返回已取部分，但**必须留下缺口日志**（不再静默）
- total 未知时保持原行为：无法区分"没有更多页"与"这页失败了"
- 反面控制：把 `_PAGE_RETRY` 归零，本用例必须**变红**（否则是装饰）
"""
import os
import sys
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


TOTAL = 554          # 概念板块的真实 total（实测 2026-11-23）
PAGE = 100           # 服务端把 pz=500 截到 100 后的真实页大小
FAIL_PN = 5          # 实测撞上瞬时失败的那一页


def _mk_page(pn, total=TOTAL, size=PAGE):
    """造第 pn 页（1-based）。返回服务端 diff 的两种形状之一。"""
    start = (pn - 1) * size
    rows = []
    for i in range(start, min(start + size, total)):
        rows.append({"f12": f"BK{1000 + i}", "f14": f"板块{i}",
                     "f3": -1.0 - i * 0.01, "f6": 1e10 - i,
                     "f62": -1e9 + i, "f184": -5.0})
    return {"rc": 0, "data": {"total": total, "diff": rows}}


class _Harness:
    """把 cj / _https_hosts / log 换成确定的桩，完全不离网。"""

    def __init__(self, fail_times=1):
        self.calls = []
        self.logs = []
        self.fail_times = fail_times   # 第 FAIL_PN 页连续失败几次
        self._failed = 0
        self.orig_cj = M.cj
        self.orig_hosts = M._https_hosts
        self.orig_log = M.log
        self.orig_budget = M.data_cap

    def __enter__(self):
        M.cj = self._cj
        M._https_hosts = lambda: ["https://stub.invalid/api/qt"]
        M.log = lambda msg, kind=None: self.logs.append(str(msg))
        M.data_cap = lambda b, what=None: b
        # ⚠️⚠️ 必须把 firecrawl 冷却也桩掉。
        # `clist()` 现在会判 `_fc_fail_get()`：冷却期内**跳过重试**
        # （此时 `cj()` 会 0.0s 空回，重试只是白打）。
        # 而 `_fc_fail_get()` 读的是 `state/cache/firecrawl_fail.json` ——
        # 不桩就是读**生产**状态：机器上刚好在限流冷却时，本文件
        # 会突然从"重试后取全"变成"只拿到 400 条"，红得莫名其妙
        # （实测踩到，且当时误以为是代码坏了）。
        self._orig_fc = M._fc_fail_get
        M._fc_fail_get = lambda: 0.0
        return self

    def __exit__(self, *a):
        M.cj = self.orig_cj
        M._https_hosts = self.orig_hosts
        M.log = self.orig_log
        M.data_cap = self.orig_budget
        M._fc_fail_get = self._orig_fc
        return False

    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        if pn == FAIL_PN and self._failed < self.fail_times:
            self._failed += 1
            return {}          # 瞬时失败：空包（真实形态）
        if (pn - 1) * PAGE >= TOTAL:
            return {"rc": 0, "data": {"total": TOTAL, "diff": []}}
        return _mk_page(pn)


# ---------- ①第 5 页失败一次 -> 必须重试并取全 ----------
with _Harness(fail_times=1) as h:
    got, total = M.clist("m:90+t:3", "f12,f14,f3,f6,f62,f184", pz=500,
                         max_pn=12, fid="f62", budget_s=180,
                         with_total=True)
check(len(got) == TOTAL,
      f"第 5 页失败一次后应取全 {TOTAL} 条，实得 {len(got)} 条 —— "
      "这正是「静默丢掉流出最猛的 154 个板块」那个缺陷")
check(total == TOTAL, f"total 应为 {TOTAL}，实得 {total}")
check(h.calls.count(FAIL_PN) >= 2,
      f"第 {FAIL_PN} 页只请求了 {h.calls.count(FAIL_PN)} 次 —— 没有重试")
check(any("重试" in s and "成功" in s for s in h.logs),
      f"重试成功没有留日志：{h.logs}")
# 去重：不许把同一批行重复计入
_ids = [r["f12"] for r in got if isinstance(r, dict)]
check(len(_ids) == len(set(_ids)), f"返回的 f12 有重复：{len(_ids)} vs {len(set(_ids))}")

# ---------- ②第 5 页一直失败 -> 返回部分，但必须报缺口 ----------
with _Harness(fail_times=99) as h:
    got2, total2 = M.clist("m:90+t:3", "f12,f14,f3,f6,f62,f184", pz=500,
                           max_pn=12, fid="f62", budget_s=180,
                           with_total=True)
check(len(got2) < TOTAL,
      f"第 5 页一直失败却取到了 {len(got2)} 条 —— 桩没生效")
check(any("名单不完整" in s for s in h.logs),
      f"缺口没有写进日志（静默截断又回来了）：{h.logs}")
_gap = [s for s in h.logs if "名单不完整" in s]
check(_gap and str(len(got2)) in _gap[0] and str(TOTAL) in _gap[0],
      "缺口日志没写清已取/总数：" + repr(_gap))

# ---------- ③total 未知时保持原行为（不得重试）----------
#
# 服务端不给 total 时，"这页空了"既可能是"没有更多页"，也可能是"这页失败了"，
# 无法区分。此时若重试，就会在真的没有更多页时白打 _PAGE_RETRY 次请求。
class _NoTotal(_Harness):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        if pn == 2:
            return {"rc": 0, "data": {"diff": []}}   # 空页，且**没有 total**
        page = _mk_page(pn)
        del page["data"]["total"]                    # 服务端不给 total
        return page


with _NoTotal(fail_times=0) as h:
    got3 = M.clist("m:90+t:3", "f12", pz=500, max_pn=12, fid="f62",
                   budget_s=180)
check(len(got3) == PAGE, f"第 2 页失败且 total 未知时应停在第 1 页，实得 {len(got3)}")
check(h.calls.count(2) == 1,
      f"total 未知时第 2 页请求了 {h.calls.count(2)} 次 —— 不该重试")

# ---------- ③a 第 1 页失败一次 -> 也必须重试 ----------
#
# ⚠️ `_total` 是在**第一页成功之后**才赋值的。所以第一页失败时
# `_total` 仍是 None，上面那个 `_total is not None and len(out) < _total`
# 判据根本不成立 —— 第一页会**直接放弃、一条都不重试**。
# 而"一条都没拿到"是比"少拿几页"更严重的情形：调用方
# `_sector_rows` 返回 [] → `check_sector_fund` 遍历不到任何行 →
# 本轮**一条板块告警都不发**，热点榜也整段消失。
# 所以判据必须额外覆盖 `out` 为空（拿不到东西时没有理由不重试）。
class _FirstPageFail(_Harness):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        if pn == 1 and self._failed < 1:
            self._failed += 1
            return {}          # 第 1 页空包一次
        return _mk_page(pn)


with _FirstPageFail(fail_times=0) as h:
    got0, tot0 = M.clist("m:90+t:3", "f12,f14,f3,f6,f62,f184", pz=500,
                         max_pn=12, fid="f62", budget_s=180, with_total=True)
check(len(got0) == TOTAL,
      f"第 1 页失败一次后应重试并取全 {TOTAL} 条，实得 {len(got0)} 条 —— "
      "第 1 页失败时 _total 还是 None，原判据会直接放弃")
check(h.calls.count(1) >= 2,
      f"第 1 页只请求了 {h.calls.count(1)} 次 —— 没有重试")

# ---------- ③b 第 1 页就失败 -> 必须留痕（否则整轮板块告警静默消失）----------
class _FirstFail(_Harness):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        return {}          # 每一页都空包


with _FirstFail(fail_times=0) as h:
    got5 = M.clist("m:90+t:3", "f12", pz=500, max_pn=12, fid="f62",
                   budget_s=180)
check(got5 == [], f"全空包时应返回空列表，实得 {len(got5)} 条")
check(any("完全取不到" in s for s in h.logs),
      "0 条时没有留痕 —— 本轮热点榜与板块资金告警都会静默跳过：" + repr(h.logs))

# ---------- ③c 服务端忽略 pn（每页都返第一页）-> 必须报残缺 ----------
class _IgnorePn(_Harness):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        return _mk_page(1)      # 无视 pn，永远第一页


with _IgnorePn(fail_times=0) as h:
    got6 = M.clist("m:90+t:3", "f12", pz=500, max_pn=12, fid="f62",
                   budget_s=180)
check(len(got6) == PAGE,
      f"每页都返第一页时应停在 {PAGE} 条，实得 {len(got6)}")
check(any("名单不完整" in s for s in h.logs),
      "忽略 pn 导致的残缺没有留痕：" + repr(h.logs))

# ---------- ③d 短页（服务端漏行）-> 必须重试 ----------
#
# ⚠️2026-11-23（codex P1）：原先只对**空包**重试。但服务端也可能返回一个
# **比前几页短**的非空页（实测第 5 页 50 条、其余 100 条）：
# `_has_data` 为真 -> 不重试 -> 累计 504/554 -> 出口只报警，数据已经错了。
# 短页判据用「已观察到的最大单页行数」作基准，不能用 `pz`
# （服务端会把 pz=500 截到 100，用 pz 判会把每页都误判成短页）。
class _ShortPage(_Harness):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        if pn == FAIL_PN and self._failed < 1:
            self._failed += 1
            start = (pn - 1) * PAGE
            rows = [{"f12": f"BK{1000 + i}", "f14": f"板块{i}",
                     "f3": -1.0, "f6": 1e10, "f62": -1e9, "f184": -5.0}
                    for i in range(start, start + 50)]        # 只给 50 条
            return {"rc": 0, "data": {"total": TOTAL, "diff": rows}}
        return _mk_page(pn)


with _ShortPage(fail_times=0) as h:
    got7, tot7 = M.clist("m:90+t:3", "f12,f14,f3,f6,f62,f184", pz=500,
                         max_pn=12, fid="f62", budget_s=180, with_total=True)
check(len(got7) == TOTAL,
      f"短页后应重试并取全 {TOTAL} 条，实得 {len(got7)} 条 —— "
      "只对空包重试会漏掉这种形态")
check(any("短页" in s for s in h.logs),
      "短页重试的日志没有标出是短页（和空包混在一起，排查时分不清）："
      + repr(h.logs))

# ---------- ③e 预算耗尽 -> 必须与「源故障」区分开 ----------
#
# ⚠️2026-11-23（codex P1）：重试循环里那条预算 `break` 会跳过 `for...else`，
# 于是「重试用尽」不打印；出口只看到 `len(out) < _total`，把**预算耗尽**
# 误报成源故障。两者处置完全不同：预算要调上限，源坏要查火线。
#
# ⚠️ 必须让预算在**页内重试时**耗尽，而不是一开工就超 ——
# 后者会被外层「总耗时超预算」那条先接住（那条文案里也有"预算"），
# 断言就变成了在同一条无关日志上打勾（实测踩过：删掉重试分支的日志，
# 用例依然全绿）。
class _SlowFail(_Harness):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        self.calls.append(pn)
        if pn == 1:
            time.sleep(1.2)      # 第一次取页就烧掉 1.2s（预算只有 1s）
            return {}
        return _mk_page(pn)


with _SlowFail(fail_times=0) as h:
    got8 = M.clist("m:90+t:3", "f12", pz=500, max_pn=12, fid="f62",
                   budget_s=1)
check(any("重试前预算已耗尽" in s for s in h.logs),
      "页内重试时预算耗尽没留痕（会被误报成源故障）：" + repr(h.logs))
check(any("不是源坏了" in s for s in h.logs),
      "预算耗尽的日志没点明这不是源坏了：" + repr(h.logs))

# ---------- ③f firecrawl 冷却期内 -> 不重试，且不得谎称试过 ----------
#
# ⚠️2026-11-23：直连被 DPI 拦死、代理未配，firecrawl 一进冷却，`cj()` 会在
# 0.0s 内空回（不发任何请求）。此时重试 N 次只是把同一页白打 N 遍，
# 还会让"名单不完整"晚 N 倍时间才报。实测一轮 13 次 clist 请求全是空转。
#
# 更忌讳的是**编造没发生的事**：若用 `range(0)` 表达"不重试"，
# `for...else` 会在空循环时立刻执行 else，日志变成"重试 2 次仍失败" ——
# 而一次都没试过。必须直接说真实原因。
with _Harness(fail_times=99) as h:
    M._fc_fail_get = lambda: time.time() + 300     # 冷却中
    got9 = M.clist("m:90+t:3", "f12", pz=500, max_pn=12, fid="f62",
                   budget_s=180)
check(any("firecrawl 正在冷却" in s for s in h.logs),
      "冷却期内没说明真实原因：" + repr(h.logs))
check(not any("仍失败" in s for s in h.logs),
      "冷却期内一次都没重试，却报了『重试 N 次仍失败』—— 在编造没发生的事："
      + repr(h.logs))

# ---------- ④反面控制：把重试关掉，①必须变红 ----------
_orig_retry = M._PAGE_RETRY
try:
    M._PAGE_RETRY = 0
    with _Harness(fail_times=1) as h:
        got4 = M.clist("m:90+t:3", "f12", pz=500, max_pn=12, fid="f62",
                       budget_s=180)
    check(len(got4) < TOTAL,
          "_PAGE_RETRY=0 时居然也取全了 —— 说明这条用例的分页桩是假的，"
          "真正守住重试的不是本文件")
    check(any("名单不完整" in s for s in h.logs),
          "_PAGE_RETRY=0 时没有缺口日志")
finally:
    M._PAGE_RETRY = _orig_retry

print(f"ALL PASS (clist 分页失败重试 {_ok} 个断言)")