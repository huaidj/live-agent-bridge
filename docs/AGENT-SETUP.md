# Agent setup

將其中一份提示整段複製給可在 Ableton Live 所在電腦執行命令的代理。Copy one complete prompt into an agent that can work on the computer running Ableton Live.

## 中文

```text
請在運行 Ableton Live 的本機安裝並接入 huaidj/live-agent-bridge v1.0.0。完成可執行的步驟，並從這個代理宿主實際呼叫 MCP 驗證。

1. 確認代理可在 Live 所在的 Windows 或 macOS 本機執行命令。先唯讀檢查既有 checkout、Python 環境、AgentBridge Control Surface、MCP 註冊與橋接狀態。符合版本且正常的部分直接沿用。若只有遠端或雲端環境，說明須在 Live 所在電腦開啟可執行本機命令的代理。

2. 從 https://github.com/huaidj/live-agent-bridge.git 確認 v1.0.0 tag 與 commit。若儲存庫需要登入，沿用已授權的 GitHub 存取，保護 token、密碼和登入資料。若無法讀取儲存庫或 tag，回報實際錯誤與所需權限。有相符的既有 checkout 就沿用。需要新 checkout 時，使用 git clone --branch v1.0.0 --single-branch <官方儲存庫 URL> <新的本機目錄>，核對 HEAD 等於 refs/tags/v1.0.0^{commit}，保留現有 checkout 的本機改動。

3. 使用 Python 3.11 以上建立或沿用這個 checkout 的獨立虛擬環境。從 checkout 根目錄，用選定的 Python 執行 -m venv .venv，再用虛擬環境內的 Python 執行 -m pip install .。核對已安裝套件版本為 1.0.0，找出 live-agent-bridge 的絕對執行檔路徑。Windows 通常是 .venv\Scripts\live-agent-bridge.exe，macOS 通常是 .venv/bin/live-agent-bridge。

4. 在 Live 設定中確認實際使用的 User Library。以相同環境的 live-agent-bridge doctor 唯讀檢查。若 Remote Script 未安裝或需要更新，使用該絕對執行檔執行 install --no-reload。自訂目錄加上 --user-library "<User Library 絕對路徑>"，並在執行 doctor 時將 ABLETON_USER_LIBRARY 指向同一目錄。若 Live 尚未載入正確版本，請使用者在 Settings → Link, Tempo & MIDI 的 Control Surface 選擇或重選 AgentBridge，Input 與 Output 都選 None。若首次安裝後清單仍沒有 AgentBridge，請使用者先儲存工程，再重開 Live。驗證僅讀取狀態，保留目前工程、軌道與播放狀態。

5. 使用此宿主支援的 MCP 註冊方式，將 command 設為前述絕對執行檔路徑，參數設為 serve --transport stdio，讓宿主啟動程式。若已有一條可用的相符註冊，沿用它。新增時保留其他設定，若名稱已由另一個程式使用，就選用新的註冊名稱。Remote Script 預設使用 127.0.0.1:9890。保持本機連線，並依宿主支援的方式重新載入 MCP 工具。

6. 從宿主呼叫 get_session(fresh=true) 和 live_status()。只有在兩個呼叫均成功，且回傳目前 Live 工程的狀態與橋接健康資訊後，才回報已接入。記錄實際 BPM、軌道數、Live 版本及 tick 模式。若工具尚未由宿主載入、Live 尚未啟動或 AgentBridge 尚未生效，說清楚缺少哪一步。

7. 如需 Max for Live，這是另一條本機通道。依 v1.0.0 的 companion/README.md，把完整的 companion/LOM API Bridge 目錄放到目前 User Library 的 Presets/MIDI Effects，將 .amxd、JS 與 schema 保持在同一目錄。這條通道需具備 Max for Live，並由使用者在可丟棄的測試 Set 的 MIDI 軌載入 LOM_API_Bridge。若裝置已在目前 Set 載入，直接唯讀驗證。呼叫 m4l_bridge(action="status")，確認 connected、readiness.status 為 ok，且 readiness.bootstrap.runtime_ready 為 true；再用 m4l_bridge(action="get", path="live_set", object_type="Song", member="tempo") 對照 Remote Script 的 BPM。預設為 127.0.0.1:9878。使用自訂 User Library 時，另核對實際檔案位置，M4L 的 install 報告可能仍指向預設目錄。未要求或未載入時，將 Max for Live 標為未驗證。

最後簡短回報版本與 commit、宿主註冊名稱、驗證讀值、選用功能狀態及任何需要使用者完成的具體步驟。以實際 MCP 回讀作為連線證據。
```

## English

```text
Install and connect huaidj/live-agent-bridge v1.0.0 on the computer running Ableton Live. Carry out the available steps and verify the connection by calling MCP tools from this agent host.

1. Confirm that the agent can run local commands on the Windows or macOS computer hosting Live. Start with read-only checks of the existing checkout, Python environment, AgentBridge Control Surface, MCP registration, and bridge health. Reuse any matching parts that already work. If you can only run commands in a remote or cloud environment, identify the need for an agent on the Live computer.

2. Verify the v1.0.0 tag and commit from https://github.com/huaidj/live-agent-bridge.git. If the repository requires sign-in, use existing authorized GitHub access and keep credentials private. If the repository or tag is inaccessible, report the actual error and required access. Reuse a matching checkout. If a new checkout is needed, run git clone --branch v1.0.0 --single-branch <official repository URL> <new local directory> and verify HEAD equals refs/tags/v1.0.0^{commit}. Preserve any local changes in existing checkouts.

3. Use Python 3.11 or newer and an isolated virtual environment for this checkout. From the checkout root, run the selected Python with "-m venv .venv" if needed. Then run the virtual environment's Python with "-m pip install ." and verify that the installed package version is 1.0.0. Resolve the absolute live-agent-bridge executable path. Typical paths are .venv\Scripts\live-agent-bridge.exe on Windows and .venv/bin/live-agent-bridge on macOS.

4. Identify the User Library configured in Live's Settings. Run doctor with the same live-agent-bridge executable for a read-only check. If the Remote Script needs installing or updating, run install --no-reload with that executable. For a custom library add --user-library "<absolute User Library path>" and set ABLETON_USER_LIBRARY to that folder for doctor. If Live has yet to load the correct script, ask the user to select or reselect AgentBridge in Settings → Link, Tempo & MIDI under Control Surface, with Input and Output both set to None. If the surface is missing after a first install, ask the user to save their work before reopening Live. Keep validation read-only and preserve the open Set, tracks, and transport state.

5. Use this host's supported MCP registration mechanism. Set command to the absolute executable and arguments to serve --transport stdio so the host launches it. Reuse a working matching entry. Preserve the existing configuration and choose a new registration name if that name is already used by a different program. The Remote Script defaults to 127.0.0.1:9890. Keep the connection local and reload tool discovery using the host's supported method.

6. From the host, call get_session(fresh=true) and live_status(). Report the Remote Script bridge connected only after both return the current Live state and bridge health. Record the observed BPM, track count, Live version and tick mode. If the host has yet to load the tools, Live is closed, or AgentBridge is inactive, identify the concrete missing step.

7. If Max for Live is requested, set up its separate local connection. Follow companion/README.md in v1.0.0 and copy the whole companion/LOM API Bridge folder to Presets/MIDI Effects in the configured User Library. Keep the .amxd, JavaScript and schema together. This requires Max for Live and LOM_API_Bridge loaded by the user on a MIDI track in a disposable Set. If the companion is already loaded in the current Set, verify it read-only. Call m4l_bridge(action="status") and confirm connected, readiness.status equal to ok, and readiness.bootstrap.runtime_ready equal to true. Then run m4l_bridge(action="get", path="live_set", object_type="Song", member="tempo") and compare it with the Remote Script BPM. The default is 127.0.0.1:9878. For a custom User Library, verify the files at the actual location since the M4L install report may still check the default folder. Mark Max for Live unverified if it was skipped or the companion is unavailable in the Set.

Finish with the version and commit, host registration name, observed verification values, optional feature status, and any exact user step still needed. Use actual MCP readback as the connection evidence.
```
