"""空 text 事件不得解除「无正文」闸门 —— 2026-11-06 全量复核 P1-11。

`marketlib.ai_analyze` 的看门狗有四道闸门：

  ① idle（连续无字节）        —— 由任何 stdout 字节刷新
  ② 首字节（150s 无 content）  —— `_content_ts` 一置上就解除
  ③ 无正文（600s 只有 thinking）—— `_text_ts` 一置上就解除
  ④ 总时长                   —— `AI_ROUND_BUDGET=None` 时**关闭**

③ 的判据是 `_ai_stream_kind(buf) == "text"`。而原实现只要某行出现
`"type":"text"` / `"type":"text_delta"` 就返回 text —— **空载荷也算**：

    {"type":"content_block_delta","delta":{"type":"text_delta","text":""}}

这一行同时做三件事：置 `_content_ts`（解除 ②）、置 `_text_ts`（解除 ③）、
刷新 idle（解除 ①）。加上 ④ 本来就关着 —— **四道闸门同时失效**，
一个持续吐空增量的流可以永远占着调度周期，把后续每个 300s 槽位吃掉。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_empty_text_gate.py`
"""
import os, sys, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M

# 各种形态的 stdout 事件行
_EMPTY_DELTA = b'{"type":"content_block_delta","delta":{"type":"text_delta","text":""}}\n'
_BLANK_DELTA = b'{"type":"content_block_delta","delta":{"type":"text_delta","text":"   "}}\n'
_REAL_DELTA = ('{"type":"content_block_delta","delta":{"type":"text_delta",'
               '"text":"涨"}}\n').encode("utf-8")
_START_EMPTY = (b'{"type":"content_block_start","content_block":'
                b'{"type":"text","text":""}}\n')
_THINK = (b'{"type":"content_block_delta","delta":'
          b'{"type":"thinking_delta","thinking":"abc"}}\n')


def check():
    # ① 空载荷一律不得判成 text —— 这是本修复的核心
    for name, buf in (("空 text_delta", _EMPTY_DELTA),
                      ("纯空白 text_delta", _BLANK_DELTA),
                      ("content_block_start 空", _START_EMPTY)):
        got = M._ai_stream_kind(buf)
        assert got != "text", f"{name} 不得判成 text（会解除无正文闸门），实得 {got!r}"

    # ② 真有正文必须照常判 text —— 闸门不能被关过头，否则合法的慢调用会被误杀
    got = M._ai_stream_kind(_REAL_DELTA)
    assert got == "text", f"非空正文必须判 text，实得 {got!r}"

    # ③ 先空后有正文：累积缓冲里只要出现真正文就该判 text
    got = M._ai_stream_kind(_EMPTY_DELTA * 3 + _REAL_DELTA)
    assert got == "text", f"空增量之后出现正文应判 text，实得 {got!r}"

    # ④ 纯 thinking 仍判 thinking（既有语义不得回归）
    assert M._ai_stream_kind(_THINK) == "thinking"
    assert M._ai_stream_kind(b"") == "none"

    # ⑤ 行被 read1 截断（有标记、没收尾引号）时**不得**误认正文 ——
    # 下一轮扫完整缓冲自然会命中，此刻认了就等于给闸门开了个后门。
    truncated = b'{"delta":{"type":"text_delta","text":"\xe6'
    assert M._ai_stream_kind(truncated) != "text", (
        "截断行不得判成 text")

    # ⑥ 大量空增量后总量判据仍不是 text（模拟"永远吐空"的流）
    flood = _EMPTY_DELTA * 200
    assert M._ai_stream_kind(flood) != "text", (
        "持续空增量不得解除闸门 —— 这正是 P1-11 的形态")

    print("ALL PASS (空 text 闸门 9 个断言)")


if __name__ == "__main__":
    check()
