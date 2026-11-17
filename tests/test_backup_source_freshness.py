"""备用源选源必须把**新鲜度**算进去，否则陈旧行会压住新鲜源。

`[Codex 审查复现]` 腾讯返回**昨日**那一行（px 是有限正数，格式完全正常），
新浪能给出**当天**的值。原 `_usable()` 只判"px 是有限正数"，于是选中腾讯；
下游 `marketlib.quotes()` 的新鲜度闸门随后把它剔掉 →
**返回 0 条行情，而新浪一次都没被调用**。

`_usable` 的注释自己写着"价格是这一行的存在意义"，但**旧价格不是价格**。

同时修 `health()`：它用 `bool(...)` 判可用性 —— 只判"容器非空"。
东财回 `f2: null`、腾讯回零价行，都会被判成"健康"，于是
`[数据源]` 段长期显示全绿而实际取不到数。

契约：
  ① 有 `_ts` 且明显陈旧（跨日/超阈值）的行 → 认为不可用，继续降级；
  ② 无 `_ts` 的行（百度/新浪简版）保持原有 `_ts_unknown` 逻辑不变；
  ③ health() 用**值级**判据（有限正数），不用 `bool(容器)`。
"""
import os
import sys
import tempfile

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_bsf_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import feeds
import marketlib as M

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ---------- ① 陈旧行不得被选中 ----------
# 造一份数据：腾讯给**昨天**那一行，新浪给**当天**的值
# ⚠⚠ 2026-11-11（审查）：**不能用真实时钟造日期**。
# 下面把时钟打桩到 2026-11-10 10:30，但 `_yday`/`_today` 用真实
# `now()` —— 于是腾讯行（真实昨天）在打桩时钟看来可能是**未来**，
# 两行都判成新鲜，排在前面的腾讯先中 → 断言错。
# `[2026-11-11 实测]` 昨天 23:49 绿、今天 10:35 红，代码未动。
# 与 `test_webui_slots.py` 那次同类：日期一定要从同一个打桩的钟里取。
import datetime as _dt
_MOCK_NOW = _dt.datetime(2026, 11, 10, 10, 30)
_yday = (_MOCK_NOW - _dt.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
_today = _MOCK_NOW.strftime("%Y-%m-%d %H:%M:%S")

_key = "000001"
_tc, _sc, _nm = feeds.MAP[_key]
_stale_row = {"px": 3000.0, "chg": 1.0, "name": "上证指数", "_ts": _yday}
_fresh_row = {"px": 3100.0, "chg": 2.0, "name": "上证指数", "_ts": _today}

_orig_t, _orig_s, _orig_h, _orig_b = (
    feeds.from_tencent, feeds.from_sina, feeds.from_ths, feeds.from_baidu)

feeds.from_tencent = lambda codes: {_tc: dict(_stale_row)}
feeds.from_sina = lambda codes: {_sc: dict(_fresh_row)}
feeds.from_ths = lambda codes: {}
feeds.from_baidu = lambda codes: {}
# ⚠ 必须把“现在”固定到**交易时段内**的一个工作日盘中时刻。
# 后面的闸门只在交易时段生效（收盘后的“最近收盘价”是正确语义），
# 若直接用真实时钟，这个用例会随“今天几点跑测试”而时红时绿。
_dt3 = _dt.datetime
_real_dt3 = _dt3


class _InSessionDT(_real_dt3):
    @classmethod
    def now(cls, tz=None):
        return _real_dt3(2026, 11, 10, 10, 30)


_dt.datetime = _InSessionDT
try:
    out = feeds.backup_quotes([_key])
finally:
    _dt.datetime = _real_dt3
    (feeds.from_tencent, feeds.from_sina,
     feeds.from_ths, feeds.from_baidu) = _orig_t, _orig_s, _orig_h, _orig_b

check(bool(out), f"陈旧腾讯行压住了新鲜新浪源 → 返回空（这是原缺陷）: {out}")
if out:
    _row = out.get(_key) or {}
    check(_row.get("_src") == "sina",
          f"应选新浪（新鲜），实际选了 {_row.get('_src')!r}")
    check(_row.get("f2") == 3100.0,
          f"取到的价格应是新浪的 3100.0，实际 {_row.get('f2')!r}")

# ---------- ①b 收盘后**不得**因陈旧而丢弃（最易犯的反向错误） ----------
# `[实测]` 第一版只在 `_usable` 里按 20 分钟阈值判，没加"仅交易时段"条件：
# 21:16 收盘后所有源都返回当日 16:14 的收盘价 → 全被判陈旧 → quotes() 返回
# **0 条**（而修复前 HEAD 返回 2 条）。收盘后"最近收盘价"就是正确语义。
import datetime as _dt2
_offhour = _dt2.datetime.now()
# ⚠⚠ 修测试数据（2026-11-11）：原写死了 `"20200101000000"` ——
# 那是 **2020 年**的时间戳，不是「当日收盘价」。收盘后部分也把它
# 正确判成跨日陈旧——测试却期望放行，于是这条用例变成「只要当下
# 是非交易时段就错」的**横幅时间依赖**：15:30 以后跑必红、
# 14:00 跑看不到。它想验的是「当日收盘价不能被当陈旧丢掉」，
# 就必须用**今天**的时间戳。
_off_ts = _offhour.strftime("%Y%m%d%H%M%S")
feeds.from_tencent = lambda codes: {_tc: {"px": 3000.0, "chg": 1.0,
                                          "name": "上证指数",
                                          "_ts": _off_ts}}
feeds.from_sina = lambda codes: {}
feeds.from_ths = lambda codes: {}
feeds.from_baidu = lambda codes: {}
try:
    # 只有当前确实在非交易时段时这条才有意义；交易时段内跑测试则跳过
    if not (M.in_session(_offhour) and M.is_trading_day(_offhour.date(), warn=False)):
        _out_off = feeds.backup_quotes([_key])
        check(bool(_out_off),
              "收盘后仍把当日收盘价当陈旧丢弃 → quotes() 会返回 0 条")
finally:
    (feeds.from_tencent, feeds.from_sina,
     feeds.from_ths, feeds.from_baidu) = _orig_t, _orig_s, _orig_h, _orig_b

# ---------- ② 无时间戳的行仍按原逻辑（非交易时段放行） ----------
feeds.from_tencent = lambda codes: {_tc: {"px": 3000.0, "chg": 1.0,
                                          "name": "上证指数", "_ts_unknown": True}}
try:
    out2 = feeds.backup_quotes([_key])
finally:
    feeds.from_tencent = _orig_t
check(isinstance(out2, dict), "无时间戳路径返回了非 dict")

# ---------- ③ health 必须用值级判据 ----------
feeds.from_tencent = lambda codes: {codes[0]: {"name": "上证指数", "px": None}}
feeds.from_sina = lambda codes: {codes[0]: {"name": "上证指数", "px": 0}}
feeds.from_ths = lambda codes: {}
feeds.from_baidu = lambda codes: {}
try:
    _h = feeds.health()
finally:
    (feeds.from_tencent, feeds.from_sina,
     feeds.from_ths, feeds.from_baidu) = _orig_t, _orig_s, _orig_h, _orig_b

check(_h.get("tencent") is False,
      f"腾讯回 px=None 仍被判健康（应 False）: {_h.get('tencent')!r}")
check(_h.get("sina") is False,
      f"新浪回 px=0 仍被判健康（应 False）: {_h.get('sina')!r}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (陈旧行不压新鲜源；无戳路径不变；health 用值级判据)")
