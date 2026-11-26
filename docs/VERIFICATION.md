# Live verification

The test runs used Ableton Live **12.4.16 Suite** on Windows. They ran against a separate, saved blank test set. The tests read back Live state and verify restoration. The procedure is in [ACCEPTANCE.md](ACCEPTANCE.md).

| Area | Verified result |
|---|---|
| MCP core | 416 passed, 26 expected host responses, zero failures. All 84 tools and 226 declared action choices were exercised. The main suite covered 81 Remote Script operations, and maintenance covered the other 2. |
| Offline regression | 250 tests passed. Ruff passed for the packaged source, tests and acceptance scripts. |
| Max for Live reads and previews | 604 exposed fixture members scanned across 667 entries, including 420 reads, 285 observations and 184 method previews. The counters overlap when an object supports multiple actions. |
| Max for Live real changes | 47 actual property changes and 17 actual method operations. Each change had evidence from both backends, with state restoration and an unchanged saved fixture hash. |
| Max for Live protocol | 31 failure-path checks passed, including stale identity, frame and connection limits, subscription isolation and reconnect. |
| Remote Script maintenance | Raw browser results, hot reload and reconnection, and a timer change and restore all passed. |
| Native workflows | Looper recorded and exported to a Session audio clip. A stock routable Max device switched between two test MIDI sources and restored the first route. Capture MIDI created a Session MIDI clip from notes sent through a temporary local MIDI input. A real save dialog on the disposable set was dismissed through the MCP button action. |
| Local analysis | Sample analysis completed, cancelled and resumed from the same job. The installed local CLAP model completed reference analysis, and a separate job cancelled without a result. |
| ACE-Step | The configured local service started and stopped. A single synthetic reference produced a 12-second stereo 48 kHz part, retrieved by its original task ID and imported into Live. Two independent MCP clients requesting the same placement reused a single clip. A different placement created a distinct clip. Owned test tracks were removed. |

The M4L member sweep tests reads, observations and previews as separate capabilities. Its coverage count does not imply an actual write to each member. The device and Remote Script expose different subsets of Live's native APIs.

Global Song undo and redo requests are rejected through the bridge to preserve Live's undo history. The Looper's device-level undo remains available. Operations such as Capture MIDI, device I/O, plug-in inspection and local generation need the corresponding MIDI input, routable device, installed plug-in or model.

The Looper check verified the exported clip and its file reference. Live held the temporary WAV open exclusively during the separate PCM inspection. Imported ACE-Step audio follows the current Live Warp defaults.

## Response times

Warm calls were timed from the caller with an idle four-track fixture. The following results describe this host and workload.

| Operation | Samples | p50 | p95 |
|---|---:|---:|---:|
| MCP session read, cached or fresh | 50 each | 21 ms | 34 ms |
| MCP batch of 100 property reads | 15 | 22 ms | 42 ms |
| Remote Script write and readback | 35 | 46 ms | 56 ms |
| M4L song read | 35 | 16 ms | 18 ms |
| M4L property write and Remote Script readback | 35 | 43 ms | 56 ms |

The cached MCP sample included one 3.84 s stall. A follow-up direct connection test of 100 cached reads measured 12.31 ms p50, 20.86 ms p95 and 21.70 ms maximum. The cause of the MCP stall remains unverified. On the direct connection, one batch of 100 reads completed in 10.89 ms. Use `live-agent-bridge bench -n 100` to measure the direct connection on another host.

Raw receipts stay local because they record file paths and test infrastructure details. The repository includes the code and instructions needed to generate new fixtures and receipts.
