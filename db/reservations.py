from sqlalchemy import (
    BIGINT,
    Boolean,
    DateTime,
    Index,
    Integer,
)
from sqlalchemy.orm import mapped_column, Session
from datetime import datetime, timezone

from . import Posts
from .db import AbstractModel, engine

def utcnow_naive():
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Reservations(AbstractModel):
    __tablename__ = "reservations"
    __table_args__ = (
        Index("ix_reservations_user_id", "user_id"),
        Index("ix_reservations_post_id", "post_id"),
        Index("ix_reservations_user_fulfilled", "user_id", "is_fulfilled"),
        Index("ix_reservations_fulfilled_created_at", "is_fulfilled", "created_at"),
        Index("ix_reservations_fulfilled_at", "is_fulfilled", "fulfilled_at"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    quantity = mapped_column(Integer, nullable=False)
    post_id = mapped_column(Integer, nullable=False)
    is_fulfilled = mapped_column(Boolean, nullable=False)
    return_order = mapped_column(Integer, default=0)
    old_price = mapped_column(Integer, nullable=False)
    created_at = mapped_column(DateTime, nullable=True, default=utcnow_naive)
    fulfilled_at = mapped_column(DateTime, nullable=True)

    @staticmethod
    def insert(
        user_id: int,
        quantity: int,
        post_id: int,
        is_fulfilled: bool = False,
        old_price: int = None,
        fulfilled_at=None,
    ):
        with Session(bind=engine) as session:
            if old_price is None:
                post = session.query(Posts).filter(Posts.id == post_id).first()
                old_price = post.price if post else 0
            reservations = Reservations(
                user_id=user_id,
                quantity=quantity,
                post_id=post_id,
                is_fulfilled=is_fulfilled,
                old_price=old_price,
                fulfilled_at=fulfilled_at,
            )
            session.add(reservations)
            session.commit()
            session.refresh(reservations)
            return reservations.id

    @staticmethod
    def get_row_by_user_id(user_id: int):
        with Session(bind=engine) as session:
            query = session.query(Reservations).filter(Reservations.user_id == user_id).all()
            return query

    @staticmethod
    def update_row(reservation_id: int, updates: dict = None, **kwargs):
        with Session(bind=engine) as session:
            reservation = session.query(Reservations).filter(Reservations.id == reservation_id).first()
            if not reservation:
                return False, "Бронирование не найдено."

            values = {}
            if updates:
                values.update(updates)
            values.update(kwargs)

            for field, value in values.items():
                if hasattr(reservation, field):
                    setattr(reservation, field, value)
            session.commit()
            return True, "Данные успешно обновлены."

    @staticmethod
    def get_row_by_id(reservation_id):
        with Session(bind=engine) as session:
            return session.query(Reservations).filter_by(id=reservation_id).first()

    @staticmethod
    def cancel_order_by_id(reservation_id: int):
        with Session(bind=engine) as session:
            order = session.query(Reservations).filter(Reservations.id == reservation_id).first()
            if order:
                session.delete(order)
                session.commit()
                return True
            return False

    @staticmethod
    def get_row_all(user_id=None):
        """
        Получить все бронирования. Если указан user_id — фильтровать по user_id.
        """
        from db import Session, engine
        with Session(bind=engine) as session:
            query = session.query(Reservations)
            if user_id is not None:
                query = query.filter(Reservations.user_id == user_id)
            return query.all()

    @staticmethod
    def delete_row(reservation_id: int):
        """Удаляет строку из таблицы reservations по id."""
        with Session(bind=engine) as session:
            try:
                # Ищем запись по reservation_id
                reservation = session.query(Reservations).filter_by(id=reservation_id).first()

                # Если запись найдена - удаляем
                if reservation:
                    session.delete(reservation)
                    session.commit()
                    return True  # Запись успешно удалена
                else:
                    return False  # Запись с таким id не найдена
            except Exception as e:
                session.rollback()  # В случае ошибки откатываем изменения
                raise Exception(f"Ошибка при удалении строки с id {reservation_id}: {e}")

    @staticmethod
    def delete_rows_by_user_id(user_id: int):
        with Session(bind=engine) as session:
            deleted_count = session.query(Reservations).filter(
                Reservations.user_id == user_id
            ).delete(synchronize_session=False)
            session.commit()
            return deleted_count
