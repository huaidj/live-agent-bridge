"""MCP plug-in tools: installed inventory filtering and plugin_learn persistence."""

from test_mcp import mcp_session, tool


def _rows(res):
    if isinstance(res, list):
        return res
    for key in ("plugins", "rows", "results", "installed", "devices"):
        if isinstance(res, dict) and isinstance(res.get(key), list):
            return res[key]
    raise AssertionError("no inventory rows in %r" % (res,))


async def test_installed_plugin_inventory_filters(live, monkeypatch):
    async with mcp_session(live, monkeypatch) as session:
        rows = _rows(await tool(session, "plugin_inventory", scope="installed"))
        assert rows
        for row in rows:
            assert "format" in row and "vendor" in row
        formats = {str(row["format"]).upper() for row in rows}
        assert "VST3" in formats and "AU" in formats
        vendors = {row["vendor"] for row in rows if row["vendor"]}
        assert {"Acme Audio", "Deep Labs"} <= vendors
        acme = _rows(await tool(session, "plugin_inventory", scope="installed", query="Acme"))
        assert acme
        for row in acme:
            text = (str(row.get("name", "")) + " " + str(row.get("vendor", ""))).lower()
            assert "acme" in text
        loose = [row for row in rows if row["name"] == "Loose Synth"]
        assert loose and loose[0]["vendor"] is None


async def test_plugin_learn_keeps_user_meaning_and_lists_in_set(live, monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_BRIDGE_DATA_DIR", str(tmp_path))
    async with mcp_session(live, monkeypatch) as session:
        first = await tool(session, "plugin_learn", plugin="Synth One", parameter="Filter 1 Cutoff",
                           role="cutoff", origin="user")
        assert first and first["status"] == "confirmed"
        later = await tool(session, "plugin_learn", plugin="Synth One", parameter="Filter 1 Cutoff",
                           role="drive", origin="inference")
        assert isinstance(later, dict)
        kept = later.get("kept")
        assert kept and kept.get("role") == "cutoff" and kept.get("status") == "confirmed"
        inv = await tool(session, "plugin_inventory", scope="in_set")
        assert "count" in inv
        for dev in inv.get("devices", []):
            assert "name" in dev and "kind" in dev
