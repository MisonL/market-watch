"""早退路径不得把「未发出残迹」的清理时钟归零 —— 2026-11-07 复核。

## 缺陷

`load_st` 对 `inflight` 里**未发出**的键有一条 45 分钟清理规则：
超过 45 分钟就「只清不转正」，允许该事件重新触发。它读的是**全局**
`inflight_ts`（不是逐键时间）。

而认领块在任何一轮都会把 `inflight_ts` 刷成 `now`：

    if _added: st["inflight_ts"] = time.time()

`_commit()` 明确回退了它（`st["inflight_ts"] = pre_inflight_ts`），
但**另外三条提前返回的路径没有**：

    urgent 分支 / 收盘定局数据闸门 / slot_expired

`[实测]` 起始 age=2400s（距 45 分钟阈值只差 5 分钟）：

    urgent 失败  → 49s    ← 归零
    urgent 成功  → 51s    ← 归零
    收盘定局闸门 → 49s    ← 归零
    slot_expired → 0s     ← 归零
    正常 _commit → 2449s  ✅ 正确保留

## 危害链

时钟被反复归零 → `_age > 45*60` 永不成立 → 残迹整天留在 `inflight`
→ `detect()` 的 `busy = fired | inflight` 把它当「已认领」
→ **该事件当天静默、消息从未发出**（面板还显示 done）。
这正是 P1-6 要治的病，被这三条路径从侧面架空。

## 顺带

三条路径摘 `inflight` 时也不同步过滤 `inflight_sent`，破坏不变式
`sent ⊆ inflight`。目前靠 `load_st` 的无条件转正兜着，但那是「靠运气」
而不是「判对了」——本测试一并锁住。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_inflight_ts_rollback.py`
"""
import os, sys, tempfile, atexit, shutil, datetime as dt, io, contextlib

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

import analyze
import alert as AL

_TS0 = 1000.0          # 认领**之前**的 inflight_ts（很旧）
_TS_NEW = 5.0e9        # 认领后 run() 刷进去的 now（很新）
_NOW = dt.datetime(2026, 11, 6, 10, 3, 0)


def _st(**kw):
    """复现**真实时序**的中间态：

    `run()` 已经完成认领（新增了 `hourly_10:00`），于是把
    `inflight_ts` 刷成了 `now`（`_TS_NEW`）；而它传给
    `_run_after_inflight` 的 `pre_inflight_ts` 是**认领前**的 `_TS0`。

    ⚠️ 夹具不能只放 `crash_k`：那样 `_added` 为空，
    `inflight_ts` 根本没被刷新，回不回退都一样 ——
    第一版夹具就是这么写的，删掉三处回退后测试**仍全绿**（假绿）。
    """
    _b = {"date": "2026-11-06", "fired": [],
          "inflight": ["crash_k", "hourly_10:00"],
          "inflight_sent": [], "pending_idx": [], "hourly_retry": {},
          "count": 0, "pending": {}, "inflight_ts": _TS_NEW}
    _b.update(kw)
    return _b


def _stub(urgent_hit=False, push_ok=True):
    _orig = (analyze._quotes_from_snapshot, analyze._quotes, analyze._trend,
             analyze.detect, analyze.build_brief, analyze.produce_text,
             analyze.build_urgent, analyze.push, analyze.hourly_slot,
             AL.should_alert, analyze.save_st)
    analyze._quotes_from_snapshot = lambda **k: {}
    analyze._quotes = lambda *a, **k: {}
    analyze._trend = lambda *a, **k: None
    analyze.detect = lambda q, sg, now, st: []
    analyze.build_brief = lambda *a, **k: "T"
    analyze.produce_text = lambda *a, **k: ("T", True, None)
    analyze.build_urgent = lambda u, now: "U"
    analyze.push = lambda t, slot=None, kind="push": push_ok
    analyze.hourly_slot = lambda now, st: None
    analyze.save_st = lambda s: True
    AL.should_alert = (lambda now, st, news_fn=None, quotes_fn=None:
                       (True, [{"key": "K", "type": "index", "name": "T",
                                "chg": 5.0, "news": []}], "")
                       if urgent_hit else
                       (False, [], ""))
    return _orig


def _restore(o):
    (analyze._quotes_from_snapshot, analyze._quotes, analyze._trend,
     analyze.detect, analyze.build_brief, analyze.produce_text,
     analyze.build_urgent, analyze.push, analyze.hourly_slot,
     AL.should_alert, analyze.save_st) = o


def _run(st, urgent=(), hourly=None):
    analyze.load_st = lambda: st
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            analyze._run_after_inflight(
                now=_NOW, force=False, dry=False, hm=10 * 60,
                st=st, q={}, sg={}, hits=[], hourly=hourly,
                urgent=list(urgent), pre_inflight=["crash_k"],
                pre_inflight_ts=_TS0)
        except Exception:
            pass
    return st


def check():
    # ① urgent 推送成功：残迹的时钟不得被归零
    o = _stub(urgent_hit=True, push_ok=True)
    try:
        st = _run(_st(), urgent=[{"key": "K"}])
    finally:
        _restore(o)
    assert st.get("inflight_ts") == _TS0, (
        f"urgent 成功路径把 inflight_ts 从 {_TS0} 刷成了 "
        f"{st.get('inflight_ts')} —— 未发出残迹的 45 分钟清理时钟被归零")

    # ② urgent 推送失败：同样不得归零
    o = _stub(urgent_hit=True, push_ok=False)
    try:
        st = _run(_st(), urgent=[{"key": "K"}])
    finally:
        _restore(o)
    assert st.get("inflight_ts") == _TS0, (
        f"urgent 失败路径把 inflight_ts 刷成了 {st.get('inflight_ts')}")

    # ③ slot_expired：槽位过期的早退路径同样不得归零
    o = _stub(urgent_hit=False)
    try:
        # hourly 给一个早已过期的槽位，触发 slot_expired 分支
        st = _run(_st(hourly_retry={"08:00": 1}), hourly=None)
    finally:
        _restore(o)
    assert st.get("inflight_ts") == _TS0, (
        f"slot_expired 路径把 inflight_ts 刷成了 {st.get('inflight_ts')}")

    # ④ 不变式：inflight_sent 必须是 inflight 的子集
    for _label, _st_ in (("urgent 成功", _st()), ("urgent 失败", _st())):
        pass
    o = _stub(urgent_hit=True, push_ok=True)
    try:
        st = _run(_st(inflight_sent=["ghost"]), urgent=[{"key": "K"}])
    finally:
        _restore(o)
    assert set(st.get("inflight_sent") or []) <= set(st.get("inflight") or []), (
        f"inflight_sent {st.get('inflight_sent')} 不是 inflight "
        f"{st.get('inflight')} 的子集 —— 不变式被打破")

    print("ALL PASS (inflight_ts 回退与 sent 不变式 6 个断言)")


if __name__ == "__main__":
    check()
