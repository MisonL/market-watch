"""时段禁操作规则与全局建议规则不得互相矛盾 —— 2026-11-07 复核 F11。

`[实测]` 改动前，同一个 prompt 里同时存在：

    09:25  「**不给买卖动作**，盘前只做信息准备。」
    10:00  「只报开盘后的**异动事实**，不做方向判断、不给操作建议。」
    全局   「**允许并鼓励写建议操作**（如"建议逢反弹分批减仓"…）」

最后一段是 `ai_analyze` 里**无条件**渲染的，对所有 slot 生效。两条互斥
指令同时进模型，哪条赢取决于措辞权重 —— 不可控。而 `safe()` 不接受
slot 参数、`建议减仓。` 能通过，所以输出侧**没有任何执行机制**。

修法（选项 1）：按 slot 条件渲染 `_advice_rule`。禁建议的槽位不再出现
「鼓励写建议」，改为正面陈述「本时段只做事实与信息准备」；其余槽位
**原样保留**鼓励建议。股份保护那段（不写仓位/金额）对全部时段成立。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_slot_advice_rule.py`
"""
import os, sys, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M
import analyze

_BRIEF = "【关注板块】\n半导体 +1.0%\n"


def _prompt_for(slot):
    """每个 slot 起一个干净沙箱，避免 AI 熔断状态跨调用污染。"""
    import subprocess
    _code = r'''
import os, sys, json
sys.path.insert(0, %r)
import marketlib as M
_cap = {}
def _spy(cmd, **kw):
    _cap["p"] = kw.get("stdin_text") or ""
    return (0, "**触发** x" + "\n" + "**关注板块** y" + "\n" + "**建议** z" + "x"*80, "", False)
M.run_with_idle_watchdog = _spy
import analyze
analyze.ai_analyze("【关注板块】\n半导体 +1.0%%\n", [], "计划", slot=%r)
print(json.dumps({"p": _cap.get("p", "")}, ensure_ascii=False))
''' % (os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"), slot)
    # ⚠️ 每个 slot 必须**独立 **MW_DIR**：共用一个时，前一个 slot 那轮
    # 的 AI 失败会写熔断，后面的 slot 直接短路、拿不到 prompt
    # （实测：09:25 通过、10:00 拿到空串）。
    _sub = tempfile.mkdtemp(prefix="mw_slot_")
    _env = os.environ.copy()
    _env["MW_DIR"] = _sub
    try:
        _r = subprocess.run([sys.executable, "-B", "-c", _code],
                            capture_output=True, text=True, env=_env)
    finally:
        shutil.rmtree(_sub, ignore_errors=True)
    import json
    try:
        return json.loads(_r.stdout.strip().split("\n")[-1])["p"]
    except Exception:
        return ""


def check():
    _enc = "允许并鼓励写建议操作"
    _ban = "只做事实与信息准备"
    _hold = "绝不能透露任何人的个人持仓状态"

    # ① 禁建议的两个 slot：不得再出现「鼓励写建议」，且必须明确禁止
    for _slot in ("09:25", "10:00"):
        _p = _prompt_for(_slot)
        assert _p, f"{_slot} 没拿到 prompt（桩未生效？）"
        assert _enc not in _p, f"{_slot} 仍写着鼓励建议 —— 冲突未消除"
        assert _ban in _p, f"{_slot} 缺少正面的禁建议表述"
        assert _hold in _p, f"{_slot} 丢了股份保护那段（不应随 slot 裁剪）"

    # ② 允许建议的 slot：原样保留（防修成全程禁建议）
    for _slot in ("11:30", "13:30", "14:30", "15:00", None, "event"):
        _p = _prompt_for(_slot)
        assert _p, f"{_slot} 没拿到 prompt"
        assert _enc in _p, f"{_slot} 不该被一起禁掉建议"
        assert _ban not in _p, f"{_slot} 不该出现禁建议表述"
        assert _hold in _p, f"{_slot} 丢了股份保护那段"

    print("ALL PASS (时段建议规则 4 组断言)")


if __name__ == "__main__":
    check()
