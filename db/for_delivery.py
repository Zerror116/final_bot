from sqlalchemy import BIGINT, Index, Integer, String
from sqlalchemy.orm import mapped_column, Session
from .db import AbstractModel, engine

class ForDelivery(AbstractModel):
    __tablename__ = "for_delivery"
    __table_args__ = (
        Index("ix_for_delivery_user_id", "user_id"),
        Index("ix_for_delivery_phone", "phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    phone = mapped_column(String, nullable=False)
    name = mapped_column(String, nullable=False)
    total_sum = mapped_column(Integer, nullable=False)
    address = mapped_column(String, nullable=False)
    user_id = mapped_column(BIGINT, nullable=False)

    @staticmethod
    def insert(user_id, name, phone, address, total_sum):
        with Session(bind=engine) as session:
            try:
                new_entry = ForDelivery(
                    user_id=user_id,
                    name=name,
                    phone=phone,
                    address=address,
                    total_sum=total_sum
                )
                session.add(new_entry)
                session.commit()  # Подтверждаем изменения
            except Exception as e:
                session.rollback()  # В случае ошибки откатываем изменения
                raise e

    @staticmethod
    def get_all_rows():
        with Session(bind=engine) as session:
            return session.query(ForDelivery).all()

    @staticmethod
    def delete_all_rows():
        with Session(bind=engine) as session:
            try:
                session.query(ForDelivery).delete()
                session.commit()
            except Exception as e:
                session.rollback()
                raise e
