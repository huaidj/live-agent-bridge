# Tool reference

Start with [the project overview](../README.md) or [the copy-once setup prompts](AGENT-SETUP.md). Discover the installed server's actual tools through MCP `tools/list`. Live versions and installed devices determine which native properties are available.

| Work | Tools to reach for |
|---|---|
| Inspect the set | `get_session`, `live_status`, `get_track`, `get_clip`, `get_notes` |
| Compose and arrange | `create_clip`, `write_drum_pattern`, `write_chords`, `write_melody`, `edit_notes`, `arrangement`, `cue_points` |
| Mix and configure | `update_track`, `load_device`, `get_device`, `set_device_params`, `automation`, `routing` |
| Search and load local audio | `sample_search`, `sample_library`, `load_sample`, `load_drum_kit`, `simpler` |
| Edit audio and capture | `clip_edit`, `warp_markers`, `looper`, `record`, `take_lanes` |
| Inspect devices and racks | `device_control`, `device_special`, `rack`, `plugin_inventory`, `plugin_inspect`, `plugin_snapshot` |
| Work with the Live Object Model | `lom_describe`, `lom_get`, `lom_set`, `lom_call`, `lom_catalog`, `lom_audit` |
| Observe and batch work | `observe`, `poll_events`, `unobserve`, `batch`, `bridge_ops` |
| Analyze and recall | `sample_analyze`, `music_reference`, `taste_profile`, `taste_recall`, `taste_remember` |
| Use independent Max transport | `m4l_bridge` with the [companion](../companion/README.md) loaded |
| Generate a local audio part | `acestep_status`, `acestep_generate_part`, `acestep_result` with a configured ACE-Step deployment |

## Editing and verification

Tracks and Session slots use zero-based indexes. Positions and clip lengths are in quarter-note beats. Prefer explicit targets and verify a change by reading the exact same target back. An uncertain write can have taken effect, so inspect state before retrying. `get_session` uses a cache by default. Pass `fresh=true` when a write needs immediate readback. Batch independent operations when it reduces round trips. The bridge groups edits where Live supports it and rejects global Song Undo and Redo.

## Devices and samples

Plug-in inspection reports which parameters Live makes controllable. Large plug-ins can require the user to expose parameters through **Configure** in Live. Meaning inferred from parameter names is provisional. Sample discovery can use Places, installed Packs, User Library and Core Library. To add a directory, use `sample_library` or set `AGENT_BRIDGE_SAMPLE_DIRS`. Locally downloaded Splice samples can be indexed. Online lookup and sample credits are outside this project's Splice integration.

## Optional backends

M4L uses a separate local device and port. Readiness depends on loading that device in the current Live Set. `set` and `call` preview by default on that path. Audio analysis extras and local CLAP or ACE-Step models have separate requirements. For ACE-Step, resume a submitted job with its original task ID. The testable deployment and configuration details are in the [acceptance guide](ACCEPTANCE.md).

## Testing and latency

Use the [acceptance guide](ACCEPTANCE.md) on a disposable set and consult the [measured results](VERIFICATION.md). For a direct round-trip benchmark run `live-agent-bridge bench -n 100`. That benchmark measures the local bridge, separate from the calling agent's overhead.
