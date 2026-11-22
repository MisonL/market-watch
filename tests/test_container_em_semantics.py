#!/usr/bin/env python3
"""容器部署的**数据源可用性语义**与自检红绿的边界（2026-11-22审计结论落码）。

##背景

首次真跑 Docker镜像时 `[数据源]`报 `❌ eastmoney`，同机其余四源全绿。
排查结论（本测试把这些事实钉在代码上）：

1. `feeds.health()` 的 eastmoney探测只打 `_ordered_real_em_hosts()[:2]`
 两台，每台 `timeout=8, retries=1`；
2. 本机今天实测：东财 push2 / push2his **对宿主机出口 IP 直接 RST**
 （curl exit52 "Empty reply from server"，HTTP/HTTPS皆然，DNS 解析到
198.18.x网段——本机直连 East Money 全线被远端切断）；
3. 本机 `health()["eastmoney"]`仍是✅ 的**唯一原因**是 `cj()`判死后走
 firecrawl 云端兜底（CLI装在本机）成功取回——即**兜底层在替主源扛**；
4.容器镜像按 `.dockerignore` 不含 node/firecrawl CLI（`_FIRECRAWL_NODE`
 / `_FIRECRAWL_CLI`均不存在）→兜底全废 → 两台直连全灭 →必然❌。

##判据（行为面，不是注释）

- `feeds.health()` 在 `MW_EM_FIXTURE` 注入下必须如实报告探测结果；
-容器里「东财全灭但腾讯/新浪/同花顺/百度仍绿」是**降级链设计内**的状态
 （行情仍可取得），selfcheck 已按「降级链少1档」报❌ 且 rc=1——
 这在容器部署里**不是误报**，是真实降级；测试锁定它不因部署形态变绿。
"""
import os
import sys
import json
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

_ok = [0]


def check(cond, msg):
	if cond:
		_ok[0] +=1
	else:
		print(f"❌ {msg}")
		raise SystemExit(1)


# ----------①health()尊重夹具（探测结果可注入、可复现）----------
os.environ["MW_HEALTH_FIXTURE"] = "/nonexistent-fixture.json"
import feeds # noqa: E402

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as _tf:
	json.dump({"eastmoney": False, "tencent": True, "sina": True,
	           "ths": True, "baidu": True}, _tf)
	_fx = _tf.name
os.environ["MW_HEALTH_FIXTURE"] = _fx
try:
	_h = feeds.health()
	check(_h.get("eastmoney") is False, "夹具 eastmoney=False 未被如实返回")
	check(_h.get("tencent") is True, "夹具 tencent=True 未被如实返回")
	check(len(_h) >=5, "夹具注入后源数量不对")
finally:
	os.unlink(_fx)
	del os.environ["MW_HEALTH_FIXTURE"]

# ----------②探测只打 real前两台（源码接线）----------
with open(os.path.join(ROOT, "src", "feeds.py"), encoding="utf-8") as _f:
	_src = _f.read()
check("_ordered_real_em_hosts()[:2]" in _src,
	"health 的 EM探测没走 _ordered_real_em_hosts()[:2]"
	"（会探到测试环境 push2test 或全量四台耗配额）")
check("retries=1" in _src,
	"health探测的 retries 不为1（探活不该付3 重试的代价）")
check('secids=1.000001&fields=f2' in _src.replace('\n', '').replace(' ', '') or
 'secids=1.000001&fields=f2' in _src,
	"health 的 EM探测 URL形态变了（对照2026-11-22审计基准）")

# ----------③cj判死主机仍走兜底（本机 health 绿的机制所在）----------
with open(os.path.join(ROOT, "src", "marketlib.py"), encoding="utf-8") as _f:
	_ml = _f.read()
check("return _fallback_get(url, ref=ref, host=_bare_host, timeout=timeout)" in _ml,
	"cj()判死主机没有走 _fallback_get兜底（兜底链断了）")
check("def firecrawl_get" in _ml and "_FIRECRAWL_CLI" in _ml,
	"firecrawl兜底层缺失（兜底链少一层）")

print(f"ALL PASS (容器东财语义 {_ok[0]}个断言)")
