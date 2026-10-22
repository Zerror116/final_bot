from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class ReservationStatEvent(AbstractModel):
    __tablename__ = "reservation_stat_events"
    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "reservation_id",
            name="uq_reservation_stat_events_event_reservation",
        ),
        Index("ix_reservation_stat_events_type_created", "event_type", "created_at"),
        Index("ix_reservation_stat_events_reservation_id", "reservation_id"),
        Index("ix_reservation_stat_events_user_id", "user_id"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type = mapped_column(String, nullable=False)
    reservation_id = mapped_column(Integer, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)
    post_id = mapped_column(Integer, nullable=False)
    quantity = mapped_column(Integer, nullable=False, default=1)
    created_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
