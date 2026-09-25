"""数字贸易合规沙盒后端的异常类型。"""

from __future__ import annotations


class SandboxError(Exception):
    """所有领域错误的基类，消息面向业务人员。"""


class ValidationError(SandboxError):
    """材料或字段不满足业务要求。"""


class StateError(SandboxError):
    """项目当前阶段或运行状态不允许该操作。"""


class ConflictError(SandboxError):
    """评审人员存在辅导或利益关系，依法应当回避。"""


class AccessDeniedError(SandboxError):
    """访问者不承担对应审查职责，或材料被争议冻结。"""


class CapacityError(SandboxError):
    """沙盒名额不足，不能作出新的准入决定。"""


class ImmutableEvidenceError(SandboxError):
    """争议冻结后的证据集合不允许改动。"""
