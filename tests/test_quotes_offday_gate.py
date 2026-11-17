"""非交易日盘中时段，quotes() 的新鲜度闸门不得丢弃上一交易日收盘价。

复现：2026-11-01（周日）11:28，in_session=True、is_trading_day=False，
上一交易日的 _ts 必然滞后 > 20 分钟 → 全部被丢 → quotes() 返回空，
沙箱自检 [行情取数] ❌，变异回归基线闸门中止整轮。
"""
import os, sys, datetime, tempfile, atexit, shutil
# 只在**本文件自己建**临时 MW_DIR 时才注册清理（setdefault 语义）。
if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import marketlib as M

SUN = datetime.datetime(2026, 11, 1, 11, 28)        # 周日、落在 TH["session"] 内
FRI_CLOSE = "20261030150000"                         # 上一交易日收盘时间戳

class _FakeDT(datetime.datetime):
    @classmethod
    def now(cls, tz=None): return SUN

def _run(row):
    M.datetime.datetime = _FakeDT
    M._fetch = lambda *a, **k: {"1.000001": dict(row)}
    M.cj = lambda *a, **k: None
    try:
        return M.quotes(["1.000001"], allow_backup=True)
    finally:
        M.datetime.datetime = datetime.datetime

assert M.in_session(SUN) and not M.is_trading_day(SUN.date(), warn=False)
q = _run({"f2": 3600.0, "f3": -1.0, "f12": "000001", "f14": "上证指数",
          "_ts": FRI_CLOSE, "_src": "tencent"})
assert "1.000001" in q, f"非交易日把上一交易日收盘价当陈旧丢弃：{q}"
print("PASS")

# 反向：交易日盘中，上一交易日的收盘价仍必须被丢弃（闸门不能被这次修改削弱）
MON = datetime.datetime(2026, 11, 2, 11, 0)
class _FakeMon(datetime.datetime):
    @classmethod
    def now(cls, tz=None): return MON
assert M.is_trading_day(MON.date(), warn=False)
M.datetime.datetime = _FakeMon
M._fetch = lambda *a, **k: {"1.000001": {"f2": 3600.0, "f3": -1.0, "f12": "000001",
                                          "f14": "上证指数", "_ts": FRI_CLOSE, "_src": "tencent"}}
try:
    q2 = M.quotes(["1.000001"], allow_backup=True)
finally:
    M.datetime.datetime = datetime.datetime
assert "1.000001" not in q2, f"交易日盘中陈旧价被放行：{q2}"
print("PASS reverse")
