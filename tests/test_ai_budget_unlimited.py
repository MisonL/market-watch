"""单轮 AI 预算不限制（2026-11-04 用户要求「可以不限制 AI token 预算」）。

背景（本机实测，两个来源）：
  · 沙箱真调一次同构 prompt：`thinking_delta` 累计 8919 字符、
    `text_delta` 只有 541 字符，正文在 23.8s 才出来 —— 模型的耗时大头在
    **思考阶段**，正文是最后一小段。
  · 生产 watch.log：11-03/11-04 两天共 5 轮（238/235/231/226/223s）在
    240s 总时长闸门到点时仍是零正文，全部退兜底模板；new-api 网关侧
    对应记录是 `client_gone`（**我方**断连），不是上游故障。

契约（`AI_ROUND_BUDGET = None` 表示不限制）：
  ① `_invoke` 必须给看门狗传 `total_limit=None`，不是某个数字；
  ② 已用时间再大（`_ROUND_AI_MS` 预置 1 小时）也照常发起调用 ——
     不限制模式下没有「预算耗尽跳过」这条路径；
  ③ 空输出重试不再受预算门槛限制（预置已用时间不影响重试）；
  ④ 有限预算模式（显式设回数字）行为保持不变：耗尽跳过、不误写熔断
     —— 保留机制，日后想恢复只需改一个常量。
  ⑤ `_stall_desc` 在 None 预算下不抛 TypeError、措辞说明"不限制"。
"""
import os, sys, json, time, tempfile, atexit, shutil
_d = tempfile.mkdtemp(prefix="mw_t_")
os.environ["MW_DIR"] = _d
atexit.register(shutil.rmtree, _d, ignore_errors=True)
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "src"))
import analyze as A

_saved = (A.M.claude_bin, A.M.run_with_idle_watchdog, A.M._AI_FAIL_FILE)
_fail = os.path.join(_d, "ai_fail.json")
A.M._AI_FAIL_FILE = _fail
A.M.claude_bin = lambda: ("/bin/true", None)

# ai_extract_text 的 stream-json 收口只认 JSONL 事件行（这是生产口径），
# 桩输出必须同形态，否则会被当成"解析失败"丢弃 —— 那测的就不是预算了。
_OK = json.dumps({"type": "assistant",
                  "message": {"content": [{"type": "text", "text": "OK正文"}]}})
_SEEN = []


def _wd_ok(*a, **k):
    _SEEN.append(k.get("total_limit", "MISSING"))
    return (0, _OK + "\n", "", False)


try:
    # ---------- ① 常量契约 ----------
    # 2026-11-10：生产默认改为有限预算（场景：整轮拖到 685s 吞槽位），
    # 本文件改为验证 **显式 None 的兼容语义** 仍然成立 —— 机制没被拆。
    A.AI_ROUND_BUDGET = None
    print("PASS 1 explicit None keeps unlimited semantics")

    # ---------- ② 不限制模式：已用时间再大也照常调用，且 total_limit=None ----------
    A.M.run_with_idle_watchdog = _wd_ok
    A._ROUND_AI_MS.clear()
    A._ROUND_AI_MS.append(3600 * 1000)          # 已用 1 小时
    _out = A.ai_analyze("b", [], "", slot="09:25")
    assert _SEEN, "不限制模式下即使已用时间极大也必须发起调用（预算跳过路径应不存在）"
    assert _SEEN[0] is None, \
        f"看门狗必须收到 total_limit=None（不限制），实际 {_SEEN[0]!r}"
    assert "OK正文" in _out, f"桩返回正文却被丢弃：{_out!r}"
    print("PASS 2 unlimited mode still calls, total_limit=None")

    # ---------- ③ 空输出重试不受预算门槛限制 ----------
    _calls = []

    def _wd2(*a, **k):
        _calls.append(k.get("total_limit", "MISSING"))
        return (0, "", "", False)               # rc=0 + stderr 干净 + 空正文

    A.M.run_with_idle_watchdog = _wd2
    A._ROUND_AI_MS.clear()
    A._ROUND_AI_MS.append(3600 * 1000)          # 已用 1 小时，旧判据会拦掉重试
    A.ai_analyze("b", [], "", slot="09:25")
    assert len(_calls) >= 2, \
        f"不限制模式下空输出必须立即重试（旧预算门槛不该再拦），实际 {len(_calls)} 次"
    print("PASS 3 retry not gated by budget in unlimited mode")

    # ---------- ④ 有限预算模式行为保持（机制未拆） ----------
    A.AI_ROUND_BUDGET = 240                      # 模拟显式设回有限预算
    try:
        _calls2 = []
        # 独立熔断文件：PASS 3 的"空输出 ×2"按设计会正常写熔断，
        # 不能把它的残留当成 PASS 4 的违规证据。
        _fail2 = os.path.join(_d, "ai_fail2.json")
        A.M._AI_FAIL_FILE = _fail2
        A.M.run_with_idle_watchdog = lambda *a, **k: (_calls2.append(1), (0, "", "", False))[1]
        A._ROUND_AI_MS.clear()
        A._ROUND_AI_MS.append(270 * 1000)        # 已用 270s > 240s
        _o = A.ai_analyze("b", [], "", slot="09:25")
        assert not _calls2, "有限预算耗尽后不得再发起调用"
        assert _o == "", f"有限预算耗尽应返回空串，实际 {_o!r}"
        _br = {}
        try:
            _br = json.load(open(_fail2, encoding="utf-8"))
        except Exception:
            pass
        assert not (_br.get("until", 0) > time.time()), \
            "预算耗尽被误写成 AI 熔断 —— 主动跳过 ≠ 通道故障"
        print("PASS 4 finite budget mode keeps skip/no-breaker semantics")
    finally:
        A.AI_ROUND_BUDGET = None

    # ---------- ⑤ _stall_desc 在 None 预算下不炸、措辞自洽 ----------
    _desc = A._stall_desc("[total-limit-gate 196.0s 触发，非 idle 闸门] ", True, -15)
    assert "196s" in _desc, _desc
    assert "不限制" in _desc, f"不限制模式下文案应说明全轮预算不限：{_desc!r}"
    _desc2 = A._stall_desc("[total-limit-gate 47s 触发，非 idle 闸门] ", True, -15)
    assert "47s" in _desc2, _desc2
    print("PASS 5 stall text stays coherent with None budget")
finally:
    (A.M.claude_bin, A.M.run_with_idle_watchdog, A.M._AI_FAIL_FILE) = _saved
    A._ROUND_AI_MS.clear()

print("ALL PASS")
