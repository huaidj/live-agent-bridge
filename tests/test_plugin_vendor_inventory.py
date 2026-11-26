"""Offline checks for plugin.inventory format/vendor values (no Live needed)."""

import os
import sys
import types

SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from live_agent_bridge.remote_script.AgentBridge import plugins  # noqa: E402


class Item(object):
    def __init__(self, name, children=None, uri=None):
        self.name = name
        self.children = list(children or [])
        self.is_folder = children is not None
        self.is_loadable = children is None
        self.uri = uri


def leaf(name, *segs):
    return Item(name, uri="query:Plugins#" + ":".join(list(segs) + [name]))


def make_tree():
    return Item("Plug-ins", [
        Item("VST3", [
            Item("VendorName", [leaf("PluginName", "VST3", "VendorName")]),
            leaf("NoVendorSynth", "VST3"),
            Item("VST3", [leaf("SameNameFx", "VST3", "VST3")]),
            Item("DeepCo", [Item("Bundle", [Item("Inner", [
                leaf("DeepPlug", "VST3", "DeepCo", "Bundle", "Inner")])])]),
        ]),
    ])


def make_ctx():
    browser = types.SimpleNamespace(plugins=make_tree())
    app = types.SimpleNamespace(browser=browser)
    return types.SimpleNamespace(app=lambda: app,
                                 browser_index=types.SimpleNamespace(by_uri={}))


def run_inventory(query=None):
    fn = getattr(plugins.plugin_inventory, "__wrapped__", plugins.plugin_inventory)
    res = fn(make_ctx(), query=query)
    if hasattr(res, "__next__"):
        try:
            while True:
                next(res)
        except StopIteration as stop:
            res = stop.value
    return res


def by_name(res):
    return dict((p["name"], p) for p in res["plugins"])


def test_format_and_vendor_values():
    res = run_inventory()
    items = by_name(res)
    assert res["total"] == 4
    p = items["PluginName"]
    assert p["format"] == "VST3"
    assert p["vendor"] == "VendorName"
    assert p["uri"] == "query:Plugins#VST3:VendorName:PluginName"
    for row in res["plugins"]:
        assert set(row) == {"name", "path", "format", "vendor", "uri"}


def test_missing_vendor_is_null():
    assert by_name(run_inventory())["NoVendorSynth"]["vendor"] is None


def test_identical_format_and_vendor_names():
    p = by_name(run_inventory())["SameNameFx"]
    assert p["format"] == "VST3"
    assert p["vendor"] == "VST3"


def test_deeper_nesting_keeps_top_vendor():
    p = by_name(run_inventory())["DeepPlug"]
    assert p["format"] == "VST3"
    assert p["vendor"] == "DeepCo"


def test_vendor_name_search_and_filtering():
    res = run_inventory(query="vendorname")
    assert [p["name"] for p in res["plugins"]] == ["PluginName"]
    assert res["total"] == 1
    res = run_inventory(query="deepco")
    assert [p["name"] for p in res["plugins"]] == ["DeepPlug"]
    res = run_inventory(query="nomatchvendor")
    assert res["total"] == 0 and res["plugins"] == []

