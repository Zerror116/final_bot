from db.clients import Clients
from db import engine, Session, Posts

# Сохранение клиента в базе
def save_client(name: str, phone: str, user_id: int, role: str = "client") -> str:
    """Сохраняет клиента в базе данных без дублирования номера."""
    try:
        with Session(bind=engine) as session:
            # Сначала проверим существование номера телефона
            existing_user_by_phone = session.query(Clients).filter(Clients.phone == phone).first()
            if existing_user_by_phone:
                return f"Ошибка: Номер телефона {phone} уже привязан к другому аккаунту."

            # Далее проверяем user_id
            existing_user = session.query(Clients).filter(Clients.user_id == user_id).first()
            if existing_user:
                existing_user.name = name
                existing_user.phone = phone
                existing_user.role = role
            else:
                # Добавляем нового клиента
                new_client = Clients(user_id=user_id, name=name, phone=phone, role=role)
                session.add(new_client)

            session.commit()
            return "Клиент успешно добавлен или обновлен!"
    except Exception as e:
        return f"Ошибка при сохранении клиента: {e}"

# Установка роли клиента
def set_client_role(user_id: int, role: str = "client"):
    """Назначение клиенту роли."""
    try:
        with Session(bind=engine) as session:
            client = session.query(Clients).filter(Clients.user_id == user_id).first()
            if not client:
                print("Клиент не найден.")
                return
            client.role = role
            session.add(client)
            session.commit()
            print(f"Роль '{role}' успешно назначена клиенту с ID {user_id}.")
    except Exception as e:
        print(f"Ошибка при назначении роли: {e}")

# Назначение роли клиенту/пользователю
def manage_client_role(user_id: int, new_role: str):
    """Изменение роли клиента."""
    if new_role not in ["client", "worker", "admin", "audit"]:
        print(f"Недопустимая роль: {new_role}")
        return

    try:
        set_client_role(user_id, new_role)
        print(f"Роль изменена на '{new_role}' для клиента с ID {user_id}.")
    except Exception as e:
        print(f"Ошибка при изменении роли: {e}")

# Удаление клиента
def delete_client(client_id: int):
    """Удаление клиента из базы данных."""
    try:
        result = Clients.delete_row(client_id)
        if not result:
            print(f"Не удалось найти клиента с ID {client_id}.")
        else:
            print(f"Клиент с ID {client_id} успешно удален.")
    except Exception as e:
        print(f"Ошибка при удалении клиента: {e}")

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
        print(f"Ошибка при получении роли клиента: {e}")
        return "error"  # Возвращаем статус ошибки в случае сбоя

# Возвращает инфу о клиенте
def get_user_data(chat_id: int):
    return {"name": f"User {chat_id}"}  # Заглушка для примера

# Получение постов пользователя
def get_user_posts(chat_id: int):
    posts = Posts.get_row(chat_id=chat_id)

    result = []
    for post in posts:
        result.append({
            "id": post.id,
            "description": post.description,
            "created_at": post.created_at,  # Предположительно это объект datetime
        })
    return result

