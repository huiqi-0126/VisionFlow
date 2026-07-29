/* cloneVideo 前端交互：上传 / 启动复刻 / 任务轮询 / 恢复 / 删除 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  let selectedPath = "";

  // ── 上传区交互 ──────────────────────────────────────────
  const zone = $("#uploadZone");
  const fileInput = $("#fileInput");
  const startBtn = $("#startBtn");

  if (zone) {
    zone.addEventListener("click", () => fileInput.click());
    zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
    zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
    zone.addEventListener("drop", (e) => {
      e.preventDefault();
      zone.classList.remove("drag");
      if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
    });
    fileInput.addEventListener("change", (e) => {
      if (e.target.files.length) handleFile(e.target.files[0]);
    });
  }

  async function handleFile(file) {
    if (!file.type.startsWith("video/")) { toast("请选择视频文件", "err"); return; }
    const formData = new FormData();
    formData.append("file", file);
    toast("上传中...");
    try {
      const resp = await fetch("/api/upload", { method: "POST", body: formData });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "上传失败");
      selectedPath = data.path;
      $("#fileInfo").style.display = "grid";
      $("#fName").textContent = data.filename;
      $("#fSize").textContent = data.size_mb + " MB";
      $("#fPath").textContent = data.path;
      startBtn.disabled = false;
      toast("上传完成，可以开始复刻", "ok");
    } catch (e) {
      toast(e.message, "err");
    }
  }

  // ── 启动复刻 ────────────────────────────────────────────
  if (startBtn) {
    startBtn.addEventListener("click", async () => {
      if (!selectedPath) { toast("请先上传视频", "err"); return; }
      startBtn.disabled = true;
      $("#jobStatus").style.display = "block";
      $("#jobText").textContent = "提交任务...";
      try {
        const resp = await fetch("/api/replicate", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ video_path: selectedPath }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "提交失败");
        pollJob(data.job_id);
      } catch (e) {
        toast(e.message, "err");
        startBtn.disabled = false;
      }
    });
  }

  // ── 任务轮询 ────────────────────────────────────────────
  function pollJob(jobId) {
    const bar = $("#jobBar");
    const text = $("#jobText");
    let elapsed = 0;
    const timer = setInterval(async () => {
      elapsed += 2;
      try {
        const resp = await fetch("/api/jobs/" + jobId);
        const job = await resp.json();
        // 进度条按已用时间估算（风格分析+8图+8视频，粗略给个增长曲线）
        const pct = Math.min(95, elapsed * 1.2);
        bar.style.width = pct + "%";
        text.textContent = `任务进行中... ${job.status} (已 ${elapsed}s)`;
        if (job.status === "done") {
          clearInterval(timer);
          bar.style.width = "100%";
          text.textContent = "完成！正在刷新...";
          toast("复刻完成，" + (job.project_id || ""), "ok");
          setTimeout(() => location.href = "/project/" + job.project_id, 1200);
        } else if (job.status === "failed") {
          clearInterval(timer);
          text.textContent = "失败：" + (job.error || "未知");
          toast("复刻失败：" + (job.error || ""), "err");
          startBtn.disabled = false;
        }
      } catch (e) { /* 网络抖动, 下次再试 */ }
    }, 2000);
  }

  // ── 恢复 / 删除（全局, 详情页和列表都用）────────────────
  window.resumeProject = async function (pid) {
    if (!confirm("恢复项目 " + pid + " 继续生成？")) return;
    toast("提交恢复任务...");
    try {
      const resp = await fetch("/api/projects/" + pid + "/resume", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "提交失败");
      toast("恢复任务已启动，请稍后刷新查看", "ok");
      // 简单轮询, 完成后刷新当前页
      const timer = setInterval(async () => {
        try {
          const r = await fetch("/api/jobs/" + data.job_id);
          const j = await r.json();
          if (j.status === "done" || j.status === "failed") {
            clearInterval(timer);
            location.reload();
          }
        } catch (e) {}
      }, 3000);
    } catch (e) {
      toast(e.message, "err");
    }
  };

  window.deleteProject = async function (pid) {
    if (!confirm("删除项目 " + pid + "？此操作不可撤销。")) return;
    try {
      const resp = await fetch("/api/projects/" + pid, { method: "DELETE" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "删除失败");
      toast("已删除", "ok");
      setTimeout(() => location.reload(), 700);
    } catch (e) {
      toast(e.message, "err");
    }
  };

  // ── toast ───────────────────────────────────────────────
  function toast(msg, kind) {
    const t = document.createElement("div");
    t.className = "toast" + (kind ? " " + kind : "");
    t.textContent = msg;
    document.body.appendChild(t);
    setTimeout(() => { t.style.opacity = "0"; t.style.transition = "opacity .3s"; }, 2400);
    setTimeout(() => t.remove(), 2800);
  }
})();
