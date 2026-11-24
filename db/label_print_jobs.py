from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import mapped_column

from .db import AbstractModel

SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class LabelPrintJob(AbstractModel):
    """Задание на печать наклейки клиента на админском ПК (забирает агент печати)."""

    __tablename__ = "label_print_jobs"
    __table_args__ = (
        Index("ix_label_print_jobs_status_created", "status", "created_at"),
        Index("ix_label_print_jobs_delivery_id", "delivery_id"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    delivery_id = mapped_column(Integer, nullable=True)
    phone = mapped_column(String, nullable=False)
    name = mapped_column(String, nullable=False)
    source = mapped_column(String, nullable=False, default="auto")
    requested_by = mapped_column(BIGINT, nullable=True)
    status = mapped_column(String, nullable=False, default="pending")
    attempts = mapped_column(Integer, nullable=False, default=0)
    created_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
    taken_at = mapped_column(DateTime, nullable=True)
    printed_at = mapped_column(DateTime, nullable=True)
    error = mapped_column(Text, nullable=True)
