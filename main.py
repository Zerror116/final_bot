import io
import re
import time
import telebot


from collections import defaultdict

from openpyxl.workbook import Workbook
from sqlalchemy import func
from bot import admin_main_menu, client_main_menu, worker_main_menu, unknown_main_menu, supreme_leader_main_menu
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto, InputFile
from database.config import *
from telebot.apihelper import ApiTelegramException
from db.for_delivery import ForDelivery
from db.temp_reservations import TempReservations
from db.in_delivery import InDelivery
from handlers.black_list import *
from handlers.clients_manage import *
from handlers.posts_manage import *
from handlers.reservations_manage import *
from types import SimpleNamespace
from handlers.reservations_manage import calculate_total_sum, calculate_processed_sum


# Настройка бота и кэш
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
delivery_active = False


# Состояния пользователя
class UserState:
    NEUTRAL = None
    STARTED_REGISTRATION = 0
    REGISTERING_NAME = 1
    REGISTERING_PHONE = 2
    CREATING_POST = 3
    EDITING_POST = 4
    EDITING_POST_PRICE = 5
    EDITING_POST_DESCRIPTION = 6
    EDITING_POST_QUANTITY = 7
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
    if role not in ["admin", "worker", "supreme_leader"]:
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
            response += f"ID: {post_id} | Цена: {price} ₽ | Описание: {description} | Количество: {quantity}\n"
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

# Хэндлер регистрации
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

# Имя для регистрации
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

# Номер для регистрации
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

# Валидация номера телефона
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

# Проверка регистрации пользователя
def is_user_registered(phone: str) -> bool:
    try:
        with Session(bind=engine) as session:
            # Ищем номер в таблице клиентов
            return session.query(Clients).filter(Clients.phone == phone).first() is not None
    except Exception as e:
        print(f"Ошибка проверки пользователя: {e}")
        return False

# Обработчик запроса бронирования
@bot.callback_query_handler(func=lambda call: call.data.startswith("reserve_"))
def handle_reservation(call):
    post_id = int(call.data.split("_", 1)[1])
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
        # Проверяем, есть ли пользователь уже в очереди
        with Session(bind=engine) as session:
            user_in_queue = session.query(TempReservations).filter(
                TempReservations.user_id == user_id,
                TempReservations.post_id == post_id,
                TempReservations.temp_fulfilled == False
            ).first()

            if user_in_queue:
                bot.answer_callback_query(
                    callback_query_id=call.id,
                    text="Вы уже стоите в очереди за этим товаром!",
                    show_alert=True,
                )
                return

            # Добавляем в очередь, если не стоит
            TempReservations.insert(
                user_id=user_id,
                quantity=1,  # Количество можно передавать динамически
                post_id=post_id,
                temp_fulfilled=False,
            )
            bot.answer_callback_query(
                callback_query_id=call.id,
                text="Вы добавлены в очередь на этот товар.",
                show_alert=True,
            )
        return

    # Если товар доступен для бронирования
    bot.answer_callback_query(
        callback_query_id=call.id,
        text="Ваш товар успешно забронирован.",
        show_alert=False,
    )

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
        f"Цена: {price} ₽\nОписание: {description}\nОстаток: {new_quantity}"
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
        caption = f"Цена: {post.price} ₽\nОписание: {post.description}\nСтатус: {status}"
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

# Отображает список заказов
@bot.callback_query_handler(func=lambda call: call.data == "my_orders")
def show_my_orders(call):
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
            keyboard.add(InlineKeyboardButton(text="На канал", url="https://t.me/MegaSkidkiTg"))
            new_message = bot.send_message(
                chat_id=user_id,
                text="У вас пока нет заказов. Начните покупки, перейдя на наш канал.",
                reply_markup=keyboard,
            )
            user_last_message_id[user_id] = new_message.message_id
    except Exception as ex:
        print(f"Ошибка в обработке команды '🛒 Мои заказы': {ex}")
# Обработчик функции 🚗 Заказы в доставке
@bot.message_handler(func=lambda message: message.text == "🚗 Заказы в доставке")
def show_delivery_orders(message):
    user_id = message.chat.id  # Идентификатор клиента
    try:
        # Получаем заказы конкретного клиента
        delivery_orders = InDelivery.get_all_rows()  # Получение всех заказов из базы
        user_orders = [order for order in delivery_orders if order.user_id == user_id]  # Фильтрация заказов по user_id

        # Если у клиента нет заказов
        if not user_orders:
            bot.send_message(
                message.chat.id,
                "У вас нет текущих заказов в доставке."
            )
            return

        # Формируем текст для клиента
        text = "🚚 Ваши заказы в доставке:\n\n"
        total_sum = 0
        for order in user_orders:
            text += f"🔹 Описание: {order.item_description}\n"
            text += f"🔹 Количество: {order.quantity}\n"
            text += f"💵 Сумма: {order.total_sum} ₽\n"
            text += "-" * 30 + "\n"
            total_sum += order.total_sum

        text += f"\n💰 Общая сумма ваших заказов в доставке: {total_sum} ₽"
        bot.send_message(message.chat.id, text)
    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка при загрузке заказов: {str(e)}"
        )

def confirm_delivery():
    """
    Перемещает обработанные заказы в in_delivery.
    """
    try:
        # Получаем всех клиентов, ожидающих доставки
        for_delivery_rows = ForDelivery.get_all_rows()

        for row in for_delivery_rows:
            user_id = row.user_id

            # Получаем обработанные на тот момент заказы пользователя
            reservations = Reservations.get_row_by_user_id(user_id)
            fulfilled_orders = [r for r in reservations if r.is_fulfilled]

            # Перемещаем обработанные заказы в in_delivery
            for order in fulfilled_orders:
                InDelivery.insert(
                    user_id=row.user_id,
                    item_description="Товар",  # Заполнить описанием из Posts
                    quantity=order.quantity,
                    total_sum=row.total_sum,
                    delivery_address=row.address
                )

            # После перемещения можно удалить из for_delivery
            ForDelivery.delete_all_rows()

        print("Все обработанные заказы перемещены в in_delivery.")
    except Exception as e:
        raise Exception(f"Ошибка при подтверждении доставки: {e}")



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

        # Проверяем очередь пользователей на этот товар
        with Session(bind=engine) as session:
            next_in_queue = session.query(TempReservations).filter(
                TempReservations.post_id == order.post_id,
                TempReservations.temp_fulfilled == False
            ).order_by(TempReservations.created_at).first()  # Берём первого из очереди

            if next_in_queue:
                # Если очередь не пуста: добавляем товар следующему пользователю
                Reservations.insert(
                    user_id=next_in_queue.user_id,
                    post_id=order.post_id,
                    quantity=1,
                    is_fulfilled=False
                )

                # Отмечаем, что очередь пользователя выполнена
                next_in_queue.temp_fulfilled = True
                session.commit()

                # Уведомляем пользователя из очереди
                bot.send_message(
                    chat_id=next_in_queue.user_id,
                    text="Ваш товар в очереди стал доступен и добавлен в вашу корзину."
                )

                # Если товар передан из очереди, НЕ увеличиваем количество на канале
                bot.answer_callback_query(call.id, "Вы успешно отказались от товара. Он передан следующему в очереди.",
                                          show_alert=False)
                # Перенаправляем пользователя в меню "Мои заказы"
                my_orders(call.message)
                return  # Завершаем обработку, дальше ничего не делаем

        # Если никто не в очереди, увеличиваем количество товара (товар вернётся на канал)
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
                f"Цена: {post.price} ₽\n"
                f"Описание: {post.description}\n"
                f"Остаток: {new_quantity}"
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

@bot.callback_query_handler(func=lambda call: call.data.startswith("enqueue_"))
def handle_enqueue(call):
    user_id = call.message.chat.id
    post_id = int(call.data.split("_")[1])

    # Проверяем, существует ли запись уже в TempReservations
    with Session(bind=engine) as session:
        existing_entry = session.query(TempReservations).filter(
            TempReservations.user_id == user_id,
            TempReservations.post_id == post_id,
            TempReservations.temp_fulfilled == False
        ).first()

        if existing_entry:
            return

    # Добавляем в таблицу TempReservations
    TempReservations.insert(user_id=user_id, quantity=1, post_id=post_id, temp_fulfilled=False)
    bot.send_message(user_id, "Вы добавлены в очередь. Как только товар станет доступен, вы будете уведомлены.")

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
    if role not in ["supreme_leader", "admin"]:
        bot.send_message(user_id, f"У вас нет прав доступа к этой функции. Ваша роль: {role}")
        return

    try:
        # Получение всех резерваций
        reservations = Reservations.get_row_all()
        if not reservations:
            bot.send_message(user_id, "Нет забронированных товаров для отправки.")
            return

        # Фильтруем необработанные резервации
        reservations_to_send = [r for r in reservations if not r.is_fulfilled]
        if not reservations_to_send:
            bot.send_message(user_id, "Все текущие товары уже были обработаны.")
            return

        # Группируем заказы по user_id и post_id, суммируя количество
        grouped_orders = defaultdict(lambda: {"quantity": 0, "reservations": []})
        for reservation in reservations_to_send:
            key = (reservation.user_id, reservation.post_id)
            grouped_orders[key]["quantity"] += reservation.quantity
            grouped_orders[key]["reservations"].append(reservation)

        # Обрабатываем и отправляем сгруппированные заказы
        for (user_id, post_id), group in grouped_orders.items():
            try:
                quantity = group["quantity"]
                reservations = group["reservations"]

                # Получение данных о посте
                post_data = Posts.get_row(post_id)
                if not post_data:
                    bot.send_message(
                        user_id, f"⚠️ Пост с ID {post_id} не найден. Пропускаем.")
                    continue

                # Получение данных о клиенте
                client_data = Clients.get_row(user_id)
                if not client_data:
                    bot.send_message(
                        user_id, f"⚠️ Клиент с ID {user_id} не найден. Пропускаем.")
                    continue

                # Формируем описание заказа
                caption = (
                    f"💼 Новый заказ:\n\n"
                    f"👤 Клиент: {client_data.name or 'Имя не указано'}\n"
                    f"📞 Телефон: {client_data.phone or 'Телефон не указан'}\n"
                    f"💰 Цена: {post_data.price or 'Не указана'}₽\n"
                    f"📦 Описание: {post_data.description or 'Описание отсутствует'}\n"
                    f"📅 Дата: {post_data.created_at.strftime('%d.%m') if post_data.created_at else 'Дата отсутствует'}\n"
                    f"📦 Количество: {quantity}"
                )

                # Создаем кнопку
                markup = InlineKeyboardMarkup()
                mark_button = InlineKeyboardButton(
                    text=f"✅ Положил {quantity} шт.",
                    callback_data=f"mark_fulfilled_group_{user_id}_{post_id}",
                )
                markup.add(mark_button)

                # Отправляем сообщение
                if post_data.photo:
                    bot.send_photo(
                        chat_id=TARGET_GROUP_ID,
                        photo=post_data.photo,
                        caption=caption,
                        reply_markup=markup,
                    )
                else:
                    bot.send_message(
                        chat_id=TARGET_GROUP_ID, text=caption, reply_markup=markup
                    )


            except Exception as e:
                bot.send_message(
                    user_id, f"⚠️ Ошибка при обработке заказа: {e}")
                print(f"⚠️ Ошибка при обработке заказа: {e}")



    except Exception as global_error:
        bot.send_message(user_id, f"Произошла ошибка: {global_error}")
        print(f"❌ Глобальная ошибка в send_all_reserved_to_group: {global_error}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_group_"))
def mark_fulfilled_group(call):
    user_id = call.from_user.id
    role = get_client_role(user_id)

    # Проверка роли
    if role not in ["admin", "supreme_leader"]:
        bot.answer_callback_query(
            call.id, "У вас нет прав доступа к этой функции.", show_alert=True
        )
        return

    try:
        # Извлекаем информацию из callback_data
        _, target_user_id, post_id = call.data.split("_")[2:]
        target_user_id = int(target_user_id)
        post_id = int(post_id)

        # Получаем все необработанные резервации клиента для данного поста
        with Session(bind=engine) as session:
            reservations = (
                session.query(Reservations)
                .filter_by(user_id=target_user_id, post_id=post_id, is_fulfilled=False)
                .all()
            )
            if not reservations:
                bot.answer_callback_query(
                    call.id, "Резервации уже обработаны.", show_alert=True)
                return

            # Обновляем все резервации как обработанные
            for reservation in reservations:
                reservation.is_fulfilled = True
                session.merge(reservation)
            session.commit()

        # Имя пользователя, который нажал кнопку
        user_first_name = call.from_user.first_name or "Администратор"
        user_username = call.from_user.username
        user_full_name = (
            f"{user_first_name} (@{user_username})"
            if user_username
            else user_first_name
        )

        # Формируем обновлённый текст
        updated_text = (
            f"{call.message.caption or call.message.text}\n\n"
            f"✅ Этот заказ теперь обработан.\n"
            f"👤 Кто положил: {user_full_name}"
        )

        # Проверяем, содержит ли сообщение фотографию
        if call.message.photo:
            bot.edit_message_caption(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                caption=updated_text,
            )
        else:
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=updated_text,
            )

        bot.answer_callback_query(call.id, "Резервация успешно обработана!")
        # Задержка секунда перед отправкой следующего поста
        time.sleep(4)
    except Exception as e:
        bot.answer_callback_query(call.id, f"Ошибка: {e}", show_alert=True)
        print(f"Ошибка в обработчике mark_fulfilled_group: {e}")

# Херня чтобы не могли отказаться от товара который уже в пакете
@bot.callback_query_handler(func=lambda call: call.data.startswith("mark_fulfilled_"))
def mark_fulfilled(call):
    # Проверка администратора
    user_id = call.from_user.id
    role = get_client_role(user_id)  # Проверяем роль пользователя

    if role not in ["admin", "supreme_leader"]:
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
    if role not in ["admin","supreme_leader"]:
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

# Проверка на регистрацию(стэйты статуса)
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
    if role not in ["admin","supreme_leader"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    # Создаем клавиатуру с кнопками
    markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
    markup.add("Удалить клиента по номеру телефона", "Просмотреть корзину", "Управление доставкой", "⬅️ Назад")
    bot.send_message(message.chat.id, "Выберите действие:", reply_markup=markup)

# Обработчик нажатия на кнопку "Просмотреть корзину"
@bot.message_handler(func=lambda message: message.text == "Просмотреть корзину")
def request_phone_last_digits(message):
    bot.send_message(
        message.chat.id,
        "Введите последние 4 цифры номера телефона клиента:",
    )
    set_user_state(message.chat.id, "AWAITING_PHONE_LAST_4")


# @bot.message_handler(func=lambda message: message.text == "Управление доставкой")

@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "AWAITING_PHONE_LAST_4")
def handle_phone_input(message):
    input_text = message.text.strip()

    # Проверяем, что введены последние 4 цифры номера телефона
    if not input_text.isdigit() or len(input_text) != 4:
        bot.send_message(
            message.chat.id,
            "Введите корректные последние 4 цифры номера телефона (4 цифры).",
        )
        return

    # Показ корзины по последним 4 цифрам номера телефона
    show_cart_by_last_phone_digits(message, input_text)


def show_cart_by_last_phone_digits(message, last_4_digits):
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
        # Показываем содержимое корзины и добавляем кнопку "Расформировать"
        send_cart_content(message.chat.id, reservations, client.user_id)

    clear_user_state(message.chat.id)


def send_cart_content(chat_id, reservations, user_id):
    """Отображает содержимое корзины и добавляет кнопку для расформирования обработанных товаров"""
    for reservation in reservations:
        post = Posts.get_row_by_id(reservation.post_id)

        if post:
            # Отправляем фото и информацию о товаре
            if post.photo:
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
                bot.send_message(
                    chat_id,
                    f"Описание: {post.description}\n"
                    f"Количество: {reservation.quantity}\n"
                    f"Статус: {'Выполнено' if reservation.is_fulfilled else 'В ожидании'}",
                )
        else:
            bot.send_message(chat_id, f"Товар с ID {reservation.post_id} не найден!")

    # Добавляем кнопку "Расформировать обработанные"
    markup = types.InlineKeyboardMarkup()
    markup.add(types.InlineKeyboardButton("Расформировать обработанные", callback_data=f"clear_processed_{user_id}"))
    bot.send_message(chat_id, "Выберите действие:", reply_markup=markup)


# Callback для кнопки "Расформировать обработанные"
@bot.callback_query_handler(func=lambda call: call.data.startswith("clear_processed_"))
def handle_clear_processed(call):
    user_id = int(call.data.split("_")[2])  # Извлекаем ID пользователя из callback_data

    # Удаляем только обработанные товары пользователя
    cleared_items = clear_processed(user_id)

    if cleared_items > 0:
        bot.send_message(call.message.chat.id,
                         f"Все обработанные товары (количество: {cleared_items}) были удалены из корзины.")
    else:
        bot.send_message(call.message.chat.id, "У пользователя нет обработанных товаров для удаления.")


def clear_processed(user_id):
    """Удаляет обработанные товары из корзины пользователя"""
    # Получаем содержимое корзины пользователя
    reservations = Reservations.get_row_by_user_id(user_id)

    # Фильтруем только выполненные (обработанные) товары
    processed_items = [item for item in reservations if item.is_fulfilled]

    # Удаляем обработанные товары из БД
    for item in processed_items:
        Reservations.delete_row(item.id)

    # Возвращаем количество удаленных товаров
    return len(processed_items)


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

# Удаление клиента по номеру телефона
@bot.message_handler(func=lambda message: message.text == "Удалить клиента по номеру телефона")
def delete_client_by_phone(message):
    user_id = message.chat.id
    role = get_client_role(message.chat.id)
    # Проверяем, является ли пользователь администратором
    if role not in ["admin","supreme_leader"]:
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
    if role not in ["admin","supreme_leader"]:
        bot.send_message(user_id, "У вас недостаточно прав.")
        return

    phone = message.text.strip()  # Убираем лишние пробелы

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
    role = get_client_role(user_id)  # Предполагается, что эта функция получает роль из Clients
    print(f"Роль пользователя {user_id}: {role}")  # Отладочный вывод роли пользователя
    return role and "admin" in role  # Если роль хранится как строка или список

def is_leader(user_id):
    """Проверяет, является ли пользователь администратором."""
    role = get_client_role(user_id)  # Предполагается, что эта функция получает роль из Clients
    print(f"Роль пользователя {user_id}: {role}")  # Отладочный вывод роли пользователя
    return role and "supreme_leader" in role  # Если роль хранится как строка или список

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

    if role not in ["worker", "admin","supreme_leader"]:
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
    if role not in ["worker", "admin","supreme_leader"]:
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
    message_id = message.message_id  # ID самого запроса

    # Удаляем запрос пользователя сразу же
    try:
        bot.delete_message(chat_id=user_id, message_id=message_id)
    except Exception as e:
        print(f"Не удалось удалить сообщение-запрос пользователя {user_id}: {e}")

    role = get_client_role(user_id)

    # Проверяем, имеет ли пользователь соответствующую роль
    if role not in ["admin", "worker", "supreme_leader"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Убедимся, что user_last_message_id[user_id] - это список
    if user_id not in user_last_message_id:
        user_last_message_id[user_id] = []
    elif not isinstance(user_last_message_id[user_id], list):
        user_last_message_id[user_id] = [user_last_message_id[user_id]]

    # Удаляем предыдущие сообщения, если они есть
    for msg_id in user_last_message_id[user_id]:
        try:
            bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception as e:
            print(f"Не удалось удалить сообщение {msg_id} для пользователя {user_id}: {e}")

    # Очищаем список сообщений пользователя после удаления
    user_last_message_id[user_id] = []

    try:
        # Получаем все посты, которые ещё не были отправлены на канал
        posts = Posts.get_unsent_posts()  # Используем метод get_unsent_posts для фильтрации
    except Exception as e:
        error_msg = bot.send_message(user_id, f"Ошибка получения постов: {e}")
        user_last_message_id[user_id].append(error_msg.message_id)
        return

    if not posts:
        no_posts_msg = bot.send_message(user_id, "Нет доступных постов.")
        user_last_message_id[user_id].append(no_posts_msg.message_id)
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
                msg = bot.send_photo(
                    chat_id=user_id,
                    photo=photo,
                    caption=f"**Пост #{post_id}:**\n"
                            f"📍 *Описание:* {description}\n"
                            f"💰 *Цена:* {price} ₽\n"
                            f"📦 *Количество:* {quantity}",
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
            else:
                msg = bot.send_message(
                    chat_id=user_id,
                    text=f"**Пост #{post_id}:**\n"
                         f"📍 *Описание:* {description}\n"
                         f"💰 *Цена:* {price} ₽\n"
                         f"📦 *Количество:* {quantity}",
                    parse_mode="Markdown",
                    reply_markup=markup,
                )
            # Сохраняем ID отправленных сообщений
            user_last_message_id[user_id].append(msg.message_id)
        except Exception as e:
            error_msg = bot.send_message(user_id, f"Ошибка при отправке поста #{post_id}: {e}")
            user_last_message_id[user_id].append(error_msg.message_id)

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_post_"))
def edit_post(call):
    post_id = int(call.data.split("_")[2])
    user_id = call.from_user.id

    # Проверяем права на редактирование
    role = get_client_role(user_id)
    if role not in ["admin", "worker", "supreme_leader"]:
        bot.answer_callback_query(
            callback_query_id=call.id,
            text="У вас нет прав доступа к этой функции.",
            show_alert=True,
        )
        return

    # Сохраняем ID поста, который пользователь хочет редактировать
    temp_post_data[user_id] = {"post_id": post_id}

    # Клавиатура с вариантами редактирования
    markup = InlineKeyboardMarkup()
    edit_price_btn = InlineKeyboardButton("💰 Цена", callback_data=f"edit_price_{post_id}")
    edit_description_btn = InlineKeyboardButton("📍 Описание", callback_data=f"edit_description_{post_id}")
    edit_quantity_btn = InlineKeyboardButton("📦 Количество", callback_data=f"edit_quantity_{post_id}")
    markup.add(edit_price_btn, edit_description_btn, edit_quantity_btn)

    # Отправляем сообщение с инлайн-кнопками и сохраняем его ID
    if call.message.text:
        msg = bot.edit_message_text(
            chat_id=call.message.chat.id,
            message_id=call.message.message_id,
            text="Что вы хотите поменять?",
            reply_markup=markup
        )
        user_last_message_id[user_id].append(call.message.message_id)  # Сохраняем ID сообщения в список
    else:
        msg = bot.send_message(
            chat_id=call.message.chat.id,
            text="Что вы хотите поменять?",
            reply_markup=markup
        )
        user_last_message_id[user_id].append(msg.message_id)  # Сохраняем ID нового сообщения

@bot.callback_query_handler(func=lambda call: call.data.startswith("edit_"))
def handle_edit_choice(call):
    print(f"Получено callback_data: {call.data}")  # Логирование данных

    try:
        data_parts = call.data.split("_")  # Разделяем строку
        if len(data_parts) == 2:  # Для команд без ID (например, "edit_address")
            action = data_parts[0]  # Действие (edit)
            target = data_parts[1]  # Цель (address)

            if action == "edit" and target == "address":
                # Переход в состояние изменения адреса
                set_user_state(call.from_user.id, "EDITING_ADDRESS")
                bot.send_message(chat_id=call.from_user.id, text="Введите новый адрес:")
            else:
                bot.send_message(chat_id=call.from_user.id, text="Неизвестная команда.")
        elif len(data_parts) == 3:  # Для команд с ID (например, "edit_post_123")
            action = data_parts[0]
            target = data_parts[1]
            post_id = int(data_parts[2])  # ID поста

            if action == "edit" and target == "post":
                bot.send_message(chat_id=call.from_user.id, text=f"Вы выбрали редактирование поста с ID {post_id}")
            else:
                bot.send_message(chat_id=call.from_user.id, text="Неизвестная команда.")
        else:
            raise ValueError("Неверный формат callback_data")

    except ValueError as e:
        bot.send_message(chat_id=call.from_user.id, text="Ошибка: Неверный формат команды.")
        print(f"Ошибка обработки команды: {e}")
    except Exception as e:
        bot.send_message(chat_id=call.from_user.id, text="Произошла ошибка при обработке вашего выбора.")
        print(f"Общая ошибка: {e}")

# Обработчики для разных типов редактирования
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.EDITING_POST_PRICE)
def edit_post_price(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id]["post_id"]

    # Проверяем, что цена введена корректно
    if not message.text.isdigit():
        bot.send_message(user_id, "Ошибка: Цена должна быть числом. Попробуйте снова.")
        return

    new_price = int(message.text)
    temp_post_data[user_id]["price"] = new_price

    try:
        # Получаем текущие данные поста, чтобы сохранить их значения для необновляемых полей
        post = Posts.get_row_by_id(post_id)  # Метод для получения поста по ID
        success, msg = Posts.update_row(
            post_id=post_id,
            price=new_price,
            description=post.description,  # Оставляем остальные поля без изменений
            quantity=post.quantity
        )
        if success:
            bot.send_message(user_id, "Цена успешно обновлена!")
        else:
            bot.send_message(user_id, f"Ошибка обновления цены: {msg}")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка обновления цены: {e}")
    finally:
        clear_user_state(user_id)  # Сбрасываем состояние пользователя


@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.EDITING_POST_DESCRIPTION)
def edit_post_description(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id]["post_id"]

    new_description = message.text
    temp_post_data[user_id]["description"] = new_description

    try:
        # Получаем текущие данные поста
        post = Posts.get_row_by_id(post_id)
        success, msg = Posts.update_row(
            post_id=post_id,
            price=post.price,  # Сохраняем текущие значения для других полей
            description=new_description,
            quantity=post.quantity
        )
        if success:
            bot.send_message(user_id, "Описание успешно обновлено!")
        else:
            bot.send_message(user_id, f"Ошибка обновления описания: {msg}")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка обновления описания: {e}")
    finally:
        clear_user_state(user_id)


@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == UserState.EDITING_POST_QUANTITY)
def edit_post_quantity(message):
    user_id = message.chat.id
    post_id = temp_post_data[user_id]["post_id"]

    # Проверяем, что количество введено корректно
    if not message.text.isdigit():
        bot.send_message(user_id, "Ошибка: Количество должно быть числом. Попробуйте снова.")
        return

    new_quantity = int(message.text)
    temp_post_data[user_id]["quantity"] = new_quantity

    try:
        # Получаем текущие данные поста
        post = Posts.get_row_by_id(post_id)
        success, msg = Posts.update_row(
            post_id=post_id,
            price=post.price,  # Сохраняем текущие значения для остальных полей
            description=post.description,
            quantity=new_quantity
        )
        if success:
            bot.send_message(user_id, "Количество успешно обновлено!")
        else:
            bot.send_message(user_id, f"Ошибка обновления количества: {msg}")
    except Exception as e:
        bot.send_message(user_id, f"Ошибка обновления количества: {e}")
    finally:
        clear_user_state(user_id)


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
    try:
        print(f"Проверка роли пользователя: {message.chat.id}")  # Логирование для отладки

        # Проверяем, является ли пользователь администратором
        if is_admin(message.chat.id):
            markup = admin_main_menu()  # Получаем меню администратора
            bot.send_message(
                message.chat.id, "Возвращаюсь в главное меню.", reply_markup=markup
            )
        else:
            markup = client_main_menu()  # Получаем меню клиента
            bot.send_message(
                message.chat.id, "Возвращаюсь в главное меню.", reply_markup=markup
            )
    except Exception as e:
        print(f"Ошибка при обработке команды '⬅️ Назад': {e}")
        bot.send_message(
            message.chat.id, "Произошла ошибка. Пожалуйста, попробуйте снова позже."
        )


# Отправка в канал
@bot.message_handler(func=lambda message: message.text == "📢 Отправить посты в канал")
def send_new_posts_to_channel(message):
    user_id = message.chat.id
    role = get_client_role(user_id)

    # Проверяем, есть ли права на отправку постов
    if role not in ["admin","supreme_leader"]:
        bot.send_message(user_id, "У вас нет прав доступа к этой функции.")
        return

    # Получаем посты, которые ещё не были отправлены в канал
    posts = Posts.get_unsent_posts()

    if posts:
        for post in posts:
            post_id = post.id
            photo = post.photo
            price = post.price
            description = post.description
            quantity = post.quantity

            # Используем user_id из Posts, чтобы найти имя создателя поста в Clients
            creator_user_id = post.chat_id
            creator_name = Clients.get_name_by_user_id(creator_user_id) or "Неизвестный автор"

            # Формируем описание поста для канала
            caption = f"Цена: {price} ₽\nОписание: {description}\nОстаток: {quantity}"

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
                f"Пост был создан пользователем: {creator_name}\n\n{caption}"
            )
            bot.send_photo(ARCHIVE, photo=photo, caption=group_caption)

            # Обновляем статус публикации
            Posts.mark_as_sent(post_id=post_id, message_id=sent_message.message_id)

            # Задержка секунда перед отправкой следующего поста
            time.sleep(4)

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

# Статистика
@bot.message_handler(commands=['statistic'])
def handle_statistic(message):
    from datetime import datetime, timedelta

    today = datetime.now()
    monday = today - timedelta(days=today.weekday())
    days_range = {'today': (today.date(), today.date()), 'week': (monday.date(), today.date())}

    statistics = {"today": {}, "week": {}}

    # Получение данных из базы данных
    all_posts = Posts.get_row_all()  # Получаем все посты
    print("Содержимое all_posts:", all_posts)  # Проверяем посты

    all_clients = Clients.get_row_all()  # Получаем всех клиентов
    print("Содержимое all_clients:", all_clients)  # Проверяем клиентов

    # Преобразование клиентов в словарь {user_id: name}
    clients_dict = {}
    if not all_clients:
        print("Данные all_clients пусты или None!")
        clients_dict = {}
    elif isinstance(all_clients, dict):
        print("all_clients — это словарь. Преобразуем его в clients_dict.")
        clients_dict = {key: value.get("name", "Неизвестный пользователь") for key, value in all_clients.items()}
    elif isinstance(all_clients, list):
        if all(isinstance(client, dict) for client in all_clients):
            print("all_clients — это список словарей. Преобразуем в clients_dict.")
            clients_dict = {client["user_id"]: client.get("name", "Неизвестный пользователь") for client in all_clients}
        else:
            print("all_clients — это список объектов. Преобразуем в clients_dict.")
            clients_dict = {client.user_id: client.name for client in all_clients}
    else:
        raise TypeError(f"Unsupported data type for 'all_clients': {type(all_clients)}")

    print("Преобразованный clients_dict:", clients_dict)

    # Генерация статистики постов
    for key, date_range in days_range.items():
        for post in all_posts:
            created_at = post.created_at.date()
            print(f"Пост ID: {post.id}, chat_id: {post.chat_id}, created_at: {post.created_at}")

            if date_range[0] <= created_at <= date_range[1]:
                creator_name = clients_dict.get(post.chat_id, "Неизвестный пользователь")
                print(f"Создатель поста с chat_id {post.chat_id}: {creator_name}")

                if creator_name not in statistics[key]:
                    statistics[key][creator_name] = 0

                statistics[key][creator_name] += 1

    # Формирование текста ответа
    response = "📊 Статистика постов:\n"
    for period, names_data in statistics.items():
        period_label = "Сегодня" if period == "today" else "На этой неделе"
        response += f"\n{period_label}:\n"

        for name, count in names_data.items():
            response += f"  - {name}: {count} постов\n"

    if not statistics["today"] and not statistics["week"]:
        response = "Нет статистики по постам за сегодня или неделю."

    bot.send_message(message.chat.id, response)


# Обработчик для кнопки 'Отправить рассылку'.
@bot.message_handler(func=lambda message: message.text == "Отправить рассылку")
def send_broadcast(message):
    user_id = message.from_user.id

    bot.send_message(chat_id=user_id, text="Начинаю рассылку подходящим пользователям...")

    try:
        # Получаем список клиентов для рассылки
        eligible_users = calculate_for_delivery()

        if eligible_users:
            for user in eligible_users:
                print(f"Рассылка: User ID: {user['user_id']}, Name: {user['name']}, Final Sum: {user['final_sum']}")
                send_delivery_offer(bot, user["user_id"], user["name"])

            # bot.send_message(chat_id=user_id, text="Рассылка завершена!")
        else:
            bot.send_message(chat_id=user_id, text="Подходящих пользователей для рассылки не найдено.")
    except Exception as e:
        # Обработка ошибок
        bot.send_message(chat_id=user_id, text=f"Ошибка при выполнении рассылки: {str(e)}")

# Обрабатывает ответ пользователя на предложение доставки с инлайн-клавиатуры.
@bot.callback_query_handler(func=lambda call: call.data in ["yes", "no"])
def handle_delivery_response_callback(call):
    # Получаем данные пользователя
    user_id = call.from_user.id
    response = call.data  # Получаем "yes" или "no" из callback data

    # Проверяем, нажато ли согласие после 14:00(временно поменял на 23)
    current_time = datetime.now().time()  # Текущее локальное время
    if response == "yes" and current_time.hour >= 23:
        # Отказываем пользователю в доставке
        bot.send_message(chat_id=user_id,
                         text="Извините, но лист на доставку уже сформирован. Ожидайте следующую отправку.")
    elif response == "yes":
        # Если "Да" и время до 14:00, сообщаем пользователю, что мы ждём адрес
        bot.send_message(chat_id=user_id, text="Пожалуйста, укажите город, адрес и подъезд")
        # Сохраняем состояние пользователя для дальнейшего ввода адреса
        set_user_state(user_id, "WAITING_FOR_ADDRESS")  # Сохраняем состояние для обработки сообщения
    elif response == "no":
        # Если "Нет", уведомляем, что доставка будет предложена позже
        bot.send_message(chat_id=user_id, text="Оповестим вас при следующей доставке.")

    # Уведомляем Telegram, что callback обработан
    bot.answer_callback_query(call.id)

# Обрабатывает ввод адреса пользователя.
@bot.message_handler(func=lambda message: get_user_state(message.chat.id) == "WAITING_FOR_ADDRESS")
def handle_address_input(message):
    user_id = message.chat.id
    address = message.text  # Сохраняем введённый адрес

    # Выбираем данные пользователя для подтверждения
    user_data = Clients.get_row_by_user_id(user_id)
    if not user_data:
        bot.send_message(chat_id=user_id, text="Ошибка! Данные пользователя отсутствуют.")
        return

    name = user_data.name
    phone = user_data.phone
    final_sum = calculate_sum_for_user(user_id)  # Вычисляем сумму заказов (см. ниже)

    # Отправляем сообщение для подтверждения
    bot.send_message(
        chat_id=user_id,
        text=f"Ваши данные:\nИмя: {name}\nТелефон: {phone}\nСумма заказов: {final_sum}\nАдрес: {address}\n\nПодтверждаете?",
        reply_markup=keyboard_for_confirmation()  # Клавиатура "Подтвердить"/"Отменить"
    )

    # Сохраняем данные во временной памяти
    temp_user_data[user_id] = {
        "name": name,
        "phone": phone,
        "final_sum": final_sum,
        "address": address
    }

    # Переключаем состояние на ожидание подтверждения
    set_user_state(user_id, "WAITING_FOR_CONFIRMATION")

# Рассчитывает общую сумму заказов для указанного пользователя.
def calculate_sum_for_user(user_id):
    with Session(bind=engine) as session:
        result = session.query(
            func.sum(Posts.price - Reservations.return_order).label("final_sum")
        ).join(
            Reservations, Posts.id == Reservations.post_id
        ).filter(
            Reservations.user_id == user_id, Reservations.is_fulfilled == True
        ).first()

        return result.final_sum if result.final_sum else 0

# Клавиатура для подтверждения
def keyboard_for_confirmation():
    """
    Создаёт клавиатуру для подтверждения.
    """
    keyboard = InlineKeyboardMarkup()
    yes_button = InlineKeyboardButton("Да", callback_data="yesС")  # Callback для подтверждения
    no_button = InlineKeyboardButton("Нет", callback_data="noС")  # Callback для отмены
    keyboard.add(yes_button, no_button)
    return keyboard


def keyboard_for_editing():
    """
    Клавиатура для выбора изменений.
    """
    keyboard = InlineKeyboardMarkup()
    edit_address_button = InlineKeyboardButton("Изменить адрес", callback_data="edit_address")
    edit_phone_button = InlineKeyboardButton("Изменить номер", callback_data="edit_phone")
    cancel_button = InlineKeyboardButton("Отмена", callback_data="cancel_edit")
    keyboard.add(edit_address_button, edit_phone_button)
    keyboard.add(cancel_button)
    return keyboard


# Обработчик подтверждения или отклонения данных доставки.
@bot.callback_query_handler(func=lambda call: get_user_state(call.from_user.id) == "WAITING_FOR_CONFIRMATION")
def handle_confirmation(call):
    user_id = call.from_user.id
    confirmation = call.data  # "yesС" или "noС"

    if confirmation == "yesС":
        user_temp_data = temp_user_data.get(user_id)
        if user_temp_data:
            name = user_temp_data.get("name")
            phone = user_temp_data.get("phone")
            address = user_temp_data.get("address")
            final_sum = user_temp_data.get("final_sum")

            # Сохраняем данные в таблицу for_delivery
            ForDelivery.insert(
                user_id=user_id,
                name=name,
                phone=phone,
                address=address,
                total_sum=final_sum
            )

            # Подтверждаем заказ пользователю
            bot.send_message(
                chat_id=user_id,
                text=f"Спасибо! Ваш заказ доставят на указанный адрес:\nИмя: {name}\nТелефон: {phone}\nАдрес: {address}\nСумма заказов: {final_sum}."
            )

            del temp_user_data[user_id]
            set_user_state(user_id, None)  # Сбрасываем состояние
        else:
            bot.send_message(chat_id=user_id,
                             text="Ошибка! Временные данные пользователя отсутствуют. Попробуйте снова.")
            set_user_state(user_id, None)

    elif confirmation == "noС":
        # Если пользователь отклоняет, предлагать изменить данные
        bot.send_message(chat_id=user_id, text="Вы хотите изменить адрес или номер телефона?",
                         reply_markup=keyboard_for_editing())

    bot.answer_callback_query(call.id)  # Уведомляем Telegram, что callback обработан

@bot.message_handler(func=lambda message: message.text == "Архив доставки")
def archive_delivery_to_excel(message):
    """
    Формирует Excel-файл с архивом доставок из таблицы in_delivery,
    отправляет его в канал delivery_archive, и очищает таблицу.
    """
    # Получение всех данных из таблицы InDelivery
    delivery_rows = InDelivery.get_all_rows()

    # Проверка: если нет данных, завершить выполнение
    if not delivery_rows:
        bot.send_message(message.chat.id, "Нет данных для архивации.")
        return None

    # Создание Excel файла в памяти
    wb = Workbook()
    ws = wb.active
    ws.title = "Архив доставок"

    # Добавление заголовков таблицы
    ws.append(["Телефон", "Имя", "Общая сумма", "Адрес доставки", "Список товаров"])

    # Получение данных и заполнение строк
    for row in delivery_rows:
        # Получение информации о клиенте по user_id из таблицы Clients
        client_data = Clients.get_row_by_user_id(row.user_id)

        # Заполнение строки для таблицы
        ws.append([
            client_data.phone if client_data else "Неизвестно",
            client_data.name if client_data else "Неизвестно",
            row.total_sum,
            row.delivery_address,
            row.item_description
        ])

    # Сохранение файла в памяти
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)  # Перемещение курсора в начало файла

    # Указание имени файла через InputFile
    file_name = f"Архив_доставок_{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    document =  InputFile(output, file_name=file_name)

    # Отправка файла в канал delivery_archive
    bot.send_document(chat_id=delivery_archive, document=document)

    # Уведомление пользователя об отправке
    bot.send_message(message.chat.id, "Архив доставок отправлен в канал!")

    # Очистка таблицы in_delivery
    InDelivery.clear_table()

    # Уведомление об успешной очистке
    bot.send_message(message.chat.id, "Все записи из таблицы in_delivery удалены.")


# Обработчик редактирования данных
@bot.callback_query_handler(func=lambda call: call.data in ["edit_address", "edit_phone", "cancel_edit"])
def handle_editing(call):
    user_id = call.from_user.id
    action = call.data

    if action == "edit_address":
        # Устанавливаем состояние для редактирования адреса
        set_user_state(user_id, "EDITING_ADDRESS")
        bot.send_message(chat_id=user_id, text="Введите новый адрес:")
    elif action == "edit_phone":
        # Устанавливаем состояние для редактирования телефона
        set_user_state(user_id, "EDITING_PHONE")
        bot.send_message(chat_id=user_id, text="Введите новый номер телефона:")
    elif action == "cancel_edit":
        # Возврат пользователя в исходное состояние
        set_user_state(user_id, None)
        bot.send_message(chat_id=user_id, text="Изменения отменены.")

    bot.answer_callback_query(call.id)


# Обработчики ввода нового адреса или телефона
@bot.message_handler(func=lambda message: get_user_state(message.from_user.id) in ["EDITING_ADDRESS", "EDITING_PHONE"])
def handle_new_data_input(message):
    user_id = message.from_user.id
    current_state = get_user_state(user_id)

    if current_state == "EDITING_ADDRESS":
        # Сохраняем новый адрес во временных данных
        if user_id in temp_user_data:
            temp_user_data[user_id]["address"] = message.text
            bot.send_message(chat_id=user_id, text=f"Адрес обновлен: {message.text}")
        else:
            bot.send_message(chat_id=user_id, text="Ошибка! Временные данные отсутствуют.")
    elif current_state == "EDITING_PHONE":
        # Проверяем номер телефона
        if is_phone_valid(message.text):
            if user_id in temp_user_data:
                temp_user_data[user_id]["phone"] = message.text
                bot.send_message(chat_id=user_id, text=f"Номер телефона обновлен: {message.text}")
            else:
                bot.send_message(chat_id=user_id, text="Ошибка! Временные данные отсутствуют.")
        else:
            bot.send_message(chat_id=user_id, text="Некорректный номер телефона. Попробуйте снова.")

    set_user_state(user_id, "WAITING_FOR_CONFIRMATION")  # Возвращаем состояние подтверждения
    bot.send_message(chat_id=user_id, text="Пожалуйста, подтвердите данные.",
                     reply_markup=keyboard_for_confirmation())

# Клавиатура для доставки да или нет
def keyboard_for_delivery():
    """
        Создает новую inline-клавиатуру с кнопками "Да" и "Нет".
        """
    keyboard = InlineKeyboardMarkup()  # Создаем разметку для клавиатуры
    yes_button = InlineKeyboardButton(text="Да", callback_data="yes")  # Кнопка "Да"
    no_button = InlineKeyboardButton(text="Нет", callback_data="no")  # Кнопка "Нет"
    keyboard.add(yes_button, no_button)  # Добавляем кнопки в клавиатуру
    return keyboard

# Получение пользователей, у которых сумма заказов минус сумма возврата >= min_order_sum.
def get_eligible_users(min_order_sum=2000):
    # Вызываем calculate_processed_sum для получения обработанных заказов
    processed_sums = calculate_processed_sum()  # Например, словарь {user_id: total_processed_sum}

    with Session(bind=engine) as session:
        # Находим все суммы возвратов для пользователей
        returns_query = session.query(
            Reservations.user_id,
            func.sum(Reservations.return_order).label("total_return_sum")
        ).group_by(Reservations.user_id).all()

        # Преобразуем данные возвратов в словарь
        returns_dict = {row.user_id: row.total_return_sum for row in returns_query}

        # Формируем список пользователей для рассылки
        eligible_users = []
        for user_id, total_sum in processed_sums.items():
            total_returns = returns_dict.get(user_id, 0)  # Если возвратов нет, считаем их равными 0
            final_sum = total_sum - total_returns  # Итоговая сумма для пользователя

            # Оставляем тех, у кого итоговая сумма >= min_order_sum
            if final_sum >= min_order_sum:
                user_data = session.query(Clients).filter(Clients.user_id == user_id).first()
                if user_data:  # Если пользователь найден в Clients
                    eligible_users.append({
                        "user_id": user_id,
                        "name": user_data.name,
                        "phone": user_data.phone,
                        "final_sum": final_sum,
                    })

        return eligible_users

# Вычисляет общую сумму обработанных заказов клиента из Posts, минусуя возвраты (return_order),и возвращает список клиентов, сумма заказов которых превышает установленный порог.
def calculate_for_delivery(min_order_sum=2000):
    with Session(bind=engine) as session:
        # Создаем запрос для вычисления итоговых сумм заказов
        query = session.query(
            Reservations.user_id,
            Clients.name,
            Clients.phone,
            # Итоговая сумма: сумма заказов из Posts минус возвраты из Reservations
            (func.sum(Posts.price) - func.sum(Reservations.return_order)).label("final_sum")
        ).join(
            Clients, Reservations.user_id == Clients.user_id
        ).join(
            Posts, Reservations.post_id == Posts.id  # Соединяем с таблицей Posts через post_id
        ).filter(
            Reservations.is_fulfilled == True  # Только выполненные заказы
        ).group_by(
            Reservations.user_id, Clients.name, Clients.phone
        ).having(
            (func.sum(Posts.price) - func.sum(Reservations.return_order)) >= min_order_sum
        ).all()

        # Преобразуем результаты в удобный формат
        results = [
            {"user_id": row.user_id, "name": row.name, "phone": row.phone, "final_sum": row.final_sum}
            for row in query
        ]

        return results

# Отправка рассылки
def send_delivery_offer(bot, user_id, user_name):

    bot.send_message(
        chat_id=user_id,
        text=f"{user_name}, готовы ли Вы принять доставку завтра с 10:00 до 16:00?",
        reply_markup=keyboard_for_delivery()  # Используем новую клавиатуру
    )

# Обработка ответа пользователя на предложение доставки.
def handle_delivery_response(bot, user_id, response):
    if response.lower() == "да":
        bot.send_message(chat_id=user_id, text="Пожалуйста, укажите город, адрес и подъезд")
        # Здесь нужно сохранить состояние пользователя, чтобы дальше запросить данные.
        set_user_state(user_id, "WAITING_FOR_ADDRESS")
    else:
        bot.send_message(
            chat_id=user_id, text="Оповестим вас при следующей доставке."
        )

# Обработка ввода адреса пользователя.
def handle_address_input(bot, user_id, address):
    # Получение имени и телефона пользователя
    user_data = Clients.get_row_by_user_id(user_id)
    if user_data:
        name = user_data.name
        phone = user_data.phone

        # Отправка данных на подтверждение
        bot.send_message(
            chat_id=user_id,
            text=f"Проверьте данные:\nИмя: {name}\nТелефон: {phone}\nАдрес: {address}\n\nПодтвердите?",
            reply_markup=create_yes_no_keyboard()
        )

        # Сохранение состояния пользователя для подтверждения
        set_user_state(user_id, "WAITING_FOR_CONFIRMATION")
        temp_user_data[user_id] = {"address": address, "name": name, "phone": phone}
    else:
        bot.send_message(chat_id=user_id, text="Ошибка. Пользователь не найден.")

# Обработка подтверждения данных пользователя.
def handle_confirmation(bot, user_id, confirmation):
    if confirmation.lower() == "да":
        # Получение временных данных пользователя, которые он вводил
        user_temp_data = temp_user_data.get(user_id)
        if user_temp_data:
            # Вставка данных в таблицу ForDelivery
            ForDelivery.insert(
                user_id=user_id,
                name=user_temp_data["name"],
                phone=user_temp_data["phone"],
                address=user_temp_data["address"]
            )
            bot.send_message(chat_id=user_id, text="Ваш заказ успешно добавлен в доставку!")
        else:
            bot.send_message(chat_id=user_id, text="Ошибка. Повторите ввод данных.")
    else:
        bot.send_message(
            chat_id=user_id,
            text="Пожалуйста, укажите адрес или номер телефона заново."
        )
        set_user_state(user_id, "WAITING_FOR_ADDRESS")  # Вернуть состояние ожидания адреса

@bot.message_handler(func=lambda message: message.text == "✅ Подтвердить доставку")
def confirm_delivery(message):
    try:
        with Session(bind=engine) as session:
            # Получаем всех пользователей из ForDelivery
            for_delivery_rows = session.query(ForDelivery).all()

            if not for_delivery_rows:
                bot.send_message(
                    message.chat.id,
                    "❌ В списке ForDelivery никого нет для подтверждения доставки."
                )
                return

            # Перемещаем данные
            for row in for_delivery_rows:
                # Ищем связанные обработанные заказы в Reservations только для текущего пользователя
                reservations = session.query(Reservations).filter(
                    Reservations.user_id == row.user_id,
                    Reservations.is_fulfilled == True
                ).all()

                # Подсчитываем уникальные заказы для каждого пользователя
                total_sum = 0
                order_descriptions = []  # Добавляем описание всех товаров клиента

                for reservation in reservations:
                    # Получаем описание товара из Posts
                    post = session.query(Posts).filter(Posts.id == reservation.post_id).first()
                    item_description = post.description if post else "Неизвестный товар"

                    # Добавляем данные по количеству и описаниям
                    order_descriptions.append(f"{item_description} x{reservation.quantity}")
                    total_sum += reservation.quantity * (post.price if post else 0)  # Умножаем цену на количество

                    # Удаляем обработанные заказы из Reservations
                    session.delete(reservation)

                # Если заказы найдены, добавляем в InDelivery
                if order_descriptions:
                    new_delivery = InDelivery(
                        user_id=row.user_id,
                        item_description="\n".join(order_descriptions),  # Описание всех товаров
                        quantity=len(order_descriptions),  # Общее количество позиций
                        total_sum=total_sum if total_sum > 0 else row.total_sum,
                        # Если общий подсчет не удался, берем старое значение
                        delivery_address=row.address,
                    )
                    session.add(new_delivery)

            # Удаляем все записи из ForDelivery
            ForDelivery.delete_all_rows()

            # Сохраняем изменения
            session.commit()
            bot.send_message(
                message.chat.id,
                "✅ Все заказы успешно перемещены из ForDelivery в InDelivery!"
            )

    except Exception as e:
        bot.send_message(
            message.chat.id,
            f"❌ Ошибка при подтверждении доставки: {str(e)}"
        )
# Запуск бота
if __name__ == "__main__":
    bot.polling(none_stop=True)
    # pizdec
