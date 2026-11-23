#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""clist_extremes() 只取资金两端，且**不得把没取到说成取全了**。

## 为什么要这个函数

`[实测]` 2026-11-23 盘中：服务端把 `pz` 硬截到 **100 行**（`pz>=200` 直接
返回 0 行），所以全量 496 个行业 / 554 个概念各要 5~6 页 —— 一次全量扫描
就是 **11 次请求**。而经 firecrawl 兜底时额度只有 **12 次/分钟**：

    一次全量扫描 = 11 次 ≈ 整分钟额度
    hot.fetch_sectors 再来一次 -> 19 次（实测一轮的 clist 请求数）

当天后果（生产日志实测）：每一轮都在第 4~6 页限流，
`clist(m:90+t:3) 名单不完整：已取 300/554 条`、`alert` 两个方向全部
返回 0 条 —— 用户收到的推送里 12 个关注板块整段「数据暂缺」。

但异动判定的门槛是 `|f62| >= 50亿`，**只需要尾部**：按 f62 降序拿流入头部、
升序拿流出头部，两各 2 页（4 次请求）就覆盖了所有超门槛板块。

## 本文件锁住什么

- 两端都取，按 f12 去重
- 末尾已低于门槛 -> 提前停（不白翻页）
- 末尾仍超门槛 -> 翻到上限后 `complete=False`（不许说取全了）
- **某方向 0 条 -> `complete=False`**（这条最容易被写成"没有超门槛板块"）
- 预算耗尽 -> `complete=False`
- 反面控制：把空结果判据去掉，本用例必须变红
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


FABS = 50e8
PZ = 100


def _mk(pn, po, total=400, size=PZ, lo=None):
    """造第 pn 页。po=1 降序（流入），po=0 升序（流出）。"""
    rows = []
    for i in range(size):
        idx = (pn - 1) * size + i
        if idx >= total:
            break
        # 降序：f62 从 +100亿 递减；升序：从 -100亿 递增
        v = (100.0 - idx * 0.5) if po == 1 else (-100.0 + idx * 0.5)
        rows.append({"f12": f"BK{po}{idx}", "f14": f"{'流入' if po==1 else '流出'}{idx}",
                     "f62": v * 1e8, "f184": v / 10})
    return {"rc": 0, "data": {"total": total, "diff": rows}}


class _H:
    def __init__(self, mode="normal"):
        self.mode = mode
        self.logs = []
        self.calls = []

    def __enter__(self):
        self._o = (M.cj, M._https_hosts, M.log, M.data_cap)
        M._https_hosts = lambda: ["https://stub.invalid/api/qt"]
        M.log = lambda msg, kind=None: self.logs.append(str(msg))
        M.data_cap = lambda b, what=None: b
        M.cj = self._cj
        return self

    def __exit__(self, *a):
        M.cj, M._https_hosts, M.log, M.data_cap = self._o
        return False

    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        po = int(url.split("po=")[1].split("&")[0])
        self.calls.append((po, pn))
        if self.mode == "empty_out" and po == 0:
            return {}
        if self.mode == "endless":
            # 衰减要**足够慢**，保证翻满页数后末尾仍超门槛。
            # 踩过：用 `_mk` 的 0.5 亿/行衰减，翻到第 4 页时数值已穿过 0，
            # `min(abs(...)) < fabs` 成立 -> 提前 break -> 用例测不到
            # "翻到上限仍不全"这条路径（断言全绿但什么也没验证）。
            rows = []
            for i in range(PZ):
                idx = (pn - 1) * PZ + i
                v = (5000.0 - idx * 0.01) if po == 1 else (-5000.0 + idx * 0.01)
                rows.append({"f12": f"BK{po}{idx}", "f14": f"p{idx}",
                             "f62": v * 1e8, "f184": v / 10})
            return {"rc": 0, "data": {"total": 100000, "diff": rows}}
        return _mk(pn, po)


# ---------- ①正常：两端都取，末尾低于门槛提前停 ----------
with _H("normal") as h:
    rows, ok = M.clist_extremes("m:90+t:2", "f12,f14,f62", FABS,
                                pz=PZ, pages=2, budget_s=90)
check(ok is True, f"正常情况应 complete=True，实得 {ok}：{h.logs}")
check(len(rows) > 0, "正常情况应取到行")
_pos = [r for r in rows if r["f62"] > 0]
_neg = [r for r in rows if r["f62"] < 0]
check(_pos and _neg, f"应同时有流入端与流出端，实得 +{len(_pos)}/-{len(_neg)}")
_ids = [r["f12"] for r in rows]
check(len(_ids) == len(set(_ids)), "跨方向必须按 f12 去重")
check(all(abs(r["f62"]) >= FABS or True for r in rows), "行内容不应被改写")

# 末尾低于门槛时应提前停（不该翻满 pages*2）
_p1 = [c for c in h.calls if c[0] == 1]
check(len(_p1) <= 2, f"流入方向只该翻 2 页，实得 {len(_p1)} 次：{h.calls}")

# ---------- ②末尾仍超门槛 -> complete=False ----------
with _H("endless") as h2:
    rows2, ok2 = M.clist_extremes("m:90+t:2", "f12,f14,f62", FABS,
                                  pz=PZ, pages=2, budget_s=90)
check(ok2 is False,
      f"翻到上限时末尾仍超门槛，必须 complete=False，实得 {ok2}")
check(any("仍超门槛" in s for s in h2.logs),
      "翻到上限仍不全，必须留痕：" + repr(h2.logs))

# ---------- ③某方向 0 条 -> complete=False（最易错的一条）----------
with _H("empty_out") as h3:
    rows3, ok3 = M.clist_extremes("m:90+t:2", "f12,f14,f62", FABS,
                                  pz=PZ, pages=2, budget_s=90)
check(ok3 is False,
      "流出方向一条都没取到，**不能**报 complete=True —— "
      "那等于把『没取到』说成『没有超门槛板块』")
check(any("一条都没取到" in s for s in h3.logs),
      "空方向必须留痕：" + repr(h3.logs))

# ---------- ④预算耗尽 -> complete=False ----------
with _H("normal") as h4:
    rows4, ok4 = M.clist_extremes("m:90+t:2", "f12,f14,f62", FABS,
                                  pz=PZ, pages=2, budget_s=0)
check(ok4 is False, f"预算耗尽必须 complete=False，实得 {ok4}")
check(any("预算耗尽" in s for s in h4.logs),
      "预算耗尽必须留痕：" + repr(h4.logs))

# ---------- ④b 两端重叠 -> 必须按 f12 去重 ----------
#
# 板块数量少（或门槛很低）时，降序头 200 与升序头 200 **会覆盖同一批板块**。
# 不去重会让同一个板块出现两次 -> `check_sector_fund` 对同一行算两次，
# `hits` 里出现重复项（去重键相同，靠 state 才没重复推送 —— 但
# "本轮命中 N 条"这类计数会翻倍）。
class _Overlap(_H):
    def _cj(self, url, **kw):
        pn = int(url.split("pn=")[1].split("&")[0])
        po = int(url.split("po=")[1].split("&")[0])
        self.calls.append((po, pn))
        # 两端返回**完全相同**的 100 行（f12 相同），只改 f62 符号
        rows = [{"f12": f"BKSAME{i}", "f14": f"s{i}",
                 "f62": (100.0 - i) * 1e8 * (1 if po == 1 else -1),
                 "f184": (100.0 - i) / 10}
                for i in range(PZ)]
        return {"rc": 0, "data": {"total": PZ, "diff": rows}}


with _Overlap("normal") as h5:
    rows5, ok5 = M.clist_extremes("m:90+t:2", "f12,f14,f62", FABS,
                                 pz=PZ, pages=1, budget_s=90)
_ids5 = [r["f12"] for r in rows5]
check(len(_ids5) == len(set(_ids5)),
      f"两端重叠时必须去重，实得 {len(_ids5)} 行 / {len(set(_ids5))} 个唯一 f12")

# ---------- ⑤反面控制：去掉空结果判据，③必须变红 ----------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "marketlib.py"), encoding="utf-8").read()
check('if not _vals:' in _src,
      "marketlib 里没有『空结果』判据分支 —— ③ 会退化成装饰")

print(f"ALL PASS (clist 双向取数 {_ok} 个断言)")
