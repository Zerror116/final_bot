import os
from datetime import datetime, timedelta

from sqlalchemy import String, BIGINT, Boolean, DateTime, Integer, func
from sqlalchemy.orm import mapped_column, Session

from .db import AbstractModel, engine


class Posts(AbstractModel):
    __tablename__ = "posts"
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id = mapped_column(BIGINT, nullable=False)
    photo = mapped_column(String, nullable=False)
    price = mapped_column(Integer, nullable=False)
    description = mapped_column(String, nullable=False)
    message_id = mapped_column(BIGINT, nullable=True)
    quantity = mapped_column(Integer, nullable=False)
    is_sent = mapped_column(Boolean, nullable=False, default=0)
    created_at = mapped_column(DateTime, nullable=False, default=func.now())

    @staticmethod
    def insert(chat_id: int, photo: str, price: str, description: str, quantity: int):
        with Session(bind=engine) as session:
            posts = Posts(
                chat_id=chat_id,
                photo=photo,
                price=price,
                description=description,
                quantity=quantity
            )
            session.add(posts)
            session.commit()

    @staticmethod
    def get_row(post_id: int):
        with Session(bind=engine) as session:
            query = session.query(Posts).filter(Posts.id == post_id).first()
            return query

    @staticmethod
    def delete_row(post_id: int):
        with Session(bind=engine) as session:
            query = session.query(Posts).filter(Posts.id == post_id).first()
            if query is None:
                return False, "Пост не найден."  # Возвращаем False, если пост не найден

            try:
                if query.photo:
                    photo_path = query.photo
                    if os.path.exists(photo_path):
                        os.remove(photo_path)
            except Exception as e:
                return False, f"Ошибка при удалении фотографии: {str(e)}"

            try:
                session.delete(query)
                session.commit()
                return True, "Пост и связанные данные успешно удалены."  # Возвращаем True, если всё успешно удалено
            except Exception as e:
                return False, f"Ошибка при удалении поста: {str(e)}"

    @staticmethod
    def update_row(post_id: int, price: int = None, description: str = None, quantity: int = None, is_sent: bool = None,
                   created_at: datetime = None):
        with Session(bind=engine) as session:
            post = session.query(Posts).filter(Posts.id == post_id).first()
            if not post:
                return False, "Пост не найден"

            # Обновляем только те значения, которые переданы
            if price is not None:
                post.price = price
            if description is not None:
                post.description = description
            if quantity is not None:
                post.quantity = quantity
            if is_sent is not None:
                post.is_sent = is_sent
            if created_at is not None:
                post.created_at = created_at

            session.commit()
            return True, "Данные успешно обновлены"

    @staticmethod
    def get_unsent_posts():
        with Session(bind=engine) as session:
            return session.query(Posts).filter(Posts.is_sent == False).all()

    @staticmethod
    def mark_as_sent(post_id, message_id):
        with Session(bind=engine) as session:
            post = session.query(Posts).filter(Posts.id == post_id).first()
            if post:
                post.is_sent = True
                post.message_id = message_id
                session.commit()

    @classmethod
    def increment_quantity_by_id(cls, post_id):
        with Session(bind=engine) as session:
            post = session.query(Posts).filter(Posts.id == post_id).first()
            if post:
                post.quantity += 1
                session.commit()

    @staticmethod
    def get_row_by_id(post_id: int):
        with Session(bind=engine) as session:
            query = session.query(Posts).filter(Posts.id == post_id).first()
            return query


    @staticmethod
    def get_posts_in_last_week(chat_id: int):
        """Получение постов за последние 7 дней."""
        now = datetime.utcnow()  # Текущее время
        last_week = now - timedelta(days=7)  # Временной интервал: последние 7 дней

        with Session(bind=engine) as session:
            posts = session.query(Posts).filter(
                Posts.chat_id == chat_id,
                Posts.created_at >= last_week
            ).all()

        return posts

    @staticmethod
    def get_row_all():
        with Session(bind=engine) as session:
            query = session.query(Posts).all()
            return query


