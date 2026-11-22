"""09:25 / 10:00 「只报事实与观察条件」的输出侧守卫 —— 场景驱动测试。

背景（只读审查，2026-11-10）：
  · prompt 侧只有半截修复（`analyze.py` 的 `_advice_rule` 按 slot 分支），
    但同一 prompt 里 `3386` 行无条件写着「减仓/加仓/观望…是允许的」，
    规则 7 又要求「风险→建议减仓/观望」。两条互斥指令同时进模型。
  · `safe()` 不接受 slot 参数：实测 `safe('建议逢反弹分批减仓') == (True, None)`，
    输出侧**零执行机制**。
  · 生产 `state/history/pushes.jsonl` 里 09:25 真实出现过
    「低开反抽不宜追」「方向建议观望不接」「反弹无量宜减仓」。
  · 现有原型只认冒号 `：`，而真实风险/机会行是 `> 风险 …`，所以从未生效。

本文件守的是「最小接线」后的行为契约：
  ① 只对 09:25/10:00 生效，其他 slot 逐字节原样；
  ② 早盘**事实优先**：纯数据/涨跌幅/资金/北向净买入/回购公告必须保留；
  ③ 具体动作**只整行或整完整句剔除**，绝不把原命中句碎片拼回去，
     替换文案固定为「本时段仅报告市场事实与观察条件。」；
  ④ 风险/机会段标题存在时，替换后这两行仍存在且非空；
  ⑤ 幂等：f(f(x)) == f(x)；
  ⑥ 不改变 `M.safe()` 的判定（本模块不碰词表）；
  ⑦ 最终出口 `push()` 真的接了这道守卫（fake webhook + patch subprocess，
     零真实外发），且接线后仍会再 `_safe(_fin(t))`、不会把请求体顶破 4096。

跑法：`python3 tests/test_slot_advice_output.py`
全部零外发：不调 AI、不读 keys/webhook 真实值、不写生产 state。
"""
import ast
import json
import os
import sys
import tempfile
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "..", "src")
sys.path.insert(0, _SRC)

_FIX_HINT = "本时段仅报告市场事实与观察条件。"


def _fail(msg):
    raise AssertionError(msg)


# ---------------------------------------------------------------- 纯函数用例
#
# ⚠️ 用例一律用**生产真实形态**：从 `state/history/pushes.jsonl` 抄出的原文，
# 行首是 `> 风险 ` / `> 机会 `（**空格分隔、无冒号**）、段头是 `**建议**`，
# 而不是「风险：…」这种带冒号的合成样例 —— 现有原型只认冒号，正是靠
# 合成样例把它放过去的。

# 生产原文（逐字取自 pushes.jsonl，未改写）
REAL_20261106_RISK = (
    "> 风险 全球芯片链共振下跌，A股半导体、CPO、印制电路板开盘若补跌，"
    "低开反抽不宜追；恒指贴近 20 日高、恒科单日大涨尚未确认，冲高回落风险大。")
REAL_20261106_OPP = (
    "> 机会 平台与内需相对占优：中概逆势收红，港股平台股是避风港；"
    "白酒、消费风格、创新药跌幅最小，AI应用远小于硬件，可作防御方向；"
    "半导体急跌后若主力回流，才轮到低吸。")
REAL_20260928_RISK = (
    "> 风险 沪深300、消费风格贴着 20 日低，若低开破位，指数与顺周期方向"
    "建议观望不接；印制电路板前收 -0.81% 相对弱，高位不追。")
REAL_20261013_RISK = (
    "> 风险 隔夜费半、韩国半导体链重挫，A 股半导体、存储、CPO、印制电路板"
    "已在 20 日低下方，低开不接飞刀，反弹无量宜减仓、不追高。")
REAL_20260924_RISK = "> 风险 外盘与中概同步走弱，科技链开盘大概率承压，CPO、AI应用方向不追高。"
REAL_20260924_OPP = (
    "> 机会 纺织、风电零部件低位放量、主力占比高，可跟踪。")

# 早盘必须原样保留的**事实行**（同一批生产推送里的真实行）
REAL_FACT_ROWS = [
    "**隔夜** 费半 -11.21%、韩国 -13.46%，芯片链遭全球抛售；美债 10 年期升至 4.84%。",
    "**关键位** 上证 3689.66，20 日高 3828.22（-3.62%）、20 日低 3574.27（+3.23%）。",
    "**今日关注** ① 半导体能否扛住费半 -11.21% 的传导；② 恒科能否守住 3995。",
    "半导体 -6.72%",
    "CPO概念 -8.81%",
    "**今日涨幅 TOP3**",
    "（本段无数据）",
    "> **盘前简报 · 外围芯片链塌方，A股科技链迎传导测试**",
]

# > 机会 里的**条件观察句**不得被误伤（下一轮能否延续，取决于量能与承接）
OBSERVE_ROWS = [
    "> 机会 若恒科收复 4450 且量能跟进，存储方向存在承接延续的条件。",
    "> 机会 玻璃基板、面板有产业消息支撑，可关注资金是否在开盘后承接。",
    # 「才有低吸窗口」是名词化描述（若主力回流才出现的条件），不是指令
    "> 机会 半导体急跌后若主力回流，才有低吸窗口。",
    "> 风险 印制电路板价涨钱走，主力净流出 50 亿，与价格背离。",
    "> 风险 沪深300 贴 20 日低，若低开破位存在补跌风险。",
]
# 北向净买入 / 回购：裸「买入」不能误伤（不在动作词表里）
BARE_WORDS = [
    "> 风险 北向资金今日净买入 38 亿，银行、保险占大头。",
    "**盘面** 多家公司公告回购，合计金额 12.6 亿；沪深300 贴 20 日低 3958.14。",
    "北向净买入 5万元",
    "公司回购公告：拟回购股份用于股权激励。",
]

# (原文, 必需保留的片段, 必需消失的片段, 说明) —— 全部 `> 风险 ` / `> 机会 ` 形态
LINES = [
    # 混合行（事实 + 指令）：事实必须留下，指令必须消失。
    # ⚠ 2026-11-10：旧版这里要求必含固定提示 —— 那是“整行替换”的口径，
    # 实测会把「恒指贴近 20 日高」这类风险事实一并删掉。
    (REAL_20261106_RISK, ["恒指贴近 20 日高"], ["不宜追", "低开反抽"], "生产 11-06 09:25 风险行"),
    (REAL_20261106_OPP, ["相对占优"], ["低吸"], "生产 11-06 09:25 机会行"),
    (REAL_20260928_RISK, ["印制电路板前收"], ["建议观望不接"], "生产 09-28 09:25"),
    (REAL_20261013_RISK, ["已在 20 日低下方"], ["宜减仓", "不接飞刀"], "生产 10-13 09:25"),
    (REAL_20260924_RISK, ["科技链开盘大概率承压"], ["不追高"], "生产 09-24 09:25"),
    (REAL_20260924_OPP, ["低位放量"], ["可跟踪"], "生产 09-24 10:00 机会行"),
    # 段头 + 两行（生产 `**建议**` 形态）
    ("**建议**\n" + REAL_20261106_RISK + "\n" + REAL_20261106_OPP,
     ["**建议**", "恒指贴近 20 日高"], ["不宜追", "低吸"], "**建议** 段头 + 真实两行"),
    # 整行都是指令 → 降级为固定提示
    ("> 风险 建议买入半导体板块。", [_FIX_HINT], ["买入"], "建议买入…板块"),
    ("> 风险 加仓半导体。", [_FIX_HINT], ["加仓"], "无建议字的加仓"),
    ("> 风险 明日开盘减一半。", [_FIX_HINT], ["减一半"], "明日开盘减一半"),
    ("> 风险 明天开盘减仓到三成。", [_FIX_HINT], ["减仓"], "减仓到三成"),
    # 空格 / markdown 拆字
    ("> 风险 建议 **减仓** 半导体。", [_FIX_HINT], ["减仓"], "加粗拆字"),
    ("> 风险 建议 逢反弹 分 批 减 仓 。", [_FIX_HINT], ["减", "仓"], "空白拆字"),
    ("> 风险 不 宜 追。", [_FIX_HINT], ["追"], "空白拆字的追高"),
    ("**风险** 反弹无量宜减仓、不追高。", [_FIX_HINT], ["减仓", "追高"], "加粗段名"),
]

KEEP = REAL_FACT_ROWS + OBSERVE_ROWS + BARE_WORDS

ALL_SLOTS = (None, "event", "11:30", "13:30", "14:30", "15:00", "summary")


def _advice():
    import analyze
    fn = getattr(analyze, "_advice_lines", None)
    assert fn is not None, "analyze 里没有 _advice_lines —— 接线缺失"
    return fn


def t_consumers():
    """真实消费者形态：09:25/10:00 的每一条风险/机会行都必须被处理。"""
    fn = _advice()
    for slot in ("09:25", "10:00"):
        for src, must, banned, why in LINES:
            got = fn(src, slot)
            assert isinstance(got, str), f"{slot} {why}: 返回非 str"
            for b in must:
                assert b in got, f"{slot} {why}: 丢了必需片段 {b!r}\n  got={got!r}"
            for b in banned:
                assert b not in got, f"{slot} {why}: 仍残留 {b!r}\n  got={got!r}"
            # 不得把原命中句碎片拼回
            assert src.strip() != got.strip() or all(b in got for b in must), \
                f"{slot} {why}: 原句原样返回（未生效）"


def t_keep_facts():
    """早盘事实优先：纯数字事实、北向净买入、回购公告必须逐字保留。"""
    fn = _advice()
    for slot in ("09:25", "10:00"):
        for src in KEEP:
            got = fn(src, slot)
            assert got == src, f"{slot}: 误伤事实行\n  in ={src!r}\n  out={got!r}"


def t_other_slots_untouched():
    """其他 slot 逐字节原样（含 11:30 明确允许的动作词）。"""
    fn = _advice()
    _live = ("> 风险 反弹无量宜减仓、不追高。\n**建议**\n> 机会 可考虑低吸。")
    for slot in ALL_SLOTS:
        got = fn(_live, slot)
        assert got == _live, \
            f"slot={slot!r} 被改了（应该原样）\n  in ={_live!r}\n  out={got!r}"
    for slot in ALL_SLOTS:
        got = fn(REAL_20261013_RISK, slot)
        assert got == REAL_20261013_RISK, \
            f"slot={slot!r} 的动作指令被改了（应该原样）\n  in ={REAL_20261013_RISK!r}\n  out={got!r}"
    for slot in ALL_SLOTS:
        got = fn(REAL_20261106_OPP, slot)
        assert got == REAL_20261106_OPP, \
            f"slot={slot!r} 被改了（应该原样）\n  in ={REAL_20261106_OPP!r}\n  out={got!r}"
    assert fn("", "09:25") == ""
    # 非 str 原样返回（与 _strip_preamble / _spacers 的契约一致：不抛、不改成 ""）
    assert fn(None, "09:25") is None


def t_headers_survive():
    """「建议」段标题存在时，风险/机会两行替换后仍存在且非空。"""
    fn = _advice()
    src = ("**建议**\n> 风险 反弹无量宜减仓、不追高。\n> 机会 建议逢反弹分批减仓。")
    got = fn(src, "09:25")
    lines = [l for l in got.split("\n") if l.strip()]
    assert any(l.startswith("**建议") for l in lines), f"丢了「建议」段头: {got!r}"
    risk = [l for l in lines if "风险" in l]
    opp = [l for l in lines if "机会" in l]
    assert risk and risk[0].replace("风险", "").strip(" ：:>"), f"风险行空了: {got!r}"
    assert opp and opp[0].replace("机会", "").strip(" ：:>"), f"机会行空了: {got!r}"


def t_idempotent():
    """幂等：f(f(x)) == f(x) —— 主出口与最终出口都调它。"""
    fn = _advice()
    for slot in ("09:25", "10:00"):
        for src in [x[0] for x in LINES] + list(KEEP):
            once = fn(src, slot)
            twice = fn(once, slot)
            assert once == twice, f"{slot}: 非幂等\n  once ={once!r}\n  twice={twice!r}"


def t_safe_unaffected():
    """不改变 M.safe()：动作词仍放行（闸门管形态不管词）。"""
    import marketlib as M
    for s in ("建议逢反弹减仓", "可考虑加仓", "北向资金今日净买入 38 亿"):
        ok, h = M.safe(s)
        assert ok, f"M.safe 被改坏了: {s!r} -> {h!r}"


# ------------------------------------------------------- 生产接线（AST + 行为）

def t_ast_wiring():
    """AST 证明：produce_text 公共出口与 push 最终出口都调了 _advice_lines。"""
    path = os.path.join(_SRC, "analyze.py")
    tree = ast.parse(open(path, encoding="utf-8").read(), filename=path)

    def _calls(fn):
        return [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name) and n.func.id == "_advice_lines"]

    def _find(name):
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == name:
                return n
        _fail(f"analyze.py 里找不到 {name}()")

    assert _calls(_find("produce_text")), "produce_text 未接 _advice_lines"
    _pc = _calls(_find("push"))
    assert _pc, "push 最终出口未接 _advice_lines"
    # push 里的调用必须发生在 b=_mk(_fin(t)) 之前（最终 _safe 检查的对象）
    _mk_lines = [n.lineno for n in ast.walk(_find("push"))
                 if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "b" for t in n.targets)]
    assert _mk_lines, "push 里找不到 b = _mk(...)"
    assert min(c.lineno for c in _pc) < min(_mk_lines), \
        "push 里的守卫在 b=_mk(_fin(t)) 之后 —— 没护住真正发出的文本"
    # 最终出口必须对 _fin(t) 再 _safe 一次
    _src = open(path, encoding="utf-8").read()
    assert "_safe(_fin(t))" in _src, "最终出口缺少 _safe(_fin(t)) 复核"


def _load_analyze_sandbox():
    """在临时 MW_DIR里加载 analyze（不碰生产 state，不调 AI）。"""
    _tmp = tempfile.mkdtemp(prefix="mw_adv_")
    os.environ["MW_DIR"] = _tmp
    # ⚠️2026-11-22（P0写穿生产）：**必须**先 refresh paths。
    # `paths.DIR`在 import时固化；只 pop analyze/marketlib而不刷新
    # paths，则 `p_hist("pushes.jsonl")`仍指向生产根 ——
    # `A.push()`会把合成数据（板块0…板块59）写进**生产**
    # pushes.jsonl并标记 ok:true。实测2026-11-19 /11-22各一次。
    import paths as _paths
    _paths.refresh()
    for m in [m for m in list(sys.modules)
        if m in ("analyze", "marketlib", "runlog")]:
        sys.modules.pop(m, None)
    import analyze
    return analyze


def t_push_endpoint():
    """fake webhook + patch subprocess：最终出口真的被守卫，零真实外发。"""
    A = _load_analyze_sandbox()
    sent = {"n": 0, "argv": None, "stdin": None}

    def _fake_run(argv, **kw):
        sent["n"] += 1
        sent["argv"] = list(argv)
        sent["stdin"] = kw.get("input") or kw.get("stdin_text")
        return types.SimpleNamespace(returncode=0, stdout='{"errcode":0}',
                                     stderr="")

    _cfg = {"webhook": "https://example.invalid/fake-webhook-not-real"}
    old_cfg, old_run = A.CFG, A.subprocess.run
    A.CFG = _cfg
    A.subprocess.run = _fake_run
    try:
        # 生产真实动作行（11-06 09:25：低开反抽不宜追 / 才有低吸窗口）
        _bad = ("**建议**\n"
                "> 风险 全球芯片链共振下跌，A股半导体、CPO、印制电路板开盘若补跌，"
                "低开反抽不宜追；恒指贴近 20 日高…冲高回落风险大。\n"
                "> 机会 平台与内需相对占优：中概逆势收红，港股平台股是避风港；"
                "半导体急跌后若主力回流，才轮到低吸。")
        A.push(_bad, slot="09:25", kind="hourly")
        assert sent["n"] == 1, f"期望 1 次外发（fake），实际 {sent['n']}"
        _body = json.dumps(sent["argv"], ensure_ascii=False) + str(sent["stdin"])
        for w in ("不宜追", "低吸"):
            assert w not in _body, f"最终发出的请求里仍有 {w!r}"
        # 混合行口径：事实保留、指令消失。固定提示只在**整行都是指令**时出现。
        assert "恒指贴近 20 日高" in _body, "真实风险事实被连带删掉了"
        assert "平台与内需相对占优" in _body, "真实机会事实被连带删掉了"
        # 其他 slot 不受影响
        sent["n"] = 0
        A.push("> 风险 反弹无量宜减仓。", slot="11:30", kind="hourly")
        assert sent["n"] == 1
        assert "减仓" in (json.dumps(sent["argv"], ensure_ascii=False)
                          + str(sent["stdin"])), "11:30 被误改"
    finally:
        A.CFG, A.subprocess.run = old_cfg, old_run


def t_no_overflow():
    """替换不会把请求体顶破：超长输入替换后请求体仍 ≤ 4096 字节。"""
    A = _load_analyze_sandbox()
    sent = {}

    def _fake_run(argv, **kw):
        sent["argv"] = list(argv)
        sent["in"] = kw.get("input") or kw.get("stdin_text")
        return types.SimpleNamespace(returncode=0, stdout='{"errcode":0}',
                                     stderr="")

    old_cfg, old_run = A.CFG, A.subprocess.run
    A.CFG = {"webhook": "https://example.invalid/fake-webhook-not-real"}
    A.subprocess.run = _fake_run
    try:
        _big = ("**建议**\n" + "\n".join(
            "> 风险 板块%d 主力净流出 %d 亿，价涨钱走，反弹无量宜减仓、不追高。"
            % (i, i) for i in range(60)))
        A.push(_big, slot="10:00", kind="hourly")
        _payload = json.dumps({"msgtype": "markdown",
                               "markdown": {"content": sent["in"]}},
                              ensure_ascii=False).encode("utf-8")
        assert len(_payload) <= 4096, f"请求体 {len(_payload)}B 超限"
    finally:
        A.CFG, A.subprocess.run = old_cfg, old_run


def main():
    _tests = [(n, f) for n, f in sorted(globals().items())
              if n.startswith("t_") and callable(f)]
    _bad = 0
    for n, f in _tests:
        try:
            f()
            print(f"  ✅ {n}")
        except AssertionError as e:
            _bad += 1
            print(f"  ❌ {n}: {e}")
        except Exception as e:          # noqa: BLE001
            _bad += 1
            print(f"  ❌ {n}: {type(e).__name__}: {e}")
    print(f"\n{'ALL PASS' if not _bad else f'FAILED {_bad}/{len(_tests)}'}")
    return 1 if _bad else 0


if __name__ == "__main__":
    sys.exit(main())
