"""变更捕获领域服务：Schema和元数据的变更捕获、版本对比和差异通知。

Pipeline 6: Change capture → diff reporting → version history

采集侧支持带类型的字段映射：before/after 以字段名为键，值的真实类型
即采集类型（整数 7 与文本 "7" 落库后依然可分），某侧缺少的键视为缺列。
"""
from __future__ import annotations

import json
import time as _time
from typing import Any

from app.diff_types import TYPE_MISSING, typed_value
from app.services.base import BaseModuleService
from app.store import store


class ChangeCaptureService(BaseModuleService):
    MODULE = "change_capture"
    ENTITY = "变更记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "目标资产", "变更类型", "变更前值", "变更后值", "变更来源", "变更时间", "影响范围"]
    REQUIRED_FIELDS = ["记录编号", "目标资产", "变更类型"]
    STATUS_ORDER = ["已捕获", "已对比", "已通知", "已确认"]
    ACTION_RULES = {"启动对比": "已对比", "发送通知": "已通知", "确认变更": "已确认"}
    NEGATIVE_ACTIONS = []

    def capture_change(self, asset_code: str, change_type: str, before: Any, after: Any,
                       source: str = "schema_evolution") -> dict[str, Any]:
        """捕获一次变更并自动触发差异对比和版本快照（Pipeline 6 入口）。"""
        change_id = store.next_id(self.MODULE)
        change = {
            "id": change_id,
            "记录编号": f"CHAN-{change_id:04d}",
            "目标资产": asset_code,
            "变更类型": change_type,
            "变更前值": str(before),
            "变更后值": str(after),
            "变更来源": source,
            "变更时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "影响范围": self._compute_impact_scope(asset_code, change_type),
            "status": "已捕获",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(change)

        # Pipeline 6: 自动触发差异报告和版本快照
        diff_result = self._generate_diff_report(change)
        version_snap = self._create_version_snapshot(change)

        # 记录血缘中受影响的下游资产
        affected_downstream = self._find_downstream_assets(asset_code)

        store.log_change(
            self.MODULE, change_id, "变更捕获完成",
            before=change_type, after=f"差异{diff_result.get('diff_count',0)}条，影响{len(affected_downstream)}个下游",
            source="pipeline_6", scope="change_capture->diff_report->version_history"
        )

        self.emit_event("change_captured", {
            "change_id": change_id,
            "asset_code": asset_code,
            "change_type": change_type,
            "downstream_count": len(affected_downstream),
        })

        return {
            "change": change,
            "diff_report": diff_result,
            "version_snapshot": version_snap,
            "affected_downstream": affected_downstream,
            "pipeline": "change_capture->diff_report->version_history",
        }

    def capture_typed_change(self, asset_code: str, change_type: str,
                             before_map: dict[str, Any] | None,
                             after_map: dict[str, Any] | None,
                             source: str = "typed_collection") -> dict[str, Any]:
        """捕获带类型映射的变更：按字段逐一生成带类型的差异记录。

        before_map/after_map 的键是字段名，值的真实类型即采集类型；
        某侧缺少的键视为缺列（missing），不与空值、文本零混淆。
        """
        from app.services.diff_report import DiffReportService  # 延迟导入，同 schedule_job

        before_typed = {k: typed_value(v) for k, v in (before_map or {}).items()}
        after_typed = {k: typed_value(v) for k, v in (after_map or {}).items()}

        change_id = store.next_id(self.MODULE)
        change = {
            "id": change_id,
            "记录编号": f"CHAN-{change_id:04d}",
            "目标资产": asset_code,
            "变更类型": change_type,
            "变更前值": json.dumps({k: v["text"] for k, v in before_typed.items()}, ensure_ascii=False),
            "变更后值": json.dumps({k: v["text"] for k, v in after_typed.items()}, ensure_ascii=False),
            "变更前值类型映射": before_typed,
            "变更后值类型映射": after_typed,
            "变更来源": source,
            "变更时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "影响范围": self._compute_impact_scope(asset_code, change_type),
            "status": "已捕获",
            "pending": True,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(change)

        # Pipeline 6（类型化）：逐字段生成带类型映射的差异记录
        diff_service = DiffReportService()
        fields = list(dict.fromkeys([*before_typed.keys(), *after_typed.keys()]))
        diff_reports = []
        for field in fields:
            from_typed = before_typed.get(field) or typed_value(None, missing=True)
            to_typed = after_typed.get(field) or typed_value(None, missing=True)
            level = "修改"
            if from_typed["type"] == TYPE_MISSING:
                level = "新增"
            elif to_typed["type"] == TYPE_MISSING:
                level = "删除"
            diff_reports.append(diff_service.generate_typed_report(
                asset_code, change_type, field, from_typed, to_typed, level=level))

        version_snap = self._create_version_snapshot(change)
        affected_downstream = self._find_downstream_assets(asset_code)

        store.log_change(
            self.MODULE, change_id, "变更捕获完成",
            before=change_type, after=f"差异{len(diff_reports)}条，影响{len(affected_downstream)}个下游",
            source="pipeline_6", scope="change_capture->diff_report->version_history"
        )

        self.emit_event("change_captured", {
            "change_id": change_id,
            "asset_code": asset_code,
            "change_type": change_type,
            "typed": True,
            "downstream_count": len(affected_downstream),
        })

        return {
            "change": change,
            "diff_reports": diff_reports,
            "version_snapshot": version_snap,
            "affected_downstream": affected_downstream,
            "pipeline": "change_capture->diff_report->version_history",
        }

    def _compute_impact_scope(self, asset_code: str, change_type: str) -> str:
        """计算影响范围。"""
        downstream = self._find_downstream_assets(asset_code)
        if downstream:
            return f"影响{len(downstream)}个下游资产: {', '.join(downstream)}"
        return f"无下游依赖"

    def _generate_diff_report(self, change: dict[str, Any]) -> dict[str, Any]:
        """生成差异报告。"""
        diff_id = store.next_id("diff_report")
        diff = {
            "id": diff_id,
            "记录编号": f"DIFF-CHG-{diff_id:04d}",
            "比较对象": change.get("目标资产", ""),
            "比较维度": change.get("变更类型", ""),
            "差异字段": change.get("变更类型", ""),
            "源值": change.get("变更前值", ""),
            "目标值": change.get("变更后值", ""),
            "差异等级": "修改",
            "报告时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": False,
            "abnormal": False,
            "change_id": change["id"],
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows("diff_report").append(diff)
        return {"diff_id": diff_id, "diff_count": 1}

    def _create_version_snapshot(self, change: dict[str, Any]) -> dict[str, Any]:
        """创建版本快照记录变更历史。"""
        store.create_snapshot(f"change-{change['id']}", [change.get("目标资产", "unknown")])
        return {"snapshot_id": f"change-{change['id']}", "created": True}

    def _find_downstream_assets(self, asset_code: str) -> list[str]:
        """查找下游资产（通过血缘关系）。"""
        downstream = []
        for row in store.rows("lineage_track"):
            if row.get("上游资产") == asset_code:
                ds = row.get("下游资产", "")
                if ds and ds not in downstream:
                    downstream.append(ds)
        return downstream

    def compare_versions(self, asset_code: str, version_a: str, version_b: str) -> list[dict[str, Any]]:
        """比较资产的两个版本差异。"""
        diffs = store.diff_snapshots(version_a, version_b, asset_code)
        return diffs

    def get_changes_by_asset(self, asset_code: str) -> list[dict[str, Any]]:
        """获取资产的所有变更记录。"""
        return store.find_by_field(self.MODULE, "目标资产", asset_code)

    def get_pending_changes(self) -> list[dict[str, Any]]:
        """获取未确认的变更。"""
        return [
            r for r in store.rows(self.MODULE)
            if r.get("status") in ("已捕获", "已对比")
        ]