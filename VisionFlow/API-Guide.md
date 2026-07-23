# AI 图片/视频生成 API 调用指南

> 本服务提供与 OpenAI 格式兼容的 API，支持通过文本提示词生成图片和视频。支持多种模型、分辨率、宽高比、持续时间和音频选项。

---

## 目录

- [快速开始](#快速开始)
- [认证方式](#认证方式)
- [支持的模型](#支持的模型)
- [图片生成](#图片生成)
- [视频生成](#视频生成)
- [查询任务状态](#查询任务状态)
- [错误处理](#错误处理)
- [调用流程图](#调用流程图)
- [附录：状态说明](#附录状态说明)

---

## 快速开始

**Base URL**：请联系管理员获取

**第一步**：获取 API Key（格式：`dp_sk_...`）。

**第二步**：发起生成请求。

```bash
# 图片生成
curl -X POST https://{base-url}/v1/images/generations \
  -H "Authorization: Bearer dp_sk_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "一只橘猫在阳光下打盹", "model": "qwen-image", "size": "1080p", "aspectRatio": "16:9"}'

# 视频生成
curl -X POST https://{base-url}/v1/videos/generations \
  -H "Authorization: Bearer dp_sk_your_api_key" \
  -H "Content-Type: application/json" \
  -d '{"prompt": "日落时分的海边浪花", "model": "v6", "size": "1080p", "duration": "8", "aspectRatio": "16:9"}'
```

**第三步**：轮询查询结果（建议间隔 3~5 秒）。

```bash
curl https://{base-url}/v1/images/generations/TASK_xxx \
  -H "Authorization: Bearer dp_sk_your_api_key"
```

---

## 认证方式

所有请求需在 Header 中携带 API Key：

```
Authorization: Bearer dp_sk_your_api_key
```

| 项目 | 说明 |
|------|------|
| 认证方式 | Bearer Token |
| Token 前缀 | `dp_sk_` |
| 传递位置 | `Authorization` 请求头 |

**认证失败**时返回 HTTP 401：

```json
{
  "error": {
    "message": "Invalid API key",
    "type": "invalid_request_error"
  }
}
```

---

## 支持的模型

### 图片模型

调用路径：`POST /v1/images/generations`

| 模型标识 | 显示名称 | 最大分辨率 |
|----------|----------|------------|
| `qwen-image` | qwen-image | 1080p |
| `seedream-5.0-lite` | seedream-5.0-lite | 1800p |
| `seedream-4.5` | seedream-4.5 | 2160p |
| `gemini-3.1-flash` | Nano Banana 2 | 1080p |
| `gemini-3.0` | Nano Banana Pro | 2160p |
| `gemini-2.5-flash` | Nano Banana | 1080p |
| `gpt-image-2.0` | GPT Image 2 | 2160p（仅支持 1080p / 2160p） |

### 视频模型

调用路径：`POST /v1/videos/generations`

| 模型标识 | 显示名称                   | 最大分辨率 | 支持的持续时间      |
|----------|------------------------|------------|--------------|
| `v6` | PixVerse V6            | 1080p | 1~15 秒（任意整数） |
| `v5.6` | PixVerse V5.6          | 1080p | 1~8 秒（任意整数）  |
| `veo-3.1-standard` | veo-3.1-standard       | 1080p | 4、6、8 秒      |
| `grok-imagine` | grok-imagine           | 720p | 1~15 秒（任意整数） |
| `sora-2-pro` | sora-2-pro             | 1080p | 4、8、12 秒     |
| `pixverse-c1` | PixVerse C1            | 1080p | 1~15 秒（任意整数） |
| `seedance-2.0-fast` | Seedance 2.0 fast      | 720p | 4~15 秒（任意整数） |
| `seedance-2.0-standard` | Seedance 2.0 standard  | 1080p | 4~15 秒（任意整数） |

> 可用分辨率：`720p`、`1080p`、`1800p`、`2160p`。选择的分辨率不能超过该模型的最大分辨率，否则系统会自动降级到模型支持的最大分辨率。
>
> 注意：`gpt-image-2.0` 仅开放 `1080p` 和 `2160p` 两档定价；其支持的宽高比为 `1:1`、`16:9`、`9:16`、`4:3`、`3:4`、`3:2`、`2:3`、`2:1`、`1:2`、`21:9`（不支持 `5:4`、`4:5`）。

---

# 图片生成 `POST /v1/images/generations`

根据文本提示词异步生成图片。提交后立即返回任务 ID，需通过状态查询接口获取最终结果。

### 请求

```
POST /v1/images/generations
Authorization: Bearer dp_sk_xxx
Content-Type: application/json
```

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | ✅ | 生成描述，即你想生成的图片内容 |
| `model` | string | ✅ | 模型标识，如 `qwen-image`、`seedream-4.5` |
| `size` | string | ✅ | 分辨率，可选值：`720p`、`1080p`、`1800p`、`2160p`。不能超过模型支持的最大分辨率 |
| `aspectRatio` | string | ❌ | 宽高比，可选值：`1:1`、`16:9`、`9:16`、`4:3`、`3:4`、`5:4`、`4:5`、`3:2`、`2:3`、`21:9`。不传则由模型自行决定 |
| `pic` | string | ❌ | 参考图片，用于图生图。支持以下格式：① 图片 URL（`https://` 或 `https://` 开头）；② Base64 编码（`data:image/xxx;base64,...` 格式或纯 Base64 字符串）。传入非图片内容时将被忽略 |

**请求示例**（文生图）：

```json
{
  "prompt": "一只橘猫在阳光下打盹",
  "model": "qwen-image",
  "size": "1080p",
  "aspectRatio": "16:9"
}
```

**请求示例**（图生图）：

```json
{
  "prompt": "将这只猫变成油画风格",
  "model": "qwen-image",
  "size": "1080p",
  "aspectRatio": "16:9",
  "pic": "https://example.com/cat.jpg"
}
```

### 响应

```json
{
  "id": "TASK_20260508150000_abc123",
  "created": 1746681600,
  "data": [
    {
      "url": null,
      "revised_prompt": "一只橘猫在阳光下打盹"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 任务编号，用于后续查询状态 |
| `created` | integer | 创建时间（Unix 时间戳） |
| `data` | array | 数据列表，异步模式下 `url` 初始为 `null` |
| `data[].url` | string \| null | 结果图片 URL，生成完成后才有值 |
| `data[].revised_prompt` | string | 原始提示词 |

> ⚠️ 图片生成是**异步**的，响应中 `url` 为 `null`。请使用返回的 `id` 轮询查询结果。

### cURL 示例

**文生图**：

```bash
curl -X POST https://{base-url}/v1/images/generations \
  -H "Authorization: Bearer dp_sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "一只橘猫在阳光下打盹",
    "model": "qwen-image",
    "size": "1080p",
    "aspectRatio": "16:9"
  }'
```

**图生图**：

```bash
curl -X POST https://{base-url}/v1/images/generations \
  -H "Authorization: Bearer dp_sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "将这只猫变成油画风格",
    "model": "qwen-image",
    "size": "1080p",
    "aspectRatio": "16:9",
    "pic": "https://example.com/cat.jpg"
  }'
```

### Python 示例

```python
import requests

BASE_URL = "https://{base-url}"
API_KEY = "dp_sk_xxx"

# 文生图
response = requests.post(
    f"{BASE_URL}/v1/images/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "一只橘猫在阳光下打盹",
        "model": "qwen-image",
        "size": "1080p",
        "aspectRatio": "16:9",
    },
)
result = response.json()
task_id = result["id"]
print(f"任务已提交，ID: {task_id}")

# 图生图（使用参考图片URL）
response = requests.post(
    f"{BASE_URL}/v1/images/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "将这只猫变成油画风格",
        "model": "qwen-image",
        "size": "1080p",
        "aspectRatio": "16:9",
        "pic": "https://example.com/cat.jpg",
    },
)

# 图生图（使用本地图片Base64）
import base64
with open("cat.jpg", "rb") as f:
    pic_base64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
response = requests.post(
    f"{BASE_URL}/v1/images/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "将这只猫变成油画风格",
        "model": "qwen-image",
        "size": "1080p",
        "aspectRatio": "16:9",
        "pic": pic_base64,
    },
)
```

---

# 视频生成 `POST /v1/videos/generations`

根据文本提示词异步生成视频。提交后立即返回任务 ID，需通过状态查询接口获取最终结果。

### 请求

```
POST /v1/videos/generations
Authorization: Bearer dp_sk_xxx
Content-Type: application/json
```

**请求体**：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `prompt` | string | ✅ | 生成描述，即你想生成的视频内容 |
| `model` | string | ✅ | 模型标识，如 `v6`、`sora-2-pro` |
| `size` | string | ✅ | 分辨率，可选值：`720p`、`1080p`、`1800p`、`2160p`。不能超过模型支持的最大分辨率 |
| `duration` | string | ✅ | 持续时间（秒），如 `"8"` 表示 8 秒。需在模型支持的范围内 |
| `audio` | boolean | ❌ | 是否带声音，`true` 表示生成带音频的视频。不传则不带声音 |
| `aspectRatio` | string | ❌ | 宽高比，可选值：`1:1`、`16:9`、`9:16`、`4:3`、`3:4`、`5:4`、`4:5`、`3:2`、`2:3`、`21:9`。不传则由模型自行决定 |
| `pic` | string | ❌ | 参考图片，用于图生视频。支持以下格式：① 图片 URL（`https://` 或 `https://` 开头）；② Base64 编码（`data:image/xxx;base64,...` 格式或纯 Base64 字符串）。传入非图片内容时将被忽略 |
| `pic2` | string | ❌ | 第二张参考图片，用于多图模式。格式与 `pic` 相同（图片 URL 或 Base64 编码）。传入两张图时将启用首尾帧/参考图模式 |
| `pics` | string[] | ❌ | 更多参考图片（第3张起），元素格式与 `pic` 相同。与 `pic`/`pic2` 合并为完整图片列表，最多共 7 张（`seedance-2.0` 模型最多 9 张）。传入 2 张及以上图片时启用多图模式 |
| `videoType` | string | ❌ | 多图模式选择，仅在传入 2 张图片时生效。`"0"` = 首尾帧模式（默认），`"1"` = 参考图模式。仅传一张图时忽略此参数；**超过 2 张图时强制按参考图模式处理** |

**请求示例**（文生视频）：

```json
{
  "prompt": "日落时分的海边浪花",
  "model": "v6",
  "size": "1080p",
  "duration": "8",
  "audio": true,
  "aspectRatio": "16:9"
}
```

**请求示例**（图生视频）：

```json
{
  "prompt": "让画面中的海浪缓缓涌动",
  "model": "v6",
  "size": "1080p",
  "duration": "8",
  "aspectRatio": "16:9",
  "pic": "https://example.com/seaside.jpg"
}
```

**请求示例**（首尾帧模式）：

```json
{
  "prompt": "从白天过渡到黑夜的城市天际线",
  "model": "v6",
  "size": "1080p",
  "duration": "8",
  "aspectRatio": "16:9",
  "pic": "https://example.com/day.jpg",
  "pic2": "https://example.com/night.jpg",
  "videoType": "0"
}
```

**请求示例**（参考图模式）：

```json
{
  "prompt": "以指定风格生成视频",
  "model": "v6",
  "size": "1080p",
  "duration": "8",
  "aspectRatio": "16:9",
  "pic": "https://example.com/style.jpg",
  "pic2": "https://example.com/content.jpg",
  "videoType": "1"
}
```

**请求示例**（3张及以上图片 · 参考图模式）：

```json
{
  "prompt": "清晨、正午、黄昏三个时段的城市主角",
  "model": "v6",
  "size": "1080p",
  "duration": "8",
  "aspectRatio": "16:9",
  "pic": "https://example.com/person.jpg",
  "pic2": "https://example.com/city.jpg",
  "pics": ["https://example.com/style.jpg"],
  "videoType": "1"
}
```

> `pics` 为数组，与 `pic`/`pic2` 合并后按顺序作为完整图片列表，最多共 7 张（`seedance-2.0` 为 9 张）。2 张图片时按 `videoType` 选择首尾帧或参考图模式；**超过 2 张时仅支持参考图模式**（无论 `videoType` 传何值都按参考图模式处理）。

### 响应

```json
{
  "id": "TASK_20260508150100_def456",
  "status": "processing",
  "url": null,
  "created": 1746681660
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 任务编号，用于后续查询状态 |
| `status` | string | 任务状态，初始为 `processing` |
| `url` | string \| null | 结果视频 URL，生成完成后才有值 |
| `created` | integer | 创建时间（Unix 时间戳） |

### cURL 示例

**文生视频**：

```bash
curl -X POST https://{base-url}/v1/videos/generations \
  -H "Authorization: Bearer dp_sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "日落时分的海边浪花",
    "model": "v6",
    "size": "1080p",
    "duration": "8",
    "audio": true,
    "aspectRatio": "16:9"
  }'
```

**图生视频**：

```bash
curl -X POST https://{base-url}/v1/videos/generations \
  -H "Authorization: Bearer dp_sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "让画面中的海浪缓缓涌动",
    "model": "v6",
    "size": "1080p",
    "duration": "8",
    "aspectRatio": "16:9",
    "pic": "https://example.com/seaside.jpg"
  }'
```

# 首尾帧模式
curl -X POST https://{base-url}/v1/videos/generations \
  -H "Authorization: Bearer dp_sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "从白天过渡到黑夜的城市天际线",
    "model": "v6",
    "size": "1080p",
    "duration": "8",
    "aspectRatio": "16:9",
    "pic": "https://example.com/day.jpg",
    "pic2": "https://example.com/night.jpg",
    "videoType": "0"
  }'

# 参考图模式
curl -X POST https://{base-url}/v1/videos/generations \
  -H "Authorization: Bearer dp_sk_xxx" \
  -H "Content-Type: application/json" \
  -d '{
    "prompt": "以指定风格生成视频",
    "model": "v6",
    "size": "1080p",
    "duration": "8",
    "aspectRatio": "16:9",
    "pic": "https://example.com/style.jpg",
    "pic2": "https://example.com/content.jpg",
    "videoType": "1"
  }'

### Python 示例

```python
import requests

BASE_URL = "https://{base-url}"
API_KEY = "dp_sk_xxx"

# 文生视频
response = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "日落时分的海边浪花",
        "model": "v6",
        "size": "1080p",
        "duration": "8",
        "audio": True,
        "aspectRatio": "16:9",
    },
)
result = response.json()
task_id = result["id"]
print(f"任务已提交，ID: {task_id}")

# 图生视频（使用参考图片URL）
response = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "让画面中的海浪缓缓涌动",
        "model": "v6",
        "size": "1080p",
        "duration": "8",
        "aspectRatio": "16:9",
        "pic": "https://example.com/seaside.jpg",
    },
)

# 图生视频（使用本地图片Base64）
import base64
with open("seaside.jpg", "rb") as f:
    pic_base64 = "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
response = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "让画面中的海浪缓缓涌动",
        "model": "v6",
        "size": "1080p",
        "duration": "8",
        "aspectRatio": "16:9",
        "pic": pic_base64,
    },
)

# 首尾帧模式（使用两张参考图片）
response = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "从白天过渡到黑夜的城市天际线",
        "model": "v6",
        "size": "1080p",
        "duration": "8",
        "aspectRatio": "16:9",
        "pic": "https://example.com/day.jpg",
        "pic2": "https://example.com/night.jpg",
        "videoType": "0",
    },
)

# 参考图模式（使用两张参考图片）
response = requests.post(
    f"{BASE_URL}/v1/videos/generations",
    headers={"Authorization": f"Bearer {API_KEY}"},
    json={
        "prompt": "以指定风格生成视频",
        "model": "v6",
        "size": "1080p",
        "duration": "8",
        "aspectRatio": "16:9",
        "pic": "https://example.com/style.jpg",
        "pic2": "https://example.com/content.jpg",
        "videoType": "1",
    },
)
```

---

## 查询任务状态

图片和视频共用相同的状态查询模式，仅路径不同。

### 图片状态查询

```
GET /v1/images/generations/{task_id}
Authorization: Bearer dp_sk_xxx
```

### 视频状态查询

```
GET /v1/videos/generations/{task_id}
Authorization: Bearer dp_sk_xxx
```

### 响应

```json
{
  "id": "TASK_20260508150000_abc123",
  "status": "success",
  "url": "https://result-cdn.example.com/output.mp4",
  "created": 1746681600
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | string | 任务编号 |
| `status` | string | 任务状态，见下表 |
| `url` | string \| null | 生成成功时返回结果文件 URL |
| `created` | integer \| null | 创建时间（Unix 时间戳） |

**status 取值**：

| 状态 | 说明 |
|------|------|
| `processing` | 处理中，请继续轮询 |
| `success` | 生成成功，`url` 字段包含结果地址 |
| `failed` | 生成失败 |

> 💡 建议轮询间隔 **3~5 秒**。当 `status` 为 `success` 或 `failed` 时停止轮询。

### 轮询示例（Python）

```python
import time
import requests

BASE_URL = "https://{base-url}"
API_KEY = "dp_sk_xxx"
TASK_ID = "TASK_20260508150000_abc123"

while True:
    response = requests.get(
        f"{BASE_URL}/v1/images/generations/{TASK_ID}",
        headers={"Authorization": f"Bearer {API_KEY}"},
    )
    result = response.json()

    if result["status"] == "success":
        print(f"生成成功！结果地址: {result['url']}")
        break
    elif result["status"] == "failed":
        print("生成失败")
        break
    else:
        print(f"状态: {result['status']}，等待中...")
        time.sleep(3)
```

---

## 错误处理

### 错误响应格式

```json
{
  "error": {
    "message": "错误描述信息",
    "type": "invalid_request_error"
  }
}
```

### 常见错误码

| HTTP 状态码 | 错误码 | 说明 | 处理建议 |
|-------------|--------|------|----------|
| 401 | — | API Key 无效或已过期 | 检查 API Key 是否正确 |
| 402 | 10001 | 用户余额不足 | 请充值后重试 |
| 200 | 10003 | 任务不存在 | 检查任务 ID 是否正确 |
| 200 | 10005 | 无可用服务资源 | 请稍后重试 |
| 200 | 20001 | 扣费规则未配置 | 联系管理员 |
| 200 | 30001 | 模型配置不存在 | 检查 model 参数是否正确 |
| 500 | — | 服务器内部错误 | 请稍后重试或联系管理员 |

### 完整错误码参考

| 错误码 | 常量名 | 说明 |
|--------|--------|------|
| 10001 | `INSUFFICIENT_BALANCE` | 用户余额不足 |
| 10002 | `FREEZE_CONFLICT` | 积分冻结冲突（并发导致） |
| 10003 | `TASK_NOT_FOUND` | 任务不存在 |
| 10004 | `TASK_ALREADY_DONE` | 任务已完成或已取消 |
| 10005 | `NO_AVAILABLE_EMAIL` | 无可用服务资源 |
| 10006 | `PIXVERSE_API_ERROR` | 底层生成服务调用失败 |
| 10007 | `TOKEN_INVALID` | Token 无效或已过期 |
| 20001 | `CREDIT_RULE_NOT_CONFIGURED` | 扣费规则未配置 |
| 20002 | `SYNC_TASK_RUNNING` | 同步任务进行中 |
| 30001 | `MODEL_NOT_FOUND` | 模型配置不存在 |

---

## 调用流程图

### 图片生成完整流程

```
┌──────────────────────────────────────────────────────────────────────┐
│                        图片生成调用流程                                │
└──────────────────────────────────────────────────────────────────────┘

  客户端                          API 服务                         说明
  ──────                          ────────                         ────
    │                                │
    │  POST /v1/images/generations   │
    │  {prompt, model, size,         │
    │   aspectRatio}                 │
    │───────────────────────────────>│
    │                                │   冻结积分 → 下发任务
    │  {id, created, data}           │
    │<───────────────────────────────│   ⚡ 立即返回任务 ID
    │                                │
    │                                │
    │  GET /v1/images/generations/{id}                      轮询
    │───────────────────────────────>│
    │  {status:"processing",url:null}│
    │<───────────────────────────────│   ⏳ 仍在生成
    │                                │
    │         ··· (间隔 3~5 秒) ···  │
    │                                │
    │  GET /v1/images/generations/{id}
    │───────────────────────────────>│
    │  {status:"success",            │
    │   url:"https://...xxx.png"}    │
    │<───────────────────────────────│   ✅ 生成完成，获取结果
    │                                │
    │  下载/展示图片                   │
    │  GET {url}                     │
    │───────────────────────────────>│   CDN / OSS
    │<───────────────────────────────│   返回图片文件
    │                                │
```

### 视频生成完整流程

```
┌──────────────────────────────────────────────────────────────────────┐
│                        视频生成调用流程                                │
└──────────────────────────────────────────────────────────────────────┘

  客户端                          API 服务                         说明
  ──────                          ────────                         ────
    │                                │
    │  POST /v1/videos/generations   │
    │  {prompt, model, size,         │
    │   duration, audio,             │
    │   aspectRatio}                 │
    │───────────────────────────────>│
    │                                │   冻结积分 → 下发任务
    │  {id, status:"processing"}     │
    │<───────────────────────────────│   ⚡ 立即返回任务 ID
    │                                │
    │                                │
    │  GET /v1/videos/generations/{id}                      轮询
    │───────────────────────────────>│
    │  {status:"processing",url:null}│
    │<───────────────────────────────│   ⏳ 仍在生成
    │                                │
    │         ··· (间隔 3~5 秒) ···  │
    │                                │
    │  GET /v1/videos/generations/{id}
    │───────────────────────────────>│
    │  {status:"success",            │
    │   url:"https://...xxx.mp4"}    │
    │<───────────────────────────────│   ✅ 生成完成，获取结果
    │                                │
    │  下载/播放视频                   │
    │  GET {url}                     │
    │───────────────────────────────>│   CDN / OSS
    │<───────────────────────────────│   返回视频文件
    │                                │
```

### 积分流转示意

```
  发起请求           生成中            生成完成
  ────────          ──────           ────────

  ┌─────────┐                        ┌─────────┐
   │ 可用积分 │ ──── 冻结 ────>        │ 冻结积分 │
  │   1000  │      -10              │    10   │
  │         │ <─── 退回 ────        │         │
  └─────────┘   (仅失败时)          └─────────┘
                                    │
                              成功 ──┼── 确认扣减
                              失败 ──┼── 退回积分
                                    │
```

> 💡 请求发起时系统会**冻结**对应积分；生成成功后正式扣减，失败则自动退回。

---

## 附录：状态说明

### 任务状态流转

```
  提交请求        排队中         处理中
  ────────      ──────         ──────

  ┌────────┐    ┌────────┐    ┌────────────┐
  │ pending │───>│ pending │───>│ processing │
  └────────┘    └────────┘    └────────────┘
                                   │     │
                              ┌────┘     └────┐
                              ▼               ▼
                        ┌────────┐      ┌────────┐
                        │ success│      │ failed │
                        └────────┘      └────────┘
```

---

## 常见问题

**Q: 生成需要多长时间？**
A: 图片通常 10~30 秒，视频通常 30~120 秒，具体取决于内容复杂度和当前负载。

**Q: 积分不足时会怎样？**
A: 请求会被拒绝，返回余额不足错误（错误码 10001）。请充值后重试。

**Q: 生成失败会扣费吗？**
A: 不会。生成失败时冻结的积分会自动退回到您的账户。

**Q: 结果 URL 有效期是多久？**
A: 请在获取结果后及时下载保存，URL 可能会在一段时间后失效。

**Q: 如何选择分辨率？**
A: 通过 `size` 参数指定，如 `"1080p"`。不能超过模型支持的最大分辨率，否则系统会自动降级。

**Q: 视频的持续时间怎么指定？**
A: 通过 `duration` 参数指定秒数（字符串格式），如 `"8"` 表示 8 秒。需在模型支持的范围内。

**Q: 视频可以带声音吗？**
A: 可以。设置 `"audio": true` 即可生成带音频的视频。默认不带声音。

**Q: 如何使用图生图 / 图生视频？**
A: 在请求体中传入 `pic` 参数，提供一张参考图片。支持两种格式：① 图片 URL（`https://` 或 `https://` 开头），服务端直接使用原 URL；② Base64 编码（推荐 `data:image/xxx;base64,...` 格式，也支持纯 Base64 字符串），服务端会自动解码保存为临时文件。传入非图片内容时该参数会被忽略，等同于未传。

---

> 📧 如有问题，请联系技术支持。
