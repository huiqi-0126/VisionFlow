# 双手机同步字幕测试网页

这个网页只做参数收集与任务展示，真正的视频生成仍由相邻目录中的原技能执行：

```text
../two-phone-synced-subtitles/scripts/render_pipeline.py
```

## 能力边界

- 输入：一段包含视频流和音轨、时长不超过 45 秒的视频。
- 字幕：`auto`（本地语音识别）或 `text`（精确文本，时间由语音识别结果对齐）。
- 输出：固定 1080×1920、24fps、H.264/AAC MP4。
- 视觉：完整保留技能内置 master、双手机坐标、红色纹理、奶油色字幕与动画时序。
- 执行：单任务串行；服务端只调用一次原流水线，流水线自身最多做一次相同渲染重试。
- 环境：技能的离线构建仅支持 macOS Apple Silicon，并要求 `python3`、`ffmpeg`、`ffprobe`、`npm`。

## 启动

在 macOS Apple Silicon 上安装好上述命令后：

```bash
cd two-phone-synced-subtitles-web
npm start
```

浏览器打开 <http://127.0.0.1:4173>。页面会先调用 `/api/capability` 做环境检查；不兼容时不会上传或触发渲染。

可选环境变量：

- `PORT`：监听端口，默认 `4173`。
- `HOST`：监听地址，默认仅本机 `127.0.0.1`。
- `JOBS_DIR`：上传文件、渲染工作目录和结果文件的保存位置，默认 `.data/jobs`。

## 接口

- `GET /api/capability`：运行环境、固定规格与依赖状态。
- `POST /api/jobs`：请求体直接传视频字节；请求头传 `X-Subtitle-Mode`、Base64 UTF-8 编码的 `X-Filename-Base64` 和 `X-Subtitle-Base64`。
- `GET /api/jobs/:id`：查询状态和流水线日志。
- `GET /api/jobs/:id/video`：支持 Range 的 MP4 预览与下载。

服务默认只监听本机，未实现账号、鉴权和多用户隔离，因此不要直接暴露到公网。
