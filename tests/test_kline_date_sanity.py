#!/usr/bin/env python3
"""板块日K 必须剔除「不可能存在的交易日」—— 2026-11-21，P0 数据正确性。

## 事故形态

板块日K 的唯一来源是 `EM_TEST`（push2test，腾讯无板块接口）。`[实测]`
2026-11-21（**周六，休市**）它给出的序列里赫然有：

    2026-11-21,2149.91,2217.80,2234.82,2149.91,17821

周六不可能有交易 —— 这根只能是合成/模拟数据。危害是 `levels()` 的
20 日高/低会把它算进去，而板块「关键位」正是读者判断位置的依据：
一根伪造的当日高点（2234.82）就能把 h20 抬高、让「距 20 日高 -x%」
显得更乐观（偏差不会报警，只会让结论偏乐观）。

同一台主机今日已被证实会返回**伪造实时行情**（3701.71 vs 真值 3533.79），
见 `tests/test_quote_source_trust.py`。

## 判据取舍

不能整条弃用（板块关键位会永远消失，而它是推送的固定段），
只剔「未来日 / 休市日」这两种**物理上不可能**的根 ——
与仓库既有的「宁可缺数据，不给错的数」同源。
实测剔除后末根回到 2026-11-20（收 1892.10），与真主机实时 f3=-6.59%
自洽（2025.48 × 0.9341 ≈ 1892.1），说明**历史是真的、只有合成的那几根假**。
"""
import datetime
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import marketlib as M                                  # noqa: E402

_ok = [0]


def check(cond, msg):
    if cond:
        _ok[0] += 1
    else:
        print(f"  ❌ {msg}")
        raise SystemExit(1)


TODAY = datetime.date.today()


def _bar(d, close):
    return f"{d.isoformat()},2000.00,{close:.2f},2100.00,1900.00,1000000"


def _weekdays_back(n):
    """从今天往前取 n 个工作日（不做节假日过滤，够用即可）。"""
    out, d = [], TODAY
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= datetime.timedelta(days=1)
    return list(reversed(out))


def _payload(bars):
    return {"data": {"klines": bars}}


# ---------- ① 未来日 / 休市日的根必须被剔除 ----------
_valid = _weekdays_back(25)              # 25 个工作日，满足 len>=20 的闸门
_bars = [_bar(d, 2000.0) for d in _valid]
# 比较基准：昨天（最后一个工作日）的收价
_last_valid = _valid[-1]
# 追加两根“不可能存在”的：今天若是休市日 → 今天那根；再加一根明年
_fake = []
if not M.is_trading_day(TODAY, warn=False):
    _fake.append(_bar(TODAY, 9999.0))
_fake.append(_bar(TODAY + datetime.timedelta(days=400), 9999.0))

# ⚠️ 必须把**磁盘日K缓存**也桩掉：`_kline_ohlc` 会把新根 merge 进
# `dcache_get()` 读到的历史（实测本地已缓存 34 根，于是断言看到 59 根）。
# 不隔离的话，这条测试测的是"缓存 + 桩数据"，与断言意图不符。
_sv = {"cj": M.cj, "log": M.log, "dcg": M.dcache_get}
M.log = lambda m, tag="": None
M.dcache_get = lambda key: None
try:
    M.cj = lambda url, **k: _payload(_bars + _fake)
    M.set_data_deadline(None)
    rows = M._kline_ohlc("90.BK1137")
finally:
    M.cj = _sv["cj"]
    M.log = _sv["log"]
    M.dcache_get = _sv["dcg"]
    M.set_data_deadline(None)

check(rows is not None, "剔除后不到 20 根就判源损坏了 —— 正常历史不该被牵连")
_dates = [str(r[0]) for r in rows]
check(not any(d == str(TODAY + datetime.timedelta(days=400)) for d in _dates),
      "未来日（+400 天）的 K 线没有被剔除 —— 会污染 20 日高/低")
if not M.is_trading_day(TODAY, warn=False):
    check(not any(d == str(TODAY) for d in _dates),
          f"{TODAY} 是休市日，它的 K 线没有被剔除 —— 周末的伪造根会抬高 h20")
check(not any(float(r[1]) >= 9999 for r in rows),
      f"伪造根的 9999 值仍在结果里：{[r for r in rows if float(r[1]) >= 9999][:2]}")

# ---------- ② 真实历史必须完整保留（不能连带删） ----------
for _d in _valid:
    check(str(_d) in _dates, f"正常工作日的 K 线 {_d} 被误删了")
check(len(rows) == len(_valid),
      f"保留根数 {len(rows)} ≠ 真实历史 {len(_valid)} —— 剔除逻辑误伤")

# ---------- ③ 全部合法时不得无谓剔除 ----------
try:
    M.log = lambda m, tag="": None
    M.cj = lambda url, **k: _payload(_bars)
    M.set_data_deadline(None)
    rows2 = M._kline_ohlc("90.BK1137")
finally:
    M.cj = _sv["cj"]
    M.log = _sv["log"]
    M.dcache_get = _sv["dcg"]
    M.set_data_deadline(None)
check(rows2 is not None and len(rows2) == len(_valid),
      f"全是合法根时也被删了：{len(rows2) if rows2 else None} vs {len(_valid)}")

print(f"ALL PASS (板块日K 剔除不可能存在的交易日 {_ok[0]} 个断言)")