import datetime
from ast import Bytes

from sqlalchemy import (
    String,
    ForeignKey,
    BIGINT,
    Boolean,
    DateTime,
    or_, Integer,
    BLOB
)
from sqlalchemy.orm import mapped_column, Session, Mapped, MappedColumn

# from main import user_data
from .db import AbstractModel, engine

class Clients(AbstractModel):
    __tablename__ = "clients"
    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    name = mapped_column(String, nullable=False)
    phone = mapped_column(String, nullable=False)
    role = mapped_column(String, nullable=False, default="client")
    # msg_ids если траблы с кешем можешь через бд пойти с удаление сообщ

    @staticmethod
    def insert(user_id: int, name: str, phone: str, role: str):
        with Session(bind=engine) as session:
            try:
                client = Clients(user_id=user_id, name=name, phone=phone, role=role)
                session.add(client)
                session.commit()
            except Exception as e:
                print(f"[insert] Ошибка при сохранении клиента: {e}")
                raise

    @staticmethod
    def get_row_all(user_id: int):
        with Session(bind=engine) as session:
            query = session.query(Clients).filter(Clients.user_id == user_id).all()
            return query

    @staticmethod
    def get_row(user_id):
        """
        Возвращает клиента по user_id или None, если записи нет.
        """
        with Session(bind=engine) as session:
            result = session.query(Clients).filter(Clients.user_id == user_id).first()

            # Логируем результат проверки
            print(f"[get_row] Проверяем user_id={user_id}. Результат: {result}")
            return result

    @staticmethod
    def get_row_by_phone(phone):
        """
        Возвращает клиента по номеру телефона или None, если записи нет.
        """
        with Session(bind=engine) as session:
            print(f"[DEBUG]: Проверка номера телефона {phone} в базе.")
            result = session.query(Clients).filter(Clients.phone == phone).first()  # Поиск клиента в базе
            return result

    @staticmethod
    def get_row_by_user_id(user_id):
        from db import Session, engine
        with Session(bind=engine) as session:
            return session.query(Clients).filter(Clients.user_id == user_id).first()

    @staticmethod
    def delete_row(client_id: int):
        with Session(bind=engine) as session:
            query = session.query(Clients).filter(Clients.id == client_id).first()
            if query is None:
                print(f"Клиент с ID {client_id} не найден для удаления.")
                return False
            print(f"Клиент {query.phone} ({query.name}) успешно удален.")
            session.delete(query)
            session.commit()

    @staticmethod
    def update_row(user_id: int, name: str, phone: str, role: str):
        with Session(bind=engine) as session:
            query = session.query(Clients).filter(Clients.user_id == user_id).first()
            if query is None:
                return False, "Пользователь не найден."

            # Обновляем данные
            query.name = name
            query.phone = phone
            query.role = role
            try:
                session.commit()  # Сохраняем изменения
                print(f"[update_row] Обновлены данные для user_id={user_id}.")
            except Exception as e:
                print(f"[update_row] Ошибка при обновлении: {e}")
                session.rollback()
                raise
            return True, "Данные пользователя обновлены успешно."

    @staticmethod
    def update_row_to_role(filters, update_values):
        # filters — словарь, например {"id": 123}
        # update_values — словарь, например {"role": "admin"}
        with Session() as session:
            # Находим записи по фильтру
            query = session.query(Clients).filter_by(**filters)
            # Выполняем обновление только указанных полей
            query.update(update_values)
            session.commit()

    @staticmethod
    def validate_phone(phone):
        if not isinstance(phone, str) or not phone.isdigit():
            raise ValueError("Некорректное значение номера телефона")
        return phone

    @staticmethod
    def get_row_by_phone_digits(phone_digits: str):
        """
        Метод для поиска клиента по последним цифрам его номера телефона.
        """
        from db import Session, engine
        with Session(bind=engine) as session:
            return session.query(Clients).filter(Clients.phone.endswith(phone_digits)).first()
