"""腾讯日K取数：直连失败时必须经代理兜底（2026-11-03 修，P3）。

复现背景：`_kline_ohlc` 的腾讯分支是**裸 curl**，不走 `cj()`/`proxy_get` 的
「直连→代理→firecrawl」降级链（其余取数路径都有）。`[实测]` 2026-11-02：
本机 IP 被腾讯 WAF 风控后 `web.ifzq.gtimg.cn` 全 501，6 个指数（上证/科创50/
沪深300/创业板指/恒指/恒科）的关键位段整段退化成"上一交易日收盘"。
走 `MW_PROXY` 指定的代理能立即绕开本机 IP 风控。

判据与取值分离：`_tx_kline_fetch` 是纯函数，喂构造的直连/代理取值即可测，
不需要真网络。

  1. 直连拿到合法 JSON → 用直连结果，**不**调代理（不白付一次代理请求）
  2. 直连空响应 → 调代理兜底
  3. 直连是 WAF 页（非 JSON）→ 调代理兜底（这是 2026-11-02 的真实形态）
  4. 直连抛异常 → 调代理兜底
  5. 代理也拿到 → 返回代理结果，不被误判为失败
  6. 两路都失败 → None（与原来裸 curl 失败同路，上层 return None）
"""
import os, sys, tempfile, atexit, shutil
# 只在**本文件自己建**临时 MW_DIR 时才注册清理（setdefault 语义）——
# 调用方给的 MW_DIR（变异回归沙箱）绝不能删。
if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import marketlib as M

# 腾讯列序: [日期, 开, 收, 高, 低, 量]（收盘在 x[2]）
GOOD = ('{"data":{"sh000001":{"qfqday":'
        '[["2026-11-03","3900","3950","3960","3880","12345"]]}}}')
WAF = "<html><head><title>501 Not Implemented</title></head></html>"

_calls = []


def _mk(direct_ret, proxy_ret):
    _calls.clear()

    def direct(url):
        _calls.append("direct")
        if isinstance(direct_ret, Exception):
            raise direct_ret
        return direct_ret

    def proxy(url):
        _calls.append("proxy")
        return proxy_ret
    return direct, proxy


# 1) 直连成功 → 不调代理
d, p = _mk(GOOD, GOOD)
_rows = M._tx_kline_fetch("u", "sh000001", direct=d, proxy=p)
assert _rows and _rows[0][2] == "3950", _rows
assert _calls == ["direct"], _calls
print("PASS 1 direct-ok-no-proxy")

# 2) 直连空响应 → 代理
d, p = _mk("", GOOD)
_rows = M._tx_kline_fetch("u", "sh000001", direct=d, proxy=p)
assert _rows and _rows[0][2] == "3950", _rows
assert _calls == ["direct", "proxy"], _calls
print("PASS 2 direct-empty-falls-back")

# 3) 直连 WAF 页（非 JSON）→ 代理
d, p = _mk(WAF, GOOD)
_rows = M._tx_kline_fetch("u", "sh000001", direct=d, proxy=p)
assert _rows and _rows[0][2] == "3950", _rows
assert _calls == ["direct", "proxy"], _calls
print("PASS 3 direct-waf-falls-back")

# 4) 直连抛异常 → 代理（异常必须被吞掉，不能冒泡）
d, p = _mk(RuntimeError("boom"), GOOD)
_rows = M._tx_kline_fetch("u", "sh000001", direct=d, proxy=p)
assert _rows and _rows[0][2] == "3950", _rows
assert _calls == ["direct", "proxy"], _calls
print("PASS 4 direct-raise-falls-back")

# 5) 两路都失败 → None
d, p = _mk(WAF, WAF)
assert M._tx_kline_fetch("u", "sh000001", direct=d, proxy=p) is None
print("PASS 5 both-fail-none")

# 6) 反向：直连空、代理给合法 JSON 时必须真的采用代理结果
d, p = _mk("", GOOD)
_r6 = M._tx_kline_fetch("u", "sh000001", direct=d, proxy=p)
assert _r6 is not None and len(_r6) == 1, _r6
print("PASS 6 proxy-result-adopted")
print("ALL PASS")
