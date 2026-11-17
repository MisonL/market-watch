"""A 股开市闸门必须落在**三条**推送通道上，而不只是采集 —— 2026-11-07 复核 F1。

2026-11-06 的 P1-2 只把闸门接到了 `collect.evaluate` 的指数行。但同一条病
（未连续竞价时 f3/f184 还是**上一交易日**的冻结值）在另外两处原封不动：

  `[A/B 实测]` 回放 11-06 09:26（A 股尚未连续竞价，港股也未开）：

    collect.evaluate(...)   -> []                        ✅ 已修
    analyze.detect(...)     -> [idx_weak, hs300_weak]    ❌ 用昨日 f3 造事件
    alert.check_index(...)  -> 第二轮吐出「上证指数 -3.20%」❌ 抢走紧急名额

后两者会把事件键写进 `inflight`/`fired` 并**直接推一条消息**。一旦占住去重键，
**开盘后真的跌破时反而不再报** —— 这正是 P1-2 自己要修的症状。

本文件锁住三处闸门，并同时锁住「开盘后照常工作」这一侧（防止修成永久静默）。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_ashare_index_gate_channels.py`
"""
import os, sys, datetime as dt, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import analyze
import alert

# 2026-11-06 是交易日（周五）。09:26/09:29 未连续竞价；10:00 在盘中。
_DAY = "2026-11-06"
# 上证 -3.20% 越过 `index_weak`(-1.0) 与 3σ(2.70)；沪深300 -2.50% 越过 hs300_weak(-1.2)
_Q = {
    "1.000001": {"f2": 3300.0, "f3": -3.20, "f14": "上证指数",
                 "f184": 9.5, "f62": -3e9, "f104": 1, "f105": 9},
    "1.000300": {"f2": 3800.0, "f3": -2.50, "f14": "沪深300",
                 "f184": 9.5, "f62": -3e9, "f104": 1, "f105": 9},
    "100.HSI": {"f2": 24000.0, "f3": -3.80, "f14": "恒生指数",
                "f184": 9.5, "f62": -3e9, "f104": 1, "f105": 9},
}


def _at(hm):
    return dt.datetime.fromisoformat(f"{_DAY}T{hm}:00")


def _q_with_ts(hm):
    """带 `_ts` 的行情（alert 的两轮确认需要"新观测"判据）。"""
    out = {}
    for k, v in _Q.items():
        d = dict(v)
        d["_ts"] = f"{_DAY} {hm}:00"
        out[k] = d
    return out


def _empty_st():
    return {"date": _DAY, "fired": [], "inflight": [],
            "pending_idx": [], "hourly_retry": {}, "count": 0}


def check():
    # ── ① analyze.detect：未连续竞价不得用昨日 f3 造出 A 股事件 ──
    for _hm in ("09:26", "09:29"):
        _h = analyze.detect(_Q, {}, _at(_hm), _empty_st())
        assert _h == [], f"{_hm} 不该有触发（仍在用昨日 f3），实得 {_h}"

    # ── ② analyze.detect：盘中照常工作（防修成永久静默）──
    _keys = {k for k, _ in analyze.detect(_Q, {}, _at("10:00"), _empty_st())}
    assert "idx_weak" in _keys, f"盘中必须照常产出 idx_weak，实得 {_keys}"
    assert "hs300_weak" in _keys, f"盘中必须照常产出 hs300_weak，实得 {_keys}"

    # ── ③ alert.check_index：未连续竞价不得确认 A 股紧急 ──
    _st = _empty_st()
    for _hm in ("09:26", "09:27", "09:28", "09:29"):
        _r = alert.check_index(_at(_hm), _st, lambda _h=_hm: _q_with_ts(_h))
        assert _r == [], f"{_hm} 不该确认 A 股紧急（用昨日 f3），实得 {_r}"

    # ── ④ alert.check_index：盘中两轮新观测照常确认 ──
    # ⚠⚠ 2026-11-10：必须**把 `ewma_sigma` 打桩**。
    # 原来没打桩，而它读的是**真实市场数据**（日K 序列算 EWMA σ）。
    # `[实测 2026-11-10]` 它当日算出 HSI σ=1.3046 → 阈值 3.914，
    # 而夹具里恒生是 -3.80% —— **差 0.11 没过阈值**，于是断言失败。
    # 这不是时钟问题（上一版注释诊断错了）：同一夹具换一天跑，
    # σ 变了结论就变 —— 测试在监视外部行情。返回 None 即回退到
    # `STATIC_SIGMA` 固定值（1.18），阈值恒为 3.54，-3.80 稳定越过。
    _orig_ewma = alert.ewma_sigma
    alert.ewma_sigma = lambda _sid: None
    try:
        _st2 = _empty_st()
        alert.check_index(_at("10:00"), _st2, lambda: _q_with_ts("10:00"))
        _r2 = alert.check_index(_at("10:05"), _st2, lambda: _q_with_ts("10:05"))
    finally:
        alert.ewma_sigma = _orig_ewma
    _names2 = [h["name"] for h in _r2]
    # ⚠️ 2026-11-08（复核）：这里原写 `== ["上证指数"]` —— **从创建就是错的**。
    # 夹具 `_Q` 故意放了恒生指数 -3.80%，而 10:00 港股**在交易**（`hk_session_open`
    # 实测为 True）。所以确认它是**正确行为**，不是多余。
    # 本项要验的是「A 股闸门没把有效判定一起挡掉」，
    # 所以只应断言**上证在其中**，不该预期它是唯一一条。
    # 实测历史：这条断言在 6e6b223（创建此测试的提交）上就已经失败，
    # 台面上显示「实得 ['上证指数', '恒生指数']」。
    assert "上证指数" in _names2, (
        f"盘中两轮新观测必须确认上证，实得 {_names2}")
    # ✅ 2026-11-10：上面那段“碍于 `index_quotes()` 无 `now`”的遗留已修。
    # `index_quotes(now=...)` 现在与同事 `check_index(now, ...)` 同口径，
    # 恒生的新鲜度判定不再随「几点跑测试」漂移；
    # `alert.check_index()` 也已把本轮时刻传下去，两处看同一个时刻。
    # 故这里**恢复应有的恒生断言**（由 `_q_with_ts` 提供固定时刻）。
    assert "恒生指数" in _names2, (
        f"盘中两轮新观测也应确认恒生，实得 {_names2}")

    # ── ⑤ 港股通道不受影响：09:26 不开盘 → 不确认（原有行为）──
    _st3 = _empty_st()
    alert.check_index(_at("09:26"), _st3, lambda: _q_with_ts("09:26"))
    _r3 = alert.check_index(_at("09:27"), _st3, lambda: _q_with_ts("09:27"))
    assert _r3 == [], f"港股未开盘不该确认，实得 {_r3}"

    # ── ⑥ 判据与 collect 同源：同一时刻三处结论必须一致 ──
    import collect
    for _hm, _want in (("09:26", False), ("10:00", True)):
        _ev = collect.evaluate(_Q, {}, _at(_hm))
        _got = any(e["k"] == "idx_weak" for e in _ev)
        assert _got == _want, (
            f"collect 在 {_hm} 的 idx_weak 结论应={_want}，实得 {_got}")
        _h2 = analyze.detect(_Q, {}, _at(_hm), _empty_st())
        _got2 = any(k == "idx_weak" for k, _ in _h2)
        assert _got2 == _want, (
            f"detect 在 {_hm} 的 idx_weak 结论必须与 collect 一致"
            f"（应={_want}，实得 {_got2}）")

    print("ALL PASS (A 股闸门三通道 · 9 个断言)")


if __name__ == "__main__":
    check()
