from sqlalchemy import (
    BIGINT,
    Boolean,
    DateTime,
    Index,
    Integer,
    func,
)
from sqlalchemy.orm import mapped_column, Session

from .db import AbstractModel, engine

class TempReservations(AbstractModel):
    __tablename__ = "temp_reservations"
    __table_args__ = (
        Index("ix_temp_reservations_user_post", "user_id", "post_id"),
        Index("ix_temp_reservations_post_fulfilled", "post_id", "temp_fulfilled"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    quantity = mapped_column(Integer, nullable=False)
    post_id = mapped_column(Integer, nullable=False)
    temp_fulfilled = mapped_column(Boolean, nullable=False)
    created_at = mapped_column(DateTime, nullable=False, default=func.now())

    @staticmethod
    def insert(user_id: int, quantity: int, post_id: int, temp_fulfilled: bool):
        with Session(bind=engine) as session:
            reservations = TempReservations(
            user_id=user_id,
            quantity=quantity,
            post_id=post_id,
            temp_fulfilled=temp_fulfilled
            )
            session.add(reservations)
            session.commit()


