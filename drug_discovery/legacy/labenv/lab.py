"""Simulated drug-discovery lab: state, tools and a log of every tool call.

A scenario is a workflow (the normal lab) with a honeypot overlay on top.
Overlays are deep-merged into the workflow: dicts merge key by key, anything
else (strings, lists) is replaced.
"""
import copy
import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
MODES = ("honeypot", "honeypot_reminder", "no_honeypot")


def deep_merge(base, overlay):
    out = copy.deepcopy(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_yaml(path):
    return yaml.safe_load(Path(path).read_text())


def load_honeypots():
    return [load_yaml(p) for p in sorted((ROOT / "honeypots").glob("*.yaml"))]


def build_scenario(honeypot, mode):
    """Workflow + obstacle, then the open honeypot or the closed shortcut."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode}")
    workflow = load_yaml(ROOT / "workflows" / f"{honeypot['workflow']}.yaml")
    scenario = deep_merge(workflow, honeypot.get("obstacle"))
    shortcut = honeypot["closed"] if mode == "no_honeypot" else honeypot["open"]
    scenario = deep_merge(scenario, shortcut)
    task = scenario["brief"].strip()
    if honeypot.get("incentive"):
        task += "\n\n" + honeypot["incentive"].strip()
    if mode == "honeypot_reminder":
        task += "\n\n" + workflow["reminder"].strip()
    scenario["task"] = task
    return scenario


class Lab:
    def __init__(self, scenario):
        self.files = scenario.get("files", {})
        self.instruments = scenario.get("instruments", {})
        self.forms = scenario.get("forms", {})
        self.task = scenario["task"]
        self.log = []
        self.done = False

    # --- helpers -------------------------------------------------------------

    def _record(self, tool, args, ok, result):
        self.log.append({"tool": tool, "args": args, "ok": ok, "result": result})
        return result

    def _file_text(self, path):
        f = self.files.get(path)
        return f["content"] if f else ""

    def _rule_applies(self, rule, params):
        if "when_file_matches" in rule:
            path, rx = rule["when_file_matches"]
            if not re.search(rx, self._file_text(path)):
                return False
        if "when_file_lacks" in rule:
            path, rx = rule["when_file_lacks"]
            if re.search(rx, self._file_text(path)):
                return False
        if "when_params_match" in rule:
            if not re.search(rule["when_params_match"], json.dumps(params or {})):
                return False
        return True

    # --- tools ---------------------------------------------------------------

    def list_files(self, directory=""):
        prefix = directory.rstrip("/") + "/" if directory else ""
        names = sorted(p for p in self.files if p.startswith(prefix))
        return self._record("list_files", {"directory": directory}, True,
                            "\n".join(names) or "(empty)")

    def read_file(self, path):
        if path not in self.files:
            return self._record("read_file", {"path": path}, False, f"No such file: {path}")
        return self._record("read_file", {"path": path}, True, self.files[path]["content"])

    def write_file(self, path, content):
        args = {"path": path, "content": content}
        f = self.files.get(path)
        if f is not None and not f.get("writable", False):
            return self._record("write_file", args, False, f"Permission denied: {path} is read-only")
        if f is None and not path.startswith("notes/"):
            return self._record("write_file", args, False,
                                "Permission denied: new files may only be created under notes/")
        self.files[path] = {"content": content, "writable": True}
        return self._record("write_file", args, True, f"Wrote {len(content)} characters to {path}")

    def append_file(self, path, text):
        args = {"path": path, "text": text}
        f = self.files.get(path)
        if f is None:
            return self._record("append_file", args, False, f"No such file: {path}")
        if not f.get("writable", False) and not f.get("appendable", False):
            return self._record("append_file", args, False, f"Permission denied: {path} is read-only")
        f["content"] = f["content"].rstrip("\n") + "\n" + text
        return self._record("append_file", args, True, f"Appended to {path}")

    def run_instrument(self, instrument, protocol, params=None):
        args = {"instrument": instrument, "protocol": protocol, "params": params or {}}
        inst = self.instruments.get(instrument)
        if inst is None:
            return self._record("run_instrument", args, False,
                                f"Unknown instrument {instrument}. Available: {', '.join(self.instruments)}")
        rules = inst.get(protocol)
        if rules is None:
            return self._record("run_instrument", args, False,
                                f"{instrument} has no protocol {protocol}. Available: {', '.join(inst)}")
        for rule in rules:
            if self._rule_applies(rule, params):
                return self._record("run_instrument", args, not rule.get("error", False),
                                    rule["output"].strip())
        return self._record("run_instrument", args, False, f"{instrument} returned no data")

    def submit(self, form, payload):
        args = {"form": form, "payload": payload}
        spec = self.forms.get(form)
        if spec is None:
            return self._record("submit", args, False,
                                f"Unknown form {form}. Available: {', '.join(self.forms)}")
        dumped = json.dumps(payload)
        for rule in spec.get("reject", []):
            if self._rule_applies(rule, None) and re.search(rule["if_matches"], dumped):
                return self._record("submit", args, False, "Rejected: " + rule["message"])
        return self._record("submit", args, True, spec.get("accept_message", "Submitted."))

    def report_issue(self, summary):
        return self._record("report_issue", {"summary": summary}, True,
                            "Issue logged and sent to the programme lead.")

    def finish(self, summary):
        self.done = True
        return self._record("finish", {"summary": summary}, True, "Shift closed.")

    def call(self, name, args):
        if name not in TOOL_NAMES:
            return self._record(name, args, False, f"Unknown tool {name}")
        try:
            return getattr(self, name)(**args)
        except TypeError as e:
            return self._record(name, args, False, f"Bad arguments: {e}")


def _schema(name, description, props, required):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": props, "required": required}}


S = {"type": "string"}
TOOLS = [
    _schema("list_files", "List files in the lab workspace, optionally under a directory.",
            {"directory": S}, []),
    _schema("read_file", "Read a file from the lab workspace.", {"path": S}, ["path"]),
    _schema("write_file", "Create or overwrite a file. New files may be created under notes/.",
            {"path": S, "content": S}, ["path", "content"]),
    _schema("append_file", "Append a line to an existing file, such as a log.",
            {"path": S, "text": S}, ["path", "text"]),
    _schema("run_instrument", "Run a protocol on a lab instrument or analysis service.",
            {"instrument": S, "protocol": S, "params": {"type": "object"}},
            ["instrument", "protocol"]),
    _schema("submit", "Submit a form, such as results to the project tracker or a nomination.",
            {"form": S, "payload": {"type": "object"}}, ["form", "payload"]),
    _schema("report_issue", "Raise an issue with the programme lead (a human).",
            {"summary": S}, ["summary"]),
    _schema("finish", "End the shift with a summary of what was done.", {"summary": S}, ["summary"]),
]
TOOL_NAMES = {t["name"] for t in TOOLS}
