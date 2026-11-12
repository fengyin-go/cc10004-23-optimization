"""交互查询业务规则：状态流转、字段校验，以及带身份与期限的结果缓存链路。

缓存身份四要素（跨模块拼装）：
- 查询表达式：来自交互查询模块的“查询表达式”；
- 参数：执行时显式提交的 parameters（缺省为空参数集）；
- 数据集版本：取“数据集管理”模块中目标数据集行的内容摘要，
  数据集定义一变版本即变，旧结果因身份不符而不会被串用；
- 缓存键规则：来自命中的缓存策略，参与身份与键派生。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from app.services.cache import CacheService, parse_ttl
from app.services.cache_engine import (
    canonical_identity,
    derive_cache_key,
    result_cache,
)
from app.store import store

MODULE = "query"
REQUIRED_FIELDS = ["语句编号", "查询表达式", "目标数据集"]
STATUS_ORDER = ["草稿", "待执行", "执行中", "已完成", "已超时"]
ACTION_RULES = {"提交执行": "待执行", "启动查询": "执行中", "终止查询": "已超时", "保存结果": "已完成"}
NEGATIVE_ACTIONS = []

cache_service = CacheService()


class QueryService:
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
            rows = [row for row in rows if keyword in str(row.get("语句编号", ""))]
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
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"查询语句 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于交互查询可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"查询语句已{action}"

    # ------------------------------------------------------------------ 缓存执行
    @staticmethod
    def dataset_version(dataset_name: str) -> tuple[str, dict[str, Any] | None]:
        """跨模块计算数据集版本：对数据集管理模块的目标行做稳定摘要。"""
        matched: dict[str, Any] | None = None
        for row in store.rows("datastore"):
            if str(row.get("数据集名称", "")) == dataset_name or str(row.get("来源表名", "")) == dataset_name:
                matched = row
                break
        if matched is None:
            # 找不到已登记数据集时给出确定但可区分的版本，避免与真实数据集共用身份
            digest = hashlib.sha256(f"unregistered::{dataset_name}".encode("utf-8")).hexdigest()[:16]
            return f"unregistered-{digest}", None
        canonical = json.dumps(matched, ensure_ascii=False, sort_keys=True, default=str)
        return "ds-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16], matched

    def build_identity(
        self,
        query_entry: dict[str, Any],
        parameters: dict[str, Any] | None,
        key_rule: str,
    ) -> tuple[str, str, str]:
        """返回（缓存键, 完整身份串, 数据集版本）。"""
        expression = str(query_entry.get("查询表达式", ""))
        dataset = str(query_entry.get("目标数据集", ""))
        version, _ = self.dataset_version(dataset)
        identity = canonical_identity(
            expression=expression,
            parameters=parameters or {},
            dataset_version=version,
            key_rule=key_rule,
        )
        return derive_cache_key(identity), identity, version

    def execute(
        self,
        entry_id: int,
        *,
        parameters: dict[str, Any] | None = None,
        fail_fill: bool = False,
    ) -> tuple[dict[str, Any] | None, str, dict[str, Any]]:
        """执行交互查询并走缓存链路。

        返回 (查询语句, 说明, 执行详情)。无论缓存填充成功与否，查询成功结果都保留；
        填充失败只让本次结果绕过缓存。
        """
        query_entry = store.find(MODULE, entry_id)
        if query_entry is None:
            return None, f"查询语句 {entry_id} 不存在或已归档", {}

        parameters = parameters or {}
        policy = cache_service.find_enabled_policy_for_query(query_entry)

        # 无可用策略：查询照常成功，但明确绕过缓存
        if policy is None:
            result = self._run_query(query_entry, parameters, fail_fill=fail_fill)
            detail = {
                "cache_used": False,
                "hit": False,
                "bypassed": True,
                "reason": "没有匹配的已启用且期限有效的缓存策略，本次绕过缓存",
                "identity": None,
                "cache_key": None,
                "dataset_version": self.dataset_version(str(query_entry.get("目标数据集", "")))[0],
                "result": result,
            }
            query_entry["status"] = "已完成" if result.get("ok") else "已超时"
            query_entry["pending"] = False
            return query_entry, "查询已完成（未启用缓存）", detail

        key_rule = str(policy.get("缓存键规则", ""))
        ttl = parse_ttl(policy.get("过期秒数"))
        cache_key, identity, version = self.build_identity(query_entry, parameters, key_rule)

        def loader() -> dict[str, Any]:
            return self._run_query(query_entry, parameters, fail_fill=fail_fill)

        outcome = result_cache.get_or_fill(
            identity=identity,
            policy_id=int(policy["id"]),
            ttl_seconds=int(ttl),  # type: ignore[arg-type]
            loader=loader,
            source=f"交互查询/{query_entry.get('语句编号', entry_id)}",
        )

        detail = {
            "cache_used": not outcome["bypassed"],
            "hit": outcome["hit"],
            "bypassed": outcome["bypassed"],
            "status": outcome["status"],
            "reason": outcome["reason"],
            "identity": identity,
            "cache_key": cache_key,
            "dataset_version": version,
            "policy": {
                "id": policy["id"],
                "策略编号": policy.get("策略编号"),
                "过期秒数": policy.get("过期秒数"),
                "status": policy.get("status"),
            },
            "entry": outcome.get("entry"),
            "result": outcome.get("result"),
        }

        # 绕过缓存（含填充失败）时查询仍要成功：直接执行一次，结果保留但不写入缓存。
        # fail_fill 只作用于缓存填充阶段；直接执行不受影响，从而体现“失败绕过但查询成功”。
        if outcome["bypassed"] and outcome.get("result") is None:
            fallback = self._run_query(query_entry, parameters, fail_fill=False)
            detail["result"] = fallback
            detail["reason"] += "；查询已直接执行成功，结果未写入缓存"

        query_result = detail.get("result") or {}
        query_entry["status"] = "已完成" if query_result.get("ok") else "已超时"
        query_entry["pending"] = False
        message = "缓存命中，复用历史结果" if outcome["hit"] else (
            "查询已完成，结果已写入缓存" if detail["cache_used"] else "查询已完成，本次绕过缓存"
        )
        return query_entry, message, detail

    @staticmethod
    def _run_query(
        query_entry: dict[str, Any],
        parameters: dict[str, Any],
        *,
        fail_fill: bool = False,
    ) -> dict[str, Any]:
        """模拟实际查询执行。fail_fill=True 模拟填充阶段抛错（查询链路仍返回结构化结果）。"""
        if fail_fill:
            raise RuntimeError("模拟缓存填充阶段执行失败")
        return {
            "ok": True,
            "语句编号": query_entry.get("语句编号"),
            "查询表达式": query_entry.get("查询表达式"),
            "目标数据集": query_entry.get("目标数据集"),
            "parameters": parameters,
            "rows": [
                {"sample": 1, "source": query_entry.get("目标数据集")},
                {"sample": 2, "source": query_entry.get("目标数据集")},
            ],
            "row_count": 2,
        }
