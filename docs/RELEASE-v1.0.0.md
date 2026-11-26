# live-agent-bridge v1.0.0

[English](#english) · [繁體中文](#中文)

## English

Turns an MCP-capable agent on the same computer into a client for Ableton Live. It can inspect a Live Set, build and move MIDI or audio clips, load local samples, adjust exposed device parameters, and read back the results. The included Max for Live companion provides a separate LiveAPI connection.

- Start with the [English overview](https://github.com/huaidj/live-agent-bridge/blob/v1.0.0/README.md) and [copy-once setup prompt](https://github.com/huaidj/live-agent-bridge/blob/v1.0.0/docs/AGENT-SETUP.md#english).
- Use the Python wheel for the MCP package, or install from the tagged source. The Max for Live ZIP contains the companion device and its runtime files. Use `SHA256SUMS.txt` to check the downloaded assets.
- Verified on Windows with Ableton Live 12.4.16 Suite. The live suite covered 84 tools and 226 action choices with 416 passes and zero unexpected failures. Separate M4L tests confirmed 64 real operations and restoration. All 250 offline tests passed.
- Warm MCP session reads measured 34 ms p95 on the idle fixture. M4L reads measured 18 ms p95. The [verification report](https://github.com/huaidj/live-agent-bridge/blob/v1.0.0/docs/VERIFICATION.md) records the workload and a tail-latency outlier.

## 中文

讓同一台電腦上的 MCP Agent 連接 Ableton Live。Agent 能讀取工程、建立和移動 MIDI 或音訊片段、載入本機取樣、調整 Live 已開放的裝置參數，並回讀變更。隨附的 Max for Live 裝置提供獨立的 LiveAPI 連線。

- 先看 [繁體中文首頁](https://github.com/huaidj/live-agent-bridge/blob/v1.0.0/README.zh-TW.md)，或直接複製 [Agent 接入提示詞](https://github.com/huaidj/live-agent-bridge/blob/v1.0.0/docs/AGENT-SETUP.md#中文)。
- 可用 Python wheel 或標籤原始碼安裝 MCP 套件。Max for Live ZIP 內含配套裝置和執行檔案。可用 `SHA256SUMS.txt` 核對下載檔。
- 實測環境為 Windows 與 Ableton Live 12.4.16 Suite。主套件涵蓋 84 個工具與 226 個動作選項，416 項通過、0 項非預期失敗。M4L 另驗證 64 次實際操作與還原，250 項離線測試全過。
- 閒置測試工程的暖機 MCP 工程讀取 p95 為 34 ms，M4L 讀取 p95 為 18 ms。[實測報告](https://github.com/huaidj/live-agent-bridge/blob/v1.0.0/docs/VERIFICATION.md) 有負載條件與一次尾端延遲紀錄。
