import os
import re

import telebot
import sqlite3

from bot import admin_main_menu, client_main_menu, worker_main_menu, unknown_main_menu
from math import ceil
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, ReplyKeyboardMarkup, \
    KeyboardButton
from database.config import TOKEN, CHANNEL_ID, ADMIN_CHAT_ID, TARGET_GROUP_ID
from telebot.apihelper import ApiTelegramException
from database.database import init_db
from pathlib import Path

# Настройка бота
bot = telebot.TeleBot(TOKEN)
user_messages = {}
user_pages = {}
PAGE_SIZE = 5
user_last_message_id = {}
last_bot_message = {}
user_data = {}

# Инициализация базы при старте
db_path = Path("MegaBot") / "bot_database.db"
init_db()


# --- Работа с базой данных ---
def save_post(chat_id, photo, price, description, quantity):
    if not price.isdigit() or not str(quantity).isdigit():
        raise ValueError("Цена и количество должны быть числами.")

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute(
        'INSERT INTO posts (chat_id, photo, price, description, quantity, is_sent) VALUES (?, ?, ?, ?, ?, ?)',
        (chat_id, photo, price, description, quantity, 0))  # is_sent по умолчанию 0
    conn.commit()
    conn.close()

def save_reservation(user_id, post_id):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('INSERT INTO reservations (user_id, post_id) VALUES (?, ?)', (user_id, post_id))
    conn.commit()
    conn.close()

def set_user_role(user_id, role):
    """Устанавливаем или обновляем роль пользователя."""
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('REPLACE INTO clients (user_id, role) VALUES (?, ?)', (user_id, role))
    conn.commit()
    conn.close()

def get_user_role(user_id):
    """Получаем роль пользователя."""
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT role FROM clients WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

@bot.message_handler(commands=["unsent_posts"])
def list_unsent_posts(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    if role not in ["admin", "worker"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем неотправленные посты
    cursor.execute('SELECT id, price, description, quantity FROM posts WHERE is_sent = 0')
    unsent_posts = cursor.fetchall()
    conn.close()

    if unsent_posts:
        response = "📮 Неотправленные посты:\n"
        for post in unsent_posts:
            post_id, price, description, quantity = post
            response += f"ID: {post_id} | Цена: {price}₽ | Описание: {description} | Количество: {quantity}\n"
        bot.send_message(user_id, response)
    else:
        bot.send_message(user_id, "✅ Все посты отправлены.")

@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.chat.id
    role = get_user_role(user_id)  # Получаем роль пользователя
    # Формируем персонализированное приветствие
    greetings = {
        "client": "Добро пожаловать в интерфейс бота, здесь вы можете просмотреть свою корзину.",
        "worker": "Давай за работу!",
        "admin": "С возвращением, Повелитель!",
    }
    greeting = greetings.get(role, "Привет, прошу пройти регистрацию")  # Приветствие по роли с дефолтом

    # Определяем клавиатуру по роли
    if role == "admin":
        markup = admin_main_menu()  # reply-клавиатура для админа
    elif role == "client":
        markup = client_main_menu()  # reply-клавиатура для клиента
    elif role == "worker":
        markup = worker_main_menu()  # reply-клавиатура для работника
    else:
        markup = unknown_main_menu()  # На случай неизвестной роли

    # Отправка нового сообщения, так как edit_message_text не подходит для reply-клавиатуры
    try:
        if user_id in last_bot_message:  # Если ранее было отправлено сообщение
            # Удаляем старое сообщение
            bot.delete_message(chat_id=user_id, message_id=last_bot_message[user_id])
        # Отправляем новое сообщение
        sent_message = bot.send_message(user_id, greeting, reply_markup=markup)
        last_bot_message[user_id] = sent_message.message_id
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

    # Удаляем сообщение пользователя (запрос /start)
    try:
        bot.delete_message(chat_id=user_id, message_id=message.message_id)
    except Exception as e:
        print(f"Ошибка при удалении сообщения пользователя: {e}")


@bot.message_handler(func=lambda message: message.text == "Регистрация")
def registration_button(message):
    chat_id = message.chat.id
    if is_user_blacklisted(chat_id):
        # Сообщение для заблокированного пользователя
        text = "К сожалению, вы заблокированы и не можете зарегистрироваться."
        try:
            if chat_id in last_bot_message:
                # Удаляем старое сообщение
                bot.delete_message(chat_id=chat_id, message_id=last_bot_message[chat_id])
            # Отправляем новое сообщение
            sent_message = bot.send_message(chat_id, text, reply_markup=None)
            last_bot_message[chat_id] = sent_message.message_id
        except Exception as e:
            print(f"Ошибка отправки сообщения: {e}")
        return

    # Подготавливаем сообщения и клавиатуру
    if is_user_registered(chat_id):
        text = "Вы уже зарегистрированы! Добро пожаловать обратно!"
        markup = None  # Если клавиатура не нужна
    else:
        user_data[chat_id] = {}
        text = "Добро пожаловать! Это разовая регистрация! Как вас зовут?"
        markup = None  # Здесь можно использовать клавиатуру, если потребуется

    try:
        if chat_id in last_bot_message:
            # Удаляем старое сообщение
            bot.delete_message(chat_id=chat_id, message_id=last_bot_message[chat_id])
        # Отправляем новое сообщение
        sent_message = bot.send_message(chat_id, text, reply_markup=markup)
        last_bot_message[chat_id] = sent_message.message_id
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

    try:
        bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Ошибка удаления сообщения пользователя: {e}")


@bot.message_handler(func=lambda message: message.chat.id in user_data and 'name' not in user_data[message.chat.id])
def handle_name(message):
    chat_id = message.chat.id
    user_data[chat_id]['name'] = message.text
    text = "Пожалуйста, введите ваш номер телефона."
    markup = None  # Замените на нужную клавиатуру, если потребуется
    try:
        if chat_id in last_bot_message:
            # Удаляем старое сообщение
            bot.delete_message(chat_id=chat_id, message_id=last_bot_message[chat_id])
        # Отправляем новое сообщение
        sent_message = bot.send_message(chat_id, text, reply_markup=markup)
        last_bot_message[chat_id] = sent_message.message_id
    except Exception as e:
        print(f"Ошибка отправки сообщения: {e}")

    try:
        bot.delete_message(chat_id=chat_id, message_id=message.message_id)
    except Exception as e:
        print(f"Ошибка удаления сообщения пользователя: {e}")


@bot.message_handler(func=lambda message: message.chat.id in user_data and 'phone' not in user_data[message.chat.id])
def handle_phone(message):
    chat_id = message.chat.id
    raw_phone = message.text
    cleaned_phone = re.sub(r'\D', '', raw_phone)  # Очищаем номер от символов, кроме цифр

    # Проверка на корректность номера телефона
    if len(cleaned_phone) == 11 and (cleaned_phone.startswith('8') or cleaned_phone.startswith('7')):
        if cleaned_phone.startswith('7'):
            cleaned_phone = '8' + cleaned_phone[1:]
        user_data[chat_id]['phone'] = cleaned_phone
        name = user_data[chat_id]['name']
        confirmation_text = f"Спасибо, {name}! Ваш номер телефона {cleaned_phone} сохранён."
        text = confirmation_text
    else:
        text = "Номер телефона должен содержать 11 цифр и начинаться с 8 или +7. Попробуйте снова."



    # Переходим к следующему шагу (если номер корректен)
    if 'phone' in user_data[chat_id]:
        save_user(name, cleaned_phone, chat_id)
        handle_start(message)


# Проверка на черный список
def is_user_blacklisted(user_id):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM black_list WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user is not None


# Проверка регистрации пользователя
def is_user_registered(user_id):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM clients WHERE user_id = ?", (user_id,))
    user = cursor.fetchone()
    conn.close()
    return user is not None


# Функция для сохранения данных в базу с user_id
def save_user(name, phone, user_id):
    """Сохранение пользователя с проверкой на роль администратора."""
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    # Определяем роль: "admin" для определённого user_id, иначе "client"
    role = "admin" if user_id == 5411051275 else "client"  # Подставьте ваш user_id администратора
    # Вставляем данные с присвоением роли
    cursor.execute("INSERT INTO clients (name, phone, user_id, role) VALUES (?, ?, ?, ?)", (name, phone, user_id, role))
    conn.commit()
    conn.close()
    return handle_start()

# Функция для сохранения данных в базу с user_id
def save_user(name, phone, user_id):
    """Сохранение пользователя с проверкой на роль администратора."""
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()


    role = "admin" if user_id == 5411051275 else "client"  # Подставьте ваш user_id администратора

    # Вставляем данные с присвоением роли
    cursor.execute("INSERT INTO clients (name, phone, user_id, role) VALUES (?, ?, ?, ?)", (name, phone, user_id, role))
    conn.commit()
    conn.close()
    
@bot.message_handler(commands=["setrole"])
def set_role_command(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    if role != "admin":
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    try:
        # Ожидаем ввод в формате "/setrole user_id role"
        _, target_user_id, role = message.text.split()
        target_user_id = int(target_user_id)

        if role not in ["client", "worker", "admin"]:
            bot.send_message(user_id, "Некорректная роль. Используйте client, worker или admin.")
            return

        # Устанавливаем роль пользователю
        set_user_role(target_user_id, role)
        bot.send_message(user_id, f"Роль {role} успешно установлена для пользователя {target_user_id}.")
    except ValueError:
        bot.send_message(user_id, "Неверный формат команды. Используйте: /setrole user_id role")
    except Exception as e:
        bot.send_message(user_id, f"Произошла ошибка: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("reserve_"))
def handle_reservation(call):
    post_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    if not is_registered(user_id):
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="Вы не зарегистрированы! Для регистрации перейдите В бота",
            show_alert=True
        )
    else:
        conn = sqlite3.connect('main/bot_database.db')
        cursor = conn.cursor()

        # Проверяем доступное количество товара, а также получаем данные
        cursor.execute('SELECT quantity, message_id, price, description FROM posts WHERE id = ?', (post_id,))
        result = cursor.fetchone()

        if not result:
            bot.send_message(user_id, "Ошибка: Товар не найден.")
            return

        current_quantity, message_id, price, description = result  # Распаковываем результат

        if current_quantity == 0:
            bot.answer_callback_query(
                callback_query_id=call.id,
                text="Этот товар уже забронирован полностью!",
                show_alert=True
            )
        else:
            # Сохраняем бронирование
            cursor.execute('INSERT INTO reservations (user_id, post_id) VALUES (?, ?)', (user_id, post_id))

            # Уменьшаем количество товара на 1
            new_quantity = current_quantity - 1
            cursor.execute('UPDATE posts SET quantity = ? WHERE id = ?', (new_quantity, post_id))
            conn.commit()
            conn.close()

            # Формируем обновлённое описание сообщения
            new_caption = f"Цена: {price}\nОписание: {description}\nОстаток: {new_quantity}"

            # Обновляем сообщение в канале (только текст, кнопка остаётся)
            try:
                bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=message_id,
                    caption=new_caption,
                    reply_markup=call.message.reply_markup  # Кнопка остаётся неизменной
                )
            except Exception as e:
                bot.send_message(user_id, f"Не удалось обновить сообщение в канале. Ошибка: {e}")

            # Информируем бронирующего, если он забронировал последний экземпляр
            if new_quantity == 0:
                bot.answer_callback_query(
                    callback_query_id=call.id,
                    text="Вы забронировали последний экземпляр товара!",
                    show_alert=True
                )


def delete_previous_messages(user_id, user_request_id=None):
    """
    Удаляет предыдущие сообщения, отправленные ботом пользователю,
    а также сообщение пользователя, если задан его ID.
    """
    # Удаляем запрос пользователя (если передан)
    if user_request_id:
        try:
            bot.delete_message(chat_id=user_id, message_id=user_request_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщения пользователя: {e}")

    # Удаляем все сообщения, отправленные ботом
    if user_id in user_messages:
        try:
            for msg_id in user_messages[user_id]:
                bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception as e:
            print(f"Ошибка при удалении сообщений бота: {e}")
        finally:
            user_messages[user_id] = []  # Очистка сохраненных сообщений

def get_user_reservations(user_id):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Добавляем is_fulfilled для каждого заказа
    cursor.execute('''
        SELECT posts.description, posts.price, posts.photo, COUNT(reservations.post_id) AS quantity, reservations.is_fulfilled
        FROM reservations
        JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = ?
        GROUP BY posts.description, posts.price, posts.photo, reservations.is_fulfilled
    ''', (user_id,))
    results = cursor.fetchall()
    conn.close()
    return results

@bot.message_handler(commands=["my_reservations"])
def show_reservations(message):
    user_id = message.chat.id

    # Проверка регистрации пользователя
    if not is_registered(user_id):
        msg = bot.send_message(user_id, "Вы не зарегистрированы! Для регистрации используйте команду /start register.")
        user_messages[user_id] = [msg.message_id]
        return

    # Получаем заказы пользователя
    reservations = get_user_reservations(user_id)

    if reservations:
        for idx, (description, price, photo, quantity, is_fulfilled) in enumerate(reservations, start=1):
            status = "✅ Положено" if is_fulfilled else "⏳ Ожидает выполнения"

            # Формируем описание
            caption = (
                f"{idx}. Описание: {description}\n"
                f"💰 Цена: {price}₽ x {quantity}\n"
                f"Статус: {status}"
            )

            if photo:
                try:
                    sent_photo = bot.send_photo(user_id, photo=photo, caption=caption)
                    user_messages.setdefault(user_id, []).append(sent_photo.message_id)
                except Exception as e:
                    bot.send_message(user_id, f"Ошибка при показе фотографии: {e}")  # Показываем ошибку


@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_post_"))
def handle_delete_post(call):
    """
    Обрабатывает нажатие на кнопку 'Удалить' для удаления поста по ID.
    После удаления поста удаляет также это сообщение в чате.
    """
    try:
        post_id = int(call.data.split("_")[2])  # Получаем ID поста из callback_data

        # Подключение к базе данных
        conn = sqlite3.connect('main/bot_database.db')
        cursor = conn.cursor()

        # Удаляем пост из базы данных
        cursor.execute("DELETE FROM posts WHERE id = ?", (post_id,))
        if cursor.rowcount > 0:  # Проверяем, был ли удалён пост
            conn.commit()

            # Удаляем сообщение о посте
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
            bot.answer_callback_query(call.id, "Пост успешно удалён.", show_alert=False)
        else:
            bot.answer_callback_query(call.id, "Пост не найден.", show_alert=True)

        conn.close()

    except Exception as e:
        print(f"Ошибка при удалении поста: {e}")
        bot.answer_callback_query(call.id, f"Произошла ошибка: {e}", show_alert=True)

# Хэндлер для обработки нажатий на заказ
@bot.callback_query_handler(func=lambda call: call.data.startswith("order_"))
def order_details(call):
    reservation_id = call.data.split("_")[1]
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    # Получаем информацию о заказе
    cursor.execute("""
        SELECT posts.photo, posts.description, posts.price, posts.quantity, reservations.is_fulfilled
        FROM reservations
        INNER JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.id = ?
    """, (reservation_id,))
    order = cursor.fetchone()
    conn.close()
    if order:
        photo, description, price, quantity, is_fulfilled = order
        status = "✔️ Обработан" if is_fulfilled else "⌛ В обработке"
        caption = (
            f"Товар: {description}\n"
            f"Цена: {price}\n"
            f"Статус: {status}"
        )
        # Кнопки для деталей заказа
        markup = InlineKeyboardMarkup()
        back_btn = InlineKeyboardButton("⬅️ Назад", callback_data="my_orders")  # callback_data для перехода
        markup.add(back_btn)
        if not is_fulfilled:
            cancel_btn = InlineKeyboardButton("❌ Отказаться", callback_data=f"cancel_{reservation_id}")
            markup.add(cancel_btn)
        # Обновляем сообщение с медиа для деталей заказа
        bot.edit_message_media(
            media=InputMediaPhoto(media=photo, caption=caption),
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=markup
        )
@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def show_my_orders(call):
    message = call.message
    my_orders(message)  # Просто вызываем метод my_orders, передав исходное сообщение


@bot.message_handler(func=lambda message: message.text == "🛒 Мои заказы")
def my_orders(message):
    keyboard = InlineKeyboardMarkup(row_width=1)
    user_id = message.chat.id
    name = message.from_user.first_name
    message_id = message.message_id  # ID сообщения, которое отправил пользователь
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем информацию о заказах пользователя
    cursor.execute("""
        SELECT reservations.id, posts.description, reservations.is_fulfilled, posts.price
        FROM reservations
        INNER JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = ?
    """, (user_id,))
    orders = cursor.fetchall()
    conn.close()

    # Удаляем поступившее сообщение пользователя
    try:
        bot.delete_message(chat_id=user_id, message_id=message_id)
    except Exception as e:
        print(f"Ошибка удаления сообщения пользователя: {e}. user_id: {user_id}, message_id: {message_id}")

    # Удаляем предыдущее сообщение бота, если оно существует
    try:
        if user_id in user_last_message_id:
            bot.delete_message(chat_id=user_id, message_id=user_last_message_id[user_id])
    except Exception as e:
        print(
            f"Попытка удалить предыдущее сообщение, строка ошибки -533-: {e}. Id телеграмма: {user_id}, Имя {name}")

    # Ответ в зависимости от наличия заказов
    if orders:
        # Сохраняем текущую страницу для пользователя
        user_pages[user_id] = 0
        # Отправляем страницу с заказами
        new_message = send_order_page(
            user_id,
            message_id=None,
            orders=orders,
            page=user_pages[user_id]
        )
        user_last_message_id[user_id] = new_message.message_id  # Сохраняем ID нового сообщения
    else:
        # Отправляем уведомление о том, что заказов нет
        to_channel_button = InlineKeyboardButton(text="На канал", url="https://t.me/mgskidkitest")
        keyboard.add(to_channel_button)
        new_message = bot.send_message(message.chat.id,
                                       "У вас нет активных заказов, начать покупки вы можете перейдя На канал",
                                       reply_markup=keyboard)
        # Сохраняем ID нового сообщения
        user_last_message_id[user_id] = new_message.message_id


def send_order_page(user_id, message_id, orders, page):
    """
    Функция для отправки или изменения страницы заказов пользователю.
    """
    total_pages = ceil(len(orders) / PAGE_SIZE)
    start_idx = page * PAGE_SIZE
    end_idx = start_idx + PAGE_SIZE

    # Подсчет общей суммы заказов
    try:
        total_sum = sum(int(order[3]) for order in orders if order[3] is not None)
    except ValueError:
        total_sum = 0
        print("Ошибка: Некорректные данные для суммы.")

    # Формируем кнопки заказов
    orders_markup = InlineKeyboardMarkup(row_width=1)
    for order in orders[start_idx:end_idx]:
        reservation_id, description, is_fulfilled, price = order
        status = "✔️ Обработан" if is_fulfilled else "⌛ В обработке"
        details_button = InlineKeyboardButton(
            text=f"{price} ₽ - {description} ({status})",
            callback_data=f"order_{reservation_id}"
        )
        orders_markup.add(details_button)

    # Добавляем кнопки навигации
    navigation_buttons = []
    if page > 0:
        navigation_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data="prev_page"))
    if page < total_pages - 1:
        navigation_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data="next_page"))
    if navigation_buttons:
        orders_markup.row(*navigation_buttons)

    # Общая сумма
    orders_markup.add(
        InlineKeyboardButton(f"🧾 Общая сумма: {total_sum} ₽.", callback_data="total_sum")
    )

    photo_placeholder = open("images/my_cart.jpg", "rb")  # Путь к фото

    if message_id:
        # Изменяем текущее сообщение
        return bot.edit_message_media(
            chat_id=user_id,
            message_id=message_id,
            media=InputMediaPhoto(
                photo_placeholder,
                caption=f"Ваши заказы (стр. {page + 1} из {total_pages}):"
            ),
            reply_markup=orders_markup
        )
    else:
        # Отправляем новое сообщение
        msg = bot.send_photo(
            chat_id=user_id,
            photo=photo_placeholder,
            caption=f"Ваши заказы (стр. {page + 1} из {total_pages}):",
            reply_markup=orders_markup
        )
        return msg

@bot.callback_query_handler(func=lambda call: call.data in ["next_page","prev_page"])
def paginate_orders(call):
    user_id = call.message.chat.id
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute("""
        SELECT reservations.id, posts.description, reservations.is_fulfilled, posts.price
        FROM reservations
        INNER JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = ?
    """, (user_id,))
    orders = cursor.fetchall()
    conn.close()

    if not isinstance(orders, list):
        orders = []  # Устанавливаем пустой список в случае ошибки
        print("Ошибка: 'orders' не является списком. Используется пустой список.")


    if user_id not in user_pages:
        user_pages[user_id] = 0


    if call.data == "next_page" and user_pages[user_id] < ceil(len(orders) / PAGE_SIZE) - 1:
        user_pages[user_id] += 1
    elif call.data == "prev_page" and user_pages[user_id] > 0:
        user_pages[user_id] -= 1


    send_order_page(
        user_id=user_id,
        message_id=call.message.message_id,
        orders=orders,
        page=user_pages[user_id]
    )

@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def cancel_reservation(call):
    try:
        user_id = call.from_user.id
        reservation_id = int(call.data.split("_")[1])
        conn = sqlite3.connect('main/bot_database.db')
        cursor = conn.cursor()
        cursor.execute("""
            SELECT reservations.post_id, posts.quantity, reservations.is_fulfilled
            FROM reservations
            JOIN posts ON reservations.post_id = posts.id
            WHERE reservations.id = ? AND reservations.user_id = ?
        """, (reservation_id, user_id))
        reservation = cursor.fetchone()
        if not reservation:
            bot.answer_callback_query(call.id, "Резерв не найден или не принадлежит вам.", show_alert=True)
            conn.close()
            return
        post_id, current_quantity, is_fulfilled = reservation
        if is_fulfilled:
            bot.answer_callback_query(call.id, "Невозможно отказаться от уже обработанного заказа.", show_alert=True)
            conn.close()
            return
        cursor.execute("DELETE FROM reservations WHERE id = ?", (reservation_id,))
        conn.commit()
        new_quantity = current_quantity + 1
        cursor.execute("UPDATE posts SET quantity = ? WHERE id = ?", (new_quantity, post_id))
        conn.commit()

        cursor.execute("SELECT message_id, description, price FROM posts WHERE id = ?", (post_id,))
        post_info = cursor.fetchone()
        conn.close()

        if post_info:
            message_id, description, price = post_info
            caption = f"Товар: {description}\nЦена: {price}\nКоличество: {new_quantity}"
            markup = InlineKeyboardMarkup()
            reserve_button = InlineKeyboardButton("🛒 Забронировать", callback_data=f"reserve_{post_id}")
            to_bot_button = InlineKeyboardButton("В бота", url="https://t.me/MegaSkidkiTgBot?start=start")
            markup.add(reserve_button, to_bot_button)
            bot.edit_message_caption(
                chat_id=CHANNEL_ID,
                message_id=message_id,
                caption=caption,
                reply_markup=markup
            )

        # Сообщаем об успешной отмене бронирования
        bot.answer_callback_query(call.id, "Вы успешно отказались от товара.", show_alert=False)

        # Переход в меню "Мои заказы" с обновлённым списком
        my_orders(call.message)

    except Exception as e:
        bot.answer_callback_query(call.id, f"Произошла ошибка: {e}", show_alert=True)



@bot.callback_query_handler(func=lambda call: call.data.startswith("orders_page_"))
def change_orders_page(call):
    try:
        page = int(call.data.split("_")[2])
        update_user_orders_menu(call.message.chat.id, call.message.message_id, page=page)
        bot.answer_callback_query(call.id)
    except Exception as ex:
        print(f"Ошибка при смене страницы заказов: {ex}")


def update_user_orders_menu(chat_id, message_id, page=0):
    """
    Функция для обновления раздела "Ваши заказы" с поддержкой страниц
    """
    try:
        conn = sqlite3.connect('main/bot_database.db')
        cursor = conn.cursor()
        # Получаем все заказы пользователя
        cursor.execute("""
            SELECT reservations.id, posts.description, reservations.is_fulfilled
            FROM reservations
            INNER JOIN posts ON reservations.post_id = posts.id
            WHERE reservations.user_id = ?
        """, (chat_id,))
        orders = cursor.fetchall()
        conn.close()

        total_orders = len(orders)
        start_index = page * PAGE_SIZE
        end_index = start_index + PAGE_SIZE
        current_orders = orders[start_index:end_index]
        total_pages = ceil(len(orders) / PAGE_SIZE)

        # Создаем интерфейс для текущей страницы
        orders_markup = InlineKeyboardMarkup(row_width=1)
        for order in current_orders:
            reservation_id, description, is_fulfilled = order
            status = "✔️ Обработан" if is_fulfilled else "⌛ В обработке"
            details_button = InlineKeyboardButton(
                text=f"{description} ({status})",
                callback_data=f"order_{reservation_id}"
            )
            orders_markup.add(details_button)

        # Добавляем кнопки для навигации между страницами
        navigation_buttons = []
        if page > 0:
            navigation_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"orders_page_{page - 1}"))
        if end_index < total_orders:
            navigation_buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"orders_page_{page + 1}"))

        if navigation_buttons:
            orders_markup.row(*navigation_buttons)

        # Заглушка с фото для обновления сообщения
        if current_orders:
            photo_placeholder = open("images/my_cart.jpg", "rb")  # Смените путь к вашей картинке
            bot.edit_message_media(
                media=InputMediaPhoto(media=photo_placeholder, caption=f"Ваши заказы (стр. {page + 1} из {total_pages}):"),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=orders_markup
            )
            photo_placeholder.close()
        else:
            return my_orders
    except Exception as ex:
        print(f"Ошибка при обновлении меню заказов: {ex}")


@bot.message_handler(func=lambda message: message.text == "🔄 Переслать забронированные посты")
def forward_reserved_posts(message):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем забронированные посты
    cursor.execute('''
           SELECT posts.photo, posts.price, posts.description, clients.name
           FROM reservations
           JOIN posts ON reservations.post_id = posts.id
           JOIN clients ON reservations.user_id = clients.user_id
       ''')
    reserved_posts = cursor.fetchall()
    conn.close()

    if reserved_posts:
        for post in reserved_posts:
            photo, price, description, client_name = post
            caption = f"Цена: {price}₽\nОписание: {description}\nЗабронировано: {client_name}"

            # Отправляем пост в указанную группу
            bot.send_photo(TARGET_GROUP_ID, photo=photo, caption=caption)
        bot.send_message(message.chat.id, "Все забронированные посты успешно отправлены в группу.")
    else:
        bot.send_message(message.chat.id, "Нет забронированных постов для отправки.")

@bot.message_handler(func=lambda message: message.text == "📦 Заказы клиентов")
def send_all_reserved_to_group(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    # Проверка ролей
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    try:
        # Получение всех забронированных товаров из базы данных
        cursor.execute('''
            SELECT reservations.id, posts.photo, posts.price, posts.description, clients.name, clients.phone
            FROM reservations
            JOIN posts ON reservations.post_id = posts.id
            JOIN clients ON reservations.user_id = clients.user_id
            WHERE reservations.is_fulfilled = 0
        ''')
        reserved_items = cursor.fetchall()

        if reserved_items:
            for item in reserved_items:
                reservation_id, photo, price, description, client_name, client_phone = item
                caption = f"💼 Новый заказ:\n\n👤 Клиент: {client_name}\n📞 Телефон: {client_phone}\n💰 Цена: {price}₽\n📦 Описание: {description}"

                # Проверка на наличие фото и отправка сообщения
                markup = InlineKeyboardMarkup()
                mark_button = InlineKeyboardButton(
                    text="✅ Положил",
                    callback_data=f"delete_msg_{reservation_id}"  # Передаём ID резервации в callback_data
                )
                markup.add(mark_button)

                if photo:
                    bot.send_photo(chat_id=TARGET_GROUP_ID, photo=photo, caption=caption, reply_markup=markup)
                else:
                    bot.send_message(chat_id=TARGET_GROUP_ID, text=caption, reply_markup=markup)

            bot.send_message(user_id, "Все забронированные товары успешно отправлены в группу.")
        else:
            bot.send_message(user_id, "Нет забронированных товаров для отправки.")
    finally:
        conn.close()

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_msg_"))
def delete_message_from_group(call):
    # Проверка роли администратора
    user_id = call.from_user.id
    role = get_user_role(user_id)

    if role != "admin":
        bot.answer_callback_query(call.id, "У вас нет прав доступа к этой функции.", show_alert=True)
        return

    # Получаем ID брони
    reservation_id = int(call.data.split("_")[2])

    try:
        # Обновляем статус выполнения заказа
        conn = sqlite3.connect('main/bot_database.db')
        cursor = conn.cursor()
        cursor.execute('UPDATE reservations SET is_fulfilled = 1 WHERE id = ?', (reservation_id,))
        conn.commit()
        conn.close()

        # Обновляем сообщение в группе
        bot.edit_message_reply_markup(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            reply_markup=None  # Удаляем кнопки после выполнения
        )
        bot.answer_callback_query(call.id, "Товар отмечен как положенный.")
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_"))
def mark_fulfilled(call):
    # Проверка администратора
    user_id = call.from_user.id
    role = get_user_role(user_id)

    if role != "admin":
        bot.answer_callback_query(call.id, "У вас нет прав доступа к этой функции.", show_alert=True)
        return

    # Получаем ID брони
    reservation_id = int(call.data.split("_")[2])

    # Обновляем статус выполнения заказа
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE reservations SET is_fulfilled = 1 WHERE id = ?', (reservation_id,))
    conn.commit()
    conn.close()

    # Уведомление об успешном выполнении
    bot.answer_callback_query(call.id, "Товар отмечен как положенный.")
    bot.send_message(call.message.chat.id, "Товар успешно отмечен как положенный!")

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_cart_"))
def view_cart(call):
    client_id = int(call.data.split("_")[2])  # Извлечение ID клиента из callback_data

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем список товаров в корзине клиента
    cursor.execute('''
        SELECT reservations.id, posts.description, posts.price, reservations.is_fulfilled
        FROM reservations
        JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = (SELECT user_id FROM clients WHERE id = ?)
    ''', (client_id,))
    cart_items = cursor.fetchall()
    conn.close()

    if cart_items:
        for reservation_id, description, price, is_fulfilled in cart_items:
            status = "✅ Положено" if is_fulfilled else "⏳ Ожидает выполнения"
            details = f"{description} - {price}₽\nСтатус: {status}"

            # Кнопка "Положено" для каждого не выполненного товара
            markup = InlineKeyboardMarkup()
            if not is_fulfilled:
                markup.add(InlineKeyboardButton("✅ Положено", callback_data=f"mark_fulfilled_{reservation_id}"))

            bot.send_message(call.message.chat.id, details, reply_markup=markup)
    else:
        bot.send_message(call.message.chat.id, "Корзина клиента пуста.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_"))
def mark_fulfilled(call):
    # Получаем ID бронирования
    reservation_id = int(call.data.split("_")[2])

    # Обновляем статус выполнения заказа в базе данных
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE reservations SET is_fulfilled = 1 WHERE id = ?', (reservation_id,))
    conn.commit()
    conn.close()

    # Ответ пользователю
    bot.answer_callback_query(call.id, "Товар отмечен как положенный.")
    bot.send_message(call.message.chat.id, "Товар успешно обновлён!")



@bot.message_handler(commands=['sync_posts'])
def sync_posts_with_channel(message):
    role = get_user_role(message.chat.id)
    user_id = message.chat.id
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT id, message_id FROM posts')
    posts = cursor.fetchall()

    deleted_posts = []
    for post_id, message_id in posts:
        try:
            # Проверяем, существует ли сообщение
            bot.forward_message(chat_id=message.chat.id, from_chat_id=CHANNEL_ID, message_id=message_id)
        except ApiTelegramException:
            # Если сообщение не найдено в канале, добавляем его в список на удаление
            deleted_posts.append(post_id)

    # Удаляем из базы данных записи о постах, которых больше нет
    for post_id in deleted_posts:
        cursor.execute('DELETE FROM posts WHERE id = ?', (post_id,))

    conn.commit()
    conn.close()

    bot.send_message(message.chat.id, f"Синхронизация завершена. Удалено записей: {len(deleted_posts)}.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled"))
def mark_fulfilled(call):
    # Получаем ID бронирования
    reservation_id = int(call.data.split("_")[2])

    # Обновляем статус выполнения заказа
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE reservations SET is_fulfilled = 1 WHERE id = ?', (reservation_id,))
    conn.commit()
    conn.close()

    bot.answer_callback_query(call.id, "Товар отмечен как положенный.")
    bot.send_message(call.message.chat.id, "Товар успешно обновлён.")

def get_user_reservations_split(user_id):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Разделяем зарезервированные и выполненные заказы
    cursor.execute('''
        SELECT posts.description, posts.price, reservations.is_fulfilled
        FROM reservations
        JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = ?
    ''', (user_id,))
    results = cursor.fetchall()
    conn.close()

    ordered = [r for r in results if not r[2]]  # is_fulfilled = 0
    fulfilled = [r for r in results if r[2]]  # is_fulfilled = 1
    return ordered, fulfilled

@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_cart_"))
def clear_cart(call):
    client_id = int(call.data.split("_")[2])

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Удаляем все заказы клиента
    cursor.execute('''
        DELETE FROM reservations
        WHERE user_id = (SELECT user_id FROM clients WHERE id = ?)
    ''', (client_id,))
    conn.commit()
    conn.close()

    bot.send_message(call.message.chat.id, "Корзина клиента успешно расформирована.")

def get_all_posts():
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    # Запрос всех постов независимо от chat_id
    cursor.execute('SELECT id, chat_id, photo, price, description, quantity FROM posts')
    results = cursor.fetchall()
    conn.close()
    return results

def update_post(post_id, price, description, quantity):
    if not price.isdigit() or not str(quantity).isdigit():
        raise ValueError("Цена и количество должны быть числами.")
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('UPDATE posts SET price = ?, description = ?, quantity = ? WHERE id = ?',
                   (price, description, quantity, post_id))
    conn.commit()
    conn.close()

def is_registered(user_id):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM clients WHERE user_id = ?', (user_id,))
    result = cursor.fetchone()
    conn.close()
    return result is not None

def register_client(user_id, name, phone):
    """Регистрация клиента с ролью 'client' по умолчанию."""
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Подготовка запроса для вставки данных с ролью 'client'
    cursor.execute('''
        INSERT OR IGNORE INTO clients (user_id, name, phone, role) 
        VALUES (?, ?, ?, ?)
    ''', (user_id, name, phone, 'client'))

    conn.commit()
    conn.close()

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.REGISTERING_PHONE)
def register_phone(message):
    user_id = message.chat.id
    phone = message.text
    name = temp_user_data[user_id]["name"]

    register_client(user_id, name, phone)  # Передаём данные клиента
    bot.send_message(user_id, "Регистрация завершена! Теперь вы можете использовать бота.")
    bot.send_message(ADMIN_CHAT_ID, f"Пользователь {name}, телефон {phone}, успешно зарегистрировался.")
    clear_user_state(user_id)

# --- Управление состояниями пользователей ---
user_states = {}
temp_user_data = {}
temp_post_data = {}

class UserState:
    REGISTERING_NAME = 1
    REGISTERING_PHONE = 2
    CREATING_POST = 3
    EDITING_POST = 4

def set_user_state(user_id, state):
    user_states[user_id] = state

def get_user_state(user_id):
    return user_states.get(user_id, None)

def clear_user_state(user_id):
    user_states.pop(user_id, None)
    temp_user_data.pop(user_id, None)
    temp_post_data.pop(user_id, None)

# --- Команды бота ---

@bot.message_handler(func=lambda message: message.text == "⚙️ Клиенты")
def manage_clients(message):
    user_id = message.chat.id
    role = get_user_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    # Создаем клавиатуру с кнопками
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Удалить клиента по номеру телефона", "Просмотреть корзину", "⬅️ Назад")
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)


@bot.message_handler(func=lambda message: message.text == "Удалить клиента по номеру телефона")
def delete_client_by_phone(message):
    user_id = message.chat.id
    role = get_user_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return
    bot.send_message(message.chat.id, "Введите номер телефона клиента для удаления:")
    set_user_state(message.chat.id, "DELETE_CLIENT_PHONE")

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "DELETE_CLIENT_PHONE")
def process_delete_client_phone(message):
    user_id = message.chat.id
    role = get_user_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return
    phone = message.text
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    protected_user_id = 5411051275

    try:
        # Получаем user_id клиента из таблицы clients
        cursor.execute("SELECT user_id FROM clients WHERE phone = ?", (phone,))
        user_data = cursor.fetchone()
        if user_data:
            user_id = user_data[0]  # Извлекаем user_id

            # Проверяем, можно ли добавлять этого клиента в черный список
            if user_id == protected_user_id:
                bot.send_message(
                    message.chat.id,
                    f"Клиент с номером телефона {phone} не хочет быть заблокированным"
                )
                return

            cursor.execute("INSERT INTO black_list (user_id, phone) VALUES (?, ?)", (user_id, phone))
            # Удаляем связанные данные из reservations
            cursor.execute("DELETE FROM reservations WHERE user_id = ?", (user_id,))
            deleted_reservations = cursor.rowcount
            # Удаляем клиента из clients
            cursor.execute("DELETE FROM clients WHERE phone = ?", (phone,))
            deleted_clients = cursor.rowcount
            conn.commit()
            bot.send_message(
                message.chat.id,
                f"Клиент с номером телефона {phone} успешно удалён. "
                f"Связанных записей в таблице reservations удалено: {deleted_reservations}."
            )
        else:
            bot.send_message(
                message.chat.id,
                f"Клиент с номером телефона {phone} не найден."
            )
    except sqlite3.Error as e:
        conn.rollback()
        bot.send_message(
            message.chat.id,
            f"Произошла ошибка при удалении данных: {e}"
        )
    finally:
        conn.close()
    clear_user_state(message.chat.id)


@bot.message_handler(func=lambda message: message.text == "Просмотреть корзину")
def search_client_by_phone(message):
    user_id = message.chat.id
    role = get_user_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return
    bot.send_message(message.chat.id, "Введите последние 4 цифры номера телефона клиента для поиска:")
    set_user_state(message.chat.id, "SEARCH_CLIENT")


@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "SEARCH_CLIENT")
def handle_client_search(message):
    search_query = message.text.strip()

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    if search_query.lower() == "все":  # Если пользователь ввел "Все" (регистр не важен)
        # Получаем всех клиентов
        cursor.execute('''
            SELECT id, name, phone FROM clients
        ''')
        clients = cursor.fetchall()
        conn.close()

        if clients:
            for client_id, name, phone in clients:
                # Вычисляем суммы заказов для каждого клиента
                total_sum, fulfilled_sum = calculate_sums_for_client(client_id)
                info = (
                    f"👤 Имя: {name}\n"
                    f"📞 Телефон: {phone}\n"
                    f"🧾 Общая сумма: {total_sum} ₽\n"
                    f"🏷️ Выполнено товаров на сумму: {fulfilled_sum}₽"
                )
                markup = InlineKeyboardMarkup()
                view_cart_button = InlineKeyboardButton("🛒 Посмотреть корзину", callback_data=f"view_cart_{client_id}")
                disbandment_button = InlineKeyboardButton("Расформировать", callback_data=f"clear_cart_{client_id}")
                markup.add(view_cart_button,disbandment_button)
                bot.send_message(message.chat.id, info, reply_markup=markup)
        else:
            bot.send_message(message.chat.id, "Клиенты не найдены.")
    else:
        # Если введено не "Все", ищем по последним 4 цифрам номера
        if not search_query.isdigit() or len(search_query) != 4:
            bot.send_message(message.chat.id, "Введите ровно 4 цифры или слово 'Все'.")
            return

        cursor.execute('''
            SELECT id, name, phone FROM clients WHERE phone LIKE ?
        ''', (f"%{search_query}",))
        clients = cursor.fetchall()
        conn.close()

        if clients:
            for client_id, name, phone in clients:
                # Вычисляем суммы заказов для каждого клиента
                total_sum, fulfilled_sum = calculate_sums_for_client(client_id)
                info = (
                    f"👤 Имя: {name}\n"
                    f"📞 Телефон: {phone}\n"
                    f"💵 Общая сумма заказов: {total_sum}₽\n"
                    f"🏷️ Выполнено товаров на сумму: {fulfilled_sum}₽"
                )
                markup = InlineKeyboardMarkup()
                view_cart_button = InlineKeyboardButton("🛒 Посмотреть корзину", callback_data=f"view_cart_{client_id}")
                disbandment_button = InlineKeyboardButton("Расформировать", callback_data=f"clear_cart_{client_id}")
                markup.add(view_cart_button,disbandment_button)
                bot.send_message(message.chat.id, info, reply_markup=markup)
        else:
            bot.send_message(message.chat.id, "Клиенты с таким номером не найдены.")

    clear_user_state(message.chat.id)

def calculate_sums_for_client(client_id):
    """
    Рассчитывает общую сумму заказов клиента и сумму выполненных заказов.
    """
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем user_id клиента
    cursor.execute('SELECT user_id FROM clients WHERE id = ?', (client_id,))
    user_id = cursor.fetchone()

    if not user_id:  # Если клиента нет, возвращаем нули
        return 0, 0

    user_id = user_id[0]  # Извлечь id

    # Рассчитываем общую сумму и сумму выполненных товаров
    cursor.execute('''
        SELECT SUM(posts.price), 
               SUM(CASE WHEN reservations.is_fulfilled = 1 THEN posts.price ELSE 0 END)
        FROM reservations
        JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = ?
    ''', (user_id,))
    result = cursor.fetchone()
    conn.close()

    total_sum = result[0] or 0  # Общая сумма всех товаров
    fulfilled_sum = result[1] or 0  # Сумма выполненных товаров
    return total_sum, fulfilled_sum

@bot.callback_query_handler(func=lambda call: call.data.startswith("view_cart_"))
def view_cart(call):
    client_id = int(call.data.split("_")[2])  # Извлечение ID клиента из callback_data

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем содержимое корзины клиента
    cursor.execute('''
        SELECT posts.description, posts.price, reservations.is_fulfilled
        FROM reservations
        JOIN posts ON reservations.post_id = posts.id
        WHERE reservations.user_id = (SELECT user_id FROM clients WHERE id = ?)
    ''', (client_id,))
    cart_items = cursor.fetchall()
    conn.close()

    if cart_items:
        total_sum = 0  # Инициализация общей суммы
        response = f"🛒 Корзина клиента (ID: {client_id}):\n\n"
        for idx, (description, price, is_fulfilled) in enumerate(cart_items, start=1):
            status = "✅ Положено" if is_fulfilled else "⏳ Ожидает выполнения"
            item_total = float(price)  # Сумма для конкретного товара
            response += f"{idx}. {description}\n💰 Цена: {price}₽\nСтатус: {status}\n\n"
            total_sum += item_total

        response += f"🧾 Общая сумма: {total_sum} ₽"
        bot.send_message(call.message.chat.id, response)
    else:
        bot.send_message(call.message.chat.id, "Корзина клиента пуста.")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_"))
def mark_fulfilled(call):
    reservation_id = int(call.data.split("_")[2])  # Извлечение ID брони

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Обновляем статус выполнения заказа
    cursor.execute('UPDATE reservations SET is_fulfilled = 1 WHERE id = ?', (reservation_id,))
    conn.commit()
    conn.close()

    bot.answer_callback_query(call.id, "Товар отмечен как положенный.")
    bot.send_message(call.message.chat.id, "Товар успешно обновлён!")
@bot.message_handler(commands=["manage_clients"])
def handle_manage_clients_command(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    # Проверяем, что команда доступна только администрации
    if role != "admin":
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Если пользователь администратор, вызываем функцию manage_clients
    manage_clients_v2(message)
def manage_clients_v2(message):
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, name, phone, role FROM clients ORDER BY role DESC")  # Отображать сначала админов и воркеров
    clients = cursor.fetchall()
    conn.close()

    if clients:
        for client in clients:
            client_id, name, phone, role = client
            info = f"👤 Имя: {name}\n📞 Телефон: {phone}\n🆔 Роль: {role}"

            markup = InlineKeyboardMarkup()
            if role != "worker":
                set_worker_button = InlineKeyboardButton("👷 Назначить worker", callback_data=f"set_worker_{client_id}")
                markup.add(set_worker_button)

            if role != "client":  # Добавить возможность убрать роль worker
                set_client_button = InlineKeyboardButton("🚫 Убрать роль worker",
                                                         callback_data=f"set_client_{client_id}")
                markup.add(set_client_button)

            bot.send_message(message.chat.id, info, reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "Нет клиентов для управления.")

@bot.callback_query_handler(
    func=lambda call: call.data.startswith("set_worker_") or call.data.startswith("set_client_"))
def handle_set_role(call):
    client_id = int(call.data.split("_")[2])
    new_role = "worker" if "set_worker" in call.data else "client"

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Установим новую роль для пользователя
    cursor.execute("UPDATE clients SET role = ? WHERE id = ?", (new_role, client_id))
    conn.commit()
    conn.close()

    bot.answer_callback_query(call.id, f"Роль успешно изменена на {new_role}.")
    bot.send_message(call.message.chat.id, f"Роль пользователя с ID {client_id} обновлена на {new_role}.")

def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    role = get_user_role(user_id)
    return role == "admin"

# Изменение клиента
@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_client_"))
def handle_edit_client(call):
    client_id = int(call.data.split("_")[2])
    temp_user_data[call.from_user.id] = {"client_id": client_id}  # Сохраняем ID клиента

    # Выводим выбор, что менять
    markup = InlineKeyboardMarkup()
    markup.add(
        InlineKeyboardButton("Изменить имя", callback_data="edit_name"),
        InlineKeyboardButton("Изменить телефон", callback_data="edit_phone")
    )
    bot.send_message(call.message.chat.id, "Что вы хотите изменить?", reply_markup=markup)

# Обработка выбора (имя или телефон)
@bot.callback_query_handler(func=lambda call: call.data in ["edit_name", "edit_phone"])
def handle_edit_choice(call):
    user_id = call.from_user.id

    if call.data == "edit_name":
        bot.send_message(call.message.chat.id, "Введите новое имя:")
        set_user_state(user_id, "EDITING_NAME")
    elif call.data == "edit_phone":
        bot.send_message(call.message.chat.id, "Введите новый номер телефона:")
        set_user_state(user_id, "EDITING_PHONE")

# Сохранение изменений в базе данных
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) in ["EDITING_NAME", "EDITING_PHONE"])
def update_client_data(message):
    state = get_user_state(message.chat.id)
    client_id = temp_user_data[message.chat.id]["client_id"]
    new_value = message.text

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    if state == "EDITING_NAME":
        cursor.execute("UPDATE clients SET name = ? WHERE id = ?", (new_value, client_id))
    elif state == "EDITING_PHONE":
        cursor.execute("UPDATE clients SET phone = ? WHERE id = ?", (new_value, client_id))
    conn.commit()
    conn.close()

    bot.send_message(message.chat.id, "Данные клиента успешно обновлены.")
    clear_user_state(message.chat.id)

# Удаление клиента
@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_client_"))
def handle_delete_client(call):
    client_id = int(call.data.split("_")[2])

    # Удаляем клиента из базы данных
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()
    cursor.execute("DELETE FROM clients WHERE id = ?", (client_id,))
    conn.commit()
    conn.close()

    bot.send_message(call.message.chat.id, f"Клиент с ID {client_id} успешно удален.")


# Новый пост
@bot.message_handler(func=lambda message: message.text == "➕ Новый пост")
def create_new_post(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    if role not in ["worker", "admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    bot.send_message(
        message.chat.id,
        "Пожалуйста, отправьте фотографию для вашего поста."
    )
    temp_post_data[message.chat.id] = {}
    set_user_state(message.chat.id, UserState.CREATING_POST)

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.chat.id
    role = get_user_role(user_id)
    state = get_user_state(message.chat.id)
    if role not in ["worker", "admin"]:
        bot.send_message(user_id, "Если у вас возникли вопросы, задайте их в чате поддержки")
        return
    if state == UserState.CREATING_POST:
        temp_post_data[message.chat.id]["photo"] = message.photo[-1].file_id
        bot.send_message(message.chat.id, "Теперь введите цену на товар.")
    else:
        bot.send_message(message.chat.id, "Сначала нажми ➕ Новый пост")

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.CREATING_POST)
def handle_post_details(message):
    chat_id = message.chat.id
    if "photo" in temp_post_data[chat_id] and "price" not in temp_post_data[chat_id]:
        if not message.text.isdigit():
            bot.send_message(chat_id, "Ошибка: Цена должна быть числом. Попробуйте снова.")
            return
        temp_post_data[chat_id]["price"] = message.text
        bot.send_message(chat_id, "Введите описание товара.")
    elif "price" in temp_post_data[chat_id] and "description" not in temp_post_data[chat_id]:
        temp_post_data[chat_id]["description"] = message.text
        bot.send_message(chat_id, "Введите количество товара.")
    elif "description" in temp_post_data[chat_id] and "quantity" not in temp_post_data[chat_id]:
        if not message.text.isdigit():
            bot.send_message(chat_id, "Ошибка: Количество должно быть числом. Попробуйте снова.")
            return
        temp_post_data[chat_id]["quantity"] = int(message.text)

        # Сохраняем пост
        data = temp_post_data[chat_id]
        save_post(chat_id, data["photo"], data["price"], data["description"], data["quantity"])
        bot.send_message(chat_id, "Ваш пост успешно создан!")
        clear_user_state(chat_id)

# Управление постами
@bot.message_handler(func=lambda message: message.text == "📄 Посты")
def manage_posts(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    # Проверяем, что пользователь является администратором
    if role not in ["admin", "worker"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Подключение к базе данных и извлечение необходимых постов
    try:
        conn = sqlite3.connect('main/bot_database.db')
        cursor = conn.cursor()
        cursor.execute('SELECT id, photo, price, description, quantity FROM posts WHERE is_sent = 0')
        posts = cursor.fetchall()
        conn.close()
    except sqlite3.Error as e:
        bot.send_message(user_id, f"Ошибка базы данных: {e}")
        return

    # Если постов нет, уведомляем администратора
    if not posts:
        bot.send_message(user_id, "Нет доступных постов для управления.")
        return

    # Обрабатываем и отправляем информацию о каждом посте
    for post in posts:
        # Данные поста
        post_id, photo, price, description, quantity = post

        # Создаем InlineKeyboardMarkup и кнопки для управления постами
        markup = InlineKeyboardMarkup()
        edit_btn = InlineKeyboardButton("✏️ Изменить", callback_data=f"edit_post_{post_id}")
        delete_btn = InlineKeyboardButton("🗑 Удалить", callback_data=f"delete_post_{post_id}")
        markup.add(edit_btn, delete_btn)

        # Отправляем пост админу
        try:
            # Отдельно обрабатываем наличие фото, если оно указано
            if photo:
                bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=f"**Пост #{post_id}:**\n"
                            f"📍 *Описание:* {description}\n"
                            f"💰 *Цена:* {price}\n"
                            f"📦 *Количество:* {quantity}",
                    parse_mode="Markdown",
                    reply_markup=markup
                )
            else:
                # Если фото нет, отправляем только текст
                bot.send_message(
                    chat_id=user_id,
                    text=f"**Пост #{post_id}:**\n"
                         f"📍 *Описание:* {description}\n"
                         f"💰 *Цена:* {price}\n"
                         f"📦 *Количество:* {quantity}",
                    parse_mode="Markdown",
                    reply_markup=markup
                )
        except Exception as e:
            bot.send_message(user_id, f"Ошибка при отправке поста #{post_id}: {e}")

def get_reserved_count():
    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Подсчитываем общее количество забронированных товаров
    cursor.execute('''
        SELECT SUM(posts.quantity - (SELECT COUNT(*) 
                                     FROM reservations 
                                     WHERE reservations.post_id = posts.id))
        FROM posts
    ''')
    count = cursor.fetchone()[0] or 0  # Если нет данных, возвращаем 0
    conn.close()
    return count

@bot.message_handler(func=lambda message: message.text == "Показать забронированные")
def show_reserved_items(message):
    reserved_count = get_reserved_count()  # Получаем количество забронированных товаров
    bot.send_message(message.chat.id, f"Общее количество забронированных товаров: {reserved_count}")

@bot.message_handler(func=lambda message: message.text == "⬅️ Назад")
def go_back(message):
    markup = admin_main_menu()
    bot.send_message(message.chat.id, "Возвращаюсь в главное меню.", reply_markup=markup)

# Отправка в канал
@bot.message_handler(func=lambda message: message.text == "📢 Отправить посты в канал")
def send_new_posts_to_channel(message):
    user_id = message.chat.id
    role = get_user_role(user_id)

    # Проверяем, есть ли права на отправку постов
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    conn = sqlite3.connect('main/bot_database.db')
    cursor = conn.cursor()

    # Получаем имя пользователя
    user_first_name = message.chat.first_name or "Без имени"

    # Получаем посты, которые ещё не были отправлены в канал
    cursor.execute('SELECT id, photo, price, description, quantity FROM posts WHERE is_sent = 0')
    posts = cursor.fetchall()

    if posts:
        for post in posts:
            post_id, photo, price, description, quantity = post
            caption = f"Цена: {price}\nОписание: {description}\nОстаток: {quantity}"

            # Добавляем кнопку для бронирования
            markup = InlineKeyboardMarkup()
            reserve_btn = InlineKeyboardButton("🛒 Забронировать", callback_data=f"reserve_{post_id}")
            to_bot_button = InlineKeyboardButton("В бота", url="https://t.me/MegaSkidkiTgBot?start=start")
            markup.add(reserve_btn, to_bot_button)

            # Отправка поста в канал
            sent_message = bot.send_photo(CHANNEL_ID, photo=photo, caption=caption, reply_markup=markup)

            # Формируем сообщение для группы
            group_caption = f"Пост был выложен пользователем: {user_first_name}\n\n{caption}"

            # Отправка поста в группу (без кнопки)
            bot.send_photo(-1002330057848, photo=photo, caption=group_caption)

            # Обновляем статус поста на "отправлено" и сохраняем message_id
            cursor.execute('UPDATE posts SET is_sent = 1, message_id = ? WHERE id = ?',
                           (sent_message.message_id, post_id))
            conn.commit()

        bot.send_message(user_id, f"✅ Все новые посты ({len(posts)}) успешно отправлены в канал и группу.")
    else:
        bot.send_message(user_id, "Нет новых постов для отправки.")

    conn.close()

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.REGISTERING_NAME)
def register_name(message):
    user_id = message.chat.id
    temp_user_data[user_id]["name"] = message.text
    bot.send_message(user_id, "Введите ваш номер телефона:")
    set_user_state(user_id, UserState.REGISTERING_PHONE)

@bot.callback_query_handler(func=lambda call: call.data.startswith("reserve_"))
def handle_reservation(call):
    post_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    if not is_registered(user_id):
        # Показываем всплывающее уведомление с понятной инструкцией и текстовой ссылкой на бота
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="Вы не зарегистрированы! Для регистрации откройте перейдите В бота",
            show_alert=True  # Включаем отображение в виде окна
        )
    else:
        # Если пользователь зарегистрирован, выполняем стандартную логику бронирования
        bot.send_message(user_id, f"Вы забронировали товар с ID {post_id}.")
        bot.send_message(
            ADMIN_CHAT_ID,
            f"Пользователь {call.from_user.first_name} забронировал пост {post_id}."
        )

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.REGISTERING_PHONE)
def register_phone(message):
    user_id = message.chat.id
    phone = message.text
    name = temp_user_data[user_id]["name"]

    register_client(user_id, name, phone)
    bot.send_message(user_id, "Регистрация завершена! Теперь вы можете бронировать товары.")
    bot.send_message(ADMIN_CHAT_ID, f"Пользователь {name}, телефон {phone}, успешно зарегистрировался.")
    clear_user_state(user_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_post_"))
def edit_post(call):
    post_id = int(call.data.split("_")[2])
    user_id = call.from_user.id

    # Проверяем, имеет ли пользователь права на редактирование
    role = get_user_role(user_id)
    if role not in ["admin", "worker"]:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="У вас нет прав доступа к этой функции.",
            show_alert=True
        )
        return

    # Очищаем временные данные пользователя
    temp_post_data[user_id] = {"post_id": post_id}

    # Отправляем сообщение с инструкцией
    message_text = "Редактирование поста. Введите новую цену для поста:"
    if call.message.text:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=message_text
        )
    else:
        msg_sent = bot.send_message(chat_id=call.message.chat.id, text=message_text)
        temp_post_data[user_id]["bot_message_id"] = msg_sent.message_id

    # Устанавливаем состояние пользователя
    set_user_state(user_id, UserState.EDITING_POST)


@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_post_"))
def edit_post(call):
    post_id = int(call.data.split("_")[2])
    user_id = call.from_user.id

    # Проверяем, имеет ли пользователь права на редактирование
    role = get_user_role(user_id)
    if role not in ["admin", "worker"]:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="У вас нет прав доступа к этой функции.",
            show_alert=True
        )
        return

    # Очищаем временные данные пользователя
    temp_post_data[user_id] = {"post_id": post_id}

    # Отправляем сообщение с инструкцией
    message_text = "Редактирование поста. Введите новую цену для поста:"
    if call.message.text:
        bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text=message_text
        )
    else:
        msg_sent = bot.send_message(chat_id=call.message.chat.id, text=message_text)
        temp_post_data[user_id]["bot_message_id"] = msg_sent.message_id

    # Устанавливаем состояние пользователя
    set_user_state(user_id, UserState.EDITING_POST)


@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.EDITING_POST)
def edit_post_details(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id].get("post_id")

    # Удаляем последние сообщения пользователя и бота
    if "last_message_id" in temp_post_data[user_id]:
        try:
            bot.delete_message(chat_id=user_id, message_id=temp_post_data[user_id]["last_message_id"])
        except Exception as e:
            pass  # Если сообщение уже удалено
    if "bot_message_id" in temp_post_data[user_id]:
        try:
            bot.delete_message(chat_id=user_id, message_id=temp_post_data[user_id]["bot_message_id"])
        except Exception as e:
            pass  # Если сообщение уже удалено

    # Если цена ещё не введена
    if "price" not in temp_post_data[user_id]:
        if not message.text.isdigit():
            error_msg = bot.send_message(user_id, "Ошибка: Цена должна быть числом. Попробуйте снова.")
            temp_post_data[user_id]["bot_message_id"] = error_msg.message_id
            return
        temp_post_data[user_id]["price"] = message.text
        msg = bot.send_message(user_id, "Теперь введите описание поста.")
        temp_post_data[user_id]["bot_message_id"] = msg.message_id
        temp_post_data[user_id]["last_message_id"] = message.message_id

    # Если цена введена, но описание ещё нет
    elif "description" not in temp_post_data[user_id]:
        temp_post_data[user_id]["description"] = message.text
        msg = bot.send_message(user_id, "Введите новое количество товара.")
        temp_post_data[user_id]["bot_message_id"] = msg.message_id
        temp_post_data[user_id]["last_message_id"] = message.message_id

    # Если описание введено, но количество ещё нет
    elif "quantity" not in temp_post_data[user_id]:
        if not message.text.isdigit():
            error_msg = bot.send_message(user_id, "Ошибка: Количество должно быть числом. Попробуйте снова.")
            temp_post_data[user_id]["bot_message_id"] = error_msg.message_id
            return
        temp_post_data[user_id]["quantity"] = int(message.text)

        # Обновляем пост в базе данных
        data = temp_post_data[user_id]
        update_post(post_id, data["price"], data["description"], data["quantity"])

        # Информируем об успешном обновлении поста
        confirmation_msg = bot.send_message(user_id, "Пост успешно обновлён!")
        temp_post_data[user_id]["bot_message_id"] = confirmation_msg.message_id
        
        # Очищаем временные данные пользователя
        del temp_post_data[user_id]
        clear_user_state(user_id)


# Запуск бота
if __name__ == "__main__":
    bot.polling(none_stop=True)
    #pizdec