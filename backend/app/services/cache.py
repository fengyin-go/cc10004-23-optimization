"""查询缓存业务规则：状态流转、字段校验、期限口径与结果留存联动。

策略侧口径：
- “过期秒数”是期限的唯一来源；缺失或非正数的策略不能启用，保持“待校验/待确认”，
  绝不能被解释成永不过期。
- 停用动作即时同步给结果缓存引擎，停用后任何新请求都不得再复用其结果。
- 策略变更不会重算已有结果的到期时点（重启/变更均不延寿）。
"""
from __future__ import annotations

from typing import Any

from app.services.cache_engine import result_cache
from app.store import store

MODULE = "cache"
REQUIRED_FIELDS = ["策略编号", "目标查询", "缓存键规则"]
OPTIONAL_FIELDS = ["过期秒数", "刷新触发", "命中率阈值", "最大条目数", "淘汰算法"]
STATUS_ORDER = ["草稿", "待校验", "已启用", "已停用"]
ACTION_RULES = {"提交校验": "待校验", "启用缓存": "已启用", "停用缓存": "已停用"}
NEGATIVE_ACTIONS = ["停用缓存"]


def parse_ttl(value: Any) -> int | None:
    """把“过期秒数”解析成正整数；无法解析时返回 None（表示期限缺失，待确认）。"""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        ttl = int(float(text))
    except (TypeError, ValueError):
        return None
    return ttl if ttl > 0 else None


class CacheService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("策略编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        for field in OPTIONAL_FIELDS:
            if field in values:
                entry[field] = values.get(field)
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        # 缺期限的新策略显式标注待确认，不允许当成无限期
        entry["ttl_confirmed"] = parse_ttl(entry.get("过期秒数")) is not None
        rows.append(entry)
        result_cache.sync_policy(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"缓存策略 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于查询缓存可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        ttl = parse_ttl(entry.get("过期秒数"))
        if action == "启用缓存":
            if ttl is None:
                # 缺少期限的旧策略保持待确认：停在待校验，绝不解释成永不过期
                entry["status"] = "待校验"
                entry["pending"] = True
                entry["abnormal"] = True
                entry["ttl_confirmed"] = False
                result_cache.sync_policy(entry)
                return entry, "策略缺少有效的过期秒数，期限待确认，不能启用（缺失期限不等于永不过期）"

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["ttl_confirmed"] = ttl is not None
        # 动作即时同步缓存引擎：停用后该策略的结果立刻不能再被新请求复用
        result_cache.sync_policy(entry)
        if action == "停用缓存":
            return entry, "缓存策略已停用，其历史结果即刻停止新复用（已留存结果保留可追溯）"
        return entry, f"缓存策略已{action}"

    def backfill_policy_flags(self) -> int:
        """启动对账：为历史策略补齐期限确认标记并同步快照。

        seed 里的旧记录不经过 create_entry，可能缺少 ttl_confirmed；
        这里统一按“过期秒数”重算，缺期限者显式标注为待确认（None/False），
        绝不让缺字段被误读成永不过期。返回补齐的条数。
        """
        changed = 0
        for policy in store.rows(MODULE):
            confirmed = parse_ttl(policy.get("过期秒数")) is not None
            if policy.get("ttl_confirmed") is None:
                policy["ttl_confirmed"] = confirmed
                changed += 1
            else:
                policy["ttl_confirmed"] = bool(policy.get("ttl_confirmed")) and confirmed
            result_cache.sync_policy(policy)
        return changed

    # ------------------------------------------------------------------ 结果留存
    def list_results(
        self,
        *,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        return result_cache.list_entries(status=status, keyword=keyword, page=page, size=size)

    def get_result(self, result_id: int) -> dict[str, Any] | None:
        return result_cache.get_entry(result_id)

    def evict_result(self, result_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = result_cache.evict_entry(result_id)
        if entry is None:
            return None, f"缓存结果 {result_id} 不存在"
        return entry, "缓存结果已淘汰，后续同身份请求将重新填充"

    def result_stats(self) -> dict[str, int]:
        return result_cache.stats()

    def find_enabled_policy_for_query(self, query_entry: dict[str, Any]) -> dict[str, Any] | None:
        """按“目标查询”匹配已启用且期限有效的策略；停用或缺期限的策略不参与复用。"""
        expression = str(query_entry.get("查询表达式", ""))
        statement_no = str(query_entry.get("语句编号", ""))
        for policy in store.rows(MODULE):
            if policy.get("status") != "已启用":
                continue
            target = str(policy.get("目标查询", ""))
            if target not in (expression, statement_no):
                continue
            if parse_ttl(policy.get("过期秒数")) is None:
                # 双保险：即便状态被写成已启用，缺期限也不允许复用
                continue
            return policy
        return None
