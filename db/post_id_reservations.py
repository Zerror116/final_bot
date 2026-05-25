import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import BIGINT, DateTime, Index, Integer
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.datetime.now(SAMARA_TZ).replace(tzinfo=None)


class PostIdReservation(AbstractModel):
    __tablename__ = "post_id_reservations"
    __table_args__ = (
        Index("ix_post_id_reservations_chat_id", "chat_id"),
        Index("ix_post_id_reservations_reserved_at", "reserved_at"),
    )

    post_id = mapped_column(Integer, primary_key=True)
    chat_id = mapped_column(BIGINT, nullable=False)
    reserved_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
