"""ACE-Step retries reconcile the actual Live destination before writing."""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from live_agent_bridge import server_acestep as ace


class LiveSet:
    def __init__(self, path):
        self.path = str(path)
        self.tracks = []
        self.mutations = Counter()
        self.before_failure = None
        self.after_failure = set()

    def track(self, name):
        track = {"name": name, "ref": {"$ref": f"id {len(self.tracks) + 1}"}, "session": {}, "arrangement": {}}
        self.tracks.append(track)
        return track

    def clip(self, track, target, position, path):
        ref = {"$ref": f"id {len(self.tracks)}-{target}-{position}", "type": "Clip", "name": "Rendered"}
        track[target][position] = {"file_path": path, "ref": ref}
        return ref

    async def call(self, op, **args):
        # Let parallel callers contend for the actual import lock.
        await asyncio.sleep(0)
        if op == "lom.get":
            return {"file_path": self.path, "view": {"$ref": "id view"}}
        if op == "overview":
            assert args["fresh"]
            return {"tracks": [{"name": track["name"]} for track in self.tracks], "scenes": [{}] * 4}
        if op == "track.create":
            return self.write(op, lambda: self.track(args["name"])["ref"])
        if op == "clip.info" and "clip" in args:
            return next(clip for track in self.tracks for clip in track["arrangement"].values()
                        if clip["ref"]["$ref"] == args["clip"])
        ref = args["track"]
        track = self.tracks[ref] if isinstance(ref, int) else next(t for t in self.tracks if t["ref"]["$ref"] == ref)
        if op == "track.info":
            return {"name": track["name"], "ref": track["ref"], "has_audio_input": True,
                    "clips": [{"slot": slot} for slot in track["session"]],
                    "arrangement_clips": len(track["arrangement"])}
        if op == "arrangement.clips":
            return {"items": [{"ref": clip["ref"], "start_time": beat, "is_midi": False}
                              for beat, clip in track["arrangement"].items()]}
        if op == "clip.info":
            return track["session"][args["slot"]]
        if op == "arrangement.create_audio_clip":
            return self.write(op, lambda: self.clip(track, "arrangement", args["start_time"], args["file_path"]))
        if op == "sample.load":
            assert args["target"] == "session"
            return self.write(op, lambda: self.clip(track, "session", args["slot"], args["path"]))
        if op == "track.set":
            return self.write(op, lambda: track.update(name=args["name"]))
        raise AssertionError(op)

    def write(self, op, apply):
        if self.before_failure == op:
            self.before_failure = None
            raise ToolError("Timeout before Live applied the mutation")
        self.mutations[op] += 1
        result = apply()
        if op in self.after_failure:
            self.after_failure.remove(op)
            raise ToolError("Timeout after Live applied the mutation")
        return result


@pytest.fixture
def test_set(tmp_path, monkeypatch):
    live = LiveSet(tmp_path / "ZZ-fixture.als")
    monkeypatch.setattr(ace.acestep, "OUTPUT_DIR", tmp_path / "results")
    monkeypatch.setattr(ace, "call", live.call)
    return live


def receipt(tmp_path):
    return {"task_id": "existing-task", "request": {"track_name": "keys"},
            "outputs": [{"use_path": str(tmp_path / "rendered.wav")}]}


@pytest.mark.parametrize(("target", "beat", "slot"), [("arrangement", 4.0, None), ("session", None, 2)])
async def test_repeated_import_reuses_exact_clip_and_ignores_unrelated_tracks(test_set, tmp_path, target, beat, slot):
    job = receipt(tmp_path)
    position = beat if target == "arrangement" else slot
    unrelated = test_set.track("ACE keys")
    unrelated_ref = test_set.clip(unrelated, target, position, str(tmp_path / "different.wav"))
    first = await ace._import(job, target, None, beat, slot)
    repeated = await ace._import(job, target, None, beat, slot)
    assert repeated == first
    assert first == [{"track": "ACE keys", "clip": test_set.tracks[1][target][position]["ref"],
                      "file": job["outputs"][0]["use_path"]}]
    assert unrelated[target][position]["ref"] == unrelated_ref
    assert test_set.mutations["track.create"] == 1


async def test_different_targets_positions_names_and_sets_stay_distinct(test_set, tmp_path, monkeypatch):
    job = receipt(tmp_path)
    first = await ace._import(job, "arrangement", None, 4.0, None)
    different_beat = await ace._import(job, "arrangement", None, 8.0, None)
    different_name = await ace._import(job, "arrangement", "Accent", 4.0, None)
    different_target = await ace._import(job, "session", None, None, 0)
    different_slot = await ace._import(job, "session", None, None, 1)
    assert len({items[0]["clip"]["$ref"] for items in (
        first, different_beat, different_name, different_target, different_slot,
    )}) == 5
    assert test_set.mutations["track.create"] == 5
    other = LiveSet(tmp_path / "ZZ-other.als")
    monkeypatch.setattr(ace, "call", other.call)
    await ace._import(job, "arrangement", None, 4.0, None)
    assert other.mutations["track.create"] == 1


async def test_concurrent_retries_create_one_track(test_set, tmp_path):
    job = receipt(tmp_path)
    first, repeated = await asyncio.gather(*[
        ace._import(job, "arrangement", None, 0.0, None) for _ in range(2)
    ])
    assert first == repeated
    assert test_set.mutations == {"track.create": 1, "arrangement.create_audio_clip": 1, "track.set": 1}


async def test_reconciles_lost_responses_without_repeating_writes(test_set, tmp_path):
    job = receipt(tmp_path)
    test_set.after_failure = {"track.create", "arrangement.create_audio_clip", "track.set"}
    result = await ace._import(job, "arrangement", None, 4.0, None)
    assert result == await ace._import(job, "arrangement", None, 4.0, None)
    assert len(test_set.tracks) == 1
    assert test_set.mutations == {"track.create": 1, "arrangement.create_audio_clip": 1, "track.set": 1}


async def test_retry_resumes_pending_track_without_touching_other_placement(test_set, tmp_path):
    job = receipt(tmp_path)
    test_set.before_failure = "sample.load"
    with pytest.raises(ToolError, match="before Live applied"):
        await ace._import(job, "session", None, None, 1)
    assert len(test_set.tracks) == 1
    assert test_set.tracks[0]["name"].startswith("ACE import pending ")
    result = await ace._import(job, "session", None, None, 1)
    assert result == await ace._import(job, "session", None, None, 1)
    assert test_set.mutations == {"track.create": 1, "sample.load": 1, "track.set": 1}


async def test_none_mode_does_not_read_or_write_live(tmp_path, monkeypatch):
    def unexpected(*args, **kwargs):
        raise AssertionError("Live must not be contacted")

    monkeypatch.setattr(ace, "call", unexpected)
    assert await ace._import(receipt(tmp_path), "none", None, 0.0, None) == []
