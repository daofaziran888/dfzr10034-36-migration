"""差异报告领域服务：数据资产的结构化差异对比、版本间变更汇总和差异评估。

接收变更捕获的事件自动生成差异报告。

类型化迁移说明：
- 旧版记录只有文本字段（源值/目标值），文本零、数值零与缺列在旧读法
  里会冲突；新版记录额外落库一份「类型映射」（见 app.diff_types），
  原文与类型同时保留；
- 存量迁移只补挂 legacy 类型映射，不改写记录编号与原文，也不凭字符
  外观猜类型；有歧义的存量报告保留原始字段和值；
- 消费方确认入口（确认差异动作 / confirm_diff）保持不变，逐条确认，
  不会把文本零、数值零、缺列的记录合成一条；
- 差异汇总随确认结果更新；读取路径回滚后，已确认的差异仍能按落库的
  类型映射回放。
"""
from __future__ import annotations

import time as _time
from typing import Any

from app.diff_types import (
    TYPE_LEGACY,
    legacy_value,
    looks_ambiguous,
)
from app.services.base import BaseModuleService
from app.store import store


class DiffReportService(BaseModuleService):
    MODULE = "diff_report"
    ENTITY = "差异记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "比较对象", "比较维度", "差异字段", "源值", "目标值", "差异等级", "报告时间"]
    REQUIRED_FIELDS = ["记录编号", "比较对象", "比较维度"]
    STATUS_ORDER = ["待生成", "已生成", "已确认", "已忽略"]
    ACTION_RULES = {"生成报告": "已生成", "确认差异": "已确认", "忽略差异": "已忽略"}
    NEGATIVE_ACTIONS = ["忽略差异"]

    SUMMARY_KEY = "diff_report:summary"
    READ_PATH_KEY = "diff_report:read_path"
    RECONCILE_KEY = "diff_report:reconcile"

    def generate_report(self, subject: str, dimension: str, field: str, from_val: Any,
                        to_val: Any, level: str = "修改") -> dict[str, Any]:
        """生成差异报告（旧版纯文本入口，保留兼容）。"""
        report_id = store.next_id(self.MODULE)
        report = {
            "id": report_id,
            "记录编号": f"DIFF-{report_id:04d}",
            "比较对象": subject,
            "比较维度": dimension,
            "差异字段": field,
            "源值": str(from_val),
            "目标值": str(to_val),
            "差异等级": level,
            "报告时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.rows(self.MODULE).append(report)
        self.refresh_summary()

        self.emit_event("diff_generated", {
            "report_id": report_id,
            "subject": subject,
            "level": level,
        })

        return report

    # ── 带类型的差异记录 ──────────────────────────────────────

    def generate_typed_report(self, subject: str, dimension: str, field: str,
                              from_typed: dict[str, Any], to_typed: dict[str, Any],
                              level: str = "修改", round_id: str | None = None) -> dict[str, Any]:
        """生成带类型映射的差异报告：原文与类型一并落库。"""
        report_id = store.next_id(self.MODULE)
        report = {
            "id": report_id,
            "记录编号": f"DIFF-{report_id:04d}",
            "比较对象": subject,
            "比较维度": dimension,
            "差异字段": field,
            "源值": from_typed.get("text", ""),
            "目标值": to_typed.get("text", ""),
            "类型映射": {"源值": dict(from_typed), "目标值": dict(to_typed)},
            "差异等级": level,
            "报告时间": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "status": "已生成",
            "pending": False,
            "abnormal": False,
            "created_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if round_id:
            report["对账轮次"] = round_id
        store.rows(self.MODULE).append(report)
        self.refresh_summary()

        self.emit_event("diff_generated", {
            "report_id": report_id,
            "subject": subject,
            "level": level,
            "typed": True,
        })
        return report

    def migrate_legacy_reports(self) -> dict[str, Any]:
        """把旧版文本差异记录迁移到类型映射。

        只补挂 legacy 类型映射，不改写记录编号与源值/目标值原文；存量值
        一律标记 legacy，不凭字符外观猜类型；旧读法下有歧义的报告加歧义
        标记并保留原始字段和值。可重复执行（幂等）。
        """
        migrated = 0
        skipped = 0
        ambiguous = 0
        for row in store.rows(self.MODULE):
            if row.get("类型映射"):
                skipped += 1
                continue
            row["类型映射"] = {
                "源值": legacy_value(row.get("源值", "")),
                "目标值": legacy_value(row.get("目标值", "")),
            }
            row["迁移标记"] = "保留原文"
            migrated += 1
            if looks_ambiguous(row.get("源值")) or looks_ambiguous(row.get("目标值")):
                row["歧义标记"] = True
                ambiguous += 1
        self.refresh_summary()
        return {"migrated": migrated, "skipped": skipped, "ambiguous": ambiguous}

    # ── 按原编号读取（消费方入口） ─────────────────────────────

    def get_report_by_code(self, code: str) -> dict[str, Any] | None:
        """按原报告编号反查（核对人/消费方沿用旧编号打开报告）。"""
        rows = store.find_by_field(self.MODULE, "记录编号", code)
        return rows[0] if rows else None

    def set_typed_read_path(self, enabled: bool) -> None:
        """切换或回滚类型读取路径；回滚后 read_report 只返回原文视图。"""
        store.set_pipeline_state(self.READ_PATH_KEY, {"typed": bool(enabled)})

    def typed_read_path_enabled(self) -> bool:
        state = store.get_pipeline_state(self.READ_PATH_KEY)
        return True if state is None else bool(state.get("typed"))

    def read_report(self, code: str) -> dict[str, Any] | None:
        """按编号打开报告：类型路径返回类型映射，回滚后返回原文视图。"""
        row = self.get_report_by_code(code)
        if row is None:
            return None
        if self.typed_read_path_enabled():
            return row
        return {
            "记录编号": row.get("记录编号"),
            "比较对象": row.get("比较对象"),
            "比较维度": row.get("比较维度"),
            "差异字段": row.get("差异字段"),
            "源值": row.get("源值"),
            "目标值": row.get("目标值"),
            "差异等级": row.get("差异等级"),
            "status": row.get("status"),
        }

    def replay_report(self, code: str) -> dict[str, Any] | None:
        """回放差异记录的类型映射。

        只依赖落库的类型映射与原文，已确认/已忽略的记录同样可回放，
        与读取路径是否回滚无关。
        """
        row = self.get_report_by_code(code)
        if row is None:
            return None
        mapping = row.get("类型映射") or {
            "源值": legacy_value(row.get("源值", "")),
            "目标值": legacy_value(row.get("目标值", "")),
        }
        return {
            "记录编号": row.get("记录编号"),
            "差异字段": row.get("差异字段"),
            "status": row.get("status"),
            "回放值": {
                "源值": dict(mapping["源值"]),
                "目标值": dict(mapping["目标值"]),
            },
            "确认结论": row.get("确认结论"),
        }

    # ── 确认与汇总 ────────────────────────────────────────────

    def confirm_diff(self, report_id: int) -> dict[str, Any] | None:
        """确认差异（消费方确认入口）：逐条确认，不合并其他记录。"""
        entry, _ = self.run_action(report_id, "确认差异")
        if entry is not None:
            entry["确认结论"] = "消费方已确认"
            entry["确认时间"] = _time.strftime("%Y-%m-%d %H:%M:%S")
            self.refresh_summary()
        return entry

    def confirm_diff_by_code(self, code: str) -> dict[str, Any] | None:
        """按原报告编号确认差异。"""
        row = self.get_report_by_code(code)
        if row is None:
            return None
        return self.confirm_diff(int(row["id"]))

    def refresh_summary(self) -> dict[str, Any]:
        """重算差异汇总并落库：确认/忽略/迁移/对账后调用。"""
        rows = store.rows(self.MODULE)
        by_status: dict[str, int] = {}
        by_type: dict[str, int] = {}
        for row in rows:
            status = row.get("status", "")
            by_status[status] = by_status.get(status, 0) + 1
            mapping = row.get("类型映射") or {}
            src_type = (mapping.get("源值") or {}).get("type", TYPE_LEGACY)
            tgt_type = (mapping.get("目标值") or {}).get("type", TYPE_LEGACY)
            key = f"{src_type}->{tgt_type}"
            by_type[key] = by_type.get(key, 0) + 1
        summary = {
            "total": len(rows),
            "by_status": by_status,
            "by_type": by_type,
            "confirmed": by_status.get("已确认", 0),
            "updated_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        store.set_pipeline_state(self.SUMMARY_KEY, summary)
        return summary

    def diff_summary(self) -> dict[str, Any]:
        """读取差异汇总（不存在则现算）。"""
        return store.get_pipeline_state(self.SUMMARY_KEY) or self.refresh_summary()

    # ── 同轮对账 ──────────────────────────────────────────────

    def reconcile_round(self, subject: str, dimension: str,
                        entries: list[dict[str, Any]], level: str = "修改") -> dict[str, Any]:
        """同一轮对账：逐条生成差异记录，文本零/数值零/缺列各自独立成行。

        entries 的每项形如 {"field": 字段名, "from": 值映射, "to": 值映射}；
        每条记录独立编号、独立状态，确认动作不会把它们合成一条。
        """
        state = store.get_pipeline_state(self.RECONCILE_KEY) or {"round": 0}
        state["round"] = int(state.get("round", 0)) + 1
        store.set_pipeline_state(self.RECONCILE_KEY, state)
        round_id = f"REC-{state['round']:04d}"

        reports = [
            self.generate_typed_report(
                subject, dimension, entry["field"],
                entry["from"], entry["to"], level=level, round_id=round_id,
            )
            for entry in entries
        ]
        self.refresh_summary()
        return {"round_id": round_id, "reports": reports, "count": len(reports)}

    # ── 旧接口保留 ────────────────────────────────────────────

    def get_reports_by_target(self, target: str) -> list[dict[str, Any]]:
        """获取指定对象的差异报告。"""
        return store.find_by_field(self.MODULE, "比较对象", target)
