"""服务层与路由链路测试：策略停用、缺期限待确认、查询执行与缓存命中。"""
from __future__ import annotations

import os
import unittest

os.environ.setdefault("CACHE_VAULT_PATH", "memory")

from app.routers import cache as router_cache  # noqa: E402
from app.routers.query import ExecutePayload, execute_query  # noqa: E402
from app.services.cache import CacheService, parse_ttl  # noqa: E402
from app.services.query import QueryService  # noqa: E402
from app.store import store  # noqa: E402


class TtlParsingTests(unittest.TestCase):
    def test_missing_or_invalid_ttl_is_none_never_infinite(self) -> None:
        self.assertIsNone(parse_ttl(None))
        self.assertIsNone(parse_ttl(""))
        self.assertIsNone(parse_ttl("查询缓存样例1"))
        self.assertIsNone(parse_ttl(0))
        self.assertIsNone(parse_ttl(-5))
        self.assertEqual(parse_ttl("60"), 60)
        self.assertEqual(parse_ttl(60.0), 60)


class PolicyActionTests(unittest.TestCase):
    def setUp(self) -> None:
        # 独立策略行，绑定只属于它的查询表达式，避免与种子策略互相干扰
        self.service = CacheService()
        entry, missing = self.service.create_entry({
            "策略编号": "CACH-UT-1",
            "目标查询": "交互查询样例1",
            "缓存键规则": "sha256(表达式+参数+数据集版本)",
            "过期秒数": "",
        })
        self.assertFalse(missing)
        self.policy_id = entry["id"]

    def test_policy_without_ttl_cannot_be_enabled(self) -> None:
        entry, message = self.service.run_action(self.policy_id, "提交校验")
        self.assertEqual(entry["status"], "待校验")
        entry, message = self.service.run_action(self.policy_id, "启用缓存")
        self.assertIn("期限待确认", message)
        self.assertEqual(entry["status"], "待校验")  # 停在待确认，不变成已启用
        self.assertFalse(entry["ttl_confirmed"])

    def test_disabled_policy_blocks_reuse_immediately(self) -> None:
        # 补上期限后启用（查询语句 id=1 的表达式为“交互查询样例1”，仅本策略匹配）
        policy = store.find("cache", self.policy_id)
        policy["过期秒数"] = 60
        entry, _ = self.service.run_action(self.policy_id, "启用缓存")
        self.assertEqual(entry["status"], "已启用")

        query = QueryService()
        statement, message, detail = query.execute(1)
        self.assertTrue(statement is not None)
        self.assertTrue(detail["cache_used"], msg=detail)

        # 停用后：同一查询立刻不能再复用（换新参数也找不到可用策略）
        self.service.run_action(self.policy_id, "停用缓存")
        statement, message, detail = query.execute(1, parameters={"round": 2})
        self.assertFalse(detail["cache_used"])
        self.assertTrue(detail["bypassed"])
        self.assertIn("没有匹配的已启用", detail["reason"])


class RouterChainTests(unittest.TestCase):
    """直接调用路由函数验证完整链路（环境未装 httpx，不走 TestClient）。"""

    def test_execute_then_hit_and_results_page(self) -> None:
        # 种子策略 CACH-0003 已启用且 TTL=60，目标查询为交互查询样例3（id=3）
        first = execute_query(3, ExecutePayload(parameters={"city": "北京"}))
        cache_detail = first.entry["cache"]
        self.assertTrue(first.ok)
        self.assertFalse(cache_detail["hit"])
        self.assertFalse(cache_detail["bypassed"])
        self.assertIsNotNone(cache_detail["cache_key"])
        self.assertIsNotNone(cache_detail["dataset_version"])

        second = execute_query(3, ExecutePayload(parameters={"city": "北京"}))
        hit_detail = second.entry["cache"]
        self.assertTrue(hit_detail["hit"])
        # 命中不改变原始到期时点
        self.assertEqual(hit_detail["entry"]["expires_at"], cache_detail["entry"]["expires_at"])

        # 参数不同 => 身份不同 => 不串用
        other = execute_query(3, ExecutePayload(parameters={"city": "上海"}))
        self.assertFalse(other.entry["cache"]["hit"])

        # 缓存页能看到留存、有效性解释和原始到期时点
        page = router_cache.list_results(status=None, keyword=None, page=1, size=20)
        self.assertGreaterEqual(page.total, 2)
        row = next(item for item in page.items if item["id"] == cache_detail["entry"]["id"])
        self.assertEqual(row["status"], "有效")
        self.assertIn("完整身份", row["validity_reason"])
        self.assertIn("expires_at_label", row)

        stats = router_cache.result_stats()
        self.assertGreaterEqual(stats["active"], 2)

    def test_fill_failure_bypasses_but_query_still_succeeds(self) -> None:
        resp = execute_query(3, ExecutePayload(fail_fill=True, parameters={"k": "fail"}))
        detail = resp.entry["cache"]
        self.assertTrue(detail["bypassed"])
        self.assertIn("绕过缓存", detail["reason"])
        self.assertTrue(detail["result"]["ok"])  # 查询本身成功，结果保留

    def test_evict_endpoint(self) -> None:
        made = execute_query(3, ExecutePayload(parameters={"k": "evict"}))
        result_id = made.entry["cache"]["entry"]["id"]
        evicted = router_cache.evict_result(result_id)
        self.assertTrue(evicted.ok)
        self.assertEqual(evicted.entry["status"], "已淘汰")

    def test_dataset_version_change_causes_identity_mismatch(self) -> None:
        # 为查询语句 2（表达式“交互查询样例2”，数据集“数据集管理样例2”）启用专属策略
        service = CacheService()
        entry, missing = service.create_entry({
            "策略编号": "CACH-UT-DS",
            "目标查询": "交互查询样例2",
            "缓存键规则": "sha256(表达式+参数+数据集版本)",
            "过期秒数": 120,
        })
        self.assertFalse(missing)
        service.run_action(entry["id"], "启用缓存")

        query = QueryService()
        _, _, first = query.execute(2, parameters={"p": 1})
        self.assertFalse(first["hit"])
        version_before = first["dataset_version"]

        # 数据集管理模块的目标行发生变化 => 数据集版本变化
        for row in store.rows("datastore"):
            if row.get("数据集名称") == "数据集管理样例2":
                row["更新策略"] = "每日全量-已调整"
                break

        _, _, second = query.execute(2, parameters={"p": 1})
        self.assertNotEqual(second["dataset_version"], version_before)
        self.assertFalse(second["hit"], msg="数据集版本变化后身份不同，不得复用旧结果")
        self.assertNotEqual(second["cache_key"], first["cache_key"])


if __name__ == "__main__":
    unittest.main()
