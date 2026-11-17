"""快讯双源：缺列表要降级，「两级都挂」不能伪装成「源正常但无快讯」。

`[Codex 审查复现]` 两个缺陷：
① 东财回 `{"data": {}}`（`fastNewsList` 缺失）时被当成**合法形状**，
   归一成 [] 并缓存 120 秒 —— 新浪调用次数 = 0，尽管新浪有新闻。
② `news_sina` 失败时返回 **[] 而不是抛异常**，于是"两级源都挂"与
   "源正常但今天没快讯"在缓存里同值，空结果被缓存 120 秒 ——
   而源故障恰恰是最需要下一轮重试的时刻。

契约：
  ① 缺 `fastNewsList` → 视为源故障，去试新浪；
  ② 两级都拿不到 → 返回 None（不构成有效缓存，下轮重试）；
  ③ 东财正常有数据 → 不碰新浪。
"""
import os
import sys
import tempfile

os.environ["MW_DIR"] = tempfile.mkdtemp(prefix="mw_news_")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import news

bad = 0


def check(cond, msg):
    global bad
    if not cond:
        bad += 1
        print(f"  ❌ {msg}")


def _reset():
    news._NEWS_MEM.update({"ts": 0.0, "limit": 0, "data": None,
                           "unavailable": False})


_sina_calls = []


def _fake_sina(limit=100, within_min=240):
    _sina_calls.append(limit)
    return [{"title": "新浪快讯A", "time": "10:00", "url": ""}]


# ---------- ① 缺 fastNewsList → 必须降级到新浪 ----------
_reset()
_sina_calls.clear()
news.curl_json = lambda url, ref=None: {"data": {}}
import marketlib
_orig_sina = marketlib.news_sina
marketlib.news_sina = _fake_sina
try:
    out = news.fetch_news(10)
finally:
    marketlib.news_sina = _orig_sina
check(len(_sina_calls) >= 1,
      f"东财缺 fastNewsList 时没去试新浪（调用 {len(_sina_calls)} 次）")
check(isinstance(out, list) and out,
      f"应拿到新浪的快讯，实际 {type(out).__name__}={out!r}")

# ---------- ② 两级都挂 → None（不当健康空缓存） ----------
_reset()
_sina_calls.clear()
news.curl_json = lambda url, ref=None: {"data": {}}


def _fail_sina(limit=100, within_min=240):
    return []          # 失败时返回 [] 而非抛异常（这就是原缺陷的形态）


marketlib.news_sina = _fail_sina
try:
    out2 = news.fetch_news(10)
finally:
    marketlib.news_sina = _orig_sina
check(out2 is None,
      f"两级源都挂时返回 {out2!r}，应为 None（否则会被当健康空缓存 120s）")
check(news._NEWS_MEM.get("data") is None,
      "两级源失败却写入了有效缓存")

# ---------- ③ 东财正常 → 不碰新浪 ----------
_reset()
_sina_calls.clear()
news.curl_json = lambda url, ref=None: {
    "data": {"fastNewsList": [{"code": "202611100001", "showTime": "2026-11-10 10:00:00",
                               "title": "东财快讯X", "summary": "摘要",
                               "url": ""}]}}
marketlib.news_sina = _fake_sina
try:
    out3 = news.fetch_news(10)
finally:
    marketlib.news_sina = _orig_sina
check(out3 and out3[0].get("title") == "东财快讯X",
      f"东财正常时结果不对: {out3!r}")
check(len(_sina_calls) == 0,
      f"东财正常时仍去打了新浪（{len(_sina_calls)} 次）")

assert bad == 0, f"{bad} 处不符合预期"
print("ALL PASS (缺列表降级；双源失败返回 None；正常不碰新浪)")
