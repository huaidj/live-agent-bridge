# live-agent-bridge

本機 Ableton Live MCP 橋接，支援 Remote Script、Max for Live LiveAPI、本機取樣分析與跨後端的品味記憶。參見 [安裝與驗收指南](docs/ACCEPTANCE.md) 及 [實機結果與延遲](docs/VERIFICATION.md)。

高速的 **AI Agent ↔ Ableton Live** MCP 桥接。

- **Live 端**：只用 Live 12 内置的官方 Python API（`Live` 模块 / `_Framework` Remote Script），零第三方依赖。
- **Agent 端**：官方 MCP Python SDK（FastMCP），asyncio 多路复用。

```
AI Agent ──MCP(stdio/HTTP)──► live-agent-bridge (FastMCP, asyncio)
                                   │  NDJSON over TCP 127.0.0.1:9890, 多请求并发 (id 匹配)
                                   ▼
                         Live: Remote Script "AgentBridge"
                           主线程 tick: 轮询 socket → 一次清空整个队列 → Live API
                                        (ping 与快照读缓存，不排在任何写操作后面)
```

## 为什么更快（相对旧版 AbletonMCP / ableton-live-mcp）

| 问题（旧版） | 本实现 |
|---|---|
| 每个命令 `schedule_message(0)`，单请求等一个约 100ms 的 tick | 每个 tick 清空**全部**排队请求，并发 N 个请求只花约 1 个 tick |
| 客户端全局锁，同一时刻只有 1 个请求 | 请求带 id，可流水线并发 |
| 靠 JSON 末尾 `}` 猜测帧边界（易错） | 换行分隔 JSON (NDJSON) 明确分帧 |
| 读状态也要等主线程 | `ping`、`get_session` 读快照缓存，不碰 Live API；代价仍是一个 tick（约 12ms），但不会被写操作堵住 |
| 模态对话框打开时卡 10 秒以上才超时 | 主线程停转超过 2 秒，立即返回 `LiveBusy`，且确保未执行 |
| 浏览器遍历阻塞 Live UI | 生成器分时切片（每 tick ≤30ms），结果缓存 |
| 工具过多时选择困难 | 核心工具和通用 LOM 网关访问当前 Live 版本公开的成员（`AGENT_BRIDGE_TOOLSET=core` 只注册核心部分） |
| 每次编辑一次往返 | `batch`：任意多操作，1 个 tick，1 个撤销步骤 |
| 采样靠 Live 浏览器慢慢遍历 | 本地 SQLite FTS5 采样索引，毫秒级检索 |

若当前 Live 版本提供 `Live.Base.Timer`，脚本会自动启用约 200Hz 的快速 tick；否则使用 `update_display`（约 10Hz）。
用 `live-agent-bridge doctor` 可以看到当前实际使用的模式。

## 安装

```bash
uv venv -p 3.11 && uv pip install -e .
live-agent-bridge install        # 复制 Remote Script 到 User Library/Remote Scripts/AgentBridge
```

然后在 Live 中：**Settings → Link, Tempo & MIDI → Control Surface 选 `AgentBridge`**，Input/Output 设为 None。
首次安装需要重启 Live 才会出现在列表里。

```bash
live-agent-bridge doctor         # 检查安装、连接、tick 模式
live-agent-bridge bench          # 真机测延迟
```

注册到 Agent（Claude Code 示例）：

```bash
claude mcp add ableton -- <venv>/Scripts/live-agent-bridge serve
```

也可以用 HTTP 方式：`live-agent-bridge serve --transport streamable-http --http-port 8765`

## 工具一览

> 工具数量随版本变化，以 MCP 的 `tools/list` 实际返回为准；本文不再写死数字。

- **会话**：`get_session`（缓存，即时返回）、`live_status`、`transport`、`set_song`
- **轨道 / 场景**：`get_track`、`create_track`、`update_track`（音量可写 `-6 dB`）、`track_action`、`scene`
- **Clip / 音符**：`create_clip`、`get_clip`、`update_clip`、`clip_action`、`get_notes`、`write_notes`、`edit_notes`（按 note_id 原位修改）
- **自然语言快捷**：`write_drum_pattern`（`x...x...`）、`write_chords`（`Am F C G` / `ii7 V7 Imaj7`，平滑声部进行，可选琶音/切分）、`write_melody`（`A3:1 C4:.5 r:.5`）、`scale`
- **采样库**：`sample_search`（BPM / 调性含关系调 / 标签 / loop 与 one-shot / 文件夹）、`load_sample`（Simpler / Drum Rack 打击垫 / Session 或 Arrangement 音频 clip）、`load_drum_kit`（一句话组鼓组）、`sample_library`
- **设备 / 浏览器**：`get_device`、`set_device_params`（按名称、归一化值或显示文本设置）、`delete_device`、`browser_search`、`load_device`
- **Arrangement**：`arrangement`（列出 clip、创建 MIDI/音频 clip、从 Session 复制到时间线）
- **视图**：`select`
- **全量 API**：`lom_describe` / `lom_get` / `lom_set` / `lom_call` / `lom_children`，可访问 Live Object Model 的任意对象、属性和方法
- **事件**：`observe` / `unobserve` / `poll_events`（同一 tick 内的多次变化合并为一条）
- **批量**：`batch` / `bridge_ops`
- **第三方插件**：`plugin_inventory`、`plugin_inspect`、`plugin_set`、`plugin_snapshot`、`plugin_preset`、`plugin_learn`（见下文）
- **本地音乐记忆与音频理解**：`taste_profile`、`taste_recall`、`taste_remember`、`music_reference`、`sample_analyze`。
- **Max for Live**：`m4l_bridge` 使用独立的 LiveAPI 通道。需要在工程中加载 [LOM API Bridge](companion/README.md)。

### 官方 API 全量覆盖（默认开启；`AGENT_BRIDGE_TOOLSET=core` 时只注册 `server.py` 的核心工具，不含 `server_ext.py` 的全量 API 工具与 ACEStep 工具）

对照官方 LOM 文档（45 个类、588 个成员，Live 12.4）逐类封装，每个调用都先做 `hasattr` 检测，跨 12.x 版本都能用：

| 工具 | 覆盖的官方 API |
|---|---|
| `clip_edit` | Clip.quantize / quantize_pitch / crop / duplicate_loop / duplicate_region / duplicate_notes_by_id / 选择音符 / move_playing_pos / scrub / set_fire_button_state；Clip.View（网格、显示 loop 和包络） |
| `warp_markers` | 音频 clip 的 warp_markers、add / move / remove_warp_marker、warp_mode、warping |
| `automation` | clip 自动化包络（Python 端的 automation_envelope、create_automation_envelope、insert_step、value_at_time）、clear_envelope / clear_all_envelopes、re_enable_automation |
| `routing` | Track 输入 / 输出路由的类型和通道、Compressor 侧链、Max 设备的 DeviceIO |
| `insert_device` / `move_device` | Track/Chain.insert_device（按名称直接插入原生设备）、Song.move_device |
| `device_control` | 开关、折叠、A/B 比较、延迟信息、store_chosen_bank、Max 参数组、删除 |
| `device_special` | 各设备专属属性和方法：Wavetable / Drift / Meld / Roar / Hybrid Reverb / EQ Eight / Shifter / Spectral Resonator / 插件预设 / Drum Cell 等 |
| `rack` | 宏（增减、随机化）、变体（存储、调用、删除）、insert_chain、Chain 调音台、Chain 设备、copy_pad、Pad 静音 / 独奏 / 清空、in_note / out_note / choke_group |
| `simpler` | 播放和切片模式、Sample 的 warp 参数、crop / reverse / warp_as / warp_double / warp_half / replace_sample、切片增删移 |
| `looper` | Looper 的录音 / 叠录 / 播放 / 停止 / 清除 / 撤销、变速、改长度、export_to_clip_slot |
| `record` | ClipSlot.fire(record_length)、trigger_session_record、capture_midi、capture_and_insert_scene、编曲录音 / 叠录 / punch / 自动化录制 |
| `song_action` | play_selection、scrub_by、nudge、back_to_arranger、Link 节拍同步、SMPTE 和小节-拍时间 |
| `cue_points` / `take_lanes` / `groove_pool` / `tuning` | 定位点、Take Lane、Groove Pool、调律系统 |
| `track_extra` | 电平表、性能占用、冻结状态、编组折叠、删除 arrangement clip、交叉推子、分离声像、Cue 和 Master 调音台 |
| `launch_scene` / `view_state` / `app_control` | 场景发射全部参数、Song.View、Application（CPU、对话框按钮、视图控制、控制面板） |
| `set_memory` / `get_memory` | Song.set_data / get_data：把 Agent 的笔记存进 Live Set |
| `lom_catalog` / `lom_audit` | 官方目录查询；对运行中的 Live 逐类内省，列出已实现、本版本缺失、文档之外的成员 |

其余任何成员都可以用 `lom_get` / `lom_set` / `lom_call` / `observe` 访问。

采样根目录从 Live 自己的 `Library.cfg` 自动发现：Places 用户文件夹、已安装的 Pack、User Library、Core Library。
另外可以通过环境变量 `AGENT_BRIDGE_SAMPLE_DIRS` 或 `sample_library add_folder` 追加目录。

## 第三方插件（VST2 / VST3 / AU / Max for Live）

- `plugin_inventory`：`in_set` 列出工程里每个插件实例，以及已开放和总参数数；`installed` 列出 Live 浏览器里已安装的插件，含格式（VST3 等）和厂商。
- `plugin_inspect`：列出插件的**全部**参数。未开放的参数通过 Python API 的 `get_parameter_names` 读取。结果包含：
  - 哪些参数现在可以控制，以及当前值、取值范围和显示文本
  - 按模块分组（振荡器、滤波器、包络、LFO、效果……）
  - 按参数名识别出的作用（截止频率、共振、失真、干湿比、ADSR、音量、宽度、反馈等）
  - 学过的参数含义，以及预设列表
- `plugin_set`：按名称、作用或序号修改参数。可以用绝对值、归一化值、显示文本（如 `2.5 kHz`）或相对调整（`delta_normalized`）。修改被当作手动操作，开启自动化录制时会被录下。被上下限截住时会明确提示。
- `plugin_snapshot`：保存、恢复、对比插件状态，或按比例在当前状态和快照之间渐变。快照存在 Live Set 里，适合做 A/B。
- `plugin_preset`：插件程序的列表和切换；打开或关闭插件窗口（会改变界面，只在你要求时使用）。
- `plugin_learn`：在本机记住参数的含义。你说的标为已确认；Agent 的推测保持待确认，并且永远不会覆盖你确认过的含义。

**限制**：
- **大型插件只开放手动配置过的参数**：Live 只把用户在设备 "Configure" 模式里添加过的参数开放给 API。Live 的 API 没有"开放参数"的接口，这一步只能在界面里做。工具会准确列出需要开放的参数名。
- **作用识别看的是参数名**：它不会去听插件发出的声音。
- **实测**：Vital 有 2852 个参数，默认只开放 2 到 3 个；DreamEater、Stacker、ORBIT、NuPump 这类参数少的插件全部开放。

## 后台运行

Agent 的操作不会改变你在 Live 里看到的内容：
- 所有写操作结束后，都会恢复你原来选中的轨道、场景和 clip 槽。
- Live 有时会稍后才自动选中刚被操作的轨道（例如替换插件乐器后约 0.6 秒）。之后 3 秒内，只要选中被跳到那条轨道，就会撤销；如果你点的是别的轨道，不会干预。
- 只有 `select`、`view_state`、`app_control` 的视图动作，以及 `device_control` 的 `select` 动作会改变界面，Agent 的说明里已要求只在你明确要求时才用。

## 安全边界

这个桥是给本机 Agent 用的，没有内建认证：

- TCP 端口默认 `127.0.0.1:9890`，任何本机进程都能连上并驱动 Live；不要把端口转发到外网，也不要在共享机器上运行。
- `lom_call` / `lom_set` 能调用 Live Object Model 的任意方法；`load_sample` 能读取本机任意路径的音频文件。
- 只让可信的 MCP 客户端连接这个服务：连上它的 Agent 等于拿到了本机文件与 Live 的控制权。

## 升级（后台热重载）

```bash
live-agent-bridge install   # 复制新代码并在运行中的 Live 里热重载，无需重选控制面板
```

## Splice

`splice_status` 根据本机文件推断 Live 内置 Splice 的状态：集成是否安装、是否登录、最近的付费下载记录，以及订阅状态（例如 `likely_active`，附推断依据，`authoritative: false`）。

已下载到 `Documents\Splice\Samples` 和 Live 内置 Splice 下载目录里的采样会自动加入索引，并打上 `splice` 标签。用 `sample_search(tags=["splice"])` 搜索，用 `load_sample` 或 `load_drum_kit` 加载。

不支持：搜索 Splice 在线曲库，以及用积分下载新采样。这些功能只能通过 Splice 的私有认证接口访问，本工具不读取你的凭证。请在 Live 浏览器的 Splice 面板里下载新采样，下载完成后会自动进入索引。

`load_sample` 不经过 Live 浏览器，而是用官方 API：插入 Simpler，再调用 `replace_sample`；放进 Drum Pad 时还会用 `insert_chain` 新建 chain 并设置 `in_note`。因此磁盘上任意位置的文件都能直接加载。

## 真机实测（Live 12.4.16 Suite，Windows）

| 项 | 结果 |
|---|---|
| tick | `Live.Base.Timer` 约 85–90Hz |
| 顺序单请求 | p50 12.4ms（约一个 tick） |
| 100 个并发请求 | 11.6ms 共计 |
| batch 100 个操作 | 10.2ms 共计 |
| 加载 Splice 采样到 Simpler / Drum Pad | 约 150–190ms |
| 3 个 pad 的鼓组 | 约 290–570ms |
| 冒烟测试 | 30/30 通过 |

## 开发

```bash
uv pip install -e ".[dev]" && pytest
```

测试使用模拟的 Live 对象（`tests/fake_live.py`）和真实 socket，并通过 MCP 内存会话做端到端测试。
