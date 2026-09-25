"""
存储层：内存仓储 + 事务（异常整体回滚）+ 哈希链操作记录 + 访问授权名单。

所有可变状态集中在 ``_state`` 中，事务开启时深拷贝；
事务内任何一步抛出异常，状态恢复到开启前，保证
"容量释放与退出生效处于同一事务"这类原子要求。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .models import MaterialKind, Person, Project, ReviewDuty


@dataclass
class RuleVersion:
    """规则版本。新版本只约束生效之后尚未作出的决定。"""

    version: int
    title: str
    effective_at: datetime
    content: dict[str, Any]
    fingerprint: str


@dataclass
class SecretGrant:
    """商业秘密开放授权：仅向承担对应审查职责的人员开放。"""

    id: str
    project_id: str
    person_id: str
    duty: ReviewDuty
    kinds: frozenset[MaterialKind]
    granted_by: str
    granted_at: datetime
    reason: str
    revoked_at: datetime | None = None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


@dataclass
class LogEntry:
    seq: int
    at: datetime
    actor: str
    action: str
    target: str
    detail: dict[str, Any]
    prev_hash: str
    entry_hash: str


def canonical(value: Any) -> str:
    """与键顺序无关的规范化 JSON，用于哈希与证据固定。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_entry(entry: LogEntry) -> str:
    payload = "|".join(
        [
            str(entry.seq),
            entry.at.isoformat(),
            entry.actor,
            entry.action,
            entry.target,
            canonical(entry.detail),
            entry.prev_hash,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, capacity: int = 10) -> None:
        self._state: dict[str, Any] = {
            "persons": {},
            "projects": {},
            "rules": {},
            "grants": [],
            "log": [],
            "capacity": capacity,
            "counters": {},
        }
        self._snapshot: dict[str, Any] | None = None

    # ---- 事务 ----------------------------------------------------------

    def begin(self) -> None:
        """开启事务。不支持嵌套，避免部分回滚语义不清。"""
        if self._snapshot is not None:
            raise RuntimeError("事务已存在，不支持嵌套事务")
        self._snapshot = copy.deepcopy(self._state)

    def commit(self) -> None:
        if self._snapshot is None:
            raise RuntimeError("没有进行中的事务")
        self._snapshot = None

    def rollback(self) -> None:
        if self._snapshot is not None:
            self._state = self._snapshot
            self._snapshot = None

    def __enter__(self) -> "Store":
        self.begin()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        return False  # 不吞异常，调用方仍可感知错误并断言

    # ---- 编号 ----------------------------------------------------------

    def next_id(self, prefix: str) -> str:
        counters = self._state["counters"]
        counters[prefix] = counters.get(prefix, 0) + 1
        return f"{prefix}-{counters[prefix]:04d}"

    # ---- 人员 ----------------------------------------------------------

    def save_person(self, person: Person) -> None:
        self._state["persons"][person.id] = person

    def get_person(self, person_id: str) -> Person:
        person = self._state["persons"].get(person_id)
        if person is None:
            raise KeyError(f"人员不存在: {person_id}")
        return person

    def list_persons(self) -> list[Person]:
        return list(self._state["persons"].values())

    # ---- 项目 ----------------------------------------------------------

    def save_project(self, project: Project) -> None:
        self._state["projects"][project.id] = project

    def get_project(self, project_id: str) -> Project:
        project = self._state["projects"].get(project_id)
        if project is None:
            raise KeyError(f"项目不存在: {project_id}")
        return project

    def list_projects(self) -> list[Project]:
        return list(self._state["projects"].values())

    # ---- 规则版本 ------------------------------------------------------

    def save_rule(self, rule: RuleVersion) -> None:
        if rule.version in self._state["rules"]:
            raise ValueError(f"规则版本已存在: v{rule.version}")
        self._state["rules"][rule.version] = rule

    def get_rule(self, version: int) -> RuleVersion:
        return self._state["rules"][version]

    def latest_rule_version(self, at: datetime | None = None) -> int:
        """返回指定时刻（默认不限）已生效的最高规则版本；未发布时为 0。"""
        versions = [
            r.version
            for r in self._state["rules"].values()
            if at is None or r.effective_at <= at
        ]
        return max(versions, default=0)

    def list_rules(self) -> list[RuleVersion]:
        return [self._state["rules"][v] for v in sorted(self._state["rules"])]

    # ---- 容量 ----------------------------------------------------------

    @property
    def capacity(self) -> int:
        return self._state["capacity"]

    def held_slots(self) -> int:
        return sum(1 for p in self._state["projects"].values() if p.slot_held)

    def free_slots(self) -> int:
        return self.capacity - self.held_slots()

    # ---- 商业秘密授权 --------------------------------------------------

    def add_grant(self, grant: SecretGrant) -> None:
        self._state["grants"].append(grant)

    def revoke_grant(self, grant_id: str, at: datetime) -> None:
        for grant in self._state["grants"]:
            if grant.id == grant_id and grant.active:
                grant.revoked_at = at
                return
        raise KeyError(f"有效授权不存在: {grant_id}")

    def active_grants(self, project_id: str) -> list[SecretGrant]:
        return [
            g
            for g in self._state["grants"]
            if g.project_id == project_id and g.active
        ]

    def grants_for(self, project_id: str) -> list[SecretGrant]:
        """项目的全部秘密授权记录（含已撤销），供争议固定访问名单。"""
        return [g for g in self._state["grants"] if g.project_id == project_id]

    def can_access_secret(
        self, project_id: str, person_id: str, kind: MaterialKind
    ) -> bool:
        return any(
            g.person_id == person_id and kind in g.kinds
            for g in self.active_grants(project_id)
        )

    # ---- 哈希链操作记录 ------------------------------------------------

    def record(
        self,
        at: datetime,
        actor: str,
        action: str,
        target: str,
        detail: dict[str, Any] | None = None,
    ) -> LogEntry:
        log: list[LogEntry] = self._state["log"]
        prev_hash = log[-1].entry_hash if log else "GENESIS"
        entry = LogEntry(
            seq=len(log) + 1,
            at=at,
            actor=actor,
            action=action,
            target=target,
            detail=detail or {},
            prev_hash=prev_hash,
            entry_hash="",
        )
        entry.entry_hash = _hash_entry(entry)
        log.append(entry)
        return entry

    def log(self) -> list[LogEntry]:
        return list(self._state["log"])

    def log_tail_hash(self) -> str:
        log: list[LogEntry] = self._state["log"]
        return log[-1].entry_hash if log else "GENESIS"

    def verify_log_chain(self) -> bool:
        """逐条复核哈希链，供争议证据与自检使用。"""
        previous = "GENESIS"
        for entry in self._state["log"]:
            if entry.prev_hash != previous:
                return False
            if entry.entry_hash != _hash_entry(entry):
                return False
            previous = entry.entry_hash
        return True
