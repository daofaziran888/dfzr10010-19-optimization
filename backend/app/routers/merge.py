"""数据分支合并冲突段续做与合并审计清单接口。

与 /api/branch 分开挂在同一前缀下，避免分支记录的动作接口继续只做状态流转：
- GET  /api/branch/merge/batches
- GET  /api/branch/merge/audits
- POST /api/branch/merge/audits/{batch_no}/consume
- GET  /api/branch/{entry_id}/merge
- POST /api/branch/{entry_id}/merge/reenter
- POST /api/branch/{entry_id}/target-commit
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.merge import merge_service

router = APIRouter(prefix="/api/branch", tags=["数据分支合并"])


@router.get("/merge/batches")
def list_batches() -> dict[str, Any]:
    """读取现有分支合并批次，重入前先确认批次号与进度。"""
    items = merge_service.list_batches()
    return {"items": items, "total": len(items)}


@router.get("/merge/audits")
def list_audits(
    branch_id: int | None = Query(default=None, description="分支记录 id"),
    batch_no: str | None = Query(default=None, description="合并批次号"),
    team: str | None = Query(default=None, description="当前操作小组，用于判定可见范围"),
    include_history: bool = Query(default=False, description="是否带出已替代的目标版本"),
) -> dict[str, Any]:
    """合并审计清单：同一批次的所有冲突处置结果归在一起，重入不会追加重复行。"""
    payload = merge_service.audit_list(
        branch_id=branch_id, batch_no=batch_no, team=team, include_history=include_history
    )
    if not payload.get("batches"):
        raise HTTPException(status_code=404, detail="没有匹配的合并批次或合并审计清单")
    return payload


@router.post("/merge/audits/{batch_no}/consume")
def consume_audits(batch_no: str, payload: EntryPayload) -> ActionResult:
    """其它业务模块消费冲突处置结果：把版本依据同步到合并审计清单。"""
    values = payload.values
    batch = merge_service.find_batch(batch_no=batch_no)
    if batch is None:
        return ActionResult(ok=False, message=f"合并批次 {batch_no} 不存在")
    result = merge_service.consume(
        int(batch.get("branch_id") or 0),
        str(values.get("module") or values.get("consumer") or values.get("消费模块") or "其它业务模块"),
        team=values.get("team") or values.get("操作小组"),
        version_basis=values.get("version_basis") or values.get("版本依据"),
        actor=values.get("operator") or values.get("操作人"),
    )
    return ActionResult(ok=bool(result.get("ok")), message=str(result.get("message")), entry=result)


@router.get("/{entry_id}/merge")
def merge_state(
    entry_id: int,
    team: str | None = Query(default=None, description="当前操作小组"),
    include_history: bool = Query(default=False, description="是否带出已替代的目标版本"),
) -> dict[str, Any]:
    """查看已完成的冲突段、待裁冲突段与唯一目标版本。"""
    state = merge_service.state(entry_id, team=team, include_history=include_history)
    if state is None:
        raise HTTPException(status_code=404, detail=f"分支 {entry_id} 没有可续做的合并批次")
    return state


@router.post("/{entry_id}/merge/reenter")
def reenter_merge(entry_id: int, payload: EntryPayload) -> ActionResult:
    """重入冲突段续做：读取现有批次，跳过已完成段，只继续未完成段并发布唯一目标版本。"""
    state, message, reentry = merge_service.reenter(entry_id, payload.values)
    if state is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=state)


@router.post("/{entry_id}/target-commit")
def target_commit(entry_id: int, payload: EntryPayload) -> ActionResult:
    """登记目标分支的新提交，用于模拟重入时目标表已向前移动。"""
    state, message = merge_service.target_commit(entry_id, payload.values)
    if state is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=state)
