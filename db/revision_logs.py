from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, DateTime, Index, Integer
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class RevisionLog(AbstractModel):
    __tablename__ = "revision_logs"
    __table_args__ = (
        Index("ix_revision_logs_auditor_created", "auditor_user_id", "created_at"),
        Index("ix_revision_logs_post_id", "post_id"),
        Index("ix_revision_logs_selected_date", "selected_date"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id = mapped_column(Integer, nullable=False)
    auditor_user_id = mapped_column(BIGINT, nullable=False)
    old_price = mapped_column(Integer, nullable=False)
    new_price = mapped_column(Integer, nullable=False)
    quantity = mapped_column(Integer, nullable=False)
    selected_date = mapped_column(DateTime, nullable=False)
    created_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
