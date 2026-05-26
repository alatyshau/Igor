"""Parse claude's ``--output-format stream-json`` NDJSON stream and emit
subchat's ``[tool]`` / ``[assistant]`` progress events.

The CLI wraps each Claude API SSE event in a JSON object on its own line.
See ``WIP_experiment_with_CLI/stream-json-spec.md`` for the field-level
schema collected from observed runs. Relevant event types for live
progress attribution:

- ``assistant`` — a checkpoint emitted by the CLI carrying the assistant's
  current ``message.content`` so far. Multiple checkpoints arrive per
  turn; each later checkpoint **supersedes** the prior one for the same
  ``content[*].index`` slot.
- ``result`` — the final event of the run, carrying ``result`` (text),
  ``session_id``, ``duration_ms``, ``usage``, ``total_cost_usd``.

Emission rule (chosen to avoid duplicate spam):
  Track which ``content`` indices have already been surfaced. On every
  ``assistant`` event, iterate ``message.content``; for any block whose
  ``index`` (or positional index if missing) is unseen and whose payload
  is non-empty, emit:
    - ``[tool] <name>`` for ``type == "tool_use"`` blocks;
    - ``[assistant] <first 80 chars>...`` for ``type == "text"`` blocks
      with non-empty text.
  Mark that block as seen and skip it next time.

The parser is the source of truth for ``session_id`` and ``response.md``
when ``output-format == stream-json`` — the caller reads
:attr:`StreamParseResult.session_id` and :attr:`response_text` after
``parse_line()`` has been driven over the whole stream.

The collected event list (a Python list — ``output.json`` is one JSON
*array*, not NDJSON concatenation, per the spec's "stream-json vs json
output handling" rule) is exposed as :attr:`events`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

# Cap on the snippet emitted with ``[assistant]`` per spec ("first 80 chars").
ASSISTANT_SNIPPET_LEN = 80


@dataclass
class StreamParseResult:
    """Accumulator state for one claude stream-json run."""

    events: list[dict] = field(default_factory=list)
    session_id: str | None = None
    response_text: str | None = None
    result_event: dict | None = None
    # Indices already surfaced via [tool] / [assistant] across the run.
    # We key by (message_id_or_None, index) so a fresh message_start resets
    # the slot — content_block index resets to 0 on each new message.
    _seen_blocks: set[tuple[str | None, int]] = field(default_factory=set)
    _current_message_id: str | None = None
    # Bookkeeping for malformed-line counting; caller may surface a [warn].
    malformed_lines: int = 0


def parse_line(
    line: str,
    state: StreamParseResult,
    emit_tool: Callable[[str], None],
    emit_assistant: Callable[[str], None],
) -> None:
    """Process a single NDJSON line.

    Lines that are blank or not valid JSON increment ``state.malformed_lines``
    silently — surface the count once at the end so we do not spam during
    the stream.
    """
    text = line.strip()
    if not text:
        return
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        state.malformed_lines += 1
        return
    if not isinstance(obj, dict):
        state.malformed_lines += 1
        return
    state.events.append(obj)

    ev_type = obj.get("type")
    if ev_type == "assistant":
        _handle_assistant(obj, state, emit_tool, emit_assistant)
    elif ev_type == "result":
        state.result_event = obj
        result_text = obj.get("result")
        if isinstance(result_text, str):
            state.response_text = result_text
        sid = obj.get("session_id")
        if isinstance(sid, str):
            state.session_id = sid
    elif ev_type == "system" and obj.get("subtype") == "init":
        sid = obj.get("session_id")
        if isinstance(sid, str) and state.session_id is None:
            state.session_id = sid


def _handle_assistant(
    obj: dict,
    state: StreamParseResult,
    emit_tool: Callable[[str], None],
    emit_assistant: Callable[[str], None],
) -> None:
    msg = obj.get("message") or {}
    msg_id = msg.get("id") if isinstance(msg.get("id"), str) else None
    # Treat a different message_id as a fresh per-message index space.
    if msg_id != state._current_message_id:
        state._current_message_id = msg_id

    content = msg.get("content")
    if not isinstance(content, list):
        return
    for pos, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        # Prefer the explicit ``index`` field (Claude API uses it), fall
        # back to positional index. Both are stable within one message.
        idx = block.get("index")
        if not isinstance(idx, int):
            idx = pos
        key = (msg_id, idx)
        if key in state._seen_blocks:
            continue
        btype = block.get("type")
        if btype == "tool_use":
            name = block.get("name")
            if isinstance(name, str) and name:
                state._seen_blocks.add(key)
                emit_tool(name)
        elif btype == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                state._seen_blocks.add(key)
                snippet = text[:ASSISTANT_SNIPPET_LEN]
                if len(text) > ASSISTANT_SNIPPET_LEN:
                    snippet = snippet + "..."
                emit_assistant(snippet)
        # ``thinking`` blocks are intentionally NOT surfaced — they are
        # internal reasoning, not progress visible to the user.
