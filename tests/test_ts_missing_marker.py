"""主行情路径「无时间戳」的行必须可观测 —— 2026-11-07 复核 F5。

`quotes()` 末尾的新鲜度闸门有三种处理，其中两种是**静默放行**：

| 形态 | 处理 |
|---|---|
| `_ts_unknown`（备用源明确说读不到时间） | 丢行 |
| `_ts` 存在但陈旧 | 丢行 |
| `_ts` 缺失 | **放行**（`if not _v.get("_ts"): return False`） |
| `_ts` 存在但解析失败 | 放行 + 记一行日志 |
| 主路径 f124 缺失/非数值 → 连 `_ts` 都没有 | 放行 + **完全静默** ← F5 |

最后一行是**第四种产出路径**：`_fetch` 会强制请求 `f124` 并注入 `_ts`，
但 f124 缺失、是字符串（东财对未交易的标的会给 `"-"`）或为 0 时，
`isinstance(_e, (int, float))` 不成立 → 什么都不做 → 该行既无 `_ts`
也无任何标记 → 新鲜度闸门跳过它。

**为什么不直接把这类行判成「丢」**：这是**主路径**，而 f124 在集合竞价/
停牌期间本来就可能缺失 —— 直接 fail-closed 会让整个市场在那些时段集体
消失，且要到下一个交易日盘中才会发现。所以本轮只把它从「完全静默」
改成「可观测」：标 `_ts_missing` 并在闸门里记一行 FEED 日志，
真实占比出来之后再决定是否锁死。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_ts_missing_marker.py`
"""
import os, sys, tempfile, atexit, shutil, datetime, inspect

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M


def check():
    # ① f124 不可用的行必须被标记（不能什么都不做）—— 直接喂行，不看源码子串
    assert hasattr(M, "_inject_ts"), (
        "`_ts` 注入必须抽成 `_inject_ts(o)`，否则无法单独验证")

    _o = {
        "no_f124":  {"f2": 1.0},
        "dash":     {"f2": 2.0, "f124": "-"},      # 东财对未交易标的给 "-"
        "zero":     {"f2": 3.0, "f124": 0},
        "bool":     {"f2": 4.0, "f124": True},
        "neg":      {"f2": 5.0, "f124": -1},
        "good":     {"f2": 6.0, "f124": 1762300000},
        "has_ts":   {"f2": 7.0, "_ts": "20260101000000"},
    }
    M._inject_ts(_o)

    for _k in ("no_f124", "dash", "zero", "bool", "neg"):
        assert _o[_k].get("_ts_missing") is True, (
            f"{_k}（f124 不可用）必须标 `_ts_missing`，实得 {_o[_k]}")
        assert not _o[_k].get("_ts"), f"{_k} 不应被注入 _ts"

    # ⚠️ 2026-11-18（CI 实测发现）：**不能硬编码带时区的期望值**。
    #
    # 原写死 `"20251105074640"` —— 那是**本机 CST** 下的结果。CI runner 是
    # UTC，同一时间戳解析出来差 8 小时，于是这条断言在 Linux 上必然失败
    # （`[实测]` `TZ=UTC` 本地可复现，得到 `20251104234640`）。
    #
    # 这里要验的是「f124 是有效 unix 秒 → 被转成 %Y%m%d%H%M%S 并带上
    # `_ts_crossday`」，不是「本机时区恰好是 CST」。所以按**同一规则**
    # 现算期望值：跑测试的机器用什么时区，生产代码也用什么时区，
    # 两者一致即可。这不是放宽断言 —— 下面的格式与 crossday 断言照旧。
    import datetime as _dt
    _want = _dt.datetime.fromtimestamp(1762300000).strftime("%Y%m%d%H%M%S")
    assert _o["good"].get("_ts") == _want, (
        f"正常 unix 秒应注入 _ts={_want}，实得 {_o['good'].get('_ts')}")
    assert len(_o["good"].get("_ts") or "") == 14, "`_ts` 必须是 14 位时间串"
    assert _o["good"].get("_ts_crossday") is True, "主路径注入的行必须标 crossday"
    assert _o["good"].get("_ts_missing") is None, "成功注入的行不该带 missing"

    # ② 已带 `_ts` 的行不被覆盖（快照复用/备用源的时间戳必须原样保留）
    assert _o["has_ts"]["_ts"] == "20260101000000", "不得覆盖上游注入的 _ts"
    assert _o["has_ts"].get("_ts_missing") is None

    # ③ 非 dict 值不能被炸掉（`_valid_quotes` 之前 q 里可能有脏值）
    _o2 = {"bad": "not-a-dict", "ok": {"f2": 1.0}}
    M._inject_ts(_o2)
    assert _o2["bad"] == "not-a-dict"
    assert _o2["ok"]["_ts_missing"] is True

    # ④ 闸门必须能看见这个标记并留痕（否则标了也没用）
    # ⚠️ 2026-11-07（测试审计）：原断言 `"_ts_missing" in inspect.getsource(...)`
    # 是**假绿** —— 那几处出现里有 3 处是**注释**，把真实判断删掉、只在注释里
    # 留一句「应读 _ts_missing 记日志」，8 条断言仍然全绿。
    # 改成行为级：桩 `_fetch` 产出一条带 `_ts_missing` 的行，真跑 `quotes()`，
    # 从捕获的日志里断言留痕。
    # ⚠️ 闸门整段受 `in_session(_now) and 交易日` 守卫，而 `_now` 是
    # 函数内部 `datetime.datetime.now()` —— 周末/非交易日跑本测试
    # 会整段跳过（实测：周六跑时日志为空）。所以必须把时钟
    # **钉在一个开市交易日的盘中**。
    import alert as _al
    # ⚠️ 必须把**模块**换成一个薅子而不是换掉 `datetime.datetime`：
    # `_is_ts_stale` 内部用 `datetime.datetime.strptime`，直接把
    # `M.datetime` 设成一个类会得到 `no attribute 'datetime'`，
    # 整段闸门被外层 except 吞成「整体异常」。
    import types as _types
    class _FakeDT(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.datetime(2026, 11, 6, 10, 0, 0)
    _fake_mod = _types.SimpleNamespace(datetime=_FakeDT,
                                       date=datetime.date,
                                       time=datetime.time,
                                       timedelta=datetime.timedelta)
    M.datetime = _fake_mod
    _row = {"f2": 1000.0, "f3": -1.0, "f12": "BK1036", "f14": "x",
            "_ts_missing": True}
    _cap_log = []
    _orig = (M.log, M._fetch, M.datetime)
    M.log = lambda msg, tag=None: _cap_log.append(f"[{tag}] {msg}")
    M._fetch = lambda *a, **k: {"90.BK1036": _row}
    try:
        M.quotes(["90.BK1036"])
    finally:
        M.log, M._fetch, M.datetime = _orig
    assert any("无时间戳" in m and "放行" in m for m in _cap_log), (
        "新鲜度闸门必须对无时间戳的行留痕（实测日志：" + repr(_cap_log[:3]) + "）")

    # ⑤ 方向不变：无时间戳的行**仍然放行**（本轮只加可观测性，不改判定）
    _now = datetime.datetime(2026, 11, 6, 10, 0, 0)
    assert M._is_ts_stale(None, _now) is not True, (
        "无时间戳时闸门不得判陈旧（本轮不改这个方向）")

    print("ALL PASS (无时间戳标记 8 个断言)")


if __name__ == "__main__":
    check()
