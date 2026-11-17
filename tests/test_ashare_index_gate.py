"""A 股指数 3σ 的开市闸门 —— 2026-11-06 全量复核 P1-2。

背景：`collect` 的采集窗口从 **09:25** 开始（`TH["session"][0]`），而 A 股
**连续竞价 09:30** 才开始。09:15–09:25 是开盘集合竞价，09:25 撮合出开盘价
—— 两者之间有 5 分钟，此刻 `f2` 是刚撮出来的开盘价，`f3`（涨跌幅）却可能
还是**上一交易日**的值。

`[实测]`（修前）：`collect.evaluate(q, [], 09:26)` 用昨天的 f3 产出
`idx_weak 上证指数 -3.20%`，写进 pending.json 并**占住 `idx_weak` 去重键**
—— 开盘后真的跌破 1% 也不会再记一条，面板上永远显示昨天那个数。

这与 2026-09-30 港股 09:26 那次（见 `collect.evaluate` 的长注释）是**同一个
病**的 A 股分支：当时只补了港股（`hk_session_open`），A 股行漏了。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_ashare_index_gate.py`
"""
import os, sys, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import datetime as dt
import marketlib as M
import collect

# 沪深两市各一条，f3 取「若用昨日冻结值就会误报」的量级
_Q = {
    "1.000001": {"f2": 3300.0, "f3": -3.20, "f14": "上证指数"},
    "1.399006": {"f2": 2100.0, "f3": -6.50, "f14": "创业板指"},
}


def _idx_events(hhmm, day="2026-11-06"):
    now = dt.datetime.fromisoformat(f"{day}T{hhmm}:00")
    return [e["k"] for e in collect.evaluate(_Q, [], now)
            if str(e.get("k", "")).startswith("idx_")]


def check():
    # ── 闸门本体（纯函数，与日历无关的部分）──
    assert M.a_session_open(dt.datetime.fromisoformat("2026-11-06T09:26:00")) is False, \
        "09:26 属于集合竞价阶段，a_session_open 必须为 False"
    assert M.a_session_open(dt.datetime.fromisoformat("2026-11-06T09:29:00")) is False
    assert M.a_session_open(dt.datetime.fromisoformat("2026-11-06T09:30:00")) is True
    assert M.a_session_open(dt.datetime.fromisoformat("2026-11-06T14:00:00")) is True
    # 收盘后不能把定格的收盘价当"盘中异动"
    assert M.a_session_open(dt.datetime.fromisoformat("2026-11-06T15:06:00")) is False
    # 周末
    assert M.a_session_open(dt.datetime.fromisoformat("2026-11-07T10:00:00")) is False

    # ── 端到端：09:26 不得产出任何指数事件 ──
    assert _idx_events("09:26") == [], (
        f"09:26 不得用昨日 f3 判 3σ，实得 {_idx_events('09:26')}")
    assert _idx_events("09:29") == [], (
        f"09:29 不得产出指数事件，实得 {_idx_events('09:29')}")

    # ── 开盘后必须照常产出（闸门不能把正常逻辑一起关掉）──
    # 注意 10:00 是 guard_open(09:30–10:00) 的右边界之外，此刻不处于保护时段。
    ev = _idx_events("10:00")
    assert "idx_weak" in ev, f"10:00 应正常产出 idx_weak，实得 {ev}"

    # ── 两种闸门互不干扰：港股行不受 A 股闸门影响 ──
    # 15:10 A 股已过闸（a_session_open=False），若同时给港股行，
    # 港股（收盘 16:00）应仍然产出 —— 说明两个判据是分开的。
    q2 = dict(_Q)
    q2["100.HSI"] = {"f2": 24000.0, "f3": -3.90, "f14": "恒生指数"}
    now = dt.datetime.fromisoformat("2026-11-06T15:10:00")
    ev2 = [e["k"] for e in collect.evaluate(q2, [], now)
           if str(e.get("k", "")).startswith("idx_")]
    assert "idx_weak" in ev2, (
        f"15:10 港股仍在交易时段，应产出恒指事件；实得 {ev2}")
    assert len(ev2) == 1, (
        f"15:10 A 股已收盘，不应再有 A 股指数事件；实得 {ev2}")

    print("ALL PASS (A 股 3σ 开市闸门 10 个断言)")


if __name__ == "__main__":
    check()
