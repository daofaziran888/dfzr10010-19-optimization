"""数据分支合并的冲突段续做与合并审计清单。

这个服务是“冲突处置结果”的唯一来源：
- 每个分支合并批次（merge batch）持有 Cxx 冲突段、运行起点版本与目标版本；
- 重入只读取同一批次，跳过已完成段，只继续未完成段；
- 发布目标版本时一次性生成/刷新该批次的冲突处置结果，供合并审计清单和
  其它业务模块（例如表快照）消费；
- 版本、字段映射、裁决与校验结果全部落在这份结果里，避免重入产生重复记录
  或把目标版本退回运行起点。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store

BRANCH_MODULE = "branch"
BATCH_MODULE = "_merge_batches"
AUDIT_MODULE = "_merge_audits"

SEGMENT_DONE = "已完成"
SEGMENT_PENDING = "待裁决"
VALID_VERDICTS = ("源分支优先", "目标分支优先", "合并保留")
DEFAULT_VERDICT = "源分支优先"
VALID_REBASE_MODES = ("current_target", "run_start")
REBASE_LABELS = {
    "current_target": "基于当前目标重放未完成段",
    "run_start": "恢复运行起点版本后重排全部冲突段",
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _segment_order(code: str) -> tuple[int, str]:
    digits = "".join(ch for ch in str(code) if ch.isdigit())
    return (_as_int(digits), str(code))


def _pick(values: dict[str, Any], *names: str, default: Any = None) -> Any:
    """同时接受英文参数名和页面/接口里的中文字段名。"""
    for name in names:
        if name in values and values[name] not in (None, ""):
            return values[name]
    return default


def _normalize_mapping(raw: Any) -> dict[str, dict[str, str]]:
    """把 {C14: {source_field, target_field}} 或中文键写法归一化。"""
    result: dict[str, dict[str, str]] = {}
    if not isinstance(raw, dict):
        return result
    for code, mapping in raw.items():
        if not isinstance(mapping, dict):
            continue
        source = mapping.get("source_field") or mapping.get("源字段") or mapping.get("源")
        target = mapping.get("target_field") or mapping.get("目标字段") or mapping.get("目标")
        result[str(code)] = {
            "source_field": str(source or "").strip(),
            "target_field": str(target or "").strip(),
        }
    return result


class MergeService:
    """读取现有合并批次、续做冲突段并发布唯一目标版本。"""

    # ---------- 查询 ----------

    def find_batch(self, *, branch_id: int | None = None, batch_no: str | None = None) -> dict[str, Any] | None:
        batches = store.rows(BATCH_MODULE)
        matched = [
            batch for batch in batches
            if (branch_id is None or _as_int(batch.get("branch_id")) == _as_int(branch_id))
            and (batch_no is None or batch.get("batch_no") == batch_no)
        ]
        if not matched:
            return None
        # 同一分支可能有历史批次，重入只看最新可继续的批次。
        matched.sort(key=lambda item: (_as_int(item.get("id")), str(item.get("batch_no"))), reverse=True)
        active = [batch for batch in matched if batch.get("status") not in ("已归档", "已删除")]
        return (active or matched)[0]

    def get_branch(self, branch_id: int) -> dict[str, Any] | None:
        return store.find(BRANCH_MODULE, branch_id)

    def segments(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        return sorted(batch.get("segments", []), key=lambda item: _segment_order(item.get("code", "")))

    def completed_segments(self, batch: dict[str, Any]) -> list[str]:
        return [seg["code"] for seg in self.segments(batch) if seg.get("status") == SEGMENT_DONE]

    def pending_segments(self, batch: dict[str, Any]) -> list[str]:
        return [seg["code"] for seg in self.segments(batch) if seg.get("status") != SEGMENT_DONE]

    def field_mapping(self, batch: dict[str, Any]) -> dict[str, Any]:
        mapping: dict[str, dict[str, str]] = {}
        missing: list[str] = []
        for seg in self.segments(batch):
            source = str(seg.get("source_field") or "").strip()
            target = str(seg.get("target_field") or "").strip()
            if not source or not target:
                missing.append(seg.get("code", "?"))
            mapping[seg.get("code", "?")] = {"source_field": source, "target_field": target}
        return {"segments": mapping, "complete": not missing, "missing": missing}

    def ensure_audits(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        """确保每个冲突段都有且只有一条审计记录；重复段不会追加第二行。"""
        audits = store.rows(AUDIT_MODULE)
        batch_no = batch.get("batch_no")
        by_segment: dict[str, dict[str, Any]] = {}
        for record in audits:
            if record.get("batch_no") == batch_no and record.get("segment") not in by_segment:
                by_segment[record.get("segment")] = record

        next_id = max((_as_int(record.get("id")) for record in audits), default=0) + 1
        for seg in self.segments(batch):
            code = seg.get("code")
            record = by_segment.get(code)
            if record is None:
                record = {
                    "id": next_id,
                    "batch_no": batch_no,
                    "branch_id": batch.get("branch_id"),
                    "segment": code,
                    "field_name": seg.get("field_name"),
                    "status": seg.get("status"),
                    "verdict": seg.get("verdict"),
                    "resolution": seg.get("resolution"),
                    "field_mapping": {
                        "source_field": seg.get("source_field"),
                        "target_field": seg.get("target_field"),
                    },
                    "target_version": batch.get("target_version"),
                    "validation": "待裁决" if seg.get("status") != SEGMENT_DONE else "待校验",
                    "effective": True,
                    "version_basis": None,
                    "consumers": [],
                    "team": batch.get("owner_team"),
                    "applied_at": seg.get("applied_at") or _now(),
                }
                audits.append(record)
                by_segment[code] = record
                next_id += 1
            else:
                # 审计记录只做字段补齐，不覆盖已有裁决/消费记录。
                record.setdefault("branch_id", batch.get("branch_id"))
                record.setdefault("field_name", seg.get("field_name"))
                record.setdefault("field_mapping", {
                    "source_field": seg.get("source_field"),
                    "target_field": seg.get("target_field"),
                })
                record.setdefault("consumers", [])
                record.setdefault("effective", True)
                record.setdefault("team", batch.get("owner_team"))
                if seg.get("status") == SEGMENT_DONE and record.get("status") != SEGMENT_DONE:
                    record["status"] = SEGMENT_DONE
                if seg.get("status") == SEGMENT_DONE:
                    record["verdict"] = record.get("verdict") or seg.get("verdict")
                    record["resolution"] = record.get("resolution") or seg.get("resolution")
        self._sync_audit_payload(batch, by_segment)
        return self.audits(batch)

    def audits(self, batch: dict[str, Any]) -> list[dict[str, Any]]:
        batch_no = batch.get("batch_no")
        records = [record for record in store.rows(AUDIT_MODULE) if record.get("batch_no") == batch_no]
        # 同一批次同一冲突段只保留一条：历史重入残留的重复行在这里归并，
        # 已完成的记录优先，其次保留 id 最小的一条，清单不会出现重复变更。
        unique: dict[str, dict[str, Any]] = {}
        duplicates = 0
        for record in records:
            code = record.get("segment")
            existing = unique.get(code)
            if existing is None:
                unique[code] = record
                continue
            duplicates += 1
            winner = existing
            if record.get("status") == SEGMENT_DONE and existing.get("status") != SEGMENT_DONE:
                winner = record
                unique[code] = record
            if winner is existing and record.get("status") == SEGMENT_DONE:
                for field in ("verdict", "resolution", "field_mapping", "applied_at", "team"):
                    if not existing.get(field) and record.get(field):
                        existing[field] = record.get(field)
                existing["status"] = SEGMENT_DONE
            consumers = list(existing.get("consumers") or [])
            for name in record.get("consumers") or []:
                if name not in consumers:
                    consumers.append(name)
            existing["consumers"] = consumers
        batch["duplicate_audit_rows"] = duplicates
        return sorted(unique.values(), key=lambda item: _segment_order(item.get("segment", "")))

    def _sync_audit_payload(self, batch: dict[str, Any], by_segment: dict[str, dict[str, Any]]) -> None:
        """把批次级目标版本与字段映射同步到每条冲突处置结果。"""
        mapping = self.field_mapping(batch)
        for seg in self.segments(batch):
            record = by_segment.get(seg.get("code"))
            if record is None:
                continue
            record["batch_no"] = batch.get("batch_no")
            record["branch_id"] = batch.get("branch_id")
            record["target_version"] = batch.get("target_version")
            record["field_mapping"] = {
                "source_field": seg.get("source_field"),
                "target_field": seg.get("target_field"),
            }
            record["mapping_complete"] = bool(mapping.get("complete"))
            if seg.get("status") == SEGMENT_DONE:
                record["status"] = SEGMENT_DONE
                record["verdict"] = record.get("verdict") or seg.get("verdict")
                record["resolution"] = record.get("resolution") or seg.get("resolution")
            else:
                record["status"] = SEGMENT_PENDING
                record["verdict"] = None
                record["resolution"] = None

    # ---------- 校验与权限 ----------

    def validate(self, batch: dict[str, Any], *, require_complete: bool = False) -> dict[str, Any]:
        # 每个冲突段都要在合并审计清单里有一条记录，缺的补一条再去校验。
        self.ensure_audits(batch)
        audits = self.audits(batch)
        segment_codes = [seg.get("code") for seg in self.segments(batch)]
        errors: list[str] = []

        if not batch.get("target_version"):
            errors.append("合并版本缺少目标表版本")
        elif batch.get("target_version") in (batch.get("run_start_version"), batch.get("target_head_at_run")):
            errors.append("目标表版本仍是运行起点旧版")

        seen: set[str] = {str(record.get("segment")) for record in audits}
        effective_versions = [
            item.get("version") for item in batch.get("published_versions", [])
            if item.get("effective")
        ]
        if len(effective_versions) > 1:
            errors.append(f"合并审计清单存在多个有效版本：{'、'.join(map(str, effective_versions))}")
        if effective_versions and batch.get("target_version") not in effective_versions:
            errors.append("合并审计清单的目标表版本不是当前有效版本")

        missing_codes = [code for code in segment_codes if code not in seen]
        if missing_codes:
            errors.append(f"合并审计清单缺少冲突段：{'、'.join(missing_codes)}")

        mapping = self.field_mapping(batch)
        for seg in self.segments(batch):
            source = str(seg.get("source_field") or "").strip()
            target = str(seg.get("target_field") or "").strip()
            if not source or not target:
                errors.append(f"冲突段 {seg.get('code')} 字段映射不完整（源字段/目标字段都要有）")
            record = next((item for item in audits if item.get("segment") == seg.get("code")), None)
            if record is not None:
                record_mapping = record.get("field_mapping") or {}
                if not str(record_mapping.get("source_field") or "").strip() or not str(
                    record_mapping.get("target_field") or ""
                ).strip():
                    errors.append(f"冲突段 {seg.get('code')} 的冲突处置结果缺少完整字段映射")

        completed_verdicts = {
            record.get("verdict") for record in audits
            if record.get("status") == SEGMENT_DONE and record.get("verdict")
        }
        if len(completed_verdicts) > 1:
            errors.append(f"最终版本存在多个裁决：{'、'.join(sorted(completed_verdicts))}")
        if batch.get("verdict") and completed_verdicts and batch.get("verdict") not in completed_verdicts:
            errors.append("合并审计清单中的裁决与批次唯一裁决不一致")

        pending = self.pending_segments(batch)
        if require_complete and pending:
            errors.append(f"仍有未完成冲突段：{'、'.join(pending)}")

        result = {
            "ok": not errors,
            "errors": errors,
            "checked_at": _now(),
            "complete": not pending,
            "completed_segments": self.completed_segments(batch),
            "pending_segments": pending,
            "mapping_complete": bool(mapping.get("complete")),
            "unique_verdict": next(iter(completed_verdicts), None) if len(completed_verdicts) <= 1 else None,
            "segment_count": len(segment_codes),
            "audit_count": len(audits),
        }
        batch["validation"] = result
        self._apply_validation_to_audits(batch, result)
        return result

    def _apply_validation_to_audits(self, batch: dict[str, Any], validation: dict[str, Any]) -> None:
        errors = validation.get("errors") or []
        for record in self.audits(batch):
            if record.get("status") != SEGMENT_DONE:
                record["validation"] = "待裁决"
            elif errors:
                relevant = any(str(record.get("segment")) in error for error in errors)
                record["validation"] = "校验未通过" if relevant else "校验通过"
            else:
                record["validation"] = "校验通过"
            record["batch_validation_ok"] = bool(validation.get("ok"))

    def permission(self, batch: dict[str, Any], team: str | None) -> dict[str, Any]:
        owner = str(batch.get("owner_team") or "")
        actor_team = str(team or owner).strip() or owner
        allowed = not actor_team or actor_team == owner
        return {
            "owner_team": owner,
            "team": actor_team,
            "can_edit": allowed,
            "read_only": not allowed,
            "view_scope": "可操作版本" if allowed else "受控版本",
            "message": "" if allowed else f"越权修改被拒绝：{actor_team} 无权处置 {owner} 的合并批次，只能查看受控版本",
        }

    # ---------- 状态组装 ----------

    def public_segments(self, batch: dict[str, Any], *, read_only: bool = False) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for seg in self.segments(batch):
            result.append({
                "code": seg.get("code"),
                "field_name": seg.get("field_name"),
                "status": seg.get("status"),
                "verdict": seg.get("verdict"),
                "resolution": seg.get("resolution"),
                "field_mapping": {
                    "source_field": seg.get("source_field"),
                    "target_field": seg.get("target_field"),
                },
                "applied_version": seg.get("applied_version"),
                "applied_at": seg.get("applied_at"),
                "read_only": read_only,
            })
        return result

    def public_audits(self, batch: dict[str, Any], permission: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for record in self.audits(batch):
            item = dict(record)
            item["visibility"] = "受控版本" if permission.get("read_only") else "可操作版本"
            item["read_only"] = bool(permission.get("read_only"))
            items.append(item)
        return items

    def effective_version(self, batch: dict[str, Any]) -> dict[str, Any]:
        validation = batch.get("validation") or self.validate(batch)
        mapping = self.field_mapping(batch)
        return {
            "version": batch.get("target_version"),
            "status": batch.get("version_status"),
            "effective": True,
            "effective_count": sum(
                1 for item in batch.get("published_versions", []) if item.get("effective")
            ),
            "verdict": batch.get("verdict"),
            "version_basis": batch.get("version_basis"),
            "rebase_mode": batch.get("rebase_mode"),
            "field_mapping": mapping,
            "mapping_complete": bool(mapping.get("complete")),
            "field_mapping_detail": mapping,
            "validation": validation,
            "published_at": batch.get("published_at"),
        }

    def state(
        self,
        branch_id: int,
        *,
        team: str | None = None,
        include_history: bool = False,
    ) -> dict[str, Any] | None:
        branch = self.get_branch(branch_id)
        batch = self.find_batch(branch_id=branch_id)
        if batch is None:
            return None
        self.ensure_audits(batch)
        validation = self.validate(batch)
        permission = self.permission(batch, team)
        completed = self.completed_segments(batch)
        pending = self.pending_segments(batch)
        state: dict[str, Any] = {
            "ok": True,
            "branch_id": branch_id,
            "branch": {
                "id": branch_id,
                "分支编号": branch.get("分支编号") if branch else batch.get("branch_code"),
                "表名称": branch.get("表名称") if branch else batch.get("table_name"),
                "源分支": branch.get("源分支") if branch else batch.get("source_branch"),
                "目标分支": branch.get("目标分支") if branch else batch.get("target_branch"),
                "分支状态": branch.get("status") if branch else None,
            },
            "batch": self.batch_summary(batch, permission),
            "segments": self.public_segments(batch, read_only=bool(permission.get("read_only"))),
            "completed_segments": completed,
            "pending_segments": pending,
            "next_segment": pending[0] if pending else None,
            "target_version": batch.get("target_version"),
            "effective_version": self.effective_version(batch),
            "audit_records": self.public_audits(batch, permission),
            "validation": validation,
            "permissions": permission,
            "read_only": bool(permission.get("read_only")),
            "target_moved": batch.get("current_target_head") != batch.get("target_head_at_run"),
            "current_target_head": batch.get("current_target_head"),
            "run_start_version": batch.get("run_start_version"),
        }
        if include_history:
            state["version_history"] = batch.get("published_versions", [])
        return state

    def batch_summary(self, batch: dict[str, Any], permission: dict[str, Any] | None = None) -> dict[str, Any]:
        return {
            "batch_no": batch.get("batch_no"),
            "branch_id": batch.get("branch_id"),
            "branch_code": batch.get("branch_code"),
            "table_name": batch.get("table_name"),
            "source_branch": batch.get("source_branch"),
            "target_branch": batch.get("target_branch"),
            "owner_team": batch.get("owner_team"),
            "status": batch.get("status"),
            "run_start_version": batch.get("run_start_version"),
            "target_head_at_run": batch.get("target_head_at_run"),
            "current_target_head": batch.get("current_target_head"),
            "target_version": batch.get("target_version"),
            "version_status": batch.get("version_status"),
            "version_basis": batch.get("version_basis"),
            "rebase_mode": batch.get("rebase_mode"),
            "verdict": batch.get("verdict"),
            "completed_segments": self.completed_segments(batch),
            "pending_segments": self.pending_segments(batch),
            "duplicate_audit_rows": batch.get("duplicate_audit_rows", 0),
            "permissions": permission or self.permission(batch, None),
        }

    # ---------- 重入续做 ----------

    def reenter(self, branch_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str, dict[str, Any] | None]:
        batch = self.find_batch(branch_id=branch_id)
        if batch is None:
            return None, f"分支 {branch_id} 没有可续做的合并批次", None
        self.ensure_audits(batch)

        team = _pick(values, "team", "operator_team", "小组", "操作小组")
        permission = self.permission(batch, team)
        if not permission.get("can_edit"):
            return None, permission["message"], {"permissions": permission, "read_only": True}

        pending = self.pending_segments(batch)
        requested_raw = _pick(values, "segments", "冲突段", "冲突段列表")
        if requested_raw:
            if isinstance(requested_raw, str):
                requested = [code.strip() for code in requested_raw.split(",") if code.strip()]
            else:
                requested = [str(code).strip() for code in requested_raw if str(code).strip()]
            unknown = [code for code in requested if code not in {seg["code"] for seg in self.segments(batch)}]
            if unknown:
                return None, f"冲突段不存在：{'、'.join(unknown)}", None
            # 已完成段是幂等的：可以在请求里出现，但不会被再次套用。
            requested = [code for code in requested if code in pending]
        else:
            requested = list(pending)

        if not pending:
            state = self.state(branch_id, team=permission.get("team"), include_history=True)
            return state, "没有未完成冲突段，已完成段不会再次应用", None
        if not requested:
            state = self.state(branch_id, team=permission.get("team"), include_history=True)
            return state, "本次没有需要续做的冲突段，已完成段保持原样", None

        verdict = str(_pick(values, "verdict", "裁决", default=batch.get("verdict") or DEFAULT_VERDICT)).strip()
        if verdict not in VALID_VERDICTS:
            return None, f"裁决「{verdict}」不在允许范围内：{'、'.join(VALID_VERDICTS)}", None

        rebase_mode = str(_pick(values, "rebase_mode", "重入方式", default="current_target")).strip()
        if rebase_mode not in VALID_REBASE_MODES:
            return None, f"重入方式「{rebase_mode}」不支持，只能是 current_target 或 run_start", None

        existing_verdicts = {
            record.get("verdict") for record in self.audits(batch)
            if record.get("status") == SEGMENT_DONE and record.get("verdict")
        }
        if existing_verdicts and (len(existing_verdicts) > 1 or verdict not in existing_verdicts):
            return None, "最终版本必须使用唯一裁决，不能与已完成冲突段的裁决冲突", None

        overrides = _normalize_mapping(_pick(values, "field_mapping", "字段映射", default={}))
        raw_overrides = _pick(values, "field_mapping", "字段映射", default={})
        if raw_overrides and not isinstance(raw_overrides, dict):
            return None, "字段映射格式不正确", None

        segment_by_code = {seg["code"]: seg for seg in self.segments(batch)}
        prepared: list[tuple[dict[str, Any], dict[str, str]]] = []
        for code in requested:
            seg = segment_by_code[code]
            override = overrides.get(code)
            source = str((override or {}).get("source_field") or seg.get("source_field") or "").strip()
            target = str((override or {}).get("target_field") or seg.get("target_field") or "").strip()
            if not source or not target:
                return None, f"冲突段 {code} 缺少完整字段映射，重入已拒绝", None
            prepared.append((seg, {"source_field": source, "target_field": target}))

        # 目标分支出现新提交时，版本依据由重入方式决定；两种方式都只发布一个有效版本。
        target_moved = batch.get("current_target_head") != batch.get("target_head_at_run")
        version_basis = (
            f"恢复运行起点 {batch.get('run_start_version')} 后重排全部冲突段"
            if rebase_mode == "run_start"
            else f"基于当前目标 {batch.get('current_target_head')} 重放未完成段"
        )
        # 即使恢复运行起点，已完成段也只重排版本依据、不重新套用，避免重复变更。
        replanned = [seg["code"] for seg in self.segments(batch)] if rebase_mode == "run_start" else list(requested)

        applied_now: list[str] = []
        for seg, mapping in prepared:
            code = seg["code"]
            seg["status"] = SEGMENT_DONE
            seg["verdict"] = verdict
            seg["resolution"] = str(
                _pick(values, "resolution", "处置说明", "处置结果")
                or f"{verdict}：{mapping['source_field']} → {mapping['target_field']}"
            )
            seg["source_field"] = mapping["source_field"]
            seg["target_field"] = mapping["target_field"]
            seg["applied_at"] = _now()
            seg["team"] = permission.get("team")
            applied_now.append(code)

            record = next((item for item in self.audits(batch) if item.get("segment") == code), None)
            if record is None:
                record = {"id": max((_as_int(item.get("id")) for item in store.rows(AUDIT_MODULE)), default=0) + 1}
                store.rows(AUDIT_MODULE).append(record)
            record.update({
                "batch_no": batch.get("batch_no"),
                "branch_id": branch_id,
                "segment": code,
                "field_name": seg.get("field_name"),
                "status": SEGMENT_DONE,
                "verdict": verdict,
                "resolution": seg["resolution"],
                "field_mapping": dict(mapping),
                "effective": True,
                "team": permission.get("team"),
                "applied_at": seg["applied_at"],
                "consumers": record.get("consumers") or [],
            })

        batch["verdict"] = verdict
        batch["rebase_mode"] = rebase_mode
        batch["version_basis"] = version_basis
        batch["updated_at"] = _now()

        # 只要续做了冲突段就发布一个新的唯一目标版本；未完成时版本状态为合并中。
        new_version = self._publish_version(batch, verdict, version_basis)
        for code in applied_now:
            segment_by_code[code]["applied_version"] = new_version
        for record in self.audits(batch):
            record["target_version"] = new_version
            record["version_basis"] = version_basis

        pending_after = self.pending_segments(batch)
        validation = self.validate(batch, require_complete=not pending_after)
        self._sync_branch_status(batch, pending_after)

        state = self.state(branch_id, team=permission.get("team"), include_history=True)
        if state is not None:
            state["reentry"] = {
                "applied_segments": applied_now,
                "skipped_completed_segments": sorted(
                    set(self.completed_segments(batch)) - set(applied_now), key=_segment_order
                ),
                "pending_segments": pending_after,
                "target_moved": target_moved,
                "rebase_mode": rebase_mode,
                "rebase_label": REBASE_LABELS[rebase_mode],
                "replanned_segments": replanned,
                "rebased_from": batch.get("run_start_version") if rebase_mode == "run_start" else batch.get("current_target_head"),
                "published_version": new_version,
                "batch_no": batch.get("batch_no"),
                "validation": validation,
            }
        message = (
            f"重入从 {applied_now[0]} 继续，已完成 {len(applied_now)} 个冲突段，发布唯一目标版本 {new_version}"
        )
        if pending_after:
            message += f"，待裁冲突段：{'、'.join(pending_after)}"
        return state, message, state.get("reentry") if state else None

    def _publish_version(self, batch: dict[str, Any], verdict: str, version_basis: str) -> str:
        seq = _as_int(batch.get("target_version_seq"), 0) + 1
        table = str(batch.get("target_table") or "reporting_tbl")
        prefix = str(batch.get("version_prefix") or datetime.now().strftime("%Y.%m.%d"))
        new_version = f"{table}@v{prefix}-{seq:02d}"
        history = batch.setdefault("published_versions", [])
        for item in history:
            item["effective"] = False
            item["status"] = "已替代"
        history.append({
            "version": new_version,
            "effective": True,
            "status": "合并中" if self.pending_segments(batch) else "已发布",
            "verdict": verdict,
            "version_basis": version_basis,
            "published_at": _now(),
            "field_mapping_complete": bool(self.field_mapping(batch).get("complete")),
        })
        batch["target_version_seq"] = seq
        batch["target_version"] = new_version
        batch["version_status"] = "合并中" if self.pending_segments(batch) else "已发布"
        batch["published_at"] = _now()
        return new_version

    def _sync_branch_status(self, batch: dict[str, Any], pending: list[str]) -> None:
        branch = self.get_branch(_as_int(batch.get("branch_id")))
        if branch is None:
            return
        if pending:
            batch["status"] = "合并中"
            branch["status"] = "合并中"
            branch["pending"] = True
            branch["合并进度"] = f"{len(self.completed_segments(batch))}/{len(self.segments(batch))}"
            branch["待裁冲突段"] = "、".join(pending)
        else:
            batch["status"] = "已合并"
            batch["version_status"] = "已发布"
            branch["status"] = "已合并"
            branch["pending"] = False
            branch["合并进度"] = f"{len(self.segments(batch))}/{len(self.segments(batch))}"
            branch["待裁冲突段"] = "无"
        branch["合并时间"] = _now()

    def target_commit(self, branch_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        batch = self.find_batch(branch_id=branch_id)
        if batch is None:
            return None, f"分支 {branch_id} 没有合并批次"
        permission = self.permission(batch, _pick(values, "team", "operator_team", "小组", "操作小组"))
        if not permission.get("can_edit"):
            return None, permission["message"]
        head = str(_pick(values, "head", "target_head", "目标提交", "目标分支提交", default="")).strip()
        if not head:
            return None, "缺少目标分支提交版本"
        batch["current_target_head"] = head
        batch["target_moved_at"] = _now()
        batch["updated_at"] = _now()
        return self.state(branch_id, team=permission.get("team")), f"目标分支新提交已登记：{head}"

    # ---------- 合并审计清单 / 消费 ----------

    def audit_list(
        self,
        *,
        branch_id: int | None = None,
        batch_no: str | None = None,
        team: str | None = None,
        include_history: bool = False,
    ) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []
        if branch_id is not None:
            batch = self.find_batch(branch_id=branch_id, batch_no=batch_no)
            if batch is not None:
                batches = [batch]
        elif batch_no:
            batch = self.find_batch(batch_no=batch_no)
            if batch is not None:
                batches = [batch]
        else:
            seen: set[str] = set()
            for batch in sorted(store.rows(BATCH_MODULE), key=lambda item: _as_int(item.get("id"))):
                if batch.get("batch_no") in seen:
                    continue
                seen.add(batch.get("batch_no"))
                batches.append(batch)

        all_items: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        for batch in batches:
            self.ensure_audits(batch)
            validation = self.validate(batch)
            permission = self.permission(batch, team)
            items = self.public_audits(batch, permission)
            all_items.extend(items)
            summary = {
                "batch_no": batch.get("batch_no"),
                "branch_id": batch.get("branch_id"),
                "branch_code": batch.get("branch_code"),
                "source_branch": batch.get("source_branch"),
                "target_branch": batch.get("target_branch"),
                "table_name": batch.get("table_name"),
                "owner_team": batch.get("owner_team"),
                "status": batch.get("status"),
                "target_version": batch.get("target_version"),
                "version_status": batch.get("version_status"),
                "version_basis": batch.get("version_basis"),
                "verdict": batch.get("verdict"),
                "completed_segments": self.completed_segments(batch),
                "pending_segments": self.pending_segments(batch),
                "effective_version": self.effective_version(batch),
                "validation": validation,
                "permissions": permission,
                "read_only": bool(permission.get("read_only")),
                "duplicate_audit_rows": batch.get("duplicate_audit_rows", 0),
                "items": items,
                "total": len(items),
            }
            if include_history:
                summary["version_history"] = batch.get("published_versions", [])
            summaries.append(summary)

        payload: dict[str, Any] = {
            "items": all_items,
            "total": len(all_items),
            "batches": summaries,
            "include_history": include_history,
        }
        if len(summaries) == 1:
            payload["batch"] = summaries[0]
            payload["effective_version"] = summaries[0]["effective_version"]
            payload["validation"] = summaries[0]["validation"]
            payload["permissions"] = summaries[0]["permissions"]
        return payload

    def consume(
        self,
        branch_id: int,
        module: str | None = None,
        *,
        team: str | None = None,
        version_basis: str | None = None,
        actor: str | None = None,
    ) -> dict[str, Any]:
        batch = self.find_batch(branch_id=branch_id)
        if batch is None:
            return {"ok": False, "message": f"分支 {branch_id} 没有合并批次", "consumers": []}
        self.ensure_audits(batch)
        validation = self.validate(batch)
        if not validation.get("ok"):
            return {
                "ok": False,
                "message": "冲突处置结果校验未通过，不能同步版本依据",
                "validation": validation,
            }
        consumer = str(module or "").strip() or "其它业务模块"
        basis = str(version_basis or batch.get("target_version") or "")
        consumed_segments: list[str] = []
        for record in self.audits(batch):
            if record.get("status") != SEGMENT_DONE:
                continue
            consumers = record.setdefault("consumers", [])
            if consumer not in consumers:
                consumers.append(consumer)
            record["version_basis"] = basis
            record["consumed_by"] = consumer
            record["consumed_at"] = _now()
            if actor:
                record["consumed_operator"] = actor
            consumed_segments.append(record.get("segment"))
        batch["last_consumer"] = consumer
        batch["last_version_basis"] = basis
        batch["updated_at"] = _now()
        return {
            "ok": True,
            "message": f"{consumer} 已消费冲突处置结果，版本依据已同步到合并审计清单",
            "batch_no": batch.get("batch_no"),
            "version_basis": basis,
            "consumers": sorted({name for record in self.audits(batch) for name in record.get("consumers", [])}),
            "consumed_segments": consumed_segments,
            "validation": validation,
        }

    def list_batches(self) -> list[dict[str, Any]]:
        summaries: list[dict[str, Any]] = []
        for batch in sorted(store.rows(BATCH_MODULE), key=lambda item: _as_int(item.get("id"))):
            summaries.append({
                "batch_no": batch.get("batch_no"),
                "branch_id": batch.get("branch_id"),
                "branch_code": batch.get("branch_code"),
                "source_branch": batch.get("source_branch"),
                "target_branch": batch.get("target_branch"),
                "table_name": batch.get("table_name"),
                "owner_team": batch.get("owner_team"),
                "status": batch.get("status"),
                "target_version": batch.get("target_version"),
                "version_status": batch.get("version_status"),
                "completed_segments": self.completed_segments(batch),
                "pending_segments": self.pending_segments(batch),
            })
        return summaries


merge_service = MergeService()
