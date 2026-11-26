# LOM API Bridge for Max for Live

The device uses Max for Live's LiveAPI and Node for Max to serve a loopback connection on TCP port `9878`. The Python Remote Script uses a separate connection.

Copy the `LOM API Bridge` folder into `Presets/MIDI Effects` under the configured Ableton User Library. Leave the `.amxd`, runtime scripts and schema together. Load `LOM_API_Bridge` onto a MIDI track in the target Live Set. Use `m4l_bridge` with action `status` to inspect the connection and runtime readiness.

`get` and `observe` operate on LiveAPI members exposed by the device. `set` and `call` preview by default. An actual write requires `write=true` and is bound to the resolved object identity. After an uncertain write, read the value before deciding whether to issue a different operation. The client can confirm delayed scalar results and bounded float rounding with a readback.

The companion is derived from [ableton-live-mcp, full LOM snapshot](https://github.com/huaidj/ableton-live-mcp/tree/a3e05194bb6862d73af3b550945727c1051e5e84/max_bridge). The repository contains the runtime variant used in the Live tests and retains the [upstream MIT license](<LOM API Bridge/LICENSE>). The LOM catalog and schema describe the interfaces in the [Cycling ’74 Live Object Model reference](https://docs.cycling74.com/apiref/lom/).
