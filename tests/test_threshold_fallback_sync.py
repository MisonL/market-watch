#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""改阈值只改配置 → 4 处代码兜底还停在旧数，配置缺键时触发线静默错位。

## 实测（2026-11-25，第三次复发）

16:53 「调研 Agent」把 `hstech_down` 从 3395.38 调到 **3367.15**（恒科跌破
前低、旧线退化成每日常中）。配置当场生效，而库里这些副本**一个都没动**：

    marketlib.TH["hstech_down"]          3395.38   ← 兜底表
    analyze.detect():  T("hstech_down", 3395.38)   ← 调用点字面默认值
    analyze.brief():   threshold('hstech_down',3395.38)
    collect.evaluate(): threshold("hstech_down", 3395.38)  ×2

`marketlib.py` 自己的注释写着「靠人记住是不行的」，然后 **11-20、11-23、
11-25 连着漏了三次**，每次都是自检 `[阈值兜底一致性]` 报 ❌ 才发现 ——
而那时配置已经生效、兜底还停在旧数：**配置缺键那一刻**（人工编辑写坏、
老配置回滚、键名打错）触发线会静默退回几十点外的旧值，没有任何检查会响。

`update_threshold` 原本只改 `sectors.json` + `plan.md`，代码里的副本全靠
自觉同步 —— 那是「判据写在注释里、没写在代码里」的典型。

## 修法

`update_threshold` 末尾调 `_code_sync_threshold(key, value)`，把新值同步进：

    marketlib.TH 表            `"hstech_down": 3367.15,`
    marketlib._HSTECH_UP_LAST_RESORT   （仅 hstech_up）
    threshold("k", N) / T("k", N)      （analyze / collect，含无空格写法）

⚠️ 这是**改源码**，所以每个文件先 `ast.parse` 再落盘 —— 语法坏了整套系统
起不来，比"兜底数字旧一点"严重得多。解析不过就不写、返回说明。

## 本文件锁住什么

① `_code_sync_threshold` 存在且被 `update_threshold` 调用（接线不能断）
② 行为：在沙箱源码树上跑一次，三文件的 5 处副本全部变成新值
③ **不误伤**：`gold_up` 改了不得连带动 `gold_dn`（同前缀键）
④ **不误伤**：`hstech_down` 改了不得动 `hstech_up`
⑤ 无空格写法 `T("gold_up",2.5)` 也要能改到
⑥ 源码树不存在时返回 None（沙箱/精简安装不是错误）
⑦ 改完仍是合法 Python（ast.parse 通过）
⑧ 与自检的判据对齐：仓库里当前 TH 与配置必须一致（今天漂移 28 点）
"""
import ast
import glob
import os
import re
import shutil
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_AGENT_SRC = os.path.join(_ROOT, "agent", "src")
sys.path.insert(0, _AGENT_SRC)

# ⚠️ 本文件要 import `mw_agent.__main__` 本体（不 import 就只能静态看源码，
# 验不了"真跑一次改到了三文件"）。而该模块 import claude_agent_sdk ——
# 系统解释器没有，直接跑会抛 ModuleNotFoundError（看起来像用例坏了）。
# 与 test_agent_memory_seed.py 同一约定：没有就 SKIP + rc=0，
# run_all.sh 已经把它路由到 agent/.venv（见那里的注释）。
try:
    import claude_agent_sdk  # noqa: F401
except Exception:
    print("SKIP: 当前解释器没有 claude_agent_sdk（请用 agent/.venv/bin/python 跑）")
    raise SystemExit(0)

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_src = open(os.path.join(_AGENT_SRC, "mw_agent", "__main__.py"),
            encoding="utf-8").read()

# ---------- ① 存在 + 接线 ----------
check("def _code_sync_threshold(" in _src,
      "`_code_sync_threshold` 不存在 —— 改阈值时 4 处代码兜底又只能靠人记住")
check("_code_note = _code_sync_threshold(key, value)" in _src,
      "`update_threshold` 没有调用 `_code_sync_threshold` —— 函数写了但没接线"
      "（「函数对、接线断」）")

# ---------- 行为：真跑一次 ----------
# 沙箱布局要能 import mw_agent：MW_DIR 指到临时根
_sb = tempfile.mkdtemp(prefix="mw_t_thsync_")
for _d in ("src", "conf", "logs", "agent/state"):
    os.makedirs(os.path.join(_sb, _d), exist_ok=True)
for _f in ("marketlib.py", "analyze.py", "collect.py"):
    shutil.copy2(os.path.join(_ROOT, "src", _f), os.path.join(_sb, "src", _f))
for _f in glob.glob(os.path.join(_ROOT, "conf", "*.json")):
    shutil.copy2(_f, os.path.join(_sb, "conf", os.path.basename(_f)))
for _f in glob.glob(os.path.join(_ROOT, "conf", "*.md")):
    shutil.copy2(_f, os.path.join(_sb, "conf", os.path.basename(_f)))

_old_mw = os.environ.get("MW_DIR")
os.environ["MW_DIR"] = _sb
try:
    import importlib
    import mw_agent.__main__ as AG
    importlib.reload(AG)
    check(AG.MW_DIR == _sb,
          f"模块没按 MW_DIR 加载（MW_DIR={AG.MW_DIR}）—— 会写真实生产目录")
    check(AG._MW_SRC == os.path.join(_sb, "src"),
          f"`_MW_SRC` 指向 {AG._MW_SRC}")

    # ② 三文件 5 处副本全部改到
    _note = AG._code_sync_threshold("hstech_down", 3300.5)
    check(_note is None, f"同步返回了失败说明：{_note}")
    _th = open(os.path.join(_sb, "src", "marketlib.py"), encoding="utf-8").read()
    _an = open(os.path.join(_sb, "src", "analyze.py"), encoding="utf-8").read()
    _co = open(os.path.join(_sb, "src", "collect.py"), encoding="utf-8").read()
    check('"hstech_down": 3300.5' in _th,
          "marketlib.TH 表没同步 —— 配置缺键时兜底仍是旧触发线")
    check('T("hstech_down", 3300.5)' in _an,
          "analyze.detect 的调用点字面默认值没同步")
    check("threshold('hstech_down',3300.5)" in _an
          or "threshold('hstech_down', 3300.5)" in _an,
          "analyze.brief 的关键位那处没同步")
    # ⚠️ 引号两种都要认：collect.py 里一处双引号（377 行）、
    # 一处单引号（382 行的 f-string 内层）—— 只认双引号会数成 1 处，
    # 把"两处都改好了"误判成"漏了一处"（实测踩到）。
    _co_n = len(re.findall(r'threshold\(\s*["\']hstech_down["\']\s*,'
                           r'\s*3300\.5\s*\)', _co))
    check(_co_n == 2,
          f"collect 的 2 处调用点字面默认值只同步了 {_co_n} 处")
    check("3395.38" not in _an.split("hstech_down")[1][:200]
          if "hstech_down" in _an else True,
          "analyze 里还有旧值 3395.38 残留在 hstech_down 附近")

    # ④ 不误伤：hstech_up 一个都不许动
    check('"hstech_up": 4243.65' in _th,
          "改 hstech_down 时把 hstech_up 也改了（前缀误伤）")

    # ⑦ 改完仍是合法 Python
    for _rel, _txt in (("marketlib.py", _th), ("analyze.py", _an),
                       ("collect.py", _co)):
        try:
            ast.parse(_txt)
        except SyntaxError as e:
            check(False, f"{_rel} 改完语法错误：{e}")

    # ⑤ 无空格写法 + ③ 同前缀键不误伤
    _note2 = AG._code_sync_threshold("gold_up", 3.3)
    check(_note2 is None, f"gold_up 同步返回失败：{_note2}")
    _an2 = open(os.path.join(_sb, "src", "analyze.py"), encoding="utf-8").read()
    check('T("gold_up",3.3)' in _an2,
          "无空格写法 `T(\"gold_up\",2.5)` 没同步到 —— 正则只认带空格的形态")
    check('T("gold_dn",-2.5)' in _an2,
          "改 gold_up 时把 gold_dn 也改了（前缀误伤）")

    # hstech_up 的末路常量
    _note3 = AG._code_sync_threshold("hstech_up", 4300.25)
    check(_note3 is None, f"hstech_up 同步返回失败：{_note3}")
    _th2 = open(os.path.join(_sb, "src", "marketlib.py"), encoding="utf-8").read()
    check("_HSTECH_UP_LAST_RESORT = 4300.25" in _th2,
          "`_HSTECH_UP_LAST_RESORT` 没同步 —— sectors.json 与 TH 同时缺键时"
          " hstech_up_threshold() 会落到旧常量")
    check('"hstech_down": 3300.5' in _th2,
          "改 hstech_up 时把 hstech_down 一起改了")

    # ⑦b ast.parse 守卫**必须真拦得住**（纵深防御，正常路径触发不到）。
    # 做法：临时把替换规则换成一条会破坏语法的（把右括号一起吃掉），
    # 验证它返回失败说明、**且文件没被写坏**。
    # ⚠️ 不这么测的话，"先 ast.parse 再落盘"这条守卫等于没验过 ——
    # 删掉它测试照样全绿（实测确认）。
    _snap = open(os.path.join(_sb, "src", "marketlib.py"),
                 encoding="utf-8").read()
    _save_pats = AG._CODE_SYNC_PATS
    try:
        AG._CODE_SYNC_PATS = (
            (re.compile(r'^(\s*"{}":\s*)(-?\d+(?:\.\d+)?)(\s*,?)', re.M), 2),
        )
        # 先人为破坏一处语法：让 TH 那行变成 `"hstech_down": <num>` 后面
        # 多一个不配对括号 —— 用替换规则直接注入坏 token
        _broken = _snap.replace('"hstech_down": 3300.5,',
                                '"hstech_down": 3300.5,,,')
        open(os.path.join(_sb, "src", "marketlib.py"), "w",
             encoding="utf-8").write(_broken)
        _note4 = AG._code_sync_threshold("hstech_down", 3400.0)
        check(_note4 is not None and "语法错误" in _note4,
              f"改写出坏语法时没有拦下（返回 {_note4!r}）—— "
              f"ast.parse 守卫形同虚设，一次改阈值就能让整套系统起不来")
        _after = open(os.path.join(_sb, "src", "marketlib.py"),
                      encoding="utf-8").read()
        check(_after == _broken,
              "坏语法被落盘了 —— 守卫只该返回说明，不该写文件")
    finally:
        AG._CODE_SYNC_PATS = _save_pats
        open(os.path.join(_sb, "src", "marketlib.py"), "w",
             encoding="utf-8").write(_snap)

    # ⑥ 源码树不存在 → 不是错误
    _empty = tempfile.mkdtemp(prefix="mw_t_thsync_empty_")
    _save = AG._MW_SRC
    try:
        AG._MW_SRC = _empty
        check(AG._code_sync_threshold("hstech_down", 1.0) is None,
              "源码树不存在时应静默跳过（沙箱/精简安装不是错误），"
              "却返回了失败说明")
    finally:
        AG._MW_SRC = _save
finally:
    if _old_mw is None:
        os.environ.pop("MW_DIR", None)
    else:
        os.environ["MW_DIR"] = _old_mw

# ---------- ⑧ 仓库当前状态：TH 与配置必须一致 ----------
_cfg = open(os.path.join(_ROOT, "conf", "sectors.json"),
            encoding="utf-8").read()
_ml = open(os.path.join(_ROOT, "src", "marketlib.py"), encoding="utf-8").read()
for _k in ("hstech_up", "hstech_down"):
    _m = re.search(r'"%s"\s*:\s*(-?\d+(?:\.\d+)?)' % _k, _cfg)
    check(bool(_m), f"conf/sectors.json 里找不到 {_k}")
    _cv = float(_m.group(1))
    _m2 = re.search(r'^\s*"%s"\s*:\s*(-?\d+(?:\.\d+)?),' % _k, _ml, re.M)
    check(bool(_m2), f"marketlib.TH 里找不到 {_k}")
    _tv = float(_m2.group(1))
    check(abs(_cv - _tv) < 1e-9,
          f"TH[{_k!r}]={_tv} 与配置 {_cv} 不一致 —— 配置缺键时触发线会静默错位"
          f"（这正是 2026-11-25 那次漂移：配置 3367.15、兜底 3395.38）")

print(f"ALL PASS (改阈值必须连代码兜底一起改 {_ok} 个断言)")
