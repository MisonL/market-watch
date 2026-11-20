#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""推送排版：超长段必须拆行、末尾残句必须剔除。

## 为什么值得单测

prompt 的排版规则写得极细（规则⑤「必须拆成 2~4 个短段，每段一件事 /
200+ 字在手机上就是一堵墙」，规则⑫「不许写半截话」），但**代码侧长期
没有任何等效检查** —— 与「必带段不在代码侧检查就会静默消失」同族，
这次缺的是排版。

`[实测]` 2026-11-19 14:53「收盘前汇总」：AI 被预算闸门掐停后，`_invoke`
按设计「保留已产出的字符作为部分结果」，于是推出去的是：

    **关键位** （223 字一行）
    **盘面**   （289 字一行）→ 手机屏宽约 22 字，折 13 行且无缩进
    **资金**   （156 字一行，末尾停在「…转为 -2.23%、CP」，悬空的半截词）

同一天 13:36 那条（正常产出）AI 自己拆了 13 段、最大行 158 字 ——
对比之下这条明显是未完成品。代码层既没拆行、也没剔残句，照发了。

## 本文件锁住什么

- 超长段按「；」/「、」边界拆行，**内容零丢失**（去空白后拼回等于原文）
- 拆行幂等、正常排版的消息一个字都不改
- 末尾残句（无句末标点）被剔除，且**只删不补**
- 正常收尾（「。」「）」「%」）不被误删
- 两个函数都是纯函数、对非 str 原样返回
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import analyze as A

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


_ORIG_LOG = A._log
A._log = lambda *a, **k: None
try:
    # ---------- ① 超长段拆行 ----------
    _wall = ("**盘面** 全天定调：资金弃电商消费与 AI 应用，单向集中硬件上游。"
             "上证 -0.75%（涨 276 跌 2052）、沪深300 -0.61%、创业板指 -1.30% 领跌，"
             "科创50 +0.29%（涨 34 跌 16）独红；主力上证 -113.24 亿（占 -2.0%）、"
             "创业板 -60.12 亿（-2.2%），唯科创50 +15.37 亿（+1.6%）。"
             "科创50 成交已达上日全天 124%、上证 91%。港股恒指 -2.14%，"
             "腾讯 -6.97%、阿里 -5.51%，中芯 +2.74% 但自 12:13 的 +6.61% "
             "回落 3.87pp；恒生科技指数 -2.59%。")
    _r = A._split_wall_lines(_wall)
    _rows = [x for x in _r.split("\n") if x.strip()]
    check(len(_rows) > 1,
          f"289 字的段必须被拆开，实得 {len(_rows)} 行")
    # 2026-11-20 阈值 132→88（用户选定「4 行」）：拆完每行必须 ≤88。
    # ⚠️ 断言用的是**生产常量** `A._SPLIT_LIMIT`，不是抄 88 ——
    # 阈值再调时这里自动跟上，不会出现「测试锁死旧值」。
    check(max(len(x.strip()) for x in _rows) <= A._SPLIT_LIMIT,
          f"拆完每行应 ≤{A._SPLIT_LIMIT} 字，"
          f"实得最大 {max(len(x.strip()) for x in _rows)}")
    # 内容无损：去掉续行缩进与空格后拼回，必须等于原文（去空格后）
    _joined = _rows[0] + "".join(x.strip() for x in _rows[1:])
    check(_joined.replace(" ", "") == _wall.replace(" ", ""),
          "拆行**不得丢内容**（去空白后拼回必须等于原文）")
    # 续行带缩进，读者才看得出是同一段的延续
    check(all(x.startswith("  ") for x in _rows[1:]),
          f"续行必须缩进两格，实得 {[_rows[1][:6]]}")
    # 幂等
    check(A._split_wall_lines(_r) == _r, "拆行必须幂等")

    # ---------- ①a-2 真实生产消息：129 字那段必须被拆 ----------
    # ⚠️⚠️ 2026-11-20 用户反馈「优化一下排版」。根因不是"没拆行"，
    # 而是阈值取宽了：盘面段 129 字，距旧阈值 132 只差 3 字 → 不触发，
    # 手机上一折 8 行、续行无缩进。本项用**真实生产正文**锁住该形状。
    _real = (
        "**盘面** 六大指数全部下跌：上证 3528.07（-1.38%，涨66跌2262）、"
        "科创50 -2.33%、沪深300 -1.52%、创业板指 -2.15%、"
        "恒生指数 22515.86（-1.43%）、恒生科技 -3.28%。"
        "上证至此刻成交 484 亿，为上日全天的 11%；"
        "主力净流出 17.61 亿（占 -3.6%），属全面下跌，并非只有权重股拖累。")
    _rr = A._split_wall_lines(_real)
    _rrows = [x for x in _rr.split("\n") if x.strip()]
    check(len(_rrows) >= 2,
          f"129 字的盘面段必须被拆（旧阈值 132 只差 3 字就不拆），"
          f"实得 {len(_rrows)} 行")
    check(all(len(x.strip()) <= A._SPLIT_LIMIT for x in _rrows),
          f"拆完每行必须 ≤{A._SPLIT_LIMIT}，"
          f"实得 {[len(x.strip()) for x in _rrows]}")
    check(re.sub(r"\s", "", _rr) == re.sub(r"\s", "", _real),
          "拆真实生产段不得丢内容")
    # 阈值必须是「4 行屏宽」这个量级，不是被谁偷偷调回 132
    check(A._SPLIT_LIMIT <= 110,
          f"_SPLIT_LIMIT={A._SPLIT_LIMIT} 太宽：22 字/行下超过 5 行就是一堵墙"
          f"（规则⑤要求 2~4 个短段）")

    # ---------- ①b 无顿号边界时不得把段头甩成空行 ----------
    # ⚠️ `[实测]` 第一版拆行只找「、」，遇到"整句只有逗号"的段（盘面常见）
    # 就走到硬切，结果第一行只剩光秃秃的 `**盘面**`、正文从第二行才开始 ——
    # 读者看到的是一个空段。
    _comma = ("**盘面** " + "资金持续从消费电子流向硬件上游，" * 10
              + "尾盘跌幅收敛。")
    _cr = A._split_wall_lines(_comma)
    _crows = [x for x in _cr.split("\n") if x.strip()]
    check(len(_crows) > 1, "无顿号的长段也应被拆开")
    check(len(_crows[0].strip()) > len("**盘面**") + 10,
          f"首行不得只剩段头，实得 {_crows[0]!r}")
    check(not _crows[1].lstrip().startswith("金持续"),
          "不得在词中硬切（「资金」不能被劈成「资」+「金」）")
    check((_crows[0] + "".join(x.strip() for x in _crows[1:])).replace(" ", "")
          == _comma.replace(" ", ""), "无标点长段拆行同样不得丢内容")

    # ---------- ② 正常排版一个字都不改 ----------
    _fine = ("> **午后开局 · 港股破位**\n\n\u3000\n"
             "**触发** 恒生科技 3648.71 跌破下方触发线 3695。\n\u3000\n"
             "**关注板块**\n半导体 -0.89%（-16.2 亿）\n存储芯片 -0.75%（+0.2 亿）")
    check(A._split_wall_lines(_fine) == _fine,
          "正常排版的消息不得被改动")

    # ---------- ③ 末尾残句剔除 ----------
    _cut = ("**资金** 半导体 +20.7 亿、CPO 由负转正 +1.2 亿；"
            "印制电路板自 12:13 的 -0.38% 转为 -2.23%、CP")
    _r2 = A._drop_dangling_tail(_cut)
    check(_r2 != _cut, "末尾悬空的「、CP」必须被剔除")
    check("CP" not in _r2.split("；")[-1],
          f"残句「CP」必须整段删掉，实得 {_r2[-30:]!r}")
    check(_r2.endswith("。") or _r2.endswith("；"),
          f"剔除后应以句末标点收尾，实得 {_r2[-3:]!r}")
    # ⚠️ 只删不补：不得出现原文没有的字
    check(all(ch in _cut or ch in "。" for ch in _r2),
          "只删不补 —— 不得补写任何原文没有的字")

    # ---------- ③b 已带系统时间戳时同样要剔 ----------
    # ⚠️ `[实测]` 我第一版见到末行是 `> 触发…` 就 `return t` —— 判据整体失效，
    # 残句照样留着。生产路径上本函数跑在 `_fin()` 加戳**之前**，所以第一版
    # 碰不到；但「只有生产调用顺序才对」不是函数契约，必须显式锁住。
    _stamped = _cut + "\n\n　\n> 触发 14:49:51 · 耗时 245s"
    _r2b = A._drop_dangling_tail(_stamped)
    # ⚠️⚠️ 判据两处踩过坑，都记在这里：
    #   ① 第一版只查 `[-1]`（末行是时间戳，当然不含残句）→ 断言恒绿，
    #      注入缺陷后 19 条全过、缺陷漏网；
    #   ② 第二版改成查整条文本 `"、CP" not in _r2b` → 更糟，因为正文里
    #      本来就有断言串（句中是「亿、CP**O**」），子串匹配恒真。
    # 正确判据：**去除系统耗时后缀后的最后一行**，必须不以残句收尾。
    _body_tail = [x for x in _r2b.split("\n")
                  if x.strip() and not x.strip().startswith(">")]
    check(_body_tail and not _body_tail[-1].rstrip().endswith("CP"),
          f"正文末行不得以残句「CP」收尾，实得 {_body_tail[-1][-16:]!r}"
          if _body_tail else "正文末行不存在")
    check(_body_tail and _body_tail[-1].rstrip().endswith(("。", "；")),
          f"正文末行应以句末标点收尾，实得 {_body_tail[-1][-6:]!r}"
          if _body_tail else "正文末行不存在")
    check(_r2b.rstrip().endswith("耗时 245s"),
          "系统时间戳必须原样保留在最后")

    # ---------- ④ 正常收尾不得误删 ----------
    for _good in ("**资金** 半导体 +20.7 亿，AI应用 -43.4 亿。",
                  "**关键位** 上证 3561.07（+0.02%）",
                  "**触发** 恒生科技跌破 3695。",
                  "**盘面** 上证 -0.75%，涨 276 跌 2052%"):
        check(A._drop_dangling_tail(_good) == _good,
              f"正常收尾不得被改：{_good[-12:]!r}")
    # 幂等
    check(A._drop_dangling_tail(_r2) == _r2, "残句剔除必须幂等")

    # ---------- ④b 续行缩进不得被下游当成"板块行" ----------
    # ⚠️⚠️ 2026-11-19 生产事故（自检 rc=1 + 推告警）：`_split_wall_lines`
    # 把隔夜外盘 / 美债 / 关键位等超长段拆成多行后，**续行缩进两格**、
    # 同样含「%」、同样不以 `**` `>` `【` 开头 —— 于是 selfcheck 里
    # 「兜底模板每行板块都要带盘前标注」那条判据把它们全当成缺标注的
    # 板块行，报「4 行缺少盘前标注」、整体 rc=1。
    #
    # 这不是 selfcheck 写错了，是**我的拆行引入的新行形态**没被下游识别。
    # 本项锁住两件事：
    #   ① 续行必须缩进（下游靠这个区分）
    #   ② selfcheck 的板块行判据必须要求顶格
    _wall2 = ("**隔夜全球** " + "、".join(
        f"指数{i} -{i}.00%" for i in range(1, 40)) + "。")
    _r3 = A._split_wall_lines(_wall2)
    _r3rows = [x for x in _r3.split("\n") if x.strip()]
    check(len(_r3rows) > 1, "超长隔夜段应被拆行")
    check(all(x.startswith("  ") for x in _r3rows[1:]),
          "续行必须缩进两格 —— 下游靠缩进区分续行与独立板块行")
    # 行为级复刻 selfcheck 的那段场景：兜底模板 + 09:18 → 每个**顶格**
    # 板块行都必须带「盘前参考价」，且续行不得被算作板块行。
    #
    # ⚠️⚠️ 我第一版这里写的是 `check('x == x.lstrip()' in 源码)` —— 而
    # selfcheck.py 的**注释里**恰好也写了 `x == x.lstrip()`，于是把代码判据
    # 删掉、注入缺陷，断言**照样绿**。这是本次任务里同一个错误犯的第三次：
    # 判据指向的对象看不见要测的缺陷（前两次分别是「查 `[-1]`」和
    # 「子串 `、CP` 撞上 `、CPO`」）。所以这里改成**跑真逻辑**。
    import datetime as _dt
    import shutil as _sh
    # ⚠️ 本项要真的产出板块行，就必须有 `conf/sectors.json`。而
    # `tests/run_all.sh` 给每个测试设了**独立临时 MW_DIR**（隔离状态），
    # 那个目录里没有 conf —— `_cfg_items("push")` 返回空，板块行一行都
    # 产不出来，本项就退化成"空转失败"（`[实测]` 单跑通过、全量跑失败，
    # 差的就是这个）。
    # 做法：把 conf 拷进当前 MW_DIR（只读复制，不改内容），让兜底模板
    # 与线上走同一份配置。
    _conf_src = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "conf")
    _conf_dst = os.path.join(str(getattr(A, "DIR", "") or
                                 os.environ.get("MW_DIR", "")), "conf")
    if _conf_dst and not os.path.isdir(_conf_dst) and os.path.isdir(_conf_src):
        _sh.copytree(_conf_src, _conf_dst)
    _orig_ai = A.ai_analyze
    try:
        A.ai_analyze = lambda *a, **k: ""          # 强制走兜底模板
        _now = _dt.datetime.combine(_dt.date.today(), _dt.time(9, 18))
        _q = {"1.000001": {"f2": 3840.0, "f3": 0.27},
              "124.HSTECH": {"f2": 4243.0, "f3": -0.15}}
        for _s, _i in A._cfg_items("push"):
            _c = _i.get("code")
            if _c:
                _q[_c] = {"f2": 1000.0, "f3": -2.5,
                          "f62": -5e9, "f184": -6.0}
        _b = A.build_brief(_q, None, [], _now, hot=None, slot="09:25")
        # ⚠️ 变量名**不能叫 `_ok`** —— 它是模块级的断言计数器，
        # 被这里覆盖后 `check()` 里的 `_ok += 1` 变成 `True += 1`，
        # 计数与"是否失败"全部失真（`[实测]` 覆盖后只报 7 个断言，
        # 实际执行了 20 多个）。
        _t, _ok_ai, _h_ai = A.produce_text(_b, [], "", "09:25", _q, _now)
        # ⚠️⚠️ **判据只有一份实现**：`analyze.sector_rows()`。
        # selfcheck 与本测试都调它 —— 这样"改了 selfcheck 的判据"能被这里
        # 测到。原先把判据在测试里**复刻**一遍，结果把 src/selfcheck.py 退回
        # 未修复版，测试照样全绿（`[实测]` 已复现）—— 那是同义反复，
        # 测的是我抄的那份副本，不是生产代码。
        _rows = A.sector_rows(_t)
        _bad = [x for x in _rows if "盘前参考价" not in x]
        check(bool(_rows), "兜底模板必须产出顶格板块行（否则本项空转）")
        check(not _bad,
              f"顶格板块行必须都带盘前参考价，实得 {len(_bad)} 行缺："
              f"{_bad[:2]}")
        # 反向校验：本场景必须**真的**产生了缩进续行，否则"续行不参与核对"
        # 这条修复无从验证（空转保护）。
        _all_rows = [x for x in _t.split("\n")
                     if "%" in x and not x.startswith(("**", ">", "【"))]
        check(len(_all_rows) > len(_rows),
              "本场景必须真的产生了缩进续行（否则本项测不到那个缺陷）")
        # 且续行里确实有"缺标注"的行 —— 证明放宽判据就会误报。
        check(any("盘前参考价" not in x for x in _all_rows),
              "续行中必须存在不带盘前参考价的行 —— 否则本项测不到误报")
    finally:
        A.ai_analyze = _orig_ai

    # ---------- ⑤ 纯函数契约 ----------
    for _f in (A._split_wall_lines, A._drop_dangling_tail):
        check(_f(None) is None and _f("") == "" and _f(123) == 123,
              f"{_f.__name__} 对非 str 必须原样返回（纯函数会被直接调用）")
finally:
    A._log = _ORIG_LOG


# ---------- ⑥ 首行必须按段头宽度扣掉可用宽度 ----------
#
# ⚠️⚠️ 2026-11-20 生产实测漏网：13:40 那条推送拆完后仍有 3 行超 88
# （**盘面** 92 / **资金** 92 / **热点** 91），全是**首行**。
# 根因：切分一律按 `_LIMIT - 2`（给续行的 2 空格缩进留位置），
# 但首行前面顶着 `**段名**`（`**盘面** ` 占 9 字）—— 两者宽度不同，
# 用一个常数切，首行就会溢出 `len(head)` 那么多。
# 段名越长越离谱，而这个洞**测试原本抓不到**（旧断言只看「拆完最大行」
# 用的是那条 289 字的假数据，恰好没触发）。
_real13 = (
    "**盘面** 全面下跌、无权重护盘：上证 3511.53（-1.85%，涨 138 跌 2194）、"
    "科创50 1225.46（-3.57%）、创业板指 2236.64（-2.47%）、"
    "恒生指数 22318.95（-2.29%）。沪市成交 为上日全天的 85%，量能未见收缩。")
_out13 = A._split_wall_lines(_real13)
_rows13 = [x for x in _out13.split("\n") if x.strip()]
check(len(_rows13) >= 2, f"92 字带段头的段必须被拆，实得 {len(_rows13)} 行")
_bad13 = [(len(x.strip()), x.strip()[:40]) for x in _rows13
          if len(x.strip()) > A._SPLIT_LIMIT]
check(not _bad13,
      f"带段头的首行也必须 ≤{A._SPLIT_LIMIT}，超限 {_bad13} —— "
      f"首行可用宽度是 `LIMIT - len(head)`，不是 `LIMIT - 2`")
check(re.sub(r"\s", "", _out13) == re.sub(r"\s", "", _real13),
      "首行扣宽不得丢内容")
check(A._split_wall_lines(_out13) == _out13, "带段头的段拆完必须幂等")

# 长段名（比普通 4 字段名更长）同样不能溢出
_longhead = ("**关注板块与资金流** " +
             "、".join(f"板块{i} -{i}.00%（-{i}0.0 亿）" for i in range(1, 12)))
_ohl = [x for x in A._split_wall_lines(_longhead).split("\n") if x.strip()]
check(all(len(x.strip()) <= A._SPLIT_LIMIT for x in _ohl),
      f"长段名首行溢出：{[len(x.strip()) for x in _ohl]} —— "
      f"段头越长，首行可用正文越窄")

print(f"ALL PASS (推送排版拆行/残句 {_ok} 个断言)")
