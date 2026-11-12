# 数据洞察分析工作台

面向多源数据接入、交互式查询分析、可视化看板搭建、分析报告生成与数据服务编排的数据洞察分析工作台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 交互查询 | `query` | 查询语句 | 语句编号、查询表达式、目标数据集 |
| 可视化图表 | `chart` | 图表配置 | 图表编号、图表名称、图表类型 |
| 数据源接入 | `source` | 接入配置 | 配置编号、连接类型、接入地址 |
| 聚合计算 | `aggregate` | 聚合方案 | 方案编号、聚合函数、目标字段 |
| 数据筛选 | `filter` | 筛选规则 | 规则编号、目标数据集、筛选字段 |
| 报告生成 | `reportgen` | 报告模板 | 模板编号、模板名称、包含图表 |
| 异常检测 | `anomaly` | 检测规则 | 规则编号、监控指标、基线均值 |
| 多维下钻 | `drill` | 下钻路径 | 路径编号、起点维度、下钻层数 |
| 对比分析 | `compare` | 对比方案 | 方案编号、基准数据集、对照数据集 |
| 分析流水线 | `pipeline` | 流水线节点 | 节点编号、节点类型、输入源 |
| 数据集管理 | `datastore` | 数据集定义 | 数据集编号、数据集名称、来源表名 |
| 趋势预测 | `forecast` | 预测模型 | 模型编号、模型类型、训练数据集 |
| 数据分片 | `segment` | 分片规则 | 规则编号、目标数据集、分片字段 |
| 调度管理 | `scheduler` | 调度配置 | 配置编号、关联任务、调度表达式 |
| 通知分发 | `notify` | 通知规则 | 规则编号、触发条件、通知渠道 |
| 查询缓存 | `cache` | 缓存策略 | 策略编号、目标查询、缓存键规则 |
| 数据标签 | `tag` | 标签定义 | 标签编号、标签名称、计算表达式 |
| 数据分享 | `share` | 分享链接 | 链接编号、目标数据集、访问密码 |

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 查询缓存：带身份与期限的结果留存

查询缓存不是简单的键值表，而是一套“键候选 + 完整身份核对 + 绝对期限 + 单飞填充”的机制，
实现集中在 `backend/app/services/cache_engine.py`，策略侧规则在 `services/cache.py`，
查询侧链路在 `services/query.py`。

- **完整身份**：每次交互查询跨模块携带四要素——查询表达式、参数、数据集版本
  （取自“数据集管理”目标行的稳定摘要，数据集一变版本即变）、缓存键规则；
  规范化后用 SHA-256 派生缓存键。键只用于候选定位，命中前必须逐字节核对完整身份，
  键碰撞不会串用结果。
- **绝对期限**：每条留存保存固定的到期时间点（`expires_at`）而不是剩余秒数；
  快照经 JSON 原子落盘，重启原样加载，**重启不延寿**。过期结果不会被迟到填充复活，
  新填充只会产生一条全新留存。
- **单飞填充**：同一缓存键的并发请求只有一个真正执行填充，其余等待并复用同一结果；
  填充失败明确返回“绕过缓存”，但查询本身仍直接执行成功，失败结果不写入。
- **策略口径**：停用即时生效（服务层每次读取实时判断，停用后立即阻止新复用）；
  缺少有效“过期秒数”的策略保持“待校验/待确认”，不能启用，**缺期限不等于永不过期**。
- **页面展示**：缓存页对每条结果给出“为何仍有效/为何失效”的解释与原始到期时点，
  支持按状态筛选、查看完整身份与手动淘汰。

相关接口：

- `POST /api/query/{id}/execute`，请求体 `{ "parameters": {...}, "fail_fill": false }`，
  返回体的 `entry.cache` 含命中标志、缓存键、数据集版本、有效性说明与留存明细。
- `GET /api/cache/results`、`GET /api/cache/results/{id}`、
  `GET /api/cache/results/stats`、`POST /api/cache/results/{id}/evict`。

缓存快照默认写到 `backend/data/cache_vault.json`，可用环境变量
`CACHE_VAULT_PATH` 覆盖；设为 `memory` 时只驻留内存（测试即用此模式）。

### 后端测试

```bash
cd backend
CACHE_VAULT_PATH=memory .venv/bin/python -m unittest discover -s tests -v
```

覆盖身份规范化与键碰撞、过期不复活、重启不延寿、并发单飞、填充失败绕过、
停用即时阻止复用、缺期限待确认、数据集版本变化导致身份失效等口径。

领域：数据工程
子方向：数据分析与数据应用
