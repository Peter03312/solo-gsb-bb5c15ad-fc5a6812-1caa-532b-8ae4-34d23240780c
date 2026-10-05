from app.timeutil import parse_instant


def test_ambiguous_fall_back():
    _, issue = parse_instant("2026-11-01T01:30:00", "America/New_York", "f.start")
    assert issue is not None
    assert issue.problem == "ambiguous"
    assert len(issue.suggestions) == 2
    assert "-04:00" in issue.suggestions[0] and "-05:00" in issue.suggestions[1]


def test_nonexistent_spring_forward():
    _, issue = parse_instant("2026-03-08T02:30:00", "America/New_York", "f.start")
    assert issue is not None
    assert issue.problem == "nonexistent"
    assert any("03:30" in s for s in issue.suggestions)


def test_normal_local_time_converts_to_utc():
    parsed, issue = parse_instant("2026-10-07T01:00:00", "America/New_York", "f.start")
    assert issue is None
    assert parsed.utc.isoformat() == "2026-10-07T05:00:00+00:00"
    assert parsed.timezone == "America/New_York"


def test_shanghai_no_dst():
    parsed, issue = parse_instant("2026-10-07T10:00:00", "Asia/Shanghai", "f.start")
    assert issue is None
    assert parsed.utc.isoformat() == "2026-10-07T02:00:00+00:00"


def test_explicit_offset_passthrough():
    parsed, issue = parse_instant("2026-10-07T01:00:00-04:00", None, "f.start")
    assert issue is None
    assert parsed.utc.isoformat() == "2026-10-07T05:00:00+00:00"


def test_z_suffix():
    parsed, issue = parse_instant("2026-10-07T05:00:00Z", None, "f.start")
    assert issue is None
    assert parsed.utc.isoformat() == "2026-10-07T05:00:00+00:00"


def test_explicit_offset_survives_dst_transition():
    # 带显式偏移的时间即使落在 DST 切换点也不应报错
    parsed, issue = parse_instant("2026-11-01T01:30:00-05:00", "America/New_York", "f.start")
    assert issue is None
    assert parsed.utc.isoformat() == "2026-11-01T06:30:00+00:00"


def test_missing_timezone():
    _, issue = parse_instant("2026-10-07T01:00:00", None, "f.start")
    assert issue.problem == "missing_timezone"


def test_unknown_timezone():
    _, issue = parse_instant("2026-10-07T01:00:00", "Mars/Olympus", "f.start")
    assert issue.problem == "unknown_timezone"


def test_bad_format():
    _, issue = parse_instant("next tuesday", "UTC", "f.start")
    assert issue.problem == "bad_format"
