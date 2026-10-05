"""API 请求模型（Pydantic v2）。

时间字段保留为字符串，由 service 层统一解析并收集所有 DST 问题，
避免 Pydantic 在第一个错误处中断，便于一次性向用户报告全部待修正时间。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WindowIn(BaseModel):
    start: str
    end: str
    timezone: str | None = None
    label: str | None = None


class ServiceIn(BaseModel):
    name: str = Field(min_length=1)
    team: str | None = None
    resources: list[str] = Field(default_factory=list)
    rollback_minutes: int = Field(default=0, ge=0, le=7 * 24 * 60)
    windows: list[WindowIn] = Field(min_length=1)


class DependencyIn(BaseModel):
    before: str = Field(min_length=1)
    after: str = Field(min_length=1)
    min_gap_minutes: int = Field(default=0, ge=0, le=30 * 24 * 60)
    note: str | None = None


class BlackoutIn(BaseModel):
    team: str = Field(min_length=1)
    start: str
    end: str
    timezone: str | None = None
    label: str | None = None


class FreezeIn(BaseModel):
    start: str
    end: str
    timezone: str | None = None
    label: str | None = None


class ScheduleRequest(BaseModel):
    services: list[ServiceIn] = Field(min_length=1)
    dependencies: list[DependencyIn] = Field(default_factory=list)
    blackouts: list[BlackoutIn] = Field(default_factory=list)
    freezes: list[FreezeIn] = Field(default_factory=list)
    resource_spacing_minutes: int = Field(default=0, ge=0, le=24 * 60)
    max_solutions: int = Field(default=1, ge=1, le=20)
