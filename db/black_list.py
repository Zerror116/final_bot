from sqlalchemy import BIGINT, Index, Integer, String
from sqlalchemy.orm import mapped_column, Session

from .db import AbstractModel, engine

class BlackList(AbstractModel):
    __tablename__ = "black_list"
    __table_args__ = (
        Index("ix_black_list_user_id", "user_id"),
        Index("ix_black_list_phone", "phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    phone = mapped_column(String, nullable=False)

    @staticmethod
    def insert(user_id: int, phone: str):
        with Session(bind=engine) as session:
            try:
                entry = BlackList(user_id=user_id, phone=phone)
                session.add(entry)
                session.commit()
            except Exception:
                raise


    @staticmethod
    def get_row(user_id: int):
        with Session(bind=engine) as session:
            query = session.query(BlackList).filter(BlackList.user_id == user_id).all()
            return query

