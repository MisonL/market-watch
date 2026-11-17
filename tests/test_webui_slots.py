"""webui 的槽位判定（build_slots）—— 面板说"漏推"到底准不准。

这是面板**唯一**会让人做出判断的输出：看到红色 missed 会去查 launchd，
看到 pending 会知道还在窗口内。判错方向会把人引向错误的排查路径。

覆盖 2026-11-10 改过的两处：
  ① 到点 ≠ 漏推：窗口未过时应为 pending，不是 missed
  ② 轮次数只算 analyze 的 end 记录（不能混进 collect 与 analyze 的 start）

webui 本身不 import analyze（会触发连网与写 state），故此处也不 import 它。
"""
import datetime
import json
import os
import sys
import tempfile

_ROOT = tempfile.mkdtemp(prefix="mw_wui_")
os.environ["MW_DIR"] = _ROOT
for _d in ("state/history", "state/cache", "conf", "logs"):
    os.makedirs(os.path.join(_ROOT, _d), exist_ok=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import webui

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


COMPLETED = 0

# ---------- ① 纯函数：窗口宽限 ----------
check(webui._slot_window_grace("10:00") == webui._FALLBACK_SLOT_TTL["10:00"]
      + webui._RETRY_GRACE,
      f"10:00 宽限算错: {webui._slot_window_grace('10:00')}")
check(webui._slot_window_grace("不存在") == 20 + webui._RETRY_GRACE,
      "未知槽位应回退到 20 分钟默认值")

# ---------- ② 到点但窗口未过 → pending ----------
# 直接构造一份当日 runs/pushes 为空的场景，然后用不同的"当前时刻"跑。
_SLOTS = {t[0]: t[2] for t in webui._FALLBACK_SLOTS if len(t) >= 3}
_1000 = _SLOTS.get("10:00", "10:00")
_h, _m = _1000.split(":")[:2]
_plan_min = int(_h) * 60 + int(_m)
_grace = webui._slot_window_grace("10:00")

_today = datetime.date.today().isoformat()
# 写空文件，保证没有执行/推送记录
open(webui.PUSHES, "w").close()
open(webui.RUNS, "w").close()
try:
    _st = {}
    json.dump({"date": _today, "fired": []},
              open(webui.STATE, "w", encoding="utf-8"))
except Exception as e:
    print(f"  (state 写入跳过: {e})")

# ⚠ 必须把钟固定到“刚过 10:00、窗口还未过”的时刻，
# 否则“pending 改回 missed”这个注入根本碍不到 —— 用真实时钟跑时，
# 测试多半在盘后执行，那时全部槽位都是 missed，断言恒真。
# ⚠⚠ 2026-11-11（修自身缺陷）：不仅要固定**时刻**，还要固定**日期**。
# 初版只把 `datetime.datetime` 打桩，而 `build_slots` 里的
#   `is_today = (date == today)` / `is_future = _d_obj > datetime.date.today()`
# 用的是 `datetime.date.today()` —— **没打桩**，取的是真实日期。
# 于是 "把日期固定成 2026-11-10"只在 **11-10 当天**成立，
# 第二天（11-11）`is_today=False` → 全部槽位落到 missed → 测试变红。
# `[实测]` 11-10 23:49 绿，11-11 08:08 红，代码未动。
# 这正是本次审查在别处批评的那类缺陷（测试随「今天几号」漂），
# 写在自己新写的测试里更丢人 —— 一并打桩 `date`。
_dt4 = datetime.datetime
_real4 = _dt4
_realdate4 = datetime.date
_MOCK_DAY = _realdate4(2026, 11, 10)


class _At1005(_real4):
    @classmethod
    def now(cls, tz=None):
        return _real4(_MOCK_DAY.year, _MOCK_DAY.month, _MOCK_DAY.day, 10, 5)

    @classmethod
    def today(cls):
        return _real4(_MOCK_DAY.year, _MOCK_DAY.month, _MOCK_DAY.day, 10, 5)


class _MockDate(_realdate4):
    @classmethod
    def today(cls):
        return _MOCK_DAY


_mock_today = _MOCK_DAY.isoformat()
_try_state = {"date": _mock_today, "fired": []}
try:
    json.dump(_try_state, open(webui.STATE, "w", encoding="utf-8"))
except Exception:
    pass
datetime.datetime = _At1005
datetime.date = _MockDate
try:
    _view = webui.build_slots(_mock_today)
finally:
    datetime.datetime = _real4
    datetime.date = _realdate4
check(isinstance(_view, dict), f"build_slots 应返回 dict，实际 {type(_view).__name__}")
_rows = [r for r in ((_view or {}).get("slots") or [])
         if isinstance(r, dict)]
check(bool(_rows), "build_slots 的 slots 为空")

# 面板对"今天"的时刻取自真实时钟，故这里只能断言**结构契约**：
# 每行必须有一个已知状态词，且 pending 必须在白名单里（前端映射依赖它）。
_KNOWN = {"done", "pushed", "failed", "skipped", "missed",
          "not_yet", "n/a", "pending", "no_trigger", "skip_lock",
          "skip_quota", "skip_dry", "slot_expired", "import_fail"}
_bad_states = sorted({str(r.get("status")) for r in _rows
                      if r.get("status") and str(r.get("status")) not in _KNOWN})
check(not _bad_states, f"出现了前端不认识的状态词: {_bad_states}")

# ①b **行为层**：10:05 时，10:00 那格还在 35 分钟窗口内 → 必须是 pending，
# 不能是 missed。这才是“到点≠漏推”真正要守的东西（上面那条只验证词汇合法）。
_1000row = next((r for r in _rows if r.get("key") == "10:00"), None)
check(_1000row is not None, "10:00 槽位行缺失")
if _1000row:
    check(_1000row.get("status") == "pending",
          f"10:05 时 10:00 应为 pending（窗口未过），实为 {_1000row.get('status')!r}")
# 09:25 已过 40 分钟（宽限 23 分）→ 应是 missed
_925row = next((r for r in _rows if r.get("key") == "09:25"), None)
if _925row:
    check(_925row.get("status") == "missed",
          f"10:05 时 09:25 已超窗口，应为 missed，实为 {_925row.get('status')!r}")
# 13:30 尚未到点 → not_yet
_1330 = next((r for r in _rows if r.get("key") == "13:30"), None)
if _1330:
    check(_1330.get("status") == "not_yet",
          f"10:05 时 13:30 还未到点，应为 not_yet，实为 {_1330.get('status')!r}")

# ---------- ③ 前端必须有 pending 的映射 ----------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "webui.py"), encoding="utf-8").read()
check('pending:"b-not_yet"' in _src,
      "前端 badge() 缺 pending 映射 → 状态会原样显示给读者")

# ---------- ④ 轮次数不得混算 ----------
check('"run_count": len(runs_rounds)' in _src,
      "run_count 仍用全量 runs（会把 1 轮显示成 3 轮）")
# ⚠ 只有“提前开放”的槽位才带窗口字段 —— 它们才需要解释
# “计划 09:25，为何 09:20 就推了”。企图写成“所有行都要有”是错的。
for _r in _rows:
    if _r.get("key") in webui._SLOT_OPEN_EARLY:
        check("window_from" in _r and "window_to" in _r,
              f"\u63d0\u524d\u5f00\u653e\u7684\u69fd\u4f4d {_r.get('key')} \u7f3a\u7a97\u53e3\u5b57\u6bb5")
        # 窗口上限必须与 pending 判定同一口径（含 _RETRY_GRACE）
        _p = webui._slot_min(_r.get("planned"))
        if _p is not None:
            _exp = _p + webui._slot_window_grace(_r["key"])
            _got = _r["window_to"]
            check(f"{_exp//60:02d}:{_exp%60:02d}" == _got,
                  f"window_to={_got} 与宽限 {_exp//60:02d}:{_exp%60:02d} 不一致")
check('runs_rounds: list[dict[str, Any]] = [' in _src,
      "缺少 runs_rounds 拆分（孤儿 start 判据依赖全量 runs）")

# ---------- ⑤ 只读断言仍然在 ----------
try:
    webui._assert_readonly()
    check(True, "")
except Exception as e:
    check(False, f"_assert_readonly 抛异常: {e}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (窗口宽限/pending 状态/前端映射/轮次不混算/只读断言)")
