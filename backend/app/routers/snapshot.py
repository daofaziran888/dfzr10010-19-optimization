"""表快照管理接口：维护快照记录，覆盖创建快照、恢复快照、清理快照等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.snapshot import SnapshotMergeSyncService, SnapshotService

router = APIRouter(prefix="/api/snapshot", tags=["表快照管理"])

service = SnapshotService()
merge_sync_service = SnapshotMergeSyncService()

LIST_FIELDS = ["快照编号", "表名称", "快照时间", "快照版本", "快照大小", "保留天数", "创建方式", "快照状态"]
STATUSES = ["已创建", "保留中", "已过期", "已清理"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按快照编号检索"),
    status: str | None = Query(default=None, description="已创建、保留中、已过期、已清理"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按快照编号与状态过滤表快照管理列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/merge-sync/{branch_id}")
def merge_sync_state(branch_id: int) -> dict:
    """读取快照模块对某一合并批次的同步进度；未同步过时给出空进度而不是 404。"""
    state = merge_sync_service.state(branch_id)
    if state is None:
        return {"ok": True, "branch_id": branch_id, "status": "未同步", "synced_segments": []}
    return state


@router.post("/merge-sync/{branch_id}/compatibility")
def merge_compatibility(branch_id: int, payload: EntryPayload) -> ActionResult:
    """只校验合并审计清单中冲突处置结果的兼容性，不改任何快照明细。"""
    result = merge_sync_service.compatibility(branch_id, team=payload.values.get("team") or payload.values.get("operator_team") or payload.values.get("操作小组"))
    message = "冲突处置结果兼容，可继续同步" if result.get("ok") else "；".join(result.get("errors", []))
    return ActionResult(ok=bool(result.get("ok")), message=message, entry=result)


@router.post("/merge-sync/{branch_id}/sync")
def merge_sync(branch_id: int, payload: EntryPayload) -> ActionResult:
    """消费冲突处置结果：兼容性校验通过后保留已有明细，只继续未完成分段并落快照。"""
    result, message = merge_sync_service.sync(branch_id, payload.values)
    if result is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=result)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出表快照管理清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "snapshot", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条快照记录明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"快照记录 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条快照记录，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="快照记录已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条快照记录执行创建快照、恢复快照、清理快照；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


