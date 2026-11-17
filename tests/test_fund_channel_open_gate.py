"""三条推送通道都必须服从 A 股开市闸门 —— 2026-11-07 复核 F1 剩余实例。

## 缺陷

`should_alert()` 依次调用三条通道：

    check_index(now, state)          ← F1 只给这一条加了 `a_session_open` 闸门
    check_sector_fund(now, state)    ← 一行闸门都没有
    check_market_fund(now, state)    ← 一行闸门都没有

后两条读的都是**当日累计值**：
- `f184` = 板块当日涨跌占比 / 净流入占比
- `f62`  = 主力净额（当日累计）
- `f6`   = 成交额（当日累计）

盘前（09:25~09:29，`a_session_open` 为假）拿到的是**上一交易日全天的冻结值**，
拿它跟「今日资金异动」的阈值比，就是用昨天的事说今天。

而 `trading_guard` 在 09:26/09:29 是**放行**的（它只挡 09:30–09:59 的开盘
半小时 3σ 误报），所以这条路径完全可达。

`[实测]` 沙箱桩 ROWS、2026-11-06（周五）：
    09:26 a_session_open=False → check_sector_fund 产出 2 条事件（与 10:30 相同）
    09:29 a_session_open=False → 同样 2 条
修复后：09:26 / 09:29 → 0 条，09:35 / 10:30 → 2 条。

## 为什么这个测试必须单列

`tests/test_ashare_push_channel_gate.py` 覆盖的是 detect / collect / check_index
三条，**恰好漏掉了这两条资金通道** —— 所以 F1 当时的"三通道一致性"是
按错误的通道清单验的。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_fund_channel_open_gate.py`
"""
import os, sys, json, tempfile, atexit, shutil, datetime as dt

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

_CONF = os.path.join(os.environ["MW_DIR"], "conf")
os.makedirs(_CONF, exist_ok=True)
_SRC_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "conf", "sectors.json")
if os.path.exists(_SRC_CONF) and not os.path.exists(
        os.path.join(_CONF, "sectors.json")):
    shutil.copy(_SRC_CONF, os.path.join(_CONF, "sectors.json"))

import marketlib as M
import alert as A

# 一条远超所有阈值的板块资金行（跌方向，f184/f62 都过线）
_ROW = {"f12": "BK1036", "f14": "90.BK1036", "f2": 1000.0, "f3": -3.0,
        "f62": -9e9, "f184": -12.5}

# 2026-11-06 是周五（交易日）
_DAY = (2026, 11, 6)


def _t(hh, mm):
    return dt.datetime(*_DAY, hh, mm, 0)


def _stub():
    _orig = (A._sector_rows, A._watch_codes)
    A._sector_rows = lambda fs: [dict(_ROW)]
    A._watch_codes = lambda: {"BK1036"}
    return _orig


def _restore(o):
    A._sector_rows, A._watch_codes = o


def _fresh_state():
    return {"date": "2026-11-06", "fired": [], "inflight": [],
            "pending_idx": [], "hourly_retry": {}, "count": 0,
            "pending": {}, "inflight_ts": 0}


def check():
    _o = _stub()
    try:
        # 闸门本身：09:26 与 09:29 必须判为「未开市」
        assert M.a_session_open(_t(9, 26)) is False
        assert M.a_session_open(_t(9, 29)) is False
        assert M.a_session_open(_t(9, 35)) is True
        assert M.a_session_open(_t(10, 30)) is True

        # ① 板块资金：盘前必须零产出
        for _hm in ((9, 26), (9, 29)):
            _h = A.check_sector_fund(_t(*_hm), _fresh_state())
            assert not _h, (
                f"check_sector_fund 在 {_hm[0]:02d}:{_hm[1]:02d}（未开市）"
                f"仍产出 {len(_h)} 条事件 —— 用的是上一交易日冻结的当日累计值")

        # ② 开盘后必须恢复正常产出（防修成永久零产出）
        for _hm in ((9, 35), (10, 30)):
            _h = A.check_sector_fund(_t(*_hm), _fresh_state())
            assert _h, (
                f"check_sector_fund 在 {_hm[0]:02d}:{_hm[1]:02d}（已开市）"
                f"产出 0 条 —— 闸门把有效判定一起挡住了")

        # ③ 大盘资金：盘前必须零产出，且**连 quotes_fn 都不该调**
        _calls = []
        _qfn = lambda: (_calls.append(1), {"000001": {"f62": -2e11,
                                                      "f6": 1e12}})[1]
        for _hm in ((9, 26), (9, 29)):
            _h = A.check_market_fund(_t(*_hm), _fresh_state(), quotes_fn=_qfn)
            assert not _h, (
                f"check_market_fund 在 {_hm[0]:02d}:{_hm[1]:02d} 仍产出事件")
        assert not _calls, (
            "盘前不该调用 quotes_fn —— 已经确定要返回空，多取一次是纯浪费")

        # ④ 三条通道口径必须一致（这是 F1 原本要保的性质）
        import analyze as _an
        for _hm in ((9, 26), (9, 29)):
            _t0 = _t(*_hm)
            _live = M.a_session_open(_t0)
            assert _live is False
            assert not A.check_sector_fund(_t0, _fresh_state())
            assert not A.check_market_fund(_t0, _fresh_state(),
                                           quotes_fn=lambda: {})
    finally:
        _restore(_o)

    print("ALL PASS (资金双通道开市闸门 8 个断言)")


if __name__ == "__main__":
    check()
