import asyncio
import json
from os import getenv
from dotenv import load_dotenv
from re import sub, escape
from copy import deepcopy
from collections import deque
from datetime import datetime
from telethon import TelegramClient, events


CHANNELS_JSON = "channels.json"
SETTINGS_JSON = "settings.json"
STATE_JSON = "state.json"
TRANSLATE_JSON = "translate.json"

load_dotenv()
client = TelegramClient("user_session", getenv("API_ID"), getenv("API_HASH"))
client.parse_mode = "html"

try:
    with open(CHANNELS_JSON, "r", encoding="utf-8") as f:
        CHANNELS = {int(k): v for k, v in json.load(f).items()}
    with open(SETTINGS_JSON, "r", encoding="utf-8") as f:
        general_settings = json.load(f)
    with open(TRANSLATE_JSON, "r", encoding="utf-8") as f:
        TRANSLATION_DICT = json.load(f)
    with open(STATE_JSON, "r", encoding="utf-8") as f:
        client.state = json.load(f)
except FileNotFoundError:
    print("[ERROR] Файл json не знайдено.")
    raise
except json.JSONDecodeError:
    print("[ERROR] Помилка формату json.")
    raise

# Блок відновлення типу змінних після читання з json
for key in ("alarm_start_time", "last_message_time"):
    if isinstance(client.state.get(key), str):
        client.state[key] = datetime.fromisoformat(client.state[key])

for i in client.state["message_stack"]:
    if isinstance(i[0], str):
        i[0] = datetime.fromisoformat(i[0])
client.state["message_stack"] = deque(client.state["message_stack"], maxlen=4)

if isinstance(client.state["message_count"], str):
    client.state["message_count"] = int(client.state["message_count"])
# Кінець блоку відновлення типу змінних після читання з json

MAX_MESSAGE_ROWS = general_settings["max_message_rows"]
MESSAGE_TTL = general_settings["message_ttl"]
ALARM_START_KEYWORD = general_settings["alarm_start_keyword"]
ALARM_END_KEYWORD = general_settings["alarm_end_keyword"]
CONTINUE_SYMBOLS = general_settings["continue_symbols"]
TARGET_CHANNEL_ID = general_settings["target_channel_id"]  # Канал призначення


def correct_punctuation(text: str) -> str:
    """
    Виправляє пунктуацію в повідомленні.
    
    Args:
        text (str): Вхідний текст для обробки.
    
    Returns:
        str: Виправлений текст.
    """
    if text:
        text = sub(r"([.!?,;])", r"\1 ", text)
        text = sub(r"\s+([.!?,;])", r"\1", text)
        text = sub(r"\s+", " ", text)

    return text


def trunc_message(text: str, trunc_word: str, continue_symbols: set, max_message_rows = MAX_MESSAGE_ROWS) -> str:
    """
    Обрізає текст, починаючи з рядка, що містить trunc_word, і до рядка, 
    який не починається з символів із continue_symbols.
    
    Args:
        text (str): Вхідний текст для обробки.
        trunc_word (str): Слово, з якого починається обрізка.
        continue_symbols (set): Набір символів, які дозволяють продовжувати обробку.
    
    Returns:
        str: Обрізаний текст, без завершальних пробілів.
    """
    if not text:
        return ""
    if not trunc_word or len(text.split("\n")) <= max_message_rows or trunc_word not in text.lower():
        return text
    
    result_lines = []
    is_processing = False
    lines = text.split("\n")
    
    for line in lines:
        if is_processing:
            if line.strip() and line.strip()[0] not in continue_symbols:
                break
            result_lines.append(line)
            
        elif trunc_word in line.lower():
            result_lines.append(line)
            is_processing = True

    return "\n".join(result_lines).strip()


def translate_text(text: str, translate_dict: dict) -> str:
    """
    Перекладає повідомлення на українську, використовуючи словник.
    
    Args:
        text (str): Вхідний текст для обробки.
        translate_dict (dict): Словник з парами слів.
    
    Returns:
        str: Перекладений текст.
    """
    pattern = r'\b(' + '|'.join(escape(key) for key in translate_dict.keys()) + r')\b'
    return sub(pattern, lambda m: translate_dict[m.group()], text.lower()).capitalize()


def replace_text(text: str, replace_dict) -> str:
    """
    Замінює символи в повідомленні, використовуючи словник.
    
    Args:
        text (str): Вхідний текст для обробки.
        replace_dict (dict): Словник з парами слів/символів.
    
    Returns:
        str: Опрацьований текст.
    """
    if replace_dict:
        for key, value in replace_dict.items():
            if key in text:
                text = text.replace(key, value)
    
    return text


def calculate_length_hm(diff: datetime) -> tuple:
    """
    Перетворює секунди на години і хвилини.
    
    Args:
        diff (datetime): Різниця між двома datetime.
    
    Returns:
        tuple: Повертає години і хвилини.
    """
    total_secs = int(diff.total_seconds())
    return total_secs // 3600, (total_secs % 3600) // 60
    

def make_set(message: str, region_list=general_settings.get("region", [])) -> set:
    """
    Перетворює рядок на множину, використовуючи заданий масив назв населених пунктів.
    
    Args:
        message (str): Повідомлення.
    
    Returns:
        set: Множина з унікальними словами - назвами населених пунктів.
    """
    return {locality for locality in region_list if locality in message.lower()}


def is_similar(message1: str, message2: str, last_message_time:datetime, message_ttl=MESSAGE_TTL) -> bool:
    """
    Порівнює два повідомлення.
    
    Args:
        message1 (str): Повідомлення 1.
        message1 (str): Повідомлення 2.
        last_message_time (datetime): Час старішого повідомлення.
    
    Returns:
        bool: Повертає True, якщо співпало 70% слів чи більше.
    """
    if (datetime.now() - last_message_time).total_seconds() > 2 * message_ttl:
        print("[DEBUG] Попереднє повідомлення старе, тому перевірка на схожість далі не здійснюється.")
        return False
    
    print(f"[DEBUG] Порівнюємо 2 повідомлення:\n1) >>> {message1}\n2) >>> {message2}")
    set1 = make_set(message1)
    print(f"[DEBUG] Set 1:\n{set1}")
    set2 = make_set(message2)
    print(f"[DEBUG] Set 2:\n{set2}")
    if not set1 or not set2:
        print("Якийсь з set пустий. Зупиняємо порівняння!")
        return False
    
    match_rate = len(set1.intersection(set2)) / max(len(set1), len(set2)) # ділення на 0!

    return match_rate >= 0.7


def select_reason(message_stack: deque, time_now: datetime, message_ttl=MESSAGE_TTL) -> str:
    """
    Обирає найкоротшу причину зі стеку повідомлень.
    
    Args:
        message_stack (deque): Стек повідомлень.
        time_now (str): Поточний час.
    
    Returns:
        str: Повертає найкоротше повідомлення.
    """
    valid_reasons = [m[1] for m in message_stack if (time_now - m[0]).total_seconds() <= message_ttl]
    return min(valid_reasons, key=len, default="")


async def send_messages(messages_to_send: list) -> None:
    """
    Надсилає повідомлення відповідно до отриманого списку.
    
    Args:
        messages_to_send (list): Список словників з даними повідомлення.
    
    Returns:
        None.
    """
    for message in messages_to_send:

        target_channel_id = message.get("target_channel_id", TARGET_CHANNEL_ID)
        file = message.get("file", None)
        message_text = message.get("message_text", "<i>Помилка надсилання повідомлення</i>")
        silent = message.get("silent", False)

        try:
            if file:
                await client.send_file(target_channel_id, file=file, caption=message_text, silent=silent)
            else:
                await client.send_message(target_channel_id, message_text, silent=silent)
            print(f"[DEBUG] Було надіслане повідомлення:\n>>> {message_text} <<<")
        except Exception as e:
            print(f"[ERROR] Помилка відправки: {e}")


def process_text(message_text: str, config: dict) -> str:
    """
    Редагує текст повідомлення відповідно до прапорців каналу.
    
    Args:
        message_text (str): Текст повідомлення.
        config (dict): Словник з налаштуваннями каналу.
    
    Returns:
        message_text (str): Текст повідомлення.
    """
    if config.get("is_correct_punctuation", False): # Корекція пунктуації
        message_text = correct_punctuation(message_text)

    if config.get("is_translate", False): # Спеціальна обробка і переклад тексту
        message_text = translate_text(message_text, TRANSLATION_DICT)
    
    if config.get("is_delete_words", False): # Видалення слів відповідно до переліку
        for delete_word in config.get("delete_words", []):
            message_text = message_text.replace(delete_word, "")
        message_text = message_text.strip()

    if config.get("is_trunc_message", False) and len(message_text.split("\n")) > MAX_MESSAGE_ROWS: # Обрізання зайвої інформації
        message_text = trunc_message(message_text, config.get("trunc_word", ""), CONTINUE_SYMBOLS)

    return message_text


def format_other_reasons(message_stack, reason, now, max_message_rows=MAX_MESSAGE_ROWS, message_ttl=MESSAGE_TTL):
    """
    Формує рядок з іншими причинами, відформатованими як цитати.
    
    Args:
        message_stack (list): Список кортежів з часом і текстом повідомлень.
        reason (str): Причина, яку потрібно виключити.
        max_message_rows (int): Максимальна кількість рядків у повідомленні.
        message_ttl (int): Час життя повідомлення в секундах.
        now (datetime): Поточний час для порівняння.
    
    Returns:
        str: Відформатований рядок з іншими причинами.
    """
    other_reasons = "\n \n".join(
        f"<blockquote>{other_reason}</blockquote>"
        for other_reason in [
            m[1] for m in message_stack
            if m[1] != reason 
            and len(m[1].split("\n")) < 2 * max_message_rows 
            and (now - m[0]).total_seconds() <= 2 * message_ttl
        ]
    )
    return other_reasons


def save_state(state_copy: dict) -> None:
    """
    Зберігає поточний стан скрипта.

    Args:
        state_copy (dict): Копія стану скрипта.
    
    Returns:
        None.
    """
    for key in ("alarm_start_time", "last_message_time"):
        if hasattr(state_copy[key], "isoformat"):
            state_copy[key] = state_copy[key].isoformat()

    if "message_stack" in state_copy:
        for message in state_copy["message_stack"]:
            if len(message) > 0 and hasattr(message[0], "isoformat"):
                message[0] = message[0].isoformat()

    state_copy["message_stack"] = list(state_copy["message_stack"])
    
    with open(STATE_JSON, "w", encoding="utf-8") as f:
        json.dump(state_copy, f, indent=4)


def exception_handler(func):

    async def wrapper(event):
        try:
            return await func(event)
        except Exception as e:
            channel_name = "невідомий канал"
            if hasattr(event, 'chat_id') and event.chat_id in CHANNELS:
                channel_name = CHANNELS[event.chat_id].get('name', 'невідомий канал')
            
            print(f"[ERROR] [{datetime.now().strftime('%H:%M:%S')}] Помилка в обробці повідомлення з '{channel_name}': {e}")
    return wrapper


@client.on(events.NewMessage(chats=list(CHANNELS.keys())))
@exception_handler
async def handler(event):

    message_text = event.raw_text
    channel_id = event.chat_id

    config = CHANNELS.get(channel_id, {})

    keywords = config.get("keywords", [])
    trunc_word = config.get("trunc_word", "")
    name = config.get("name", "невідомий")
    url = config.get("url", "")
    is_filter_stop_words = config.get("is_filter_stop_words", False) 
    stop_length = config.get("stop_length", 0)
    stop_words = config.get("stop_words", [])
    is_silent = config.get("is_silent", False)
    is_save_for_alarm = config.get("is_save_for_alarm", False)
    is_forward_images = config.get("is_forward_images", False)
    is_alarm_source = config.get("is_alarm_source", False)
    is_read_only_when_alarm = config.get("is_read_only_when_alarm", False)

    state = client.state
    now = datetime.now()
    other_reasons = ""
    messages_to_send = []
    is_save_right_now = False   # Прапорець, який каже що треба зберегти стан прямо зараз

    print(f"\n[DEBUG] [{now.strftime('%H:%M:%S')}] Повідомлення з '{name}':\n{message_text or "* EMPTY *"}\n")

    if not message_text and not is_forward_images:
        return
    if is_read_only_when_alarm and not state["is_alarm"]:
        print("[INFO] Пропущене повідомлення з каналу, який відстежується тільки під час тривоги.")
        return
    
    if is_save_for_alarm and not state["is_alarm"] and len(message_text.split()) > 1: # Зберігаємо можливі причини тривоги в стек
        state["message_stack"].append([now, process_text(message_text, config)]) # Зберігаємо текст і час

    if state["is_show_next_event"] and is_alarm_source: # Якщо треба обов'язково показати наступне повідомлення
        state["is_show_next_event"] = False
        if (now - state["alarm_start_time"]).total_seconds() < MESSAGE_TTL:
            message_text = trunc_message(message_text, trunc_word, CONTINUE_SYMBOLS)
            messages_to_send.append({"message_text": f"<i>Ймовірна причина тривоги:</i>\n{message_text}\n(<i>{url}</i>)", "silent": True})

    for keyword in keywords:

        if keyword in message_text.lower():

            if is_filter_stop_words:
                if len(message_text) > stop_length or any(stop_word in message_text for stop_word in stop_words):
                    print(f"[DEBUG] Знайдено ключове слово '{keyword}', але повідомлення відфільтроване.")
                    break

            # Обробка тексту
            message_text = process_text(message_text, config)

            additional_message = ""

            # Блок опрацювання тривоги і відбою. Винести у функцію
            if is_alarm_source:

                if keyword == ALARM_START_KEYWORD:
                    message_text = replace_text(message_text, config.get("replace_words", {}))
                    state["is_alarm"] = True
                    state["alarm_start_time"] = now
                    # print(f"[DEBUG] Початок тривоги о {now.strftime('%H:%M:%S')}")

                    reason = select_reason(state["message_stack"], now)
                    if reason:
                        additional_message = f"\n<i>Ймовірна причина тривоги:\n{reason}</i>"
                        other_reasons = format_other_reasons(state["message_stack"], reason, now)
                    else:
                        state["is_show_next_event"] = True
                        additional_message = f"\n<i>Ймовірна причина тривоги не визначена.\nОчікуйте на причину в наступних повідомленнях.</i>"

                elif keyword == ALARM_END_KEYWORD:
                    message_text = replace_text(message_text, config.get("replace_words", {}))
                    state["is_alarm"] = False
                    hours, minutes = calculate_length_hm(now - state["alarm_start_time"])
                    additional_message = f"\n<i>Тривалість: {hours} г. {minutes} хв.</i>"

                message_text = f"<b>{message_text}</b>"
                is_save_right_now = True # Терміново зберігаємо стан, якщо ключове слово з каналу-джерела тривоги
            # Кінець блоку опрацювання тривоги і відбою

            # Додаємо мітку каналу-джерела
            message_text += f"\n<i>({url})</i>"      

            if not is_similar(message_text, state["last_message"], state["last_message_time"]):

                if is_forward_images and event.photo:
                    messages_to_send.append({"file": event.photo, "message_text": f"{message_text}{additional_message}", "silent": is_silent})
                else:
                    messages_to_send.append({"message_text": f"{message_text}{additional_message}", "silent": is_silent})
                print(f"[DEBUG] Знайдено ключове слово '{keyword}' — повідомлення надіслане.")

                if other_reasons:
                    messages_to_send.append({"message_text": f"Інші можливі причини тривоги:\n{other_reasons}", "silent": True})   
                
            else:
                print(f"[DEBUG] Повідомлення пропущене: '{message_text}' схоже на '{state["last_message"]}'.")

            if not is_alarm_source:
                state["last_message"] = message_text # Зберігаємо текст останнього надісланого повідомлення для майбутньої перевірки
                state["last_message_time"] = now
            state["message_count"] += 1
            print(f"[DEBUG] message_count = {state["message_count"]}")
            
            if state["message_count"] >= 10 or is_save_right_now:
                save_state(deepcopy(client.state))
                is_save_right_now = False
                state["message_count"] = 0

            break # Зупиняє перебір ключових слів, якщо було хоч одне співпадіння
    else:
        print(f"[INFO] Ключових слів не знайдено.")

    if messages_to_send:
        await send_messages(messages_to_send)


async def main():
    await client.start()
    print(f"[DEBUG] [{datetime.now().strftime('%H:%M:%S')}] Бот запущений.")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())


# === notes/todo ===
# TODO await client.pin_message(chat, message, notify=False)