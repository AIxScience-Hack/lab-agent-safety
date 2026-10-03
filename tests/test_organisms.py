"""The prompt ladder: files exist and follow the rules in ladder.yaml, the composed
rungs are built from the single-component ones, and lab suites refuse unfrozen prompts."""
import json
import re

import pytest

from organisms import ladder

RUNGS = ladder.load()
TEXT = {r["name"]: ladder.text_of(r["path"]) for r in RUNGS if r["path"]}

# Words that would tell the model what to do or where it is. The integrity rungs name
# prohibited actions on purpose.
INSTRUCTS = re.compile(r"fabricat|falsif|fake|cheat|hack|shortcut|bypass|tamper|conceal|hide|secret|"
                       r"evaluat|test(ed|ing)?\b|monitor|oversight", re.I)
DOMAIN = re.compile(r"\blab\b|laborator|instrument|assay|sample|cell|batch|reagent|sensor|calibrat|"
                    r"warehouse|pipeline|backup", re.I)


def test_ladder_is_well_formed():
    names = [r["name"] for r in RUNGS]
    assert len(names) == len(set(names))
    assert names[0] == "baseline" and RUNGS[0]["path"] is None
    assert {r["set"] for r in RUNGS} <= set(ladder.SETS)
    for r in RUNGS[1:]:
        assert r["path"].is_file() and TEXT[r["name"]], r["name"]


@pytest.mark.parametrize("name", sorted(TEXT))
def test_prompt_follows_the_rules(name):
    text = TEXT[name]
    assert not DOMAIN.search(text), f"{name} uses task vocabulary: {DOMAIN.search(text).group(0)}"
    if "integrity" not in name:
        assert not INSTRUCTS.search(text), f"{name} instructs or cues: {INSTRUCTS.search(text).group(0)}"


def test_composed_rungs_are_built_from_their_parts():
    seeker, upper = TEXT["seeker_a"], TEXT["upper"]
    goal, stakes, analysis = seeker.split(". ")[0] + ".", *upper.split("\n\n")[1:]
    assert upper == "\n\n".join([seeker, stakes, analysis])
    assert TEXT["upper_no_stakes"] == "\n\n".join([seeker, analysis])
    assert TEXT["upper_no_analysis"] == "\n\n".join([seeker, stakes])
    assert TEXT["upper_no_metric"] == "\n\n".join([goal, stakes, analysis])
    assert TEXT["seeker_integrity"] == "\n\n".join([seeker, TEXT["integrity"]])


def test_lab_suites_need_frozen_prompts(tmp_path):
    frozen = tmp_path / "frozen.json"
    with pytest.raises(SystemExit, match="not frozen"):
        ladder.check_frozen(RUNGS, frozen)
    frozen.write_text(json.dumps(ladder.digests(RUNGS)), encoding="utf-8")
    ladder.check_frozen(RUNGS, frozen)
    edited = dict(ladder.digests(RUNGS), seeker_a="0" * 64)
    frozen.write_text(json.dumps(edited), encoding="utf-8")
    with pytest.raises(SystemExit, match="seeker_a"):
        ladder.check_frozen(RUNGS, frozen)


def test_ladder_refuses_lab_run_before_freeze(monkeypatch, tmp_path):
    monkeypatch.setattr(ladder, "FROZEN", tmp_path / "frozen.json")
    with pytest.raises(SystemExit, match="not frozen"):
        ladder.main(["--bench", "labbench", "--model", "mockllm/model", "--dry-run"])
    assert ladder.main(["--bench", "practice", "--model", "mockllm/model", "--dry-run", "--rungs", "baseline"]) == 0
