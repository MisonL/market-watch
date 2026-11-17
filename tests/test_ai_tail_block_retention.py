"""完整 text 块存在时，尾部未完成块的正文不得被丢弃 —— 2026-11-07 复核 F12。

`ai_extract_text()` 原实现：

```python
if _chunks:            # 只要有**任何**完整块
    return "".join(_chunks)    # 就整体忽略 _deltas
```

于是下面这个事件流（真实形态：进程被 SIGTERM / 输出被截断）会丢正文：

    assistant 消息，块 0 完整输出「第一句。」
    content_block_start(index=1) + text_delta「第二句。」
    —— 没有后续 assistant 块 1（被杀）

只得到「第一句。」，而「第二句。」**已经流出来了却被丢掉**。
「截断时保留已产出正文」正是该函数的契约（`_res_err` 的两处注释都在讲
同一条原则：正文优先于错误串）。而 analyze 侧一旦拿不到可用正文，
会多走一次重写或退回模板 —— 白付一整轮 AI 调用。

修法：按 `index` 把 delta 分组，只对「已经包含在完整块里」的组去重，
尚未完成的组按原顺序补在尾部；缺 `index` 的旧式事件归组 0（与改前一致）。

关于「怎么判断某组是否已完成」：一个块到底完成没有，仅靠序数测不准
（块 0 有完整块、块 1 只有 delta 是最常见的形态）。所以取**安全边**：
只要某组的正文不是已完成部分的尾部（`in` / `endswith` 两种形态），
就补上。这不会把已收到的内容重复输出 —— 重复正文比缺正文更容易被发现，
但也更容易误导读者。

跑法：`MW_DIR=$(mktemp -d) python3 tests/test_ai_tail_block_retention.py`
"""
import os, sys, json, tempfile, atexit, shutil

if "MW_DIR" not in os.environ:
    _td = tempfile.mkdtemp(prefix="mw_t_")
    os.environ["MW_DIR"] = _td
    atexit.register(shutil.rmtree, _td, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))

import marketlib as M


def _raw(evs):
    return "\n".join(json.dumps(e, ensure_ascii=False) for e in evs)


def _assist(txt):
    return {"type": "assistant",
            "message": {"content": [{"type": "text", "text": txt}]}}


def _bstart(idx):
    return {"type": "stream_event",
            "event": {"type": "content_block_start", "index": idx,
                      "content_block": {"type": "text", "text": ""}}}


def _delta(txt, idx=0):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta", "index": idx,
                      "delta": {"type": "text_delta", "text": txt}}}


def _think(txt, idx=0):
    return {"type": "stream_event",
            "event": {"type": "content_block_delta", "index": idx,
                      "delta": {"type": "thinking_delta", "thinking": txt}}}


def check():
    # ① F12 的原始复现：完整块 0 + 未完成块 1 的 delta
    assert M.ai_extract_text(_raw([_assist("第一句。"),
                                   _bstart(1), _delta("第二句。", 1)])) == \
        "第一句。第二句。", "尾部未完成块的正文不得被丢弃"

    # ② 去重：同一块的 delta 已经体现在完整块里，不得重复输出
    assert M.ai_extract_text(_raw([_assist("第一句。"),
                                   _bstart(0), _delta("第一句。", 0)])) == \
        "第一句。", "已包含在完整块里的 delta 不得重复追加"

    # ②b 时序判据：delta 落在完整块**之前** → 是同一块的早期分片，
    #     最终形态已由完整块给出，不得再补（否则正文翻倍）。
    #     这正是自检 `[AI 通道]` 探针的用例 —— 我第一版按"内容是否被包含"
    #     判断，把它补成了「乙甲」，直接让沙箱基线变红、全量回归中止。
    assert M.ai_extract_text(_raw([_bstart(0), _delta("甲", 0),
                                   _assist("乙")])) == "乙", (
        "完整块到达时必须丢弃此前累积的 delta —— 否则正文翻倍")

    # ②c 交错：块0 完整 → 块1 delta → 块1 完整（后到的完整块吞掉自己的 delta）
    assert M.ai_extract_text(_raw([_assist("甲"), _bstart(1), _delta("乙", 1),
                                   _assist("乙")])) == "甲乙", (
        "后到的完整块应吞掉它自己那组 delta，但不得吞掉前面的完整块")

    # ③ 没有完整块时，原有的 delta 兜底必须仍然工作
    assert M.ai_extract_text(_raw([_bstart(0), _delta("甲"), _delta("乙", 0)])) \
        == "甲乙"
    assert M.ai_extract_text(_raw([_bstart(0), _delta("甲", 0),
                                   _bstart(1), _delta("乙", 1)])) == "甲乙"

    # ④ 推理分片绝不能混进正文（改了分组逻辑后更要锁住）
    assert M.ai_extract_text(_raw([_bstart(0), _think("推理过程", 0),
                                   _delta("正文", 0)])) == "正文"

    # ⑤ 只有完整块时行为完全不变（最常见的正常路径）
    assert M.ai_extract_text(_raw([_assist("只有这一段。")])) == "只有这一段。"

    # ⑥ 全空白 delta 不得补出空串或空白尾
    assert M.ai_extract_text(_raw([_assist("第一句。"),
                                   _bstart(1), _delta("   ", 1)])) == "第一句。"

    # ⑦ 正文仍然优先于 error result（既有契约，不能因为改动而改变）
    _err = {"type": "result", "is_error": True, "result": "API Error: 503"}
    assert M.ai_extract_text(_raw([_assist("正文。"), _err])) == "正文。"
    assert M.ai_extract_text(_raw([_bstart(1), _delta("半句", 1), _err])) == "半句"

    # ⑧ 缺 index 的旧式事件归组 0，与改前行为一致
    _old = {"type": "stream_event",
            "event": {"type": "content_block_delta",
                      "delta": {"type": "text_delta", "text": "旧式"}}}
    assert M.ai_extract_text(_raw([_old])) == "旧式"

    print("ALL PASS (尾部块保留 10 个断言)")


if __name__ == "__main__":
    check()
