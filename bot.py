import asyncio
import json
import re
from copy import deepcopy
from collections import deque
from datetime import datetime
from telethon import TelegramClient, events
from secret import api_id, api_hash

# Server closed the connection: [WinError 121] The semaphore timeout period has expired
# await client.pin_message(chat, message, notify=False)

# quick replace
# "🚨": "🔴🔴🔴",
# "🟢": "🟢🟢🟢"

client = TelegramClient("user_session", api_id, api_hash)
client.parse_mode = "html"
message_stack = deque(maxlen=4) # Стек для причин тривоги
message_count = 0

state_defaults = {
    "is_alarm": False,
    "is_show_next_event": False,
    "current_channel": None,
    "last_message": "",
    "alarm_start_time": datetime.now()
}

try:
    with open("channels.json", "r", encoding="utf-8") as f:
        CHANNELS = {int(k): v for k, v in json.load(f).items()}
    with open("dict.json", "r", encoding="utf-8") as f:
        TRANSLATION_DICT = json.load(f)
    with open("state.json", "r", encoding="utf-8") as f:
        client.state = json.load(f)
    with open("settings.json", "r", encoding="utf-8") as f:
        general_settings = json.load(f)
except FileNotFoundError:
    print("[ERROR] Файл не знайдено.")
    raise
except json.JSONDecodeError:
    print("[ERROR] Помилка формату json.")
    raise

client.state = {**state_defaults, **client.state}
print(f"[INFO] client.state = {client.state}")

for key in ("alarm_start_time",):
    if isinstance(client.state.get(key), str):
        client.state[key] = datetime.fromisoformat(client.state[key])

MAX_MESSAGE_ROWS = general_settings["max_message_rows"]
MESSAGE_TTL = general_settings["message_ttl"]
ALARM_START_KEYWORD = general_settings["alarm_start_keyword"]
ALARM_END_KEYWORD = general_settings["alarm_end_keyword"]
CONTINUE_SYMBOLS = general_settings["continue_symbols"]
TARGET_CHANNEL_ID = general_settings["target_channel_id"]  # Канал призначення


def correct_punctuation(raw_str: str) -> str:
    """
    Виправляє пунктуацію в повідомленні.
    
    Args:
        raw_str (str): Вхідний текст для обробки.
    
    Returns:
        str: Виправлений текст у нижньому регістрі.
    """
    if raw_str:
        raw_str = raw_str.strip().replace(" ,", ",")
        raw_str = raw_str.replace(",", ", ")
        raw_str = re.sub(r'\s+', ' ', raw_str)

    return raw_str.lower()


def trunc_message(raw_str: str, trunc_word: str, continue_symbols: set) -> str:
    """
    Обрізає текст, починаючи з рядка, що містить trunc_word, і до рядка, 
    який не починається з символів із continue_symbols.
    
    Args:
        raw_str (str): Вхідний текст для обробки.
        trunc_word (str): Слово, з якого починається обрізка.
        continue_symbols (set): Набір символів, які дозволяють продовжувати обробку.
    
    Returns:
        str: Обрізаний текст, без завершальних пробілів.
    """
    if not raw_str or not trunc_word:
        return ""
    
    result_lines = []
    is_processing = False
    lines = raw_str.split("\n")
    
    for line in lines:
        if is_processing:
            if line.strip() and line.strip()[0] not in continue_symbols:
                break
            result_lines.append(line)
            
        elif trunc_word in line.lower():
            result_lines.append(line)
            is_processing = True

    return "\n".join(result_lines).strip()


def replace_whole_words(text: str, translate_dict: dict) -> str:
    """
    Перекладає повідомлення, використовуючи словник.
    
    Args:
        text (str): Вхідний текст для обробки.
        translate_dict (dict): Словник з парами слів.
    
    Returns:
        str: Перекладений текст.
    """
    pattern = r'\b(' + '|'.join(re.escape(key) for key in translate_dict.keys()) + r')\b'
    return re.sub(pattern, lambda m: translate_dict[m.group()], text.lower())


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
    

def make_set(message: str) -> set:
    """
    Перетворює рядок на множину.
    
    Args:
        message (str): Повідомлення.
    
    Returns:
        set: Повертає множину з унікальними словами.
    """
    return set([word.lower().strip()[:9] for word in message.split()])


def is_similar(message1: str, message2: str) -> bool:
    """
    Порівнює два повідомлення.
    
    Args:
        message1 (str): Повідомлення 1.
        message1 (str): Повідомлення 2.
    
    Returns:
        bool: Повертає True, якщо співпало 50% слів чи більше.
    """
    set1 = make_set(message1)
    set2 = make_set(message2)
    match_rate = len(set1.intersection(set2)) / max(len(set1), len(set2))

    return match_rate >= 0.5


def select_reason(message_stack: deque, base_time: datetime, message_ttl=MESSAGE_TTL) -> str:
    """
    Обирає найкоротшу причину зі стеку повідомлень.
    
    Args:
        message_stack (deque): Стек повідомлень.
        base_time (str): Час, від .
    
    Returns:
        str: Повертає найкоротше повідомлення.
    """
    valid_reasons = [m[1] for m in message_stack if (base_time - m[0]).total_seconds() <= message_ttl]
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
        message_text = message.get("message_text", "_Помилка надсилання повідомлення_")
        silent = message.get("silent", False)

        try:
            if file:
                print("[INFO] Зараз будемо надсилати повідомлення з картинкою.")
                await client.send_file(target_channel_id, file=file, caption=message_text, silent=silent)
            else:
                print(f"[INFO] Зараз будемо надсилати просте повідомлення:\n{message_text}")
                await client.send_message(target_channel_id, message_text, silent=silent)
        except Exception as e:
            print(f"[ERROR] Помилка відправки: {e}")


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

    global message_stack
    global message_count

    message_text = event.raw_text
    channel_id = event.chat_id
    messages_to_send = []

    config = CHANNELS.get(channel_id, {})
    keywords = config.get("keywords", [])
    trunc_word = config.get("truncword", "")
    name = config.get("name", "невідомий")
    url = config.get("url", "")
    is_filter_stopwords = config.get("isfilterstopwords", False) 
    stop_length = config.get("stoplength", 0)
    stopwords = config.get("stopwords", [])
    deletewords = config.get("deletewords", [])
    is_silent = config.get("issilent", False)
    is_save_for_alarm = config.get("issaveforalarm", False)
    is_forward_images = config.get("isforwardimages", False)
    is_trunc_message = config.get("istruncmessage", False)
    is_correct_punctuation = config.get("iscorrectpunctuation", False)
    is_alarm_source = config.get("isalarmsource", False)
    is_translate = config.get("istranslate", False)
    is_delete_words = config.get("isdeletewords", False)

    state = client.state
    now = datetime.now()
    other_reasons = ""

    print(f"\n[INFO] [{now.strftime('%H:%M:%S')}] Повідомлення з '{name}':\n{message_text or "[EMPTY]"}\n")

    if not message_text and not is_forward_images:
        print("[INFO] Повідомлення пусте і не треба пересилати картинку - не обробляємо далі!")
        return
    
    if is_save_for_alarm and not state["is_alarm"]: # Спеціальна обробка каналів-джерел інформації про тривогу
        if is_trunc_message and trunc_word in message_text: # Якщо раптом повідомлення можна обрізати - то обрізаємо
            message_text = trunc_message(message_text, trunc_word, CONTINUE_SYMBOLS)
        message_stack.append([now, message_text]) # Зберігаємо текст і час
        print(f"[INFO] Це повідомлення збережене у стек для наступної тривоги.")
    # tmp
    else:
        if not is_save_for_alarm:
            print("[INFO] Повідомлення з цього каналу у стек не зберігаються.")
        else:
            print("[INFO] Зараз тривога, тому повідомлення у стек не зберігаються.")
    # tmp end

    if state["is_show_next_event"]: # Якщо треба обов'язково показати наступне повідомлення
        state["is_show_next_event"] = False
        if is_trunc_message and trunc_word in message_text: # Якщо раптом повідомлення можна обрізати - то обрізаємо
            message_text = trunc_message(message_text, trunc_word, CONTINUE_SYMBOLS)
        messages_to_send.append({"message_text": f"Ймовірна причина тривоги:\n{message_text}\n(<i>{url}</i>)", "silent": True})

    for keyword in keywords:

        if keyword.lower() in message_text.lower():

            if is_filter_stopwords:
                if len(message_text) > stop_length or any(stop_word in message_text for stop_word in stopwords):
                    print(f"[INFO] Знайдено ключове слово '{keyword}', але повідомлення відфільтроване.")
                    break

            # Блок обробки тексту. винести у функцію
            if is_correct_punctuation: # Корекція пунктуації
                message_text = correct_punctuation(message_text)

            if is_translate: # Спеціальна обробка і переклад тексту
                message_text = replace_whole_words(message_text, TRANSLATION_DICT).capitalize()
            
            if is_delete_words: # Видалення слів з переліку
                for delete_word in deletewords:
                    message_text = message_text.replace(delete_word, "")
                message_text = message_text.strip()

            if is_trunc_message and len(message_text.split("\n")) > MAX_MESSAGE_ROWS: # Обрізання зайвої інформації
                message_text = trunc_message(message_text, trunc_word, CONTINUE_SYMBOLS)
            # Кінець блоку обробки тексту

            additional_message = ""

            # Блок опрацювання тривоги і відбою. винести у функцію
            if is_alarm_source:

                if keyword == ALARM_START_KEYWORD:
                    state["is_alarm"] = True
                    state["alarm_start_time"] = now
                    print(f"[INFO] Початок тривоги о {now.strftime('%H:%M:%S')}")
                    reason = select_reason(message_stack, now)
                    if reason:
                        additional_message = f"\n<i>Ймовірна причина тривоги:\n{reason}</i>"
                        # Винести наступний рядок у функцію?
                        other_reasons = "\n".join(f"<blockquote>{other_reason}</blockquote>" for other_reason in [m[1] for m in message_stack if m[1] != reason and len(m[1].split("\n")) < 2 * MAX_MESSAGE_ROWS and (now - m[0]).total_seconds() <= 2 * MESSAGE_TTL])
                    else:
                        state["is_show_next_event"] = True
                        additional_message = f"\n<i>Ймовірна причина тривоги не визначена.\nОчікуйте на причину в наступному повідомленні.</i>"

                elif keyword == ALARM_END_KEYWORD:
                    state["is_alarm"] = False
                    hours, minutes = calculate_length_hm(now - state["alarm_start_time"])
                    additional_message = f"\n<i>Тривалість: {hours} г. {minutes} хв.</i>"

                message_text = f"<b>{message_text}</b>"
                message_count = 50 # Терміново зберігаємо стан
            # Кінець блоку опрацювання тривоги і відбою

            # Додаємо мітку каналу-джерела
            if state["current_channel"] != channel_id:
                message_text += f"\n<i>({url})</i>"
                state["current_channel"] = channel_id
            
            if not is_similar(message_text, state["last_message"]):

                if is_forward_images and event.photo:
                    print("[INFO] Є картинка! Спробуємо надіслати.")
                    messages_to_send.append({"file": event.photo, "message_text": f"{message_text}{additional_message}", "silent": is_silent})
                else:
                    print("[INFO] Звичайне повідомлення, тільки текст.")
                    messages_to_send.append({"message_text": f"{message_text}{additional_message}", "silent": is_silent})
                print(f"[INFO] Знайдено ключове слово '{keyword}' — повідомлення надіслане.")

                if other_reasons:
                    messages_to_send.append({"message_text": f"Інші можливі причини тривоги:\n{other_reasons}", "silent": True})
                    print(f"[INFO] Інші причини:\n{other_reasons}")
                
            else:
                print(f"[INFO] Повідомлення пропущене: '{message_text}' схоже на '{state["last_message"]}'.")

            state["last_message"] = message_text # Зберігаємо текст останнього надісланого повідомлення для майбутньої перевірки

            message_count += 1
            if message_count >= 30:
                # Блок збереження конфігу. Винести у функцію
                state_copy = deepcopy(client.state)
                state_copy["alarm_start_time"] = state_copy["alarm_start_time"].isoformat()

                with open("state.json", "w", encoding="utf-8") as f:
                    json.dump(state_copy, f, indent=4)
                # Кінець блоку збереження конфігу
                message_count = 0

            break # Зупиняє перебір ключових слів, якщо було хоч одне співпадіння
    else:
        print(f"[INFO] Ключових слів не знайдено.")

    if messages_to_send:
        await send_messages(messages_to_send)


async def main():
    await client.start()
    print(f"[INFO] [{datetime.now().strftime('%H:%M:%S')}] Бот запущений.")

    #   temporary
    # messages_to_send_tmp = []
    # m_text = "<blockquote>Тестування цитат.\nДругий рядок.</blockquote><b>Жирний текст!<b><i>Курсивний текст.</i>"
    # messages_to_send_tmp.append({"message_text": m_text, "silent": True})
    # await send_messages(messages_to_send_tmp)
    #   temporary end

    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())