from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import mapped_column

from .db import AbstractModel

SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class PcWakeJob(AbstractModel):
    """Команда включить админский ПК (забирает телефон рядом с ПК и шлёт Wake-on-LAN)."""

    __tablename__ = "pc_wake_jobs"
    __table_args__ = (
        Index("ix_pc_wake_jobs_status_created", "status", "created_at"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    requested_by = mapped_column(BIGINT, nullable=True)
    chat_id = mapped_column(BIGINT, nullable=True)
    status = mapped_column(String, nullable=False, default="pending")
    created_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    taken_at = mapped_column(DateTime, nullable=True)
    done_at = mapped_column(DateTime, nullable=True)
    error = mapped_column(Text, nullable=True)
