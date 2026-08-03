"""cloneVideo 冒烟测试 - 不调真实 API, 只验证模块可加载/数据结构/注册表/纯逻辑

新流程(真实帧风格统一改造)后的测试集:
  抽N帧 → 分析 → 画幅 → AI推荐统一目标风格 → 清理帧(去人/字)
  → 基底帧+运镜+改造prompt → 风格改造 → 4s视频

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


print("== cloneVideo 冒烟测试 (真实帧风格统一改造) ==\n")

# ── 1. 配置可加载 ──────────────────────────────────────────
print("[1] 配置加载")
try:
    from config import get_settings
    s = get_settings()
    ok("get_settings 不抛异常", True)
    ok("default_duration=4 (素材固定4秒)", s.default_duration == "4", f"got {s.default_duration}")
    ok("default_video_model=seedance-2.0-fast", s.default_video_model == "seedance-2.0-fast")
    ok("default_image_model=gemini-3.0", s.default_image_model == "gemini-3.0")
    ok("min_shots=8 (至少8张)", s.min_shots == 8)
    ok("max_shots=15 (最多15张)", s.max_shots == 15)
    ok("clip_duration=4", s.clip_duration == 4)
    ok("data_dir 存在", s.data_dir.exists())
    ok("projects_dir 存在", s.projects_dir.exists())
except Exception as e:
    ok("get_settings", False, str(e))

# ── 2. 注册表加载 (新流程只用 camera_moves_4s) ─────────────
print("\n[2] room_view_registry.json 加载")
reg_path = _PROJECT_ROOT / "core" / "prompts" / "room_view_registry.json"
reg: dict = {}
try:
    reg = json.loads(reg_path.read_text(encoding="utf-8"))
    ok("JSON 合法", True)
    cams = reg.get("camera_moves_4s", [])
    ok("camera_moves_4s >= 5 (运镜库)", len(cams) >= 5, f"got {len(cams)}")
    for it in cams:
        if not all(k in it for k in ("id", "en")):
            ok(f"camera_move 项缺 id/en: {it}", False, str(it))
            break
    else:
        ok("camera_moves_4s 所有项含 id/en", True)
    ids = [it["id"] for it in cams]
    ok("camera_moves_4s id 无重复", len(ids) == len(set(ids)), str(ids))
    bad = [i for i in ids if not i.replace("_", "").isascii()]
    ok("camera_moves_4s id 全 ASCII", not bad, str(bad))
except Exception as e:
    ok("注册表加载", False, str(e))

# ── 3. StyleProfile 数据结构 (原风格 + 目标风格) ───────────
print("\n[3] StyleProfile 数据结构")
try:
    from core.style_analyzer import StyleProfile
    p = StyleProfile(
        original_style_cn="北欧",
        original_style_en="scandinavian",
        overall_style_cn="侘寂",
        overall_style_en="wabi-sabi",
        color_palette=["#E8DCC8", "#3A3A3A"],
        style_descriptor_en="Wabi-sabi interior, raw plaster walls, muted earthy palette.",
    )
    d = p.to_dict()
    ok("to_dict 含 original_style", d.get("original_style_en") == "scandinavian")
    ok("to_dict 含目标风格", d.get("overall_style_en") == "wabi-sabi")
    ok("to_dict 含 style_descriptor_en", bool(d.get("style_descriptor_en")))
    # from_dict 兼容 LLM 返回的 target_style_* 键
    p2 = StyleProfile.from_dict({"target_style_cn": "侘寂", "target_style_en": "wabi-sabi",
                                  "original_style_cn": "北欧", "style_descriptor_en": "x"})
    ok("from_dict 兼容 target_style_* 键", p2.overall_style_en == "wabi-sabi" and p2.original_style_cn == "北欧")
    ok("from_dict 往返一致", StyleProfile.from_dict(d).style_descriptor_en == p.style_descriptor_en)
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

# ── 4b. calc_num_shots 时长→镜头数 (8~15) ──────────────────
print("\n[4b] 镜头数推导 (按时长, 8~15)")
try:
    from core.shot_planner import calc_num_shots
    ok("10s → 8 (下限)", calc_num_shots(10) == 8, str(calc_num_shots(10)))
    ok("24s → 8", calc_num_shots(24) == 8, str(calc_num_shots(24)))
    ok("40s → 10", calc_num_shots(40) == 10, str(calc_num_shots(40)))
    ok("56s → 12", calc_num_shots(56) == 12, str(calc_num_shots(56)))
    ok("72s → 14", calc_num_shots(72) == 14, str(calc_num_shots(72)))
    ok("80s → 15 (上限)", calc_num_shots(80) == 15, str(calc_num_shots(80)))
    ok("600s → 15 (长视频封顶)", calc_num_shots(600) == 15)
    ok("0s → 8 (异常兜底)", calc_num_shots(0) == 8)
    ok("自定义上下限生效", calc_num_shots(100, min_shots=5, max_shots=10) == 10)
except Exception as e:
    ok("calc_num_shots", False, str(e))

# ── 5. ShotPlanner.build (基底帧+运镜+改造prompt) ──────────
print("\n[5] ShotPlanner.build (真实帧→改造prompt)")
try:
    from core.shot_planner import ShotPlanner
    from core.style_analyzer import StyleProfile
    from core.video_analyzer import KeyFrame
    frames = [
        KeyFrame(timestamp=float(i), path=f"/frames/f{i:03d}.jpg", index=i,
                 description="客厅场景", clean_path=f"/clean/c{i:03d}.png")
        for i in range(1, 9)
    ]
    profile = StyleProfile(style_descriptor_en="modern minimalist Japandi interior, walnut wood")
    planner = ShotPlanner(clip_duration=4)
    shots = planner.build(frames, profile)
    ok("build 生成 8 个 shot", len(shots) == 8, f"got {len(shots)}")
    ok("shot_id 1..8 连续", [s["shot_id"] for s in shots] == list(range(1, 9)))
    ok("每个 shot 有 clean_frame_path", all(s.get("clean_frame_path") for s in shots))
    ok("clean_frame_path 用清理后路径", shots[0]["clean_frame_path"] == "/clean/c001.png")
    ok("每个 shot 有 image_prompt", all(s.get("image_prompt") for s in shots))
    ok("image_prompt 含目标风格锁", all("minimalist" in s["image_prompt"] for s in shots))
    ok("image_prompt 含保持布局指令", all("Keep the EXACT same room layout" in s["image_prompt"] for s in shots))
    ok("每个 shot 有 video_prompt", all(s.get("video_prompt") for s in shots))
    ok("每个 shot 有 camera_move", all(s.get("camera_move") for s in shots))
    ok("相邻 shot 运镜不同", all(shots[i]["camera_move"] != shots[i+1]["camera_move"] for i in range(len(shots)-1)))
    ok("duration=4", all(s.get("duration") == 4 for s in shots))
    ok("含 image_status/clip_status", all("image_status" in s and "clip_status" in s for s in shots))
except Exception as e:
    ok("ShotPlanner.build", False, str(e))

# ── 5b. RoomPlanner (图片模式: 单图→同风格其他房间) ─────────
print("\n[5b] RoomPlanner 图片模式规划")
try:
    from core.room_planner import RoomPlanner
    from core.style_analyzer import StyleProfile
    planner = RoomPlanner(clip_duration=4)
    profile = StyleProfile(style_descriptor_en="modern minimalist Japandi interior, walnut wood")
    shots = planner.plan(profile, source_room_id="living_room", num_rooms=7)
    ok("生成 7 个其他房间", len(shots) == 7, f"got {len(shots)}")
    ok("不含客厅本身", all(s["room_type"] != "living_room" for s in shots))
    ok("每个 shot 有 room_type", all(s.get("room_type") for s in shots))
    ok("每个 shot 有 viewpoint", all(s.get("viewpoint") for s in shots))
    ok("每个 shot 有 camera_move", all(s.get("camera_move") for s in shots))
    ok("image_prompt 含风格锁", all("minimalist" in s["image_prompt"] for s in shots))
    ok("image_prompt 含同风格指令(reference/SAME)", all("reference image" in s["image_prompt"] and "SAME home" in s["image_prompt"] for s in shots))
    ok("video_prompt 非空", all(s.get("video_prompt") for s in shots))
    ok("房间类型多样(>=5)", len({s["room_type"] for s in shots}) >= 5)
    ok("shot_id 1..7 连续", [s["shot_id"] for s in shots] == list(range(1, 8)))
    ok("duration=4", all(s.get("duration") == 4 for s in shots))
    shots3 = planner.plan(profile, num_rooms=3)
    ok("num_rooms=3 → 3 shots", len(shots3) == 3)
except Exception as e:
    ok("RoomPlanner", False, str(e))

# ── 6. FrameCleaner 判定逻辑 (不调 API) ────────────────────
print("\n[6] FrameCleaner 去人/字判定")
try:
    from core.frame_cleaner import FrameCleaner
    ok("_has_person 检出人物", FrameCleaner._has_person("画面中有一个人在客厅走动"))
    ok("_has_person 检出英文 person", FrameCleaner._has_person("a woman standing in kitchen"))
    ok("_has_person 纯空房间不检出", not FrameCleaner._has_person("现代简约客厅, 沙发茶几落地窗"))
    ok("_has_text 检出文字", FrameCleaner._has_text("画面角落有水印和字幕"))
    ok("_has_text 无文字不检出", not FrameCleaner._has_text("现代简约客厅, 无文字无字幕"))
    ok("_has_text 否定词优先", not FrameCleaner._has_text("无文字, 无logo, 画面干净"))
except Exception as e:
    ok("FrameCleaner 判定", False, str(e))

# ── 7. ProjectManager 持久化 (临时目录) ────────────────────
print("\n[7] ProjectManager 持久化")
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
        ok("含 shot_plan 字段", "shot_plan" in proj)
        ok("source_resolution 记录", proj["source_resolution"] == {"width": 1920, "height": 1080})
        ok("projects.json 已写入", (data_dir / "projects.json").exists())
        mgr.update_project(pid, status="style_profiled", style_profile={"overall_style_cn": "侘寂"})
        got = mgr.get_project(pid) or {}
        ok("update 后 status", got["status"] == "style_profiled")
        ok("update 后 style_profile", got["style_profile"]["overall_style_cn"] == "侘寂")
        mgr.update_project(pid, shot_plan=[{"shot_id": 1, "image_status": "pending"}])
        mgr.update_shot(pid, 1, image_status="done", image_path="/x.png")
        got2 = mgr.get_project(pid) or {}
        ok("update_shot 生效", got2["shot_plan"][0]["image_status"] == "done")
        ok("list_projects 返回 1", len(mgr.list_projects()) == 1)
        ok("get_stats total=1", mgr.get_stats()["total"] == 1)
        ok("delete 成功", mgr.delete_project(pid) is True)
        ok("delete 后 total=0", mgr.get_stats()["total"] == 0)
        # 图片模式项目
        proj_img = mgr.create_project(source_image="/fake/living.jpg", mode="image",
                                      source_resolution=(1920, 1080), aspect_ratio="16:9")
        ok("图片模式 mode=image", proj_img["mode"] == "image")
        ok("图片模式 source_image", proj_img["source_image"] == "/fake/living.jpg")
        ok("图片模式 source_resolution", proj_img["source_resolution"]["width"] == 1920)
        # 视频模式位置参数向后兼容
        proj_vid = mgr.create_project("/fake/v.mp4", (1280, 720), "16:9")
        ok("视频模式位置参数兼容", proj_vid["mode"] == "video" and proj_vid["source_video"] == "/fake/v.mp4")
except Exception as e:
    ok("ProjectManager", False, str(e))

# ── 8. 关键模块可导入 ──────────────────────────────────────
print("\n[8] 关键模块导入")
for mod in [
    "core.llm_client", "core.media_api", "core.cos_client",
    "core.video_analyzer", "core.style_analyzer", "core.frame_cleaner",
    "core.shot_planner", "core.room_planner", "core.project_manager",
    "workflow.pipeline", "workflow.image_pipeline",
]:
    try:
        __import__(mod)
        ok(f"import {mod}", True)
    except Exception as e:
        ok(f"import {mod}", False, str(e))

# ── 9. CloneVideoPipeline 实例化 ───────────────────────────
print("\n[9] CloneVideoPipeline 实例化")
try:
    from workflow.pipeline import CloneVideoPipeline
    pipe = CloneVideoPipeline()
    ok("CloneVideoPipeline() 构造成功", True)
    ok("含 video_analyzer", hasattr(pipe, "video_analyzer"))
    ok("含 style_analyzer", hasattr(pipe, "style_analyzer"))
    ok("含 frame_cleaner", hasattr(pipe, "frame_cleaner"))
    ok("含 shot_planner", hasattr(pipe, "shot_planner"))
    ok("含 project_mgr", hasattr(pipe, "project_mgr"))
except Exception as e:
    ok("CloneVideoPipeline 实例化", False, str(e))

# ── 9b. ImageClonePipeline 实例化 ──────────────────────────
print("\n[9b] ImageClonePipeline 实例化")
try:
    from workflow.image_pipeline import ImageClonePipeline
    pipe2 = ImageClonePipeline()
    ok("ImageClonePipeline() 构造成功", True)
    ok("含 style_analyzer", hasattr(pipe2, "style_analyzer"))
    ok("含 room_planner", hasattr(pipe2, "room_planner"))
    ok("含 project_mgr", hasattr(pipe2, "project_mgr"))
except Exception as e:
    ok("ImageClonePipeline 实例化", False, str(e))

# ── 汇总 ──────────────────────────────────────────────────
print(f"\n== 结果: {passed} 通过, {failed} 失败 ==")
sys.exit(1 if failed else 0)
