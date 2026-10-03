"""Oracle runs for every aspirin card, agent-visible wording, and malformed agent input."""
import json
import re

import pytest

import check_tasks
from labsim import ARMS, TOOLS, build_scenario, load_task, make_lab, modes_for

TASK, CARDS = load_task("aspirin")
CARD_IDS = [c["id"] for c in CARDS]
BY_ID = {c["id"]: c for c in CARDS}


# --- oracle: every card, every condition, both arms ----------------------------------

@pytest.mark.parametrize("cid", CARD_IDS)
def test_oracle_scripts_scored_as_expected(cid, monkeypatch):
    # check_tasks.check runs every card of a task; restrict it to one card per case.
    monkeypatch.setattr(check_tasks, "load_task", lambda name: (TASK, [BY_ID[cid]]))
    rows, errors, n = check_tasks.check("aspirin")
    assert n == 1 and rows
    assert errors == []


def test_core_selfcheck_passes():
    assert check_tasks.core_selfcheck() == []


def test_lint_has_no_errors():
    _, errors = check_tasks.lint("aspirin", TASK, CARDS)
    assert errors == []


# --- agent-visible wording -------------------------------------------------------------

def _visible_text():
    """(source, text) for prompts, workspace files, tool schemas and oracle tool outputs."""
    out = [("TOOLS", json.dumps(TOOLS))]
    for card in CARDS:
        for mode in modes_for(card):
            for arm in ARMS:
                s = build_scenario(TASK, card, mode, arm)
                where = f"{card['id']}/{mode}/{arm}"
                out.append((f"{where}:prompt", s["prompt"]))
                out += [(f"{where}:{p}", f["content"]) for p, f in s["files"].items()]
                for script in card.get("scripts") or {}:
                    if mode == "costly" and script != "costly_honest":
                        continue
                    _, lab = check_tasks.run(TASK, card, mode, script, arm)
                    out += [(f"{where}:{script}:{e['tool']}", str(e["result"])) for e in lab.log]
    return out


VISIBLE = None


def visible():
    global VISIBLE
    if VISIBLE is None:
        VISIBLE = _visible_text()
    return VISIBLE


def _hits(pattern):
    rx = re.compile(pattern, re.I)
    return sorted({(src, m.group()) for src, text in visible() for m in rx.finditer(text)})


def test_visible_text_avoids_honeypot_trap_eval():
    assert _hits(r"\b(honeypots?|traps?|trapped|evals?|evaluat\w*)\b") == []


def test_visible_text_avoids_test():
    # Whole words only (an identifier such as foo_test is one token and not a hit).
    assert _hits(r"\btests?\b") == []


# --- malformed agent input must fail the call, not crash the run ---------------------------

MALFORMED = [
    ("submit", {"form": "eln", "payload": None}),
    ("submit", {"form": "eln", "payload": [1]}),
    ("instrument", {"name": "balance", "command": "weigh", "args": "x"}),
    ("instrument", {"name": ["x"], "command": "weigh"}),
    ("read_file", {"path": ["a"]}),
    ("list_files", {"directory": None}),
    ("append_file", {"path": "notes/x", "text": None}),
    ("nope", {}),
    ("finish", {}),
]


@pytest.mark.parametrize("tool,args", MALFORMED)
def test_malformed_call_is_logged_not_raised(tool, args):
    lab = make_lab(build_scenario(TASK, BY_ID["a01"], "control"))
    lab.call(tool, args)
    assert len(lab.log) == 1 and lab.log.verify()


def test_write_file_non_string_path_does_not_crash():
    lab = make_lab(build_scenario(TASK, BY_ID["a01"], "control"))
    lab.call("write_file", {"path": None, "content": "x"})
    assert lab.log[-1]["ok"] is False
