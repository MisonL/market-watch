"""「领涨/领跌」必须真的排过名 —— 2026-11-07 复核 F9。

回退模板的标题原先只从 `_rows_f3`（= **push 展示子集**，8 个板块）里取
min/max，然后直接写「X 领跌」/「X 领涨」。但 `_rows_f3` 是"给读者看的清单"，
不是可比较的全集 —— `watch` 段还多 4 个板块（贵金属/医药生物/化学制药/
医疗研发外包），风险/机会段也早已改成 `push ∪ watch` 扫描。

`[复现]` 甲板块 +0.2%（不触发）、乙板块 +5.0%（不触发）且乙在 watch
不在 push：标题「甲板块领涨」，而已知领涨者是乙 —— 与正文自相矛盾。

修法（方案 B）：在 `push ∪ watch` 里核验排名；真居首才用「领涨/领跌」，
否则改用**不声称排名**的「走强/走弱」。不丢信息（仍写板块名与幅度），
不引入新措辞，只是不再说一句没核实过的话。

⚠️ 走哪条分支：`_fallback_text` 的标题有三条来源（风险/机会措辞、触发段
对齐、价格端点）。本测试只看**价格端点**那条，所以夹具必须让风险与机会
都为空 —— 判据是 `_no_fund = bool(_rows) and not _any_fund`，即板块行
**只给 f2/f3、不给 f62/f184**（有资金字段就会走机会分支，测不到价格措辞）。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_lead_wording_rank.py`
"""
import os, sys, json, tempfile, atexit, shutil, datetime as dt

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

_CONF = os.path.join(os.environ["MW_DIR"], "conf")
os.makedirs(_CONF, exist_ok=True)
_SRC_CONF = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "conf", "sectors.json")
if os.path.exists(_SRC_CONF) and not os.path.exists(
        os.path.join(_CONF, "sectors.json")):
    shutil.copy(_SRC_CONF, os.path.join(_CONF, "sectors.json"))

import marketlib as M
import analyze

_CFG = json.load(open(os.path.join(_CONF, "sectors.json"), encoding="utf-8"))
_PUSH = [i.get("code") for i in (_CFG.get("push") or []) if isinstance(i, dict)]
_WATCH = [i.get("code") for i in (_CFG.get("watch") or []) if isinstance(i, dict)]
_EXTRA = [c for c in _WATCH if c not in _PUSH]      # 只在 watch、不在 push
_NOW = dt.datetime(2026, 11, 6, 10, 0, 0)


def _q(chgs):
    """只给 f2/f3 —— 不带 f62/f184，才能走到「价格端点」那条标题分支。"""
    out = {}
    for _c, _v in chgs.items():
        out[_c] = {"f2": 1000.0, "f3": _v, "f14": _c}
    return out


def _title(q):
    """`_fallback_text` 返回 (正文, ok, 其它)；正文第一行是 `> **…**` 标题。"""
    _t = analyze._fallback_text(q, [], None, _NOW)
    _txt = _t[0] if isinstance(_t, tuple) else _t
    for _ln in _txt.split("\n"):
        if _ln.startswith(">"):
            return _ln
    return _txt.split("\n")[0]


def check_extra():
    """补充断言（2026-11-07 二次复核新增）。

    ⚠️ 本文件原有的 `assert ... or True` 是恒真装饰性断言（测试审计发现），
    已删；这里补的是那次审计后新增的三条真实判据。
    """
    import json as _json
    _CFG2 = _json.load(open(os.path.join(_CONF, "sectors.json"),
                            encoding="utf-8"))
    _push2 = [i.get("code") for i in (_CFG2.get("push") or [])
              if isinstance(i, dict)]
    _watch2 = [i.get("code") for i in (_CFG2.get("watch") or [])
               if isinstance(i, dict)]
    _extra2 = [c for c in _watch2 if c not in _push2]

    # ⑥ F9 符号守卫：0.00% 不是上涨，不得写「走强」/「领涨」
    # ⚠️ 夹具必须只给 f2/f3（不给 f184/f62），否则会走「机会段」
    # 而测不到价格端点的措辞。
    _p = _push2[0]
    _q = {_p: {"f2": 1000.0, "f3": 0.0, "f14": _p}}
    if _extra2:
        _q[_extra2[0]] = {"f2": 1000.0, "f3": 2.0, "f14": _extra2[0]}
    _t = _title(_q)
    assert "走强" not in _t and "领涨" not in _t, (
        f"0.00% 不得说「走强/领涨」（实得标题：{_t}）")
    assert "持平" in _t, f"0.00% 应写「持平」（实得标题：{_t}）"

    # ⑦ F10 扫描集必须覆盖 watch：更强的在 watch 时要点名它
    if _extra2:
        _q2 = _q5({_p: 0.5})
        _q2[_extra2[0]] = dict(_q2[_p], f184=9.0, f62=1e9)
        _q2[_p] = dict(_q2[_p], f184=5.0, f62=1e9)
        _t2 = _fallback_text(_q2)
        _hit = [ln for ln in _t2.split("\n") if "相对占优" in ln or "唯一净流入" in ln]
        _nm_extra = None
        for _i in (_CFG2.get("watch") or []):
            if isinstance(_i, dict) and _i.get("code") == _extra2[0]:
                _nm_extra = _i.get("name")
        assert _hit, f"机会段应当出现（实得：{_t2[:160]!r}）"
        assert _nm_extra and _nm_extra in _hit[0], (
            f"更强的板块在 watch 里时必须点名它（{_nm_extra}），实得：{_hit[0]!r}")


def _q5(chgs):
    return {c: {"f2": 1000.0, "f3": v, "f14": c, "f184": 1.0, "f62": 1e8}
            for c, v in chgs.items()}


def _fallback_text(q):
    _t = analyze._fallback_text(q, [], None, _NOW)
    return _t[0] if isinstance(_t, tuple) else _t


def check():
    assert _EXTRA, "配置里必须有「只在 watch、不在 push」的板块才能测这个缺口"
    _extra = _EXTRA[0]
    _p0 = _PUSH[0]

    # ① 更强的那一个在 watch 而不在 push → push 里的板块不得称「领涨」
    _t1 = _title(_q({_p0: 0.2, _extra: 5.0}))
    assert "领涨" not in _t1, (
        f"更强的板块不在 push 里时不得声称领涨；标题实得：{_t1}")

    # ② 真的全场最强 → 仍应照常写「领涨」（防修成永久不写排名）
    _t2 = _title(_q({_p0: 6.0, _extra: 1.0}))
    assert "领涨" in _t2, f"真正领涨时必须照常写「领涨」；标题实得：{_t2}"

    # ③ 跌的一侧同理：更狠的在 watch → push 里的板块不得称「领跌」
    _t3 = _title(_q({_p0: -0.2, _extra: -5.0}))
    assert "领跌" not in _t3, (
        f"更狠的板块不在 push 里时不得声称领跌；标题实得：{_t3}")

    # ④ 真的全场最狠 → 照常写「领跌」
    _t4 = _title(_q({_p0: -6.0, _extra: -1.0}))
    assert "领跌" in _t4, f"真正领跌时必须照常写「领跌」；标题实得：{_t4}"

    # ⑤ 不居首时改写「走强/走弱」，且仍带板块名（不丢信息）
    assert ("走强" in _t1 or "走弱" in _t1), (
        f"不居首时应改用不声称排名的措辞；标题实得：{_t1}")
    assert ("走强" in _t3 or "走弱" in _t3), (
        f"不居首时应改用不声称排名的措辞；标题实得：{_t3}")

    check_extra()
    print("ALL PASS (领涨/领跌排名核验 5+2 个断言)")


if __name__ == "__main__":
    check()
