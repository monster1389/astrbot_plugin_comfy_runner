"""任务账本：用 SQLite 持久化任务状态、参数摘要与正式产物位置。"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .comfy import ArtifactRef

TASK_STATUSES = frozenset(
    {
        "waiting_dependency",
        "queued",
        "running",
        "succeeded",
        "failed",
        "unknown",
        "cancelled",
    }
)
TERMINAL_TASK_STATUSES = frozenset({"succeeded", "failed", "unknown", "cancelled"})
DELIVERY_STATUSES = frozenset({"pending", "skipped", "delivered", "failed"})


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """账本里记录的单条正式产物元数据。"""

    index: int
    output: str
    type: str
    ref: ArtifactRef | None = None
    text: str | None = None
    downloaded: bool = False


@dataclass(frozen=True, slots=True)
class TaskRecord:
    """账本里记录的单条工作流任务。"""

    prompt_id: str
    workflow: str
    sender_id: str
    origin: str
    arguments: dict[str, Any]
    status: str
    deliver_requested: bool
    delivery_status: str
    error: str
    created_at: float
    updated_at: float
    artifacts: tuple[StoredArtifact, ...] = ()


class TaskStore:
    """基于 SQLite 的持久化任务账本。"""

    def __init__(self, db_path: Path) -> None:
        """初始化数据库文件与表结构。

        Args:
            db_path: SQLite 数据库文件路径。
        """
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def create_task(
        self,
        prompt_id: str,
        workflow: str,
        sender_id: str,
        origin: str,
        arguments: dict[str, Any],
        status: str,
        deliver_requested: bool,
        delivery_status: str,
    ) -> None:
        """登记一个新任务。

        Args:
            prompt_id: 任务 ID（正式 ``prompt_id`` 或依赖等待期的临时 ID）。
            workflow: 工作流名称。
            sender_id: 带平台前缀的提交者标识。
            origin: 来源会话标识。
            arguments: 本次调用传入的参数。
            status: 初始任务状态。
            deliver_requested: 完成后是否要求自动发送。
            delivery_status: 初始交付状态。
        """
        _validate_status(status, delivery_status)
        now = time.time()
        args_json = json.dumps(arguments, ensure_ascii=False)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM tasks WHERE prompt_id = ?", (prompt_id,))
            conn.execute(
                "INSERT INTO tasks ("
                "prompt_id, workflow, sender_id, origin, arguments_json, "
                "status, deliver_requested, delivery_status, error, "
                "created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                (
                    prompt_id,
                    workflow,
                    sender_id,
                    origin,
                    args_json,
                    status,
                    1 if deliver_requested else 0,
                    delivery_status,
                    now,
                    now,
                ),
            )

    def bind_prompt_id(self, temp_id: str, prompt_id: str) -> None:
        """把等待前置任务阶段的临时 ID 绑定为 ComfyUI 返回的真实 ID。

        Args:
            temp_id: 提交前分配的临时任务 ID。
            prompt_id: ComfyUI 分配的真实任务 ID。
        """
        now = time.time()
        with closing(self._connect()) as conn, conn:
            if temp_id != prompt_id:
                conn.execute("DELETE FROM tasks WHERE prompt_id = ?", (prompt_id,))
            conn.execute(
                "UPDATE tasks SET prompt_id = ?, status = 'queued', "
                "updated_at = ? WHERE prompt_id = ?",
                (prompt_id, now, temp_id),
            )
            if temp_id != prompt_id:
                conn.execute(
                    "INSERT OR REPLACE INTO task_aliases (alias_id, prompt_id) "
                    "VALUES (?, ?)",
                    (temp_id, prompt_id),
                )

    def update_status(
        self,
        prompt_id: str,
        status: str,
        error: str = "",
    ) -> None:
        """更新任务的执行状态与错误信息。

        Args:
            prompt_id: 任务 ID 或别名。
            status: 新任务状态。
            error: 失败或取消说明。
        """
        if status not in TASK_STATUSES:
            raise ValueError(f"非法的任务状态: {status}")
        now = time.time()
        with closing(self._connect()) as conn, conn:
            canonical = self._resolve_id(conn, prompt_id)
            conn.execute(
                "UPDATE tasks SET status = ?, error = ?, updated_at = ? "
                "WHERE prompt_id = ?",
                (status, error, now, canonical),
            )

    def record_success(
        self,
        prompt_id: str,
        artifacts: Sequence[StoredArtifact],
        delivery_status: str,
    ) -> None:
        """记录任务成功完成及其正式产物位置。

        Args:
            prompt_id: 任务 ID 或别名。
            artifacts: 提取到的正式产物元数据列表。
            delivery_status: 更新后的交付状态。
        """
        if delivery_status not in DELIVERY_STATUSES:
            raise ValueError(f"非法的交付状态: {delivery_status}")
        now = time.time()
        with closing(self._connect()) as conn, conn:
            canonical = self._resolve_id(conn, prompt_id)
            conn.execute(
                "UPDATE tasks SET status = 'succeeded', error = '', "
                "delivery_status = ?, updated_at = ? WHERE prompt_id = ?",
                (delivery_status, now, canonical),
            )
            conn.execute(
                "DELETE FROM task_artifacts WHERE prompt_id = ?",
                (canonical,),
            )
            for item in artifacts:
                conn.execute(
                    "INSERT INTO task_artifacts ("
                    "prompt_id, artifact_index, output_name, output_type, "
                    "filename, subfolder, storage_type, text_content, downloaded"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        canonical,
                        item.index,
                        item.output,
                        item.type,
                        item.ref.filename if item.ref else "",
                        item.ref.subfolder if item.ref else "",
                        item.ref.type if item.ref else "",
                        item.text,
                        1 if item.downloaded else 0,
                    ),
                )

    def update_delivery(
        self,
        prompt_id: str,
        delivery_status: str,
        error: str = "",
    ) -> None:
        """更新任务的交付状态。

        Args:
            prompt_id: 任务 ID 或别名。
            delivery_status: 新交付状态。
            error: 交付失败时的说明。
        """
        if delivery_status not in DELIVERY_STATUSES:
            raise ValueError(f"非法的交付状态: {delivery_status}")
        now = time.time()
        with closing(self._connect()) as conn, conn:
            canonical = self._resolve_id(conn, prompt_id)
            conn.execute(
                "UPDATE tasks SET delivery_status = ?, error = ?, updated_at = ? "
                "WHERE prompt_id = ?",
                (delivery_status, error, now, canonical),
            )

    def mark_downloaded(self, prompt_id: str, index: int) -> None:
        """标记某条产物已被显式下载到临时目录。

        Args:
            prompt_id: 任务 ID 或别名。
            index: 从 1 开始的产物序号。
        """
        now = time.time()
        with closing(self._connect()) as conn, conn:
            canonical = self._resolve_id(conn, prompt_id)
            conn.execute(
                "UPDATE task_artifacts SET downloaded = 1 "
                "WHERE prompt_id = ? AND artifact_index = ?",
                (canonical, index),
            )
            conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE prompt_id = ?",
                (now, canonical),
            )

    def get(self, prompt_id: str) -> TaskRecord | None:
        """按任务 ID 或历史别名取出任务记录。

        Args:
            prompt_id: 任务 ID。

        Returns:
            任务记录；不存在时为 None。
        """
        with closing(self._connect()) as conn:
            canonical = self._resolve_id(conn, prompt_id)
            row = conn.execute(
                "SELECT * FROM tasks WHERE prompt_id = ?",
                (canonical,),
            ).fetchone()
            if row is None:
                return None
            return self._build_record(conn, row)

    def list_recent(self, limit: int = 20) -> list[TaskRecord]:
        """按提交时间倒序列出最近的任务。

        Args:
            limit: 返回条数上限。

        Returns:
            任务记录列表。
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, rowid DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [self._build_record(conn, row) for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    prompt_id TEXT PRIMARY KEY,
                    workflow TEXT NOT NULL,
                    sender_id TEXT NOT NULL,
                    origin TEXT NOT NULL,
                    arguments_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    deliver_requested INTEGER NOT NULL,
                    delivery_status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS task_artifacts (
                    prompt_id TEXT NOT NULL,
                    artifact_index INTEGER NOT NULL,
                    output_name TEXT NOT NULL,
                    output_type TEXT NOT NULL,
                    filename TEXT NOT NULL DEFAULT '',
                    subfolder TEXT NOT NULL DEFAULT '',
                    storage_type TEXT NOT NULL DEFAULT '',
                    text_content TEXT,
                    downloaded INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (prompt_id, artifact_index),
                    FOREIGN KEY (prompt_id) REFERENCES tasks(prompt_id)
                        ON DELETE CASCADE ON UPDATE CASCADE
                );
                CREATE TABLE IF NOT EXISTS task_aliases (
                    alias_id TEXT PRIMARY KEY,
                    prompt_id TEXT NOT NULL,
                    FOREIGN KEY (prompt_id) REFERENCES tasks(prompt_id)
                        ON DELETE CASCADE ON UPDATE CASCADE
                );
                """
            )

    def _resolve_id(self, conn: sqlite3.Connection, prompt_id: str) -> str:
        row = conn.execute(
            "SELECT prompt_id FROM task_aliases WHERE alias_id = ?",
            (prompt_id,),
        ).fetchone()
        return str(row["prompt_id"]) if row is not None else prompt_id

    def _build_record(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> TaskRecord:
        pid = str(row["prompt_id"])
        artifact_rows = conn.execute(
            "SELECT * FROM task_artifacts WHERE prompt_id = ? "
            "ORDER BY artifact_index ASC",
            (pid,),
        ).fetchall()
        artifacts = tuple(_row_to_artifact(item) for item in artifact_rows)
        return TaskRecord(
            prompt_id=pid,
            workflow=str(row["workflow"]),
            sender_id=str(row["sender_id"]),
            origin=str(row["origin"]),
            arguments=json.loads(str(row["arguments_json"])),
            status=str(row["status"]),
            deliver_requested=bool(row["deliver_requested"]),
            delivery_status=str(row["delivery_status"]),
            error=str(row["error"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            artifacts=artifacts,
        )


def _validate_status(status: str, delivery_status: str) -> None:
    if status not in TASK_STATUSES:
        raise ValueError(f"非法的任务状态: {status}")
    if delivery_status not in DELIVERY_STATUSES:
        raise ValueError(f"非法的交付状态: {delivery_status}")


def _row_to_artifact(row: sqlite3.Row) -> StoredArtifact:
    filename = str(row["filename"])
    ref = (
        ArtifactRef(
            filename=filename,
            subfolder=str(row["subfolder"]),
            type=str(row["storage_type"]),
        )
        if filename
        else None
    )
    return StoredArtifact(
        index=int(row["artifact_index"]),
        output=str(row["output_name"]),
        type=str(row["output_type"]),
        ref=ref,
        text=row["text_content"],
        downloaded=bool(row["downloaded"]),
    )
