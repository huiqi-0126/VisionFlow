import {createReadStream, createWriteStream, existsSync} from 'node:fs';
import {mkdir, stat, unlink} from 'node:fs/promises';
import http from 'node:http';
import {once} from 'node:events';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {randomUUID} from 'node:crypto';
import {spawn, spawnSync} from 'node:child_process';

const WEB_DIR = path.dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = path.join(WEB_DIR, 'public');
const SKILL_DIR = path.resolve(WEB_DIR, '..', 'two-phone-synced-subtitles');
const PIPELINE = path.join(SKILL_DIR, 'scripts', 'render_pipeline.py');
const BUNDLED_BROWSER = path.join(
  SKILL_DIR,
  'assets',
  'browser',
  'macos-arm64',
  'chrome-headless-shell',
);
const JOBS_DIR = path.resolve(process.env.JOBS_DIR || path.join(WEB_DIR, '.data', 'jobs'));
const HOST = process.env.HOST || '127.0.0.1';
const PORT = Number(process.env.PORT || 4173);
const MAX_UPLOAD_BYTES = 1024 * 1024 * 1024;
const MAX_TEXT_LENGTH = 8000;

const jobs = new Map();
let activeJobId = null;

function commandAvailable(command) {
  const result = spawnSync(command, ['--version'], {
    encoding: 'utf8',
    stdio: 'ignore',
    windowsHide: true,
  });
  return !result.error && result.status === 0;
}

function capability() {
  const commands = {
    python3: commandAvailable('python3'),
    ffmpeg: commandAvailable('ffmpeg'),
    ffprobe: commandAvailable('ffprobe'),
    npm: commandAvailable('npm'),
  };
  const platformSupported = process.platform === 'darwin' && process.arch === 'arm64';
  const browserPresent = existsSync(BUNDLED_BROWSER);
  const pipelinePresent = existsSync(PIPELINE);
  const ready = platformSupported
    && browserPresent
    && pipelinePresent
    && Object.values(commands).every(Boolean);

  let message = '运行环境已就绪，可以生成视频。';
  if (!platformSupported) {
    message = `原技能仅支持 macOS Apple Silicon；当前是 ${process.platform}/${process.arch}。`;
  } else if (!browserPresent) {
    message = '技能捆绑的 Headless Shell 不存在。';
  } else if (!pipelinePresent) {
    message = '找不到技能渲染脚本。';
  } else {
    const missing = Object.entries(commands).filter(([, ok]) => !ok).map(([name]) => name);
    if (missing.length) message = `缺少运行依赖：${missing.join('、')}。`;
  }

  return {
    ready,
    message,
    runtime: `${process.platform}/${process.arch}`,
    commands,
    limits: {
      maxDurationSeconds: 45,
      output: '1080×1920 · 24fps · H.264/AAC',
      renderDeadlineMinutes: 20,
      maxUploadBytes: MAX_UPLOAD_BYTES,
    },
    activeJobId,
  };
}

function sendJson(response, statusCode, value) {
  const body = Buffer.from(JSON.stringify(value));
  response.writeHead(statusCode, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': body.length,
    'Cache-Control': 'no-store',
  });
  response.end(body);
}

function sendError(response, statusCode, message) {
  sendJson(response, statusCode, {error: message});
}

function decodeHeader(value, field) {
  if (!value || Array.isArray(value)) return '';
  try {
    return Buffer.from(value, 'base64').toString('utf8');
  } catch {
    throw new Error(`${field} 参数编码无效。`);
  }
}

function publicJob(job) {
  return {
    id: job.id,
    status: job.status,
    message: job.message,
    mode: job.mode,
    filename: job.filename,
    uploadedBytes: job.uploadedBytes,
    createdAt: job.createdAt,
    finishedAt: job.finishedAt || null,
    logs: job.logs,
    videoUrl: job.status === 'completed' ? `/api/jobs/${job.id}/video` : null,
  };
}

function appendLog(job, source, line) {
  const clean = line.trim();
  if (!clean) return;
  job.logs.push({time: new Date().toISOString(), source, text: clean});
  if (job.logs.length > 300) job.logs.splice(0, job.logs.length - 300);

  if (clean.startsWith('STATUS=')) job.message = clean.slice('STATUS='.length);
  if (clean.startsWith('ERROR=')) job.message = clean.slice('ERROR='.length);
  if (clean.startsWith('OUTPUT=')) job.outputPath = clean.slice('OUTPUT='.length);
}

function connectLines(stream, job, source) {
  let pending = '';
  stream.setEncoding('utf8');
  stream.on('data', (chunk) => {
    pending += chunk;
    const lines = pending.split(/\r?\n/);
    pending = lines.pop() || '';
    for (const line of lines) appendLog(job, source, line);
  });
  stream.on('end', () => appendLog(job, source, pending));
}

async function runJob(job) {
  job.status = 'running';
  job.message = '正在启动原技能流水线';
  const args = [
    PIPELINE,
    '--media', job.inputPath,
    '--mode', job.mode,
  ];
  if (job.mode === 'text') args.push('--text', job.text);
  args.push('--work-dir', job.renderDir);

  try {
    const child = spawn('python3', args, {
      cwd: WEB_DIR,
      env: process.env,
      shell: false,
      windowsHide: true,
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    job.pid = child.pid;
    connectLines(child.stdout, job, 'stdout');
    connectLines(child.stderr, job, 'stderr');

    const [code] = await once(child, 'close');
    const expectedOutput = path.join(job.renderDir, 'out', 'two-phone-synced-subtitles.mp4');
    if (code === 0 && existsSync(expectedOutput)) {
      job.status = 'completed';
      job.message = '视频生成并验证完成';
      job.outputPath = expectedOutput;
    } else {
      job.status = 'failed';
      job.message = job.message || `渲染进程退出，状态码 ${code}`;
    }
  } catch (error) {
    job.status = 'failed';
    job.message = error.message;
    appendLog(job, 'server', error.stack || error.message);
  } finally {
    job.finishedAt = new Date().toISOString();
    job.pid = null;
    if (activeJobId === job.id) activeJobId = null;
  }
}

async function receiveUpload(request, response) {
  const current = capability();
  if (!current.ready) return sendError(response, 409, current.message);
  if (activeJobId) return sendError(response, 409, '已有生成任务正在运行，请等待它完成。');

  const mode = request.headers['x-subtitle-mode'];
  const filename = decodeHeader(request.headers['x-filename-base64'], '文件名') || 'input.mp4';
  const text = decodeHeader(request.headers['x-subtitle-base64'], '字幕文本');
  const declaredLength = Number(request.headers['content-length'] || 0);

  if (!['auto', 'text'].includes(mode)) return sendError(response, 400, '字幕模式必须是 auto 或 text。');
  if (mode === 'text' && !text.trim()) return sendError(response, 400, '精确字幕模式必须填写字幕文本。');
  if (text.length > MAX_TEXT_LENGTH) return sendError(response, 400, `字幕文本不能超过 ${MAX_TEXT_LENGTH} 个字符。`);
  if (declaredLength > MAX_UPLOAD_BYTES) return sendError(response, 413, '上传文件超过 1 GiB 限制。');

  const id = randomUUID();
  const jobRoot = path.join(JOBS_DIR, id);
  const inputPath = path.join(jobRoot, 'input-video');
  const job = {
    id,
    status: 'uploading',
    message: '正在上传视频',
    mode,
    text,
    filename,
    inputPath,
    renderDir: path.join(jobRoot, 'render'),
    uploadedBytes: 0,
    createdAt: new Date().toISOString(),
    logs: [],
  };
  jobs.set(id, job);
  activeJobId = id;
  await mkdir(jobRoot, {recursive: false});

  const output = createWriteStream(inputPath, {flags: 'wx'});
  try {
    for await (const chunk of request) {
      job.uploadedBytes += chunk.length;
      if (job.uploadedBytes > MAX_UPLOAD_BYTES) throw new Error('上传文件超过 1 GiB 限制。');
      if (!output.write(chunk)) await once(output, 'drain');
    }
    output.end();
    await once(output, 'finish');
    if (job.uploadedBytes === 0) throw new Error('没有收到视频文件。');

    job.status = 'queued';
    job.message = '上传完成，等待渲染';
    sendJson(response, 202, publicJob(job));
    queueMicrotask(() => runJob(job));
  } catch (error) {
    output.destroy();
    job.status = 'failed';
    job.message = error.message;
    job.finishedAt = new Date().toISOString();
    if (activeJobId === id) activeJobId = null;
    try { await unlink(inputPath); } catch {}
    if (!response.headersSent) sendError(response, 400, error.message);
  }
}

function serveStatic(response, fileName, contentType) {
  const filePath = path.join(PUBLIC_DIR, fileName);
  stat(filePath).then((info) => {
    response.writeHead(200, {
      'Content-Type': contentType,
      'Content-Length': info.size,
      'Cache-Control': 'no-cache',
    });
    createReadStream(filePath).pipe(response);
  }).catch(() => sendError(response, 404, '页面资源不存在。'));
}

async function serveVideo(request, response, job) {
  if (job.status !== 'completed' || !job.outputPath || !existsSync(job.outputPath)) {
    return sendError(response, 404, '视频尚未生成。');
  }
  const info = await stat(job.outputPath);
  const range = request.headers.range;
  if (!range) {
    response.writeHead(200, {
      'Content-Type': 'video/mp4',
      'Content-Length': info.size,
      'Accept-Ranges': 'bytes',
    });
    return createReadStream(job.outputPath).pipe(response);
  }

  const match = /^bytes=(\d*)-(\d*)$/.exec(range);
  if (!match) {
    response.writeHead(416, {'Content-Range': `bytes */${info.size}`});
    return response.end();
  }
  const start = match[1] ? Number(match[1]) : 0;
  const end = match[2] ? Math.min(Number(match[2]), info.size - 1) : info.size - 1;
  if (start > end || start >= info.size) {
    response.writeHead(416, {'Content-Range': `bytes */${info.size}`});
    return response.end();
  }
  response.writeHead(206, {
    'Content-Type': 'video/mp4',
    'Content-Length': end - start + 1,
    'Content-Range': `bytes ${start}-${end}/${info.size}`,
    'Accept-Ranges': 'bytes',
  });
  createReadStream(job.outputPath, {start, end}).pipe(response);
}

const server = http.createServer(async (request, response) => {
  try {
    const url = new URL(request.url, `http://${request.headers.host || 'localhost'}`);
    if (request.method === 'GET' && url.pathname === '/') {
      return serveStatic(response, 'index.html', 'text/html; charset=utf-8');
    }
    if (request.method === 'GET' && url.pathname === '/styles.css') {
      return serveStatic(response, 'styles.css', 'text/css; charset=utf-8');
    }
    if (request.method === 'GET' && url.pathname === '/app.js') {
      return serveStatic(response, 'app.js', 'text/javascript; charset=utf-8');
    }
    if (request.method === 'GET' && url.pathname === '/api/capability') {
      return sendJson(response, 200, capability());
    }
    if (request.method === 'POST' && url.pathname === '/api/jobs') {
      return await receiveUpload(request, response);
    }

    const jobMatch = /^\/api\/jobs\/([0-9a-f-]+)(?:\/(video))?$/.exec(url.pathname);
    if (jobMatch) {
      const job = jobs.get(jobMatch[1]);
      if (!job) return sendError(response, 404, '找不到这个任务。');
      if (request.method === 'GET' && jobMatch[2] === 'video') return await serveVideo(request, response, job);
      if (request.method === 'GET' && !jobMatch[2]) return sendJson(response, 200, publicJob(job));
    }
    sendError(response, 404, '接口不存在。');
  } catch (error) {
    sendError(response, 500, error.message);
  }
});

await mkdir(JOBS_DIR, {recursive: true});
server.listen(PORT, HOST, () => {
  console.log(`Two Phone test page: http://${HOST}:${PORT}`);
  console.log(capability().message);
});
