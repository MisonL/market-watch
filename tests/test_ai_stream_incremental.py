#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stream-json 事件流必须**增量**扫描，不能每块重扫全量（O(N²)）。

## 为什么（2026-11-24 实测）

`run_with_idle_watchdog` 的 pump 线程原先每收到一块 stdout 就做两件事：

    _all = b"".join(bufs[1])          # ① 把已累积的整个流重新拼一遍
    _kind = _ai_stream_kind(_all)     # ② 再从头 split + 逐行扫一遍

而 read1 的块很小（一条 JSONL 几十~几百字节），同样字节被扫了 N 次。
用**思考密集流**（thinking_delta 占绝大多数，扫描不会早退 —— 这是生产
真实形态）量出来的纯解析耗时：

    累计 0.16MB ->  0.07s
    累计 1.5MB  ->  6.8s
    累计 2.3MB  -> 17.3s
    累计 3.0MB  -> 28.7s

生产 2026-11-24 09:19 那轮的事件流是 **12.4MB** —— 外推是**几分钟**纯 CPU。

危害不只是慢：pump 是子进程 stdout 的**唯一读者**，它一忙，64KB 管道缓冲
就填满，子进程 write 阻塞、模型输出跟着停住。于是「模型慢」和「读得慢」
互相放大，最后 AI 撞上单轮预算被掐停 —— 而日志只会说「AI 自己跑太久」。

## 本文件锁住什么

`_StreamScanner` 与 `_ai_stream_kind` **语义必须完全一致**（后者是旧实现的
判据，多个检查项依赖它），只是把「每次重扫全量」换成「每次只扫新行」：

  ① 非空正文才算 text（`"text":""` 的空载荷不能解除闸门）
  ② 跨块被 read1 拆开的行要能认出来（半行由 `_rest` 承接）
  ③ 没凑出完整行时只留尾巴，绝不再扫全量
  ④ 判定 text 之后立即停止扫描（正文出现，闸门已解除，再扫无收益）
  ⑤ **性能断言**：喂 N 块的总耗时必须近似线性，不能是 N²

⑤ 是这份文件的核心 —— 前四条即使全过，只要实现退化成"每块重扫"，
功能仍然正确而性能又回到 O(N²)。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M

_ok = 0


def check(cond, msg):
    global _ok
    if cond:
        _ok += 1
    else:
        print(f"  ❌ {msg}")
        sys.exit(1)


def _line(delta_type, text, key="text"):
    import json
    # ⚠️ **必须紧凑分隔符**：CLI 真实输出是 `"text":"..."`（无空格），
    # 而 `_AI_TEXT_PAYLOAD` 判的正是 `b'"text":"'`。用 json.dumps 默认的
    # `", "` / `": "` 会生成 `"text": "..."`，骨架**永远命中不了** ——
    # 第一版就是这么错的：测试自己造了不存在的格式，然后声称实现有问题。
    return json.dumps({"type": "content_block_delta",
                       "delta": {"type": delta_type, key: text}},
                      ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8") + b"\n"


# ---------- ①空载荷不算正文 ----------
_s = M._StreamScanner()
_s.feed(_line("text_delta", ""))
check(_s.kind == "none",
      f"空 text 增量 `\"text\":\"\"` 不能算作正文（会解除全部闸门），"
      f"实得 kind={_s.kind!r}")

# ---------- ②非空正文算正文，且记下时刻 ----------
_s = M._StreamScanner()
_s.feed(_line("text_delta", "上证 3538.95"))
check(_s.kind == "text", f"非空正文应判 text，实得 {_s.kind!r}")
check(_s._text_at is not None, "判定正文时必须记下时刻（_text_ts 要用）")

# ---------- ③thinking 流判 thinking，不解除正文闸门 ----------
_s = M._StreamScanner()
_s.feed(_line("thinking_delta", "让我想想", key="thinking"))
check(_s.kind == "thinking",
      f"只有思考时应判 thinking（正文闸门不解除），实得 {_s.kind!r}")
check(_s._text_at is None, "只有思考时不能记正文时刻")

# ---------- ④跨块被拆开的行必须能认出来 ----------
# read1 会把一行切成两块。原实现的承诺是"下一轮扫完整缓冲自然会命中"，
# 增量版必须用 _rest 承接半行，否则这条判据会**静默失效**。
_s = M._StreamScanner()
_full = _line("text_delta", "创业板指涨超 3%")
_cut = len(_full) // 2
_s.feed(_full[:_cut])
check(_s.kind == "none", "半行时不该提前判定（还没凑出完整行）")
_s.feed(_full[_cut:])
check(_s.kind == "text",
      f"分两块喂入的同一行必须能认出正文，实得 {_s.kind!r}")

# ---------- ⑤没凑出完整行时只留尾巴 ----------
_s = M._StreamScanner()
_s.feed(b'{"type":"content_block_delta","delta":{"type":"text_delta","te')
check(len(_s._rest) <= M._STREAM_SCAN_REST_MAX,
      "半行缓冲超过上限时必须截尾（否则异常流会把内存吃光）")
_s.feed('xt":"补齐了"}}\n'.encode("utf-8"))
check(_s.kind == "text", "续行补齐后应能认出正文")

# ---------- ⑤b 完整行之后的长尾巴也要截 ----------
# 上一例走的是"连完整行都没有"的分支；这条走"有完整行、尾巴超长"的分支。
# 两处截尾是**独立的**，只测一处时删掉另一处照样全绿（实测过）。
_s = M._StreamScanner()
_s.feed(_line("thinking_delta", "短", key="thinking")
        + b"z" * (M._STREAM_SCAN_REST_MAX + 5000))
check(len(_s._rest) <= M._STREAM_SCAN_REST_MAX,
      f"完整行之后的长尾巴必须截断到上限，实得 {len(_s._rest)} 字节"
      f"（上限 {M._STREAM_SCAN_REST_MAX}）—— 否则无换行的异常流会把内存吃光")

# ---------- ⑤c 完全没有换行的超长块也要截 ----------
# 第三条分支：`_cut < 0`（一个完整行都没有）。三处截尾互相独立，
# 缺任何一处都会留下一类"能撑爆内存"的输入形态。
_s = M._StreamScanner()
_s.feed(b"q" * (M._STREAM_SCAN_REST_MAX + 5000))
check(len(_s._rest) <= M._STREAM_SCAN_REST_MAX,
      f"无换行的超长块必须截断，实得 {len(_s._rest)} 字节"
      f"（上限 {M._STREAM_SCAN_REST_MAX}）")

# ---------- ⑥与 _ai_stream_kind 语义一致（同一输入同一结论） ----------
_cases = [
    b"",
    _line("thinking_delta", "想", key="thinking"),
    _line("text_delta", ""),
    _line("text_delta", "正文"),
    _line("thinking_delta", "想", key="thinking") + _line("text_delta", ""),
]
for _buf in _cases:
    _ref = M._ai_stream_kind(_buf)
    _sc = M._StreamScanner()
    _sc.feed(_buf)
    _got = _sc.kind
    # `_ai_stream_kind` 没有 "none" 之外的差异：空 -> none
    check(_got == _ref,
          f"与 `_ai_stream_kind` 结论不一致：输入 {_buf[:50]!r} "
          f"-> 增量版 {_got!r} / 参考版 {_ref!r}")

# ---------- ⑦内容标记（首字节闸门） ----------
_s = M._StreamScanner()
check(not _s.saw_content, "初始状态不该有 content 标记")
_s.feed(b'{"type":"assistant","message":{}}\n')
check(_s.saw_content, "assistant 事件必须让 `saw_content` 置位"
                      "（首字节闸门靠它解除）")

# ---------- ⑧判定 text 后不再扫描（省 CPU，且不影响结果） ----------
_s = M._StreamScanner()
_s.feed(_line("text_delta", "第一条正文"))
_first_kind = _s.kind
_s.feed(_line("thinking_delta", "之后的思考", key="thinking"))
check(_s.kind == _first_kind == "text",
      "判定 text 后 kind 不该被后续 thinking 覆盖")
check(_s.feed(_line("text_delta", "更多正文")) is False,
      "判定 text 之后再喂数据应返回 False（已停止扫描）")

# ---------- ⑨【核心】性能必须近似线性，不能 O(N²) ----------
# 用思考密集流（不早退，与生产形态一致）。判定标准：
#   · 2 倍数据量的耗时不得超过 3 倍（O(N²) 会是 4 倍）
#   · 且绝对耗时必须在秒级以内
def _feed_mb(mb):
    _sc = M._StreamScanner()
    _chunk = b"".join(_line("thinking_delta", "y" * 300, key="thinking")
                      for _ in range(4))          # ~1.5KB
    _n = int(mb * 1024 * 1024) // len(_chunk)
    _t0 = time.monotonic()
    for _ in range(_n):
        _sc.feed(_chunk)
    return time.monotonic() - _t0


_t1 = _feed_mb(1.0)
_t2 = _feed_mb(2.0)
check(_t2 < max(_t1 * 3.0, 0.02),
      f"扫描耗时随数据量超线性增长（1MB={_t1:.3f}s, 2MB={_t2:.3f}s）"
      f" —— 增量扫描退回到了 O(N²) 全量重扫")
check(_t2 < 2.0,
      f"2MB 事件流解析耗时 {_t2:.2f}s 过慢 —— 生产 12.4MB 的流会烧掉分钟级 CPU")

# ---------- ⑩接线断言：pump 必须真的用增量版 ----------
_src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "..", "src", "marketlib.py"), encoding="utf-8").read()
check("_StreamScanner()" in _src and _src.count("_StreamScanner") >= 2,
      "`_StreamScanner` 只有定义、没有实例化 —— 增量扫描没接进 pump")
check("_sc.feed(chunk)" in _src,
      "pump 没有把**新块**喂给扫描器")
# ⚠️ 这条防的是"接了一半"：扫描器建了，但旧的 O(N²) 重扫还留着。
# 只要 `_kind = _ai_stream_kind(b"".join(...))` 还在，性能就没救。
check('_kind = _ai_stream_kind(b"".join(bufs[1]))' not in _src,
      "pump 里仍残留 `_ai_stream_kind(b\"\".join(bufs[1]))` 全量重扫 —— "
      "增量版接了，但旧的 O(N²) 路径没拆掉")

print(f"ALL PASS (增量事件流扫描 {_ok} 个断言)")
