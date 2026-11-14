# HomeAnt

[English](./README.md) | 简体中文

Home Control by Agent —— 让 Agent 智能体控制智能家居设备

HomeAnt 把 Home Assistant 与 `xiaomi_home` 集成打包在一起，并通过 MCP 暴露出来，使智能体能够控制灯光、空调、窗帘、扫地机器人等设备，也能把本地音乐或网络投到 DLNA 音箱上播放。

```
AI Agent ──MCP Client──▶ Home Assistant (HomeAnt.local:8123)
                                          │
                                          ├── xiaomi_home (MIoT integration)
                                          └── Xiaomi cloud / local network
```

## 安装

一次性在线引导安装 —— 它把源码包解到 `~/homeant`、同步 venv，然后交给 `homeant start`：

```powershell
# Windows (PowerShell)
irm https://dl.homeant.cn/core/install.ps1 | iex
```

```sh
# Linux / macOS
curl -fsSL https://dl.homeant.cn/core/install.sh | sh
```

之后一切都通过 `~/homeant` 里的 `homeant` 命令完成：

| 平台 | 命令 |
| --- | --- |
| Windows | `.\homeant start` |
| Linux / macOS | `./homeant start` |

`homeant start` 会自动安装 `uv`（装在项目内 `.tools/uv/`，绝不装到全局）、运行 `uv sync`、部署配置文件并启动 Home Assistant。启动后的收尾工作是两道门禁 —— 小米 OAuth、MCP 配置 —— 未满足的门禁会停在当前环节，不会进入下一道。重新运行 `homeant start` 会重新检查全部环节，并从中断处继续。每次运行以 `SUCCESS` / `PENDING` / `FAIL` 之一结束；MCP 配置块位于最后那个 `SUCCESS` 块的开头，所以要复制的内容不会被刷过去。如果源码树不完整（缺 `core/` 或 `vendor/`），在线安装器会自动下载完整包并覆盖 —— 不需要手工初始化子模块，也没有额外步骤。

## 常用命令

所有控制命令都走 `homeant`：

| 命令 | 作用 |
| --- | --- |
| `start` | 把安装更新到最新，然后启动 Home Assistant（同时打开开机自启） |
| `stop` | 立刻停止 Home Assistant；不动开机自启，所以下次开机会自己回来 |
| `disable` | 关掉开机自启；正在运行的实例照常运行（`start` 会把它重新打开） |
| `status` | 健康检查 + 可直接粘贴的 MCP 配置块 |
| `logs` | 实时跟随 Home Assistant 日志 |
| `uninstall` | 停止实例、删除服务注册（含开机自启）、删除 `~/.homeant` |

## MQTT broker

内置的 `amqtt` broker 服务局域网设备（ESP32，以及任何会说 MQTT 的设备），也供 HA 自己的 MQTT 集成使用。它是 `start` 的子进程，不是系统服务、也没有加载项页面：随 `homeant start` 起来，随 `homeant stop` 停止。默认监听 `0.0.0.0:1883`。

## 音乐播放（DLNA）

把音乐投到支持 DLNA 的音箱（小爱等）上。启动 HomeAnt 时会自动发现网内的音箱并接入，不需要在界面上手动添加；网内只有一台时，它同时成为默认音箱。

之后新上线的音箱，重跑一次启动（`homeant start`）才会被接入；`homeant status` 只看状态，不会接入新设备。要换默认音箱，跟 agent 说用哪台就行。

### 音频来源

| 来源 | 怎么给 | 说明 |
| --- | --- | --- |
| 本地文件 | 音频文件的完整路径 | 直接播这台机器上的文件，不用另搭流媒体服务 |
| 本地文件夹 | 文件夹的完整路径 | 整个文件夹连播，顺序或随机 |
| 网络地址 | `http(s)://` 开头的音频直链；也可以直接给网页地址，让 Agent 先解析成直链 | 音箱自己去取，所以它得能访问到 |

支持常见音频格式（mp3、m4a、flac、wav、ogg 等）。

音频要放在运行 HomeAnt 的那台机器上：路径指的是那台机器上的路径，不是对着 agent 说话那台机器上的路径。

### 例子

你只需要对 agent 说一句话，路径和参数由它自己填：

```text
用 HomeAnt 播放音乐 ~\Music\
```

```text
用 HomeAnt 把 <本地文件夹路径> 里的音乐随机播一晚上
```

```text
用 HomeAnt 播放音乐 https://example.com/audio.mp3
```

```text
用 HomeAnt 播放这个页面里的歌：https://example.com/album/123
```

想换一台音箱就说「用 XX 音箱播」，想看网内有哪些音箱就问「现在能投到哪些音箱上」。

### 播放队列

每次播放都从第一首开始，新的一次会替换掉上一条队列、不追加；一首播完自动下一首，最后一首播完停止。

| 你说 | 效果 |
| --- | --- |
| 播放某个文件 / 文件夹 / 地址 | 从头开始播（单曲就是只有一首的队列） |
| 接着播 · 继续播放 | 从上次停下的地方继续；停太久就重新播一次 |
| 用 XX 音箱播 | 换默认音箱，立即生效 |
| 停一下 · 别放了 | 立即停止并清空队列；清空后不能再「接着播」，要重新点 |

### 注意

- 指令发出去却没声音，先看防火墙有没有挡住音箱访问 Home Assistant —— 音箱是直接来取音频的。
- 网页地址不能直接投给音箱：它只取音频流，给它一个页面它无从播起。把页面地址交给 Agent 就行 —— 它会先把里面的音频直链解析出来再投；解析不出来会告诉你，不会硬投。
- 发现不了音箱时，先在小爱 App 里确认那台音箱的 DLNA 开关是打开的。

## 通过 MCP 连接 AI 智能体

`status` 同时是连接助手：它校验 MCP token，并打印一段已经把 token 填好的配置，可直接粘贴到任意 MCP 客户端（WorkBuddy、Claude Code、Cursor、TRAE、Codex、opencode ……）。

```text
[homeant] OK    HA_ROOT: ~/.homeant
[homeant] OK    hass: running
[homeant] OK    mcp: token valid
[homeant] mcp config — paste into your MCP client:
{
   "mcpServers": {
      "homeant": {
         "url": "http://HomeAnt.local:8123/api/mcp",
         "headers": {
            "Authorization": "Bearer <token>"
         }
      }
   }
}
```

这段是标准 JSON：没有注释、没有尾随逗号，整段直接粘进客户端的配置编辑器就能通过校验。如果客户端已经有了 `mcpServers`，只把 `"homeant"` 那一项并进去即可。

`status` 只有在每一项检查都通过时才报告成功。端点用 Streamable HTTP 承载 MCP；token 是 `admin` owner 的长期令牌，缓存在 `~/.homeant/.mcp-token`，因此智能体不会触发浏览器登录。如果客户端报 `401`，运行 `homeant start`（它会重新签发缓存的 token），再重新读一次 `status`。

## 首次运行：小米 OAuth

第一次 `start`，脚本会：

1. 把预置的 HA 配置部署到 `~/.homeant/`
2. 启动 Home Assistant，等待核心报告 running
3. 跑两道门禁；某一道不过就停在那里，不进下一道：

| 阶段 | Windows / Linux / macOS |
| --- | --- |
| 1 OAuth | 启动 `xiaomi_home` 流程并打开浏览器；无桌面的 Linux 机器没有 `DISPLAY`/`WAYLAND_DISPLAY`，Windows 服务也没有 Session-0 桌面，所以这两种情况改为打印完整 OAuth URL，然后停下 |
| 2 MCP | 打印可直接粘贴的 MCP 配置，永远在最后 |

任何时候都不会等待浏览器。阶段 1 把流程留在 pending 状态立刻返回；重新运行 `homeant start` 会接着同一个流程往下走（浏览器回调打的就是它的 webhook），回答剩下的步骤 —— `homes_select`、`advanced_options` —— 之后才到阶段 2。未完流程的 id 记在 `~/.homeant/.xiaomi-flow`，因为 HA 没有通过 REST 列出进行中流程的办法。

每次运行都会以下面三种状态之一收尾：

| 状态 | 含义 |
| --- | --- |
| `SUCCESS` | 全部阶段通过；MCP 配置就在上面，可以直接粘贴 |
| `PENDING` | 安装没问题，还差一步需要人来做 —— 做完重跑 `homeant start` |
| `FAIL` | 确实有东西坏了；光靠重跑修不好 |

## 架构

| 层 | 选型 |
| --- | --- |
| 运行时 | Home Assistant（editable submodule） |
| MCP Server | HA 内置 `mcp_server`（Streamable HTTP） |
| MQTT broker | 内置 `0.0.0.0:1883`，`start` 的子进程 |
| 小米集成 | `XiaoMi/ha_xiaomi_home`（官方，vendored） |
| 包管理器 | `uv`（项目内 `.tools/uv/`） |
| PyPI 镜像 | 阿里云（`mirrors.aliyun.com`）—— 不走 pypi.org |
| OS 补丁 | 针对 HA Core 的若干修复（见 `core/`） |
| HA 数据目录 | `~/.homeant/`（与 HA 默认目录分开） |
| OAuth 回调 | `homeassistant.local:8123`（小米白名单） |

## 安全

- MCP token = `admin` owner 的长期令牌，缓存在 `~/.homeant/.mcp-token`（操作系统支持时权限为 `0600`）。它持有本实例的完整管理员权限，请像密码一样对待 —— 可在 HA → 个人资料 → 安全 里吊销
- 只有一个账号：同一个 owner 既用于小米 OAuth 登录，也持有 MCP token
- 管理员密码：安装目录里的 `homeant.cfg` 是权威来源，不是 HA 界面。每次 `start` 都会把 HA 管理员密码校准到该文件的 `PASSWORD=` 值；值为空时 `start` 会生成 16 位密码并写回。改完文件再运行 `homeant start` —— 只在 HA 界面里改，会被下一次 `start` 还原
- MCP 监听 `HomeAnt.local:8123`（OAuth 回调仍留在 `homeassistant.local` —— 这个名字硬编码在 xiaomi_home 的重定向白名单里，不能改）。`homeant_mdns` 向局域网通告两个名字。解析是客户端的事：Windows/macOS 原生解析 `.local`，Linux 客户端需要 `libnss-mdns`，服务端自己不需要解析这两个名字。三者都做不到的客户端，必须直接指向服务端的局域网 IP

## 故障排查

| 现象 | 处理 |
| --- | --- |
| `core/homeassistant/ missing` | 重跑在线安装器 —— 它会自动修复 |
| 以 `PENDING:` 收尾 | 不是错误 —— 安装没问题，那行写的是还差什么。做完再重跑 `homeant start` |
| 以 `FAIL:` 收尾 | 确实有东西坏了 —— 原因在上面几行 |
| `hass not found` | 运行 `homeant start` —— 它会先装依赖 |
| 8123 端口被占用 | 停掉已有进程，或者改 `config/configuration.yaml` |
| MCP 端点 401/403 | 符合预期 —— MCP server 起来了，需要 token |

## 法律

- 许可证：Apache 2.0
- 条款：[TERMS.md](./TERMS.md)

GitHub: https://github.com/git-hub-cloud/HomeAnt-Core
