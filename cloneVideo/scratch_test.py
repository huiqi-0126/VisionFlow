"""cloneVideo 冒烟测试 - 不调真实 API, 只验证模块可加载/数据结构/注册表/纯逻辑

运行: python scratch_test.py
全部通过 = 工程骨架完整可用, 接入真实 .env 凭据后即可跑完整流程。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

passed = 0
failed = 0


def ok(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}  {detail}")


print("== cloneVideo 冒烟测试 ==\n")

# ── 1. 配置可加载 ──────────────────────────────────────────
print("[1] 配置加载")
try:
    from config import get_settings
    s = get_settings()
    ok("get_settings 不抛异常", True)
    ok("default_duration=4 (素材固定4秒)", s.default_duration == "4", f"got {s.default_duration}")
    ok("default_video_model=seedance-2.0-fast", s.default_video_model == "seedance-2.0-fast")
    ok("default_image_model=gemini-3.0", s.default_image_model == "gemini-3.0")
    ok("min_shots=4", s.min_shots == 4)
    ok("max_shots=15", s.max_shots == 15)
    ok("clip_duration=4", s.clip_duration == 4)
    ok("data_dir 存在", s.data_dir.exists())
    ok("projects_dir 存在", s.projects_dir.exists())
except Exception as e:
    ok("get_settings", False, str(e))

# ── 2. 注册表加载 ──────────────────────────────────────────
print("\n[2] room_view_registry.json 加载")
reg_path = _PROJECT_ROOT / "core" / "prompts" / "room_view_registry.json"
reg: dict = {}
try:
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    ok("JSON 合法", True)
    rooms = reg.get("room_types", [])
    views = reg.get("viewpoints", [])
    cams = reg.get("camera_moves_4s", [])
    ok("room_types >= 8", len(rooms) >= 8, f"got {len(rooms)}")
    ok("viewpoints >= 5", len(views) >= 5, f"got {len(views)}")
    ok("camera_moves_4s >= 5", len(cams) >= 5, f"got {len(cams)}")
    # 校验每项都有 id/cn/en
    for key, items in [("room_types", rooms), ("viewpoints", views), ("camera_moves_4s", cams)]:
        for it in items:
            if not all(k in it for k in ("id", "en")):
                ok(f"{key} 项缺少 id/en: {it}", False, str(it))
                break
        else:
            ok(f"{key} 所有项含 id/en", True)
    # 校验 id 无重复且无中文混入
    for key, items in [("room_types", rooms), ("viewpoints", views), ("camera_moves_4s", cams)]:
        ids = [it["id"] for it in items]
        ok(f"{key} id 无重复", len(ids) == len(set(ids)), str(ids))
        bad = [i for i in ids if not i.replace("_", "").isascii()]
        ok(f"{key} id 全 ASCII", not bad, str(bad))
except Exception as e:
    ok("注册表加载", False, str(e))

# shot_planning_prompt 占位符
prompt = reg.get("shot_planning_prompt", "")
for ph in ["{num_shots}", "{style_descriptor_en}", "{room_options}", "{viewpoint_options}", "{camera_options}"]:
    ok(f"shot_planning_prompt 含占位符 {ph}", ph in prompt)

# ── 3. StyleProfile 序列化 ─────────────────────────────────
print("\n[3] StyleProfile 数据结构")
try:
    from core.style_analyzer import StyleProfile
    p = StyleProfile(
        overall_style_cn="现代简约",
        overall_style_en="modern minimalist",
        color_palette=["#E8DCC8", "#3A3A3A"],
        materials_cn=["胡桃木"],
        materials_en=["walnut wood"],
        style_descriptor_en="Modern minimalist interior, warm walnut wood, microcement walls.",
        reference_indices=[1, 3],
    )
    d = p.to_dict()
    ok("to_dict 含 style_descriptor_en", bool(d.get("style_descriptor_en")))
    ok("to_dict reference_indices", d.get("reference_indices") == [1, 3])
    p2 = StyleProfile.from_dict(d)
    ok("from_dict 往返一致", p2.overall_style_cn == "现代简约" and p2.style_descriptor_en == p.style_descriptor_en)
    ok("from_dict 容错(reference_indices 非数字)", StyleProfile.from_dict({"reference_indices": ["1", "x", 2]}).reference_indices == [1, 2])
except Exception as e:
    ok("StyleProfile", False, str(e))

# ── 4. aspect_ratio_from_resolution ────────────────────────
print("\n[4] 画幅推导 (尺寸跟随源视频)")
try:
    from core.video_analyzer import aspect_ratio_from_resolution as ar
    ok("1920x1080 → 16:9", ar(1920, 1080) == "16:9", ar(1920, 1080))
    ok("1080x1920 → 9:16 (竖屏)", ar(1080, 1920) == "9:16", ar(1080, 1920))
    ok("1080x1080 → 1:1", ar(1080, 1080) == "1:1", ar(1080, 1080))
    ok("1440x1080 → 4:3", ar(1440, 1080) == "4:3", ar(1440, 1080))
    ok("2560x1080 → 21:9 (超宽)", ar(2560, 1080) == "21:9", ar(2560, 1080))
    ok("0x0 兜底 16:9", ar(0, 0) == "16:9", ar(0, 0))
except Exception as e:
    ok("aspect_ratio", False, str(e))

# ── 4b. calc_num_shots 时长→镜头数 ────────────────────────
print("\n[4b] 镜头数推导 (按时长, 最多15)")
try:
    from core.shot_planner import calc_num_shots
    ok("3s → 4 (下限)", calc_num_shots(3) == 4, str(calc_num_shots(3)))
    ok("12s → 4", calc_num_shots(12) == 4, str(calc_num_shots(12)))
    ok("24s → 6", calc_num_shots(24) == 6, str(calc_num_shots(24)))
    ok("36s → 8", calc_num_shots(36) == 8, str(calc_num_shots(36)))
    ok("60s → 12", calc_num_shots(60) == 12, str(calc_num_shots(60)))
    ok("90s → 15 (上限)", calc_num_shots(90) == 15, str(calc_num_shots(90)))
    ok("600s → 15 (长视频封顶)", calc_num_shots(600) == 15)
    ok("0s → 4 (异常兜底)", calc_num_shots(0) == 4)
    ok("自定义上下限生效", calc_num_shots(100, min_shots=5, max_shots=10) == 10)
except Exception as e:
    ok("calc_num_shots", False, str(e))

# ── 5. ShotPlanner 兜底规划 (不调 LLM) ─────────────────────
print("\n[5] ShotPlanner 规则兜底")
try:
    from core.llm_client import LLMClient
    from core.shot_planner import ShotPlanner
    from core.style_analyzer import StyleProfile
    # 用一个会立刻抛异常的假 LLM, 强制走 fallback
    class FakeLLM(LLMClient):
        def __init__(self):
            pass
        def chat(self, *a, **k):
            raise RuntimeError("fake")
    planner = ShotPlanner(FakeLLM(), num_shots=8, clip_duration=4)
    profile = StyleProfile(style_descriptor_en="modern minimalist Japandi interior")
    shots = planner.plan(profile)
    ok("fallback 生成 8 个 shot", len(shots) == 8, f"got {len(shots)}")
    ok("每个 shot 有 image_prompt", all(s.get("image_prompt") for s in shots))
    ok("每个 shot 有 video_prompt", all(s.get("video_prompt") for s in shots))
    ok("每个 shot 含风格锁关键词", all("minimalist" in s["image_prompt"] for s in shots))
    ok("shot_id 1..8 连续", [s["shot_id"] for s in shots] == list(range(1, 9)))
    ok("duration=4", all(s.get("duration") == 4 for s in shots))
    ok("含 image_status/clip_status 初始字段", all("image_status" in s and "clip_status" in s for s in shots))
except Exception as e:
    ok("ShotPlanner fallback", False, str(e))

# ── 6. ProjectManager CRUD (临时目录) ──────────────────────
print("\n[6] ProjectManager 持久化")
try:
    from core.project_manager import ProjectManager
    with tempfile.TemporaryDirectory() as td:
        data_dir = Path(td) / "data"
        out_dir = Path(td) / "out"
        data_dir.mkdir(); out_dir.mkdir()
        mgr = ProjectManager(data_dir, out_dir)
        proj = mgr.create_project("/fake/video.mp4", (1920, 1080), "16:9")
        pid = proj["project_id"]
        ok("create_project 返回 8 位 id", len(pid) == 8)
        ok("含 reference_frames 字段", "reference_frames" in proj)
        ok("含 style_profile 字段", "style_profile" in proj)
        ok("含 shot_plan 字段", "shot_plan" in proj)
        ok("source_resolution 记录", proj["source_resolution"] == {"width": 1920, "height": 1080})
        # 持久化文件生成
        ok("projects.json 已写入", (data_dir / "projects.json").exists())
        # update
        mgr.update_project(pid, status="style_profiled", style_profile={"overall_style_cn": "侘寂"})
        got = mgr.get_project(pid) or {}
        ok("update 后 status", got["status"] == "style_profiled")
        ok("update 后 style_profile", got["style_profile"]["overall_style_cn"] == "侘寂")
        # update_shot
        mgr.update_project(pid, shot_plan=[{"shot_id": 1, "image_status": "pending"}])
        mgr.update_shot(pid, 1, image_status="done", image_path="/x.png")
        got2 = mgr.get_project(pid) or {}
        ok("update_shot 生效", got2["shot_plan"][0]["image_status"] == "done")
        # list / stats / delete
        ok("list_projects 返回 1", len(mgr.list_projects()) == 1)
        ok("get_stats total=1", mgr.get_stats()["total"] == 1)
        ok("delete 成功", mgr.delete_project(pid) is True)
        ok("delete 后 total=0", mgr.get_stats()["total"] == 0)
except Exception as e:
    ok("ProjectManager", False, str(e))

# ── 7. 关键模块可导入 ──────────────────────────────────────
print("\n[7] 关键模块导入")
for mod in [
    "core.llm_client", "core.media_api", "core.cos_client",
    "core.video_analyzer", "core.style_analyzer", "core.shot_planner",
    "core.project_manager", "workflow.pipeline",
]:
    try:
        __import__(mod)
        ok(f"import {mod}", True)
    except Exception as e:
        ok(f"import {mod}", False, str(e))

# ── 8. Pipeline 可实例化 ───────────────────────────────────
print("\n[8] CloneVideoPipeline 实例化")
try:
    from workflow.pipeline import CloneVideoPipeline
    pipe = CloneVideoPipeline()
    ok("CloneVideoPipeline() 构造成功", True)
    ok("含 video_analyzer", hasattr(pipe, "video_analyzer"))
    ok("含 style_analyzer", hasattr(pipe, "style_analyzer"))
    ok("含 project_mgr", hasattr(pipe, "project_mgr"))
except Exception as e:
    ok("CloneVideoPipeline 实例化", False, str(e))

# ── 汇总 ──────────────────────────────────────────────────
print(f"\n== 结果: {passed} 通过, {failed} 失败 ==")
sys.exit(1 if failed else 0)
