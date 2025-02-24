from db import Posts, Clients
from datetime import datetime, timedelta



# Сохранение поста
def save_post(chat_id: int, photo: str, price: str, description: str, quantity: int):
    """Сохранение поста в базу данных."""
    Posts.insert(chat_id=chat_id, photo=photo, price=price, description=description, quantity=quantity)

# Получение всех постов
def get_all_posts(chat_id: int):
    return Posts.get_row(chat_id=chat_id)

# Обновление постов
def update_post(post_id: int, price: str, description: str, quantity: int):
    result, message = Posts.update_row(post_id=post_id, price=price, description=description, quantity=quantity)
    if not result:
        raise ValueError(message)

# Херня для фото
def handle_photo(chat_id: int, photo: str, details: dict):
    price = details.get("price", "0")
    description = details.get("description", "No description")
    quantity = details.get("quantity", 1)
    save_post(chat_id=chat_id, photo=photo, price=price, description=description, quantity=quantity)

# Обработка описания
def handle_post_details(post_id: int, price: str, description: str, quantity: int):
    update_post(post_id=post_id, price=price, description=description, quantity=quantity)

# Синхронизация удаленного поста с ботом
def sync_posts_with_channel(chat_id: int):
    posts = get_all_posts(chat_id)
    for post in posts:
        print(f"Синхронизация поста {post.id}: {post.description}")

#Удаление поста
def delete_post(post_id: int):
    result, message = Posts.delete_row(post_id)
    if not result:
        raise ValueError(message)
    return f"Post with ID {post_id} has been successfully deleted."

# Для статистики
def calculate_user_statistics(chat_id: int):
    posts = Posts.get_posts_in_last_week(chat_id)

    if not posts:
        return {
            "username": None,
            "post_count_week": 0,
            "post_count_today": 0,
            "active_days": 0
        }

    # Имя
    client = Clients.get_row(user_id=chat_id)  # Ищем клиента в БД
    username = client.name if client else "Unknown"

    # За неделю
    post_count_week = len(posts)

    # Подсчёт
    unique_days = {post.created_at.date() for post in posts}
    active_days = len(unique_days)

    # За сегодня
    today = datetime.utcnow().date()
    post_count_today = sum(1 for post in posts if post.created_at.date() == today)

    return {
        "username": username,
        "post_count_week": post_count_week,
        "post_count_today": post_count_today,
        "active_days": active_days,
    }
