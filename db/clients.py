from sqlalchemy import (
    Index,
    String,
    BIGINT,
    Integer,
)
from sqlalchemy.orm import mapped_column, Session

from .db import AbstractModel, engine

class Clients(AbstractModel):
    __tablename__ = "clients"
    __table_args__ = (
        Index("ix_clients_user_id", "user_id"),
        Index("ix_clients_phone", "phone"),
        Index("ix_clients_name_phone", "name", "phone"),
    )

    id = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id = mapped_column(BIGINT, nullable=False)
    name = mapped_column(String, nullable=False)
    phone = mapped_column(String, nullable=False)
    role = mapped_column(String, nullable=False, default="client")
    # msg_ids если траблы с кешем можешь через бд пойти с удаление сообщ

    @staticmethod
    def normalize_phone(phone):
        import re

        digits = re.sub(r"\D", "", str(phone or ""))
        if len(digits) == 10:
            return f"8{digits}"
        if len(digits) == 11 and digits.startswith("7"):
            return f"8{digits[1:]}"
        return digits

    @staticmethod
    def phone_variants(phone):
        normalized = Clients.normalize_phone(phone)
        variants = {str(phone or "").strip(), normalized}
        if len(normalized) == 11 and normalized.startswith("8"):
            variants.add(f"7{normalized[1:]}")
            variants.add(f"+7{normalized[1:]}")
        return {variant for variant in variants if variant}

    @staticmethod
    def insert(user_id: int, name: str, phone: str, role: str):
        with Session(bind=engine) as session:
            try:
                client = Clients(
                    user_id=user_id,
                    name=name,
                    phone=Clients.normalize_phone(phone),
                    role=role,
                )
                session.add(client)
                session.commit()
            except Exception:
                raise

    @staticmethod
    def get_row_all():
        """
        Получить всех клиентов из базы данных.
        """
        from db import Session, engine
        with Session(bind=engine) as session:
            query = session.query(Clients)
            return query.all()

    @staticmethod
    def get_row(user_id):
        """
        Возвращает клиента по user_id или None, если записи нет.
        """
        with Session(bind=engine) as session:
            result = session.query(Clients).filter(Clients.user_id == user_id).first()

            # Логируем результат проверки
            return result

    @staticmethod
    def get_row_by_phone(phone):
        variants = Clients.phone_variants(phone)
        """
        Возвращает клиента по номеру телефона или None, если записи нет.
        """
        with Session(bind=engine) as session:
            result = session.query(Clients).filter(Clients.phone.in_(variants)).first()  # Поиск клиента в базе
            return result

    @staticmethod
    def get_rows_by_phone(phone):
        variants = Clients.phone_variants(phone)
        """
        Возвращает всех клиентов с точным совпадением полного номера телефона.
        """
        with Session(bind=engine) as session:
            return session.query(Clients).filter(Clients.phone.in_(variants)).all()

    @staticmethod
    def get_row_by_user_id(user_id):
        from db import Session, engine
        with Session(bind=engine) as session:
            return session.query(Clients).filter(Clients.user_id == user_id).first()

    @staticmethod
    def get_row_by_id(client_id):
        with Session(bind=engine) as session:
            return session.query(Clients).filter(Clients.id == client_id).first()

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
        phone = Clients.normalize_phone(phone)
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
    def get_row_by_phone_digits(phone_digits):
        phone_digits = Clients.normalize_phone(phone_digits)[-4:]
        """Получение всех пользователей с совпадающими последними цифрами номера."""
        with Session(bind=engine) as session:
            clients = session.query(Clients).all()
            return [
                client for client in clients
                if Clients.normalize_phone(client.phone).endswith(phone_digits)
            ]

    @staticmethod
    def get_name_by_user_id(user_id):
        client = Clients.get_row_by_user_id(user_id)
        if client:
            return client.name
        return None

    @staticmethod
    def get_row_for_work_name_number(name: str, phone_ending: str):
        phone_ending = Clients.normalize_phone(phone_ending)[-4:]
        """
        Поиск пользователя по имени и последним цифрам номера телефона.
        """
        users = Clients.get_rows_for_work_name_number(name, phone_ending)
        return users[0] if users else None

    @staticmethod
    def get_rows_for_work_name_number(name: str, phone_ending: str):
        phone_ending = Clients.normalize_phone(phone_ending)[-4:]
        with Session(bind=engine) as session:
            clients = session.query(Clients).filter(Clients.name == name).all()
            return [
                client for client in clients
                if Clients.normalize_phone(client.phone).endswith(phone_ending)
            ]

    @staticmethod
    def update_row_for_work(user_id, updates):
        try:
            if "phone" in updates:
                updates = {**updates, "phone": Clients.normalize_phone(updates["phone"])}
            # Пример обновления через SQLAlchemy
            with Session(bind=engine) as session:
                session.query(Clients).filter(Clients.user_id == user_id).update(updates)
                session.commit()  # Убедитесь, что изменения фиксируются
            return True
        except Exception as e:
            print(f"Error in update_row_for_work: {e}")
            session.rollback()  # Отменяем изменения при возникновении ошибки
            return False
