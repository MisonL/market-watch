"""早盘动作指令剔除必须**只删指令子句**，保留同一行的事实。

背景：初版整行替换成固定提示，实测把
    > 风险 低开反抽不宜追；恒指贴近20日高，冲高回落风险大。
整行换成「本时段仅报告市场事实与观察条件。」—— 风险事实连带被删。
09:25 的价值就在这些事实，删掉等于用合规换掉信息量。

预期行为：
  · 含动作指令的子句被删；
  · 其余子句逐字保留；
  · 若整行都是指令，才降级为固定提示（并保留 `> 风险 ` 行首标记）；
  · 幂等；其他 slot 原样。
"""
import os
import sys
import tempfile

_old = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_adv_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import analyze as A

FIX = "本时段仅报告市场事实与观察条件。"

# (原文, 必须保留的片段, 必须消失的片段)
CASES = [
    ("> 风险 低开反抽不宜追；恒指贴近20日高，冲高回落风险大。",
     ["恒指贴近20日高", "冲高回落风险大"], ["不宜追"]),
    ("> 机会 半导体急跌后若主力回流，才有低吸窗口；白酒相对占优。",
     ["白酒相对占优"], []),
    ("> 风险 北向净买入 38 亿，但主力净流出 12 亿。",
     ["北向净买入 38 亿", "主力净流出 12 亿"], []),
    ("> 风险 反弹无量宜减仓、不追高。", ["> 风险"], ["减仓", "不追高"]),
    # 真实 09-28 行：「高位不追」是命令，必须消失；同行的隧幅数据必须留
    ("> 风险 沪深300贴着 20 日低，若低开破位；印制电路板前收 -0.81% 相对弱，高位不追。",
     ["沪深300贴着 20 日低", "-0.81%"], ["高位不追"]),
    ("**建议** 建议逢反弹分批减仓，先看 20 日低是否失守。",
     ["20 日低"], ["分批减仓"]),
]

bad = 0
for src, keep, drop in CASES:
    out = A._advice_lines(src, "09:25")
    miss = [k for k in keep if k not in out]
    leak = [k for k in drop if k in out]
    if miss or leak:
        bad += 1
        print(f"  ❌ {src}\n     -> {out}\n     丢:{miss} 漏:{leak}")
    if A._advice_lines(out, "09:25") != out:
        bad += 1
        print(f"  ❌ 非幂等: {src}")

# 整行都是指令时才用固定提示
full = A._advice_lines("> 风险 反弹无量宜减仓、不追高。", "09:25")
if FIX not in full:
    bad += 1
    print(f"  ❌ 纯指令行应降级为固定提示，实际: {full}")

# 其他槽位逐字节原样
for s in ("09:25", "10:00", "11:30", "13:30", "15:00", None):
    src = "> 风险 反弹无量宜减仓、不追高。"
    out = A._advice_lines(src, s)
    if s not in ("09:25", "10:00") and out != src:
        bad += 1
        print(f"  ❌ 槽位 {s} 被改动: {out}")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (子句级剔除：事实保留 / 指令消失 / 幂等 / 其他槽位原样)")
