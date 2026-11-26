# Release acceptance

Run the checks for the features you intend to verify. Record the source revision, Python and Live versions, and companion device version where applicable. Label each check with its observed result and attach the corresponding receipt after reviewing it for private data.

| Check | Evidence it provides | Prerequisites |
| --- | --- | --- |
| Offline regression and lint | Mock Live behavior, protocol tests and lint for the source under test | Python and development dependencies |
| Live acceptance and coverage | MCP tool calls against Live, fixture cleanup and reported tool, action and operation coverage | Windows, Live, AgentBridge, fixture WAV and the Max for Live companion for the current full harness |
| Max for Live sweep and failure paths | Exposed member reads and previews, selected writes, and protocol behavior | Live with Max for Live and the separately installed companion |
| Max for Live writes | Scoped writes and method calls checked against the Remote Script, fixture cleanup and saved-file hash | Same companion and a disposable Live Set. Audio-clip cases also need fixture audio |
| Remote Script maintenance | Browser result, reload and reconnect, and fresh fixture comparison | Live and AgentBridge. Timer testing also needs the active timer configuration |
| Local analysis jobs | Fixture decoding, job lifecycle and isolated storage over stdio | Local WAV and audio dependencies. Reference analysis also needs a local CLAP model |
| ACE-Step | Readiness, generation and output retrieval for the configured service | Separately provisioned GPU service, an authorized reference WAV and permission to generate |

## Environment and fixture on Windows

Run these commands from the repository root in PowerShell. Use a clean virtual environment and install development and audio dependencies.

```powershell
py -3.11 -m venv .venv
$python = '.\.venv\Scripts\python.exe'
$bridge = '.\.venv\Scripts\live-agent-bridge.exe'
& $python -m pip install -e '.[dev,audio]'
& $python -m pytest -q
& $python -m ruff check src tests
New-Item -ItemType Directory -Force -Path '.\artifacts\fixtures' | Out-Null
```

1. Save any work in Live. Create a blank, disposable Set and save it as `artifacts/fixtures/ZZ-acceptance.als`. Put a short local PCM WAV you can use for testing at `artifacts/fixtures/test.wav`.
2. Run `& $bridge install --no-reload`. Use `--user-library` if Live uses a different User Library from the CLI default. In Live Settings, select `AgentBridge` under **Link, Tempo & MIDI → Control Surface** and set Input and Output to **None**. Restart Live after a first installation if the surface has yet to appear.
3. Open the saved fixture. Stop playback, turn off Arrangement Record, Session Record and both punch switches, and disarm all tracks. Leave the fixture idle while the suites run. Run `& $bridge doctor` to verify the connection.

The setup uses Live's settings and the Windows CLI. The tests drive the local bridge directly and need no screen automation. Keep the transport on loopback and use a fresh output directory for each run.

```powershell
$fixture = (Resolve-Path -LiteralPath '.\artifacts\fixtures\ZZ-acceptance.als').Path
$wav = (Resolve-Path -LiteralPath '.\artifacts\fixtures\test.wav').Path
$reports = (New-Item -ItemType Directory -Path (Join-Path '.\artifacts' ('acceptance-' + [guid]::NewGuid()))).FullName
$env:AGENT_BRIDGE_DATA_DIR = Join-Path $reports 'agent-data'
```

## Live acceptance and coverage

The full harness temporarily edits the test Set and records tool, action and operation coverage. Complete the companion setup below before running it. `acceptance_on_copy.py` opens a copy of the saved fixture and reopens the fixture afterward. Leave the default safety checks enabled and use a separate output directory each time.

```powershell
& $python .\scripts\acceptance_on_copy.py $fixture --json (Join-Path $reports 'live.json')
& $python .\scripts\coverage_report.py (Join-Path $reports 'full_live_test_rows.json.coverage.json') `
    --json (Join-Path $reports 'coverage.json')
```

Check the wrapper exit code, `live.json`, and the coverage receipt. Verify fixture restoration, zero unexpected failures and the reported action and operation gaps. A tool appearing in the coverage table only establishes that a call was attempted. `EXPECTED` denotes a documented refusal. `NOT_RUN` and `BOUNDED` describe checks that could not be established in that run.

### Harness input portability

Use the Python interpreter in the same environment as the installed `live-agent-bridge` command. The core harness and M4L read sweep can generate a synthetic WAV in the receipt directory. Pass `--sample` to the core or the sweep to supply a different test WAV. `ZZ_TEST_FIXTURE_WAV` is also supported when launching the core through `acceptance_on_copy.py`.

## Max for Live companion and tests

The Python package includes the M4L client. Follow the [LOM API Bridge installation guide](../companion/README.md) to install the companion files into the Ableton User Library. `live-agent-bridge install` installs the Python Remote Script. If the companion uses a custom loopback port, set `ABLETON_MAX_PORT` to that port before starting the test clients.

For an already loaded device, the MCP `m4l_bridge` action `status` can verify connectivity and runtime readiness. An installed-file check alone does not demonstrate a functioning runtime.

```powershell
& $python .\scripts\acceptance_on_copy.py $fixture --script .\scripts\m4l_sweep.py `
    --json (Join-Path $reports 'm4l-sweep.json')
& $python .\scripts\acceptance_on_copy.py $fixture --script .\scripts\m4l_failure_paths.py `
    --json (Join-Path $reports 'm4l-failure-paths.json')
& $python .\scripts\m4l_live_writes.py --fixture $fixture --sample $wav `
    --json (Join-Path $reports 'm4l-writes.json')
```

The sweep depends on what the companion advertises for reachable fixture objects. Preview rows provide evidence for preview behavior. Verified real-write rows require read-back and restoration. The failure-path suite deliberately disconnects and reloads the companion on the disposable copy. Record the actual coverage and the capabilities the tested versions did not expose.

## Remote Script maintenance

With the exact `ZZ-` fixture still open and idle, run the maintenance suite. Its reload check drops active bridge connections briefly, so run it separately from the other suites.

```powershell
& $python .\scripts\mcp_live_maintenance.py --fixture $fixture `
    --json (Join-Path $reports 'maintenance.json')
```

The receipt records the raw `browser.tree` list validation, the reload acknowledgment, the old socket closing, the fresh reconnection and the before and after fixture comparison. Add `--timer-config $timerConfig` only after independently identifying the active installed `AgentBridge/config.json` and assigning its resolved path to `$timerConfig`. The timer test needs a readable exact interval and an active fast timer so it can change and restore the interval. Otherwise the receipt marks that test `BOUNDED`.

## Local analysis jobs

The local jobs harness starts a fixture-scoped stdio server and uses its own temporary files and databases. It can run with Live closed. Supply an existing WAV no larger than 64 MiB and a new absolute run directory.

```powershell
& $python .\scripts\mcp_local_jobs_acceptance.py --fixture-wav $wav `
    --run-dir (Join-Path $reports 'local-jobs')
```

For reference analysis, append `--clap-model $modelDir` with a verified local model directory. Append `--audio-python $audioPython` if the model needs a separate installed interpreter. The reference case is `NOT_RUN` when the model is omitted. Check `local-jobs/acceptance.json` for job results and fixture hash verification. Keep the run directory private.

## Optional ACE-Step

ACE-Step is a separately deployed local service. Configure `ACESTEP_URL`, `ACESTEP_MODEL`, `ACESTEP_PROCESS_TEMP` and `ACESTEP_OUTPUT_DIR` for that deployment. Service start and stop also require `ACESTEP_PROJECT` to point to a deployment that supplies its own service scripts.

- Use `acestep_status` to record readiness and model identity. A readiness probe establishes connectivity and status only.
- The documented `lego` task generates a new instrument part as audio from a 10–120 second WAV excerpt. Tempo and key guide the model. Use `import_to="none"` for an output-only check, or import into the disposable fixture to verify the Live integration.
- If generation returns a task ID before completion, call `acestep_result` with that ID. For a service-off run, confirm the configured endpoint is unavailable before launching the full Live harness. The current harness can generate and import audio when that endpoint reports ready.

## Sharing release evidence

Keep raw receipts and logs out of commits. They can contain resolved paths, device and track names, commands, sample references and job identifiers. Publish a redacted summary with versions, pass and failure counts, scoped coverage, cleanup results and the reason for each conditional check. Record successful offline, Live and companion-device runs separately.
