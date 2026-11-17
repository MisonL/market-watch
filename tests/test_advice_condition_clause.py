"""早盘守卫不能把「若X则Y」切成「若X」——条件与结论必须同生共死。

`[回放证据]` 用 pushes.jsonl 的真实推送回放，抓到守卫正在删事实：

    原文  > 风险 沪深300贴着20日低，若低开破位，指数与顺周期方向建议观望不接；
          印制电路板前收-0.81%相对弱，高位不追。
    输出  > 风险 沪深300贴着20日低，若低开破位；印制电路板前收-0.81%相对弱

「若低开破位」是**条件**，后面才是结论。只删结论、留下悬空的条件从句，
读者拿到的比 AI 原文更绝对 —— 而这恰好发生在最需要谨慎的 09:25。

另有一类反方向误伤：`开盘/盘中/高位/低位` 是**方位词**（描述时段/位置），
不是指令语气；「北向资金开盘净买入 38 亿」是早盘最核心的事实之一，
却被判成操作指令、整行替换成空提示。

契约：
  ① 含条件标记（若/如果/一旦/待…）的段落被命中时，**整行降级为固定提示**，
     不允许留下悬空从句；
  ② 动作词前有资金主体（北向/ETF/主力/机构…）时**不算指令**，原样保留。
"""
import os
import sys
import tempfile

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_advc_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import analyze as A

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


# ① 条件 + 指令：不得留下悬空从句
_cases = [
    "> 风险 沪深300、消费风格贴着20日低，若低开破位，指数与顺周期方向建议观望不接；印制电路板前收-0.81%相对弱，高位不追。",
    "> 风险 恒科破位与解禁抛压都压在半导体链，反弹未获量价确认前观望、不追高。",
    "> 风险 A股半导体、CPO、印制电路板开盘若补跌，低开反抽不宜追；恒指贴近20日高。",
]
for _c in _cases:
    _out = A._advice_trim_line(_c) or ""
    # 悬空判据：以「若/一旦/待」结尾，或条件词后面直接紧跟分隔符
    _dangling = any(_seg.strip().endswith(("若", "如果", "一旦", "待"))
                    for _seg in _out.replace(">", "").split("；"))
    check(not _dangling, f"留下悬空条件从句: {_out[:60]!r}")
    check("观望不接" not in _out and "不追高" not in _out and "不宜追" not in _out,
          f"指令没删干净: {_out[:60]!r}")

# ② 资金主体 + 动作词：不算指令，必须原样保留
_keep = [
    "> 风险 北向资金开盘净买入 38 亿",
    "> 风险 ETF 开盘净买入 3.2 亿",
    "> 机会 主力开盘净买入 12 亿，位置在高位",
]
for _c in _keep:
    check(A._advice_hit(_c) is None, f"资金主体句被误判成指令: {_c!r}")
    _out = A._advice_trim_line(_c)
    check(_out is None or "净买入" in _out, f"事实被删: {_out!r}")

# ③ 真指令仍必须被拦（守卫不能失效）
for _c in ["> 风险 建议减仓一半", "> 机会 可逢低买入", "> 风险 开盘不追高"]:
    check(A._advice_hit(_c) is not None, f"真指令漏网: {_c!r}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (条件从句不悬空；资金主体不误伤；真指令仍拦截)")
