"""指数异动的「两轮确认」必须同向 —— 2026-11-06 全量复核 P1-10。

`alert.check_index` 的确认闸门语义是「**同一个**异动持续到下一轮新观测才推」，
防的是单帧尖峰。而 pending 键原先只含 `sid + 日期 (+ 观测时刻)`，**不含方向**：

    10:00  上证 +4.0%  -> 记 pending
    10:05  上证 -4.0%  -> 观测时刻不同 -> 判「确认通过」-> 直接推

可这两次不是同一个异动：一个涨破、一个跌破，中间没有任何持续性。等于
**反向尖峰只要出现一次就直接推**，两轮确认完全失效。

修法：方向编进键（`_+` / `_-`），确认只在同向的 pending 之间成立；
反向尖峰重新进入 pending，等它自己的第二轮。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_index_confirm_direction.py`
"""
import os, sys, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import datetime as dt
import alert

_NOW = dt.datetime.fromisoformat("2026-11-06T10:00:00")


def _q(f3, ts):
    return {"1.000001": {"f2": 3300.0, "f3": f3, "f14": "上证指数",
                         "f104": 100, "f105": 900, "_ts": ts}}


def _fresh():
    return {"fired": [], "inflight": [], "pending_idx": []}


def _names(hits):
    return [f"{h['name']} {h['chg']:+.2f}" for h in hits]


def check():
    # ① 反向尖峰不得被当确认
    st = _fresh()
    r1 = alert.check_index(_NOW, st, lambda: _q(+4.0, "2026-11-06 10:00:00"))
    assert r1 == [], f"首轮只该记 pending，实得 {_names(r1)}"
    r2 = alert.check_index(_NOW, st, lambda: _q(-4.0, "2026-11-06 10:05:00"))
    assert r2 == [], (
        f"反向尖峰必须重新进 pending，不得当同向确认；实得 {_names(r2)}")
    # 反向那一轮自己也该留下 pending，等它的第二轮
    assert any("_-" in str(k) for k in st["pending_idx"]), (
        f"反向观测应记入 pending，实得 {st['pending_idx']}")

    # ② 同向第二帧才确认（闸门本身不能被关掉）
    st = _fresh()
    alert.check_index(_NOW, st, lambda: _q(+4.0, "2026-11-06 10:00:00"))
    r2 = alert.check_index(_NOW, st, lambda: _q(+4.2, "2026-11-06 10:05:00"))
    assert len(r2) == 1 and r2[0]["key"].startswith("idx_"), (
        f"同向第二轮应确认，实得 {_names(r2)}")

    # ③ 同帧复用仍不算第二轮（2026-11-01 修的既有判据，不得回归）
    st = _fresh()
    alert.check_index(_NOW, st, lambda: _q(+4.0, "2026-11-06 10:00:00"))
    r2 = alert.check_index(_NOW, st, lambda: _q(+4.0, "2026-11-06 10:00:00"))
    assert r2 == [], f"同一帧数据不得确认，实得 {_names(r2)}"

    # ④ 回落（回到阈值内）必须清掉两个方向的 pending
    st = _fresh()
    alert.check_index(_NOW, st, lambda: _q(+4.0, "2026-11-06 10:00:00"))
    alert.check_index(_NOW, st, lambda: _q(+0.2, "2026-11-06 10:05:00"))
    assert st["pending_idx"] == [], (
        f"回落后应清空 pending，实得 {st['pending_idx']}")

    # ⑤ 反向之后同向再来：仍需要一个自己的"第二轮"
    st = _fresh()
    alert.check_index(_NOW, st, lambda: _q(+4.0, "2026-11-06 10:00:00"))
    alert.check_index(_NOW, st, lambda: _q(-4.0, "2026-11-06 10:05:00"))
    r = alert.check_index(_NOW, st, lambda: _q(-4.1, "2026-11-06 10:10:00"))
    assert len(r) == 1 and r[0]["chg"] < 0, (
        f"反向尖峰的第二帧（同向）应确认，实得 {_names(r)}")

    print("ALL PASS (指数确认同向 5 个断言)")


if __name__ == "__main__":
    check()
