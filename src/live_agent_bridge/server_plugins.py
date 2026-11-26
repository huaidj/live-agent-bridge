"""Third-party plug-in tools (VST2/VST3/AU and Max for Live devices)."""

from __future__ import annotations

import asyncio
from typing import Annotated, Literal

from mcp.server.fastmcp.exceptions import ToolError
from pydantic import BaseModel, Field

from . import plugins as knowledge
from .server import RO, RW, DevicePath, DeviceSel, TrackRef, call, mcp


@mcp.tool(annotations=RO)
async def plugin_inventory(
    scope: Annotated[Literal["in_set", "installed"], Field(description=(
        "in_set: every plug-in/Max device instance in the open set with how many parameters are exposed; "
        "installed: plug-ins in Live's browser with format and vendor"))] = "in_set",
    query: str | None = None,
) -> dict:
    """Which third-party plug-ins exist: in this set, or installed (loadable with load_device uri)."""
    if scope == "in_set":
        return await call("plugin.list_in_set")
    return await call("plugin.inventory", timeout=120.0, query=query)


@mcp.tool(annotations=RO)
async def plugin_inspect(
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
    filter: Annotated[str | None, Field(description="Only parameters whose name contains this")] = None,
    include_unexposed: Annotated[bool, Field(description="List every unexposed parameter name too")] = False,
) -> dict:
    """Understand a third-party plug-in (or any device): ALL its parameters (also those Live has not
    exposed yet), which are controllable now, values/ranges/display, module grouping (oscillators,
    filter, envelopes, LFOs, FX...), detected roles (cutoff, resonance, drive, mix, ADSR, volume,
    width, feedback...), learned meanings, presets, and exactly what to configure for the rest."""
    data = await call("plugin.inspect", device=path, track=track, index=device)
    data = await asyncio.to_thread(knowledge.understand, data)
    if filter:
        f = filter.lower()
        data["exposed_parameters"] = [p for p in data["exposed_parameters"] if f in p["name"].lower()]
        data["unexposed_parameter_names"] = [n for n in data["unexposed_parameter_names"] if f in n.lower()]
    elif not include_unexposed and len(data["unexposed_parameter_names"]) > 60:
        data["unexposed_parameter_names_sample"] = data["unexposed_parameter_names"][:60]
        del data["unexposed_parameter_names"]
    return data


class PluginChange(BaseModel):
    name: str | None = Field(None, description="Parameter name (exact, case-insensitive or substring)")
    role: str | None = Field(None, description=(
        "Or a role: cutoff, resonance, drive, mix, attack, decay, sustain, release, volume, width, pitch, "
        "feedback, delay_time, space_size, lfo_rate, lfo_depth, low_cut, high_cut, unison, pan, macro"))
    index: int | None = None
    value: float | None = Field(None, allow_inf_nan=False, description="Raw value within min..max")
    normalized: float | None = Field(None, ge=0, le=1, allow_inf_nan=False)
    display: str | None = Field(None, description="Display text, e.g. '2.5 kHz', '-6 dB', '35 %', or an item name")
    delta_normalized: float | None = Field(None, ge=-1, le=1, allow_inf_nan=False, description="Relative move, e.g. -0.1 = 10% lower")


@mcp.tool(annotations=RW)
async def plugin_set(
    changes: list[PluginChange],
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
) -> dict:
    """Move plug-in parameters by name, role or index: absolute, normalized, display text or relative
    (delta_normalized). Changes are recorded as automation when automation recording is armed.
    Unexposed parameters are refused with instructions to configure them."""
    specs = []
    inspected = None
    for ch in changes:
        spec = ch.model_dump(exclude_none=True)
        role = spec.pop("role", None)
        if role and "name" not in spec and "index" not in spec:
            if inspected is None:
                inspected = await call("plugin.inspect", device=path, track=track, index=device)
            try:
                spec["name"] = await asyncio.to_thread(knowledge.resolve_role, inspected, role)
            except ValueError as e:
                raise ToolError(str(e)) from None
        specs.append(spec)
    res = await call("plugin.set", changes=specs, device=path, track=track, index=device)
    return {"changes": res.get("items", res) if isinstance(res, dict) else res}


@mcp.tool(annotations=RW)
async def plugin_snapshot(
    action: Literal["save", "recall", "morph", "diff", "list", "delete"] = "list",
    name: str | None = None,
    amount: Annotated[float, Field(ge=0, le=1, description="morph: 0 = current, 1 = snapshot")] = 0.5,
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
) -> dict:
    """Save / recall / compare / morph between states of a device's exposed parameters (stored in the
    Live set, per track and device) - safe A/B experiments on any plug-in."""
    return await call("plugin.snapshot", action=action, name=name, amount=amount, device=path, track=track,
                      index=device)


@mcp.tool(annotations=RW)
async def plugin_preset(
    action: Literal["list", "select", "open_editor", "close_editor"] = "list",
    preset: Annotated[int | str | None, Field(description="Preset index or name")] = None,
    track: TrackRef | None = None,
    device: DeviceSel = None,
    path: DevicePath = None,
) -> dict:
    """Plug-in programs/presets exposed to Live, and the plug-in window (open/close changes the UI: only
    when the user asks, e.g. to configure parameters)."""
    return await call("plugin.preset", action=action, preset=preset, device=path, track=track, index=device)


@mcp.tool(annotations=RW)
async def plugin_learn(
    plugin: Annotated[str, Field(description="Plug-in name as in plugin_inspect 'plugin_name', e.g. 'Vital'")],
    parameter: str,
    role: str | None = None,
    note: Annotated[str, Field(max_length=500)] = "",
    origin: Annotated[Literal["user", "inference"], Field(description=(
        "user = the user said it (confirmed); inference = your guess (stays pending, never overrides user)"))
    ] = "inference",
    action: Annotated[Literal["learn", "forget"], Field(description=(
        "learn = store the meaning; forget = remove it (an inference only removes a pending guess, "
        "a user-confirmed meaning needs origin='user')"))] = "learn",
) -> dict:
    """Remember what a plug-in parameter does (role and note), locally and across sessions/models.
    plugin_inspect and role-based plugin_set use it next time. action='forget' removes a learned meaning."""
    try:
        if action == "forget":
            return await asyncio.to_thread(knowledge.forget, plugin, parameter, origin)
        return await asyncio.to_thread(knowledge.learn, plugin, parameter, role, note, origin)
    except (ValueError, OSError, TimeoutError) as e:
        raise ToolError(str(e)) from None

