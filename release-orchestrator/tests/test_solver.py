import json
from pathlib import Path

from app.models import ScheduleRequest
from app.service import handle_request

EXAMPLES = Path(__file__).resolve().parent.parent / "app" / "static" / "examples"


def run(payload: dict):
    return handle_request(ScheduleRequest.model_validate(payload))


def load(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


# ----------------------------------------------------------- 可行场景

def test_feasible_example():
    status, body = run(load("feasible.json"))
    assert status == 200
    assert body["status"] == "feasible"
    sched = {r["service"]: r for r in body["schedule"]}
    assert set(sched) == {"auth-service", "billing-service", "notify-service"}
    # 依赖先后：auth(10-07) < billing(10-08) < notify(10-08)
    assert sched["auth-service"]["start_utc"] < sched["billing-service"]["start_utc"]
    assert sched["billing-service"]["rollback_until_utc"] <= sched["notify-service"]["start_utc"]
    # 回滚缓冲 = 窗口结束 + rollback_minutes
    assert sched["auth-service"]["rollback_until_utc"] == "2026-10-07T06:30:00Z"
    # 每个未选中的候选窗口都有解释
    rejected = body["rejected_windows"]
    assert rejected and all(r["reasons"] for r in rejected)
    # billing 的上海窗口A 应因依赖间隔不足被排除
    bwa = next(r for r in rejected
               if r["service"] == "billing-service" and r["window"]["window"] == "上海窗口A")
    assert any("依赖" in reason for reason in bwa["reasons"])
    # 约束余量齐全
    assert len(body["constraint_margins"]) == 3  # 2 依赖 + 1 资源共享对（core-db）


def test_blackout_rejection_reason():
    payload = {
        "services": [{
            "name": "svc", "team": "t1", "rollback_minutes": 0,
            "windows": [
                {"label": "被禁发覆盖", "start": "2026-10-07T01:00:00Z", "end": "2026-10-07T02:00:00Z"},
                {"label": "可用", "start": "2026-10-07T05:00:00Z", "end": "2026-10-07T06:00:00Z"},
            ],
        }],
        "blackouts": [{"team": "t1", "label": "夜间禁发",
                       "start": "2026-10-07T00:00:00Z", "end": "2026-10-07T03:00:00Z"}],
    }
    status, body = run(payload)
    assert body["status"] == "feasible"
    assert body["schedule"][0]["window"] == "可用"
    reason = body["rejected_windows"][0]["reasons"][0]
    assert "夜间禁发" in reason and "团队禁发" in reason


def test_freeze_blocks_occupied_interval_including_rollback():
    payload = {
        "services": [{
            "name": "svc", "team": "t1", "rollback_minutes": 60,
            "windows": [{"label": "w", "start": "2026-10-07T01:00:00Z", "end": "2026-10-07T02:00:00Z"}],
        }],
        # 冻结从 02:30 开始：窗口本身不重叠，但占用区间(含回滚)到 03:00 → 被排除
        "freezes": [{"label": "冻结", "start": "2026-10-07T02:30:00Z", "end": "2026-10-07T05:00:00Z"}],
    }
    status, body = run(payload)
    assert body["status"] == "infeasible"
    blk = body["blocking_constraints"][0]
    assert blk["type"] == "unary"
    assert "冻结" in blk["details"][0]["reasons"][0]


# ----------------------------------------------------------- 不可行场景

def test_infeasible_example_reports_dependency_blocker():
    status, body = run(load("infeasible.json"))
    assert status == 200
    assert body["status"] == "infeasible"
    assert "schedule" not in body  # 不返回部分结果
    blk = body["blocking_constraints"][0]
    assert blk["type"] == "dependency"
    assert "api" in blk["services"] and "worker" in blk["services"]
    assert "缺少 30 分钟" in blk["details"]


def test_unary_blocker_lists_blackout():
    payload = {
        "services": [{
            "name": "svc", "team": "t1",
            "windows": [{"label": "w1", "start": "2026-10-07T01:00:00Z", "end": "2026-10-07T02:00:00Z"}],
        }],
        "blackouts": [{"team": "t1", "label": "全天禁发",
                       "start": "2026-10-07T00:00:00Z", "end": "2026-10-08T00:00:00Z"}],
    }
    _, body = run(payload)
    assert body["status"] == "infeasible"
    blk = body["blocking_constraints"][0]
    assert blk["type"] == "unary"
    assert "全天禁发" in blk["constraints"]


def test_resource_pair_blocker():
    payload = {
        "services": [
            {"name": "a", "resources": ["db"], "windows": [
                {"label": "wa", "start": "2026-10-07T01:00:00Z", "end": "2026-10-07T03:00:00Z"}]},
            {"name": "b", "resources": ["db"], "windows": [
                {"label": "wb", "start": "2026-10-07T02:00:00Z", "end": "2026-10-07T04:00:00Z"}]},
        ],
    }
    _, body = run(payload)
    assert body["status"] == "infeasible"
    blk = body["blocking_constraints"][0]
    assert blk["type"] == "resource"
    assert "db" in blk["constraints"][0]


def test_minimal_unsat_core_three_services():
    # A[00:00,10:00]、B[11:00,12:00] 共享资源；C 的两个窗口分别只能避开 A 或 B，
    # 两两可满足但三者不可同时满足 → 触发极小约束核提取
    payload = {
        "services": [
            {"name": "A", "resources": ["r"], "windows": [
                {"label": "wa", "start": "2026-10-07T00:00:00Z", "end": "2026-10-07T10:00:00Z"}]},
            {"name": "B", "resources": ["r"], "windows": [
                {"label": "wb", "start": "2026-10-07T11:00:00Z", "end": "2026-10-07T12:00:00Z"}]},
            {"name": "C", "resources": ["r"], "windows": [
                {"label": "c1", "start": "2026-10-07T01:00:00Z", "end": "2026-10-07T02:00:00Z"},
                {"label": "c2", "start": "2026-10-07T10:30:00Z", "end": "2026-10-07T11:30:00Z"}]},
        ],
    }
    _, body = run(payload)
    assert body["status"] == "infeasible"
    blk = body["blocking_constraints"][0]
    assert blk["type"] == "constraint_set"
    # 核中应恰好是涉及 C 的两条资源约束（A-B 约束本就满足，不在核中）
    assert len(blk["constraints"]) == 2
    assert all("C" in c for c in blk["constraints"])
    assert set(blk["services"]) == {"A", "B", "C"}


def test_dependency_cycle():
    payload = {
        "services": [
            {"name": "a", "windows": [{"start": "2026-10-07T01:00:00Z", "end": "2026-10-07T02:00:00Z"}]},
            {"name": "b", "windows": [{"start": "2026-10-07T03:00:00Z", "end": "2026-10-07T04:00:00Z"}]},
        ],
        "dependencies": [
            {"before": "a", "after": "b"},
            {"before": "b", "after": "a"},
        ],
    }
    _, body = run(payload)
    assert body["status"] == "infeasible"
    assert body["blocking_constraints"][0]["type"] == "dependency_cycle"


def test_dependency_gap_respected_in_solution():
    payload = {
        "services": [
            {"name": "up", "rollback_minutes": 30, "windows": [
                {"start": "2026-10-07T00:00:00Z", "end": "2026-10-07T01:00:00Z"}]},
            {"name": "down", "windows": [
                {"label": "太早", "start": "2026-10-07T02:00:00Z", "end": "2026-10-07T03:00:00Z"},
                {"label": "够晚", "start": "2026-10-07T03:00:00Z", "end": "2026-10-07T04:00:00Z"}]},
        ],
        # up 占用至 01:30，+60 分钟间隔 → down 必须 ≥ 02:30
        "dependencies": [{"before": "up", "after": "down", "min_gap_minutes": 60}],
    }
    _, body = run(payload)
    assert body["status"] == "feasible"
    down = next(r for r in body["schedule"] if r["service"] == "down")
    assert down["window"] == "够晚"


# ----------------------------------------------------------- 输入校验

def test_dst_errors_collected_at_once():
    status, body = run(load("dst_error.json"))
    assert status == 422
    assert body["status"] == "invalid_time"
    problems = {e["problem"] for e in body["errors"]}
    assert problems == {"nonexistent", "ambiguous"}
    assert all(e["suggestions"] for e in body["errors"])


def test_unknown_service_in_dependency():
    payload = {
        "services": [{"name": "a", "windows": [
            {"start": "2026-10-07T01:00:00Z", "end": "2026-10-07T02:00:00Z"}]}],
        "dependencies": [{"before": "a", "after": "ghost"}],
    }
    status, body = run(payload)
    assert status == 400
    assert body["status"] == "invalid_request"


def test_window_end_before_start():
    payload = {"services": [{"name": "a", "windows": [
        {"start": "2026-10-07T03:00:00Z", "end": "2026-10-07T02:00:00Z"}]}]}
    status, body = run(payload)
    assert status == 400
