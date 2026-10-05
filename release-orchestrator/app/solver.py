"""发布窗口约束求解与冲突解释。

约束语义（所有区间均为 UTC，闭开区间 [start, end)）：
- 占用区间：服务窗口 [start, end) 加上回滚缓冲，即 [start, end + rollback)；
  依赖间隔、资源间隔、禁发/冻结检查均基于占用区间，保证回滚时间内不占资源、不越约束。
- 依赖 (before → after, gap)：after.start >= before.occupied_end + gap。
- 资源占用：共享同一资源的服务，占用区间两两不得重叠，且间隔 >= resource_spacing。
- 团队禁发 / 维护冻结：占用区间不得与对应区间相交。

不可行时不返回部分结果，而是给出阻塞约束组合：
1. 某服务全部候选窗口被禁发/冻结排除（一元约束）；
2. 某条依赖或某对资源共享在任意窗口组合下都无法满足（二元约束，附“最接近的缺口”）；
3. 以上都通过但整体仍不可行时，用删除法求极小不可行约束核（MUS）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone


def fmt(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _minutes(delta: timedelta) -> float:
    return delta.total_seconds() / 60.0


# ---------------------------------------------------------------- 领域对象

@dataclass
class Candidate:
    service: str
    index: int
    start: datetime          # UTC
    end: datetime            # UTC
    occupied_end: datetime   # end + 回滚缓冲
    label: str
    display: str             # 原始本地表示（开始）
    end_display: str         # 原始本地表示（结束）
    tzname: str | None
    unary_reasons: list[str] = field(default_factory=list)

    @property
    def feasible(self) -> bool:
        return not self.unary_reasons

    def brief(self) -> dict:
        return {
            "window": self.label,
            "start_utc": fmt(self.start),
            "end_utc": fmt(self.end),
            "occupied_until_utc": fmt(self.occupied_end),
            "start_local": self.display,
            "end_local": self.end_display,
            "timezone": self.tzname,
        }


@dataclass
class ServiceNode:
    name: str
    team: str | None
    resources: list[str]
    rollback_minutes: int
    candidates: list[Candidate]

    @property
    def feasible_candidates(self) -> list[Candidate]:
        return [c for c in self.candidates if c.feasible]


@dataclass
class Dependency:
    before: str
    after: str
    gap: timedelta
    note: str | None = None

    @property
    def description(self) -> str:
        base = (f"依赖：{self.before} → {self.after}"
                f"（占用结束后最小间隔 {int(_minutes(self.gap))} 分钟）")
        return base + (f"，{self.note}" if self.note else "")


@dataclass
class ResourcePair:
    s1: str
    s2: str
    resource: str
    spacing: timedelta

    @property
    def description(self) -> str:
        return (f"资源占用：{self.s1} 与 {self.s2} 共享资源「{self.resource}」，"
                f"占用区间不得重叠且间隔 ≥{int(_minutes(self.spacing))} 分钟")


@dataclass
class NamedInterval:
    start: datetime
    end: datetime
    kind: str                # "blackout" | "freeze"
    label: str
    team: str | None = None


@dataclass
class RelConstraint:
    """关系约束的统一包装，用于求解时按子集启用/禁用（极小核提取）。"""
    cid: str
    kind: str                # "dependency" | "resource"
    description: str
    services: tuple[str, str]
    dep: Dependency | None = None
    pair: ResourcePair | None = None


# ---------------------------------------------------------------- 一元约束

def apply_unary_constraints(services: list[ServiceNode], intervals: list[NamedInterval]) -> None:
    """禁发/冻结过滤，为每个被拒绝的候选窗口记录原因。"""
    for svc in services:
        for c in svc.candidates:
            for iv in intervals:
                if iv.kind == "blackout" and iv.team != svc.team:
                    continue
                if c.start < iv.end and iv.start < c.occupied_end:
                    kind_zh = "团队禁发时段" if iv.kind == "blackout" else "维护冻结"
                    c.unary_reasons.append(
                        f"与{kind_zh}「{iv.label}」重叠：占用区间 {fmt(c.start)}–{fmt(c.occupied_end)} "
                        f"与 {fmt(iv.start)}–{fmt(iv.end)} 相交"
                    )


# ---------------------------------------------------------------- 关系约束判定

def _dep_ok(dep: Dependency, before_c: Candidate, after_c: Candidate) -> bool:
    return after_c.start >= before_c.occupied_end + dep.gap


def _res_ok(pair: ResourcePair, c1: Candidate, c2: Candidate) -> bool:
    return (c1.occupied_end + pair.spacing <= c2.start
            or c2.occupied_end + pair.spacing <= c1.start)


def _pair_ok(constraints: list[RelConstraint], n1: str, c1: Candidate, n2: str, c2: Candidate) -> bool:
    for rc in constraints:
        if rc.kind == "dependency":
            d = rc.dep
            if d.before == n1 and d.after == n2 and not _dep_ok(d, c1, c2):
                return False
            if d.before == n2 and d.after == n1 and not _dep_ok(d, c2, c1):
                return False
        else:
            p = rc.pair
            if {p.s1, p.s2} == {n1, n2} and not _res_ok(p, c1, c2):
                return False
    return True


# ---------------------------------------------------------------- 搜索

def find_solutions(services: list[ServiceNode], constraints: list[RelConstraint],
                   max_solutions: int = 1, budget: int = 200_000):
    """回溯搜索。返回 (solutions, budget_exceeded)。

    solutions 中每个解为 {服务名: Candidate}。服务按可行窗口数升序展开，
    候选窗口按用户提交顺序尝试（即用户窗口顺序即偏好顺序）。
    """
    by_name = {s.name: s for s in services}
    order = sorted(services, key=lambda s: (len(s.feasible_candidates), s.name))
    if any(not s.feasible_candidates for s in order):
        return [], False

    cons_by_service: dict[str, list[RelConstraint]] = {s.name: [] for s in services}
    for rc in constraints:
        for n in rc.services:
            if n in cons_by_service:
                cons_by_service[n].append(rc)

    assignment: dict[str, Candidate] = {}
    solutions: list[dict[str, Candidate]] = []
    state = {"nodes": 0, "exceeded": False}

    def consistent(svc: ServiceNode, cand: Candidate) -> bool:
        for rc in cons_by_service[svc.name]:
            a, b = rc.services
            other_name = b if a == svc.name else a
            other = assignment.get(other_name)
            if other is None:
                continue
            if not _pair_ok([rc], svc.name, cand, other_name, other):
                return False
        return True

    def backtrack(i: int) -> None:
        if state["exceeded"] or len(solutions) >= max_solutions:
            return
        if i == len(order):
            solutions.append(dict(assignment))
            return
        svc = order[i]
        for cand in svc.feasible_candidates:
            state["nodes"] += 1
            if state["nodes"] > budget:
                state["exceeded"] = True
                return
            if consistent(svc, cand):
                assignment[svc.name] = cand
                backtrack(i + 1)
                del assignment[svc.name]
                if state["exceeded"] or len(solutions) >= max_solutions:
                    return

    backtrack(0)
    return solutions, state["exceeded"]


# ---------------------------------------------------------------- 不可行解释

def find_dependency_cycle(deps: list[Dependency]) -> list[str] | None:
    """检测依赖环，返回环上的服务名列表（首尾相接），无环返回 None。"""
    graph: dict[str, list[str]] = {}
    for d in deps:
        graph.setdefault(d.before, []).append(d.after)
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = 1
        stack.append(u)
        for v in graph.get(u, []):
            if color.get(v, 0) == 1:
                return stack[stack.index(v):] + [v]
            if color.get(v, 0) == 0:
                r = dfs(v)
                if r:
                    return r
        stack.pop()
        color[u] = 2
        return None

    for n in list(graph):
        if color.get(n, 0) == 0:
            r = dfs(n)
            if r:
                return r
    return None


def explain_infeasible(services: list[ServiceNode], deps: list[Dependency],
                       pairs: list[ResourcePair], constraints: list[RelConstraint]) -> list[dict]:
    """按“一元 → 二元 → 极小约束核”的顺序定位阻塞约束组合。"""
    by_name = {s.name: s for s in services}
    blockers: list[dict] = []

    # 1) 一元：某服务所有候选窗口均被禁发/冻结排除
    for svc in services:
        if svc.feasible_candidates:
            continue
        involved = sorted({r.split("「")[1].split("」")[0]
                           for c in svc.candidates for r in c.unary_reasons if "「" in r})
        blockers.append({
            "type": "unary",
            "summary": f"服务 {svc.name} 的全部 {len(svc.candidates)} 个候选窗口均被禁发/冻结时段排除",
            "services": [svc.name],
            "constraints": involved,
            "details": [{"window": c.brief(), "reasons": c.unary_reasons} for c in svc.candidates],
        })
    if blockers:
        return blockers

    # 2a) 二元：依赖在任意窗口组合下均不满足
    for d in deps:
        a, b = by_name[d.before], by_name[d.after]
        best = None  # (slack_minutes, wa, wb)
        ok = False
        for wa in a.feasible_candidates:
            for wb in b.feasible_candidates:
                slack = _minutes(wb.start - wa.occupied_end - d.gap)
                if slack >= 0:
                    ok = True
                    break
                if best is None or slack > best[0]:
                    best = (slack, wa, wb)
            if ok:
                break
        if not ok:
            slack, wa, wb = best
            blockers.append({
                "type": "dependency",
                "summary": f"{d.description} —— 两个服务的候选窗口没有任何组合能满足",
                "services": [d.before, d.after],
                "constraints": [d.description],
                "details": (
                    f"最接近的组合仍缺少 {-slack:.0f} 分钟：{d.before} 窗口「{wa.label}」占用至 "
                    f"{fmt(wa.occupied_end)}，{d.after} 窗口「{wb.label}」开始于 {fmt(wb.start)}。"
                ),
            })

    # 2b) 二元：共享资源的服务对没有任何互不重叠的窗口组合
    for p in pairs:
        s1, s2 = by_name[p.s1], by_name[p.s2]
        best = None  # (need_minutes, c1, c2)
        ok = False
        for c1 in s1.feasible_candidates:
            for c2 in s2.feasible_candidates:
                if _res_ok(p, c1, c2):
                    ok = True
                    break
                need = min(_minutes(c1.occupied_end + p.spacing - c2.start),
                           _minutes(c2.occupied_end + p.spacing - c1.start))
                if best is None or need < best[0]:
                    best = (need, c1, c2)
            if ok:
                break
        if not ok:
            need, c1, c2 = best
            blockers.append({
                "type": "resource",
                "summary": f"{p.description} —— 两个服务的候选窗口没有任何组合能满足",
                "services": [p.s1, p.s2],
                "constraints": [p.description],
                "details": (
                    f"最接近的组合仍需再错开 {need:.0f} 分钟：{p.s1} 窗口「{c1.label}」"
                    f"（{fmt(c1.start)}–{fmt(c1.occupied_end)}）与 {p.s2} 窗口「{c2.label}」"
                    f"（{fmt(c2.start)}–{fmt(c2.occupied_end)}）。"
                ),
            })
    if blockers:
        return blockers

    # 3) 极小不可行约束核（删除法）：核中每条约束都是不可行的必要条件
    core = list(constraints)
    for rc in list(core):
        trial = [x for x in core if x is not rc]
        sols, _ = find_solutions(services, trial, max_solutions=1)
        if not sols:
            core = trial
    involved_services = sorted({n for rc in core for n in rc.services})
    return [{
        "type": "constraint_set",
        "summary": "以下约束组合共同导致无可行排程（移除其中任意一条即可恢复可行）；"
                   "请放宽其中至少一条（增加候选窗口、缩短间隔/回滚缓冲、或调整禁发/冻结时段）",
        "services": involved_services,
        "constraints": [rc.description for rc in core],
        "details": "各服务经禁发/冻结过滤后的可行窗口数："
                   + "，".join(f"{s.name}={len(s.feasible_candidates)}" for s in services),
    }]


# ---------------------------------------------------------------- 可行解的解释

def relational_rejections(services: list[ServiceNode], constraints: list[RelConstraint],
                          chosen: dict[str, Candidate]) -> list[dict]:
    """对未选中的候选窗口，给出其相对已选方案不可行的关系约束原因。"""
    rejected = []
    for svc in services:
        for cand in svc.candidates:
            if chosen.get(svc.name) is cand:
                continue
            if cand.unary_reasons:
                reasons = list(cand.unary_reasons)
            else:
                reasons = []
                for rc in constraints:
                    a, b = rc.services
                    if svc.name not in (a, b):
                        continue
                    other_name = b if a == svc.name else a
                    other = chosen.get(other_name)
                    if other is None:
                        continue
                    if rc.kind == "dependency":
                        d = rc.dep
                        if d.before == svc.name and not _dep_ok(d, cand, other):
                            reasons.append(
                                f"依赖 {d.before}→{d.after}：{d.after} 已排窗口开始于 {fmt(other.start)}，"
                                f"本窗口占用至 {fmt(cand.occupied_end)}，无法满足 ≥{int(_minutes(d.gap))} 分钟间隔")
                        elif d.after == svc.name and not _dep_ok(d, other, cand):
                            reasons.append(
                                f"依赖 {d.before}→{d.after}：{d.before} 已排窗口占用至 {fmt(other.occupied_end)}，"
                                f"本窗口开始于 {fmt(cand.start)}，无法满足 ≥{int(_minutes(d.gap))} 分钟间隔")
                    else:
                        p = rc.pair
                        if svc.name in (p.s1, p.s2) and other_name in (p.s1, p.s2) and not _res_ok(p, cand, other):
                            reasons.append(
                                f"资源「{p.resource}」与 {other_name} 的已排窗口冲突：占用区间 "
                                f"{fmt(cand.start)}–{fmt(cand.occupied_end)} 与 "
                                f"{fmt(other.start)}–{fmt(other.occupied_end)} 间隔不足 "
                                f"{int(_minutes(p.spacing))} 分钟")
                if not reasons:
                    reasons.append("该窗口本身可行，但求解器按提交顺序优先选择了排在前面的可行窗口")
            rejected.append({"service": svc.name, "window": cand.brief(), "reasons": reasons})
    return rejected


def constraint_margins(constraints: list[RelConstraint], chosen: dict[str, Candidate]) -> list[dict]:
    """已选方案下每条关系约束的剩余间隔（分钟），0 表示刚好卡线。"""
    margins = []
    for rc in constraints:
        a, b = rc.services
        ca, cb = chosen.get(a), chosen.get(b)
        if ca is None or cb is None:
            continue
        if rc.kind == "dependency":
            d = rc.dep
            slack = _minutes(cb.start - ca.occupied_end - d.gap)
        else:
            p = rc.pair
            if ca.occupied_end <= cb.start:
                slack = _minutes(cb.start - ca.occupied_end - p.spacing)
            else:
                slack = _minutes(ca.start - cb.occupied_end - p.spacing)
        margins.append({
            "constraint": rc.description,
            "slack_minutes": round(slack, 1),
            "tight": slack == 0,
        })
    return margins
