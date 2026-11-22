"""
Экспорт чата Telegram с автоматическим пропуском зависших файлов.
Делает HTML (messages.html, messages2.html, ...) и result.json — как формат «Both» в Telegram.

Установка:   pip install telethon tqdm
Запуск:      python tg_export.py

Прокси: по умолчанию настроено на tg-ws-proxy (MTProto, 127.0.0.1:1443).
Перед запуском скрипта tg-ws-proxy должен быть запущен.

api_id и api_hash берутся на https://my.telegram.org -> API development tools.
При первом запуске скрипт спросит номер телефона и код из Telegram.
Если оставить CHAT = "", скрипт покажет список ваших чатов с их id.
Повторный запуск продолжает с места остановки (уже скачанное не качается снова),
а HTML и JSON пересоздаются целиком.
"""
import asyncio
import html as htmlmod
import json
import os
import re
import shutil
import time
from urllib.parse import parse_qs, quote, urlparse

from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.extensions import html as tl_html
from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
from tqdm import tqdm

# ======================= НАСТРОЙКИ =======================
API_ID = 123456
API_HASH = "ваш_api_hash"
CHAT = ""  # @username, ссылка t.me/..., или числовой id. Пусто = показать список чатов

OUT_ROOT = os.path.join(os.path.expanduser("~"), "Downloads", "Telegram Export")

# ---- Прокси ----
# PROXY_TYPE: "mtproto" (tg-ws-proxy и любые MTProto-прокси), "socks5", или "" — без прокси.
PROXY_TYPE = "mtproto"
# Для mtproto проще всего вставить ссылку из tg-ws-proxy (в трее: «Скопировать ссылку»):
PROXY_LINK = ""  # например: tg://proxy?server=127.0.0.1&port=1443&secret=dd0123...
# ...или заполнить вручную (используются, если PROXY_LINK пустой):
PROXY_HOST = "127.0.0.1"
PROXY_PORT = 1443
PROXY_SECRET = ""        # для mtproto: секрет из настроек tg-ws-proxy (обычно начинается с dd)
PROXY_USER = ""          # для socks5, если нужен логин
PROXY_PASS = ""

# Как галочки в окне экспорта Telegram
TYPES = {
    "photos": True,          # Photos
    "videos": True,          # Videos
    "voice_messages": True,  # Voice messages
    "video_messages": True,  # Video messages (кружочки)
    "stickers": True,        # Stickers
    "gifs": True,            # GIFs
    "files": True,           # Files (документы, музыка, архивы и т.п.)
}
SIZE_LIMIT_MB = 4000      # Size limit: файлы крупнее не качаются
STALL_TIMEOUT = 120       # сек БЕЗ ЕДИНОГО нового байта -> загрузка считается зависшей
RETRIES = 2               # сколько раз повторить файл перед пропуском
DISK_RESERVE_MB = 500     # сколько места оставить свободным на диске
MESSAGES_PER_PAGE = 1000  # сообщений на одну HTML-страницу (как в Telegram)
# =========================================================

KIND_NAMES = {
    "photos": "Фото",
    "videos": "Видео",
    "voice_messages": "Голосовое сообщение",
    "video_messages": "Видеосообщение",
    "stickers": "Стикер",
    "gifs": "GIF",
    "files": "Файл",
}
JSON_MEDIA_TYPE = {
    "videos": "video_file",
    "gifs": "animation",
    "video_messages": "video_message",
    "voice_messages": "voice_message",
    "stickers": "sticker",
}
MISSING_REASON = {
    "disabled": "Не скачано: этот тип отключён в настройках экспорта",
    "too_big": f"Не скачано: больше лимита {SIZE_LIMIT_MB} МБ",
    "failed": "Не удалось скачать (файл недоступен или загрузка зависла)",
    "no_space": "Не скачано: закончилось место на диске",
    "missing": "Файл не найден",
}
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# ----------------------------- утилиты -----------------------------

def human(n):
    n = float(n or 0)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if n < 1024 or unit == "ТБ":
            return f"{n:.1f} {unit}" if unit != "Б" else f"{int(n)} Б"
        n /= 1024


def fmt_duration(sec):
    if not sec:
        return ""
    sec = int(sec)
    h, rest = divmod(sec, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def safe(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name[:150] or "file"


def esc(s):
    return htmlmod.escape(str(s or ""), quote=True)


def ent_name(e):
    if e is None:
        return None
    if getattr(e, "title", None):
        return e.title
    n = " ".join(filter(None, [getattr(e, "first_name", None), getattr(e, "last_name", None)]))
    if n:
        return n
    if getattr(e, "deleted", False):
        return "Удалённый аккаунт"
    return getattr(e, "username", None) or str(getattr(e, "id", ""))


def peer_key(peer):
    if peer is None:
        return None
    for attr, prefix in (("user_id", "user"), ("channel_id", "channel"), ("chat_id", "chat")):
        if hasattr(peer, attr):
            return f"{prefix}{getattr(peer, attr)}"
    return None


def initials(name):
    parts = [p for p in re.split(r"\s+", name or "") if p]
    return ("".join(p[0] for p in parts[:2]) or "?").upper()


def media_kind(msg):
    m = msg.media
    if isinstance(m, MessageMediaPhoto):
        return "photos" if msg.photo else None
    if not isinstance(m, MessageMediaDocument) or not msg.document:
        return None  # превью ссылок, опросы, геопозиции и т.п. — без файла
    if msg.sticker:
        return "stickers"
    if msg.gif:
        return "gifs"
    if msg.video_note:
        return "video_messages"
    if msg.voice:
        return "voice_messages"
    if msg.video:
        return "videos"
    return "files"


def media_size(msg):
    try:
        return msg.file.size or 0
    except Exception:
        return 0


def target_path(out_dir, msg, kind):
    f = msg.file
    name = getattr(f, "name", None)
    ext = getattr(f, "ext", None) or ""
    base = f"{msg.id}_{name}" if name else f"{msg.id}{ext}"
    return os.path.join(out_dir, kind, safe(base))


def free_space(path):
    return shutil.disk_usage(path).free


def text_to_html(msg):
    raw = msg.message or ""
    if not raw:
        return ""
    try:
        out = tl_html.unparse(raw, msg.entities or [])
    except Exception:
        out = esc(raw)
    return out.replace("\n", "<br>")


# --------------------- служебные и «прочие» медиа ---------------------

def service_text(msg, who):
    a = msg.action
    n = type(a).__name__
    who = who or "Кто-то"
    if n == "MessageActionChatCreate":
        return f"{who} создал(а) группу «{a.title}»"
    if n == "MessageActionChannelCreate":
        return f"Канал «{a.title}» создан"
    if n == "MessageActionChatEditTitle":
        return f"{who} изменил(а) название на «{a.title}»"
    if n == "MessageActionChatEditPhoto":
        return f"{who} изменил(а) фото группы"
    if n == "MessageActionChatDeletePhoto":
        return f"{who} удалил(а) фото группы"
    if n == "MessageActionChatAddUser":
        if list(getattr(a, "users", [])) == [msg.sender_id]:
            return f"{who} вступил(а) в группу"
        return f"{who} добавил(а) участников"
    if n == "MessageActionChatDeleteUser":
        if getattr(a, "user_id", None) == msg.sender_id:
            return f"{who} покинул(а) группу"
        return f"{who} удалил(а) участника"
    if n in ("MessageActionChatJoinedByLink", "MessageActionChatJoinedByRequest"):
        return f"{who} вступил(а) в группу по ссылке"
    if n == "MessageActionPinMessage":
        return f"{who} закрепил(а) сообщение"
    if n == "MessageActionPhoneCall":
        d = fmt_duration(getattr(a, "duration", 0))
        return f"Звонок{(' · ' + d) if d else ' (без ответа)'}"
    if n in ("MessageActionChatMigrateTo", "MessageActionChannelMigrateFrom"):
        return "Группа преобразована в супергруппу"
    if n == "MessageActionHistoryClear":
        return "История очищена"
    if n == "MessageActionScreenshotTaken":
        return f"{who} сделал(а) скриншот"
    if n == "MessageActionGroupCall":
        return "Видеочат"
    return f"{who}: {n.replace('MessageAction', '')}"


def _txt(x):
    return getattr(x, "text", x) or ""


def other_media(msg):
    """HTML и текст для медиа без файла (опрос, гео, контакт, ссылка, кубик)."""
    m = msg.media
    n = type(m).__name__
    try:
        if n == "MessageMediaPoll":
            q = _txt(m.poll.question)
            opts = "".join(f"<div class='poll_answer'>• {esc(_txt(a.text))}</div>" for a in m.poll.answers)
            return (f"<div class='media_poll'><div class='bold'>📊 {esc(q)}</div>{opts}</div>",
                    {"poll": {"question": q, "answers": [_txt(a.text) for a in m.poll.answers]}})
        if n in ("MessageMediaGeo", "MessageMediaGeoLive", "MessageMediaVenue"):
            lat, lon = m.geo.lat, m.geo.long
            title = getattr(m, "title", "") or "Геопозиция"
            url = f"https://maps.google.com/maps?q={lat},{lon}"
            return (f"<a class='media_other' href='{url}' target='_blank'>📍 {esc(title)}<br>"
                    f"<span class='details'>{lat:.6f}, {lon:.6f}</span></a>",
                    {"location_information": {"latitude": lat, "longitude": lon}, "place_name": title})
        if n == "MessageMediaContact":
            nm = " ".join(filter(None, [m.first_name, m.last_name]))
            return (f"<div class='media_other'>👤 {esc(nm)}<br><span class='details'>{esc(m.phone_number)}</span></div>",
                    {"contact_information": {"first_name": m.first_name, "last_name": m.last_name,
                                             "phone_number": m.phone_number}})
        if n == "MessageMediaWebPage":
            wp = m.webpage
            url = getattr(wp, "url", None)
            if not url:
                return "", {}
            title = getattr(wp, "title", None) or getattr(wp, "site_name", None) or url
            desc = getattr(wp, "description", None) or ""
            return (f"<a class='media_webpage' href='{esc(url)}' target='_blank'>"
                    f"<div class='bold'>{esc(title)}</div><div class='details'>{esc(desc[:300])}</div></a>", {})
        if n == "MessageMediaDice":
            return (f"<div class='media_other'>{esc(m.emoticon)} — {m.value}</div>",
                    {"dice": {"emoticon": m.emoticon, "value": m.value}})
    except Exception:
        pass
    return f"<div class='media_other details'>{esc(n.replace('MessageMedia', ''))}</div>", {}


# ------------------------------ загрузка ------------------------------

async def list_dialogs(client):
    print("\nВаши чаты (id — название):")
    async for d in client.iter_dialogs(limit=200):
        print(f"  {d.id:>16}  {d.name}")
    print("\nВпишите нужный id или @username в CHAT и запустите снова.")


async def scan(client, entity):
    """Первый проход: считаем, что и сколько нужно скачать."""
    wanted = {}  # msg_id -> (kind, size)
    too_big = set()
    total_msgs = 0
    async for msg in client.iter_messages(entity, reverse=True):
        total_msgs += 1
        if total_msgs % 500 == 0:
            print(f"\r  Просмотрено сообщений: {total_msgs}", end="", flush=True)
        if not msg.media:
            continue
        kind = media_kind(msg)
        if not kind or not TYPES.get(kind):
            continue
        size = media_size(msg)
        if size > SIZE_LIMIT_MB * 1024 * 1024:
            too_big.add(msg.id)
            continue
        wanted[msg.id] = (kind, size)
    print(f"\r  Просмотрено сообщений: {total_msgs}      ")
    return total_msgs, wanted, too_big


async def download_one(msg, path, bar):
    """Качает один файл. Таймер STALL_TIMEOUT сбрасывается при КАЖДОМ полученном куске,
    поэтому большой, но медленно качающийся файл не прерывается."""
    state = {"got": 0, "t": time.monotonic()}

    def cb(cur, total):
        bar.update(cur - state["got"])
        state["got"] = cur
        state["t"] = time.monotonic()

    os.makedirs(os.path.dirname(path), exist_ok=True)
    task = asyncio.create_task(msg.download_media(file=path, progress_callback=cb))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=5)
            if not task.done() and time.monotonic() - state["t"] > STALL_TIMEOUT:
                raise TimeoutError(f"нет прогресса {STALL_TIMEOUT} с")
        result = task.result()
        if not result:
            raise RuntimeError("файл недоступен")
        return result
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=10)
        bar.update(-state["got"])
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
        raise


# ------------------------------- HTML -------------------------------

CSS = """
body{margin:0;background:#fff;color:#000;font:13px/19px -apple-system,"Segoe UI","Open Sans",Roboto,Arial,sans-serif}
a{color:#168acd;text-decoration:none}a:hover{text-decoration:underline}
.page_header{position:sticky;top:0;z-index:5;background:#fff;border-bottom:1px solid #e3e6e8}
.page_header .content{max-width:740px;margin:0 auto;padding:12px 16px}
.page_header .text{font-size:15px;font-weight:700}
.page_header .sub{color:#70777b;font-size:12px}
.history{max-width:740px;margin:0 auto;padding:8px 0 40px}
.clearfix:after{content:"";display:table;clear:both}
.pull_left{float:left}.pull_right{float:right}
.bold{font-weight:700}.details{color:#70777b}
.message{padding:10px 16px 2px}
.message.joined{padding-top:2px}
.message:target{animation:hl 2.5s ease}
@keyframes hl{0%{background:#cde6f7}100%{background:transparent}}
.userpic{width:42px;height:42px;border-radius:50%;color:#fff;display:flex;align-items:center;justify-content:center;font-size:15px;font-weight:600;margin-right:12px}
.c0{background:#e17076}.c1{background:#faa774}.c2{background:#a695e7}.c3{background:#7bc862}.c4{background:#6ec9cb}.c5{background:#65aadd}.c6{background:#ee7aae}
.n0{color:#c03d33}.n1{color:#d2712a}.n2{color:#8250c4}.n3{color:#4a9a32}.n4{color:#2b9da1}.n5{color:#3a83c5}.n6{color:#c7508b}
.message .body{margin-left:54px}
.message .date{margin-left:8px;font-size:12px}
.from_name{font-weight:700;padding-bottom:3px}
.text{word-wrap:break-word;overflow-wrap:anywhere}
.message.service{padding:12px 16px}
.message.service .body{margin:0;text-align:center}
.message.service .details{display:inline-block;background:#f1f3f5;border-radius:12px;padding:3px 12px}
.forwarded{border-left:2px solid #65aadd;padding-left:10px;margin:2px 0 4px}
.forwarded .from_name{color:#3a83c5}
.reply_to{border-left:2px solid #65aadd;padding-left:8px;margin:2px 0 6px;font-size:12px;line-height:16px}
.reply_to .rname{color:#3a83c5;font-weight:600}
.media_wrap{padding:4px 0}
.photo{max-width:320px;max-height:320px;border-radius:8px;display:block}
.video,.gif{max-width:360px;max-height:360px;border-radius:8px;display:block;background:#000}
.round{width:220px;height:220px;border-radius:50%;object-fit:cover;background:#000;display:block}
.sticker{max-width:180px;max-height:180px;display:block}
audio{width:320px;max-width:100%}
.media_caption{font-size:12px;color:#70777b;padding-top:2px}
.media_file,.media_missing,.media_other,.media_webpage,.media_poll{display:inline-flex;flex-direction:column;padding:8px 12px;border-radius:8px;background:#f1f3f5;color:#000;max-width:420px}
.media_file{flex-direction:row;align-items:center;gap:12px;color:#000}
.media_file .icon{width:42px;height:42px;border-radius:50%;background:#4f9cd9;color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px;flex:none}
.media_webpage{border-left:3px solid #65aadd;border-radius:4px}
.poll_answer{padding-top:2px}
.signature{font-size:12px;color:#70777b;padding-top:2px}
.pagination{display:block;text-align:center;padding:14px;margin:6px 16px;border-radius:8px;background:#f1f3f5;font-weight:600}
blockquote{border-left:2px solid #65aadd;margin:4px 0;padding-left:8px}
pre,code{font-family:Consolas,Menlo,monospace;background:#f5f5f5;border-radius:4px;padding:0 3px}
pre{padding:6px 8px;white-space:pre-wrap}
tg-spoiler{background:#c8ccd0;color:transparent;border-radius:3px;cursor:pointer}
tg-spoiler:hover{background:transparent;color:inherit}
"""


def page_name(n):
    return "messages.html" if n == 1 else f"messages{n}.html"


def color_idx(key):
    m = re.search(r"\d+", key or "")
    return int(m.group()) % 7 if m else 0


def media_block(m):
    if not m:
        return ""
    t = m["type"]
    if t == "other":
        return m.get("html", "")
    label = KIND_NAMES.get(t, "Файл")
    info = " · ".join(filter(None, [fmt_duration(m.get("duration")), human(m.get("size")) if m.get("size") else ""]))
    if m["status"] != "ok" or not m.get("path"):
        return (f"<div class='media_missing'><div class='bold'>{esc(label)}"
                f"{(' · ' + esc(info)) if info else ''}</div>"
                f"<div class='details'>{esc(MISSING_REASON.get(m['status'], ''))}</div></div>")
    u = quote(m["path"].replace(os.sep, "/"))
    ext = (m.get("ext") or "").lower()
    if t == "photos":
        return f"<a href='{u}' target='_blank'><img class='photo' src='{u}' loading='lazy'></a>"
    if t == "videos":
        return (f"<video class='video' src='{u}' controls preload='none'></video>"
                f"<div class='media_caption'>Видео{(' · ' + esc(info)) if info else ''}</div>")
    if t == "gifs":
        return f"<video class='gif' src='{u}' controls loop muted preload='metadata'></video>"
    if t == "video_messages":
        return f"<video class='round' src='{u}' controls preload='metadata'></video>"
    if t == "voice_messages":
        return (f"<audio src='{u}' controls preload='none'></audio>"
                f"<div class='media_caption'>Голосовое{(' · ' + esc(info)) if info else ''}</div>")
    if t == "stickers":
        if ext == ".webm":
            return f"<video class='sticker' src='{u}' autoplay loop muted playsinline></video>"
        if ext == ".tgs":
            return f"<a class='media_other' href='{u}'>Анимированный стикер (.tgs)</a>"
        return f"<img class='sticker' src='{u}' loading='lazy'>"
    name = m.get("name") or os.path.basename(m["path"])
    return (f"<a class='media_file' href='{u}' target='_blank'><div class='icon'>📄</div>"
            f"<div><div class='bold'>{esc(name)}</div><div class='details'>{esc(info)}</div></div></a>")


def render_html(out_dir, title, subtitle, records):
    css_dir = os.path.join(out_dir, "css")
    os.makedirs(css_dir, exist_ok=True)
    with open(os.path.join(css_dir, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)

    pages = [records[i:i + MESSAGES_PER_PAGE] for i in range(0, len(records), MESSAGES_PER_PAGE)] or [[]]
    page_of = {}
    previews = {}
    for pi, chunk in enumerate(pages, 1):
        for r in chunk:
            page_of[r["id"]] = pi
            if r["service"]:
                previews[r["id"]] = (None, r["service_text"])
            else:
                snippet = r["text"].strip().replace("\n", " ")[:80]
                if not snippet and r["media"]:
                    snippet = KIND_NAMES.get(r["media"]["type"], "Медиа")
                previews[r["id"]] = (r["from"], snippet)

    for pi, chunk in enumerate(pages, 1):
        out = [f"<!DOCTYPE html><html><head><meta charset='utf-8'><title>{esc(title)}</title>"
               "<meta name='viewport' content='width=device-width, initial-scale=1'>"
               "<link href='css/style.css' rel='stylesheet'></head><body>"
               f"<div class='page_header'><div class='content'><div class='text'>{esc(title)}</div>"
               f"<div class='sub'>{esc(subtitle)} · страница {pi} из {len(pages)}</div></div></div>"
               "<div class='history'>"]
        if pi > 1:
            out.append(f"<a class='pagination' href='{page_name(pi - 1)}'>↑ Предыдущие сообщения</a>")

        prev = None
        last_day = None
        for r in chunk:
            d = r["date"]
            day = d.date()
            if day != last_day:
                out.append(f"<div class='message service'><div class='body'>"
                           f"<div class='details'>{d.day} {MONTHS[d.month - 1]} {d.year}</div></div></div>")
                last_day = day
                prev = None

            if r["service"]:
                link = ""
                if r["reply_to"] and r["reply_to"] in page_of:
                    rp = page_of[r["reply_to"]]
                    href = f"#message{r['reply_to']}" if rp == pi else f"{page_name(rp)}#message{r['reply_to']}"
                    link = f" <a href='{href}'>→</a>"
                out.append(f"<div class='message service' id='message{r['id']}'><div class='body'>"
                           f"<div class='details'>{esc(r['service_text'])}{link}</div></div></div>")
                prev = None
                continue

            joined = (prev is not None and not r["fwd"] and not prev["fwd"]
                      and prev["from_id"] == r["from_id"]
                      and (d - prev["date"]).total_seconds() < 900)
            ci = color_idx(r["from_id"])
            full_dt = d.strftime("%d.%m.%Y %H:%M:%S") + " UTC" + d.strftime("%z")[:3] + ":" + d.strftime("%z")[3:]
            parts = [f"<div class='message default clearfix{' joined' if joined else ''}' id='message{r['id']}'>"]
            if not joined:
                parts.append(f"<div class='pull_left'><div class='userpic c{ci}'>{esc(initials(r['from']))}</div></div>")
            parts.append("<div class='body'>")
            parts.append(f"<div class='pull_right date details' title='{full_dt}'>{d.strftime('%H:%M')}</div>")
            if not joined:
                parts.append(f"<div class='from_name n{ci}'>{esc(r['from'])}</div>")

            if r["reply_to"]:
                rid = r["reply_to"]
                if rid in page_of:
                    rp = page_of[rid]
                    href = f"#message{rid}" if rp == pi else f"{page_name(rp)}#message{rid}"
                    rname, snip = previews.get(rid, (None, ""))
                    parts.append(f"<a class='reply_to' href='{href}' style='display:block'>"
                                 f"<div class='rname'>{esc(rname or 'Сообщение')}</div>"
                                 f"<div class='details'>{esc(snip)}</div></a>")
                else:
                    parts.append("<div class='reply_to details'>В ответ на сообщение, которого нет в экспорте</div>")

            inner = []
            if r["media"]:
                mb = media_block(r["media"])
                if mb:
                    inner.append(f"<div class='media_wrap clearfix'>{mb}</div>")
            if r["text_html"]:
                inner.append(f"<div class='text'>{r['text_html']}</div>")
            if r["fwd"]:
                parts.append(f"<div class='forwarded'><div class='from_name'>Переслано от {esc(r['fwd'])}</div>"
                             + "".join(inner) + "</div>")
            else:
                parts.extend(inner)

            sig = []
            if r.get("author"):
                sig.append(esc(r["author"]))
            if r["edited"]:
                sig.append("изменено")
            if sig:
                parts.append(f"<div class='signature'>{' · '.join(sig)}</div>")
            parts.append("</div></div>")
            out.append("".join(parts))
            prev = r

        if pi < len(pages):
            out.append(f"<a class='pagination' href='{page_name(pi + 1)}'>↓ Следующие сообщения</a>")
        out.append("</div></body></html>")
        with open(os.path.join(out_dir, page_name(pi)), "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    return len(pages)


def write_json(out_dir, entity, title, records):
    msgs = []
    for r in records:
        d = r["date"]
        j = {
            "id": r["id"],
            "type": "service" if r["service"] else "message",
            "date": d.strftime("%Y-%m-%dT%H:%M:%S"),
            "date_unixtime": str(int(d.timestamp())),
        }
        if r["edited"]:
            j["edited"] = r["edited"].strftime("%Y-%m-%dT%H:%M:%S")
            j["edited_unixtime"] = str(int(r["edited"].timestamp()))
        if r["service"]:
            j["actor"] = r["from"]
            j["actor_id"] = r["from_id"]
            j["action"] = r["service_text"]
        else:
            j["from"] = r["from"]
            j["from_id"] = r["from_id"]
        if r["fwd"]:
            j["forwarded_from"] = r["fwd"]
        if r.get("author"):
            j["author"] = r["author"]
        if r["reply_to"]:
            j["reply_to_message_id"] = r["reply_to"]
        m = r["media"]
        if m:
            if m["type"] == "other":
                j.update(m.get("json", {}))
            else:
                val = m["path"].replace(os.sep, "/") if m["status"] == "ok" and m.get("path") else \
                    f"(File not included. {MISSING_REASON.get(m['status'], '')})"
                if m["type"] == "photos":
                    j["photo"] = val
                else:
                    j["file"] = val
                    if m.get("name"):
                        j["file_name"] = m["name"]
                    if m["type"] in JSON_MEDIA_TYPE:
                        j["media_type"] = JSON_MEDIA_TYPE[m["type"]]
                    if m.get("mime"):
                        j["mime_type"] = m["mime"]
                    if m.get("duration"):
                        j["duration_seconds"] = int(m["duration"])
                if m.get("size"):
                    j["file_size"] = m["size"]
        j["text"] = r["text"]
        msgs.append(j)

    etype = type(entity).__name__
    chat_type = {"User": "personal_chat", "Chat": "private_group"}.get(
        etype, "public_channel" if getattr(entity, "broadcast", False) else "private_supergroup")
    with open(os.path.join(out_dir, "result.json"), "w", encoding="utf-8") as f:
        json.dump({"name": title, "type": chat_type, "id": entity.id, "messages": msgs},
                  f, ensure_ascii=False, indent=1)


# ------------------------------- прокси -------------------------------

def make_client():
    kwargs = dict(connection_retries=10, retry_delay=3, request_retries=10, timeout=30)
    ptype = (PROXY_TYPE or "").lower()

    if ptype == "mtproto":
        from telethon import connection
        host, port, secret = PROXY_HOST, PROXY_PORT, PROXY_SECRET
        if PROXY_LINK:
            link = PROXY_LINK.strip().replace("tg://proxy?", "https://t.me/proxy?")
            q = parse_qs(urlparse(link).query)
            host = q.get("server", [host])[0]
            port = int(q.get("port", [port])[0])
            secret = q.get("secret", [secret])[0]
        secret = (secret or "").strip()
        if not secret:
            raise SystemExit("Не указан секрет прокси: вставьте ссылку в PROXY_LINK или секрет в PROXY_SECRET.")
        if secret.lower().startswith("ee"):
            raise SystemExit("Секрет начинается с 'ee' — это режим Fake TLS, Telethon его не поддерживает.\n"
                             "Выключите Fake TLS в настройках tg-ws-proxy (нужен обычный секрет с 'dd') "
                             "и скопируйте ссылку заново.")
        print(f"Подключение через MTProto-прокси {host}:{port}")
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (host, port, secret)

    elif ptype == "socks5":
        try:
            import python_socks  # noqa: F401
        except ImportError:
            raise SystemExit("Для SOCKS5 установите пакет: pip install python-socks[asyncio]")
        print(f"Подключение через SOCKS5 {PROXY_HOST}:{PROXY_PORT}")
        kwargs["proxy"] = {
            "proxy_type": "socks5", "addr": PROXY_HOST, "port": int(PROXY_PORT),
            "username": PROXY_USER or None, "password": PROXY_PASS or None, "rdns": True,
        }
    else:
        print("Подключение напрямую (без прокси)")

    return TelegramClient("tg_export_session", API_ID, API_HASH, **kwargs)


# ------------------------------- main -------------------------------

async def main():
    async with make_client() as client:
        if not CHAT:
            await list_dialogs(client)
            return

        target = int(CHAT) if str(CHAT).lstrip("-").isdigit() else CHAT
        entity = await client.get_entity(target)
        title = ent_name(entity)
        out_dir = os.path.join(OUT_ROOT, safe(title))
        os.makedirs(out_dir, exist_ok=True)

        print(f"\nЧат: {title}\nПапка: {out_dir}\n\nСканирую историю...")
        total_msgs, wanted, too_big = await scan(client, entity)

        done_path = os.path.join(out_dir, "done.txt")
        done = set()
        if os.path.exists(done_path):
            with open(done_path, encoding="utf-8") as f:
                done = {int(x) for x in f.read().split() if x.isdigit()}

        todo = {k: v for k, v in wanted.items() if k not in done}
        todo_bytes = sum(s for _, s in todo.values())

        # --- Сводка ---
        print("\nЧто будет скачано:")
        for kind, label in KIND_NAMES.items():
            if not TYPES.get(kind):
                continue
            items = [s for (k, s) in wanted.values() if k == kind]
            left = [s for (k, s) in todo.values() if k == kind]
            if items:
                print(f"  {label:<20} {len(items):>6} шт., {human(sum(items)):>10}"
                      f"   (осталось {len(left)} шт., {human(sum(left))})")
        if too_big:
            print(f"  Пропущено из-за лимита {SIZE_LIMIT_MB} МБ: {len(too_big)} шт.")
        print(f"\nУже скачано ранее: {len(wanted) - len(todo)} файлов")
        print(f"Осталось скачать:  {len(todo)} файлов, {human(todo_bytes)}")

        # --- Проверка места ---
        free = free_space(out_dir)
        need = todo_bytes + DISK_RESERVE_MB * 1024 * 1024
        print(f"Свободно на диске: {human(free)}")
        if need > free:
            print(f"\n⚠ МЕСТА НЕ ХВАТИТ: нужно ~{human(need)} (с запасом), свободно {human(free)}.")
            ans = input("Всё равно начать? Скачивание остановится, когда место кончится. [y/N]: ")
            if ans.strip().lower() not in ("y", "д", "yes", "да"):
                return
        else:
            print(f"Места хватает (после загрузки останется ~{human(free - todo_bytes)}).")

        input("\nНажмите Enter, чтобы начать...")

        # --- Второй проход: загрузка + сбор сообщений ---
        ok = len(wanted) - len(todo)
        failed = 0
        disk_full = False
        records = []
        bar = tqdm(total=todo_bytes, unit="B", unit_scale=True, unit_divisor=1024,
                   desc="Скачано", dynamic_ncols=True, smoothing=0.05)
        bar.set_postfix_str(f"файлов {ok}/{len(wanted)}, пропущено {failed}")

        with open(done_path, "a", encoding="utf-8") as donef, \
             open(os.path.join(out_dir, "failed.txt"), "a", encoding="utf-8") as failf:

            async for msg in client.iter_messages(entity, reverse=True):
                media_info = None
                kind = media_kind(msg) if msg.media else None

                if kind:
                    size = media_size(msg)
                    path = target_path(out_dir, msg, kind)
                    f = msg.file
                    media_info = {
                        "type": kind, "size": size, "path": None,
                        "name": getattr(f, "name", None), "ext": getattr(f, "ext", None),
                        "mime": getattr(f, "mime_type", None), "duration": getattr(f, "duration", None),
                    }
                    if not TYPES.get(kind):
                        status = "disabled"
                    elif msg.id in too_big:
                        status = "too_big"
                    elif msg.id in done:
                        status = "ok" if os.path.exists(path) else "missing"
                    elif msg.id in todo:
                        if disk_full or free_space(out_dir) < size + DISK_RESERVE_MB * 1024 * 1024:
                            if not disk_full:
                                tqdm.write("\n⛔ Закончилось место на диске — дальше файлы не качаются, "
                                           "но тексты и HTML будут сохранены. Освободите место и "
                                           "запустите снова, чтобы докачать.")
                            disk_full = True
                            status = "no_space"
                            bar.total -= size
                            bar.refresh()
                        else:
                            attempt = 0
                            while True:
                                try:
                                    await download_one(msg, path, bar)
                                    status = "ok"
                                    ok += 1
                                    donef.write(f"{msg.id}\n")
                                    donef.flush()
                                    break
                                except FloodWaitError as e:
                                    tqdm.write(f"Telegram просит подождать {e.seconds} с...")
                                    await asyncio.sleep(e.seconds + 1)
                                except (KeyboardInterrupt, asyncio.CancelledError):
                                    raise
                                except Exception as e:
                                    attempt += 1
                                    if attempt <= RETRIES:
                                        tqdm.write(f"Повтор {attempt}/{RETRIES} для сообщения {msg.id}: {e}")
                                        await asyncio.sleep(3)
                                        continue
                                    status = "failed"
                                    failed += 1
                                    bar.total -= size
                                    bar.refresh()
                                    tqdm.write(f"Пропущен файл (сообщение {msg.id}, {human(size)}): {e}")
                                    failf.write(f"{msg.id}\t{kind}\t{size}\t{e!r}\n")
                                    failf.flush()
                                    break
                            bar.set_postfix_str(f"файлов {ok}/{len(wanted)}, пропущено {failed}")
                    else:
                        status = "missing"
                    media_info["status"] = status
                    if status == "ok":
                        media_info["path"] = os.path.relpath(path, out_dir)
                elif msg.media:
                    h, j = other_media(msg)
                    if h:
                        media_info = {"type": "other", "html": h, "json": j}

                sender = msg.sender
                from_name = ent_name(sender) or getattr(msg, "post_author", None) or title
                from_id = peer_key(msg.from_id) or peer_key(msg.peer_id)
                fwd = None
                if msg.fwd_from:
                    try:
                        fwd = ent_name(msg.forward.sender) or ent_name(msg.forward.chat)
                    except Exception:
                        fwd = None
                    fwd = fwd or msg.fwd_from.from_name or "Неизвестно"
                is_service = msg.action is not None

                records.append({
                    "id": msg.id,
                    "service": is_service,
                    "service_text": service_text(msg, from_name) if is_service else None,
                    "date": msg.date.astimezone(),
                    "edited": msg.edit_date.astimezone() if getattr(msg, "edit_date", None)
                              and not getattr(msg, "edit_hide", False) else None,
                    "from": from_name,
                    "from_id": from_id,
                    "fwd": fwd,
                    "author": getattr(msg, "post_author", None),
                    "reply_to": msg.reply_to_msg_id,
                    "text": msg.raw_text or "",
                    "text_html": "" if is_service else text_to_html(msg),
                    "media": media_info,
                })

        bar.close()

        print("\nСоздаю HTML и JSON...")
        subtitle = f"{len(records)} сообщений"
        pages = render_html(out_dir, title, subtitle, records)
        write_json(out_dir, entity, title, records)

        print(f"\nГотово. Скачано файлов: {ok}/{len(wanted)}, пропущено: {failed}.")
        if failed:
            print("Список пропущенных — в failed.txt. Повторный запуск попробует скачать их снова.")
        if disk_full:
            print("Часть файлов не скачана из-за нехватки места — освободите место и запустите снова.")
        print(f"Откройте: {os.path.join(out_dir, 'messages.html')}  ({pages} стр.)")
        print(f"JSON:     {os.path.join(out_dir, 'result.json')}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nОстановлено. Запустите снова — загрузка продолжится с места остановки, "
              "HTML будет создан в конце.")
