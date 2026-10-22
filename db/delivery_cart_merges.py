from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import DateTime, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import mapped_column

from .db import AbstractModel


SAMARA_TZ = ZoneInfo("Europe/Samara")


def local_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


class DeliveryCartMerge(AbstractModel):
    __tablename__ = "delivery_cart_merges"
    __table_args__ = (
        UniqueConstraint("extra_phone", name="uq_delivery_cart_merges_extra_phone"),
        Index("ix_delivery_cart_merges_main_phone", "main_phone"),
        Index("ix_delivery_cart_merges_extra_phone", "extra_phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    main_phone = mapped_column(String, nullable=False)
    extra_phone = mapped_column(String, nullable=False)
    merge_order = mapped_column(Integer, nullable=False, default=0)
    created_at = mapped_column(DateTime, nullable=False, default=local_now_naive)
