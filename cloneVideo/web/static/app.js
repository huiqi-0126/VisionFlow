/* cloneVideo 前端交互：双模式上传(视频/图片) / 启动复刻 / 任务轮询 / 恢复 / 重试 / 删除 / 剪辑 */

(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);

  // ── 通用: 配置化一个上传模式（视频 / 图片 并列）─────────────
  function setupMode(cfg) {
    // cfg: { zone, input, info, fName, fSize, btn, job, jobText, jobBar, kind, url, buildBody }
    const zone = $(cfg.zone);
    const input = $(cfg.input);
    const btn = $(cfg.btn);
    let path = "";

    if (zone && input) {
      zone.addEventListener("click", () => input.click());
      zone.addEventListener("dragover", (e) => { e.preventDefault(); zone.classList.add("drag"); });
      zone.addEventListener("dragleave", () => zone.classList.remove("drag"));
      zone.addEventListener("drop", (e) => {
        e.preventDefault();
        zone.classList.remove("drag");
        if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
      });
      input.addEventListener("change", (e) => {
        if (e.target.files.length) handleFile(e.target.files[0]);
      });
    }

    async function handleFile(file) {
      const okType = cfg.kind === "image" ? file.type.startsWith("image/") : file.type.startsWith("video/");
      if (!okType) { toast(cfg.kind === "image" ? "请选择图片文件" : "请选择视频文件", "err"); return; }
      const fd = new FormData();
      fd.append("file", file);
      toast("上传中...");
      try {
        const resp = await fetch("/api/upload", { method: "POST", body: fd });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || "上传失败");
        path = data.path;
        $(cfg.info).style.display = "grid";
        $(cfg.fName).textContent = data.filename;
        $(cfg.fSize).textContent = data.size_mb + " MB";
        btn.disabled = false;
        toast("上传完成，可以开始", "ok");
      } catch (e) {
        toast(e.message, "err");
      }
    }

    if (btn) {
      btn.addEventListener("click", async () => {
        if (!path) { toast("请先上传文件", "err"); return; }
        btn.disabled = true;
        $(cfg.job).style.display = "block";
        $(cfg.jobText).textContent = "提交任务...";
        try {
          const resp = await fetch(cfg.url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(cfg.buildBody(path)),
          });
          const data = await resp.json();
          if (!resp.ok) throw new Error(data.error || "提交失败");
          pollJob(data.job_id, btn, cfg.jobText, cfg.jobBar);
        } catch (e) {
          toast(e.message, "err");
          btn.disabled = false;
        }
      });
    }
  }

  // 视频模式
  setupMode({
    zone: "#videoZone", input: "#videoInput", info: "#videoInfo",
    fName: "#vName", fSize: "#vSize", btn: "#videoBtn",
    job: "#videoJob", jobText: "#videoJobText", jobBar: "#videoJobBar",
    kind: "video", url: "/api/replicate",
    buildBody: (p) => ({ video_path: p }),
  });

  // 图片模式
  setupMode({
    zone: "#imageZone", input: "#imageInput", info: "#imageInfo",
    fName: "#iName", fSize: "#iSize", btn: "#imageBtn",
    job: "#imageJob", jobText: "#imageJobText", jobBar: "#imageJobBar",
    kind: "image", url: "/api/replicate_image",
    buildBody: (p) => ({
      image_path: p,
      num_rooms: parseInt(($("#numRooms") && $("#numRooms").value) || "7", 10) || 7,
    }),
  });

  // ── 任务轮询 ────────────────────────────────────────────
  function pollJob(jobId, btn, textSel, barSel) {
    const text = $(textSel);
    const bar = $(barSel);
    let elapsed = 0;
    const timer = setInterval(async () => {
      elapsed += 2;
      try {
        const resp = await fetch("/api/jobs/" + jobId);
        const job = await resp.json();
        bar.style.width = Math.min(95, elapsed * 1.2) + "%";
        text.textContent = `任务进行中... ${job.status} (已 ${elapsed}s)`;
        if (job.status === "done") {
          clearInterval(timer);
          bar.style.width = "100%";
          text.textContent = "完成！正在跳转...";
          toast("复刻完成", "ok");
          setTimeout(() => location.href = "/project/" + job.project_id, 1200);
        } else if (job.status === "failed") {
          clearInterval(timer);
          text.textContent = "失败：" + (job.error || "未知");
          toast("复刻失败：" + (job.error || ""), "err");
          btn.disabled = false;
        }
      } catch (e) { /* 网络抖动, 下次再试 */ }
    }, 2000);
  }

  // ── 恢复 / 重试 / 删除（全局）────────────────────────────
  window.resumeProject = async function (pid) {
    if (!confirm("恢复项目 " + pid + " 继续生成？")) return;
    toast("提交恢复任务...");
    try {
      const resp = await fetch("/api/projects/" + pid + "/resume", { method: "POST" });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "提交失败");
      toast("恢复任务已启动，请稍后刷新查看", "ok");
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

  window.retryShot = async function (pid, shotId) {
    if (!confirm("确定要重新生成该镜头素材吗？")) return;
    try {
      let res = await fetch(`/api/projects/${pid}/retry_shot/${shotId}`, { method: "POST" });
      if (res.ok) { toast("已提交重新生成任务", "ok"); setTimeout(() => location.reload(), 1000); }
      else toast("重新生成失败", "err");
    } catch (e) { toast("请求异常", "err"); }
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

  // ── 一键剪辑成片 (quick-cut) ──────────────────────────────
  window.selectAllClips = function (checked) {
    document.querySelectorAll(".clip-check").forEach((cb) => (cb.checked = checked));
  };

  window.quickCut = async function (pid) {
    const checked = Array.from(document.querySelectorAll(".clip-check:checked")).map((cb) => cb.value);
    const skillEl = document.querySelector("#qcSkill");
    const skill = skillEl ? skillEl.value : "quick-cut";
    const title = (document.querySelector("#qcTitle").value || "").trim();
    const accent = (document.querySelector("#qcAccent").value || "").trim();
    if (!checked.length) { toast("请先勾选要剪辑的素材", "err"); return; }
    if (!title) { toast("请填写成片标题", "err"); return; }

    const btn = document.querySelector("#qcBtn");
    const status = document.querySelector("#qcStatus");
    const text = document.querySelector("#qcText");
    const bar = document.querySelector("#qcBar");
    btn.disabled = true;
    status.style.display = "block";
    text.textContent = "提交剪辑任务 (" + skill + ")...";
    document.querySelector("#qcResult").innerHTML = "";

    try {
      const resp = await fetch("/api/projects/" + pid + "/quick_cut", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clip_paths: checked, title: title, accent: accent, skill: skill }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "提交失败");
      pollQuickCut(data.job_id, btn, text, bar);
    } catch (e) {
      toast(e.message, "err");
      btn.disabled = false;
    }
  };

  function pollQuickCut(jobId, btn, text, bar) {
    let elapsed = 0;
    const timer = setInterval(async () => {
      elapsed += 3;
      try {
        const r = await fetch("/api/jobs/" + jobId);
        const j = await r.json();
        bar.style.width = Math.min(95, elapsed * 4) + "%";
        text.textContent = "剪辑中... " + j.status + " (已 " + elapsed + "s)";
        if (j.status === "done") {
          clearInterval(timer);
          bar.style.width = "100%";
          text.textContent = "剪辑完成";
          const out = j.output_path || (j.result && j.result.output_path) || "";
          const box = document.querySelector("#qcResult");
          if (out) {
            const url = "/api/media?path=" + encodeURIComponent(out);
            box.innerHTML =
              '<div style="display:flex; flex-direction:column; align-items:center; margin-top:10px;">' +
              '<video src="' + url + '" controls style="max-height:1080px; max-width:100%; border-radius:8px; background:#000; box-shadow: 0 4px 16px rgba(0,0,0,0.4);"></video>' +
              '<div style="margin-top:10px;"><a class="btn" href="' + url + '" download>下载成片</a></div>' +
              '</div>';
          }
          toast("剪辑完成", "ok");
          btn.disabled = false;
        } else if (j.status === "failed") {
          clearInterval(timer);
          text.textContent = "剪辑失败";
          toast("剪辑失败: " + (j.error || ""), "err");
          btn.disabled = false;
        }
      } catch (e) { /* 网络抖动, 下次再试 */ }
    }, 3000);
  }
})();
