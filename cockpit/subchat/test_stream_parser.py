#!/usr/bin/env python3
"""Unit tests for ``stream_parser.py``.

Shapes mirror real claude stream-json events (sampled from the archived
runs at ``WIP_experiment_with_CLI/.../baseline/01/stream.ndjson``).

Run from this directory:
    python3 -m unittest test_stream_parser.py -v
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import stream_parser as sp  # noqa: E402


def _drive(state: sp.StreamParseResult, lines: list[str]) -> tuple[list[str], list[str]]:
    tool_events: list[str] = []
    assistant_events: list[str] = []
    for line in lines:
        sp.parse_line(line, state, tool_events.append, assistant_events.append)
    return tool_events, assistant_events


class TestBasicEmission(unittest.TestCase):

    def test_emits_tool_event_for_tool_use_block(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "ToolSearch",
                     "input": {}, "index": 0},
                ],
            },
        })
        state = sp.StreamParseResult()
        tools, assistants = _drive(state, [line])
        self.assertEqual(tools, ["ToolSearch"])
        self.assertEqual(assistants, [])

    def test_emits_assistant_text_truncated_at_80_chars(self):
        long_text = "x" * 200
        line = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [{"type": "text", "text": long_text, "index": 0}],
            },
        })
        state = sp.StreamParseResult()
        _, assistants = _drive(state, [line])
        self.assertEqual(len(assistants), 1)
        self.assertTrue(assistants[0].endswith("..."))
        # 80 char prefix + "..." → 83 total
        self.assertEqual(len(assistants[0]), sp.ASSISTANT_SNIPPET_LEN + 3)

    def test_short_assistant_text_no_ellipsis(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [{"type": "text", "text": "hello", "index": 0}],
            },
        })
        state = sp.StreamParseResult()
        _, assistants = _drive(state, [line])
        self.assertEqual(assistants, ["hello"])


class TestNoDoubleEmit(unittest.TestCase):

    def test_repeated_checkpoint_does_not_re_emit_same_block(self):
        # Same message, same block index — emit once across many checkpoints.
        chk = lambda blocks: json.dumps({
            "type": "assistant",
            "message": {"id": "msg_X", "content": blocks},
        })
        b1 = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}, "index": 0}
        state = sp.StreamParseResult()
        tools, _ = _drive(state, [chk([b1]), chk([b1]), chk([b1])])
        self.assertEqual(tools, ["Read"])

    def test_new_block_appended_in_later_checkpoint_emits_once(self):
        b0 = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}, "index": 0}
        b1 = {"type": "tool_use", "id": "t2", "name": "Bash", "input": {}, "index": 1}
        msg1 = json.dumps({"type": "assistant",
                           "message": {"id": "M", "content": [b0]}})
        msg2 = json.dumps({"type": "assistant",
                           "message": {"id": "M", "content": [b0, b1]}})
        state = sp.StreamParseResult()
        tools, _ = _drive(state, [msg1, msg2])
        self.assertEqual(tools, ["Read", "Bash"])

    def test_new_message_id_resets_index_space(self):
        # Per spec: content_block index resets to 0 on every new message_start.
        # Two different message_ids → block 0 of each is a distinct slot.
        b = {"type": "tool_use", "name": "Read", "id": "t", "input": {}, "index": 0}
        m1 = json.dumps({"type": "assistant",
                         "message": {"id": "msg_A", "content": [b]}})
        m2 = json.dumps({"type": "assistant",
                         "message": {"id": "msg_B", "content": [b]}})
        state = sp.StreamParseResult()
        tools, _ = _drive(state, [m1, m2])
        self.assertEqual(tools, ["Read", "Read"])


class TestThinkingNotSurfaced(unittest.TestCase):

    def test_thinking_blocks_are_silent(self):
        line = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {"type": "thinking", "thinking": "internal", "index": 0},
                ],
            },
        })
        state = sp.StreamParseResult()
        tools, assistants = _drive(state, [line])
        self.assertEqual(tools, [])
        self.assertEqual(assistants, [])


class TestSystemAndResult(unittest.TestCase):

    def test_system_init_captures_session_id(self):
        line = json.dumps({
            "type": "system",
            "subtype": "init",
            "session_id": "sid-from-init",
        })
        state = sp.StreamParseResult()
        _drive(state, [line])
        self.assertEqual(state.session_id, "sid-from-init")

    def test_result_event_captures_response_and_session(self):
        line = json.dumps({
            "type": "result",
            "subtype": "success",
            "result": "the final reply",
            "session_id": "sid-from-result",
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        })
        state = sp.StreamParseResult()
        _drive(state, [line])
        self.assertEqual(state.response_text, "the final reply")
        self.assertEqual(state.session_id, "sid-from-result")
        self.assertIsNotNone(state.result_event)


class TestRobustness(unittest.TestCase):

    def test_malformed_lines_counted_not_raised(self):
        state = sp.StreamParseResult()
        _drive(state, [
            "not-json",
            "",
            "{not-closed",
            json.dumps({"type": "system", "subtype": "init",
                        "session_id": "ok"}),
        ])
        self.assertEqual(state.malformed_lines, 2)  # blank line skipped
        self.assertEqual(state.session_id, "ok")
        self.assertEqual(len(state.events), 1)

    def test_assistant_event_without_index_uses_positional(self):
        # Real CLI events sometimes omit the explicit "index" field on
        # the checkpoint copy of message.content; falling back to position
        # in the list keeps emission stable.
        b0 = {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}
        b1 = {"type": "text", "text": "hello world"}
        line = json.dumps({
            "type": "assistant",
            "message": {"id": "M", "content": [b0, b1]},
        })
        state = sp.StreamParseResult()
        tools, assistants = _drive(state, [line])
        self.assertEqual(tools, ["Read"])
        self.assertEqual(assistants, ["hello world"])


if __name__ == "__main__":
    unittest.main()
