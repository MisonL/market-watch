"""VIX 备用源（新浪 znb_VIX）—— 腾讯 usVIX 是冻结占位值（2026-11-03 修）。

背景：腾讯对 `usVIX` 返回**冻结占位值**（时间戳停在 2026-09-14/15，值恒
21.67/0.00%），陈旧闸门（`_OV_STALE_H=96h`）每天都在丢弃它 —— 于是盘前
简报**长期缺失 VIX 这个"跌是普跌还是恐慌"的判断维度**，三源里只有腾讯供
VIX 且那个源已死。`[实测]` 2026-11-03：新浪 `znb_VIX` 返回**活的**现货
（`VIX恐慌指数,21.1400,1.73,8.91,...,2026-11-03,05:15:01,...`）。

判据与取值分离：`_sina_vix_row(body)` 是纯函数，喂构造响应即可测。
"""
import os, sys, tempfile, atexit, shutil
# ⚠️ 只在**本文件自己建**临时 MW_DIR 时才注册清理（setdefault 语义）：
# 调用方若已给 MW_DIR（如变异回归的沙箱），绝不能删它。原先建了不删，
# `[实测]` 2026-11-03：/tmp 里堆了 36 个 `mw_t_*` 空目录。
if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import marketlib as M

# 真实形态：名称,最新,涨跌额,涨跌幅%,空,空,日期,时间,开,高,低,昨收,标志
GOOD = ('var hq_str_znb_VIX="VIX恐慌指数,21.1400,1.73,8.91,,,'
        '2026-11-03,05:15:01,19.0500,19.4100,21.7700,18.0300,0";')

# 1) 正常 → 取第 4 列（涨跌幅%），名称为「VIX波动率」（与腾讯同标签，
#    便于 _ov_line 与去重口径一致）
row = M._sina_vix_row(GOOD, today="2026-11-03")
assert row == ("VIX波动率", 8.91), row
print("PASS 1 parse-good")

# 2) 空响应（新浪对不存在的代码返回 `var hq_str_xxx="";`）→ None
assert M._sina_vix_row('var hq_str_znb_VIX="";', today="2026-11-03") is None
print("PASS 2 empty-none")

# 3) 字段不足 → None（不能 IndexError）
assert M._sina_vix_row('var hq_str_znb_VIX="VIX,21.14";',
                       today="2026-11-03") is None
print("PASS 3 short-none")

# 4) 非数值 → None
assert M._sina_vix_row('var hq_str_znb_VIX="VIX,a,b,c,,,2026-11-03,05:15";',
                       today="2026-11-03") is None
print("PASS 4 non-numeric-none")

# 5) ★陈旧日期 → None（防重蹈腾讯"冻结占位值"的覆辙：新浪若某天也冻结，
#    不能把几个月前的 VIX 当今天的）
STALE = ('var hq_str_znb_VIX="VIX恐慌指数,21.1400,1.73,8.91,,,'
         '2026-09-15,05:15:01,19.0500,19.4100,21.7700,18.0300,0";')
assert M._sina_vix_row(STALE, today="2026-11-03") is None, \
    "陈旧日期的 VIX 必须被丢（否则重蹈腾讯冻结值的覆辙）"
print("PASS 5 stale-date-dropped")

# 5b) ★跨周末的旧日期仍可用（2026-11-05 修）：VIX 是美股品种，周一早上
#     （北京时间）最新数据是**上周五**的 —— "必须今天"会让每个周末都判
#     VIX 缺失。窗口与 `_ov_tencent` 同源：_OV_STALE_H = 96h（4 天）。
FRI = ('var hq_str_znb_VIX="VIX恐慌指数,21.1400,1.73,8.91,,,'
       '2026-10-30,05:15:01,19.0500,19.4100,21.7700,18.0300,0";')
assert M._sina_vix_row(FRI, today="2026-11-02") is not None, \
    "3 天前的 VIX 应在 96h 窗口内被采用（周一/美股假日场景）"
print("PASS 5b weekend-vix-kept-within-96h")
# 边界外：_OV_STALE_H 天数 +1 → 丢
assert M._sina_vix_row(FRI, today="2026-11-04") is None, \
    "超过 96h 窗口的 VIX 必须丢弃"
print("PASS 5c beyond-96h-dropped")

# 6) 日期可解析且为今天 → 采用
assert M._sina_vix_row(GOOD, today="2026-11-03") is not None
print("PASS 6 fresh-date-kept")

# 7) 脏 body（HTML / 空串 / None）不抛
for bad in ("", "<html>403</html>", None, "garbage"):
    assert M._sina_vix_row(bad, today="2026-11-03") is None, bad
print("PASS 7 dirty-input-none")

# 8) usVIX 已从腾讯 OVERSEAS 名单移除（死源不再每轮白解析）
assert "usVIX" not in [c for c, _ in M.OVERSEAS], \
    "usVIX 是腾讯冻结占位值，应从 OVERSEAS 移除"
print("PASS 8 usVIX-removed-from-tencent")
print("ALL PASS")
