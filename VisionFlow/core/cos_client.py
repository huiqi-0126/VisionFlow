"""火山云 TOS 文件上传客户端 (兼容原腾讯云 COS 接口)"""

from __future__ import annotations

import logging
import uuid
from datetime import date
from pathlib import Path

import tos

logger = logging.getLogger(__name__)


class COSClient:
    """火山云对象存储上传客户端"""

    def __init__(
        self,
        secret_id: str,
        secret_key: str,
        region: str,
        bucket: str,
        base_url: str = "",
    ) -> None:
        self.bucket = bucket
        self.base_url = base_url.rstrip("/") if base_url else ""
        
        endpoint = f"tos-{region}.volces.com"
        self.client = tos.TosClientV2(secret_id, secret_key, endpoint, region)

    def _make_remote_path(self, local_path: str | Path, prefix: str = "visionflow") -> str:
        """生成远程存储路径: visionflow/2026-05-21/uuid.ext"""
        ext = Path(local_path).suffix or ".mp4"
        today = date.today().isoformat()
        name = f"{uuid.uuid4().hex[:12]}{ext}"
        return f"{prefix}/{today}/{name}"

    def upload_file(
        self,
        local_path: str | Path,
        remote_path: str | None = None,
    ) -> str:
        """上传本地文件到 TOS，返回公网 URL"""
        local_path = Path(local_path)
        if not local_path.exists():
            raise FileNotFoundError(f"文件不存在: {local_path}")

        remote_path = remote_path or self._make_remote_path(local_path)
        logger.info("上传: %s → tos://%s/%s", local_path.name, self.bucket, remote_path)

        self.client.put_object_from_file(
            self.bucket,
            remote_path,
            str(local_path),
        )

        url = f"{self.base_url}/{remote_path}" if self.base_url else remote_path
        logger.info("上传完成: %s", url)
        return url

    def upload_bytes(
        self,
        data: bytes,
        remote_path: str,
        content_type: str = "video/mp4",
    ) -> str:
        """上传二进制数据到 TOS"""
        logger.info("上传 bytes (%d bytes) → %s", len(data), remote_path)

        self.client.put_object(
            self.bucket,
            remote_path,
            content=data,
        )

        url = f"{self.base_url}/{remote_path}" if self.base_url else remote_path
        return url
