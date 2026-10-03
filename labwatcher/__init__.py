"""LabWatcher: Apollo-style Watcher pipeline for lab-automation agents.

Exports are resolved lazily (PEP 562) so that importing the package works even while
sibling modules (pipeline.py, models.py, ...) are still being written by other owners.
"""

_EXPORTS = {
    "Watcher": "labwatcher.pipeline",
    "Action": "labwatcher.pipeline",
    "Decision": "labwatcher.pipeline",
    "Settings": "labwatcher.settings",
    "Store": "labwatcher.store",
    "RuleEngine": "labwatcher.rules",
    "Rule": "labwatcher.rules",
    "RuleHit": "labwatcher.rules",
    "TAXONOMY": "labwatcher.rules",
}

__all__ = sorted(_EXPORTS)


def __getattr__(name):
    module_name = _EXPORTS.get(name)
    if module_name is None:
        raise AttributeError(f"module 'labwatcher' has no attribute {name!r}")
    import importlib
    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))
