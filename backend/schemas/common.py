"""统一响应包络与分页模型（架构文档 §6.1）。"""
from __future__ import annotations

from typing import Generic, TypeVar, Optional

from pydantic import BaseModel, Field

T = TypeVar("T")


class Resp(BaseModel, Generic[T]):
    code: int = 0
    msg: str = "ok"
    data: Optional[T] = None


class PageQuery(BaseModel):
    """分页入参：page 从 1 开始；size 默认 10、上限 100。"""

    page: int = Field(default=1, ge=1)
    size: int = Field(default=10, ge=1, le=100)

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.size


class PageData(BaseModel, Generic[T]):
    total: int = 0
    list: list[T] = Field(default_factory=list)
