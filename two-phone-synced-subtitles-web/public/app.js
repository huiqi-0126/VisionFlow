const form = document.querySelector('#renderForm');
const runtimeCard = document.querySelector('#runtimeCard');
const runtimeTitle = document.querySelector('#runtimeTitle');
const runtimeMessage = document.querySelector('#runtimeMessage');
const runtimeLabel = document.querySelector('#runtimeLabel');
const fileInput = document.querySelector('#videoFile');
const dropZone = document.querySelector('#dropZone');
const fileTitle = document.querySelector('#fileTitle');
const fileMeta = document.querySelector('#fileMeta');
const textField = document.querySelector('#textField');
const subtitleText = document.querySelector('#subtitleText');
const textCount = document.querySelector('#textCount');
const submitButton = document.querySelector('#submitButton');
const formError = document.querySelector('#formError');
const emptyState = document.querySelector('#emptyState');
const progressState = document.querySelector('#progressState');
const progressTitle = document.querySelector('#progressTitle');
const progressMessage = document.querySelector('#progressMessage');
const resultSubtitle = document.querySelector('#resultSubtitle');
const videoState = document.querySelector('#videoState');
const resultVideo = document.querySelector('#resultVideo');
const downloadLink = document.querySelector('#downloadLink');
const logPanel = document.querySelector('#logPanel');
const logOutput = document.querySelector('#logOutput');

let capability = null;
let currentJobId = null;
let pollingTimer = null;

function bytesLabel(bytes) {
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function utf8Base64(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = '';
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index]);
  return btoa(binary);
}

function setRuntime(data) {
  capability = data;
  runtimeCard.className = `runtime-card ${data.ready ? 'ready' : 'blocked'}`;
  runtimeTitle.textContent = data.ready ? '本地渲染环境已就绪' : '当前环境不能执行原技能';
  runtimeMessage.textContent = data.message;
  runtimeLabel.textContent = data.runtime;
  submitButton.disabled = !data.ready;
}

async function loadCapability() {
  try {
    const response = await fetch('/api/capability');
    setRuntime(await response.json());
  } catch (error) {
    runtimeCard.className = 'runtime-card blocked';
    runtimeTitle.textContent = '无法连接本地服务';
    runtimeMessage.textContent = error.message;
  }
}

function updateSelectedFile() {
  const file = fileInput.files[0];
  if (!file) {
    fileTitle.textContent = '点击选择，或拖入视频';
    fileMeta.textContent = 'MP4、MOV 等可被 FFmpeg 读取的格式';
    dropZone.classList.remove('has-file');
    return;
  }
  fileTitle.textContent = file.name;
  fileMeta.textContent = `${bytesLabel(file.size)} · ${file.type || '视频文件'}`;
  dropZone.classList.add('has-file');
}

function selectedMode() {
  return form.elements.mode.value;
}

function updateMode() {
  const textMode = selectedMode() === 'text';
  textField.classList.toggle('hidden', !textMode);
  subtitleText.required = textMode;
}

function showStage(name) {
  emptyState.classList.toggle('hidden', name !== 'empty');
  progressState.classList.toggle('hidden', name !== 'progress');
  videoState.classList.toggle('hidden', name !== 'video');
}

function renderLogs(logs = []) {
  if (!logs.length) return;
  logPanel.classList.remove('hidden');
  logOutput.textContent = logs.map((item) => `[${item.source}] ${item.text}`).join('\n');
  logOutput.scrollTop = logOutput.scrollHeight;
}

async function parseResponse(response) {
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `请求失败（${response.status}）`);
  return data;
}

async function pollJob() {
  if (!currentJobId) return;
  try {
    const response = await fetch(`/api/jobs/${currentJobId}`);
    const job = await parseResponse(response);
    progressTitle.textContent = job.status === 'queued' ? '上传完成' : '正在生成视频';
    progressMessage.textContent = job.message;
    resultSubtitle.textContent = `${job.filename} · ${job.mode === 'auto' ? '自动字幕' : '精确文本'}`;
    renderLogs(job.logs);

    if (job.status === 'completed') {
      clearInterval(pollingTimer);
      pollingTimer = null;
      showStage('video');
      resultVideo.src = job.videoUrl;
      downloadLink.href = job.videoUrl;
      submitButton.disabled = false;
      submitButton.querySelector('span').textContent = '再次生成';
    } else if (job.status === 'failed') {
      clearInterval(pollingTimer);
      pollingTimer = null;
      progressTitle.textContent = '生成失败';
      progressState.classList.add('failed');
      formError.textContent = job.message;
      submitButton.disabled = false;
    }
  } catch (error) {
    formError.textContent = error.message;
  }
}

form.addEventListener('change', (event) => {
  if (event.target === fileInput) updateSelectedFile();
  if (event.target.name === 'mode') updateMode();
});

subtitleText.addEventListener('input', () => {
  textCount.textContent = subtitleText.value.length;
});

for (const eventName of ['dragenter', 'dragover']) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.add('dragging');
  });
}
for (const eventName of ['dragleave', 'drop']) {
  dropZone.addEventListener(eventName, (event) => {
    event.preventDefault();
    dropZone.classList.remove('dragging');
  });
}
dropZone.addEventListener('drop', (event) => {
  const [file] = event.dataTransfer.files;
  if (!file) return;
  const transfer = new DataTransfer();
  transfer.items.add(file);
  fileInput.files = transfer.files;
  updateSelectedFile();
});

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  formError.textContent = '';
  progressState.classList.remove('failed');
  const file = fileInput.files[0];
  const mode = selectedMode();

  if (!capability?.ready) return formError.textContent = capability?.message || '渲染环境未就绪。';
  if (!file) return formError.textContent = '请选择一个视频文件。';
  if (mode === 'text' && !subtitleText.value.trim()) return formError.textContent = '请填写精确字幕文本。';

  submitButton.disabled = true;
  submitButton.querySelector('span').textContent = '正在提交…';
  showStage('progress');
  progressTitle.textContent = '正在上传视频';
  progressMessage.textContent = `${file.name} · ${bytesLabel(file.size)}`;
  resultSubtitle.textContent = '任务正在进行，请保持页面打开';

  try {
    const response = await fetch('/api/jobs', {
      method: 'POST',
      headers: {
        'Content-Type': file.type || 'application/octet-stream',
        'X-Subtitle-Mode': mode,
        'X-Filename-Base64': utf8Base64(file.name),
        'X-Subtitle-Base64': utf8Base64(mode === 'text' ? subtitleText.value : ''),
      },
      body: file,
    });
    const job = await parseResponse(response);
    currentJobId = job.id;
    progressTitle.textContent = '上传完成';
    progressMessage.textContent = job.message;
    submitButton.querySelector('span').textContent = '生成中…';
    pollingTimer = setInterval(pollJob, 1000);
    await pollJob();
  } catch (error) {
    showStage('empty');
    formError.textContent = error.message;
    submitButton.disabled = !capability?.ready;
    submitButton.querySelector('span').textContent = '生成视频';
  }
});

updateMode();
loadCapability();
