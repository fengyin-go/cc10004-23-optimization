"""结果缓存引擎与策略链路的单元测试（仅标准库，python -m unittest 即可运行）。"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest

from app.services.cache_engine import (
    STATUS_ACTIVE,
    STATUS_BYPASS,
    STATUS_EVICTED,
    STATUS_EXPIRED,
    CacheVault,
    ResultCache,
    canonical_identity,
    derive_cache_key,
)


class FakeClock:
    """受控墙钟：测试可主动推进时间，验证绝对期限口径。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_identity(expression: str = "SELECT 1", *, params=None, version: str = "ds-v1", rule: str = "rule-a") -> str:
    return canonical_identity(
        expression=expression,
        parameters=params or {},
        dataset_version=version,
        key_rule=rule,
    )


class IdentityAndCollisionTests(unittest.TestCase):
    def test_parameter_order_does_not_change_identity(self) -> None:
        a = make_identity(params={"a": 1, "b": 2})
        b = make_identity(params={"b": 2, "a": 1})
        self.assertEqual(a, b)
        self.assertEqual(derive_cache_key(a), derive_cache_key(b))

    def test_dataset_version_change_breaks_identity(self) -> None:
        old = make_identity(version="ds-v1")
        new = make_identity(version="ds-v2")
        self.assertNotEqual(old, new)

    def test_key_collision_does_not_cross_results(self) -> None:
        clock = FakeClock()
        cache = ResultCache(vault=CacheVault("memory"), clock=clock)
        identity_a = make_identity(expression="SELECT A")
        identity_b = make_identity(expression="SELECT B")

        # 人为制造同键不同身份：直接写入一条留存模拟键碰撞
        collision_key = derive_cache_key(identity_a)
        cache._entries.append({
            "id": 1,
            "cache_key": collision_key,
            "identity": identity_b,  # 同键但身份不同
            "policy_id": 7,
            "status": STATUS_ACTIVE,
            "result": {"ok": True, "rows": ["B"]},
            "stored_at": clock.now,
            "stored_at_label": "t0",
            "expires_at": clock.now + 60,
            "expires_at_label": "t1",
            "hit_count": 0,
        })
        outcome = cache.get_or_fill(
            identity=identity_a, policy_id=7, ttl_seconds=60, loader=lambda: {"ok": True, "rows": ["A"]}
        )
        # 碰撞结果不得串用：未命中并填充 A 自己的结果
        self.assertFalse(outcome["hit"])
        self.assertEqual(outcome["result"]["rows"], ["A"])
        self.assertEqual(len(cache._entries), 2)
        self.assertTrue(all(entry["identity"] in (identity_a, identity_b) for entry in cache._entries))


class ExpiryTests(unittest.TestCase):
    def test_expired_result_is_not_revived_by_late_fill(self) -> None:
        clock = FakeClock()
        cache = ResultCache(vault=CacheVault("memory"), clock=clock)
        identity = make_identity()
        first = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=30, loader=lambda: {"ok": True, "v": 1}
        )
        self.assertFalse(first["hit"])
        original_expiry = first["entry"]["expires_at"]

        clock.advance(31)  # 到期
        peeked = cache.peek(cache_key=derive_cache_key(identity), identity=identity, policy_id=1)
        self.assertEqual(peeked["status"], STATUS_EXPIRED)

        # 迟到填充：模拟在到期前启动、到期后才返回的 loader
        def late_loader() -> dict:
            clock.advance(5)
            return {"ok": True, "v": 2}

        outcome = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=30, loader=late_loader
        )
        # 旧记录保持已过期且到期时点不变；新结果是一条全新留存，而不是复活旧记录
        self.assertEqual(peeked["expires_at"], original_expiry)
        self.assertEqual(peeked["status"], STATUS_EXPIRED)
        self.assertFalse(outcome["hit"])
        self.assertEqual(outcome["entry"]["result"]["v"], 2)
        self.assertNotEqual(outcome["entry"]["id"], peeked["id"])
        self.assertGreater(outcome["entry"]["expires_at"], original_expiry)

    def test_restart_keeps_original_expiry_and_does_not_extend_life(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "vault.json")
            clock = FakeClock()
            cache = ResultCache(vault=CacheVault(path), clock=clock)
            identity = make_identity()
            outcome = cache.get_or_fill(
                identity=identity, policy_id=1, ttl_seconds=30, loader=lambda: {"ok": True}
            )
            expiry_before = outcome["entry"]["expires_at"]
            self.assertTrue(os.path.exists(path))

            clock.advance(10)  # 重启发生在有效期内
            reloaded = ResultCache(vault=CacheVault(path), clock=clock)
            entry = reloaded.peek(
                cache_key=derive_cache_key(identity), identity=identity, policy_id=1
            )
            self.assertIsNotNone(entry)
            self.assertEqual(entry["expires_at"], expiry_before)  # 到期时点原样保留
            self.assertEqual(entry["status"], STATUS_ACTIVE)

            clock.advance(21)  # 跨过原始到期时点
            expired = reloaded.peek(
                cache_key=derive_cache_key(identity), identity=identity, policy_id=1
            )
            self.assertEqual(expired["status"], STATUS_EXPIRED)

            # 落盘快照里存的也是绝对时间点
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
            self.assertEqual(raw["entries"][0]["expires_at"], expiry_before)


class ConcurrencyTests(unittest.TestCase):
    def test_concurrent_fillers_accept_only_one_result(self) -> None:
        clock = FakeClock()
        cache = ResultCache(vault=CacheVault("memory"), clock=clock)
        identity = make_identity()
        started = threading.Event()
        release = threading.Event()
        calls = {"count": 0}
        lock = threading.Lock()

        def loader() -> dict:
            with lock:
                calls["count"] += 1
            started.set()
            release.wait(timeout=5)
            return {"ok": True, "worker": threading.get_ident()}

        outcomes: list[dict] = []
        threads = []

        def worker() -> None:
            outcomes.append(
                cache.get_or_fill(identity=identity, policy_id=1, ttl_seconds=60, loader=loader)
            )

        for _ in range(5):
            thread = threading.Thread(target=worker)
            threads.append(thread)
            thread.start()

        self.assertTrue(started.wait(timeout=5))
        release.set()
        for thread in threads:
            thread.join(timeout=10)

        self.assertEqual(calls["count"], 1)  # 只有一个填充真正执行
        self.assertEqual(len(outcomes), 5)
        self.assertEqual(sum(1 for item in outcomes if item["hit"]), 4)
        self.assertEqual(sum(1 for item in outcomes if not item["hit"]), 1)
        self.assertEqual(len(cache._entries), 1)  # 只接纳一条留存
        self.assertEqual(cache._entries[0]["hit_count"], 4)

    def test_failed_fill_bypasses_cache_but_query_succeeds_upstream(self) -> None:
        clock = FakeClock()
        cache = ResultCache(vault=CacheVault("memory"), clock=clock)
        identity = make_identity()

        def failing_loader() -> dict:
            raise RuntimeError("boom")

        outcome = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=60, loader=failing_loader
        )
        self.assertTrue(outcome["bypassed"])
        self.assertEqual(outcome["status"], STATUS_BYPASS)
        self.assertIn("绕过缓存", outcome["reason"])
        self.assertIsNone(outcome["entry"])
        self.assertEqual(len(cache._entries), 0)  # 失败不写入
        self.assertNotIn(derive_cache_key(identity), cache._inflight)  # 占位被清理

        # 失败后下一次请求仍可正常填充
        ok = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=60, loader=lambda: {"ok": True}
        )
        self.assertFalse(ok["bypassed"])
        self.assertEqual(len(cache._entries), 1)

    def test_wait_timeout_clears_inflight_and_later_fill_succeeds(self) -> None:
        clock = FakeClock()
        cache = ResultCache(vault=CacheVault("memory"), clock=clock)
        identity = make_identity()
        release = threading.Event()

        def slow_loader() -> dict:
            release.wait(timeout=5)
            return {"ok": True, "v": "late"}

        holder_started = threading.Event()

        def holder() -> None:
            holder_started.set()
            cache.get_or_fill(identity=identity, policy_id=1, ttl_seconds=60, loader=slow_loader)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(holder_started.wait(timeout=5))

        # 等待者只给 0.1 秒：超时后应绕过，并把悬挂占位清掉
        outcome = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=60,
            loader=lambda: {"ok": True}, wait_timeout=0.1,
        )
        self.assertTrue(outcome["bypassed"])
        self.assertNotIn(derive_cache_key(identity), cache._inflight)

        # 放行原填充者；它迟到返回时占位已不在，结果被拒绝接纳（不复活、不串写）
        release.set()
        thread.join(timeout=5)

        # 后续请求可以正常发起全新填充
        fresh = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=60, loader=lambda: {"ok": True, "v": "fresh"}
        )
        self.assertFalse(fresh["bypassed"])
        self.assertEqual(fresh["result"]["v"], "fresh")


class ManagementTests(unittest.TestCase):
    def test_eviction_and_stats(self) -> None:
        clock = FakeClock()
        cache = ResultCache(vault=CacheVault("memory"), clock=clock)
        identity = make_identity()
        cache.get_or_fill(identity=identity, policy_id=1, ttl_seconds=60, loader=lambda: {"ok": True})
        evicted = cache.evict_entry(1)
        self.assertEqual(evicted["status"], STATUS_EVICTED)
        self.assertIn("淘汰", cache.describe_validity(evicted))

        # 淘汰后同身份请求重新填充，不会复用旧记录
        again = cache.get_or_fill(
            identity=identity, policy_id=1, ttl_seconds=60, loader=lambda: {"ok": True, "v": 2}
        )
        self.assertFalse(again["hit"])
        stats = cache.stats()
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["evicted"], 1)
        self.assertEqual(stats["active"], 1)


if __name__ == "__main__":
    unittest.main()
