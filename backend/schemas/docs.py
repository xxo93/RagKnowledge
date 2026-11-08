"""文档知识库接口的数据模型（需求 F-01~F-03 / 架构 §6.2）。"""
from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class DocItem(BaseModel):
    doc_id: int
    doc_name: str
    file_size: int
    chunk_count: int
    status: int                 # 0 处理中 / 1 成功 / 2 失败
    error_msg: str | None = None
    create_time: datetime | None = None


class DocUploadResult(BaseModel):
    doc_id: int
    chunk_count: int
    status: int


class DocDeleteResult(BaseModel):
    deleted_chunks: int
