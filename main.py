import re
import telebot
import psycopg2


from bot import admin_main_menu, client_main_menu, worker_main_menu, unknown_main_menu, supreme_leader_main_menu
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from database.config import TOKEN, CHANNEL_ID, ADMIN_USER_ID, TARGET_GROUP_ID
from telebot.apihelper import ApiTelegramException
from db import Reservations, Posts, Clients, BlackList
from handlers.clients_manage import *
from handlers.posts_manage import *
from types import SimpleNamespace
from datetime import datetime, timedelta

from handlers.reservations_manage import calculate_total_sum, calculate_processed_sum

# Настройка бота
bot = telebot.TeleBot(TOKEN)
user_messages = {}
user_pages = {}
PAGE_SIZE = 5
user_last_message_id = {}
last_bot_message = {}
user_data = {}
user_states = {}
temp_user_data = {}
temp_post_data = {}
last_start_time = {}


# Состояния пользователя
class UserState:
    STARTED_REGISTRATION = 0
    REGISTERING_NAME = 1
    REGISTERING_PHONE = 2
    CREATING_POST = 3
    EDITING_POST = 4
    NEUTRAL = None

# Сохранение бронирования
def save_reservation(user_id, post_id, quantity=1, is_fulfilled=False):
    try:
        Reservations.insert(
            user_id=user_id,
            quantity=quantity,
            post_id=post_id,
            is_fulfilled=is_fulfilled,
        )
        return True, "Резервация успешно сохранена"
    except Exception as e:
        return False, f"Ошибка при сохранении резервации: {e}"

# Получение неотправленных постов
@bot.message_handler(commands=["unsent_posts"])
def list_unsent_posts(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем роль пользователя
    if role not in ["admin", "worker"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Получаем неотправленные посты через метод класса
    try:
        unsent_posts = Posts.get_unsent_posts()
    except Exception as e:
        bot.send_message(user_id, f"Ошибка при получении данных: {e}")
        return

    # Формируем и отправляем ответ
    if unsent_posts:
        response = "📮 Неотправленные посты:\n"
        for post in unsent_posts:
            post_id, price, description, quantity = post
            response += f"ID: {post_id} | Цена: {price}₽ | Описание: {description} | Количество: {quantity}\n"
        bot.send_message(user_id, response)
    else:
        bot.send_message(user_id, "✅ Все посты отправлены.")

# Инициализация старта бота
@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.chat.id

    # Получаем роль пользователя
    role = get_client_role(user_id)

    # Формируем персонализированное приветствие
    greetings = {
        "client": "Добро пожаловать в интерфейс бота, здесь вы можете просмотреть свою корзину или задать вопросы в чате поддержки.",
        "worker": "Давай за работу!",
        "supreme_leader": "С возвращением, Повелитель!",
        "admin": "С возвращением в меню администратора",
    }
    greeting = greetings.get(role, "Привет, прошу пройти регистрацию")

    # Ссылки для инлайн-клавиатуры
    support_link = "https://t.me/+Li2_LC6Anm9iMTli"  # Ссылка на поддержку
    channel_link = "https://t.me/MegaSkidkiTg"  # Ссылка на канал

    inline_markup = InlineKeyboardMarkup()
    inline_markup.add(InlineKeyboardButton("В поддержку", url=support_link))
    inline_markup.add(InlineKeyboardButton("На канал", url=channel_link))

    # Определяем reply-клавиатуру по роли пользователя
    if role == "admin":
        reply_markup = admin_main_menu()
    elif role == "client":
        reply_markup = client_main_menu()
    elif role == "worker":
        reply_markup = worker_main_menu()
    elif role == "supreme_leader":
        reply_markup = supreme_leader_main_menu()
    else:
        reply_markup = unknown_main_menu()

    # Удаляем старые сообщения бота, если они существуют
    if user_id in last_bot_message:
        try:
            # Удаляем сообщение с приветствием
            bot.delete_message(
                chat_id=user_id, message_id=last_bot_message[user_id]["greeting"]
            )
        except Exception as e:
            print(
                f"Не удалось удалить старое приветственное сообщение для {user_id}: {e}"
            )
        try:
            # Удаляем сообщение с ресурсами
            bot.delete_message(
                chat_id=user_id, message_id=last_bot_message[user_id].get("resources")
            )
        except Exception as e:
            print(f"Не удалось удалить старое сообщение с ресурсами для {user_id}: {e}")

    # Отправляем новое сообщение с приветствием
    try:
        sent_greeting = bot.send_message(user_id, greeting, reply_markup=reply_markup)

        # Если роль клиента, отправляем сообщение с ресурсами
        if role == "client":
            sent_resources = bot.send_message(
                user_id, "Посетите наши ресурсы:", reply_markup=inline_markup
            )
        else:
            sent_resources = None

        # Сохраняем ID новых сообщений
        last_bot_message[user_id] = {
            "greeting": sent_greeting.message_id,
            "resources": sent_resources.message_id if sent_resources else None,
        }
    except Exception as e:
        print(f"Ошибка при отправке сообщения для {user_id}: {e}")

    # Удаляем сообщение пользователя (команда /start)
    try:
        bot.delete_message(chat_id=user_id, message_id=message.message_id)
    except Exception as e:
        print(f"Ошибка при удалении сообщения пользователя {message.message_id}: {e}")


@bot.message_handler(func=lambda message: message.text == "Регистрация")
def handle_registration(message):
    chat_id = message.chat.id

    # Проверяем, находится ли пользователь в черном списке
    if is_user_blacklisted(chat_id):
        bot.send_message(chat_id, "К сожалению, вы не можете зарегистрироваться, так как находитесь в черном списке.")
        return

    # Проверяем, зарегистрирован ли уже пользователь
    if Clients.get_row_by_user_id(chat_id):
        bot.send_message(chat_id, "Вы уже зарегистрированы!")
        handle_start(message)
        return

    # Сохраняем состояние для шага регистрации имени
    set_user_state(chat_id, UserState.REGISTERING_NAME)
    bot.send_message(chat_id, "Введите ваше имя:")

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.REGISTERING_NAME)
def handle_name_registration(message):
    chat_id = message.chat.id
    user_name = message.text.strip()

    # Проверяем валидность имени
    if len(user_name) < 2:
        bot.send_message(chat_id, "Имя слишком короткое. Пожалуйста, введите хотя бы 2 символа.")
        return

    # Сохраняем имя во временные данные
    if chat_id not in temp_user_data:
        temp_user_data[chat_id] = {}
    temp_user_data[chat_id]["name"] = user_name

    # Переход к следующему шагу - вводу номера
    set_user_state(chat_id, UserState.STARTED_REGISTRATION)
    bot.send_message(chat_id, "Введите ваш номер телефона:")

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.STARTED_REGISTRATION)
def handle_phone_registration(message):
    chat_id = message.chat.id
    raw_phone = message.text.strip()
    cleaned_phone = re.sub(r"\D", "", raw_phone)

    # Проверка валидности номер через регулярное выражение
    if is_phone_valid(cleaned_phone):
        if cleaned_phone.startswith("7"):
            cleaned_phone = "8" + cleaned_phone[1:]

        # Проверяем наличие номера в базе
        existing_client = Clients.get_row_by_phone(cleaned_phone)
        if existing_client:
            # Номер уже существует
            bot.send_message(
                chat_id,
                f"⚠️ Номер телефона {cleaned_phone} уже зарегистрирован. Вы уверены, что хотите его привязать к своему аккаунту? "
                "Эта операция уведомит текущего владельца номера.",
                reply_markup=create_yes_no_keyboard()  # Генерируем клавиатуру
            )
            # Сохраняем телефон во временные данные для подтверждения
            if chat_id not in temp_user_data:
                temp_user_data[chat_id] = {}
            temp_user_data[chat_id]["phone"] = cleaned_phone
            set_user_state(chat_id, UserState.REGISTERING_PHONE)  # Переходим в состояние подтверждения номера
        else:
            # Если номер уникальный, завершаем регистрацию
            complete_registration(chat_id, cleaned_phone)
    else:
        bot.send_message(chat_id, "❌ Введите корректный номер телефона. Например, +7XXXXXXXXXX")

def is_phone_valid(phone):
    pattern = r"^(8|7|\+7)\d{10}$"
    return re.match(pattern, phone) is not None

# Подтверждение номера
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.REGISTERING_PHONE)
def confirm_phone_registration(message):
    chat_id = message.chat.id
    response = message.text.strip().lower()

    # Проверяем временные данные
    if chat_id not in temp_user_data or "phone" not in temp_user_data[chat_id]:
        bot.send_message(chat_id, "❌ Ошибка: временные данные не найдены. Попробуйте снова.")
        clear_user_state(chat_id)
        return

    new_phone = temp_user_data[chat_id]["phone"]
    user_name = temp_user_data[chat_id].get("name", "Неизвестный")  # Используем сохранённое имя

    if response == "да":
        # Уведомляем текущего владельца номера
        existing_client = Clients.get_row_by_phone(new_phone)
        if existing_client:
            bot.send_message(
                existing_client.user_id,
                f"⚠️ Ваш номер телефона ({new_phone}) был использован другим пользователем. "
                "Если это действие вызвало проблемы, обратитесь в поддержку."
            )
        # Завершаем регистрацию
        complete_registration(chat_id, new_phone)

    elif response == "нет":
        bot.send_message(chat_id, "❌ Регистрация номера отменена. Введите новый номер:")
        set_user_state(chat_id, UserState.STARTED_REGISTRATION)  # Позволяем повторить ввод номера
    else:
        bot.send_message(
            chat_id,
            "⚠️ Пожалуйста, введите *'Да'* для подтверждения или *'Нет'* для отказа.",
            parse_mode="Markdown"
        )

# Завершение регистрации
def complete_registration(chat_id, phone):
    """Завершает регистрацию пользователя"""
    name = temp_user_data.get(chat_id, {}).get("name", "Неизвестный")  # Подстраховка получения имени
    try:
        # Проверить, существует ли пользователь с данным user_id
        role = "supreme_leader" if chat_id == ADMIN_USER_ID else "client"

        # Проверить, существует ли пользователь с данным user_id
        existing_user = Clients.get_row_by_user_id(chat_id)

        if existing_user:
            # Если пользователь уже существует, обновляем его данные
            success, message = Clients.update_row(
                user_id=chat_id,
                name=name,
                phone=phone,
                role=role  # Установить роль
            )

            if success:
                print(f"[DEBUG]: Данные пользователя обновлены: {message}")
            else:
                print(f"[DEBUG]: Ошибка при обновлении: {message}")
        else:
            # Если пользователь не найден, создаём новую запись
            Clients.insert(
                user_id=chat_id,
                name=name,
                phone=phone,
                role=role  # Установить роль
            )
        # Сброс временных данных пользователя
        clear_user_state(chat_id)

        # Уведомить пользователя о регистрации/обновлении
        bot.send_message(
            chat_id,
            f"✅ Ваш аккаунт зарегистрирован с номером: {phone}",
            reply_markup=types.ReplyKeyboardRemove()
        )

        # Инициализация start
        handle_start(SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=None))
    except Exception as e:
        bot.send_message(chat_id, "❌ Произошла ошибка при регистрации. Попробуйте позже.")
        print(f"[ERROR]: Ошибка добавления/обновления в БД: {e}")

# Создание клавиатуры да или нет для подтверждения номера
def create_yes_no_keyboard():
    """Генерирует клавиатуру для подтверждения"""
    markup = types.ReplyKeyboardMarkup(row_width=2, resize_keyboard=True, one_time_keyboard=True)
    markup.add(types.KeyboardButton("Да"), types.KeyboardButton("Нет"))
    return markup

# Проверка на черный список no
def is_user_blacklisted(user_id: int) -> bool:
    # Используем метод get_row для получения строки из таблицы black_list
    blacklisted_user = BlackList.get_row(user_id)
    # Если результат не пустой, то пользователь в черном списке
    return bool(blacklisted_user)

# Проверка регистрации пользователя no
def is_user_registered(phone: str) -> bool:
    try:
        with Session(bind=engine) as session:
            # Ищем номер в таблице клиентов
            return session.query(Clients).filter(Clients.phone == phone).first() is not None
    except Exception as e:
        print(f"Ошибка проверки пользователя: {e}")
        return False

# Установка роли для человека
# @bot.message_handler(commands=["setrole"])
# def set_role_command(message):
#     user_id = message.chat.id
#
#     # Проверяем, есть ли у текущего пользователя права администратора
#     user_data = Clients.get_row(user_id)  # Передаем только ID
#     if not user_data or user_data.role != "admin":
#         bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
#         return
#
#     try:
#         # Ожидаем ввод в формате "/setrole user_id role"
#         _, target_user_id, role = message.text.split()  # Команда, ID, роль
#         target_user_id = int(target_user_id)  # Преобразуем ID в int
#
#         # Проверяем валидность роли
#         if role not in ["client", "worker", "admin"]:
#             bot.send_message(
#                 user_id, "Некорректная роль. Используйте одну из: client, worker, admin."
#             )
#             return
#
#         # Проверяем, существует ли пользователь с указанным ID
#         target_user_data = Clients.get_row(target_user_id)  # Снова передаем только ID
#         if not target_user_data:
#             bot.send_message(user_id, f"Пользователь с ID {target_user_id} не найден.")
#             return
#
#         # Обновляем роль пользователя в базе
#         Clients.update_row_to_role({"id": target_user_id}, {"role": role})
#
#         bot.send_message(
#             user_id,
#             f"Роль '{role}' успешно установлена для пользователя {target_user_id}.",
#         )
#     except ValueError:
#         bot.send_message(user_id, "Неверный формат команды. Используйте: /setrole user_id role")
#     except Exception as e:
#         bot.send_message(user_id, f"Произошла ошибка: {e}")

# Обработчик запроса бронирования
@bot.callback_query_handler(func=lambda call: call.data.startswith("reserve_"))
def handle_reservation(call):
    post_id = int(call.data.split("_")[1])
    user_id = call.from_user.id

    if is_user_blacklisted(user_id):
        return "Вы не можете бронировать товары, так как вы были заблокированы"
    # Проверяем, зарегистрирован ли пользователь
    if not is_registered(user_id):
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="Вы не зарегистрированы! Для регистрации перейдите в бота",
            show_alert=True,
        )
        return

    # Получаем данные о посте
    post = Posts.get_row_by_id(post_id)
    if not post:  # Если пост не найден или get_row_by_id вернул None
        bot.send_message(user_id, "Ошибка: Товар не найден.")
        return

    # Если метод get_row_by_id возвращает объект, работаем с ним напрямую
    current_quantity = post.quantity
    message_id = post.message_id
    price = post.price
    description = post.description

    # Проверяем доступное количество товара
    if current_quantity == 0:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="Этот товар уже забронирован полностью!",
            show_alert=True,
        )
        return

    # Сохраняем бронирование
    Reservations.insert(user_id=user_id, quantity=1, post_id=post_id, is_fulfilled=False)

    # Уменьшаем количество товара на 1
    new_quantity = current_quantity - 1
    update_status, message = Posts.update_row(
        post_id=post_id, price=price, description=description, quantity=new_quantity
    )
    if not update_status:
        bot.send_message(user_id, f"Не удалось обновить данные о товаре: {message}")
        return

    # Формируем новое описание для сообщения в канале
    new_caption = (
        f"Цена: {price}\nОписание: {description}\nОстаток: {new_quantity}"
    )

    # Обновляем сообщение в канале
    try:
        bot.edit_message_caption(
            chat_id=CHANNEL_ID,
            message_id=message_id,
            caption=new_caption,
            reply_markup=call.message.reply_markup,  # Кнопки остаются неизменными
        )
    except Exception as e:
        bot.send_message(
            user_id, f"Не удалось обновить сообщение в канале. Ошибка: {e}"
        )

    # Информируем пользователя, если он забронировал последний экземпляр
    if new_quantity == 0:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="Вы забронировали последний экземпляр товара!",
            show_alert=True,
        )
    else:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="Бронь успешно сохранена!",
            show_alert=False,
        )

# Получение бронирования пользователя
def get_user_reservations(user_id):
    # Получаем все бронирования пользователя
    reservations = Reservations.get_row_all(user_id)
    if not reservations:
        return []

    # Собираем результаты
    results = []
    for reservation in reservations:
        post_id = reservation.post_id
        print(f"Checking post_id: {post_id}")  # Отладка: вывод post_id

        # Получаем данные о посте, связанном с бронированием
        post = Posts.get_row_by_id(post_id)
        if not post:
            print(f"Post not found for post_id: {post_id}")  # Отладка: если пост не найден
            continue  # Пропускаем записи с несуществующими постами

        # Заполняем результат
        results.append({
            "description": post.description,
            "price": post.price,
            "photo": post.photo,
            "quantity": reservation.quantity,
            "is_fulfilled": reservation.is_fulfilled
        })

    print(f"Final results: {results}")  # Отладка: что будет возвращено
    return results

# Обработчик моих забронированных товаров
@bot.message_handler(commands=["my_reservations"])
def show_reservations(message):
    user_id = message.chat.id
    query = Clients.get_row(user_id=user_id)
    # Проверка регистрации пользователя
    # if not is_registered(user_id):
    if query is None:
        msg = bot.send_message(
            user_id,
            "Вы не зарегистрированы! Для регистрации используйте команду /start register.",
        )
        user_messages[user_id] = [msg.message_id]
        return

    # Получаем заказы пользователя
    reservations = get_user_reservations(user_id)

    if reservations:
        for idx, (description, price, photo, quantity, is_fulfilled) in enumerate(
            reservations, start=1
        ):
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
                    bot.send_message(
                        user_id, f"Ошибка при показе фотографии: {e}"
                    )  # Показываем ошибку

# Хэндлер для обработки нажатий на заказ
@bot.callback_query_handler(func=lambda call: call.data.startswith("order_"))
def order_details(call):
    reservation_id = int(call.data.split("_")[1])

    try:
        # Получаем информацию о заказе через ORM
        order = Reservations.get_row_by_id(reservation_id)
        if not order:
            bot.answer_callback_query(call.id, "Заказ не найден.", show_alert=True)
            return

        # Получаем пост, связанный с этим заказом
        post = Posts.get_row_by_id(order.post_id)
        if not post:
            bot.answer_callback_query(call.id, "Товар не найден.", show_alert=True)
            return

        status = "✔️ Обработан" if order.is_fulfilled else "⌛ В обработке"
        caption = f"Товар: {post.description}\nЦена: {post.price}\nСтатус: {status}"

        # Создаём кнопки возврата или отмены
        markup = InlineKeyboardMarkup()
        back_btn = InlineKeyboardButton("⬅️ Назад", callback_data="my_orders")
        markup.add(back_btn)
        # Добавляем кнопку отмены, если заказ ещё не обработан
        if not order.is_fulfilled:
            cancel_btn = InlineKeyboardButton("❌ Отказаться", callback_data=f"cancel_{reservation_id}")
            markup.add(cancel_btn)

        # Обновляем сообщение с деталями заказа
        bot.edit_message_media(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            media=InputMediaPhoto(media=post.photo, caption=caption),
            reply_markup=markup
        )
    except Exception as e:
        print(f"Ошибка отображения деталей заказа: {e}")
        bot.answer_callback_query(call.id, "Произошла ошибка.", show_alert=True)

@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def show_my_orders(call):
    """
    Обработчик кнопки 'Назад'.
    Отображает список заказов.
    """
    message = call.message
    my_orders(message)  # Вызываем my_orders, передаём исходное сообщение
    bot.answer_callback_query(call.id)  # Подтверждаем обработку нажатия

# Обработчик функции Мои заказы
@bot.message_handler(func=lambda message: message.text == "🛒 Мои заказы")
def my_orders(message):
    user_id = message.chat.id
    message_id = message.message_id  # Идентификатор сообщения пользователя
    keyboard = InlineKeyboardMarkup(row_width=1)

    try:
        # Удаляем сообщение пользователя сразу, чтобы не мешало
        try:
            bot.delete_message(chat_id=user_id, message_id=message_id)
        except Exception as e:
            print(f"Ошибка удаления сообщения пользователя: {e}")

        # Удаляем предыдущее сообщение от бота, если есть
        last_message_id = user_last_message_id.get(user_id)
        if last_message_id:
            try:
                bot.delete_message(chat_id=user_id, message_id=last_message_id)
            except Exception as e:
                print(f"Ошибка удаления последнего сообщения от бота: {e}")

        # Получаем заказы пользователя через ORM
        orders = Reservations.get_row_by_user_id(user_id)

        # Если заказы есть, показываем их
        if orders:
            user_pages[user_id] = 0  # Устанавливаем текущую страницу на первую
            new_message = send_order_page(user_id=user_id, message_id=None, orders=orders, page=user_pages[user_id])
            user_last_message_id[user_id] = new_message.message_id  # Сохраняем последнее сообщение для пользователя
        else:
            # Если заказов нет, отправляем сообщение с предложением перейти на канал
            keyboard.add(InlineKeyboardButton(text="На канал", url="https://t.me/mgskidkitest"))
            new_message = bot.send_message(
                chat_id=user_id,
                text="У вас пока нет заказов. Начните покупки, перейдя на наш канал.",
                reply_markup=keyboard,
            )
            user_last_message_id[user_id] = new_message.message_id
    except Exception as ex:
        print(f"Ошибка в обработке команды '🛒 Мои заказы': {ex}")

# Создает страницу с заказами
def send_order_page(user_id, message_id, orders, page):
    orders_per_page = 5  # Количество заказов на одной странице
    start = page * orders_per_page
    end = start + orders_per_page
    total_pages = (len(orders) - 1) // orders_per_page + 1
    selected_orders = orders[start:end]

    # Считаем общую сумму всех заказов
    total_sum = sum(
        Posts.get_row_by_id(order.post_id).price for order in orders if Posts.get_row_by_id(order.post_id)
    )

    # Формирование текста для страницы. Колонки: описание, цена, статус заказа.
    text = f"Ваши заказы (стр. {page + 1} из {total_pages}):\n\n"
    keyboard = InlineKeyboardMarkup(row_width=1)

    for order in selected_orders:
        post = Posts.get_row_by_id(order.post_id)  # Проверка и получение данных поста через ORM
        if post:
            status = "✔️ Обработан" if order.is_fulfilled else "⌛ В обработке"
            keyboard.add(InlineKeyboardButton(
                text=f"{post.price} ₽ - {post.description} ({status})",
                callback_data=f"order_{order.id}"
            ))

    # Добавляем строку с общей суммой заказов
    text += f"\nОбщая сумма заказов: {total_sum} ₽\n"

    # Навигация по страницам
    if page > 0:
        keyboard.add(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"orders_page_{page - 1}"))
    if end < len(orders):
        keyboard.add(InlineKeyboardButton(text="➡️ Вперёд", callback_data=f"orders_page_{page + 1}"))

    # Фото для страницы
    photo_path = "images/my_cart.jpg"
    with open(photo_path, "rb") as photo:
        if message_id:
            return bot.edit_message_media(
                chat_id=user_id,
                message_id=message_id,
                media=InputMediaPhoto(photo, caption=text),
                reply_markup=keyboard
            )
        else:
            return bot.send_photo(
                chat_id=user_id,
                photo=photo,
                caption=text,
                reply_markup=keyboard
            )

# Обработчик навигации между страницами
@bot.callback_query_handler(func=lambda call: call.data.startswith("orders_page_"))
def paginate_orders(call):
    user_id = call.message.chat.id
    message_id = call.message.message_id
    page = int(call.data.split("_")[2])

    # Получаем заказы пользователя
    orders = Reservations.get_row_by_user_id(user_id=user_id)

    # Отправляем страницу с заказами
    try:
        new_message = send_order_page(user_id=user_id, message_id=message_id, orders=orders, page=page)
        user_last_message_id[user_id] = new_message.message_id  # Обновляем последний ID
    except Exception as e:
        print(f"Ошибка при попытке пагинации заказов: {e}")
    finally:
        bot.answer_callback_query(call.id)  # Подтверждаем успешную обработку

# Обработка отмены заказа
@bot.callback_query_handler(func=lambda call: call.data.startswith("cancel_"))
def cancel_reservation(call):
       reservation_id = int(call.data.split("_")[1])  # Извлекаем ID бронирования
       user_id = call.from_user.id  # Берём ID пользователя

       try:
           # Получаем данные о бронировании через ORM
           order = Reservations.get_row_by_id(reservation_id)
           if not order or order.user_id != user_id:
               bot.answer_callback_query(call.id, "Резерв не найден или не принадлежит вам.", show_alert=True)
               return

           # Проверяем, выполнено ли бронирование
           if order.is_fulfilled:
               bot.answer_callback_query(call.id, "Невозможно отказаться от уже обработанного заказа.", show_alert=True)
               return

           # Получаем информацию о товаре
           post = Posts.get_row_by_id(order.post_id)
           if not post:
               bot.answer_callback_query(call.id, "Товар для отмены не найден.", show_alert=True)
               return

           # Удаляем заказ из Reservations
           success = Reservations.cancel_order_by_id(reservation_id)
           if not success:
               bot.answer_callback_query(call.id, "Ошибка отмены заказа.", show_alert=True)
               return

           # Увеличиваем количество товара
           Posts.increment_quantity_by_id(order.post_id)

           # Удаляем сообщение о товаре в группе (основано на post.message_id)
           if post.message_id:  # Если сообщение связано с товаром
               try:
                   bot.delete_message(chat_id=TARGET_GROUP_ID, message_id=post.message_id)
                   print(f"Сообщение с ID {post.message_id} удалено из группы {TARGET_GROUP_ID}.")
               except Exception as e:
                   print(f"Ошибка удаления сообщения из группы: {e}")
           else:
               print("Не найден message_id для удаления сообщения из группы.")

           # Обновляем пост на канале, увеличив количество
           if post.message_id:  # Если у товара сохранён message_id поста
               new_quantity = post.quantity + 1  # Увеличенное количество
               updated_caption = (
                   f"Товар: {post.description}\n"
                   f"Цена: {post.price} ₽\n"
                   f"Количество: {new_quantity}"
               )
               markup = InlineKeyboardMarkup()
               reserve_button = InlineKeyboardButton("🛒 Забронировать", callback_data=f"reserve_{post.id}")
               to_bot_button = InlineKeyboardButton("В Бота", url="https://t.me/MegaSkidkiTgBot?start=start")
               markup.add(reserve_button, to_bot_button)

               try:
                   bot.edit_message_caption(
                       chat_id=CHANNEL_ID,
                       message_id=post.message_id,
                       caption=updated_caption,
                       reply_markup=markup,
                   )
               except Exception as e:
                   print(f"Ошибка обновления поста на канале: {e}")

           # Уведомляем об успешной отмене
           bot.answer_callback_query(call.id, "Вы успешно отказались от товара.", show_alert=False)

           # Перенаправляем пользователя в меню "Мои заказы"
           my_orders(call.message)

       except Exception as e:
           print(f"Ошибка при попытке отказаться от заказа: {e}")
           bot.answer_callback_query(call.id, "Произошла ошибка при обработке отмены.", show_alert=True)

# Возврат в меню заказов
@bot.callback_query_handler(func=lambda call: call.data == "go_back")
def go_back_to_menu(call):
    bot.answer_callback_query(call.id)
    bot.send_message(call.message.chat.id, "Главное меню! Выберите интересующий вас пункт.")

# Перессылка забронированного товара в группу Брони Мега Скидки
@bot.message_handler(func=lambda message: message.text == "📦 Заказы клиентов")
def send_all_reserved_to_group(message):
    user_id = message.chat.id
    role = get_client_role(user_id)  # Получение роли клиента

    # Проверка ролей
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    try:
        # Получение всех резерваций без фильтрации по конкретному пользователю
        reservations = (
            Reservations.get_row_all()
        )  # Предполагается, что вы добавите метод `get_all` в Reservations

        # Проверка: есть ли резервации
        if not reservations:
            bot.send_message(user_id, "Нет забронированных товаров для отправки.")
            return

        # Фильтруем необработанные резервации
        reservations_to_send = [r for r in reservations if not r.is_fulfilled]

        if not reservations_to_send:
            bot.send_message(user_id, "Все текущие товары уже были обработаны.")
            print(f"✅ Все бронирования уже обработаны.")
            return

        # Логирование и начало обработки
        print(f"Найдено {len(reservations_to_send)} необработанных бронирований.")

        for reservation in reservations_to_send:
            try:
                reservation_id = reservation.id
                print(
                    f"Обрабатывается бронирование ID: {reservation_id}, post_id: {reservation.post_id}"
                )

                # Получение данных о посте через его ID
                post_data = Posts.get_row(
                    reservation.post_id
                )  # Предполагается наличие метода get_row

                if not post_data:
                    bot.send_message(
                        user_id,
                        f"⚠️ Пост с ID {reservation.post_id} не найден. Пропускаем.",
                    )
                    print(
                        f"⚠️ Пост с ID {reservation.post_id} отсутствует в базе данных."
                    )
                    continue

                # Извлечение данных из поста
                photo = post_data.photo
                price = post_data.price or "Не указана"
                description = post_data.description or "Описание отсутствует"

                # Получение информации о клиенте
                client_data = Clients.get_row(
                    reservation.user_id
                )  # Аналогично, проверка пользователя

                if not client_data:
                    bot.send_message(
                        user_id,
                        f"⚠️ Клиент с ID {reservation.user_id} не найден. Пропускаем.",
                    )
                    print(f"⚠️ Клиент с ID {reservation.user_id} отсутствует в базе.")
                    continue

                # Извлечение данных клиента
                client_name = client_data.name or "Имя не указано"
                client_phone = client_data.phone or "Телефон не указан"

                # Формирование описания заказа
                caption = (
                    f"💼 Новый заказ:\n\n"
                    f"👤 Клиент: {client_name}\n"
                    f"📞 Телефон: {client_phone}\n"
                    f"💰 Цена: {price}₽\n"
                    f"📦 Описание: {description}"
                )

                # Создание кнопки
                markup = InlineKeyboardMarkup()
                mark_button = InlineKeyboardButton(
                    text="✅ Положил",
                    callback_data=f"mark_fulfilled_{reservation_id}",
                )
                markup.add(mark_button)

                # Отправка поста с фотографией или без
                if photo:
                    bot.send_photo(
                        chat_id=TARGET_GROUP_ID,
                        photo=photo,
                        caption=caption,
                        reply_markup=markup,
                    )
                else:
                    bot.send_message(
                        chat_id=TARGET_GROUP_ID, text=caption, reply_markup=markup
                    )

                print(f"✅ Успешно отправлен заказ ID {reservation_id} в группу.")
            except Exception as e:
                bot.send_message(
                    user_id, f"⚠️ Ошибка при обработке ID {reservation_id}: {e}"
                )
                print(f"⚠️ Ошибка при обработке резервации ID {reservation_id}: {e}")

        # Уведомление об успешной отправке
        bot.send_message(
            user_id, "✅ Все забронированные товары успешно отправлены в группу."
        )

    except Exception as global_error:
        bot.send_message(user_id, f"Произошла ошибка: {global_error}")
        print(f"❌ Глобальная ошибка в send_all_reserved_to_group: {global_error}")

# Херня чтобы не могли отказаться от товара который уже в пакете
@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_"))
def mark_fulfilled(call):
    # Проверка администратора
    user_id = call.from_user.id
    role = get_client_role(user_id)  # Проверяем роль пользователя

    if role != "admin":
        bot.answer_callback_query(
            call.id, "У вас нет прав доступа к этой функции.", show_alert=True
        )
        return

    try:
        # Получаем ID брони из callback_data
        reservation_id = int(call.data.split("_")[2])

        # Загружаем бронирование из базы данных
        reservation = Reservations.get_row_by_id(reservation_id)
        if not reservation:
            bot.answer_callback_query(call.id, "Бронь не найдена.", show_alert=True)
            return

        # Проверяем, не было ли уже отмечено
        if reservation.is_fulfilled:
            bot.answer_callback_query(
                call.id, "Этот товар уже отмечен как положенный.", show_alert=True
            )
            return

        # Получаем имя пользователя (кто положил)
        user_first_name = call.from_user.first_name or "Администратор"
        user_username = call.from_user.username
        user_who = (
            f"{user_first_name} (@{user_username})"
            if user_username
            else user_first_name
        )

        # Обновляем статус текущей брони
        with Session(bind=engine) as session:
            reservation.is_fulfilled = True
            session.merge(reservation)
            session.commit()

            # Проверяем, остались ли ещё необработанные брони
            remaining_reservations = (
                session.query(Reservations).filter_by(is_fulfilled=False).count()
            )

        # Если больше нет необработанных товаров, удаляем сообщение
        if remaining_reservations == 0:
            bot.delete_message(
                chat_id=call.message.chat.id, message_id=call.message.message_id
            )
            bot.answer_callback_query(
                call.id, "Все товары обработаны, сообщение удалено из канала."
            )
            return

        # Формируем обновлённое описание поста
        original_caption = (
            call.message.caption or call.message.text
        )  # Сохраняем оригинальный текст
        updated_caption = (
            f"{original_caption}\n\n"
            f"✅ Этот заказ был обработан.\n"
            f"👤 Кто положил: {user_who}"
        )

        # Обновляем описание сообщения (caption или text)
        if call.message.photo:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=updated_caption,
            )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=updated_caption,
            )

        # Уведомляем администратора, что товар положен
        bot.answer_callback_query(call.id, "Товар отмечен как положенный!")

    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)
        print(f"Ошибка в обработчике mark_fulfilled: {e}")

# Синхронизация вручную удаленного поста с группы и бота
@bot.message_handler(commands=["sync_posts"])
def sync_posts_with_channel(message):
    role = get_client_role(message.chat.id)
    user_id = message.chat.id
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Получаем все посты из таблицы Posts
    posts = Posts.get_row_all()  # Метод для получения всех строк таблицы
    deleted_posts = []

    for post in posts:
        # Проверяем, является ли `post` объектом и используем атрибуты
        post_id = post.id  # Предположим, что `post` – объект модели с атрибутами
        message_id = post.message_id  # Аналогично, если у объекта есть `message_id`

        try:
            # Проверяем, существует ли сообщение
            bot.forward_message(
                chat_id=message.chat.id,
                from_chat_id=CHANNEL_ID,
                message_id=message_id
            )
        except ApiTelegramException:
            # Если сообщение не найдено в канале, добавляем его в список на удаление
            deleted_posts.append(post_id)

    # Удаляем из базы данные о постах, которых больше нет
    for post_id in deleted_posts:
        Posts.delete_row(post_id)  # Используем метод удаления строки

    bot.send_message(
        message.chat.id,
        f"Синхронизация завершена. Удалено записей: {len(deleted_posts)}.",
    )

# Хэндлер для очистки корзины
@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_cart_"))
def clear_cart(call):
    # Получаем ID клиента из callback данных
    client_id = int(call.data.split("_")[2])

    # Используем get_row, чтобы получить user_id из таблицы clients
    client = Clients.get_row("clients", {"id": client_id})

    if not client:
        bot.send_message(call.message.chat.id, "Клиент не найден.")
        return

    user_id = client["user_id"]

    # Используем update_row для удаления всех заказов клиента в таблице reservations
    Reservations.update_row("reservations", {"user_id": user_id},
               {"deleted": True})  # Например, здесь устанавливается поле deleted в True

    bot.send_message(call.message.chat.id, "Корзина клиента успешно расформирована.")

# Проверка на регистрацию
def is_registered(user_id):
    """
    Проверяет, зарегистрирован ли пользователь в таблице clients.
    Использует метод get_row для получения данных.
    """
    client = Clients.get_row(user_id=user_id)
    return client is not None
def set_user_state(user_id, state):
    user_states[user_id] = state
    print(f"Установлено новое состояние для пользователя {user_id}: {state}")
def get_user_state(chat_id):
    state = user_states.get(chat_id, None)

    return state
def clear_user_state(user_id):
    if user_id in user_states:  # user_states, вероятно, это где хранится состояние пользователей
        del user_states[user_id]
        print(f"Состояние сброшено для пользователя {user_id}")
    else:
        print(f"Состояние не найдено для пользователя {user_id}")


# Обработчик кнопки "⚙️ Клиенты"
@bot.message_handler(func=lambda message: message.text == "⚙️ Клиенты")
def manage_clients(message):
    user_id = message.chat.id
    role = get_client_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    # Создаем клавиатуру с кнопками
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Удалить клиента по номеру телефона", "Просмотреть корзину", "⬅️ Назад")
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)


# Обработчик нажатия на кнопку "Просмотреть корзину"
@bot.message_handler(func=lambda message: message.text == "Просмотреть корзину")
def request_phone_last_digits(message):
    bot.send_message(
        message.chat.id,
        "Введите последние 4 цифры номера телефона клиента или напишите 'Все', чтобы увидеть список всех пользователей:",
    )
    set_user_state(message.chat.id, "AWAITING_PHONE_LAST_4")


# Обработчик ввода последних 4 цифр номера телефона или текста "Все"
@bot.message_handler( func=lambda message: get_user_state(message.chat.id) == "AWAITING_PHONE_LAST_4")
def handle_phone_input_or_list_clients(message):
    input_text = message.text.strip()

    if input_text.lower() == "все":
        # Получаем список всех пользователей
        clients = Clients.get_row_all()

        if not clients:
            bot.send_message(message.chat.id, "Список пользователей пуст.")
            clear_user_state(message.chat.id)
            return

        # Формируем сообщение без инлайн-кнопок "Просмотреть корзину"
        for client in clients:
            # Рассчитываем общую сумму заказов и обработанных заказов
            total_orders = calculate_total_sum(client.user_id)
            processed_orders = calculate_processed_sum(client.user_id)

            bot.send_message(
                message.chat.id,
                f"Имя: {client.name}\n"
                f"Телефон: {client.phone}\n"
                f"Роль: {client.role}\n"
                f"Общая сумма заказов: {total_orders} руб.\n"
                f"Общая сумма обработанных заказов: {processed_orders} руб.",
            )

        clear_user_state(message.chat.id)  # Очищаем состояние
        return

    # Если ввод — последние 4 цифры номера телефона
    if not input_text.isdigit() or len(input_text) != 4:
        bot.send_message(
            message.chat.id,
            "Введите корректные последние 4 цифры номера телефона (4 цифры) или 'Все'.",
        )
        return

    # Обрабатываем как ввод последних 4 цифр
    show_cart_by_last_phone_digits(message, input_text)


def show_cart_by_last_phone_digits(message, last_4_digits):
    """
    Показывает корзину по последним 4 цифрам номера телефона.
    """
    client = Clients.get_row_by_phone_digits(last_4_digits)

    if not client:
        bot.send_message(
            message.chat.id,
            "Пользователь с такими последними цифрами номера телефона не найден.",
        )
        clear_user_state(message.chat.id)
        return

    # Рассчитываем общую сумму заказов и обработанных заказов
    total_orders = calculate_total_sum(client.user_id)
    processed_orders = calculate_processed_sum(client.user_id)

    # Отправляем информацию о клиенте и его заказах
    bot.send_message(
        message.chat.id,
        f"Корзина пользователя: {client.name}\n"
        f"Общая сумма заказов: {total_orders} руб.\n"
        f"Общая сумма обработанных заказов: {processed_orders} руб.",
    )

    # Получаем содержимое корзины
    reservations = Reservations.get_row_by_user_id(client.user_id)

    if not reservations:
        bot.send_message(message.chat.id, "Корзина пользователя пуста.")
    else:
        send_cart_content(message.chat.id, reservations)

    clear_user_state(message.chat.id)


# Callback для инлайн-кнопок "Просмотреть корзину"
@bot.callback_query_handler(func=lambda call: call.data.startswith("view_cart_"))
def callback_view_cart(call):
    client_id = int(call.data.split("_")[2])  # Извлекаем ID клиента из callback_data

    # Получаем данные клиента
    client = Clients.get_row(client_id)

    if not client:
        bot.send_message(call.message.chat.id, "Пользователь не найден.")
        return

    # Информируем, чью корзину будем смотреть
    bot.send_message(call.message.chat.id, f"Корзина пользователя: {client.name}")

    # Получаем содержимое корзины
    reservations = Reservations.get_row_by_user_id(client.user_id)

    if not reservations:
        bot.send_message(call.message.chat.id, "Корзина пользователя пуста.")
    else:
        send_cart_content(call.message.chat.id, reservations)


def send_cart_content(chat_id, reservations):
    """
    Форматирует и отправляет содержимое корзины.
    """
    for reservation in reservations:
        post = Posts.get_row_by_id(reservation.post_id)

        if post:
            # Отправляем фото и информацию о товаре
            if post.photo:  # Если есть фото
                bot.send_photo(
                    chat_id,
                    photo=post.photo,
                    caption=(
                        f"Описание: {post.description}\n"
                        f"Количество: {reservation.quantity}\n"
                        f"Статус: {'Выполнено' if reservation.is_fulfilled else 'В ожидании'}"
                    ),
                )
            else:
                # Если фото нет, просто описываем товар текстом
                bot.send_message(
                    chat_id,
                    f"Описание: {post.description}\n"
                    f"Количество: {reservation.quantity}\n"
                    f"Статус: {'Выполнено' if reservation.is_fulfilled else 'В ожидании'}",
                )
        else:
            bot.send_message(chat_id, f"Товар с ID {reservation.post_id} не найден!")

# Удаление клиента по номеру телефона
@bot.message_handler(func=lambda message: message.text == "Удалить клиента по номеру телефона")
def delete_client_by_phone(message):
    user_id = message.chat.id
    role = get_client_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return
    bot.send_message(message.chat.id, "Введите номер телефона клиента для удаления:")
    set_user_state(message.chat.id, "DELETE_CLIENT_PHONE")

# Функция для удаления клиента по номеру телефона
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "DELETE_CLIENT_PHONE")
def process_delete_client_phone(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем права пользователя
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    phone = message.text.strip()  # Убираем лишние пробелы
    protected_user_id = 5411051275  # ID специального защищенного пользователя

    try:
        # Получаем клиента по номеру телефона
        client = Clients.get_row_by_phone(phone)

        if client:
            client_user_id = client.user_id  # Извлекаем user_id клиента

            # Проверяем, не выполняются ли действия с защищённым пользователем
            if client_user_id == protected_user_id:
                bot.send_message(
                    user_id, f"Клиент с номером телефона {phone} защищен от удаления."
                )
                return

            # Добавляем клиента в черный список (защищенный пользователь не будет добавлен)
            if client_user_id != protected_user_id:
                BlackList.insert(user_id=client_user_id, phone=phone)

            # Удаляем клиента из таблицы reservations
            # Используем SQLAlchemy напрямую или другую существующую логику для удаления
            with Session(bind=engine) as session:
                deleted_reservations_count = session.query(Reservations).filter(
                    Reservations.user_id == client_user_id
                ).delete()
                session.commit()

            # Удаляем клиента из таблицы clients
            Clients.delete_row(client.id)

            bot.send_message(
                user_id,
                f"Клиент с номером телефона {phone} успешно удалён. "
                f"Связанных записей в таблице reservations удалено: {deleted_reservations_count}.",
            )
        else:
            bot.send_message(user_id, f"Клиент с номером телефона {phone} не найден.")
    except Exception as e:
        # Сообщаем об ошибке
        bot.send_message(user_id, f"Произошла ошибка при удалении данных: {e}")
    finally:
        clear_user_state(user_id)

#Просмотр корзины по 4 последним цифрам телефона(Доделать)


# Возможность установить клиенту статус рабочего
@bot.callback_query_handler(func=lambda call: call.data.startswith("set_worker_") or call.data.startswith("set_client_"))
def handle_set_role(call):
    client_id = int(call.data.split("_")[2])
    new_role = "worker" if "set_worker" in call.data else "client"

    # Получаем клиента по ID (используем get_row)
    client = Clients.get_row("clients", {"id": client_id})

    if not client:
        bot.answer_callback_query(call.id, f"Клиент с ID {client_id} не найден.")
        return

    # Обновляем роль клиента (используем update_row)
    update_result = Clients.update_row("clients", {"role": new_role}, {"id": client_id})

    if update_result:
        bot.answer_callback_query(call.id, f"Роль успешно изменена на {new_role}.")
        bot.send_message(
            call.message.chat.id,
            f"Роль пользователя с ID {client_id} обновлена на {new_role}.",
        )
    else:
        bot.answer_callback_query(call.id, "Не удалось обновить роль, попробуйте позже.")

# Проверка на админа
def is_admin(user_id):
    """Проверяет, является ли пользователь администратором."""
    role = get_client_role(user_id)
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
        InlineKeyboardButton("Изменить телефон", callback_data="edit_phone"),
    )
    bot.send_message(
        call.message.chat.id, "Что вы хотите изменить?", reply_markup=markup
    )

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

# Новый пост
@bot.message_handler(func=lambda message: message.text == "➕ Новый пост")
def create_new_post(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    if role not in ["worker", "admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    bot.send_message(
        message.chat.id, "Пожалуйста, отправьте фотографию для вашего поста."
    )
    temp_post_data[message.chat.id] = {}
    set_user_state(message.chat.id, UserState.CREATING_POST)

# Фото
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.chat.id
    role = get_client_role(user_id)
    state = get_user_state(message.chat.id)
    if role not in ["worker", "admin"]:
        bot.send_message(
            user_id, "Если у вас возникли вопросы, задайте их в чате поддержки"
        )
        return
    if state == UserState.CREATING_POST:
        temp_post_data[message.chat.id]["photo"] = message.photo[-1].file_id
        bot.send_message(message.chat.id, "Теперь введите цену на товар.")
    else:
        bot.send_message(message.chat.id, "Сначала нажми ➕ Новый пост")

# Описание
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.CREATING_POST)
def handle_post_details(message):
    chat_id = message.chat.id
    if "photo" in temp_post_data[chat_id] and "price" not in temp_post_data[chat_id]:
        if not message.text.isdigit():
            bot.send_message(
                chat_id, "Ошибка: Цена должна быть числом. Попробуйте снова."
            )
            return
        temp_post_data[chat_id]["price"] = message.text
        bot.send_message(chat_id, "Введите описание товара.")
    elif (
        "price" in temp_post_data[chat_id]
        and "description" not in temp_post_data[chat_id]
    ):
        temp_post_data[chat_id]["description"] = message.text
        bot.send_message(chat_id, "Введите количество товара.")
    elif (
        "description" in temp_post_data[chat_id]
        and "quantity" not in temp_post_data[chat_id]
    ):
        if not message.text.isdigit():
            bot.send_message(
                chat_id, "Ошибка: Количество должно быть числом. Попробуйте снова."
            )
            return
        temp_post_data[chat_id]["quantity"] = int(message.text)

        # Сохраняем пост
        data = temp_post_data[chat_id]
        save_post(
            chat_id, data["photo"], data["price"], data["description"], data["quantity"]
        )
        bot.send_message(chat_id, "Ваш пост успешно создан!")
        clear_user_state(chat_id)

# Управление постами
@bot.message_handler(func=lambda message: message.text == "📄 Посты")
def manage_posts(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем, что пользователь имеет соответствующую роль
    if role not in ["admin", "worker"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    try:
        # Получаем все посты, которые ещё не были отправлены на канал
        posts = Posts.get_unsent_posts()  # Используем метод get_unsent_posts для фильтрации

    except Exception as e:
        bot.send_message(user_id, f"Пизда постам: {e}")
        return

    if not posts:
        bot.send_message(user_id, "Нет доступных постов.")
        return

    # Выводим информацию о каждом посте
    for post in posts:
        post_id = post.id
        description = post.description
        price = post.price
        quantity = post.quantity
        photo = post.photo  # Если фото есть

        # Создаем клавиатуру для управления постом
        markup = InlineKeyboardMarkup()
        edit_btn = InlineKeyboardButton(
            "✏️ Изменить", callback_data=f"edit_post_{post_id}"
        )
        delete_btn = InlineKeyboardButton(
            "🗑 Удалить", callback_data=f"delete_post_{post_id}"
        )
        markup.add(edit_btn, delete_btn)

        # Отправляем сообщение с фото или текстом
        try:
            if photo:
                bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=f"**Пост #{post_id}:**\n"
                            f"📍 *Описание:* {description}\n"
                            f"💰 *Цена:* {price}\n"
                            f"📦 *Количество:* {quantity}",
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
            else:
                bot.send_message(
                    chat_id=user_id,
                    text=f"**Пост #{post_id}:**\n"
                         f"📍 *Описание:* {description}\n"
                         f"💰 *Цена:* {price}\n"
                         f"📦 *Количество:* {quantity}",
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
        except Exception as e:
            bot.send_message(user_id, f"Ошибка при отправке поста #{post_id}: {e}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("delete_post_"))
def delete_post_handler(call):
    post_id = int(call.data.split("_")[2])  # Извлечение ID поста
    try:
        # Удалить пост из базы данных (если успешно)
        result, msg = Posts.delete_row(post_id=post_id)
        if result:
            # Сообщаем о результате
            bot.answer_callback_query(call.id, "Пост успешно удалён.")

            # Удаляем сообщение бота с постом и кнопками
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)

            # Удаляем сообщение пользователя (с его запросом)
            bot.delete_message(chat_id=call.message.chat.id, message_id=call.message.message_id)
        else:
            # Возникает ошибка при удалении поста
            bot.answer_callback_query(call.id, f"Ошибка: {msg}")
    except Exception as e:
        # Обработка исключений, если что-то пошло не так
        bot.answer_callback_query(call.id, f"Ошибка: {e}")


# Кнопка назад
@bot.message_handler(func=lambda message: message.text == "⬅️ Назад")
def go_back(message):
    markup = admin_main_menu()
    bot.send_message(
        message.chat.id, "Возвращаюсь в главное меню.", reply_markup=markup
    )

# Отправка в канал
@bot.message_handler(func=lambda message: message.text == "📢 Отправить посты в канал")
def send_new_posts_to_channel(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем, есть ли права на отправку постов
    if role not in ["admin"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Получаем имя пользователя
    user_first_name = message.chat.first_name or "Без имени"

    # Получаем посты, которые ещё не были отправлены в канал
    posts = Posts.get_unsent_posts()

    if posts:
        for post in posts:
            post_id = post.id
            photo = post.photo
            price = post.price
            description = post.description
            quantity = post.quantity
            caption = f"Цена: {price}\nОписание: {description}\nОстаток: {quantity}"

            # Добавляем кнопки
            markup = InlineKeyboardMarkup()
            reserve_btn = InlineKeyboardButton(
                "🛒 Забронировать", callback_data=f"reserve_{post_id}"
            )
            to_bot_button = InlineKeyboardButton(
                "В бота", url="https://t.me/MegaSkidkiTgBot?start=start"
            )
            markup.add(reserve_btn, to_bot_button)

            # Отправка поста в канал
            sent_message = bot.send_photo(
                CHANNEL_ID, photo=photo, caption=caption, reply_markup=markup
            )

            # Формируем сообщение для группы
            group_caption = (
                f"Пост был выложен пользователем: {user_first_name}\n\n{caption}"
            )
            bot.send_photo(-1002330057848, photo=photo, caption=group_caption)

            # Обновляем статус публикации
            Posts.mark_as_sent(post_id=post_id, message_id=sent_message.message_id)

        bot.send_message(
            user_id,
            f"✅ Все новые посты ({len(posts)}) успешно отправлены в канал и группу.",
        )
    else:
        bot.send_message(user_id, "Нет новых постов для отправки.")

# Для регистрации чета
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.REGISTERING_NAME)
def register_name(message):
    user_id = message.chat.id
    temp_user_data[user_id]["name"] = message.text
    bot.send_message(user_id, "Введите ваш номер телефона:")
    set_user_state(user_id, UserState.REGISTERING_PHONE)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_post_"))
def edit_post(call):
    post_id = int(call.data.split("_")[2])
    user_id = call.from_user.id

    # Проверяем, имеет ли пользователь права на редактирование
    role = get_client_role(user_id)
    if role not in ["admin", "worker"]:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="У вас нет прав доступа к этой функции.",
            show_alert=True,
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
            text=message_text,
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
            bot.delete_message(
                chat_id=user_id, message_id=temp_post_data[user_id]["last_message_id"]
            )
        except Exception:
            pass  # Игнорируем ошибки удаления
    if "bot_message_id" in temp_post_data[user_id]:
        try:
            bot.delete_message(
                chat_id=user_id, message_id=temp_post_data[user_id]["bot_message_id"]
            )
        except Exception:
            pass  # Игнорируем ошибки удаления

    # Если цена ещё не введена
    if "price" not in temp_post_data[user_id]:
        if not message.text.isdigit():  # Проверяем, что цена - это число
            error_msg = bot.send_message(
                user_id, "Ошибка: Цена должна быть числом. Попробуйте снова."
            )
            temp_post_data[user_id]["bot_message_id"] = error_msg.message_id
            return

        temp_post_data[user_id]["price"] = int(message.text)
        msg = bot.send_message(user_id, "Теперь введите описание поста.")
        temp_post_data[user_id]["bot_message_id"] = msg.message_id
        temp_post_data[user_id]["last_message_id"] = message.message_id

    elif "description" not in temp_post_data[user_id]:
        temp_post_data[user_id]["description"] = message.text
        msg = bot.send_message(user_id, "Введите новое количество товара.")
        temp_post_data[user_id]["bot_message_id"] = msg.message_id
        temp_post_data[user_id]["last_message_id"] = message.message_id

    elif "quantity" not in temp_post_data[user_id]:
        if not message.text.isdigit():  # Проверяем, что количество - это число
            error_msg = bot.send_message(
                user_id, "Ошибка: Количество должно быть числом. Попробуйте снова."
            )
            temp_post_data[user_id]["bot_message_id"] = error_msg.message_id
            return

        temp_post_data[user_id]["quantity"] = int(message.text)
        data = temp_post_data[user_id]

        try:
            # Используем метод Posts.update_row для обновления записи в базе
            success, msg = Posts.update_row(
                post_id=post_id,
                price=data["price"],
                description=data["description"],
                quantity=data["quantity"],
            )

            if success:
                confirmation_msg = bot.send_message(user_id, "Пост успешно обновлён!")
                temp_post_data[user_id]["bot_message_id"] = confirmation_msg.message_id
                del temp_post_data[user_id]  # Очищаем временные данные
                clear_user_state(user_id)
            else:
                bot.send_message(user_id, f"Ошибка: {msg}")

        except Exception as e:
            bot.send_message(
                user_id, f"Произошла ошибка при обновлении поста: {str(e)}"
            )

@bot.message_handler(commands=['statistic'])
def handle_statistic_command(message):
    """Обработчик команды /statistic."""
    chat_id = message.chat.id

    # Расчет статистики пользователя
    statistics = calculate_user_statistics(chat_id)

    username = statistics.get("username", "Неизвестный пользователь")

    post_count_week = statistics.get("post_count", 0)
    active_days = statistics.get("active_days", 0)
    post_count_today = statistics.get("post_count_today", 0)

    # Формирование сообщения
    response = (
        f"Статистика для: {username}\n"
        f"- Количество постов за неделю: {post_count_week}\n"
        f"- Количество постов сегодня: {post_count_today}\n"
        f"- Активных дней: {active_days}"
    )
    # Отправка сообщения пользователю
    bot.send_message(chat_id, response)

# Запуск бота
if __name__ == "__main__":
    bot.polling(none_stop=True)
    # pizdec
