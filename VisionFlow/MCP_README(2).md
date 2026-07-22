# RCS Phone Remote Control — MCP 接入指南

本指南面向**外部开发者**，帮助你将 RCS 手机远程控制服务集成到自己的 AI 应用中（如 Claude Desktop、Cursor、自定义 Agent 等）。

---

## 概述

RCS 通过 [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) 暴露手机操控接口。接入后，AI 模型可以直接：

- 查看在线设备列表和状态
- 锁定/释放一台或多台设备
- 执行触控、滑动、输入文字、截屏等操作
- 向多台设备批量广播指令
- 观察设备状态而不锁定（不干扰其他使用者）

**所有触控坐标均使用归一化值（0.0~1.0）**，MCP Server 自动转换为设备像素坐标。

---

## 1. 获取 API Key

所有 MCP 调用均需 API Key 认证。

1. 登录 RCS Web 管理后台（由服务提供方提供地址）。
2. 在侧边栏点击 **「API Key 管理」**。
3. 点击 **「+ 新建 Key」**，填写标记名后生成。
4. 复制生成的 `rc_xxxxxxxx...` 字符串，妥善保管。

> **注意：** Key 只在创建时显示一次完整内容，请务必立即复制保存。

---

## 2. 账号等级（Tier）说明

你的账号等级决定了可同时控制的设备数量和锁定时长：

| 等级 | 同时锁定设备数 | 设备池容量 | 锁定超时 | 说明 |
|------|---------------|-----------|---------|------|
| **Free** | 1 台 | 1 台 | 60 分钟 | 共享策略，超时自动释放 |
| **Pro** | 5 台 | 5 台 | 60 分钟 | 共享策略，适合小团队 |
| **Enterprise** | 5 台 | 5 台 | 无限制 | 独占策略，在线期间持续持有 |

- **锁定超时**：超过指定时间未操作，设备自动释放给其他用户。
- **设备池**：你可以同时持有的设备总数。池满后需释放才能获取新设备。

> 如需升级等级，请联系服务提供方管理员。

---

## 3. 接入方式

RCS MCP 支持两种接入方式，根据你的 AI 客户端选择：

| 方式 | 传输协议 | 是否需要安装 | 适用客户端 |
|------|---------|-------------|-----------|
| **远程 HTTP 直连** | Streamable HTTP | 不需要 | Antigravity IDE 等 |
| **本地 stdio 连接** | stdio | 需要 Node.js >= 18 | Claude Desktop、Cursor 等 |

> 两种方式的工具和功能完全一致，只是传输协议不同。

### 方式 A：远程 HTTP 直连（推荐）

无需安装任何软件，直接通过 `serverUrl` 连接正式环境：

```
服务地址: https://rc.guokecs.com/mcp
认证方式: Authorization: Bearer rc_你的API_Key
```

### 方式 B：本地 stdio 连接

需要本地安装 Node.js >= 18，然后安装 `phone-remote-control-mcp` 包。

#### 关于 `phone-remote-control-mcp`

这是 RCS MCP Server 的 npm 包名。包内注册了两个可执行命令：

| 命令 | 说明 |
|------|------|
| `phone-rc-mcp` | stdio 模式（本地进程通信） |
| `phone-rc-mcp-http` | HTTP 模式（远程服务） |

#### 安装方式

**从 npm 安装（已发布时）：**

```bash
npm install -g phone-remote-control-mcp
```

安装后即可直接使用 `phone-rc-mcp` 命令。

**从源码安装：**

```bash
git clone <仓库地址> && cd RemoteControl/mcp && npm install
npm link   # 将 phone-rc-mcp 注册为全局命令
```

`npm link` 会根据 `package.json` 中的 `bin` 字段注册全局可执行命令，效果等同于 `npm install -g`。

> 如果不想使用 `npm link`，也可以在配置中直接指定脚本路径：
> ```json
> {
>   "command": "node",
>   "args": ["/你的路径/RemoteControl/mcp/index.js"],
>   "env": { ... }
> }
> ```

---

## 4. 各平台配置

### Antigravity IDE (Google)

Antigravity 内置 MCP Store，推荐使用 **远程 HTTP 直连**。

**方式一：远程 HTTP 直连（推荐）**

1. 打开编辑器侧边栏顶部的 **「...」** 菜单 → **MCP Store**。
2. 点击 **Manage MCP Servers** → **View raw config**。
3. 编辑配置文件 `~/.gemini/antigravity/mcp_config.json`：

```json
{
  "mcpServers": {
    "RCS-Phones": {
      "serverUrl": "https://rc.guokecs.com/mcp",
      "headers": {
        "Authorization": "Bearer rc_你的API_Key"
      }
    }
  }
}
```

保存后 Antigravity 会自动加载，RCS 工具即可在 Agent 对话中使用。

**使用示例：**

在 Antigravity 的 Agent 对话窗口中用自然语言指示即可，AI 会自动选择并调用对应工具：

| 你说的话 | AI 调用的工具 |
|---------|-------------|
| "列出当前所有在线设备" | `list_devices` |
| "获取一台 iOS 设备" | `get_device` (deviceType="iOS") |
| "回到主屏幕，然后截个图看看上面有什么应用" | `home` → `screenshot` |
| "在搜索框里输入 WiFi 并搜索" | `tap` → `input_text` |
| "向下滑动一下" | `swipe` |
| "看看哪些设备在线，但不要锁定它们" | `observe_devices` |
| "同时获取 3 台 Android 设备，全部截屏" | `get_devices` → `broadcast_command` |
| "用完了，释放所有设备" | `release_all_devices` |

> 截屏图片会直接显示在对话中，AI 会自动分析屏幕内容并决定下一步操作。不需要手动调用工具，用自然语言描述你想做什么就行。

**方式二：本地 stdio 连接**

需要先执行 `npm install -g phone-remote-control-mcp`：

```json
{
  "mcpServers": {
    "RCS-Phones": {
      "command": "phone-rc-mcp",
      "env": {
        "RC_API_KEY": "rc_你的API_Key",
        "RC_SERVER_HOST": "rc.guokecs.com",
        "RC_SERVER_PORT": "80"
      }
    }
  }
}
```

> **参考：** [Antigravity MCP 官方文档](https://antigravity.google/docs/mcp)

### Claude Desktop

Claude Desktop 目前仅支持 stdio 模式，需要先安装 `phone-remote-control-mcp`：

```bash
npm install -g phone-remote-control-mcp
```

编辑配置文件：
- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "RCS-Phones": {
      "command": "phone-rc-mcp",
      "env": {
        "RC_API_KEY": "rc_你的API_Key",
        "RC_SERVER_HOST": "rc.guokecs.com",
        "RC_SERVER_PORT": "80"
      }
    }
  }
}
```

保存后重启 Claude Desktop，侧栏出现工具图标即表示连接成功。

### Cursor IDE

在 Settings → MCP 中添加与 Claude Desktop 相同的 stdio 配置。

### Python 自定义 Agent（stdio 模式）

```bash
pip install mcp
```

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_PARAMS = StdioServerParameters(
    command="phone-rc-mcp",
    env={
        "RC_API_KEY": "rc_你的API_Key",
        "RC_SERVER_HOST": "rc.guokecs.com",
        "RC_SERVER_PORT": "80",
    },
)


async def main():
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            result = await session.call_tool("list_devices", {})
            print(result)

            result = await session.call_tool("get_device", {"deviceType": "iOS"})
            print(result)

            result = await session.call_tool("screenshot", {})
            print(result)

            await session.call_tool("release_device", {})


asyncio.run(main())
```

### Node.js 自定义 Agent（stdio 模式）

```javascript
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const transport = new StdioClientTransport({
    command: 'phone-rc-mcp',
    env: {
        RC_API_KEY: 'rc_你的API_Key',
        RC_SERVER_HOST: 'rc.guokecs.com',
        RC_SERVER_PORT: '80',
    },
});

const client = new Client({ name: 'my-agent', version: '1.0.0' });
await client.connect(transport);

const devices = await client.callTool({ name: 'list_devices', arguments: {} });
console.log(devices);

const acquired = await client.callTool({ name: 'get_device', arguments: { deviceType: 'iOS' } });
console.log(acquired);

await client.callTool({ name: 'release_device', arguments: {} });
```

---

## 5. 工具参考

### 5.1 设备发现

#### `list_devices`

列出你账号下所有可见设备及其状态。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| — | — | — | 无参数 |

**返回示例：**
```
ID: ABC-iPhone15-001
Type: iOS
Model: iPhone 15
OS: 17.2
Status: idle (Online)
Capabilities: tap, swipe, longPress, input, screenshot, home
```

---

### 5.2 设备获取与释放

#### `get_device`

获取一台空闲设备（自动锁定）。可按条件筛选。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceType` | string | 否 | `"iOS"` 或 `"Android"` |
| `deviceModel` | string | 否 | 型号模糊匹配，如 `"iPhone"`、`"Pixel"` |
| `osVersion` | string | 否 | 系统版本模糊匹配，如 `"17"`、`"14"` |

#### `acquire_device`

按设备 ID 精确锁定指定设备。如果设备已被他人锁定或离线，则返回失败。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | **是** | 设备的完整 ID |

> **提示：** 先用 `list_devices` 获取 ID，再用此工具锁定。

#### `get_devices`

批量获取多台空闲设备。实际获取数量可能少于请求数（受设备可用性和等级限制）。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `count` | number | 否 | 需要的设备数量（1-10，默认 1） |
| `deviceType` | string | 否 | 设备类型筛选 |
| `deviceModel` | string | 否 | 型号模糊匹配 |
| `osVersion` | string | 否 | 系统版本模糊匹配 |

#### `release_device`

释放指定设备。省略 `deviceId` 时释放池中第一台设备。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 要释放的设备 ID |

#### `release_all_devices`

一次性释放你当前持有的所有设备。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| — | — | — | 无参数 |

#### `list_pool`

查看你当前设备池中的所有设备。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| — | — | — | 无参数 |

---

### 5.3 设备观察

#### `observe_devices`

观察设备状态，**不锁定设备**，不影响其他用户使用。适合在获取设备前查看在线情况。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceIds` | string[] | 否 | 指定设备 ID。省略则查看所有可见设备 |
| `includeScreenshot` | boolean | 否 | 是否包含截屏（消耗较多 Token，建议不超过 3-5 台） |

---

### 5.4 批量广播

#### `broadcast_command`

向多台设备同时发送同一条指令。以任务形式异步执行，立即返回 `taskId`。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `command` | object | **是** | 指令对象（见下方说明） |
| `deviceIds` | string[] | 否 | 目标设备 ID 列表。省略则广播到池中所有设备 |
| `batchSize` | number | 否 | 每批设备数（默认 50） |

**command 对象支持的字段：**

```json
{
  "cmd": "tap | swipe | longPress | input | home | back | recent | keyevent | launchApp | killApp | openUrl | screenshot | saveMedia | lockOrientation | unlockOrientation",
  "x": 0.5,
  "y": 0.5,
  "startX": 0.0, "startY": 1.0,
  "endX": 0.0, "endY": 0.3,
  "duration": 0.3,
  "text": "Hello",
  "keyCode": 66,
  "packageName": "com.android.browser",
  "url": "https://example.com",
  "downloadUrl": "https://example.com/video.mp4",
  "mediaType": "video"
}
```

> **注意：** 广播模式下的坐标**不会**自动转换，需自行确保坐标值合理。

#### `check_broadcast`

查询广播任务的执行进度。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `taskId` | string | **是** | `broadcast_command` 返回的任务 ID |

---

### 5.5 设备控制指令

所有控制指令均支持可选的 `deviceId` 参数。省略时默认操作池中第一台设备。

**坐标一律使用归一化值（0.0=左/上，1.0=右/下）**，自动转换为像素。

#### `tap` — 点击

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `x` | number | **是** | X 坐标 (0.0~1.0) |
| `y` | number | **是** | Y 坐标 (0.0~1.0) |
| `deviceId` | string | 否 | 目标设备 ID |

#### `swipe` — 滑动

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `startX` | number | **是** | 起始 X (0.0~1.0) |
| `startY` | number | **是** | 起始 Y (0.0~1.0) |
| `endX` | number | **是** | 终止 X (0.0~1.0) |
| `endY` | number | **是** | 终止 Y (0.0~1.0) |
| `duration` | number | 否 | 滑动时长（秒），默认 0.3 |
| `deviceId` | string | 否 | 目标设备 ID |

#### `long_press` — 长按

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `x` | number | **是** | X 坐标 (0.0~1.0) |
| `y` | number | **是** | Y 坐标 (0.0~1.0) |
| `duration` | number | 否 | 按压时长（秒），默认 1.0 |
| `deviceId` | string | 否 | 目标设备 ID |

#### `input_text` — 输入文字

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `text` | string | **是** | 要输入的文本 |
| `deviceId` | string | 否 | 目标设备 ID |

#### `screenshot` — 截屏

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

返回图片数据（Base64 编码），AI 模型可直接查看。

#### `get_screen_info` — 获取屏幕尺寸

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

#### `home` — 返回主屏幕

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

#### `back` — 返回键（Android）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

#### `recent_apps` — 最近任务（Android）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

#### `key_event` — 按键事件（Android）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `keyCode` | number | **是** | Android KeyEvent 码（如 66=回车，67=删除，24=音量+） |
| `deviceId` | string | 否 | 目标设备 ID |

#### `launch_app` — 启动应用

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `packageName` | string | **是** | 应用包名（如 `com.android.browser`） |
| `deviceId` | string | 否 | 目标设备 ID |

#### `kill_app` — 强制停止应用

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `packageName` | string | **是** | 应用包名 |
| `deviceId` | string | 否 | 目标设备 ID |

#### `open_url` — 打开 URL

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | **是** | 完整 URL（需含 `http://` 或 `https://`） |
| `deviceId` | string | 否 | 目标设备 ID |

#### `set_language` — 切换系统语言

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `language` | string | **是** | BCP-47 语言标签（如 `"en"`、`"zh-Hans"`、`"ja"`） |
| `deviceId` | string | 否 | 目标设备 ID |

> 切换后设备 UI 会重启，等待约 10 秒。

#### `set_locale` — 切换系统区域

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `locale` | string | **是** | 区域标识（如 `"en_US"`、`"zh_CN"`、`"ja_JP"`） |
| `deviceId` | string | 否 | 目标设备 ID |

#### `set_language_and_locale` — 同时切换语言和区域

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `language` | string | 否 | BCP-47 语言标签 |
| `locale` | string | 否 | 区域标识 |
| `deviceId` | string | 否 | 目标设备 ID |

> 至少提供一个参数。同时设置可避免 UI 多次重启。

#### `save_media` — 保存媒体到相册

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `downloadUrl` | string | **是** | 媒体文件的下载链接（需设备可访问） |
| `mediaType` | string | **是** | 媒体类型：`"image"` 或 `"video"` |
| `deviceId` | string | 否 | 目标设备 ID |

#### `lock_orientation` — 锁定屏幕方向（iOS）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

> 强制锁定为竖屏（Portrait）。仅支持 iOS。

#### `unlock_orientation` — 解锁屏幕方向（iOS）

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `deviceId` | string | 否 | 目标设备 ID |

> 解除方向锁定。仅支持 iOS。

#### `wait` — 等待

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `seconds` | number | **是** | 等待秒数（0.5~30） |

---

## 6. 典型使用流程

### 单设备操控

```
1. list_devices          → 查看可用设备
2. get_device            → 自动获取一台空闲设备
3. screenshot            → 截屏查看当前界面
4. tap / swipe / input   → 执行操作
5. release_device        → 用完后释放
```

### 指定设备操控

```
1. list_devices          → 找到目标设备的 ID
2. acquire_device        → 按 ID 锁定指定设备
3. screenshot            → 查看界面
4. tap / input_text      → 操作
5. release_device        → 释放
```

### 多设备并行操控

```
1. list_devices          → 查看所有设备
2. get_devices (count=3) → 批量获取 3 台设备
3. list_pool             → 确认当前池中的设备
4. screenshot (deviceId="设备A") → 对 A 截屏
5. tap (deviceId="设备B")       → 对 B 执行点击
6. broadcast_command     → 向所有设备广播同一操作
7. check_broadcast       → 查看广播进度
8. release_all_devices   → 全部释放
```

### 设备观察（不锁定）

```
1. observe_devices                          → 查看所有设备在线状态
2. observe_devices (includeScreenshot=true) → 含截屏（注意 Token 消耗）
```

---

## 7. Python 完整示例：多设备巡检脚本

```python
import asyncio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER_PARAMS = StdioServerParameters(
    command="phone-rc-mcp",
    env={
        "RC_API_KEY": "rc_你的Key",
        "RC_SERVER_HOST": "rc.guokecs.com",
        "RC_SERVER_PORT": "80",
    },
)


async def device_patrol():
    """获取所有在线设备，逐台截屏检查。"""
    async with stdio_client(SERVER_PARAMS) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. 观察所有设备（不锁定）
            obs = await session.call_tool("observe_devices", {})
            print(f"[观察] {obs}")

            # 2. 批量获取 3 台 iOS 设备
            result = await session.call_tool(
                "get_devices", {"count": 3, "deviceType": "iOS"}
            )
            print(f"[获取] {result}")

            # 3. 查看设备池
            pool = await session.call_tool("list_pool", {})
            print(f"[设备池] {pool}")

            # 4. 查看当前池中的设备列表
            # pool 返回 JSON，解析出 device ID 列表
            # 此处简化演示，假设已知 ID
            device_ids = ["device-001", "device-002"]

            # 5. 对每台设备回到桌面并截屏
            for did in device_ids:
                await session.call_tool("home", {"deviceId": did})
                await session.call_tool("wait", {"seconds": 2})
                screen = await session.call_tool("screenshot", {"deviceId": did})
                print(f"[截屏] {did}: {screen.content[0].text[:100]}...")

            # 6. 全部释放
            await session.call_tool("release_all_devices", {})
            print("[释放] 所有设备已释放")


asyncio.run(device_patrol())
```

---

## 8. Node.js 完整示例：批量截图并分析

```javascript
import { Client } from '@modelcontextprotocol/sdk/client/index.js';
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js';

const transport = new StdioClientTransport({
    command: 'phone-rc-mcp',
    env: {
        RC_API_KEY: 'rc_你的Key',
        RC_SERVER_HOST: 'rc.guokecs.com',
        RC_SERVER_PORT: '80',
    },
});

async function batchScreenshot() {
    const client = new Client({ name: 'batch-screenshot', version: '1.0.0' });
    await client.connect(transport);

    // 获取 5 台 Android 设备
    const result = await client.callTool({
        name: 'get_devices',
        arguments: { count: 5, deviceType: 'Android' },
    });
    console.log('获取结果:', result.content[0].text);

    // 查看池
    const pool = await client.callTool({ name: 'list_pool', arguments: {} });
    console.log('设备池:', pool.content[0].text);

    // 广播：所有设备回到桌面
    const broadcast = await client.callTool({
        name: 'broadcast_command',
        arguments: { command: { cmd: 'home' } },
    });
    console.log('广播结果:', broadcast.content[0].text);

    // 等待 2 秒
    await client.callTool({ name: 'wait', arguments: { seconds: 2 } });

    // 广播截屏
    const ssBroadcast = await client.callTool({
        name: 'broadcast_command',
        arguments: { command: { cmd: 'screenshot' } },
    });
    const taskId = ssBroadcast.content[0].text.match(/Task ID: (\S+)/)?.[1];
    console.log('截屏任务:', taskId);

    // 轮询进度
    if (taskId) {
        let done = false;
        while (!done) {
            const status = await client.callTool({
                name: 'check_broadcast',
                arguments: { taskId },
            });
            const text = status.content[0].text;
            console.log(text);
            done = text.includes('DONE') || text.includes('FAILED') || text.includes('PARTIAL');
            if (!done) await new Promise(r => setTimeout(r, 3000));
        }
    }

    // 释放所有
    await client.callTool({ name: 'release_all_devices', arguments: {} });
    console.log('全部释放完成');
}

batchScreenshot().catch(console.error);
```

---

## 9. 环境变量说明

### 远程 HTTP 模式

无需环境变量，通过请求头传递 API Key：

| 参数 | 位置 | 说明 |
|------|------|------|
| `Authorization` | Header | `Bearer rc_你的API_Key` |
| `Mcp-Session-Id` | Header | 会话 ID（首次由服务器返回，后续请求需携带） |

### stdio 模式

| 变量 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| `RC_API_KEY` | **是** | — | 你的 API Key（`rc_` 开头） |
| `RC_SERVER_HOST` | 否 | `rc.guokecs.com` | 服务器地址 |
| `RC_SERVER_PORT` | 否 | `80` | 服务器端口 |

---

## 10. 本地调试（MCP Inspector）

使用官方 Inspector 工具可独立调试每个 MCP 工具的输入输出：

**远程 HTTP 模式：**

在 MCP Inspector 中输入 `https://rc.guokecs.com/mcp` 作为服务器地址，添加 `Authorization: Bearer rc_你的Key` 请求头即可连接。

**stdio 模式：**

```bash
RC_API_KEY="rc_你的Key" RC_SERVER_HOST="rc.guokecs.com" RC_SERVER_PORT="80" \
  npx @modelcontextprotocol/inspector phone-rc-mcp
```

打开终端输出的本地地址（通常 `http://localhost:5173`），点击 **Connect** → **Tools** 即可手动测试每个接口。

---

## 11. 设备状态说明

| 状态 | 含义 |
|------|------|
| **idle** | 空闲，可被任何用户锁定 |
| **busy** | 已被锁定，仅锁定者可操作 |

**自动释放触发条件：**
- 锁定超时（根据等级：60 分钟或无限制）
- MCP 客户端进程退出或断开连接
- 主动调用 `release_device` / `release_all_devices`

---

## 12. 常见问题

**Q: 提示 "Failed to get device: No idle device available"**
A: 当前没有空闲设备。可用 `observe_devices` 查看哪些设备在线且未被锁定，等待释放后再试。

**Q: 提示 "Tier limit exceeded"**
A: 你的设备池已满。调用 `list_pool` 查看当前持有设备，`release_device` 释放后再获取。

**Q: 截屏返回乱码或失败**
A: 确保设备处于唤醒状态且屏幕亮起。可先执行 `home` 指令唤醒设备。

**Q: Android 专用指令（back、recent_apps、key_event）在 iOS 上无响应**
A: 这些指令仅支持 Android 设备。iOS 设备请使用 `home`、`tap`、`swipe` 等通用指令。

**Q: 如何在多台设备间切换操作？**
A: 所有控制指令均支持 `deviceId` 参数。用 `list_pool` 查看池中设备，在指令中指定 `deviceId` 即可操作不同设备。
