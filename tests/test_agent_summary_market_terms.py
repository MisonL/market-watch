"""agent 摘要闸门：市场口径必须放行，个人口径必须拦下 —— 2026-11-06 复核。

背景（两次改动叠加出的一次真实回归）：

1. `_chk()` 原先 `return _ML.safe(s)` 直接返回主系统结论，后面的
   `_SUMMARY_BANNED`（仓位/持仓/买入/卖出/收益…）**从不执行** —— 死代码。
   2026-11-06 把它接通（P0-4）。

2. 接通后暴露出本地表的一个问题：它是**子串匹配**，而这些词在**市场数据**
   里是常见措辞。`safe()` 全部放行、只有本地表拦：
     「南向资金净**买入** 42 亿港元」  ← 资金流向，不是个人操作
     「ETF 遭净**卖出** 8 亿元」        ← 标的级资金数据
     「机构**减仓**迹象不明显」         ← 市场参与者行为

   而 agent 推送的正是盘面研判，「资金流向」是「② 驱动与证据」段的主线内容
   —— 逐句降级下去会把那段删空，或干脆整条不推。

修法：判定前把「资金主体 + 买卖动词」这种市场口径**中性化**
（买入→流入 / 卖出→流出 / 减仓→减持），与 `marketlib._yield_neutral`
处理「债券口径收益率」是同一个思路。只改判定用的副本，不改输出。
覆盖面对刻意为窄：只认**有明确主语**且动词距离不超过 12 字的组合 ——
「建议买入」「我已减仓」这类个人口径没有这些主语，照样拦。

另补：本地表原本**缺「加仓」**（减仓/清仓/重仓/底仓/空仓/满仓都在，唯独
建仓方向的操作词漏了），一并补上。

跑法：`MW_DIR=$(mktemp -d) agent/.venv/bin/python tests/test_agent_summary_market_terms.py`
"""
import os, sys, importlib.util

_HERE = os.path.dirname(os.path.abspath(__file__))
_MW = os.path.dirname(_HERE)
# 主系统的 marketlib 在 ../src，agent 包在 ../agent/src
sys.path.insert(0, os.path.join(_MW, "src"))
sys.path.insert(0, os.path.join(_MW, "agent", "src"))

try:
    import marketlib  # noqa: F401
except Exception as e:                       # 系统 python 下不该发生
    print(f"SKIP: 无法导入 marketlib（{e}）")
    raise SystemExit(0)

_spec = importlib.util.spec_from_file_location(
    "mw_agent_main", os.path.join(_MW, "agent", "src", "mw_agent", "__main__.py"))
_m = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_m)
except Exception as _e:        # 系统 python 下缺 claude_agent_sdk
    print(f"SKIP: 无法加载 agent 模块（{type(_e).__name__}: {_e}）"
          f"\n      请用 agent/.venv/bin/python 运行本文件")
    raise SystemExit(0)

# ── 必须放行：市场口径（主语是资金/标的/行业，不是"我"）──
KEEP = [
    "南向资金净买入 42 亿港元，连续三日净流入。",
    "ETF 遭净卖出 8 亿元。",
    "板块主力净流入 12 亿元，机构减仓迹象不明显。",
    "北向资金今日净买入 15 亿元，主要流向半导体。",
    "两融余额 1.79 万亿。",
    "行业指数净买入 5 亿。",
    "① 盘面与关键位：上证收 3402.15，+0.42%；恒生科技盘中 4243.65。",
    "② 驱动与证据：三季报显示半导体设备板块净利同比 +59.5%，已证实。",
    "③ 昨日判断验证：昨日观察点「跌破 3737」未触发；触发线维持不动。",
    "④ 明日观察点：周四 21:30 美国 10 月 CPI。",
    "⑤ 风险：美债 10 年期收益率上行至 4.32%，压制成长股估值。",
    "⑥ 板块资金：融资余额 1.8 万亿，主力净流出 28 亿元。",
]

# ── 必须拦下：个人口径 ──
BLOCK = [
    "建议买入半导体板块。",          # 无主语的买卖建议
    "建议卖出半导体板块。",
    "我已减仓至三成。",              # 第一人称
    "加仓半导体",                    # 本地表原本缺这个词
    "净值收益率 12.5%",
    "我的成本价 3.2",
    "仓位 6 成",
    "持仓 20 万",
    "账户浮盈 3 万",
    "本金 20 万",
    "空仓观望",
    "满仓持有",
    "该板块 9 月亏损企业占比升至 12%",   # 主系统也拦（保留主系统口径）
]


def check():
    _bad = []
    for _c in KEEP:
        if not _m._safe_summary(_c).strip():
            _bad.append(("误拦", _c))
    for _c in BLOCK:
        if _m._safe_summary(_c).strip():
            _bad.append(("漏拦", _c))
    assert not _bad, "\n".join(f"  {k}: {c}" for k, c in _bad)

    # 中性化只用于判定，**不得**改坏判定函数对原文的处理语义
    assert _m._mkt_neutral("南向资金净买入 42 亿") == "南向资金净流入 42 亿"
    assert _m._mkt_neutral("建议买入") == "建议买入", (
        "无资金主语的买卖动词不得被中性化（否则个人买卖建议会被放过）")
    assert _m._mkt_neutral("") == ""

    print(f"ALL PASS (摘要闸门 {len(KEEP)} 放行 + {len(BLOCK)} 拦下 + 3 中性化断言)")


if __name__ == "__main__":
    check()
