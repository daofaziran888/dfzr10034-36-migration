"""差异采集流水线类型化迁移测试：旧文本记录迁移到类型映射后的行为。

覆盖需求：
- 存量迁移保留报告编号与原文，不凭字符外观猜类型，歧义报告保留原始字段和值
- 变更捕获消费带类型的映射；文本零、数值零、缺列在同一轮对账中各自独立成行
- 消费方用原编号反查仍看到原文，确认动作不把记录合成一条
- 差异汇总随确认结果更新
- 读取路径回滚后，已确认的差异仍能按落库类型映射回放
"""
import app.store as store_mod
from app.diff_types import (
    TYPE_INT,
    TYPE_LEGACY,
    TYPE_MISSING,
    TYPE_TEXT,
    legacy_value,
    typed_value,
)
from app.services.change_capture import ChangeCaptureService
from app.services.diff_report import DiffReportService


def _fresh():
    """重置存储并返回服务实例。"""
    store_mod.store.reset()
    return DiffReportService(), ChangeCaptureService()


def _seed_legacy(code, source, target, field="订单号"):
    """模拟旧版纯文本差异记录（无类型映射）。"""
    store = store_mod.store
    row = {
        "id": store.next_id("diff_report"),
        "记录编号": code,
        "比较对象": "订单表",
        "比较维度": "字段核对",
        "差异字段": field,
        "源值": source,
        "目标值": target,
        "差异等级": "修改",
        "报告时间": "2026-09-01 00:00:00",
        "status": "已生成",
        "pending": False,
        "abnormal": False,
        "created_at": "2026-09-01 00:00:00",
    }
    store.rows("diff_report").append(row)
    return row


def _round_rows(round_id):
    return [r for r in store_mod.store.rows("diff_report") if r.get("对账轮次") == round_id]


# ══════════════════════════════════════════════════════════════════
# 存量迁移：保留编号与原文，不猜类型
# ══════════════════════════════════════════════════════════════════

class TestLegacyMigration:

    def test_migration_preserves_report_code_and_original_text(self):
        diff_svc, _ = _fresh()
        _seed_legacy("D07", "0007", "0008")
        result = diff_svc.migrate_legacy_reports()
        assert result["migrated"] >= 1
        report = diff_svc.get_report_by_code("D07")
        assert report is not None
        assert report["记录编号"] == "D07"
        assert report["源值"] == "0007"  # 四位原文可取回
        assert report["类型映射"]["源值"]["type"] == TYPE_LEGACY
        assert report["类型映射"]["源值"]["text"] == "0007"

    def test_migration_does_not_guess_types_from_appearance(self):
        diff_svc, _ = _fresh()
        _seed_legacy("D-A1", "7", "8")
        _seed_legacy("D-A2", "0", "0.0")
        _seed_legacy("D-A3", "", "7.0")
        diff_svc.migrate_legacy_reports()
        for code, original in [("D-A1", "7"), ("D-A2", "0"), ("D-A3", "")]:
            row = diff_svc.get_report_by_code(code)
            # 数字外观/空串一律不猜成数值，保留原始字段和值
            assert row["类型映射"]["源值"]["type"] == TYPE_LEGACY
            assert row["源值"] == original
            assert row["歧义标记"] is True

    def test_migration_is_idempotent(self):
        diff_svc, _ = _fresh()
        _seed_legacy("D-IDEM", "0007", "0007")
        first = diff_svc.migrate_legacy_reports()
        second = diff_svc.migrate_legacy_reports()
        assert first["migrated"] >= 1
        assert second["migrated"] == 0
        assert second["skipped"] >= first["migrated"]
        assert diff_svc.get_report_by_code("D-IDEM")["源值"] == "0007"


# ══════════════════════════════════════════════════════════════════
# 变更捕获消费带类型的映射
# ══════════════════════════════════════════════════════════════════

class TestChangeCaptureTyped:

    def test_capture_typed_change_generates_typed_diffs(self):
        _, change_svc = _fresh()
        result = change_svc.capture_typed_change(
            "T_TYPED_ASSET", "字段核对",
            before_map={"订单号": 7, "备注": "旧"},
            after_map={"订单号": "7"},
        )
        assert result["change"]["status"] == "已捕获"
        assert result["version_snapshot"]["created"] is True
        assert len(result["diff_reports"]) == 2
        by_field = {r["差异字段"]: r for r in result["diff_reports"]}
        # 整数 7 与文本 "7" 落库后依然可分
        assert by_field["订单号"]["类型映射"]["源值"]["type"] == TYPE_INT
        assert by_field["订单号"]["类型映射"]["目标值"]["type"] == TYPE_TEXT
        # 缺列一侧标记 missing，差异等级为删除
        assert by_field["备注"]["类型映射"]["目标值"]["type"] == TYPE_MISSING
        assert by_field["备注"]["差异等级"] == "删除"
        # 变更记录本身也落库类型映射；缺列字段不出现在该侧映射里
        change = result["change"]
        assert change["变更前值类型映射"]["订单号"]["type"] == TYPE_INT
        assert "备注" not in change["变更后值类型映射"]


# ══════════════════════════════════════════════════════════════════
# 同轮对账：文本零/数值零/缺列各自独立，确认不合并，汇总随确认更新
# ══════════════════════════════════════════════════════════════════

class TestTypedReconcile:

    def test_int_text_missing_stay_distinct_in_one_round(self):
        diff_svc, _ = _fresh()
        result = diff_svc.reconcile_round("订单表", "字段核对", [
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(7)},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value("7")},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(None, missing=True)},
        ])
        assert result["count"] == 3
        assert [r["类型映射"]["目标值"]["type"] for r in result["reports"]] == [
            TYPE_INT, TYPE_TEXT, TYPE_MISSING]
        # 缺列记录保留缺失性
        assert result["reports"][2]["类型映射"]["目标值"]["present"] is False
        # 同一轮对账
        assert {r["对账轮次"] for r in result["reports"]} == {result["round_id"]}

    def test_confirm_does_not_merge_records(self):
        diff_svc, _ = _fresh()
        result = diff_svc.reconcile_round("订单表", "字段核对", [
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(7)},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value("7")},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(None, missing=True)},
        ])
        confirmed = diff_svc.confirm_diff_by_code(result["reports"][0]["记录编号"])
        assert confirmed["status"] == "已确认"
        # 三笔仍然各自独立成行，确认动作不合成一条
        rows = _round_rows(result["round_id"])
        assert len(rows) == 3
        assert sorted(r["status"] for r in rows) == ["已生成", "已生成", "已确认"]
        assert sorted(r["类型映射"]["目标值"]["type"] for r in rows) == sorted(
            [TYPE_INT, TYPE_TEXT, TYPE_MISSING])

    def test_summary_follows_confirmation(self):
        diff_svc, _ = _fresh()
        result = diff_svc.reconcile_round("订单表", "字段核对", [
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(7)},
        ])
        before = diff_svc.diff_summary()
        diff_svc.confirm_diff_by_code(result["reports"][0]["记录编号"])
        after = diff_svc.diff_summary()
        assert after["confirmed"] == before["confirmed"] + 1
        assert after["by_status"]["已确认"] == before["by_status"].get("已确认", 0) + 1
        assert after["by_type"]["legacy->int"] == 1


# ══════════════════════════════════════════════════════════════════
# 读取路径回滚与回放
# ══════════════════════════════════════════════════════════════════

class TestReadPathRollbackAndReplay:

    def test_replay_works_for_confirmed_after_rollback(self):
        diff_svc, _ = _fresh()
        result = diff_svc.reconcile_round("订单表", "字段核对", [
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(7)},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value("7")},
        ])
        code = result["reports"][0]["记录编号"]
        diff_svc.confirm_diff_by_code(code)

        # 回滚读取路径：只回原文视图
        diff_svc.set_typed_read_path(False)
        legacy_view = diff_svc.read_report(code)
        assert "类型映射" not in legacy_view
        assert legacy_view["目标值"] == "7"

        # 已确认的差异仍能回放落库类型
        replayed = diff_svc.replay_report(code)
        assert replayed["status"] == "已确认"
        assert replayed["回放值"]["目标值"]["type"] == TYPE_INT
        assert replayed["回放值"]["源值"]["type"] == TYPE_LEGACY
        assert replayed["回放值"]["源值"]["text"] == "0007"
        assert replayed["确认结论"] == "消费方已确认"

        # 恢复类型读取路径
        diff_svc.set_typed_read_path(True)
        assert "类型映射" in diff_svc.read_report(code)

    def test_confirmed_legacy_report_replays_original_text(self):
        diff_svc, _ = _fresh()
        _seed_legacy("D07", "0007", "0008")
        diff_svc.migrate_legacy_reports()
        diff_svc.confirm_diff_by_code("D07")
        diff_svc.set_typed_read_path(False)
        replayed = diff_svc.replay_report("D07")
        assert replayed["status"] == "已确认"
        assert replayed["回放值"]["源值"]["type"] == TYPE_LEGACY
        assert replayed["回放值"]["源值"]["text"] == "0007"


# ══════════════════════════════════════════════════════════════════
# D07 场景：旧编号反查 + 新三笔 + 零值同轮对账 + 回放比对
# ══════════════════════════════════════════════════════════════════

class TestD07Scenario:

    def test_d07_lookup_and_three_new_records(self):
        diff_svc, _ = _fresh()
        # 旧差异单 D07 保存字符串 0007
        _seed_legacy("D07", "0007", "0008")
        diff_svc.migrate_legacy_reports()

        # 新采集同时送来：整数 7、文本 7、缺列
        result = diff_svc.reconcile_round("订单表", "字段核对", [
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(7)},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value("7")},
            {"field": "订单号", "from": legacy_value("0007"), "to": typed_value(None, missing=True)},
        ])

        # 消费方用 D07 反查仍看到四位原文
        assert diff_svc.get_report_by_code("D07")["源值"] == "0007"
        # 新三笔保留各自类型和缺失性
        assert [r["类型映射"]["目标值"]["type"] for r in result["reports"]] == [
            TYPE_INT, TYPE_TEXT, TYPE_MISSING]

        # 确认其中一笔，不在确认动作中自动合成一条
        diff_svc.confirm_diff_by_code(result["reports"][0]["记录编号"])
        assert len(_round_rows(result["round_id"])) == 3

    def test_zero_variants_one_round_then_open_historical_report(self):
        diff_svc, _ = _fresh()
        _seed_legacy("D07", "0007", "0008")
        diff_svc.migrate_legacy_reports()

        # 文本零、数值零、空字段送入同一轮对账
        result = diff_svc.reconcile_round("账户表", "余额核对", [
            {"field": "余额", "from": legacy_value("0"), "to": typed_value("0")},
            {"field": "余额", "from": legacy_value("0"), "to": typed_value(0)},
            {"field": "余额", "from": legacy_value("0"), "to": typed_value(None, missing=True)},
        ])
        reports = result["reports"]
        # 文本零与数值零原文相同，类型不同
        assert reports[0]["目标值"] == reports[1]["目标值"] == "0"
        assert [r["类型映射"]["目标值"]["type"] for r in reports] == [
            TYPE_TEXT, TYPE_INT, TYPE_MISSING]

        # 确认数值零那一笔，差异汇总跟着确认结果更新
        before = diff_svc.diff_summary()
        diff_svc.confirm_diff_by_code(reports[1]["记录编号"])
        after = diff_svc.diff_summary()
        assert after["confirmed"] == before["confirmed"] + 1

        # 打开旧编号的历史报告：原文仍在
        assert diff_svc.read_report("D07")["源值"] == "0007"

        # 比较确认结果和回放得到的类型
        replayed = diff_svc.replay_report(reports[1]["记录编号"])
        assert replayed["status"] == "已确认"
        assert replayed["回放值"]["目标值"]["type"] == TYPE_INT
        assert replayed["回放值"]["目标值"]["text"] == "0"
        # 文本零回放仍是文本，不与数值零混淆
        assert diff_svc.replay_report(reports[0]["记录编号"])["回放值"]["目标值"]["type"] == TYPE_TEXT
        # 历史报告回放仍是 legacy 原文
        d07_replay = diff_svc.replay_report("D07")
        assert d07_replay["回放值"]["源值"]["type"] == TYPE_LEGACY
        assert d07_replay["回放值"]["源值"]["text"] == "0007"
