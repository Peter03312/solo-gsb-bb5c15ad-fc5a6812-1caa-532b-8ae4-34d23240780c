# 跨时区发布窗口编排（Release Window Orchestrator）

输入**服务依赖、候选发布时间窗、团队禁发时段、维护冻结、回滚耗时**，输出满足
**依赖先后 / 间隔 / 资源占用**约束的可执行排程，并解释每个冲突。

- 夏令时（DST）**重复**或**不存在**的本地时间：明确拒绝并给出修正建议，要求显式修正后重新提交，系统不做隐式猜测；
- **无可行排程**时：返回阻塞它的约束组合（一元排除 → 二元冲突 → 极小不可行约束核），**不返回部分结果**；
- 技术栈：Python 3.11+ / FastAPI / 自研回溯约束求解器 / `zoneinfo`（IANA 时区库）。

## 快速启动

### 方式一：Docker Compose（推荐）

```bash
cd release-orchestrator
docker compose up --build
```

### 方式二：Docker

```bash
cd release-orchestrator
docker build -t release-orchestrator .
docker run --rm -p 8000:8000 release-orchestrator
```

### 方式三：本地运行

```bash
cd release-orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

启动后访问：

| 地址 | 说明 |
|---|---|
| http://localhost:8000/ | 操作页面（内置可行 / 不可行 / DST 错误三个示例，一键载入提交） |
| http://localhost:8000/docs | Swagger 交互式 API 文档 |
| `GET /api/health` | 健康检查 |
| `POST /api/schedule` | 排程求解 |

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## API 使用

```bash
curl -s -X POST http://localhost:8000/api/schedule \
  -H 'Content-Type: application/json' \
  --data @app/static/examples/feasible.json | jq
```

### 请求体

```jsonc
{
  "services": [{
    "name": "auth-service",            // 服务名（唯一）
    "team": "identity",                // 所属团队（用于匹配禁发时段）
    "resources": ["core-db"],          // 占用的资源（同名资源互斥）
    "rollback_minutes": 30,            // 回滚耗时：窗口结束后占用区间延长 30 分钟
    "windows": [{                      // 候选时间窗，提交顺序即偏好顺序
      "label": "纽约窗口A",
      "start": "2026-10-07T01:00:00",  // 本地时间 + timezone，或直接带偏移 "...-04:00" / "Z"
      "end":   "2026-10-07T02:00:00",
      "timezone": "America/New_York"   // IANA 时区名
    }]
  }],
  "dependencies": [{                   // before 必须先于 after 完成
    "before": "auth-service", "after": "billing-service",
    "min_gap_minutes": 60              // 自 before 占用结束（含回滚缓冲）起算的最小间隔
  }],
  "blackouts": [{                      // 团队禁发时段：该团队服务的占用区间不得与之相交
    "team": "payments", "label": "结算禁发",
    "start": "...", "end": "...", "timezone": "Asia/Shanghai"
  }],
  "freezes": [{                        // 全局维护冻结：所有服务的占用区间不得与之相交
    "label": "季度审计冻结", "start": "...", "end": "...", "timezone": "..."
  }],
  "resource_spacing_minutes": 15,      // 共享资源的服务之间的最小间隔
  "max_solutions": 1                   // 最多返回的可行解个数（1–20）
}
```

### 约束语义

- **占用区间** = 发布窗口 + 回滚缓冲，即 `[start, end + rollback)`；
  依赖间隔、资源间隔、禁发/冻结检查全部基于占用区间 —— 回滚时间内不释放资源、不越过任何约束；
- **依赖** `before → after`：`after.start ≥ before.占用结束 + min_gap`；
- **资源占用**：共享同一资源的服务两两占用区间不得重叠，且间隔 ≥ `resource_spacing_minutes`；
- **禁发 / 冻结**：占用区间不得与对应区间相交。

### 响应

**可行（200）**：`schedule`（按开始时间排序，含 UTC 与原始本地时间、回滚缓冲截止时刻）、
`constraint_margins`（每条约束的剩余分钟数，0 为刚好卡线）、
`rejected_windows`（**每个被排除候选窗口的具体原因**：禁发/冻结重叠、依赖间隔不足、资源冲突，或“可行但未被选中”）。

**不可行（200）**：`blocking_constraints` —— 阻塞约束组合，按三层定位：

1. `unary`：某服务全部候选窗口被禁发/冻结排除，逐窗口列出与哪个时段重叠；
2. `dependency` / `resource`：某条依赖或资源共享在**任意**窗口组合下都无法满足，
   附“最接近的组合仍缺少 N 分钟”的量化缺口；
3. `constraint_set`：以上都通过但整体仍不可行时，用删除法求出的**极小不可行约束核**
   （核中每条约束都是不可行的必要条件，移除任意一条即可恢复可行）。

依赖成环会以 `dependency_cycle` 单独报告。不可行时**不返回任何部分排程**。

**时间需修正（422）**：`status=invalid_time`，一次性列出全部问题时间：

```jsonc
{
  "status": "invalid_time",
  "errors": [{
    "field": "services[0](web).windows[0].start",
    "value": "2026-03-08T02:30:00",
    "timezone": "America/New_York",
    "problem": "nonexistent",          // 或 ambiguous / missing_timezone / unknown_timezone / bad_format
    "message": "本地时间 ... 在夏令时拨快中被跳过，该时刻不存在 ...",
    "suggestions": ["2026-03-08T03:30:00（America/New_York，拨快后的有效时间）",
                    "2026-03-08T02:30:00-05:00  （按切换前偏移 EST 解释）", "..."]
  }]
}
```

修正规则：带显式 UTC 偏移（`±HH:MM` 或 `Z`）的时间永远无歧义；
不带偏移的本地时间必须提供 `timezone`，且不得落在 DST 切换的缺口/重复段内。

## 项目结构

```
release-orchestrator/
├── app/
│   ├── main.py            # FastAPI 入口（API + 静态页面）
│   ├── models.py          # 请求模型（Pydantic）
│   ├── timeutil.py        # 本地时间解析与 DST 校验（zoneinfo）
│   ├── solver.py          # 回溯求解器 + 冲突解释（一元/二元/极小约束核）
│   ├── service.py         # 编排层：校验 → 求解 → 组装响应
│   └── static/
│       ├── index.html     # 操作页面（零依赖单页）
│       └── examples/      # feasible / infeasible / dst_error 三个示例
├── tests/                 # pytest：DST 分类、求解器、API 共 28 例
├── requirements.txt       # 运行依赖（含 tzdata，容器内保证时区库可用）
├── requirements-dev.txt   # 测试依赖
├── Dockerfile
└── docker-compose.yml
```
