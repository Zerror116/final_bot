from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, Boolean, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class DeletedPostSnapshot(AbstractModel):
    __tablename__ = "deleted_post_snapshots"
    __table_args__ = (
        UniqueConstraint("post_id", name="uq_deleted_post_snapshots_post_id"),
        Index("ix_deleted_post_snapshots_post_id", "post_id"),
        Index("ix_deleted_post_snapshots_created_at", "created_at"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id = mapped_column(Integer, nullable=False)
    chat_id = mapped_column(BIGINT, nullable=False)
    photo = mapped_column(String, nullable=True)
    price = mapped_column(Integer, nullable=False)
    description = mapped_column(String, nullable=False)
    message_id = mapped_column(BIGINT, nullable=True)
    quantity = mapped_column(Integer, nullable=False)
    is_sent = mapped_column(Boolean, nullable=False, default=0)
    created_at = mapped_column(DateTime, nullable=False)
    deleted_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    reason = mapped_column(String, nullable=False)
