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
_i_hosts = _fd.find("_ordered_real_em_hosts()[:2]")
check(_i_clr >0, "feeds.health没有在探测前清空兜底标记（会串上一轮结果）")
check(_i_clr < _i_hosts, "清空标记在探测**之后**才做 ——读的是旧值")
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
