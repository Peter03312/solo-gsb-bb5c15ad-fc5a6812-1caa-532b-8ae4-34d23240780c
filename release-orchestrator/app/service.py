"""请求编排：解析校验 → 构建领域对象 → 求解 → 组装响应。

handle_request 返回 (http_status, payload)，供 HTTP 层与测试直接调用。
"""

from __future__ import annotations

from datetime import timedelta
from itertools import combinations

from . import solver
from .models import ScheduleRequest
from .timeutil import parse_instant


def handle_request(req: ScheduleRequest) -> tuple[int, dict]:
    structural: list[str] = []
    time_issues = []

    # ---- 结构校验
    names = [s.name for s in req.services]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        structural.append(f"服务名重复：{dupes}")
    for d in req.dependencies:
        for n in (d.before, d.after):
            if n not in names:
                structural.append(f"依赖 {d.before}→{d.after} 引用了未知服务 '{n}'")

    # ---- 解析禁发 / 冻结时段
    intervals: list[solver.NamedInterval] = []
    for i, b in enumerate(req.blackouts):
        ps, e1 = parse_instant(b.start, b.timezone, f"blackouts[{i}].start")
        pe, e2 = parse_instant(b.end, b.timezone, f"blackouts[{i}].end")
        time_issues += [e for e in (e1, e2) if e]
        if ps and pe:
            if pe.utc <= ps.utc:
                structural.append(f"blackouts[{i}]（{b.label or b.team}）结束时间必须晚于开始时间")
            else:
                intervals.append(solver.NamedInterval(
                    ps.utc, pe.utc, "blackout", b.label or f"{b.team} 禁发", team=b.team))
    for i, f in enumerate(req.freezes):
        ps, e1 = parse_instant(f.start, f.timezone, f"freezes[{i}].start")
        pe, e2 = parse_instant(f.end, f.timezone, f"freezes[{i}].end")
        time_issues += [e for e in (e1, e2) if e]
        if ps and pe:
            if pe.utc <= ps.utc:
                structural.append(f"freezes[{i}]（{f.label or '维护冻结'}）结束时间必须晚于开始时间")
            else:
                intervals.append(solver.NamedInterval(
                    ps.utc, pe.utc, "freeze", f.label or f"维护冻结#{i + 1}"))

    # ---- 解析服务窗口，构建候选
    nodes: list[solver.ServiceNode] = []
    for si, s in enumerate(req.services):
        cands: list[solver.Candidate] = []
        for wi, w in enumerate(s.windows):
            field = f"services[{si}]({s.name}).windows[{wi}]"
            ps, e1 = parse_instant(w.start, w.timezone, field + ".start")
            pe, e2 = parse_instant(w.end, w.timezone, field + ".end")
            time_issues += [e for e in (e1, e2) if e]
            if not (ps and pe):
                continue
            if pe.utc <= ps.utc:
                structural.append(f"{field}（{w.label or f'窗口{wi + 1}'}）结束时间必须晚于开始时间")
                continue
            rollback = timedelta(minutes=s.rollback_minutes)
            cands.append(solver.Candidate(
                service=s.name, index=wi,
                start=ps.utc, end=pe.utc, occupied_end=pe.utc + rollback,
                label=w.label or f"窗口{wi + 1}",
                display=ps.display, end_display=pe.display,
                tzname=ps.timezone or w.timezone,
            ))
        nodes.append(solver.ServiceNode(
            name=s.name, team=s.team, resources=s.resources,
            rollback_minutes=s.rollback_minutes, candidates=cands))

    # ---- 时间/结构问题优先返回，要求修正后重新提交
    if time_issues:
        payload = {
            "status": "invalid_time",
            "message": "存在需要修正的时间输入（夏令时重复/不存在或缺少时区），系统不做隐式猜测，请按建议修正后重新提交。",
            "errors": [t.to_dict() for t in time_issues],
        }
        if structural:
            payload["structural_errors"] = structural
        return 422, payload
    if structural:
        return 400, {"status": "invalid_request", "errors": structural}

    # ---- 一元约束：禁发 / 冻结
    solver.apply_unary_constraints(nodes, intervals)

    # ---- 关系约束
    deps = [solver.Dependency(d.before, d.after, timedelta(minutes=d.min_gap_minutes), d.note)
            for d in req.dependencies]
    spacing = timedelta(minutes=req.resource_spacing_minutes)
    pairs: list[solver.ResourcePair] = []
    for s1, s2 in combinations(nodes, 2):
        for r in sorted(set(s1.resources) & set(s2.resources)):
            pairs.append(solver.ResourcePair(s1.name, s2.name, r, spacing))
    constraints: list[solver.RelConstraint] = []
    for i, d in enumerate(deps):
        constraints.append(solver.RelConstraint(
            f"dep#{i}", "dependency", d.description, (d.before, d.after), dep=d))
    for i, p in enumerate(pairs):
        constraints.append(solver.RelConstraint(
            f"res#{i}", "resource", p.description, (p.s1, p.s2), pair=p))

    stats = {
        "services": len(nodes),
        "candidate_windows": sum(len(s.candidates) for s in nodes),
        "windows_after_blackout_freeze": sum(len(s.feasible_candidates) for s in nodes),
        "dependencies": len(deps),
        "resource_pairs": len(pairs),
    }

    # ---- 依赖环：直接作为阻塞约束组合报告
    cycle = solver.find_dependency_cycle(deps)
    if cycle:
        return 200, {
            "status": "infeasible",
            "message": "依赖关系构成环，不存在满足全部约束的排程（不返回部分结果）。",
            "blocking_constraints": [{
                "type": "dependency_cycle",
                "summary": "依赖环：" + " → ".join(cycle),
                "services": sorted(set(cycle)),
                "constraints": [d.description for d in deps
                                if d.before in cycle and d.after in cycle],
                "details": "依赖先后关系不可成环，请解除环上至少一条依赖。",
            }],
            "stats": stats,
        }

    # ---- 求解
    solutions, exceeded = solver.find_solutions(nodes, constraints, req.max_solutions)
    if exceeded and not solutions:
        return 200, {
            "status": "unknown",
            "message": "搜索空间超出预算，未能判定。请减少候选窗口数量或拆分请求后重试。",
            "stats": stats,
        }

    if solutions:
        schedules = []
        for sol in solutions:
            rows = []
            for svc in nodes:
                c = sol[svc.name]
                rows.append({
                    "service": svc.name,
                    "team": svc.team,
                    "window": c.label,
                    "start_utc": solver.fmt(c.start),
                    "end_utc": solver.fmt(c.end),
                    "rollback_until_utc": solver.fmt(c.occupied_end),
                    "start_local": c.display,
                    "end_local": c.end_display,
                    "timezone": c.tzname,
                })
            rows.sort(key=lambda r: r["start_utc"])
            schedules.append(rows)
        primary = solutions[0]
        return 200, {
            "status": "feasible",
            "message": f"找到满足全部约束的可行排程（共 {len(solutions)} 个，按窗口提交顺序取优）。",
            "schedule": schedules[0],
            "schedules": schedules,
            "constraint_margins": solver.constraint_margins(constraints, primary),
            "rejected_windows": solver.relational_rejections(nodes, constraints, primary),
            "stats": stats,
        }

    # ---- 不可行：返回阻塞约束组合，不给部分结果
    return 200, {
        "status": "infeasible",
        "message": "不存在满足全部约束的排程；以下为阻塞它的约束组合（不返回部分结果）。",
        "blocking_constraints": solver.explain_infeasible(nodes, deps, pairs, constraints),
        "rejected_windows": [
            {"service": s.name, "window": c.brief(), "reasons": c.unary_reasons}
            for s in nodes for c in s.candidates if c.unary_reasons
        ],
        "stats": stats,
    }
