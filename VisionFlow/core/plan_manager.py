"""30天内容规划管理器 - MySQL 数据库持久化

与 ProjectManager 分离，专门管理内容规划项目。
每个规划项目包含: persona + 30天日历 + 每天脚本 + 视频生成状态。
"""

from __future__ import annotations

import json
import logging
import uuid
import pymysql
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from config import get_settings

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class PlanManager:
    """基于 MySQL 数据库的内容规划项目管理"""

    def __init__(self, data_dir: str | Path, plans_output_dir: str | Path) -> None:
        self._output_dir = Path(plans_output_dir)
        self.settings = get_settings()
        self._init_db()

    def _get_conn(self) -> pymysql.Connection:
        return pymysql.connect(
            host=self.settings.db_host,
            port=self.settings.db_port,
            user=self.settings.db_user,
            password=self.settings.db_pass,
            database=self.settings.db_name,
            charset='utf8mb4',
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True
        )

    def _init_db(self) -> None:
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS plans (
                            plan_id VARCHAR(64) PRIMARY KEY,
                            type VARCHAR(64),
                            status VARCHAR(64),
                            persona LONGTEXT,
                            content_tracks LONGTEXT,
                            calendar LONGTEXT,
                            scripts LONGTEXT,
                            summary LONGTEXT,
                            output_dir TEXT,
                            error TEXT,
                            created_at VARCHAR(64),
                            updated_at VARCHAR(64),
                            portrait_image_url TEXT,
                            portrait_image_file TEXT
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    ''')
                    # 反馈数据表：记录用户对生成视频的隐式反馈
                    # - label='negative'：用户点击 regenerate_script / regenerate_all（对当前视频不满意）
                    # - label='positive'：用户点击 schedule（认可并发布）
                    # 该表是"自动优化"的数据基础：positive 样本可作为 few-shot 注入下次生成，
                    # negative 样本可离线聚类分析共性问题，反向迭代 prompt 模板。
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS video_feedback (
                            id BIGINT AUTO_INCREMENT PRIMARY KEY,
                            plan_id VARCHAR(64) NOT NULL,
                            day INT NOT NULL,
                            label VARCHAR(16) NOT NULL,
                            action VARCHAR(64) NOT NULL,
                            track VARCHAR(8),
                            persona LONGTEXT,
                            calendar_entry LONGTEXT,
                            script_snapshot LONGTEXT,
                            video_prompt LONGTEXT,
                            video_url TEXT,
                            device_id VARCHAR(128),
                            created_at VARCHAR(64),
                            INDEX idx_label_track (label, track),
                            INDEX idx_plan_day (plan_id, day)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
                    ''')
        except Exception as e:
            logger.error("初始化 MySQL 数据库失败: %s", e)

    def _row_to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        def parse_json(val):
            if not val:
                return None
            try:
                return json.loads(val)
            except Exception:
                return None

        return {
            "plan_id": row.get("plan_id"),
            "type": row.get("type"),
            "status": row.get("status"),
            "persona": parse_json(row.get("persona")) or {},
            "content_tracks": parse_json(row.get("content_tracks")),
            "calendar": parse_json(row.get("calendar")) or [],
            "scripts": parse_json(row.get("scripts")) or [],
            "summary": parse_json(row.get("summary")) or {},
            "output_dir": row.get("output_dir"),
            "error": row.get("error"),
            "created_at": row.get("created_at"),
            "updated_at": row.get("updated_at"),
            "portrait_image_url": row.get("portrait_image_url"),
            "portrait_image_file": row.get("portrait_image_file"),
        }

    # ── CRUD ──────────────────────────────────────────────────

    def create_plan(
        self,
        persona: dict[str, Any],
        content_tracks: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """创建新的内容规划项目"""
        plan_id = uuid.uuid4().hex[:8]

        plan_dir = self._output_dir / plan_id
        plan_dir.mkdir(parents=True, exist_ok=True)

        now = _now_iso()
        plan: dict[str, Any] = {
            "plan_id": plan_id,
            "type": "content_plan",
            "status": "created",
            "persona": persona,
            "content_tracks": content_tracks,
            "calendar": [],
            "scripts": [],
            "summary": {},
            "output_dir": str(plan_dir),
            "error": "",
            "created_at": now,
            "updated_at": now,
            "portrait_image_url": None,
            "portrait_image_file": None,
        }

        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    INSERT INTO plans (
                        plan_id, type, status, persona, content_tracks,
                        calendar, scripts, summary, output_dir, error,
                        created_at, updated_at, portrait_image_url, portrait_image_file
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ''', (
                    plan["plan_id"],
                    plan["type"],
                    plan["status"],
                    json.dumps(plan["persona"], ensure_ascii=False),
                    json.dumps(plan["content_tracks"], ensure_ascii=False) if plan["content_tracks"] else None,
                    json.dumps(plan["calendar"], ensure_ascii=False),
                    json.dumps(plan["scripts"], ensure_ascii=False),
                    json.dumps(plan["summary"], ensure_ascii=False),
                    plan["output_dir"],
                    plan["error"],
                    plan["created_at"],
                    plan["updated_at"],
                    plan["portrait_image_url"],
                    plan["portrait_image_file"]
                ))

        logger.info("规划项目已创建: %s", plan_id)
        return plan

    def get_plan(self, plan_id: str) -> dict[str, Any] | None:
        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    SELECT plan_id, type, status, persona, content_tracks,
                           calendar, scripts, summary, output_dir, error,
                           created_at, updated_at, portrait_image_url, portrait_image_file
                    FROM plans WHERE plan_id = %s
                ''', (plan_id,))
                row = cursor.fetchone()
                if row:
                    return self._row_to_dict(row)
        return None

    def update_plan(self, plan_id: str, **kwargs: Any) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if not plan:
            logger.warning("更新规划项目未找到: %s", plan_id)
            return {}

        plan.update(kwargs)
        plan["updated_at"] = _now_iso()

        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    UPDATE plans SET
                        status = %s,
                        persona = %s,
                        content_tracks = %s,
                        calendar = %s,
                        scripts = %s,
                        summary = %s,
                        error = %s,
                        updated_at = %s,
                        portrait_image_url = %s,
                        portrait_image_file = %s
                    WHERE plan_id = %s
                ''', (
                    plan.get("status"),
                    json.dumps(plan.get("persona", {}), ensure_ascii=False),
                    json.dumps(plan.get("content_tracks"), ensure_ascii=False) if plan.get("content_tracks") else None,
                    json.dumps(plan.get("calendar", []), ensure_ascii=False),
                    json.dumps(plan.get("scripts", []), ensure_ascii=False),
                    json.dumps(plan.get("summary", {}), ensure_ascii=False),
                    plan.get("error"),
                    plan["updated_at"],
                    plan.get("portrait_image_url"),
                    plan.get("portrait_image_file"),
                    plan_id
                ))
        return plan

    def list_plans(self, limit: int = 50) -> list[dict[str, Any]]:
        plans = []
        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('''
                    SELECT plan_id, type, status, persona, content_tracks,
                           calendar, scripts, summary, output_dir, error,
                           created_at, updated_at, portrait_image_url, portrait_image_file
                    FROM plans ORDER BY created_at DESC LIMIT %s
                ''', (limit,))
                for row in cursor.fetchall():
                    plans.append(self._row_to_dict(row))
        # 逆序以便较早的项目在前面，保持之前的一致性
        return list(reversed(plans))

    def delete_plan(self, plan_id: str) -> bool:
        with self._get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('DELETE FROM plans WHERE plan_id = %s', (plan_id,))
                if cursor.rowcount > 0:
                    logger.info("规划项目已删除: %s", plan_id)
                    return True
        return False

    def update_day_script(
        self,
        plan_id: str,
        day: int,
        script: dict[str, Any],
    ) -> dict[str, Any] | None:
        """更新某一天的脚本"""
        plan = self.get_plan(plan_id)
        if not plan:
            return None

        scripts = plan.get("scripts", [])
        found = False
        for i, s in enumerate(scripts):
            if s.get("day") == day:
                scripts[i] = script
                found = True
                break
        if not found:
            scripts.append(script)

        self.update_plan(plan_id, scripts=scripts)
        return script

    # ── 用户反馈数据闭环 ─────────────────────────────────────
    #
    # 设计原则：
    # 1. record_feedback 必须健壮 —— 失败只记 log，绝不抛异常，否则会阻塞
    #    用户的主流程（regenerate / schedule），让埋点反而变成故障源。
    # 2. 自动从当前 plan 抓取 snapshot（persona / calendar_entry / script /
    #    video_prompt），调用方只需传 plan_id / day / label / action，
    #    避免在 web 层重复读取 plan 数据。
    # 3. 调用时机的关键约束：必须在 regenerate_xxx_for_day 之前调用，
    #    否则 plan 里的 script 已被新版本覆盖，记录到的是"新版本"而非
    #    "用户不满意的旧版本"。

    def record_feedback(
        self,
        plan_id: str,
        day: int,
        label: str,
        action: str,
        device_id: str | None = None,
    ) -> bool:
        """记录一条用户反馈（隐式信号）

        Args:
            plan_id: 规划项目 ID
            day: 视频对应的日期 (1-30)
            label: 'positive'（用户认可，点 schedule 发布）|
                   'negative'（用户不满意，点 regenerate）
            action: 触发动作的接口名，用于后续细分分析：
                    'regenerate_script' / 'regenerate_all' / 'schedule'
            device_id: schedule 时才有，发布到哪台设备

        Returns:
            True 写入成功，False 失败（已 log，不抛异常）
        """
        try:
            plan = self.get_plan(plan_id)
            if not plan:
                logger.warning("record_feedback: plan %s 不存在", plan_id)
                return False

            script = next(
                (s for s in plan.get("scripts", []) if s.get("day") == day),
                None,
            )
            if not script:
                logger.warning(
                    "record_feedback: plan %s day %d 找不到 script", plan_id, day
                )
                return False

            calendar_entry = next(
                (e for e in plan.get("calendar", []) if e.get("day") == day),
                {},
            )

            with self._get_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO video_feedback (
                            plan_id, day, label, action, track,
                            persona, calendar_entry, script_snapshot,
                            video_prompt, video_url, device_id, created_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (
                        plan_id,
                        day,
                        label,
                        action,
                        script.get("track") or calendar_entry.get("track"),
                        json.dumps(plan.get("persona", {}), ensure_ascii=False),
                        json.dumps(calendar_entry, ensure_ascii=False),
                        json.dumps(script, ensure_ascii=False),
                        script.get("video_prompt", ""),
                        script.get("video_file", ""),
                        device_id,
                        _now_iso(),
                    ))
            logger.info(
                "反馈已记录: plan=%s day=%d label=%s action=%s",
                plan_id, day, label, action,
            )
            return True
        except Exception as exc:
            # 关键：埋点失败不能影响业务流程
            logger.error("record_feedback 失败（已忽略）: %s", exc)
            return False

    def get_positive_examples(
        self,
        track: str | None = None,
        k: int = 3,
    ) -> list[dict[str, Any]]:
        """获取最近的 positive 样本，用于 few-shot 注入下次生成

        Args:
            track: 限定内容板块 (A/B/C/D)，None 表示不限定
            k: 最多返回多少条

        Returns:
            list of {video_prompt, title_cn, core_topic, day, plan_id}
            按 created_at 倒序，最多 k 条。
            当样本量 < 3 时返回空列表 —— 信号太弱时不要注入。
        """
        try:
            sql = (
                "SELECT video_prompt, script_snapshot, plan_id, day, created_at "
                "FROM video_feedback WHERE label = 'positive' "
                "AND video_prompt IS NOT NULL AND video_prompt != '' "
            )
            params: list = []
            if track:
                sql += "AND track = %s "
                params.append(track)
            sql += "ORDER BY created_at DESC LIMIT %s"
            params.append(max(k, 3))  # 先取够门槛量

            with self._get_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(sql, params)
                    # _get_conn 配置了 DictCursor，运行时返回 list[dict]，
                    # 但 Pyright 推断为基础 Cursor 的 tuple，用 cast 显式 narrowing。
                    rows = cast(list[dict[str, Any]], cursor.fetchall())

            if len(rows) < 3:
                # 样本太少，注入会引入偏见，宁可不用
                return []

            examples = []
            for row in rows[:k]:
                script = {}
                try:
                    script = json.loads(row.get("script_snapshot") or "{}")
                except Exception:
                    pass
                examples.append({
                    "video_prompt": row.get("video_prompt", ""),
                    "title_cn": script.get("title_cn", ""),
                    "core_topic": script.get("core_topic", ""),
                    "day": row.get("day"),
                    "plan_id": row.get("plan_id"),
                })
            return examples
        except Exception as exc:
            logger.warning("get_positive_examples 失败（已降级为空）: %s", exc)
            return []

    def get_feedback_stats(self) -> dict[str, Any]:
        """反馈数据统计，用于排查和后续报表

        返回:
            {
                "total_positive": int,
                "total_negative": int,
                "by_track": {"A": {"positive": int, "negative": int}, ...},
                "by_action": {"regenerate_script": int, ...},
            }
        """
        try:
            with self._get_conn() as conn:
                with conn.cursor() as cursor:
                    cursor.execute('''
                        SELECT label, action, track, COUNT(*) AS cnt
                        FROM video_feedback
                        GROUP BY label, action, track
                    ''')
                    rows = cast(list[dict[str, Any]], cursor.fetchall())

            stats: dict[str, Any] = {
                "total_positive": 0,
                "total_negative": 0,
                "by_track": {},
                "by_action": {},
            }
            for row in rows:
                label = row.get("label", "")
                action = row.get("action", "")
                track = row.get("track") or "?"
                cnt = row.get("cnt", 0)

                if label == "positive":
                    stats["total_positive"] += cnt
                elif label == "negative":
                    stats["total_negative"] += cnt

                stats["by_action"][action] = stats["by_action"].get(action, 0) + cnt

                track_entry = stats["by_track"].setdefault(
                    track, {"positive": 0, "negative": 0}
                )
                if label in track_entry:
                    track_entry[label] += cnt

            return stats
        except Exception as exc:
            logger.warning("get_feedback_stats 失败: %s", exc)
            return {"total_positive": 0, "total_negative": 0, "by_track": {}, "by_action": {}}
