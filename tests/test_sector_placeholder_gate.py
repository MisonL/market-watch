"""板块源失效时，AI 不得把「缺数据」写成一行行 `+0.00%（暂无资金数据）`。

`[生产实测]` 2026-11-10 10:04 那条推送：`push2test.eastmoney.com` 挂了，
兜底主机能取指数但**取不到板块序列**，于是 brief 的【关注板块】段是
`（本段无数据）`。AI 没有写「数据未取到」，而是照旧铺了 8 行：

    半导体 +0.00%（暂无资金数据）
    存储芯片 +0.00%（暂无资金数据）
    ...

+0.00% 看起来像"今天平盘"，实际是**完全没有报价**。读者会当成真实涨跌幅。

结构闸门 `_struct_reason` 当时对它是**豁免**的（`（本段无数据）` →
不要求必带段），于是这条直接推出去了。

契约：brief 的板块段是空段时，输出里**不得**出现
「（暂无资金数据）」这类伪造占位行；要么如实说没取到，要么不写这些板块。
"""
import os
import sys
import tempfile

_old = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_sec_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import analyze as A

EMPTY_BRIEF = ("【指数】\n  上证 3900 +0.3%\n"
               "【关注板块】（本段无数据）\n"
               "【港股】\n  恒生 26000 +0.4%\n")
FULL_BRIEF = ("【指数】\n  上证 3900 +0.3%\n"
              "【关注板块】\n  半导体 +1.68%（主力 +2.3 亿）\n"
              "  存储芯片 +2.1%\n"
              "【港股】\n  恒生 26000 +0.4%\n")

_PAD = "x" * 80
_FAKE_ROWS = ("> **开盘异动**\n\n**触发** 未命中阈值\n\n**盘面** 指数普涨\n\n"
              "**关注板块**\n半导体 +0.00%（暂无资金数据）\n"
              "存储芯片 +0.00%（暂无资金数据）\n"
              "CPO概念 +0.00%（暂无资金数据）\n\n"
              "**建议** 看硬件链能否承接\n" + _PAD)
_HONEST = ("> **开盘异动**\n\n**触发** 未命中阈值\n\n**盘面** 指数普涨\n\n"
           "**关注板块** 本轮板块接口未取到数据，不做板块点评。\n\n"
           "**建议** 看硬件链能否承接\n" + _PAD)

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ① 板块段是空段 + AI 写伪造占位行 → 必须被闸门拦下
r = A._struct_reason(_FAKE_ROWS, EMPTY_BRIEF, "10:00")
check(r is not None, "板块全缺时仍写 +0.00% 占位行，闸门却放行了")

# ② 同样输入，如实说明「未取到」→ 放行（不能矫枉过正把好的也拦了）
r = A._struct_reason(_HONEST, EMPTY_BRIEF, "10:00")
check(r is None, f"如实说明缺数据的正常文本被误拦: {r!r}")

# ③ 有真实板块数据时，正常输出必须放行（回归保护）
real = ("> **开盘异动**\n\n**触发** 未命中阈值\n\n**盘面** 指数普涨\n\n"
        "**关注板块** 半导体 +1.68%、存储芯片 +2.1%\n\n"
        "**建议** 看硬件链能否承接\n" + _PAD)
r = A._struct_reason(real, FULL_BRIEF, "10:00")
check(r is None, f"有数据时的正常推送被误拦: {r!r}")

# ④ brief 有数据、AI 却写占位行 → 也该拦（不是"数据缺才拦"）
r = A._struct_reason(_FAKE_ROWS, FULL_BRIEF, "10:00")
check(r is not None, "brief 有板块数据时 AI 写占位行，闸门放行了")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (板块缺数据不得写伪造占位行；如实说明与正常推送均放行)")
