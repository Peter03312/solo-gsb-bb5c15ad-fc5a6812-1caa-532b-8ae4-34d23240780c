"""本地时间解析与夏令时（DST）校验。

规则：
- 带显式 UTC 偏移（如 2026-10-06T02:30:00-04:00 或结尾 Z）的时间直接使用；
- 不带偏移的本地时间必须同时提供 IANA 时区名（如 America/New_York）；
- 若本地时间在 DST 切换中“不存在”（春季拨快被跳过）或“重复出现”
  （秋季回拨出现两次），拒绝该输入，返回问题说明与可选修正建议，
  由调用方显式修正后重新提交 —— 系统不做隐式猜测。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass
class ParsedTime:
    utc: datetime          # 归一化到 UTC 的感知时间
    display: str           # 原始时区/偏移下的 ISO 表示（用于回显）
    timezone: str | None   # IANA 时区名；固定偏移输入时为 None


@dataclass
class TimeIssue:
    field: str
    value: str
    timezone: str | None
    problem: str           # ambiguous / nonexistent / missing_timezone / unknown_timezone / bad_format
    message: str
    suggestions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "field": self.field,
            "value": self.value,
            "timezone": self.timezone,
            "problem": self.problem,
            "message": self.message,
            "suggestions": self.suggestions,
        }


def _offset_str(dt: datetime) -> str:
    off = dt.utcoffset()
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


def parse_instant(raw: str, tzname: str | None, field_path: str):
    """解析单个时间点。成功返回 (ParsedTime, None)，失败返回 (None, TimeIssue)。"""
    try:
        dt = datetime.fromisoformat(str(raw).strip())
    except ValueError:
        return None, TimeIssue(
            field=field_path, value=str(raw), timezone=tzname, problem="bad_format",
            message=f"无法解析时间 '{raw}'，请使用 ISO 8601 格式，如 2026-10-06T02:30:00。",
        )

    # 显式偏移（含 Z）：无 DST 歧义，直接使用
    if dt.tzinfo is not None:
        utc = dt.astimezone(timezone.utc)
        return ParsedTime(utc=utc, display=dt.isoformat(), timezone=None), None

    if not tzname:
        return None, TimeIssue(
            field=field_path, value=raw, timezone=None, problem="missing_timezone",
            message=f"本地时间 '{raw}' 未携带 UTC 偏移，必须提供 timezone（IANA 时区名），或改为显式偏移。",
            suggestions=[
                f"{raw}+08:00  （显式偏移示例）",
                "或补充 \"timezone\": \"Asia/Shanghai\" 等 IANA 时区名",
            ],
        )

    try:
        tz = ZoneInfo(tzname)
    except ZoneInfoNotFoundError:
        return None, TimeIssue(
            field=field_path, value=raw, timezone=tzname, problem="unknown_timezone",
            message=f"未知时区 '{tzname}'，请使用 IANA 时区名，如 Asia/Shanghai、America/New_York、Europe/Berlin。",
        )

    aware0 = dt.replace(tzinfo=tz, fold=0)
    aware1 = dt.replace(tzinfo=tz, fold=1)

    if aware0.utcoffset() != aware1.utcoffset():
        # 两个 fold 偏移不同：可能是“重复”（回拨）也可能是“不存在”（拨快），
        # 用 round-trip 区分 —— 能映射回同一墙钟时间的是重复，否则是不存在。
        back = aware0.astimezone(timezone.utc).astimezone(tz)
        if back.replace(tzinfo=None) == dt:
            return None, TimeIssue(
                field=field_path, value=raw, timezone=tzname, problem="ambiguous",
                message=(
                    f"本地时间 '{raw}' 在时区 {tzname} 的夏令时回拨中重复出现两次"
                    f"（{aware0.tzname()} UTC{_offset_str(aware0)} 与 {aware1.tzname()} UTC{_offset_str(aware1)}），"
                    "无法确定指哪一次，请显式指定 UTC 偏移后重新提交。"
                ),
                suggestions=[
                    f"{raw}{_offset_str(aware0)}  （{aware0.tzname()}，夏令时，第一次出现）",
                    f"{raw}{_offset_str(aware1)}  （{aware1.tzname()}，标准时，第二次出现）",
                ],
            )
        shifted = back.replace(tzinfo=None)
        return None, TimeIssue(
            field=field_path, value=raw, timezone=tzname, problem="nonexistent",
            message=(
                f"本地时间 '{raw}' 在时区 {tzname} 的夏令时拨快中被跳过，该时刻不存在，请修正后重新提交。"
            ),
            suggestions=[
                f"{shifted.isoformat()}（{tzname}，拨快后的有效时间）",
                f"{raw}{_offset_str(aware0)}  （按切换前偏移 {aware0.tzname()} 解释）",
                f"{raw}{_offset_str(aware1)}  （按切换后偏移 {aware1.tzname()} 解释）",
            ],
        )

    utc = aware0.astimezone(timezone.utc)
    return ParsedTime(utc=utc, display=aware0.isoformat(), timezone=tzname), None
