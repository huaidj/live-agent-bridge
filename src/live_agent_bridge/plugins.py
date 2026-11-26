"""Understanding third-party plug-ins: parameter roles, module grouping and learned knowledge.

Role detection is a transparent keyword map over parameter names (it does not listen to the
plug-in). Knowledge the user teaches is stored locally per plug-in and wins over detection;
agent guesses stay pending until the user confirms them.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .taste.locking import file_lock
from .taste.paths import data_dir

# Ordered: specific roles first so e.g. "Low Cut" is not taken for a filter cutoff.
ROLES: list[tuple[str, str]] = [
    ("bypass", r"\bbypass\b|\bpower\b|on/off|device on|\benable"),
    ("low_cut", r"low ?cut|lo ?cut|high ?pass|\bhpf?\b"),
    ("high_cut", r"high ?cut|hi ?cut|low ?pass|\blpf?\b"),
    ("lfo_rate", r"lfo.*(rate|freq|speed|sync)|\brate\b|\bspeed\b"),
    ("lfo_depth", r"lfo.*(amount|depth|amt)|\bdepth\b"),
    ("cutoff", r"cutoff|cut off|\bfreq(uency)?\b|\bfc\b"),
    ("resonance", r"\breso|resonance|\bq\b|emphasis"),
    ("drive", r"drive|distort|satur|overdrive|\bfuzz|\bcrush"),
    ("mix", r"\bmix\b|dry ?/ ?wet|\bwet\b|\bdry\b|blend|amount"),
    ("attack", r"attack|\batk\b"),
    ("decay", r"\bdecay\b|\bdcy\b"),
    ("sustain", r"sustain|\bsus\b"),
    ("release", r"release|\brel\b"),
    ("feedback", r"feedback|\bfb\b"),
    ("delay_time", r"delay|\btime\b|predelay|pre-delay"),
    ("space_size", r"\bsize\b|\broom\b|\bspace\b|reverb"),
    ("width", r"width|stereo|spread|\bhaas"),
    ("pitch", r"pitch|tune|transpose|\bsemi|coarse|\bfine\b|detune|octave"),
    ("unison", r"unison|voices|\bsuper\b"),
    ("pan", r"\bpan"),
    ("volume", r"volume|\blevel\b|\bgain\b|output|\bvol\b|\bamp\b|\bmaster\b"),
    ("macro", r"macro"),
]
_COMPILED = [(role, re.compile(rx, re.I)) for role, rx in ROLES]


def role_of(name: str) -> str | None:
    for role, rx in _COMPILED:
        if rx.search(name):
            return role
    return None


def group_of(name: str) -> str:
    words = name.replace("_", " ").split()
    if not words:
        return "other"
    if words[0].lower() in ("lo", "low", "hi", "high") and len(words) > 1 and words[1].lower() in ("cut", "pass", "shelf"):
        return "filter"
    if len(words) > 1 and (words[1].isdigit() or len(words[1]) == 1):
        return f"{words[0]} {words[1]}"
    return words[0] if len(words) > 1 else "global"


def _knowledge_path() -> Path:
    return data_dir() / "plugin_knowledge.json"


def load_knowledge() -> dict:
    path = _knowledge_path()
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def learn(plugin: str, parameter: str, role: str | None, note: str, origin: str) -> dict:
    if origin not in ("user", "inference"):
        raise ValueError("origin must be user or inference")
    if not plugin or not parameter:
        raise ValueError("plugin and parameter are required")
    path = _knowledge_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(path.with_suffix(".lock"), timeout=5):
        data = load_knowledge()
        entry = {"role": role, "note": note[:500], "origin": origin,
                 "status": "confirmed" if origin == "user" else "pending", "updated": time.time()}
        previous = data.setdefault(plugin, {}).get(parameter)
        if previous and previous.get("status") == "confirmed" and origin != "user":
            return {"kept": previous, "ignored": entry,
                    "reason": "a user-confirmed meaning is never overwritten by an inference"}
        data[plugin][parameter] = entry
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return {"plugin": plugin, "parameter": parameter, **entry}


def forget(plugin: str, parameter: str, origin: str) -> dict:
    """Remove one learned meaning. An inference may only remove a pending guess; deleting a
    user-confirmed meaning needs origin='user' (the user asked for it), mirroring learn()."""
    if origin not in ("user", "inference"):
        raise ValueError("origin must be user or inference")
    if not plugin or not parameter:
        raise ValueError("plugin and parameter are required")
    path = _knowledge_path()
    if not path.exists():
        return {"plugin": plugin, "parameter": parameter, "deleted": False, "reason": "nothing learned"}
    with file_lock(path.with_suffix(".lock"), timeout=5):
        data = load_knowledge()
        entry = data.get(plugin, {}).get(parameter)
        if entry is None:
            return {"plugin": plugin, "parameter": parameter, "deleted": False, "reason": "nothing learned"}
        if entry.get("status") == "confirmed" and origin != "user":
            return {"plugin": plugin, "parameter": parameter, "deleted": False, "kept": entry,
                    "reason": "a user-confirmed meaning is only removed when the user asks (origin='user')"}
        del data[plugin][parameter]
        if not data[plugin]:
            del data[plugin]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
    return {"plugin": plugin, "parameter": parameter, "deleted": True, "removed": entry}


def understand(inspect: dict) -> dict:
    """Add roles, groups and learned knowledge to a plugin.inspect result."""
    plugin = inspect.get("plugin_name") or inspect.get("name") or ""
    learned = load_knowledge().get(plugin, {})
    exposed_names = set()
    roles: dict[str, list[str]] = {}
    for p in inspect.get("exposed_parameters", []):
        exposed_names.add(p["name"])
        knowledge = learned.get(p["name"])
        confirmed = bool(knowledge and knowledge.get("role") and knowledge.get("status") == "confirmed")
        role = knowledge["role"] if confirmed else role_of(p["name"])
        p["role"] = role
        p["group"] = group_of(p["name"])
        if knowledge:
            p["learned"] = knowledge
        if role:
            roles.setdefault(role, []).append(p["name"])
    groups: dict[str, dict] = {}
    for name in [p["name"] for p in inspect.get("exposed_parameters", [])] + inspect.get("unexposed_parameter_names", []):
        g = groups.setdefault(group_of(name), {"exposed": 0, "unexposed": 0, "examples": []})
        g["exposed" if name in exposed_names else "unexposed"] += 1
        if len(g["examples"]) < 4:
            g["examples"].append(name)
    hidden_roles: dict[str, list[str]] = {}
    for name in inspect.get("unexposed_parameter_names", []):
        r = role_of(name)
        if r and len(hidden_roles.setdefault(r, [])) < 6:
            hidden_roles[r].append(name)
    inspect["understanding"] = {
        "controllable_roles": roles,
        "roles_needing_configure": hidden_roles,
        "modules": dict(sorted(groups.items(), key=lambda kv: -(kv[1]["exposed"] + kv[1]["unexposed"]))[:40]),
        "learned_parameters": len(learned),
        "method": "parameter-name keyword roles + user-taught knowledge; the plug-in's sound is not analysed",
    }
    return inspect


def resolve_role(inspect: dict, role: str) -> str:
    """Pick the one exposed parameter that plays ``role`` (learned meaning first)."""
    understood = understand(inspect)
    learned = [p["name"] for p in understood["exposed_parameters"]
               if p.get("learned", {}).get("role") == role and p["learned"].get("status") == "confirmed"]
    if len(learned) == 1:
        return learned[0]
    candidates = understood["understanding"]["controllable_roles"].get(role, [])
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        hidden = understood["understanding"]["roles_needing_configure"].get(role)
        if hidden:
            raise ValueError(f"No exposed parameter plays {role!r}; configure one of {hidden} in Live first")
        raise ValueError(f"No parameter with role {role!r} was found; use a parameter name")
    raise ValueError(f"Several parameters play {role!r}: {candidates}; name one explicitly or teach it "
                     "with plugin_learn")
