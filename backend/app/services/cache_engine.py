"""带身份与期限的查询结果缓存引擎。

设计要点（对应需求口径）：

1. 缓存键规则只产出候选键；命中后必须逐条核对完整身份
   （查询表达式 / 参数 / 数据集版本 / 缓存键规则），任一不一致即视为碰撞，不得串用。
2. 每条结果留存固定的到期时间点（绝对时间戳），展示原始到期时点；
   重启只重新加载快照、不重算 TTL，因此不会延寿。
3. 过期结果不会被任何迟到填充复活：迟到结果只能在占位仍有效时提交，
   提交时再次核验身份与期限，失败则丢弃并绕过缓存。
4. 同一键的并发填充只接纳一个有效结果（单飞 single-flight），其余请求等待首个结果；
   填充失败时清理占位并明确返回“绕过缓存”，但查询本身仍成功。
5. 策略停用的判定发生在服务层每次读取时（快照 + 实时状态），停用立即阻止新复用。

时间来源统一走 ``clock``（可注入，默认 ``time.monotonic`` 之外的墙钟 time.time），
方便测试用受控时钟模拟过期与迟到填充。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

# 结果留存的状态机
STATUS_ACTIVE = "有效"
STATUS_EXPIRED = "已过期"
STATUS_EVICTED = "已淘汰"
STATUS_BYPASS = "绕过缓存"


def _iso(epoch_seconds: float) -> str:
    """把 epoch 秒转成可读的 UTC ISO 字符串。"""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat()


def canonical_identity(
    *,
    expression: str,
    parameters: dict[str, Any] | None,
    dataset_version: str,
    key_rule: str,
) -> str:
    """生成完整身份的规范化字符串。

    参数按 key 排序后序列化，保证同一逻辑参数集合不会因字典顺序不同而变成不同身份；
    规范化串既用于派生缓存键，也用于命中后的逐字节身份核对。
    """
    payload = {
        "expression": expression,
        "parameters": parameters or {},
        "dataset_version": dataset_version,
        "key_rule": key_rule,
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive_cache_key(identity: str) -> str:
    """缓存键规则的默认实现：对完整身份做 SHA-256 摘要。

    键只用于候选定位，不作为命中的充分条件。
    """
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


class CacheVault:
    """缓存结果与策略快照的持久化层。

    用 JSON 原子落盘（临时文件 + os.replace），重启时原样加载，
    保存的是绝对到期时间点而不是剩余秒数，所以重启不改变任何到期时点。
    落盘路径可通过环境变量 ``CACHE_VAULT_PATH`` 覆盖；指向 ``memory`` 时只驻留内存。
    """

    def __init__(self, path: str | None = None) -> None:
        self.path = path or os.environ.get("CACHE_VAULT_PATH", "data/cache_vault.json")
        self._lock = threading.Lock()

    @property
    def memory_only(self) -> bool:
        return self.path == "memory"

    def load(self) -> dict[str, Any]:
        if self.memory_only:
            return {"entries": [], "policies": {}}
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {"entries": [], "policies": {}}
        except (json.JSONDecodeError, OSError):
            # 快照损坏时按空仓启动，绝不据此推断“永不过期”之类的宽松口径
            return {"entries": [], "policies": {}}
        data.setdefault("entries", [])
        data.setdefault("policies", {})
        return data

    def save(self, entries: list[dict[str, Any]], policies: dict[str, dict[str, Any]]) -> None:
        if self.memory_only:
            return
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        tmp_path = f"{self.path}.tmp"
        with self._lock:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump({"entries": entries, "policies": policies}, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self.path)


class ResultCache:
    """带身份核对、绝对期限与单飞填充的结果缓存。"""

    def __init__(
        self,
        vault: CacheVault | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.clock = clock or time.time
        self.vault = vault or CacheVault()
        snapshot = self.vault.load()
        # 重启直接沿用快照中的绝对时间点，不刷新任何 expires_at
        self._entries: list[dict[str, Any]] = list(snapshot.get("entries", []))
        self._policy_snapshot: dict[str, dict[str, Any]] = dict(snapshot.get("policies", {}))
        # key -> 填充占位；占位不跨进程持久化，重启后允许重新填充
        self._inflight: dict[str, dict[str, Any]] = {}
        self._key_locks: dict[str, threading.Lock] = {}
        self._guard = threading.RLock()
        self._seq = 0

    # ------------------------------------------------------------------ 内部工具
    def _next_id(self) -> int:
        self._seq += 1
        return max(self._seq, max((int(item.get("id", 0)) for item in self._entries), default=0) + 1)

    def _key_lock(self, cache_key: str) -> threading.Lock:
        with self._guard:
            lock = self._key_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                self._key_locks[cache_key] = lock
            return lock

    def _persist(self) -> None:
        # 只持久化已接纳的结果与策略快照；inflight 占位是进程内并发原语，不落盘
        self.vault.save(self._entries, self._policy_snapshot)

    @staticmethod
    def _same_identity(entry: dict[str, Any], identity: str) -> bool:
        """完整身份核对：规范化串必须完全一致，键碰撞在此被拦截。"""
        return entry.get("identity") == identity

    def _find_candidate(
        self,
        cache_key: str,
        identity: str,
        *,
        policy_id: int,
    ) -> dict[str, Any] | None:
        """按缓存键找候选，再核对策略归属与完整身份；身份不符的候选直接忽略。"""
        now = self.clock()
        for entry in self._entries:
            if entry.get("cache_key") != cache_key:
                continue
            if int(entry.get("policy_id", -1)) != policy_id:
                continue
            if entry.get("status") not in (STATUS_ACTIVE, STATUS_EXPIRED):
                continue
            if not self._same_identity(entry, identity):
                # 键规则碰撞：绝不串用，继续找或当作未命中
                continue
            # 惰性标记过期，但不修改原始到期时点
            if entry["status"] == STATUS_ACTIVE and now >= float(entry["expires_at"]):
                entry["status"] = STATUS_EXPIRED
                self._persist()
            return entry
        return None

    def describe_validity(self, entry: dict[str, Any]) -> str:
        """给出一条结果“为何仍有效/为何失效”的可读解释。"""
        now = self.clock()
        expires_at = float(entry["expires_at"])
        if entry.get("status") == STATUS_EVICTED:
            return f"结果已于 {entry.get('evicted_at_label', '—')} 被手动淘汰，不再复用"
        if now < expires_at and entry.get("status") == STATUS_ACTIVE:
            remain = int(expires_at - now)
            return (
                f"完整身份（表达式/参数/数据集版本/键规则）核对一致，"
                f"距原始到期时点 {entry['expires_at_label']} 还有 {remain} 秒"
            )
        return f"已过原始到期时点 {entry['expires_at_label']}，结果失效且不会被迟到填充复活"

    # ------------------------------------------------------------------ 策略快照
    def sync_policy(self, policy: dict[str, Any]) -> None:
        """保存策略快照（含 TTL），供结果留存解释来源；不改变任何已有结果的到期时点。"""
        self._policy_snapshot[str(policy["id"])] = {
            "policy_id": policy["id"],
            "策略编号": policy.get("策略编号"),
            "缓存键规则": policy.get("缓存键规则"),
            "过期秒数": policy.get("过期秒数"),
            "status": policy.get("status"),
        }
        self._persist()

    def policy_snapshot(self, policy_id: int) -> dict[str, Any] | None:
        return self._policy_snapshot.get(str(policy_id))

    # ------------------------------------------------------------------ 读取链路
    def peek(
        self,
        *,
        cache_key: str,
        identity: str,
        policy_id: int,
    ) -> dict[str, Any] | None:
        """只观察当前留存（含过期/淘汰记录），不触发填充。"""
        with self._guard:
            return self._find_candidate(cache_key, identity, policy_id=policy_id)

    def get_or_fill(
        self,
        *,
        identity: str,
        policy_id: int,
        ttl_seconds: int,
        loader: Callable[[], Any],
        source: str = "交互查询",
        wait_timeout: float = 30.0,
    ) -> dict[str, Any]:
        """命中则复用，未命中则单飞填充；任何填充失败都明确绕过缓存但不吞掉查询。

        返回统一结构：
          {hit, bypassed, status, reason, entry, result}
        """
        cache_key = derive_cache_key(identity)
        now = self.clock()
        lock = self._key_lock(cache_key)

        with lock:
            candidate = self._find_candidate(cache_key, identity, policy_id=policy_id)
            if candidate is not None and candidate["status"] == STATUS_ACTIVE:
                candidate["hit_count"] = int(candidate.get("hit_count", 0)) + 1
                candidate["last_hit_at"] = now
                candidate["last_hit_at_label"] = _iso(now)
                self._persist()
                return {
                    "hit": True,
                    "bypassed": False,
                    "status": STATUS_ACTIVE,
                    "reason": self.describe_validity(candidate),
                    "entry": candidate,
                    "result": candidate.get("result"),
                }

            # 已有同键占位：本线程不重复填充，等待首个填充者的结果
            inflight = self._inflight.get(cache_key)
            if inflight is not None:
                return self._wait_inflight(
                    cache_key, identity, policy_id, inflight, lock, wait_timeout
                )

            # 本线程成为该键唯一的填充者
            token = f"fill-{now:f}-{threading.get_ident()}-{os.getpid()}"
            self._inflight[cache_key] = {
                "token": token,
                "identity": identity,
                "policy_id": policy_id,
                "started_at": now,
                "event": threading.Event(),
            }

        try:
            result = loader()
        except Exception as exc:  # 填充失败：清理占位，明确绕过缓存
            with lock:
                holder = self._inflight.get(cache_key)
                if holder and holder["token"] == token:
                    event = holder.pop("event")
                    self._inflight.pop(cache_key, None)
                    event.set()
            return {
                "hit": False,
                "bypassed": True,
                "status": STATUS_BYPASS,
                "reason": f"缓存填充失败已绕过缓存：{type(exc).__name__}: {exc}",
                "entry": None,
                "result": None,
            }

        with lock:
            holder = self._inflight.get(cache_key)
            event = holder.pop("event") if holder else None
            self._inflight.pop(cache_key, None)

            # 迟到填充的接纳条件：占位仍是自己、身份仍一致、且当前没有有效留存
            fresh_now = self.clock()
            recheck = self._find_candidate(cache_key, identity, policy_id=policy_id)
            if holder is None or holder.get("token") != token:
                self._safe_set(event)
                return self._bypass("填充结果迟到：占位已被其他填充接管，旧结果不予接纳")
            if recheck is not None and recheck["status"] == STATUS_ACTIVE:
                self._safe_set(event)
                return {
                    "hit": True,
                    "bypassed": False,
                    "status": STATUS_ACTIVE,
                    "reason": "等待期间已有并发请求填入有效结果，直接复用该结果",
                    "entry": recheck,
                    "result": recheck.get("result"),
                }
            if not isinstance(result, dict) or not result.get("ok", False):
                self._safe_set(event)
                return self._bypass("填充结果不是有效查询结果，不写入缓存")

            # 只接纳一个有效结果；TTL 从接纳时刻起算，形成全新留存而非复活旧记录
            expires_at = fresh_now + ttl_seconds
            entry = {
                "id": self._next_id(),
                "cache_key": cache_key,
                "identity": identity,
                "policy_id": policy_id,
                "source": source,
                "status": STATUS_ACTIVE,
                "result": result,
                "stored_at": fresh_now,
                "stored_at_label": _iso(fresh_now),
                "expires_at": expires_at,
                "expires_at_label": _iso(expires_at),
                "hit_count": 0,
            }
            self._entries.append(entry)
            self._persist()
            self._safe_set(event)
            return {
                "hit": False,
                "bypassed": False,
                "status": STATUS_ACTIVE,
                "reason": (
                    "缓存未命中，已接纳本次填充的唯一有效结果；"
                    f"原始到期时点固定为 {entry['expires_at_label']}，重启不延寿"
                ),
                "entry": entry,
                "result": result,
            }

    def _wait_inflight(
        self,
        cache_key: str,
        identity: str,
        policy_id: int,
        inflight: dict[str, Any],
        lock: threading.Lock,
        wait_timeout: float,
    ) -> dict[str, Any]:
        """等待同键的首个填充者；超时或占位失效时绕过缓存，保证查询不被阻塞。"""
        event = inflight.get("event")
        if event is None:
            return self._bypass("同键填充正在切换状态，本次绕过缓存")
        # 占位身份不一致本身就是异常信号：不等待、不串用
        if inflight.get("identity") != identity or int(inflight.get("policy_id", -1)) != policy_id:
            return self._bypass("同键占位的完整身份不一致（疑似键碰撞），拒绝串用结果")
        lock.release()
        try:
            finished = event.wait(timeout=wait_timeout)
        finally:
            lock.acquire()
        if not finished:
            # 等待超时即判定占位失效：清理后本次绕过，后续请求可重新发起填充。
            # 即便原生产者之后迟到返回，接纳前的活性/身份复核也会拦下重复写入，
            # 因此清理占位不会破坏“只接纳一个有效结果”。
            stale = self._inflight.get(cache_key)
            if stale is not None and stale.get("identity") == identity:
                self._inflight.pop(cache_key, None)
            return self._bypass("等待同键填充超时，本次绕过缓存直接返回")
        candidate = self._find_candidate(cache_key, identity, policy_id=policy_id)
        if candidate is not None and candidate["status"] == STATUS_ACTIVE:
            candidate["hit_count"] = int(candidate.get("hit_count", 0)) + 1
            candidate["last_hit_at"] = self.clock()
            candidate["last_hit_at_label"] = _iso(self.clock())
            self._persist()
            return {
                "hit": True,
                "bypassed": False,
                "status": STATUS_ACTIVE,
                "reason": f"并发填充已完成且身份核对一致，复用唯一结果（{self.describe_validity(candidate)}）",
                "entry": candidate,
                "result": candidate.get("result"),
            }
        return self._bypass("首个填充未产生可用缓存结果，本次绕过缓存")

    @staticmethod
    def _safe_set(event: threading.Event | None) -> None:
        if event is not None:
            event.set()

    @staticmethod
    def _bypass(reason: str) -> dict[str, Any]:
        return {
            "hit": False,
            "bypassed": True,
            "status": STATUS_BYPASS,
            "reason": reason,
            "entry": None,
            "result": None,
        }

    # ------------------------------------------------------------------ 管理链路
    def list_entries(
        self,
        *,
        status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """分页查看结果留存；同时把每条记录的有效性解释与原始到期时点带给页面。"""
        now = self.clock()
        rows: list[dict[str, Any]] = []
        with self._guard:
            for entry in self._entries:
                if entry["status"] == STATUS_ACTIVE and now >= float(entry["expires_at"]):
                    entry["status"] = STATUS_EXPIRED
                    changed = True
                else:
                    changed = False
                view = dict(entry)
                view["validity_reason"] = self.describe_validity(entry)
                view["now_label"] = _iso(now)
                rows.append(view)
                if changed:
                    self._persist()
        if status:
            rows = [row for row in rows if row.get("status") == status]
        if keyword:
            rows = [
                row
                for row in rows
                if keyword in str(row.get("cache_key", ""))
                or keyword in str(row.get("source", ""))
                or keyword in str(row.get("identity", ""))
            ]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with self._guard:
            for entry in self._entries:
                if int(entry.get("id", 0)) == entry_id:
                    view = dict(entry)
                    view["validity_reason"] = self.describe_validity(entry)
                    return view
        return None

    def evict_entry(self, entry_id: int) -> dict[str, Any] | None:
        """手动淘汰：标记状态并记录淘汰时间，原始到期时点保留可追溯。"""
        now = self.clock()
        with self._guard:
            for entry in self._entries:
                if int(entry.get("id", 0)) == entry_id:
                    entry["status"] = STATUS_EVICTED
                    entry["evicted_at"] = now
                    entry["evicted_at_label"] = _iso(now)
                    self._persist()
                    view = dict(entry)
                    view["validity_reason"] = self.describe_validity(entry)
                    return view
        return None

    def stats(self) -> dict[str, int]:
        now = self.clock()
        counts = {STATUS_ACTIVE: 0, STATUS_EXPIRED: 0, STATUS_EVICTED: 0}
        hits = 0
        with self._guard:
            for entry in self._entries:
                if entry["status"] == STATUS_ACTIVE and now >= float(entry["expires_at"]):
                    entry["status"] = STATUS_EXPIRED
                counts[entry["status"]] = counts.get(entry["status"], 0) + 1
                hits += int(entry.get("hit_count", 0))
        return {
            "total": len(self._entries),
            "active": counts.get(STATUS_ACTIVE, 0),
            "expired": counts.get(STATUS_EXPIRED, 0),
            "evicted": counts.get(STATUS_EVICTED, 0),
            "hits": hits,
        }


# 进程内单例：服务层、动作接口与页面链路共用同一份留存
result_cache = ResultCache()
