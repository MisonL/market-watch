#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""取数阶段的**全局**时间上限（2026-11-20，P1）。

## 它治的那个结构性缺陷

`AI_ROUND_BUDGET`(240s) 是**减去取数耗时**再给 AI 的：

    _budget = AI_ROUND_BUDGET − (取数耗时 + 已用 AI 耗时)

而取数阶段原先**没有总数**，只有各数据源自己的预算，三者可叠加：

    hot.fetch_sectors 的 clist   60s
    hot.MEMBERS_BUDGET           40s
    hot.VR_BUDGET                80s
    ─────────────────────────── 180s

取数一旦吃到 172s，AI 只剩 68s —— 而成功轮 AI 耗时的 P90 是 140s、
最长 210s，**结构性必死**。`[实测]` 11-18 14:54（取数 172s / AI 上限
68s / 退模板）、11-19 09:23（108s / 132s / 退模板）都是这个形态。

近 20 天 986 轮 `data_ms`：P50=0.1s P90=13.6s P99=75s 最大 171.8s；
>60s 只占 1.6%。故 120s 作硬上界 —— 截掉长尾，P99 的正常轮不受影响。

## 本文件锁住什么

① `data_cap` 的四种组合（自身／全局／取小／都为 None）
② **默认不生效**：没调 `set_data_deadline` 时 `data_cap(60)` 仍是 60
   （否则等于给所有数据源平白加了 120s 上限，改了原有语义）
③ 钳到了必须**留痕**（每源每轮一次）—— 降级不可见是本仓库反复的缺陷
④ 三处接线真的接上了（clist / members / 量比），不是加了函数没人调
⑤ `analyze.run()` 每轮都会设截止，且用的是本轮起点 `_t0`
"""
import os
import sys
import time
import tempfile
import atexit
import shutil
import ast

_d = tempfile.mkdtemp(prefix="mw_t_dbud_")
os.environ["MW_DIR"] = _d
atexit.register(shutil.rmtree, _d, ignore_errors=True)
_src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
sys.path.insert(0, _src)

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_logs = []
M.log = lambda m, tag="": _logs.append(m)

# ---------- ② 默认不生效（最重要的一条）----------
M.set_data_deadline(None)
_ok_before = _ok
check(M.data_left() is None, "未设截止时 data_left() 应为 None")
check(M.data_cap(60) == 60,
      f"未设截止时 data_cap(60) 必须是 60（不限制），实得 {M.data_cap(60)}")
check(M.data_cap(None) is None, "未设截止时 data_cap(None) 应为 None")
check(not _logs, f"未设截止时不该记任何日志，实得 {_logs}")

# ---------- ① 四种组合 ----------
M.set_data_deadline(time.time() + 30)
check(abs(M.data_cap(60) - 30) < 2,
      f"全局 30s 更紧时应取 30，实得 {M.data_cap(60)}")
M.set_data_deadline(time.time() + 90)
check(abs(M.data_cap(60) - 60) < 2,
      f"自身 60s 更紧时应取 60，实得 {M.data_cap(60)}")
M.set_data_deadline(time.time() + 45)
check(abs(M.data_cap(None) - 45) < 2,
      f"自身 None 时应用全局 45，实得 {M.data_cap(None)}")
# 已超时：返回负数（刻意不钳到 0 —— 调用方自己加下限，
# 钳成 0 会让 budget_s=0 变成"一页都不翻"，连已到手的第一页都拿不到）
M.set_data_deadline(time.time() - 5)
check(M.data_cap(60) < 0,
      f"已超时时应返回负值（由调用方加下限），实得 {M.data_cap(60)}")

# ---------- ③ 钳到必须留痕，且每源每轮只记一次 ----------
_logs.clear()
M.set_data_deadline(time.time() + 10)
for _ in range(3):
    M.data_cap(80, "量比")
_cap_logs = [x for x in _logs if "量比" in x]
check(len(_cap_logs) == 1,
      f"同一数据源一轮内只该记 1 条降级日志，实得 {len(_cap_logs)}")
check("取数全局上限生效" in _cap_logs[0],
      f"降级日志要能被 grep（含「取数全局上限生效」），实得 {_cap_logs[0]!r}")
M.data_cap(80, "members")
check(len([x for x in _logs if "members" in x]) == 1,
      "不同数据源应各自留痕")
# 新一轮（重设截止）后去重集清空
_logs.clear()
M.set_data_deadline(time.time() + 10)
M.data_cap(80, "量比")
check(len(_logs) == 1, f"新轮次应重新留痕，实得 {_logs}")

# 没被钳（自身更紧）时**不该**留痕 —— 否则日志噪音淹没真降级
_logs.clear()
M.set_data_deadline(time.time() + 999)
M.data_cap(60, "clist")
check(not _logs, f"未被钳时不该留痕，实得 {_logs}")

M.set_data_deadline(None)

# ---------- ④ 三处接线（读生产源码，不是读副本）----------
_tree_ml = ast.parse(open(os.path.join(_src, "marketlib.py"),
                          encoding="utf-8").read())
_tree_hot = ast.parse(open(os.path.join(_src, "hot.py"),
                           encoding="utf-8").read())
_tree_an = ast.parse(open(os.path.join(_src, "analyze.py"),
                          encoding="utf-8").read())


def _calls_in(tree, names):
    """返回树里所有 `X.name(...)` / `name(...)` 的 attr/id 名单。"""
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            if isinstance(n.func, ast.Attribute):
                out.append(n.func.attr)
            elif isinstance(n.func, ast.Name):
                out.append(n.func.id)
    return [x for x in out if x in names]


_ml_caps = _calls_in(_tree_ml, {"data_cap"})
_hot_caps = _calls_in(_tree_hot, {"data_cap"})
_an_sets = _calls_in(_tree_an, {"set_data_deadline"})

check(_ml_caps, "marketlib.clist 没接 data_cap —— 全局上限对板块列表无效")
check(len(_hot_caps) >= 2,
      f"hot.py 应有 ≥2 处 data_cap（members + 量比），实得 {len(_hot_caps)}")
check(_an_sets, "analyze.run() 没调 set_data_deadline —— 全局上限永不生效")

# ⑤ run() 设截止必须用 _t0（与本轮耗时同源），不能重新取时钟
_found_t0 = False
for _n in ast.walk(_tree_an):
    if (isinstance(_n, ast.Call) and isinstance(_n.func, ast.Attribute)
            and _n.func.attr == "set_data_deadline"):
        _txt = ast.unparse(_n)
        if "_t0" in _txt and "DATA_BUDGET" in _txt:
            _found_t0 = True
check(_found_t0,
      "set_data_deadline 必须以 `_t0 + DATA_BUDGET` 调用（与 _ROUND_DATA_MS "
      "同源，否则「取数没超预算」这句话会自相矛盾）")

# ⑥ 常量本身要覆盖实测 P99（75s）又不至于形同废除
check(90 <= M.DATA_BUDGET <= 180,
      f"DATA_BUDGET={M.DATA_BUDGET} 不在合理区间 [90,180]："
      f"太小会截掉正常轮次（P99=75s），太大等于没设")



# ==================== 新增：编排层预算闸（2026-11-20，P1）====================
#
# `[实测]` 2026-11-20 11:42 那轮 300s 的构成：
#     0~138s   quotes/trend/detect（静默，一条日志都没有）
#     138~151s clist/members（已被 120s 上限钳住 → 立即放弃）
#     151~300s **news**（含 11:40:20 的 17s curl 超时）
# 代价极不对称：news 只影响 brief 里几行"相关快讯"，
# 而它跑满让 AI 拿到 0s —— 那轮 AI 完全没跑，推送只剩 406 字残稿
# （「热点」段头下空无一字），还饿死 1 次触发（starved_slots=1）。
#
# 所以在**编排层**（analyze）加闸：预算耗尽就跳过 trend 补取与 news 取数。
# 刻意**不动 cj()** 的单次超时 —— 把 20s 压到 3s 会让本来能成功的请求
# 失败，是另一类错误（同 `data_cap` 定义处的取舍）。
_an = open(os.path.join(_src, "analyze.py"), encoding="utf-8").read()

# ① 两处闸门都真的存在，且用的是**同一个** `data_left()` 时钟
check(_an.count("M.data_left()") >= 2,
      f"analyze 应至少有 2 处 data_left() 闸（trend + news），"
      f"实得 {_an.count('M.data_left()')}")

# ② news 闸必须真的把 ns 置空，而不是只打日志
_idx_ns = _an.find("跳过快讯取数")
check(_idx_ns > 0, "缺少「跳过快讯取数」日志 —— news 闸没接上")
_after = _an[_idx_ns:_idx_ns + 400]
check("ns = []" in _after,
      f"news 闸打日志后必须真的把 ns 置空，实得 {_after[:160]!r}")

# ③ trend 闸：条件必须真的能被求值左右（AST 级别，不是看源码形状）
#
# ⚠️⚠️ 这条判据我写坏过一次，记在这里：第一版是"源码里有没有
# `_trend(`、缩进够不够"—— 把条件是 `if _dl is not None and _dl <= 0:`
# 改成 `if False:`，**断言照样全绿**（`_trend(` 还在 else 里、缩进也没变）。
# 那测的是"这段代码长这样"，不是"闸门会生效"。
# 现在改成解析 AST：取含「跳过分时补取」的那个 If 节点，
# 断言它的 test **真的引用了 data_left/_dl**、body 里有跳过日志、
# orelse 里有 `_trend` 调用。三样缺一即失败。
_tree_an = ast.parse(_an)


def _find_if_with(marker, tree):
    """取**最内层**含 `marker` 的 if。

    ⚠️ 不能"第一个匹配"：外层 `if not isinstance(sg, dict):` 的 body 里
    就嵌着内层那个闸门，`ast.unparse(外层)` 自然含 marker，
    于是"第一个"永远返回外层 —— 判的就不是闸门本身。
    取节点数最少的那个 = 最内层 = 真正包住日志的那一层。
    """
    _cands = []
    for _n in ast.walk(tree):
        if isinstance(_n, ast.If) and marker in ast.unparse(_n):
            _cands.append((sum(1 for _ in ast.walk(_n)), _n))
    if not _cands:
        return None
    _cands.sort(key=lambda x: x[0])
    return _cands[0][1]


_if_tr = _find_if_with("跳过分时补取", _tree_an)
check(_if_tr is not None, "找不到「跳过分时补取」的 if 闸门")
if _if_tr is not None:
    _test_src = ast.unparse(_if_tr.test)
    check(("data_left" in _test_src or "_dl" in _test_src),
          f"trend 闸的条件必须真的求值预算（含 data_left/_dl），"
          f"实得 test = {_test_src!r}")
    check(_if_tr.orelse,
          "trend 闸必须有 else 分支 —— 否则预算充足时分时永远不取")
    _else_src = ast.unparse(ast.Module(body=_if_tr.orelse, type_ignores=[]))
    check("_trend(" in _else_src,
          f"trend 闸的 else 分支必须调用 _trend，实得 {_else_src[:120]!r}")

# ③b news 闸同样用 AST 判（条件 + 置空 + else 取数）
_if_ns = _find_if_with("跳过快讯取数", _tree_an)
check(_if_ns is not None, "找不到「跳过快讯取数」的 if 闸门")
if _if_ns is not None:
    _t2 = ast.unparse(_if_ns.test)
    check(("data_left" in _t2 or "_dl2" in _t2),
          f"news 闸的条件必须真的求值预算，实得 test = {_t2!r}")
    _body2 = ast.unparse(ast.Module(body=_if_ns.body, type_ignores=[]))
    check("ns = []" in _body2 or "ns=[]" in _body2,
          f"news 闸 body 必须真的把 ns 置空，实得 {_body2[:160]!r}")
    _else2 = ast.unparse(ast.Module(body=_if_ns.orelse, type_ignores=[]))
    check("fetch_news" in _else2,
          f"news 闸 else 必须仍调 fetch_news，实得 {_else2[:120]!r}")

# ④ 空 news 列表下游必须安全（否则跳过会炸整轮）
sys.path.insert(0, _src)
import news as _NEWS
check(_NEWS.related_news("半导体", [], within_min=120, max_n=2) == [],
      "related_news([]) 必须返回空列表，不能抛异常")
check(_NEWS.macro_news([], within_min=120, max_n=2) == [],
      "macro_news([]) 必须返回空列表，不能抛异常")


# ---------- ⑤ 行为级：直接执行生产源码里的闸门语句 ----------
#
# ⚠️⚠️ 为什么还要这一层：上面 ③③b 的 AST 判据只看"条件里有没有 data_left"，
# 把条件改成 `if False:` 能抓住，但改成 `if True:` 或 `if _dl is not None
# and _dl > 9999:` 这类"形式对、语义错"的写法仍然全绿。
# 这里把**生产源码里那两条真实语句**取出来 `exec` 一遍（不抄副本），
# 用两头夹：预算耗尽必须跳、预算充足必须取。少了任何一头，
# "闸门在 ≠ 闸门生效"就会重新溜过去。
_tree_b = ast.parse(_an)


def _gate_block(marker):
    """取包含该 if 的真实语句块（连同前面 `_dl = M.data_left()` 的赋值）。"""
    _c = []
    for _n in ast.walk(_tree_b):
        if isinstance(_n, ast.If) and marker in ast.unparse(_n):
            _c.append((sum(1 for _ in ast.walk(_n)), _n))
    if not _c:
        return None
    _c.sort(key=lambda x: x[0])
    _tgt = _c[0][1]
    for _n in ast.walk(_tree_b):
        for _f in ("body", "orelse"):
            _b = getattr(_n, _f, None)
            if isinstance(_b, list) and _tgt in _b:
                _i = _b.index(_tgt)
                _j = _i - 1 if _i > 0 and isinstance(_b[_i - 1], ast.Assign) else _i
                return _b[_j:_i + 1]
    return [_tgt]


class _FakeM:
    def __init__(self, left):
        self.left = left

    def data_left(self):
        return self.left

    def log(self, *a, **k):
        pass


class _Cnt:
    def __init__(self):
        self.n = 0

    def __call__(self, *a, **k):
        self.n += 1
        return {}


def _exec_gate(marker, left, extra):
    _blk = _gate_block(marker)
    check(_blk is not None, f"取不到「{marker}」闸门的真实语句块")
    if _blk is None:
        return {}
    _ns = {"M": _FakeM(left)}
    _ns.update(extra)
    _code = "\n".join(ast.unparse(_x) for _x in _blk)
    exec(compile(_code, "<gate>", "exec"), _ns)   # noqa: S102
    return _ns


# trend 闸：耗尽跳过 / 充足取数（两头夹）
_c1 = _Cnt()
_exec_gate("跳过分时补取", -30, {"_trend": _c1})
check(_c1.n == 0, f"预算耗尽(left=-30s)时 _trend 仍被调用 {_c1.n} 次 —— 闸门没生效")
_c2 = _Cnt()
_exec_gate("跳过分时补取", 60, {"_trend": _c2})
check(_c2.n == 1,
      f"预算充足(left=+60s)时 _trend 应被调用 1 次，实得 {_c2.n} —— "
      f"闸门把正常轮次也跳过了")

# news 闸：耗尽必须置空且不取数 / 充足取数
class _NewsStub:
    def __init__(self, c):
        self.c = c

    def fetch_news(self, *a, **k):
        self.c.n += 1
        return []


_c3 = _Cnt()
_ns3 = _exec_gate("跳过快讯取数", -30, {"ns": None, "NEWS": _NewsStub(_c3)})
check(_c3.n == 0, f"预算耗尽时 fetch_news 仍被调用 {_c3.n} 次")
check(_ns3.get("ns") == [], f"预算耗尽时 ns 必须置空，实得 {_ns3.get('ns')!r}")
_c4 = _Cnt()
_exec_gate("跳过快讯取数", 60, {"ns": None, "NEWS": _NewsStub(_c4)})
check(_c4.n == 1, f"预算充足时 fetch_news 应被调用 1 次，实得 {_c4.n}")

# 边界：left 恰为 0 必须跳过（<=0 而不是 <0）
_c5 = _Cnt()
_exec_gate("跳过分时补取", 0, {"_trend": _c5})
check(_c5.n == 0, "left 恰为 0 时必须跳过（判据是 <=0），否则边界上会多跑一次取数")

# 未设截止（data_left()=None，collect 等场景）不得跳过
_c6 = _Cnt()
_exec_gate("跳过分时补取", None, {"_trend": _c6})
check(_c6.n == 1,
      "data_left()=None（本轮未设截止）时必须照常取数，实得跳过 —— "
      "会把 collect 等无截止场景一起误伤")

print(f"ALL PASS (取数全局上限 + 编排层闸门 {_ok} 个断言)")
