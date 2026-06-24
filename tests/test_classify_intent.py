"""拒绝意图识别(Issue #1)单测 —— 纯标准库 unittest。

覆盖:
- _parse_intent_json: JSON 解析鲁棒性(纯文本/```json 围栏/非法/越界 confidence)
- decide_rejection: 策略决策(明显拒绝/委婉拒绝/中性/低置信/禁用)
- classify_intent: 降级路径(无消息/未配置/API失败) + 正常解析(mock DeepSeek)

运行: python3 -m unittest tests.test_classify_intent  (或 pytest tests/)
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import chat_reply  # noqa: E402
from app.chat_reply import _parse_intent_json, decide_rejection, classify_intent  # noqa: E402


class TestParseIntentJson(unittest.TestCase):
    def test_plain_json(self):
        r = _parse_intent_json('{"intent":"reject_explicit","confidence":0.9,"reason":"已入职"}')
        self.assertEqual(r["intent"], "reject_explicit")
        self.assertEqual(r["confidence"], 0.9)
        self.assertEqual(r["reason"], "已入职")

    def test_json_in_code_fence(self):
        content = '这是判断结果：\n```json\n{"intent":"interested","confidence":0.7,"reason":"在问薪资"}\n```'
        r = _parse_intent_json(content)
        self.assertEqual(r["intent"], "interested")
        self.assertEqual(r["confidence"], 0.7)

    def test_malformed_falls_back_unknown(self):
        self.assertEqual(_parse_intent_json("不是json啊")["intent"], "unknown")
        self.assertEqual(_parse_intent_json("")["intent"], "unknown")
        self.assertEqual(_parse_intent_json("{坏的")["intent"], "unknown")

    def test_invalid_intent_value_to_unknown(self):
        r = _parse_intent_json('{"intent":"很想去","confidence":0.5}')
        self.assertEqual(r["intent"], "unknown")

    def test_confidence_clamped_and_coerced(self):
        self.assertEqual(_parse_intent_json('{"intent":"neutral","confidence":5}')["confidence"], 1.0)
        self.assertEqual(_parse_intent_json('{"intent":"neutral","confidence":-2}')["confidence"], 0.0)
        # 非数字 confidence → 0.0，不抛异常
        self.assertEqual(_parse_intent_json('{"intent":"neutral","confidence":"高"}')["confidence"], 0.0)


class TestDecideRejection(unittest.TestCase):
    def test_explicit_high_conf_marks(self):
        d = decide_rejection({"intent": "reject_explicit", "confidence": 0.9, "reason": "已找到工作"})
        self.assertEqual(d["action"], "mark")
        self.assertIn("已找到工作", d["reason"])

    def test_explicit_low_conf_does_not_mark(self):
        # 置信度低于阈值 → 不标记，照常回复(防误杀)
        d = decide_rejection({"intent": "reject_explicit", "confidence": 0.5, "reason": "?"})
        self.assertEqual(d["action"], "reply")

    def test_soft_default_stop_not_mark(self):
        # 委婉拒绝默认只停止追问、不标记(已确认的最保守策略)
        d = decide_rejection({"intent": "reject_soft", "confidence": 0.9, "reason": "再看看"})
        self.assertEqual(d["action"], "stop")

    def test_soft_can_be_configured_to_mark(self):
        d = decide_rejection(
            {"intent": "reject_soft", "confidence": 0.9, "reason": "考虑下"},
            policy={"soft_action": "mark", "min_confidence": 0.8},
        )
        self.assertEqual(d["action"], "mark")

    def test_soft_ignore_replies(self):
        d = decide_rejection(
            {"intent": "reject_soft", "confidence": 0.9, "reason": "x"},
            policy={"soft_action": "ignore"},
        )
        self.assertEqual(d["action"], "reply")

    def test_interested_and_neutral_reply(self):
        self.assertEqual(decide_rejection({"intent": "interested", "confidence": 0.9})["action"], "reply")
        self.assertEqual(decide_rejection({"intent": "neutral", "confidence": 0.9})["action"], "reply")
        self.assertEqual(decide_rejection({"intent": "unknown", "confidence": 0.0})["action"], "reply")

    def test_disabled_policy_always_reply(self):
        d = decide_rejection({"intent": "reject_explicit", "confidence": 1.0}, policy={"enabled": False})
        self.assertEqual(d["action"], "reply")


class TestClassifyIntentDegradation(unittest.TestCase):
    def test_empty_message_unknown_no_api(self):
        # 空消息直接 unknown，不应调用 API
        with mock.patch.object(chat_reply, "_post_deepseek") as m:
            r = classify_intent("   ")
            self.assertEqual(r["intent"], "unknown")
            m.assert_not_called()

    def test_no_api_key_unknown(self):
        with mock.patch.object(chat_reply, "_get_deepseek_config",
                               return_value={"api_key": "", "base_url": "x", "model": "y"}):
            r = classify_intent("你好")
            self.assertEqual(r["intent"], "unknown")

    def test_api_failure_unknown(self):
        with mock.patch.object(chat_reply, "_get_deepseek_config",
                               return_value={"api_key": "k", "base_url": "http://x", "model": "y"}), \
             mock.patch.object(chat_reply, "_post_deepseek", return_value=(None, "HTTP 500")):
            r = classify_intent("我考虑一下")
            self.assertEqual(r["intent"], "unknown")

    def test_normal_classification(self):
        fake = {"choices": [{"message": {"content": '{"intent":"reject_explicit","confidence":0.95,"reason":"已入职"}'}}]}
        with mock.patch.object(chat_reply, "_get_deepseek_config",
                               return_value={"api_key": "k", "base_url": "http://x", "model": "y"}), \
             mock.patch.object(chat_reply, "_post_deepseek", return_value=(fake, "")):
            r = classify_intent("谢谢，我已经入职了")
            self.assertEqual(r["intent"], "reject_explicit")
            self.assertGreaterEqual(r["confidence"], 0.9)


if __name__ == "__main__":
    unittest.main()
