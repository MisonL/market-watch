"""盘前（09:25）本来就没有当日涨跌榜，不该给读者两个空标题。

`[实测]` 09:25 推送 26 条里 **18 条（69%）**都带着：

    **今日涨幅 TOP3**
    （本段无数据）
    **今日跌幅 TOP3**
    （本段无数据）

盘前个股还没开盘，全市场涨跌榜在**语义上就不存在**——不是"取数失败"。
把它和真正的取数失败（数据源挂了）用同一形态表达，既占版面，
又让读者以为"系统出问题了"。

`build_brief` 里触发渲染的条件是：

    if isinstance(hot.get("top_up"), list) or isinstance(hot.get("top_dn"), list):

空列表也满足 `isinstance(..., list)`，于是照样渲染两个空段。

契约：两个榜**都为空**时不渲染这两个段（无话可说就不说）；
有任意一边有数据时照旧渲染（另一边标"本段无数据"是正确的）。
"""
import os
import sys
import tempfile

_old = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_top3_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import analyze as A

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ① 两榜都空 → 不得出现段头
empty = A._chg_board({"top_up": [], "top_dn": []}, "top_up", "今日涨幅 TOP3", "")
check(empty == [], f"空榜仍渲染出段头: {empty!r}")

# ② 有数据 → 正常渲染
_one = [{"name": "半导体", "chg": 3.18}]
got = A._chg_board({"top_up": _one, "top_dn": []}, "top_up", "今日涨幅 TOP3", "")
check(bool(got) and "半导体" in "".join(got), f"有数据却没渲染: {got!r}")

# ③ 有数据但另一边空 → 空的那边仍要如实标注（不能连"无数据"都吞掉）
none_side = A._chg_board({"top_up": _one, "top_dn": []}, "top_dn", "今日跌幅 TOP3", "")
check(any("本段无数据" in x for x in none_side),
      f"一边有数据时，另一边应标注无数据: {none_side!r}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (空榜不渲染段头；有数据正常；单边空仍如实标注)")
