from db import Posts, clients, Clients
from datetime import datetime, timedelta



# Сохранение
def save_post(chat_id: int, photo: str, price: str, description: str, quantity: int):
    """Сохранение поста в базу данных."""
    Posts.insert(chat_id=chat_id, photo=photo, price=price, description=description, quantity=quantity)

# Получение всего
def get_all_posts(chat_id: int):
    """Получение всех постов для указанного chat_id."""
    return Posts.get_row(chat_id=chat_id)

# Обновление
def update_post(post_id: int, price: str, description: str, quantity: int):
    """Обновление поста по ID."""
    result, message = Posts.update_row(post_id=post_id, price=price, description=description, quantity=quantity)
    if not result:
        raise ValueError(message)

# Херня для фото
def handle_photo(chat_id: int, photo: str, details: dict):
    """Обработка фото для поста."""
    price = details.get("price", "0")
    description = details.get("description", "No description")
    quantity = details.get("quantity", 1)
    save_post(chat_id=chat_id, photo=photo, price=price, description=description, quantity=quantity)

# Обработка описания
def handle_post_details(post_id: int, price: str, description: str, quantity: int):
    """Обработка изменения информации о посте."""
    update_post(post_id=post_id, price=price, description=description, quantity=quantity)

# Синхронизация удаленного поста с ботом
def sync_posts_with_channel(chat_id: int):
    """Синхронизация постов для канала с `chat_id`."""
    posts = get_all_posts(chat_id)
    for post in posts:
        # Пример синхронизации: просто вывод в консоль
        print(f"Синхронизация поста {post.id}: {post.description}")

#Удаление поста
def delete_post(post_id: int):
    """Удаление поста по ID."""
    result, message = Posts.delete_row(post_id)
    if not result:
        raise ValueError(message)
    return f"Post with ID {post_id} has been successfully deleted."

def calculate_user_statistics(chat_id: int):
    """Вычислить статистику пользователя за последнюю неделю и за сегодня."""
    # Получаем посты за последнюю неделю
    posts = Posts.get_posts_in_last_week(chat_id)

    # Если нет постов, возвращаем статистику с нулями
    if not posts:
        return {
            "username": None,
            "post_count_week": 0,
            "post_count_today": 0,
            "active_days": 0
        }

    # Имя пользователя
    client = Clients.get_row(user_id=chat_id)  # Ищем клиента в БД
    username = client.name if client else "Unknown"

    # Подсчёт постов за неделю
    post_count_week = len(posts)

    # Подсчёт уникальных дней, на которые приходятся посты
    unique_days = {post.created_at.date() for post in posts}
    active_days = len(unique_days)

    # Подсчёт постов за сегодня
    today = datetime.utcnow().date()
    post_count_today = sum(1 for post in posts if post.created_at.date() == today)

    return {
        "username": username,
        "post_count_week": post_count_week,
        "post_count_today": post_count_today,
        "active_days": active_days,
    }
