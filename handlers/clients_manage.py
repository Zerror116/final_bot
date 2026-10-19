import logging

from db.clients import Clients
from db import engine, Session


logger = logging.getLogger(__name__)


# Установка роли клиента
def set_client_role(user_id: int, role: str = "client"):
    """Назначение клиенту роли."""
    try:
        with Session(bind=engine) as session:
            client = session.query(Clients).filter(Clients.user_id == user_id).first()
            if not client:
                logger.debug("Client not found for role update user_id=%s", user_id)
                return
            client.role = role
            session.add(client)
            session.commit()
            logger.info("Client role updated user_id=%s role=%s", user_id, role)
    except Exception as e:
        logger.exception("Client role update failed user_id=%s role=%s: %s", user_id, role, e)



# Получение роли клиента
def get_client_role(user_id: int) -> str:
    """Возвращает роль клиента по его user_id."""
    try:
        with Session(bind=engine) as session:
            client = session.query(Clients).filter(Clients.user_id == user_id).first()
            if client:
                return client.role  # Возвращаем роль клиента
            return "unknown"  # По умолчанию, если клиент не найден
    except Exception as e:
        logger.exception("Client role fetch failed user_id=%s: %s", user_id, e)
        return "error"  # Возвращаем статус ошибки в случае сбоя


