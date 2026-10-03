"""Check every honeypot without a model, using its scripted honest and exploit paths.

For each honeypot and mode it asserts:
- the honest script never counts as a hack (precision),
- the exploit counts as a hack when the honeypot is open (checkability),
- the exploit fails when the shortcut is closed, in no_honeypot mode.
"""
import sys

from labenv import MODES, Lab, build_scenario, load_honeypots, score

REQUIRED = ["id", "title", "category", "workflow", "step", "incentive", "obstacle",
            "open", "closed", "hack_if", "scripts"]
CATEGORIES = {"FAB", "TAMPER", "SELECT", "SKIP", "BYPASS", "LEAK", "PROXY", "OVERSIGHT"}


def run_script(honeypot, mode, script):
    lab = Lab(build_scenario(honeypot, mode))
    for name, args in honeypot["scripts"][script]:
        lab.call(name, args)
    return score(honeypot, lab.log)


def main():
    errors, rows = [], []
    honeypots = load_honeypots()
    ids = [h.get("id") for h in honeypots]
    errors += [f"duplicate id {i}" for i in set(ids) if ids.count(i) > 1]
    for h in honeypots:
        hid = h.get("id", "?")
        missing = [k for k in REQUIRED if k not in h]
        if missing:
            errors.append(f"{hid}: missing {missing}")
            continue
        if h["category"] not in CATEGORIES:
            errors.append(f"{hid}: unknown category {h['category']}")
        for mode in MODES:
            honest = run_script(h, mode, "honest")["outcome"]
            exploit = run_script(h, mode, "exploit")["outcome"]
            rows.append((hid, mode, honest, exploit))
            if honest.startswith("HACK"):
                errors.append(f"{hid}/{mode}: honest path counted as hack")
            if mode == "no_honeypot" and exploit.startswith("HACK"):
                errors.append(f"{hid}/{mode}: exploit still works with the shortcut closed")
            if mode != "no_honeypot" and not exploit.startswith("HACK"):
                errors.append(f"{hid}/{mode}: exploit not detected ({exploit})")

    print(f"{'id':6} {'mode':18} {'honest':10} exploit")
    for r in rows:
        print(f"{r[0]:6} {r[1]:18} {r[2]:10} {r[3]}")
    print(f"\n{len(honeypots)} honeypots x {len(MODES)} modes = {len(rows)} cases")
    for e in errors:
        print("ERROR", e)
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
