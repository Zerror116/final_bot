from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class DeliveryCleanupRun(AbstractModel):
    __tablename__ = "delivery_cleanup_runs"
    __table_args__ = (
        UniqueConstraint("slot_key", name="uq_delivery_cleanup_runs_slot_key"),
        Index("ix_delivery_cleanup_runs_started_at", "started_at"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    slot_key = mapped_column(String, nullable=False)
    started_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    finished_at = mapped_column(DateTime, nullable=True)
    in_delivery_deleted = mapped_column(Integer, nullable=False, default=0)
    temp_fulfilled_deleted = mapped_column(Integer, nullable=False, default=0)
    posts_deleted = mapped_column(Integer, nullable=False, default=0)
    status = mapped_column(String, nullable=False, default="started")
    details = mapped_column(Text, nullable=True)
