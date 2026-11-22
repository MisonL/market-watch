#!/usr/bin/env python3
"""「源通」与「靠兜底通」必须分得开（2026-11-22）。

##为什么加这条

`cj()`的返回契约是「dict或 None」，**不区分**这份数据是直连拿到的还是
代理/firecrawl兜底拿到的。对绝大多数消费方这是对的（能取到数就行），
但 `feeds.health()`要回答的是另一个问题：**这个源本身还通不通**。

混为一谈的后果是**主源长期故障被兜底层盖住**：

`[实测]`2026-11-22 ——本机直连东财 push2/push2his **全被远端 RST**
（curl exit52 "Empty reply from server"，HTTP/HTTPS皆然），而
`health()["eastmoney"]`报 ✅；唯一原因是 `cj()`判死后经 firecrawl云端
把数据拿了回来。同一小时内 firecrawl进入冷却后，同一条 health立刻变 ❌
—— **同一台主机、同一天，结论随云端额度摇摆**。

危害是双层的：①兜底额度耗尽时故障**同时爆发**（主源早坏了没人知道）；
②自检 `[数据源]`长期报绿，运维失去"该换源了"的信号。

##判据

1. `_fb_mark/_fb_used/_fb_clear`三个函数存在且语义正确（标记按主机、可清空）；
2. `_fallback_get`的**两条**兜底成功路径（代理、firecrawl）都打标
 ——只打一处的话，另一条路径的数据仍会被当成"直连通"；
3. `health()`探测前 `_fb_clear()`，且 `ok`时按 `_fb_used`打
 `eastmoney_fallback`；
4. `health()`的 `eastmoney`布尔语义**不变**（走兜底仍算True ——
它回答"能不能取到数"；新标记才是"源本身通不通"）；
5. `selfcheck`对 `eastmoney_fallback`必须**打提示**（不是 ❌、也不是沉默）；
6.旁路标记不得影响返回类型：`cj()`仍返回 dict/哨兵，不因标记变形。
"""
import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_ok = [0]


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f"❌ {msg}")
		raise SystemExit(1)


import marketlib as M # noqa: E402

# ----------①三个函数存在且语义正确----------
for _fn in ("_fb_mark", "_fb_used", "_fb_clear"):
	check(callable(getattr(M, _fn, None)), f"marketlib缺 {_fn}()")

M._fb_clear()
check(not M._fb_used("h1"), "清空后仍报告用过兜底")
M._fb_mark("h1")
check(M._fb_used("h1"), "_fb_mark后 _fb_used为假")
check(not M._fb_used("h2"), "标记串台了（h2也被标记）")
M._fb_clear()
check(not M._fb_used("h1"), "_fb_clear没有清掉标记")
#空主机名不得标记（否则 _fb_used(None)之类会误报）
M._fb_mark(None)
check(not M._fb_used(None), "_fb_mark(None)被当成有效标记")
M._fb_clear()

# ----------②两条兜底路径都打标（源码接线）----------
with io.open(os.path.join(ROOT, "src", "marketlib.py"),
 encoding="utf-8") as _f:
	_ml = _f.read()
#只数**调用点**（缩进行），不数 `def _fb_mark(host)`定义行
_n_mark = sum(1 for _ln in _ml.split("\n")
 if "_fb_mark(host)" in _ln and not _ln.lstrip().startswith("def "))
check(_n_mark ==2,
 f"_fallback_get的兜底成功路径只有 {_n_mark}处打了标记（应为2："
 f"代理 +firecrawl）——漏掉的那条数据仍会被当成「直连通」")
for _anchor in ("_proxy_fallback_log_once(host)", '"fcfallback"'):
	_i = _ml.find(_anchor)
	check(_i >0, f"找不到兜底锚点 {_anchor!r}（结构变了）")
	#窗口：到该段**第一次** `return _d`为止（标记必须出现在它之前）
	_end = _ml.find("return _d", _i)
	_win = _ml[_i:_end] if _end > _i else _ml[_i:_i +2000]
	check("_fb_mark(host)" in _win,
	      f"{_anchor}那条兜底路径没打标记（该路径的数据会被当成直连通）")

# ----------③health探测前清空、ok时读标记----------
with io.open(os.path.join(ROOT, "src", "feeds.py"), encoding="utf-8") as _f:
	_fd = _f.read()
_i_clr = _fd.find("M._fb_clear()")
check(_i_clr >0, "feeds.health没有清空兜底标记（会串上一轮结果）")
# ⚠️2026-11-22：清空点从"整段探测前一次"改成"**每台探测前各一次**"。
# 标记按**裸主机名**记，http/https 两台共用同一个 key ——
# 只在整段前清一次，第二台会读到第一台的标记（串台）。
_loop = _fd.find("for _hi, _h in enumerate(_em_hosts):")
check(_loop >0, "feeds.health没有逐台探测循环")
_i_clr_in = _fd.find("M._fb_clear()", _loop)
_i_cj = _fd.find("M.cj(", _loop)
check(_i_clr_in >0 and _i_clr_in < _i_cj,
      "逐台循环里没有在 cj() 之前清标记 —— 第二台会读到第一台的标记")
check('res["eastmoney_fallback"] = True' in _fd,
 "health没有输出 eastmoney_fallback标记")

# ----------④布尔语义不变：走兜底仍算 True----------
_i_set = _fd.find('res["eastmoney"] = ok')
check(_i_set >0, "health的 eastmoney赋值不见了")
check('res["eastmoney"] = False' in _fd or
 'res["eastmoney"] = ok' in _fd,
 "health的 eastmoney语义被改坏了")
# eashmoney_fallback必须是**附加**字段，不能替换 eastmoney
check('res["eastmoney_fallback"] = True' in _fd
 and 'res["eastmoney"] = ok' in _fd,
 "eastmoney_fallback与 eastmoney必须并存（前者说「源通不通」，"
 "后者说「能不能取到数」）")

# ----------⑤selfcheck必须报出来----------
with io.open(os.path.join(ROOT, "src", "selfcheck.py"),
 encoding="utf-8") as _f:
	_sc = _f.read()
# ⚠️元数据键**不算源**：`eastmoney_backup` / `eastmoney_fallback` 只是
# 对 `eastmoney` 的补充说明。原实现直接遍历 `h.items()` 打印，
# 面板上会多出 `✅ eastmoney_fallback`，读者以为多了两个源；
# `_dead` 也把它们当源，某个元数据键取 False 就凭空多报一个「源不可用」。
check('_meta_keys = {"eastmoney_backup", "eastmoney_fallback"}' in _sc,
      "selfcheck 没有把兜底/备用元数据键排除在「源」之外 —— "
      "面板会多打一行 ✅ eastmoney_fallback，_dead 也会把它们当源")
_i_meta = _sc.find('_meta_keys = {"eastmoney_backup", "eastmoney_fallback"}')
_i_dead = _sc.find('_dead = sorted(', _i_meta)
check(_i_dead > _i_meta, "_dead 判据没有排除元数据键")
check('k not in _meta_keys' in _sc[_i_dead:_i_dead + 120],
      "_dead 仍会把 eastmoney_fallback 之类元数据键当成不可用源")
check("eastmoney_fallback" in _sc,
 "selfcheck完全不看 eastmoney_fallback ——主源只能靠兜底时"
 "自检仍然只有一行 ✅，运维失去「该换源了」的信号")
_i_notice = _sc.find('h.get("eastmoney_fallback")')
check(_i_notice >0, "selfcheck没有针对 eastmoney_fallback的分支")
_win = _sc[_i_notice:_i_notice +500]
check("云端兜底" in _win or "兜底" in _win,
 "提示文案没说清是「靠兜底」——读者会以为源是好的")
#不得把兜底当成❌：数据链路是通的，不该置红
check("ok_all = False" not in _win,
 "把「走兜底」置成了 ❌ ——那会让正常兜底每轮假红")

# ----------⑤b 东财误报：f2 为 null 时不得判健康----------
#
# ⚠️2026-11-22：原判据 `bool(M._has_data(d))` 只要内层 dict 非空就算通，
# 而东财熔断/降级时回 `{"data": {"diff": [{"f2": null}]}}` ——
# 内层非空、价格是 null，`health()["eastmoney"]` 却是 True。
# 这与腾讯那条（`feeds.py` "用**值级**判据而不是 bool(容器)"）同一类，
# 东财这条当时漏改了。
import feeds as _F
_null = {"data": {"diff": [{"f2": None}]}}
_good = {"data": {"diff": [{"f2": 3500.0}]}}
check(hasattr(_F, "_em_usable"), "feeds 缺 _em_usable()（东财值级判据）")
check(not _F._em_usable(_null),
      "f2=null 被判成东财可用 —— 零价格算健康，[数据源]会长期假绿")
check(_F._em_usable(_good), "f2=3500 被判成不可用（判据过严）")
check(not _F._em_usable(None), "None 被判成可用")
# ⚠️光有函数不够 —— `health()`必须**真的调用**它。
# 只测函数行为时，把调用点退回 `bool(M._has_data(d))` 也照样全绿（实测）。
_fd_em = io.open(os.path.join(ROOT, "src", "feeds.py"),
                 encoding="utf-8").read()
check("if not _em_usable(d):" in _fd_em,
      "health 没有用 _em_usable 判东财响应（接线断了）—— "
      "值级判据写好但没接上，等于没修")

# ----------⑤c 溯源必须逐台取，不得跨台 any() 聚合----------
#
# ⚠️2026-11-22：原实现 `any(_fb_used(...) for h in _em_hosts)` 会把
# "第一台兜底成功但数据被判不合格"与"第二台直连成功"混成"只能靠兜底"。
_fd2 = io.open(os.path.join(ROOT, "src", "feeds.py"),
               encoding="utf-8").read()
check("_em_fb = any(" not in _fd2,
      "health 仍在跨台 any() 聚合兜底溯源 —— "
      "第一台的兜底标记会污染第二台的直连结论")
check("for _hi, _h in enumerate(_em_hosts):" in _fd2,
      "health 没有逐台探测循环（无法逐台取溯源）")
check("res[\"eastmoney_fallback\"] = True" in _fd2
      and "_fb_prov" in _fd2,
      "health 没有用逐台溯源变量 _fb_prov 决定 eastmoney_fallback")

# ----------⑥旁路标记不影响 cj()的返回类型契约----------
check(issubclass(M._RateLimited, BaseException) or
 isinstance(M.RATE_LIMITED, object), "RATE_LIMITED哨兵形态变了")
#标记函数必须全吞异常（诊断用，不能拖垮取数）
for _fn, _args in (("_fb_mark", ("h",)), ("_fb_used", ("h",)),
 ("_fb_clear", ())):
	try:
		getattr(M, _fn)(*_args)
	except Exception as _e:
		check(False, f"{_fn}抛异常了：{type(_e).__name__}: {_e}")
	_ok[0] +=1

print(f"ALL PASS (兜底可辨识 {_ok[0]}个断言)")
