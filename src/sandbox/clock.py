"""时间来源：运行期间用可暂停的业务时钟，测试可控制当前时刻。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable


def utc_now() -> datetime:
    """统一使用带时区的 UTC 时间，避免跨期限计算歧义。"""
    return datetime.now(timezone.utc)


@dataclass
class Clock:
    """业务时钟。

    服务中断时调用 ``pause``，恢复时调用 ``resume``；
    ``now()`` 在中断期间保持为中断发生时刻，使整改期限等
    以"实际可服务时间"推进，跨期中断恢复后仍按原计划顺延。
    """

    _source: Callable[[], datetime] = utc_now
    _paused_at: datetime | None = None
    _accumulated: timedelta = field(default_factory=lambda: timedelta(0))

    def pause(self, at: datetime | None = None) -> datetime:
        # 记录的是"冻结的业务时刻"（已剔除既往停顿），
        # 使多次中断也能正确累计。
        moment = at or self.now()
        if self._paused_at is None:
            self._paused_at = moment
        return self._paused_at

    def resume(self, at: datetime | None = None) -> timedelta:
        if self._paused_at is not None:
            wall = at or self._source()
            self._accumulated = wall - self._paused_at
            self._paused_at = None
        return self._accumulated

    def now(self) -> datetime:
        if self._paused_at is not None:
            return self._paused_at
        return self._source() - self._accumulated

    @property
    def is_paused(self) -> bool:
        return self._paused_at is not None

    def deadline_after(self, duration: timedelta) -> datetime:
        """从当前业务时刻起算的期限时刻；暂停区间不计入。"""
        return self.now() + duration
