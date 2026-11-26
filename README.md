# live-agent-bridge

English · [繁體中文](README.zh-TW.md) · [Latest release](https://github.com/huaidj/live-agent-bridge/releases/latest)

**Connect an MCP-capable agent to the Ableton Live Set on your computer.** The agent can read tracks, clips, devices, and transport from the current Set. It can make targeted edits and read back what Live applied, which makes it useful for work that would otherwise take repeated clicks across the Set.

## What you can do

- **Compose and arrange.** Create tracks, scenes, and clips. Write MIDI notes, chords, or drum patterns, then place clips in Arrangement.
- **Work with devices.** Load native devices and inspect or change parameters that Live exposes, including configured third-party plug-in parameters. Related edits can be grouped with `batch`.
- **Use local sounds.** Search your local sample library, load sounds into Simpler or Drum Rack, and analyze WAV files. Reference-song analysis needs a configured local CLAP model.

Once your agent is connected, try one of these requests.

> Add a two-bar drum pattern on a new MIDI track. Keep playback stopped and read back the notes.

> Find three local percussion one-shots and build a Drum Rack on a new track.

> Inspect the filter on my Bass track. Show me its exposed cutoff before changing it to 850 Hz, then read it back.

See the [tool reference](docs/TOOLS.md) for tool names, features, and limits.

## Two ways into Live

| Backend | What it does |
| --- | --- |
| **AgentBridge Remote Script** | The default backend uses Live's Python API for sessions, MIDI, arrangement, devices, and routing. The MCP server supports stdio and local HTTP. |
| **Max for Live LiveAPI** | The optional companion provides an independent path through `m4l_bridge`. It can read and observe exposed members and preview supported writes and calls. Pass `write=true` to make a change. The client checks the resolved object identity. |

The two APIs expose different members. Max for Live capabilities depend on the device and Live version. [Install the optional companion](companion/README.md) if you need the LiveAPI path.

## Install v1.0.0 from source on Windows

You need Python 3.11 or newer and Ableton Live 12. The release was verified on **Windows with Ableton Live 12.4.16 Suite**. The optional companion requires Max for Live.

```powershell
git clone --branch v1.0.0 --depth 1 https://github.com/huaidj/live-agent-bridge.git
Set-Location live-agent-bridge
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[audio]'
& .\.venv\Scripts\live-agent-bridge.exe install --no-reload
```

1. In Live, go to **Settings → Link, Tempo & MIDI → Control Surface** and select **AgentBridge**. Set Input and Output to **None**. If the surface is missing after the first installation, reopen Live.
2. With Live running, verify the connection.

   ```powershell
   & .\.venv\Scripts\live-agent-bridge.exe doctor
   ```

3. [Copy the agent setup prompt](docs/AGENT-SETUP.md) into the MCP-capable client you use with Live.

## Verification and speed

On the recorded Windows test fixture, a warm MCP session read had a **21 ms p50** and a batch of 100 property reads had a **22 ms p50**. An M4L song read had a **16 ms p50**. The run also recorded a 3.84-second outlier on the cached MCP path. See the [verification report](docs/VERIFICATION.md) for p95, sample counts, and the direct-connection follow-up.

The [acceptance guide](docs/ACCEPTANCE.md) explains how to repeat offline checks, real Live tests, and optional M4L tests on a disposable Set.

## Local access

The bridge connects to Live on loopback. Local analysis and library indexing work with files on your machine. Keep the endpoints on loopback and connect only trusted clients. The bridge has no built-in authentication, and a connected client can issue edits and request local audio files.

[MIT license](LICENSE)
