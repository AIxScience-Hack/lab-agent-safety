"""One line per sample from Inspect .eval logs: outcome, stop reason, calls, failed calls.

    python summarize_logs.py LOGDIR [-v]

-v adds the last submit/report/finish results, the first failed calls and the most-called tools.
Reads the scorer's audit log, so this is for us, never for a monitor.
"""
import collections
import glob
import json
import sys

from inspect_ai.log import read_eval_log


def summarize(sample, verbose):
    sc = (sample.scores or {}).get("lab_score")
    md = sc.metadata if sc else {}
    log = md.get("audit_log", [])
    failed = [e for e in log if not e.get("ok", True)]
    err = sample.error.message[:80] if sample.error else None
    print(f"  {sample.id:40s} {(sc.answer if sc else '-'):18s} stop={md.get('stop_reason')} "
          f"calls={md.get('tool_calls')} failed_calls={len(failed)} "
          f"cite_viol={md.get('citation_violation')} err={err}")
    if not verbose:
        return
    for e in [e for e in log if e.get("tool") in ("submit", "report_issue", "finish")][-4:]:
        print("     ", e["tool"], "ok=", e.get("ok"), "|", str(e.get("result"))[:300].replace("\n", " "))
    for e in failed[:6]:
        print("      FAIL", e["tool"], json.dumps(e.get("args"))[:140], "->",
              str(e.get("result"))[:140].replace("\n", " "))
    tools = collections.Counter(
        e.get("tool") + (f":{e.get('args', {}).get('name')}" if e.get("tool") == "instrument" else "")
        for e in log)
    print("      most-called:", tools.most_common(6))


def main():
    verbose = "-v" in sys.argv
    totals = collections.Counter()
    for path in sorted(glob.glob(f"{sys.argv[1]}/**/*.eval", recursive=True)):
        log = read_eval_log(path)
        print("==", path.split("/")[-1], log.status, log.eval.model)
        for s in log.samples or []:
            summarize(s, verbose)
            sc = (s.scores or {}).get("lab_score")
            totals[sc.answer if sc else "unscored"] += 1
    print("outcomes:", dict(totals))


if __name__ == "__main__":
    main()
