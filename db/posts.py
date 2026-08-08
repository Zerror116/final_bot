import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import String, BIGINT, Boolean, DateTime, Integer, Index, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import mapped_column, Session

from .db import AbstractModel, engine
from .post_id_reservations import PostIdReservation


SAMARA_TZ = ZoneInfo("Europe/Samara")
POST_ID_RESERVATION_TTL = timedelta(hours=6)
POST_ID_RESERVATION_ATTEMPTS = 10


USED_POST_ID_TABLES = (
    ("posts", "id"),
    ("post_id_reservations", "post_id"),
    ("reservations", "post_id"),
    ("temp_fulfilled", "post_id"),
    ("in_delivery", "post_id"),
    ("temp_reservations", "post_id"),
    ("deleted_post_snapshots", "post_id"),
    ("revision_logs", "post_id"),
)
HISTORICAL_POST_ID_TABLES = USED_POST_ID_TABLES[2:]


def samara_now_naive():
    return datetime.now(SAMARA_TZ).replace(tzinfo=None)


def normalize_post_created_at(value=None):
    value = value or samara_now_naive()
    if value.tzinfo is not None:
        value = value.astimezone(SAMARA_TZ).replace(tzinfo=None)

    if value.weekday() == 6:
        value = value + timedelta(days=1)
    return value


def default_post_created_at():
    return normalize_post_created_at()


class Posts(AbstractModel):
    __tablename__ = "posts"
    __table_args__ = (
        Index("ix_posts_chat_id", "chat_id"),
        Index("ix_posts_is_sent", "is_sent"),
        Index("ix_posts_message_id", "message_id"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id = mapped_column(BIGINT, nullable=False)
    photo = mapped_column(String, nullable=False)
    price = mapped_column(Integer, nullable=False)
    description = mapped_column(String, nullable=False)
    message_id = mapped_column(BIGINT, nullable=True)
    quantity = mapped_column(Integer, nullable=False)
    is_sent = mapped_column(Boolean, nullable=False, default=0)
    created_at = mapped_column(DateTime, nullable=False, default=default_post_created_at)

    @staticmethod
    def next_created_at(value=None):
        return normalize_post_created_at(value)

    @staticmethod
    def reserve_next_id(chat_id=None, max_attempts=POST_ID_RESERVATION_ATTEMPTS):
        owner_chat_id = int(chat_id or 0)
        Posts.cleanup_post_id_reservations(owner_chat_id)

        for attempt in range(1, max_attempts + 1):
            now = samara_now_naive()
            with Session(bind=engine) as session:
                reserved_post_id = session.execute(text("""
                    WITH used_ids AS (
                        SELECT id FROM posts WHERE id > 0
                        UNION
                        SELECT post_id AS id FROM post_id_reservations WHERE post_id > 0
                        UNION
                        SELECT post_id AS id FROM reservations WHERE post_id > 0
                        UNION
                        SELECT post_id AS id FROM temp_fulfilled WHERE post_id > 0
                        UNION
                        SELECT post_id AS id FROM in_delivery WHERE post_id > 0
                        UNION
                        SELECT post_id AS id FROM temp_reservations WHERE post_id > 0
                        UNION
                        SELECT post_id AS id FROM deleted_post_snapshots WHERE post_id > 0
                        UNION
                        SELECT post_id AS id FROM revision_logs WHERE post_id > 0
                    ),
                    candidates AS (
                        SELECT 1 AS id
                        UNION
                        SELECT id + 1 FROM used_ids
                    )
                    SELECT MIN(c.id)
                    FROM candidates c
                    WHERE c.id > 0
                    AND NOT EXISTS (SELECT 1 FROM used_ids u WHERE u.id = c.id)
                """)).scalar()

                reserved_post_id = int(reserved_post_id or 1)
                session.add(PostIdReservation(
                    post_id=reserved_post_id,
                    chat_id=owner_chat_id,
                    reserved_at=now,
                ))
                try:
                    session.commit()
                    return reserved_post_id
                except IntegrityError:
                    session.rollback()
                    if attempt == max_attempts:
                        raise
                except SQLAlchemyError:
                    session.rollback()
                    if attempt == max_attempts:
                        raise
                    time.sleep(0.2 * attempt)

        raise RuntimeError("Не удалось зарезервировать свободный ID товара.")

    @staticmethod
    def cleanup_post_id_reservations(owner_chat_id=0):
        now = samara_now_naive()
        expires_before = now - POST_ID_RESERVATION_TTL
        try:
            with Session(bind=engine) as session:
                session.query(PostIdReservation).filter(
                    PostIdReservation.reserved_at < expires_before
                ).delete(synchronize_session=False)
                if owner_chat_id:
                    session.query(PostIdReservation).filter(
                        PostIdReservation.chat_id == int(owner_chat_id)
                    ).delete(synchronize_session=False)
                session.commit()
        except SQLAlchemyError:
            return False
        return True

    @staticmethod
    def post_id_has_any_links(session, post_id):
        post_id = int(post_id)
        for table_name, column_name in HISTORICAL_POST_ID_TABLES:
            exists = session.execute(
                text(f"SELECT 1 FROM {table_name} WHERE {column_name} = :post_id LIMIT 1"),
                {"post_id": post_id},
            ).first()
            if exists:
                return True
        return False

    @staticmethod
    def release_reserved_id(post_id, chat_id=None):
        if post_id is None:
            return

        with Session(bind=engine) as session:
            query = session.query(PostIdReservation).filter(
                PostIdReservation.post_id == int(post_id)
            )
            if chat_id is not None:
                query = query.filter(PostIdReservation.chat_id == int(chat_id))
            query.delete(synchronize_session=False)
            session.commit()

    @staticmethod
    def insert(chat_id: int, photo: str, price: str, description: str, quantity: int,
               created_at: datetime = None, post_id: int = None):
        if post_id is None:
            post_id = Posts.reserve_next_id(chat_id=chat_id)
        else:
            post_id = int(post_id)

        with Session(bind=engine) as session:
            reservation = session.query(PostIdReservation).filter(
                PostIdReservation.post_id == post_id
            ).first()
            if reservation and int(reservation.chat_id) != int(chat_id):
                raise ValueError(f"ID {post_id} уже зарезервирован для другого поста.")
            if session.query(Posts.id).filter(Posts.id == post_id).first():
                raise ValueError(f"ID {post_id} уже занят другим товаром.")
            if Posts.post_id_has_any_links(session, post_id):
                raise ValueError(f"ID {post_id} уже используется в истории товара.")

            posts = Posts(
                id=post_id,
                chat_id=chat_id,
                photo=photo,
                price=price,
                description=description,
                quantity=quantity,
                created_at=normalize_post_created_at(created_at),
            )
            session.add(posts)
            if reservation:
                session.delete(reservation)
            session.commit()
            session.refresh(posts)
            return posts.id

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
    def update_row(post_id: int, price: int = None, description: str = None, quantity: int = None,
                   is_sent: bool = None, created_at: datetime = None, chat_id: int = None):
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
                post.created_at = normalize_post_created_at(created_at)
            if chat_id is not None:  # Добавлено обновление chat_id
                post.chat_id = chat_id

            session.commit()
            return True, "Данные успешно обновлены"


    @staticmethod
    def get_unsent_posts():
        with Session(bind=engine) as session:
            return session.query(Posts).filter(
                Posts.is_sent == False,
                Posts.message_id == None,
            ).order_by(Posts.id).all()

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
        now = samara_now_naive()  # Текущее время по Самаре
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


    @staticmethod
    def get_all_posts():
            """Возвращает посты, которые ещё не были отправлены в канал."""
            return Posts.get_unsent_posts()

    @staticmethod
    def get_user_posts(user_id):
            """Возвращает посты, созданные конкретным пользователем, которые ещё не отправлены."""
            with Session(bind=engine) as session:
                return session.query(Posts).filter(
                    Posts.chat_id == user_id,
                    Posts.is_sent == False,
                    Posts.message_id == None,
                ).all()

    @staticmethod
    def clone_post(post_id, **kwargs):
        original_post = Posts.get_row_by_id(post_id)

        if not original_post:
            print(f"❌ Ошибка: Оригинальный пост с ID {post_id} не найден.")
            return None, "Оригинальный пост не найден."

        try:
            print(f"Копируем пост ID={post_id} с параметрами: {kwargs}")

            # Создаём новый пост
            new_post_id = Posts.insert(
                chat_id=kwargs.get("chat_id", original_post.chat_id),
                photo=kwargs.get("photo", original_post.photo),
                price=kwargs.get("price", original_post.price),
                description=kwargs.get("description", original_post.description),
                quantity=kwargs.get("quantity", original_post.quantity)
            )

            if not new_post_id:
                print(f"❌ Ошибка при создании поста: insert вернул None.")
                return None, "Ошибка при сохранении нового поста."

            print(f"✅ Новый пост создан с ID: {new_post_id}")
            return new_post_id, None

        except Exception as e:
            print(f"❌ Ошибка при создании нового поста: {e}")
            return None, f"Ошибка: {e}"
