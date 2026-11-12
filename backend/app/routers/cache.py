"""查询缓存接口：维护缓存策略与结果留存，覆盖启用/停用、留存查看、淘汰等动作。

路由顺序约定：所有静态路径（/results、/export 等）必须声明在动态路径 /{entry_id}
之前，否则“results”会被当成策略 id 解析导致 422/404。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.cache import CacheService

router = APIRouter(prefix="/api/cache", tags=["查询缓存"])

service = CacheService()

LIST_FIELDS = ["策略编号", "目标查询", "缓存键规则", "过期秒数", "刷新触发", "命中率阈值", "最大条目数", "淘汰算法"]
STATUSES = ["草稿", "待校验", "已启用", "已停用"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按策略编号检索"),
    status: str | None = Query(default=None, description="草稿、待校验、已启用、已停用"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按策略编号与状态过滤查询缓存列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条缓存策略，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="缓存策略已登记", entry=entry)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出查询缓存清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "cache", "total": total, "items": items}


@router.get("/results/stats")
def result_stats() -> dict[str, Any]:
    """结果留存看板：有效/已过期/已淘汰数量与累计命中次数。"""
    return service.result_stats()


@router.get("/results", response_model=PageResult[dict])
def list_results(
    status: str | None = Query(default=None, description="有效、已过期、已淘汰"),
    keyword: str | None = Query(default=None, description="按缓存键、来源或身份片段检索"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分页查看缓存结果留存；每条带有效性解释与原始到期时点。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_results(status=status, keyword=keyword, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/results/{result_id}", response_model=dict)
def get_result(result_id: int) -> dict[str, Any]:
    """读取单条缓存结果留存，含“为何仍有效/为何失效”的解释。"""
    entry = service.get_result(result_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存结果 {result_id} 不存在")
    return entry


@router.post("/results/{result_id}/evict", response_model=ActionResult)
def evict_result(result_id: int) -> ActionResult:
    """手动淘汰一条结果留存；原始到期时点保留可追溯，淘汰后不再复用。"""
    entry, message = service.evict_result(result_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# 动态路径放在所有静态路径之后
@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条缓存策略明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"缓存策略 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条缓存策略执行提交校验、启用缓存、停用缓存；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
