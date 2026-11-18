"""表快照管理业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.merge import merge_service
from app.store import store

MODULE = "snapshot"
REQUIRED_FIELDS = ["快照编号", "表名称", "快照时间"]
STATUS_ORDER = ["已创建", "保留中", "已过期", "已清理"]
ACTION_RULES = {"创建快照": "已创建", "恢复快照": "保留中", "清理快照": "已过期"}
NEGATIVE_ACTIONS = []


class SnapshotService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("快照编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"快照记录 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于表快照管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"快照记录已{action}"


# ---------------------------------------------------------------------------
# 快照模块消费数据分支合并的冲突处置结果
#
# 约定：
# 1. 先校验合并审计清单里冲突处置结果的兼容性（字段映射完整、唯一裁决、版本不是运行起点），
#    校验不过就不动任何已有明细；
# 2. 已有快照明细原样保留，只追加本次合并版本对应的快照，上一稳定记录继续可用；
# 3. 同步过程按冲突段记录进度，中断后重入只继续未完成分段，不重复处理已完成分段。
# ---------------------------------------------------------------------------

SNAPSHOT_SYNC_MODULE = "_snapshot_syncs"
SNAPSHOT_STABLE_STATUSES = ("保留中", "已创建")


class SnapshotMergeSyncService:
    """把合并审计清单的冲突处置结果同步进表快照管理。"""

    def _sync_rows(self) -> list[dict[str, Any]]:
        return store.rows(SNAPSHOT_SYNC_MODULE)

    def _find_sync(self, branch_id: int) -> dict[str, Any] | None:
        return next(
            (row for row in self._sync_rows() if int(row.get("branch_id", 0)) == int(branch_id)),
            None,
        )

    def _last_stable_record(self, table_name: str) -> dict[str, Any] | None:
        candidates = [
            row for row in store.rows(MODULE)
            if row.get("表名称") == table_name and row.get("status") in SNAPSHOT_STABLE_STATUSES
        ]
        return candidates[-1] if candidates else None

    def compatibility(self, branch_id: int, *, team: str | None = None) -> dict[str, Any]:
        """校验合并审计清单中冲突处置结果的兼容性；只读，不改明细。"""
        state = merge_service.state(branch_id, team=team)
        if state is None:
            return {"ok": False, "errors": [f"分支 {branch_id} 没有合并批次"], "segments": []}
        validation = state.get("validation") or {}
        audit_records = state.get("audit_records") or []
        errors: list[str] = list(validation.get("errors") or [])

        done_records = [record for record in audit_records if record.get("status") == "已完成"]
        verdicts = {record.get("verdict") for record in done_records if record.get("verdict")}
        if len(verdicts) > 1:
            errors.append("冲突处置结果存在多个裁决，快照无法确定版本依据")
        if not verdicts:
            errors.append("冲突处置结果还没有任何裁决")

        incomplete_mapping = [
            record.get("segment") for record in done_records
            if not str((record.get("field_mapping") or {}).get("source_field") or "").strip()
            or not str((record.get("field_mapping") or {}).get("target_field") or "").strip()
        ]
        if incomplete_mapping:
            errors.append(f"冲突处置结果字段映射不完整：{'、'.join(map(str, incomplete_mapping))}")

        target_version = state.get("target_version")
        if not target_version:
            errors.append("合并审计清单缺少目标表版本")
        elif target_version == state.get("run_start_version"):
            errors.append("目标表版本仍是运行起点旧版，不能作为快照依据")

        table_name = (state.get("branch") or {}).get("表名称")
        stable = self._last_stable_record(table_name) if table_name else None
        return {
            "ok": not errors,
            "errors": errors,
            "branch_id": branch_id,
            "batch_no": (state.get("batch") or {}).get("batch_no"),
            "target_version": target_version,
            "verdict": next(iter(verdicts), None),
            "completed_segments": state.get("completed_segments", []),
            "pending_segments": state.get("pending_segments", []),
            "field_mapping_complete": bool((state.get("effective_version") or {}).get("mapping_complete")),
            "last_stable_snapshot": {
                "id": stable.get("id"),
                "快照编号": stable.get("快照编号"),
                "快照版本": stable.get("快照版本"),
                "status": stable.get("status"),
            } if stable else None,
            "read_only": bool(state.get("read_only")),
        }

    def sync(
        self,
        branch_id: int,
        values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        """消费冲突处置结果：兼容性校验 → 保留已有明细 → 只续做未完成分段。"""
        team = values.get("team") or values.get("operator_team") or values.get("操作小组")
        check = self.compatibility(branch_id, team=team)
        if not check.get("ok"):
            return None, f"冲突处置结果兼容性校验未通过：{'；'.join(check.get('errors', []))}"
        if check.get("read_only"):
            return None, "越权修改被拒绝：其他小组只能查看受控版本，不能触发快照同步"

        state = merge_service.state(branch_id, team=team)
        assert state is not None
        batch = state.get("batch") or {}
        all_segments = [seg.get("code") for seg in state.get("segments", [])]
        done_segments = list(state.get("completed_segments") or [])
        pending_merge_segments = list(state.get("pending_segments") or [])

        sync_row = self._find_sync(branch_id)
        if sync_row is None:
            sync_row = {
                "id": max((int(row.get("id", 0)) for row in self._sync_rows()), default=0) + 1,
                "branch_id": branch_id,
                "batch_no": batch.get("batch_no"),
                "table_name": (state.get("branch") or {}).get("表名称"),
                "synced_segments": [],
                "stable_snapshot_id": (check.get("last_stable_snapshot") or {}).get("id"),
                "stable_version": (check.get("last_stable_snapshot") or {}).get("快照版本"),
                "status": "同步中",
            }
            self._sync_rows().append(sync_row)

        # 已完成明细保留，不因为重入被清空或重算。
        synced = list(sync_row.get("synced_segments") or [])
        remaining = [code for code in done_segments if code not in synced]

        interrupt_after_raw = values.get("interrupt_after")
        interrupt_after = int(interrupt_after_raw) if interrupt_after_raw not in (None, "") else None
        processed: list[str] = []
        for code in remaining:
            if interrupt_after is not None and len(processed) >= interrupt_after:
                sync_row["synced_segments"] = synced
                sync_row["status"] = "已中断"
                sync_row["updated_at"] = _now_text()
                sync_row["last_stable_snapshot"] = check.get("last_stable_snapshot")
                return (
                    self._state(branch_id, sync_row, check),
                    f"快照同步已中断，已完成分段 {'、'.join(synced) or '无'}，"
                    f"上一稳定记录 {sync_row.get('stable_version')} 保留",
                )
            if code not in synced:
                synced.append(code)
            processed.append(code)
            # 每完成一段就落进度，中断后重入只会继续未完成分段。
            sync_row["synced_segments"] = synced

        sync_row["synced_segments"] = synced
        sync_row["updated_at"] = _now_text()

        if pending_merge_segments:
            # 合并批次还有待裁冲突段：只同步已完成段的依据，不生成新的稳定快照。
            sync_row["status"] = "同步中"
            result = self._state(branch_id, sync_row, check)
            result["consumed"] = merge_service.consume(
                branch_id, "表快照管理", team=team, actor=values.get("operator")
            )
            return result, (
                f"已同步 {len(synced)} 个已完成冲突段，仍有待裁冲突段：{'、'.join(pending_merge_segments)}，"
                "未生成新的稳定快照"
            )

        existing = self._snapshot_for_version(state.get("target_version"), sync_row)
        if existing is not None and sync_row.get("snapshot_version") == state.get("target_version"):
            # 同一批次同一目标版本已经落过快照：重入只补齐消费记录，不再追加重复快照。
            sync_row["status"] = "已同步"
            consumption = merge_service.consume(
                branch_id,
                "表快照管理",
                team=team,
                version_basis=existing.get("快照版本"),
                actor=values.get("operator"),
            )
            result = self._state(branch_id, sync_row, check)
            result["snapshot"] = existing
            result["consumed"] = consumption
            return result, (
                f"同一目标版本 {state.get('target_version')} 的快照已存在，"
                "保留已有明细，不重复生成快照"
            )

        snapshot_record = self._publish_snapshot(branch_id, state, sync_row, values)
        sync_row["status"] = "已同步"
        sync_row["snapshot_id"] = snapshot_record.get("id")
        sync_row["snapshot_version"] = snapshot_record.get("快照版本")
        sync_row["all_segments"] = all_segments
        consumption = merge_service.consume(
            branch_id,
            "表快照管理",
            team=team,
            version_basis=snapshot_record.get("快照版本"),
            actor=values.get("operator"),
        )
        result = self._state(branch_id, sync_row, check)
        result["snapshot"] = snapshot_record
        result["consumed"] = consumption
        message = (
            f"冲突处置结果已同步到快照模块，生成快照 {snapshot_record.get('快照编号')}"
            f"（版本 {snapshot_record.get('快照版本')}），上一稳定记录保留"
        )
        return result, message

    def _snapshot_for_version(self, target_version: Any, sync_row: dict[str, Any]) -> dict[str, Any] | None:
        for row in store.rows(MODULE):
            if (
                row.get("合并批次") == sync_row.get("batch_no")
                and row.get("快照版本") == target_version
                and row.get("快照状态") == "合并版本快照"
            ):
                return row
        return None

    def _publish_snapshot(
        self,
        branch_id: int,
        state: dict[str, Any],
        sync_row: dict[str, Any],
        values: dict[str, Any],
    ) -> dict[str, Any]:
        rows = store.rows(MODULE)
        table_name = (state.get("branch") or {}).get("表名称") or ""
        stable = self._last_stable_record(table_name)
        if stable is not None:
            sync_row["stable_snapshot_id"] = stable.get("id")
            sync_row["stable_version"] = stable.get("快照版本")
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        effective = state.get("effective_version") or {}
        verdicts = {
            record.get("verdict") for record in state.get("audit_records", [])
            if record.get("status") == "已完成" and record.get("verdict")
        }
        record = {
            "id": next_id,
            "status": "保留中",
            "pending": False,
            "abnormal": False,
            "快照编号": f"SNAP-{next_id:04d}",
            "表名称": table_name,
            "快照时间": _now_text()[:10],
            "快照版本": state.get("target_version"),
            "快照大小": values.get("快照大小") or "按合并版本统计",
            "保留天数": values.get("保留天数") or "30",
            "创建方式": "分支合并冲突处置结果同步",
            "快照状态": "合并版本快照",
            "合并批次": sync_row.get("batch_no"),
            "冲突段": "、".join(sync_row.get("synced_segments") or []),
            "裁决": next(iter(verdicts), effective.get("verdict")),
            "字段映射": effective.get("field_mapping"),
            "字段映射完整": bool(effective.get("mapping_complete")),
            "上一稳定快照": sync_row.get("stable_version"),
            "版本依据": "合并审计清单",
        }
        rows.append(record)
        return record

    def _state(self, branch_id: int, sync_row: dict[str, Any], check: dict[str, Any]) -> dict[str, Any]:
        return {
            "ok": True,
            "branch_id": branch_id,
            "batch_no": sync_row.get("batch_no"),
            "status": sync_row.get("status"),
            "synced_segments": list(sync_row.get("synced_segments") or []),
            "stable_snapshot_id": sync_row.get("stable_snapshot_id"),
            "stable_version": sync_row.get("stable_version"),
            "snapshot_id": sync_row.get("snapshot_id"),
            "snapshot_version": sync_row.get("snapshot_version"),
            "compatibility": check,
            "preserved_snapshot_count": len([
                row for row in store.rows(MODULE)
                if row.get("表名称") == sync_row.get("table_name")
            ]),
        }

    def state(self, branch_id: int) -> dict[str, Any] | None:
        sync_row = self._find_sync(branch_id)
        if sync_row is None:
            return None
        return self._state(branch_id, sync_row, self.compatibility(branch_id))


def _now_text() -> str:
    from datetime import datetime

    return datetime.now().strftime("%Y-%m-%d %H:%M")
