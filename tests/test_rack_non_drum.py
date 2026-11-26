"""rack info on an Audio Effect Rack must not touch drum-pad properties: Live raises RuntimeError
("Only drum racks can have pads!") for them, which hasattr() does not swallow (seen 2026-11-16)."""

from fake_live import Obj


class Chain(Obj):
    def __init__(self, parent, name):
        Obj.__init__(self, parent)
        self.name = name
        self.devices = []
        self.mute = self.solo = False


class AudioRack(Obj):
    class_name = "AudioEffectGroupDevice"
    can_have_chains = True
    can_have_drum_pads = False

    def __init__(self, parent):
        Obj.__init__(self, parent)
        self.name = "Audio Effect Rack"
        self.parameters = []
        self.chains = []
        self.variation_count = 0

    @property
    def has_drum_pads(self):
        raise RuntimeError("Only drum racks can have pads!")

    def insert_chain(self, i):
        self.chains.insert(i, Chain(self, "Chain %d" % (i + 1)))


async def test_insert_chain_on_audio_effect_rack(client, live):
    t = live.song.tracks[0]
    t.devices.append(AudioRack(t))
    r = await client.request("rack", {"action": "insert_chain", "track": 0, "index": 0})
    assert len(r["chains"]) == 1 and "has_drum_pads" not in r and "pads" not in r
