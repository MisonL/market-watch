"""指数紧急的两轮确认必须有年龄上限 —— 2026-11-07 复核 F3。

P1-10（2026-11-06）给 pending 键编了方向，堵住「反向尖峰被当成确认」。
但那一版只比较「有没有同向 pending」与「观测时刻是否相同」，
**不看 pending 有多旧**，确认后也不摘除。于是单日的 pending 会一直挂到收盘，
后面任何一个顺势尖峰都能拿它当「第一轮」直接确认 —— 两轮确认退化成单帧确认。

`[A/B 实测]` 上证阈值 2.70%：
    10:00 +4.00 → 记 pending
    10:05 -4.00 → 另记（反向，正确）
    10:10 -4.20 → 确认推送（正确，两轮同向新观测）
    11:30 +4.10 → **直接推「上证指数涨」** ❌ 用的是 90 分钟前那条 pending

修法：pending 超过 `PEND_MAX_AGE_MIN`（15 分钟）即作废，当作首次记录；
确认成立后把该方向的 pending 摘掉，使一次尖峰只能被消费一次。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_index_pending_age.py`
"""
import os, sys, datetime as dt, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import alert

_DAY = "2026-11-06"          # 交易日（周五）


def _q(ts, chg):
    return {"1.000001": {"f2": 3300.0, "f3": chg, "f14": "上证指数",
                         "f104": 1, "f105": 9, "_ts": f"{_DAY} {ts}:00"}}


def _empty_st():
    return {"date": _DAY, "fired": [], "inflight": [],
            "pending_idx": [], "hourly_retry": {}, "count": 0}


def _round(st, hm, chg):
    """跑一轮 check_index（时钟与数据时刻对齐，模拟真实推进）。"""
    _now = dt.datetime.fromisoformat(f"{_DAY}T{hm}:00")
    return alert.check_index(_now, st, lambda: _q(hm, chg))


def check():
    # ① 陈旧 pending 不得用来确认（F3 的原始复现序列）
    st = _empty_st()
    assert _round(st, "10:00", +4.00) == []      # 记 pending
    assert _round(st, "10:05", -4.00) == []      # 反向，另记
    assert [h["name"] for h in _round(st, "10:10", -4.20)] == ["上证指数"], (
        "反向后的同向两轮必须确认")
    assert _round(st, "11:30", +4.10) == [], (
        "90 分钟前的 pending 不得用来确认一个仅一帧的尖峰")

    # ② 正常的两轮新观测仍必须确认（防修成永久静默）
    st2 = _empty_st()
    assert _round(st2, "10:00", -3.20) == []
    assert [h["name"] for h in _round(st2, "10:05", -3.30)] == ["上证指数"]

    # ③ 间隔超过 15 分钟：pending 作废，重新计基准
    st3 = _empty_st()
    assert _round(st3, "10:00", -3.20) == []
    assert _round(st3, "10:16", -3.30) == [], (
        "相隔 16 分钟的两个点不构成「持续」，必须重新计基准")

    # ④ 确认后 pending 被摘除：第三次不得再推（一次尖峰只消费一次）
    st4 = _empty_st()
    _round(st4, "10:00", -3.20)
    assert [h["name"] for h in _round(st4, "10:05", -3.30)] == ["上证指数"]
    assert _round(st4, "10:10", -3.40) == [], (
        "确认后必须摘掉该方向的 pending，否则后续每一轮都会重复推")

    # ⑤ 边界：恰好 15 分钟仍算有效（上限是闭区间）
    st5 = _empty_st()
    assert _round(st5, "10:00", -3.20) == []
    assert [h["name"] for h in _round(st5, "10:15", -3.30)] == ["上证指数"], (
        "恰好 15 分钟应仍在窗口内")

    print("ALL PASS (pending 年龄 6 个断言)")


if __name__ == "__main__":
    check()
