"""Inspect harness with a scripted mock model: audit entries carry tool-call IDs, calls
that fail Inspect's schema check are still logged, the saved log verifies, labels stay out
of the top-level metadata, and the cap keeps the dropped calls."""
import pytest

pytest.importorskip("inspect_ai")

from inspect_ai import eval as inspect_eval  # noqa: E402
from inspect_ai.model import ChatMessageAssistant, ModelOutput, get_model  # noqa: E402
from inspect_ai.tool import ToolCall  # noqa: E402

import inspect_tasks  # noqa: E402
from labsim import AuditLog  # noqa: E402
from labsim.core import monitor_view  # noqa: E402

M = "mockllm/model"


def _multi(calls):
    """One assistant message with several tool calls [(id, name, args)]."""
    msg = ChatMessageAssistant(content="", tool_calls=[
        ToolCall(id=i, function=n, arguments=a) for i, n, a in calls])
    return ModelOutput.from_message(msg) if hasattr(ModelOutput, "from_message") else \
        ModelOutput(model=M, choices=[{"message": msg, "stop_reason": "tool_calls"}])


def _run(outputs, max_tool_calls=100):
    task = inspect_tasks.lab_benchmark(env="aspirin", honeypots="a01", conditions="control",
                                       arms="neutral", max_tool_calls=max_tool_calls,
                                       ask_awareness=False)
    model = get_model(M, custom_outputs=outputs)
    log = inspect_eval(task, model=model, display="none", log_dir=None, log_format="json")[0]
    assert log.status == "success", log.error
    return log.samples[0]


def test_harness_logs_ids_bad_args_and_session_end(tmp_path, monkeypatch):
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path))
    outputs = [
        _multi([("c1", "list_files", {"directory": "docs"}),
                ("c2", "read_file", {"path": "docs/forms.md"})]),
        ModelOutput.for_tool_call(M, "read_file", {"path": 7, "extra": True}, tool_call_id="c3"),
        ModelOutput.for_tool_call(M, "instrument", {"name": "fume_hood", "command": "status",
                                                    "args": None}, tool_call_id="c4"),
        ModelOutput.for_tool_call(M, "finish", {"summary": "done"}, tool_call_id="c5"),
    ]
    sample = _run(outputs)
    store = sample.store
    entries = store["lab_log"]
    by_id = {e.get("call_id"): e for e in entries}
    assert [e.get("call_id") for e in entries[:-1]] == ["c1", "c2", "c3", "c4", "c5"]
    assert by_id["c3"]["ok"] is False and by_id["c3"]["result"].startswith("Bad arguments")
    assert by_id["c4"]["ok"] and by_id["c4"]["record"]["units"] == "m/s"
    assert entries[-1]["tool"] == "_session_end" and entries[-1]["args"] == {"reason": "finish"}
    assert store["audit_verified"] is True
    assert AuditLog.verify_entries(entries, head=store["audit_head"], genesis=store["audit_genesis"],
                                   salt=store["audit_salt"])
    view = store["lab_log_monitor"]
    assert all("flags" not in e for e in view) and view == monitor_view(entries)
    assert AuditLog.verify_entries(view, head=store["audit_head"], genesis=store["audit_genesis"])
    assert not AuditLog.verify_entries(entries[:-1], head=store["audit_head"])
    assert store["session_end_message_index"] <= len(sample.messages)
    md = sample.metadata
    assert "honeypot" not in md and md["labels"]["honeypot"] == "a01"
    score = sample.scores["lab_score"]
    assert score.metadata["steps"] == len(entries) - 1


def test_harness_keeps_calls_dropped_at_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("INSPECT_LOG_DIR", str(tmp_path))
    outputs = [_multi([("d1", "list_files", {}), ("d2", "list_files", {}),
                       ("d3", "read_file", {"path": "docs/forms.md"})])]
    sample = _run(outputs, max_tool_calls=2)
    store = sample.store
    assert store["stop_reason"] == "tool_call_limit"
    assert [c["id"] for c in store["dropped_tool_calls"]] == ["d3"]
    assert store["lab_log"][-1]["args"] == {"reason": "tool_call_limit"}
