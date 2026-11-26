# live-agent-bridge

[English](README.md) · 繁體中文 · [最新版本](https://github.com/huaidj/live-agent-bridge/releases/latest)

**讓支援 MCP 的代理連上電腦裡的 Ableton Live Set。** 代理可以讀取目前的軌道、片段、裝置和播放狀態，對指定目標編輯，再回讀 Live 實際套用的結果。適合把原本要在多條軌道和視窗間反覆點選的工作交給代理處理。

## 可以做什麼

- **創作與編曲。** 建立軌道、場景和片段，寫入 MIDI 音符、和弦或鼓組節奏，再將片段放進編曲時間軸。
- **編輯裝置。** 載入 Live 原生裝置，讀取或修改 Live 開放的參數，包含已在 Live 中設定開放的第三方外掛參數。可用 `batch` 合併相關操作。
- **使用本機音色。** 搜尋本機取樣庫、把音檔載入 Simpler 或 Drum Rack，並分析 WAV 檔。參考曲分析需要已設定的本機 CLAP 模型。

接好後，可以試著交代以下工作。

> 在新 MIDI 軌做兩小節的鼓組節奏，保持停止播放，完成後回讀音符。

> 在本機找三個單發打擊音色，在新軌做成 Drum Rack。

> 檢查 Bass 軌上的濾波器，先顯示它開放的截止頻率，再調成 850 Hz 並回讀結果。

工具名稱、功能和限制可參見[工具參考](docs/TOOLS.md)。

## 兩個 Live 後端

| 後端 | 用途 |
| --- | --- |
| **AgentBridge Remote Script** | 預設後端，透過 Live 的 Python API 操作會話、MIDI、編曲、裝置和路由。MCP 伺服器支援 stdio 和本機 HTTP。 |
| **Max for Live LiveAPI** | 選用裝置透過 `m4l_bridge` 提供獨立通道，用來讀取和觀察公開成員，也能預覽支援的寫入與方法呼叫。要實際修改時請設定 `write=true`，客戶端會核對解析出的物件身分。 |

兩套 API 開放的成員各有差異。Max for Live 可用的功能取決於裝置和 Live 版本。需要 LiveAPI 通道時，請參照[選用裝置安裝指南](companion/README.md)。

## 在 Windows 從原始碼安裝 v1.0.0

需要 Python 3.11 或更新版本，以及 Ableton Live 12。本版實機驗證環境為 **Windows 上的 Ableton Live 12.4.16 Suite**。選用裝置需要 Max for Live。

```powershell
git clone --branch v1.0.0 --depth 1 https://github.com/huaidj/live-agent-bridge.git
Set-Location live-agent-bridge
py -3.11 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e '.[audio]'
& .\.venv\Scripts\live-agent-bridge.exe install --no-reload
```

1. 在 Live 的 **Settings → Link, Tempo & MIDI → Control Surface** 選擇 **AgentBridge**，Input 和 Output 都設為 **None**。首次安裝後若清單還沒出現 AgentBridge，請重新開啟 Live。
2. 保持 Live 開啟，檢查連線。

   ```powershell
   & .\.venv\Scripts\live-agent-bridge.exe doctor
   ```

3. 將[代理設定提示詞](docs/AGENT-SETUP.md)複製到你搭配 Live 使用、支援 MCP 的客戶端。

## 驗證與速度

在 Windows 測試工程上，暖機後的 MCP 會話讀取 **p50 為 21 ms**，一批 100 次屬性讀取 **p50 為 22 ms**，M4L 讀取 Song 屬性 **p50 為 16 ms**。MCP 快取讀取另出現一次 3.84 秒的延遲。[實機驗證報告](docs/VERIFICATION.md)列出 p95、樣本數和直連複測數據。

[驗收指南](docs/ACCEPTANCE.md)說明如何在獨立測試工程重跑離線檢查、Live 實機測試與選用的 M4L 測試。

## 本機存取

橋接透過回環位址連線至 Live。本機分析和取樣索引使用電腦裡的檔案。請將端點保留在回環位址，並只讓信任的客戶端連線。橋接沒有內建驗證，連線後的客戶端可以編輯工程，也能請求載入本機音檔。

[MIT 授權](LICENSE)
