"""
Telegram Chat Exporter — графическая версия.

Запуск: двойной клик по «Запуск.bat» (сам установит нужные пакеты)
или вручную:  pip install telethon cryptg qrcode python-socks[asyncio]
              python tg_export_gui.py

Все настройки задаются в окне и сохраняются в tg_export_settings.json рядом со скриптом.
"""
import asyncio
import collections
import html as htmlmod
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, simpledialog, ttk
from urllib.parse import parse_qs, quote, urlparse

try:
    from telethon import TelegramClient, connection
    from telethon.errors import FloodWaitError, PhoneCodeInvalidError, SessionPasswordNeededError
    from telethon.extensions import html as tl_html
    from telethon.tl.types import MessageMediaDocument, MessageMediaPhoto
except ImportError:
    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Не хватает пакетов",
                         "Не установлен Telethon.\n\nЗапустите «Запуск.bat» или выполните:\n"
                         "pip install telethon cryptg qrcode python-socks[asyncio]")
    sys.exit(1)

APP_DIR = os.path.dirname(os.path.abspath(sys.argv[0]))
CONFIG_PATH = os.path.join(APP_DIR, "tg_export_settings.json")
SESSION_PATH = os.path.join(APP_DIR, "tg_export_session")

DEFAULTS = {
    "api_id": "", "api_hash": "", "phone": "",
    "proxy_type": "mtproto", "proxy_link": "", "proxy_host": "127.0.0.1", "proxy_port": "1443",
    "proxy_secret": "", "proxy_user": "", "proxy_pass": "",
    "types": {"photos": True, "videos": True, "voice_messages": True, "video_messages": True,
              "stickers": True, "gifs": True, "files": True},
    "size_limit_mb": 4000,
    "out_root": os.path.join(os.path.expanduser("~"), "Downloads", "Telegram Export"),
    "parallel": 4, "threads_per_file": 4, "stall_timeout": 120, "retries": 2, "disk_reserve_mb": 500,
    "make_html": True, "make_json": True, "messages_per_page": 1000,
    "last_chat": "",
    "auto_download": True,
    "all_kinds": {"Личный": True, "Группа": True, "Канал": True},
    "all_skip_done": True,
    "theme": "system",          # light / dark / system
    "details_tab": "general",   # вкладка панели деталей в «Моих экспортах»
    "details_h": 0,             # высота панели деталей (0 — по содержимому)
    "graph_period": "300",
    "col_widths": {},           # ширина колонок таблиц, которую выставил пользователь
    "multi_mode": "sequential", # несколько чатов: по очереди / сначала сканировать все
    "language": "system",       # ru / en / system
    "takeout": {},              # режим экспорта Telegram: последнее известное состояние
    "takeout_hidden": "",       # какое состояние плашки пользователь скрыл
}

KIND_NAMES = {
    "photos": "Фото",
    "videos": "Видео",
    "voice_messages": "Голосовое сообщение",
    "video_messages": "Видеосообщение",
    "stickers": "Стикер",
    "gifs": "GIF",
    "files": "Файл",
}
TYPE_LABELS = {
    "photos": "Фото", "videos": "Видео", "voice_messages": "Голосовые сообщения",
    "video_messages": "Видеосообщения (кружочки)", "stickers": "Стикеры", "gifs": "GIF",
    "files": "Файлы",
}
JSON_MEDIA_TYPE = {
    "videos": "video_file",
    "gifs": "animation",
    "video_messages": "video_message",
    "voice_messages": "voice_message",
    "stickers": "sticker",
}
SIZE_LIMIT_MB = 4000
MESSAGES_PER_PAGE = 1000


def build_missing_reason():
    return {
        "disabled": "Не скачано: этот тип отключён в настройках экспорта",
        "too_big": f"Не скачано: больше лимита {SIZE_LIMIT_MB} МБ" if SIZE_LIMIT_MB else "Не скачано",
        "failed": "Не удалось скачать (файл недоступен или загрузка зависла)",
        "no_space": "Не скачано: закончилось место на диске",
        "missing": "Файл не найден",
    }


MISSING_REASON = build_missing_reason()
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


WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                *(f"LPT{i}" for i in range(1, 10))}


def safe(name, limit=120):
    """Безопасное имя для Windows: без запрещённых символов, не слишком длинное
    (с сохранением расширения), не совпадающее с зарезервированными именами (CON, NUL…)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(name)).strip(" .")
    if len(name) > limit:
        stem, ext = os.path.splitext(name)
        if 0 < len(ext) <= 12:
            name = stem[:limit - len(ext)].rstrip(" .") + ext
        else:
            name = name[:limit].rstrip(" .")
    if name.split(".")[0].upper() in WIN_RESERVED:
        name = "_" + name
    return name or "file"


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


def free_space(path):
    return shutil.disk_usage(path).free


# ============ формат экспорта Telegram Desktop (result.json + файлы) ============
# Повторяет структуру, которую пишет сам Telegram Desktop (export_output_json.cpp):
# те же ключи, порядок, имена папок и файлов, строки для нескачанных файлов.

NOT_INCLUDED = "(File not included. Change data exporting settings to download.)"
TOO_BIG = "(File exceeds maximum size. Change data exporting settings to download.)"
UNAVAILABLE = "(File unavailable, please try again later)"
SKIP_PATH = {"disabled": NOT_INCLUDED, "too_big": TOO_BIG, "failed": UNAVAILABLE,
             "no_space": UNAVAILABLE, "missing": UNAVAILABLE}
FILE_PH = "\x00FILE\x00"
THUMB_PH = "\x00THUMB\x00"
CE_PH = "\x00CE\x00"


def cname(x):
    return type(x).__name__ if x is not None else ""


def _txt(x):
    """TextWithEntities или строка → строка."""
    if x is None:
        return ""
    return getattr(x, "text", x) if not isinstance(x, str) else x


def tg_date(dt):
    """Как в Telegram: локальное время без часового пояса + unixtime строкой."""
    if not dt:
        return None, None
    if isinstance(dt, (int, float)):
        import datetime as _d
        dt = _d.datetime.fromtimestamp(dt, _d.timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%dT%H:%M:%S"), str(int(dt.timestamp()))


def ent_key(e):
    """Сущность Telethon → 'user123' / 'chat123' / 'channel123' (как from_id у Telegram)."""
    n = cname(e)
    if n in ("User", "UserEmpty", "InputPeerUser", "PeerUser"):
        return f"user{getattr(e, 'id', None) or getattr(e, 'user_id', '')}"
    if n in ("Chat", "ChatForbidden", "ChatEmpty", "InputPeerChat", "PeerChat"):
        return f"chat{getattr(e, 'id', None) or getattr(e, 'chat_id', '')}"
    if n in ("Channel", "ChannelForbidden", "InputPeerChannel", "PeerChannel"):
        return f"channel{getattr(e, 'id', None) or getattr(e, 'channel_id', '')}"
    return peer_key(e)


# ---------- медиа: что за объект, какой тип, какой размер ----------

def media_object(msg):
    """Photo или Document, который надо скачать (в том числе фото из служебных сообщений)."""
    act = getattr(msg, "action", None)
    if act is not None:
        ph = getattr(act, "photo", None)
        return ph if cname(ph) == "Photo" else None
    m = msg.media
    n = cname(m)
    if n == "MessageMediaPhoto":
        return m.photo if cname(m.photo) == "Photo" else None
    if n == "MessageMediaDocument":
        return m.document if cname(m.document) == "Document" else None
    return None   # превью ссылок, опросы, геопозиции, истории, платные медиа — без файла (как у Telegram)


def doc_info(doc):
    a = {}
    for x in getattr(doc, "attributes", None) or []:
        a.setdefault(cname(x), x)
    v = a.get("DocumentAttributeVideo")
    au = a.get("DocumentAttributeAudio")
    st = a.get("DocumentAttributeSticker") or a.get("DocumentAttributeCustomEmoji")
    img = a.get("DocumentAttributeImageSize")
    round_ = bool(v and getattr(v, "round_message", False))
    return {
        "name": getattr(a.get("DocumentAttributeFilename"), "file_name", "") or "",
        "is_sticker": bool(st), "sticker_emoji": (getattr(st, "alt", "") or "") if st else "",
        "is_animated": "DocumentAttributeAnimated" in a,
        "is_round": round_, "is_video": bool(v) and not round_,
        "is_voice": bool(au and getattr(au, "voice", False)),
        "is_audio": bool(au) and not getattr(au, "voice", False),
        "duration": int(round(float(getattr(v or au, "duration", 0) or 0))) if (v or au) else 0,
        "width": getattr(v, "w", 0) or getattr(img, "w", 0) or 0,
        "height": getattr(v, "h", 0) or getattr(img, "h", 0) or 0,
        "performer": getattr(au, "performer", None) if au else None,
        "title": getattr(au, "title", None) if au else None,
        "mime": getattr(doc, "mime_type", "") or "",
    }


def doc_folder(i):
    # порядок проверки как в Telegram Desktop (DocumentFolder)
    if i["is_video"]:
        return "video_files"
    if i["is_animated"]:
        return "animations"
    if i["is_sticker"]:
        return "stickers"
    if i["is_voice"]:
        return "voice_messages"
    if i["is_round"]:
        return "round_video_messages"
    return "files"


def doc_media_type(i):
    if i["is_sticker"]:
        return "sticker"
    if i["is_round"]:
        return "video_message"
    if i["is_voice"]:
        return "voice_message"
    if i["is_animated"]:
        return "animation"
    if i["is_video"]:
        return "video_file"
    if i["is_audio"]:
        return "audio_file"
    return None


def media_kind(msg):
    """К какой галочке настроек относится файл: photos / videos / voice_messages /
    video_messages / stickers / gifs / files."""
    obj = media_object(msg)
    if obj is None:
        return None
    if cname(obj) == "Photo":
        return "photos"
    i = doc_info(obj)
    if i["is_sticker"]:
        return "stickers"
    if i["is_round"]:
        return "video_messages"
    if i["is_voice"]:
        return "voice_messages"
    if i["is_animated"]:
        return "gifs"
    if i["is_video"]:
        return "videos"
    return "files"


def photo_size_bytes(s):
    n = cname(s)
    if n == "PhotoSize":
        return s.size or 0
    if n == "PhotoSizeProgressive":
        return max(s.sizes) if s.sizes else 0
    if n == "PhotoCachedSize":
        return len(s.bytes or b"")
    return 0


def photo_main_size(photo):
    sizes = getattr(photo, "sizes", None) or []
    return sizes[-1] if sizes else None   # этот размер скачивает Telethon (и Telegram Desktop)


def media_size(msg, obj=None):
    obj = obj if obj is not None else media_object(msg)
    if obj is None:
        return 0
    if cname(obj) == "Photo":
        return photo_size_bytes(photo_main_size(obj))
    return getattr(obj, "size", 0) or 0


def ext_for(mime):
    import mimetypes
    fixed = {"image/jpeg": ".jpg", "video/quicktime": ".mov", "audio/ogg": ".ogg", "video/mp4": ".mp4",
             "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "image/webp": ".webp", "video/webm": ".webm",
             "application/x-tgsticker": ".tgs", "audio/x-m4a": ".m4a", "audio/mp4": ".m4a"}
    if mime in fixed:
        return fixed[mime]
    return mimetypes.guess_extension(mime or "") or None


class Namer:
    """Имена файлов как у Telegram Desktop (photo_1@ДД-ММ-ГГГГ_ЧЧ-ММ-СС.jpg, audio_N@…ogg,
    исходное имя документа, повторы — «имя (1).ext»). Имена закрепляются за сообщением
    в names.json и не меняются между запусками — это нужно для докачки."""

    def __init__(self, path):
        self.path = path
        data = {}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
        self.paths = data.get("paths", {})
        self.counters = data.get("counters", {})
        self.used = {p.lower() for p in self.paths.values()}
        self.dirty = False

    def _n(self, kind):
        self.counters[kind] = self.counters.get(kind, 0) + 1
        return self.counters[kind]

    def _unique(self, folder, base):
        stem, ext = os.path.splitext(base)
        cand, k = base, 0
        while f"{folder}/{cand}".lower() in self.used:
            k += 1
            cand = f"{stem} ({k}){ext}"
        rel = f"{folder}/{cand}"
        self.used.add(rel.lower())
        return rel

    def assign(self, key, obj, date, folder=None):
        if key in self.paths:
            return self.paths[key]
        dp = ("@" + date.astimezone().strftime("%d-%m-%Y_%H-%M-%S")) if date else ""
        if cname(obj) == "Photo":
            folder, base = "photos", f"photo_{self._n('photo')}{dp}.jpg"
        else:
            i = doc_info(obj)
            folder = folder or doc_folder(i)
            if i["name"]:
                base = i["name"]
            elif i["is_voice"]:
                base = f"audio_{self._n('audio')}{dp}" + (".mp3" if i["mime"] == "audio/mp3" else ".ogg")
            elif i["is_video"]:
                base = f"video_{self._n('video')}{dp}" + (ext_for(i["mime"]) or ".mov")
            else:
                base = f"file_{self._n('file')}{dp}" + (ext_for(i["mime"]) or ".unknown")
        rel = self._unique(folder, safe(base))
        self.paths[key] = rel
        self.dirty = True
        return rel

    def assign_plain(self, key, folder, base):
        if key in self.paths:
            return self.paths[key]
        rel = self._unique(folder, safe(base))
        self.paths[key] = rel
        self.dirty = True
        return rel

    def save(self):
        if not self.dirty:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"paths": self.paths, "counters": self.counters}, f, ensure_ascii=False)
        os.replace(tmp, self.path)
        self.dirty = False


def abs_path(out_dir, rel):
    return os.path.join(out_dir, *rel.split("/"))


def best_doc_thumb(doc):
    th = [t for t in (getattr(doc, "thumbs", None) or [])
          if cname(t) in ("PhotoSize", "PhotoSizeProgressive", "PhotoCachedSize")]
    if not th:
        return None
    return max(th, key=lambda t: (getattr(t, "w", 0) or 0) * (getattr(t, "h", 0) or 0))


async def download_thumb(client, doc, path):
    """Миниатюра документа → path (как «…_thumb.jpg» у Telegram). Возвращает размер или None."""
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return os.path.getsize(path)
    t = best_doc_thumb(doc)
    if t is None:
        return None
    if cname(t) == "PhotoCachedSize":
        data = t.bytes
    else:
        from telethon.tl.types import InputDocumentFileLocation
        loc = InputDocumentFileLocation(id=doc.id, access_hash=doc.access_hash,
                                        file_reference=doc.file_reference, thumb_size=t.type)
        data = b""
        async for chunk in client.iter_download(loc, dc_id=doc.dc_id, file_size=photo_size_bytes(t) or None):
            data += chunk
    if not data:
        return None
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "wb") as f:
        f.write(data)
    os.replace(path + ".tmp", path)
    return len(data)


# ---------- текст и оформление ----------

ENT_TYPES = {
    "MessageEntityMention": "mention", "MessageEntityHashtag": "hashtag",
    "MessageEntityBotCommand": "bot_command", "MessageEntityUrl": "link", "MessageEntityEmail": "email",
    "MessageEntityBold": "bold", "MessageEntityItalic": "italic", "MessageEntityCode": "code",
    "MessageEntityPre": "pre", "MessageEntityTextUrl": "text_link",
    "MessageEntityMentionName": "mention_name", "InputMessageEntityMentionName": "mention_name",
    "MessageEntityPhone": "phone", "MessageEntityCashtag": "cashtag",
    "MessageEntityUnderline": "underline", "MessageEntityStrike": "strikethrough",
    "MessageEntityBlockquote": "blockquote", "MessageEntityBankCard": "bank_card",
    "MessageEntitySpoiler": "spoiler", "MessageEntityCustomEmoji": "custom_emoji",
}


def _surr(s):
    try:
        from telethon.helpers import add_surrogate, del_surrogate
        return add_surrogate, del_surrogate
    except Exception:
        return (lambda t: "".join(c if ord(c) < 0x10000 else
                                  chr(0xD800 + ((ord(c) - 0x10000) >> 10)) + chr(0xDC00 + ((ord(c) - 0x10000) & 0x3FF))
                                  for c in t),
                lambda t: t.encode("utf-16", "surrogatepass").decode("utf-16"))


def build_text(raw, entities):
    """→ (text, text_entities, [id кастомных эмодзи]) в формате Telegram.
    Смещения сущностей Telegram считает в UTF-16 — учитываем это (эмодзи и т.п.)."""
    raw = raw or ""
    if not raw:
        return "", [], []
    add_s, del_s = _surr(raw)
    s = add_s(raw)
    parts, pos, custom = [], 0, []
    for e in sorted(entities or [], key=lambda e: (e.offset, -e.length)):
        st, en = max(e.offset, pos), e.offset + e.length
        if en <= st:
            continue
        if st > pos:
            parts.append({"type": "plain", "text": del_s(s[pos:st])})
        t = ENT_TYPES.get(cname(e), "unknown")
        p = {"type": t, "text": del_s(s[st:en])}
        if t == "pre":
            p["language"] = getattr(e, "language", "") or ""
        elif t == "text_link":
            p["href"] = getattr(e, "url", "") or ""
        elif t == "mention_name":
            uid = getattr(e, "user_id", None)
            p["user_id"] = getattr(uid, "user_id", uid)
        elif t == "custom_emoji":
            p["document_id"] = f"{CE_PH}{e.document_id}"
            custom.append(e.document_id)
        elif t == "blockquote":
            p["collapsed"] = bool(getattr(e, "collapsed", False))
        parts.append(p)
        pos = en
    if pos < len(s):
        parts.append({"type": "plain", "text": del_s(s[pos:])})
    elif len(parts) > 1:
        parts.append({"type": "plain", "text": ""})   # так делает Telegram Desktop
    if len(parts) == 1 and parts[0]["type"] == "plain":
        return parts[0]["text"], [dict(parts[0])], custom
    text = [p["text"] if p["type"] == "plain" else dict(p) for p in parts]
    return text, [dict(p) for p in parts], custom


# ---------- кнопки под сообщением ----------

BTN_TYPES = {
    "KeyboardButtonUrl": "url", "KeyboardButtonCallback": "callback",
    "KeyboardButtonRequestPhone": "request_phone", "KeyboardButtonRequestGeoLocation": "request_location",
    "KeyboardButtonSwitchInline": "switch_inline", "KeyboardButtonGame": "game",
    "KeyboardButtonBuy": "buy", "KeyboardButtonUrlAuth": "auth", "InputKeyboardButtonUrlAuth": "auth",
    "KeyboardButtonRequestPoll": "request_poll", "KeyboardButtonUserProfile": "user_profile",
    "InputKeyboardButtonUserProfile": "user_profile", "KeyboardButtonWebView": "web_view",
    "KeyboardButtonSimpleWebView": "simple_web_view", "KeyboardButtonRequestPeer": "request_peer",
    "InputKeyboardButtonRequestPeer": "request_peer", "KeyboardButtonCopy": "copy_text",
    "KeyboardButton": "default",
}


def build_buttons(msg):
    import base64
    rm = getattr(msg, "reply_markup", None)
    if cname(rm) != "ReplyInlineMarkup":
        return None
    rows = []
    for row in rm.rows:
        out = []
        for b in row.buttons:
            n = cname(b)
            t = BTN_TYPES.get(n, "default")
            if n == "KeyboardButtonSwitchInline" and getattr(b, "same_peer", False):
                t = "switch_inline_same"
            d = {"type": t, "text": getattr(b, "text", "") or ""}
            if n == "KeyboardButtonCallback":
                d["data"] = ""
                d["dataBase64"] = base64.b64encode(b.data or b"").decode()
            elif n in ("KeyboardButtonUrl", "KeyboardButtonWebView", "KeyboardButtonSimpleWebView",
                       "KeyboardButtonUrlAuth"):
                d["data"] = getattr(b, "url", "") or ""
            elif n == "KeyboardButtonSwitchInline":
                d["data"] = getattr(b, "query", "") or ""
            elif n == "KeyboardButtonCopy":
                d["data"] = getattr(b, "copy_text", "") or ""
            if n == "KeyboardButtonUrlAuth" and getattr(b, "fwd_text", None):
                d["forward_text"] = b.fwd_text
            if getattr(b, "button_id", None) is not None:
                d["button_id"] = b.button_id
            out.append(d)
        rows.append(out)
    return rows or None


# ---------- имена людей/чатов ----------

class Names:
    """Имя по id собеседника (для реакций, участников, пересланных и т.п.) с кэшем."""

    def __init__(self, client):
        self.client = client
        self.cache = {}

    def remember(self, ent):
        if ent is not None and cname(ent) not in ("InputPeerUser", "InputPeerChat", "InputPeerChannel"):
            k = ent_key(ent)
            if k:
                self.cache[k] = ent_name(ent)

    async def name(self, peer):
        if peer is None:
            return None
        if isinstance(peer, int):
            from telethon.tl.types import PeerUser
            peer = PeerUser(peer)
        k = ent_key(peer)
        if k in self.cache:
            return self.cache[k]
        try:
            ent = await self.client.get_entity(peer)
            nm = ent_name(ent)
        except Exception:
            nm = None
        self.cache[k] = nm
        return nm


# ---------- реакции ----------

async def build_reactions(msg, names):
    r = getattr(msg, "reactions", None)
    if not r or not getattr(r, "results", None):
        return None, []
    custom = []

    def rkey(x):
        n = cname(x)
        if n == "ReactionEmoji":
            return ("emoji", x.emoticon)
        if n == "ReactionCustomEmoji":
            return ("custom_emoji", x.document_id)
        return (n, None)

    recent = {}
    for pr in getattr(r, "recent_reactions", None) or []:
        d, du = tg_date(pr.date)
        recent.setdefault(rkey(pr.reaction), []).append(
            {"from": await names.name(pr.peer_id), "from_id": ent_key(pr.peer_id), "date": d})
    out = []
    for rc in r.results:
        x = rc.reaction
        n = cname(x)
        if n == "ReactionEmoji":
            d = {"type": "emoji", "count": rc.count, "emoji": x.emoticon}
        elif n == "ReactionCustomEmoji":
            d = {"type": "custom_emoji", "count": rc.count, "document_id": f"{CE_PH}{x.document_id}"}
            custom.append(x.document_id)
        elif n == "ReactionPaid":
            d = {"type": "paid", "count": rc.count}
        else:
            continue
        if rkey(x) in recent:
            d["recent"] = recent[rkey(x)]
        out.append(d)
    return out or None, custom


# ---------- поля медиа для JSON ----------

def photo_fields(j, photo, spoiler=False, ttl=None):
    s = photo_main_size(photo)
    j["photo"] = FILE_PH
    j["photo_file_size"] = photo_size_bytes(s)
    if getattr(s, "w", 0) and getattr(s, "h", 0):
        j["width"], j["height"] = s.w, s.h
    if spoiler:
        j["media_spoiler"] = True
    if ttl:
        j["self_destruct_period_seconds"] = ttl


def document_fields(j, doc, spoiler=False, ttl=None):
    i = doc_info(doc)
    j["file"] = FILE_PH
    if i["name"]:
        j["file_name"] = i["name"]
    j["file_size"] = getattr(doc, "size", 0) or 0
    if best_doc_thumb(doc) is not None:
        j["thumbnail"] = THUMB_PH
        j["thumbnail_file_size"] = 0
    mt = doc_media_type(i)
    if mt:
        j["media_type"] = mt
    if mt == "sticker" and i["sticker_emoji"]:
        j["sticker_emoji"] = i["sticker_emoji"]
    if mt == "audio_file":
        if i["performer"]:
            j["performer"] = i["performer"]
        if i["title"]:
            j["title"] = i["title"]
    if i["mime"]:
        j["mime_type"] = i["mime"]
    if i["duration"]:
        j["duration_seconds"] = i["duration"]
    if i["width"] and i["height"]:
        j["width"], j["height"] = i["width"], i["height"]
    if spoiler:
        j["media_spoiler"] = True
    if ttl:
        j["self_destruct_period_seconds"] = ttl


def geo_obj(g):
    if cname(g) != "GeoPoint":
        return None
    return {"latitude": g.lat, "longitude": g.long}


def other_media_fields(j, msg, namer, out_dir, key):
    """Медиа без скачиваемого файла. Возвращает HTML-дополнение для того, чего нет в JSON Telegram
    (кубик, превью ссылки, история)."""
    m = msg.media
    n = cname(m)
    extra = ""
    if n == "MessageMediaContact":
        j["contact_information"] = {"first_name": m.first_name or "", "last_name": m.last_name or "",
                                    "phone_number": m.phone_number or ""}
        if getattr(m, "vcard", None):
            data = m.vcard.encode("utf-8")
            rel = namer.paths.get(f"{key}:vcard") or namer.assign_plain(
                f"{key}:vcard", "contacts", f"contact_{namer._n('contact')}.vcard")
            p = abs_path(out_dir, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as f:
                f.write(data)
            j["contact_vcard"] = rel
            j["contact_vcard_file_size"] = len(data)
    elif n in ("MessageMediaGeo", "MessageMediaGeoLive"):
        j["location_information"] = geo_obj(m.geo)
        if n == "MessageMediaGeoLive" and getattr(m, "period", 0):
            j["live_location_period_seconds"] = m.period
    elif n == "MessageMediaVenue":
        j["place_name"] = m.title or ""
        j["address"] = m.address or ""
        j["location_information"] = geo_obj(m.geo)
    elif n == "MessageMediaGame":
        g = m.game
        j["game_title"] = g.title or ""
        j["game_description"] = g.description or ""
        bot = getattr(getattr(msg, "via_bot", None), "username", None) or \
            getattr(getattr(msg, "sender", None), "username", None)
        if bot and g.short_name:
            j["game_link"] = f"https://t.me/{bot}?game={g.short_name}"
    elif n == "MessageMediaInvoice":
        info = {"title": m.title or "", "description": m.description or "",
                "amount": m.total_amount, "currency": m.currency}
        if getattr(m, "receipt_msg_id", None):
            info["receipt_message_id"] = m.receipt_msg_id
        j["invoice_information"] = info
    elif n == "MessageMediaPoll":
        p, res = m.poll, m.results
        votes = {v.option: v for v in (getattr(res, "results", None) or [])}
        j["poll"] = {
            "question": _txt(p.question),
            "closed": bool(getattr(p, "closed", False)),
            "total_voters": getattr(res, "total_voters", 0) or 0,
            "answers": [{"text": _txt(a.text),
                         "voters": getattr(votes.get(a.option), "voters", 0) or 0,
                         "chosen": bool(getattr(votes.get(a.option), "chosen", False))}
                        for a in p.answers],
        }
    elif n == "MessageMediaToDo":
        t = m.todo
        j["todo_list"] = {
            "title": _txt(t.title),
            "others_can_append": bool(getattr(t, "others_can_append", False)),
            "others_can_complete": bool(getattr(t, "others_can_complete", False)),
            "answers": [{"text": _txt(it.title), "id": it.id} for it in t.list],
        }
    elif n == "MessageMediaGiveaway":
        j["giveaway_information"] = {
            "quantity": m.quantity, "months": getattr(m, "months", None),
            "until_date": tg_date(m.until_date)[0], "channels": list(m.channels or []),
            "countries": list(getattr(m, "countries_iso2", None) or []),
            "additional_prize": getattr(m, "prize_description", "") or "",
            "stars": getattr(m, "stars", None),
            "is_only_new_subscribers": bool(getattr(m, "only_new_subscribers", False)),
        }
    elif n == "MessageMediaGiveawayResults":
        j["giveaway_results"] = {
            "channel": m.channel_id, "winners": list(m.winners or []),
            "additional_prize": getattr(m, "prize_description", "") or "",
            "until_date": tg_date(m.until_date)[0], "launch_message_id": m.launch_msg_id,
            "additional_peers_count": getattr(m, "additional_peers_count", 0) or 0,
            "winners_count": m.winners_count, "unclaimed_count": m.unclaimed_count,
            "months": getattr(m, "months", None), "stars": getattr(m, "stars", None),
            "is_refunded": bool(getattr(m, "refunded", False)),
            "is_only_new_subscribers": bool(getattr(m, "only_new_subscribers", False)),
        }
    elif n == "MessageMediaPaidMedia":
        j["paid_stars_amount"] = m.stars_amount
        extra = f"<div class='media_other'>⭐ Платный контент · {m.stars_amount} звёзд</div>"
    elif n == "MessageMediaDice":
        extra = f"<div class='media_other dice'>{esc(m.emoticon)} <b>{m.value}</b></div>"
    elif n == "MessageMediaWebPage":
        wp = m.webpage
        url = getattr(wp, "url", None)
        if url:
            title = getattr(wp, "title", None) or getattr(wp, "site_name", None) or url
            desc = getattr(wp, "description", None) or ""
            extra = (f"<a class='media_webpage' href='{esc(url)}' target='_blank'>"
                     f"<div class='bold'>{esc(title)}</div><div class='details'>{esc(desc[:300])}</div></a>")
    elif n == "MessageMediaStory":
        extra = "<div class='media_other details'>📖 История</div>"
    elif n and n not in ("MessageMediaEmpty", "MessageMediaUnsupported"):
        extra = f"<div class='media_other details'>{esc(n.replace('MessageMedia', ''))}</div>"
    return extra


# ---------- служебные сообщения ----------

def _snake(n):
    return re.sub(r"(?<!^)(?=[A-Z])", "_", n.replace("MessageAction", "")).lower()


async def service_fields(j, msg, names):
    """Заполняет action и поля конкретного служебного события как Telegram Desktop.
    Возвращает True, если у события есть фото для скачивания."""
    a = msg.action
    n = cname(a).replace("MessageAction", "")
    has_photo = False

    async def members(ids):
        return [await names.name(u) for u in ids or []]

    def setp(action, **kw):
        j["action"] = action
        for k, v in kw.items():
            j[k] = v

    reply_id = getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None)
    if n == "ChatCreate":
        setp("create_group", title=a.title, members=await members(a.users))
    elif n == "ChatEditTitle":
        setp("edit_group_title", title=a.title)
    elif n in ("ChatEditPhoto", "SuggestProfilePhoto"):
        j["action"] = "edit_group_photo" if n == "ChatEditPhoto" else "suggest_profile_photo"
        if cname(getattr(a, "photo", None)) == "Photo":
            photo_fields(j, a.photo)
            has_photo = True
    elif n == "ChatDeletePhoto":
        setp("delete_group_photo")
    elif n == "ChatAddUser":
        setp("invite_members", members=await members(a.users))
    elif n == "ChatDeleteUser":
        setp("remove_members", members=await members([a.user_id]))
    elif n == "ChatJoinedByLink":
        setp("join_group_by_link", inviter=await names.name(a.inviter_id))
    elif n == "ChannelCreate":
        setp("create_channel", title=a.title)
    elif n == "ChatMigrateTo":
        setp("migrate_to_supergroup")
    elif n == "ChannelMigrateFrom":
        setp("migrate_from_group", title=a.title)
    elif n == "PinMessage":
        setp("pin_message", message_id=reply_id)
    elif n == "HistoryClear":
        setp("clear_history")
    elif n == "GameScore":
        setp("score_in_game", game_message_id=reply_id, score=a.score)
    elif n in ("PaymentSent", "PaymentSentMe"):
        setp("send_payment", amount=a.total_amount, currency=a.currency, invoice_message_id=reply_id)
        if getattr(a, "recurring_init", False):
            j["recurring"] = "init"
        elif getattr(a, "recurring_used", False):
            j["recurring"] = "used"
    elif n in ("PhoneCall", "ConferenceCall"):
        j["action"] = "phone_call" if n == "PhoneCall" else "conference_call"
        if getattr(a, "duration", None):
            j["duration_seconds"] = a.duration
        r = cname(getattr(a, "reason", None)).replace("PhoneCallDiscardReason", "").lower()
        if r:
            j["discard_reason"] = {"migrateconferencecall": "migrate_conference_all"}.get(r, r)
    elif n == "ScreenshotTaken":
        setp("take_screenshot")
    elif n == "CustomAction":
        j["information_text"] = a.message
    elif n == "BotAllowed":
        act = "attach_menu_bot_allowed" if getattr(a, "attach_menu", False) else \
            "allow_sending_messages" if getattr(a, "from_request", False) else "web_app_bot_allowed"
        j["action"] = act
        app = getattr(a, "app", None)
        if app is not None:
            j["reason_app_id"] = getattr(app, "id", None)
            j["reason_app_name"] = getattr(app, "title", None)
        if getattr(a, "domain", None):
            j["reason_domain"] = a.domain
    elif n in ("SecureValuesSent", "SecureValuesSentMe"):
        setp("send_passport_values",
             values=[cname(v.type if hasattr(v, "type") else v).replace("SecureValueType", "")
                     for v in (getattr(a, "types", None) or getattr(a, "values", None) or [])])
    elif n == "ContactSignUp":
        setp("joined_telegram")
    elif n == "GeoProximityReached":
        setp("proximity_reached", **{"from": await names.name(a.from_id), "from_id": ent_key(a.from_id),
                                     "to": await names.name(a.to_id), "to_id": ent_key(a.to_id),
                                     "distance": a.distance})
    elif n == "GroupCall":
        setp("group_call")
        if getattr(a, "duration", None):
            j["duration"] = a.duration
    elif n == "InviteToGroupCall":
        setp("invite_to_group_call", members=await members(a.users))
    elif n == "SetMessagesTTL":
        setp("set_messages_ttl", period=a.period)
    elif n == "GroupCallScheduled":
        setp("group_call_scheduled", schedule_date=tg_date(a.schedule_date)[0])
    elif n == "SetChatTheme":
        setp("edit_chat_theme", emoticon=getattr(a, "emoticon", "") or "")
    elif n == "ChatJoinedByRequest":
        setp("join_group_by_request")
    elif n in ("WebViewDataSent", "WebViewDataSentMe"):
        setp("send_webview_data", text=getattr(a, "text", "") or "")
    elif n == "GiftPremium":
        setp("send_premium_gift", cost=getattr(a, "amount", None),
             days=getattr(a, "days", None) or (getattr(a, "months", 0) or 0) * 30)
    elif n == "TopicCreate":
        setp("topic_created", title=a.title)
    elif n == "TopicEdit":
        setp("topic_edit")
        if getattr(a, "title", None):
            j["new_title"] = a.title
        if getattr(a, "icon_emoji_id", None):
            j["new_icon_emoji_id"] = a.icon_emoji_id
    elif n in ("RequestedPeer", "RequestedPeerSentMe"):
        setp("requested_peer", button_id=a.button_id, peers=[ent_key(p) for p in a.peers])
    elif n == "GiftCode":
        setp("gift_code_prize", gift_code=a.slug,
             boost_peer_id=ent_key(a.boost_peer) if getattr(a, "boost_peer", None) else None,
             days=getattr(a, "days", None) or (getattr(a, "months", 0) or 0) * 30,
             unclaimed=bool(getattr(a, "unclaimed", False)), via_giveaway=bool(getattr(a, "via_giveaway", False)))
    elif n == "GiveawayLaunch":
        setp("giveaway_launch")
        if getattr(a, "stars", None):
            j["stars"] = a.stars
    elif n == "GiveawayResults":
        setp("giveaway_results", winners=a.winners_count, unclaimed=a.unclaimed_count,
             stars=bool(getattr(a, "stars", False)))
    elif n == "SetChatWallPaper":
        setp("set_same_chat_wallpaper" if getattr(a, "same", False) else "set_chat_wallpaper",
             message_id=reply_id)
    elif n == "BoostApply":
        setp("boost_apply", boosts=a.boosts)
    elif n == "PaymentRefunded":
        setp("refunded_payment", amount=a.total_amount, currency=a.currency,
             peer_name=await names.name(a.peer), peer_id=ent_key(a.peer),
             charge_id=getattr(getattr(a, "charge", None), "id", None))
    elif n == "GiftStars":
        setp("send_stars_gift", cost=a.amount, amount_whole=a.stars, amount_nano=0)
    elif n == "GiftTon":
        setp("send_ton_gift", cost=a.amount, amount_whole=a.crypto_amount // 10 ** 9,
             amount_nano=a.crypto_amount % 10 ** 9)
    elif n == "PrizeStars":
        setp("stars_prize", boost_peer_id=ent_key(a.boost_peer), boost_peer_name=await names.name(a.boost_peer),
             stars=a.stars, is_unclaimed=bool(getattr(a, "unclaimed", False)),
             giveaway_msg_id=a.giveaway_msg_id, transaction_id=a.transaction_id)
    elif n in ("StarGift", "StarGiftUnique"):
        g = a.gift
        setp("send_star_gift", gift_id=getattr(g, "id", None), stars=getattr(g, "stars", None),
             is_limited=bool(getattr(g, "limited", False)),
             is_anonymous=bool(getattr(a, "name_hidden", False)),
             gift_text=_txt(getattr(a, "message", None)))
        if n == "StarGiftUnique":
            j["is_unique"] = True
            if getattr(g, "title", None):
                j["gift_title"] = g.title
    elif n == "PaidMessagesRefunded":
        setp("paid_messages_refund", messages_count=a.count, stars_count=a.stars)
    elif n == "PaidMessagesPrice":
        setp("paid_messages_price_change", price_stars=a.stars,
             is_broadcast_messages_allowed=bool(getattr(a, "broadcast_messages_allowed", False)))
    elif n == "TodoCompletions":
        setp("todo_completions", completed=list(a.completed or []), incompleted=list(a.incompleted or []))
    elif n == "TodoAppendTasks":
        setp("todo_append_tasks", items=[{"text": _txt(t.title), "id": t.id} for t in a.list])
    elif n == "SuggestBirthday":
        b = a.birthday
        setp("suggest_birthday", day=b.day, month=b.month, year=getattr(b, "year", None))
    else:
        j["action"] = _snake(cname(a))
    # пустые значения Telegram не пишет
    for k in [k for k, v in j.items() if v is None]:
        del j[k]
    return has_photo


SERVICE_RU = {
    "create_group": "создал(а) группу «{title}»", "edit_group_title": "изменил(а) название на «{title}»",
    "edit_group_photo": "изменил(а) фото группы", "delete_group_photo": "удалил(а) фото группы",
    "invite_members": "добавил(а) {members}", "remove_members": "удалил(а) {members}",
    "join_group_by_link": "вступил(а) по ссылке-приглашению", "create_channel": "создал(а) канал «{title}»",
    "migrate_to_supergroup": "преобразовал(а) группу в супергруппу",
    "migrate_from_group": "группа «{title}» преобразована в супергруппу", "pin_message": "закрепил(а) сообщение",
    "clear_history": "очистил(а) историю", "score_in_game": "набрал(а) {score} очков",
    "send_payment": "оплатил(а) {amount} {currency}", "take_screenshot": "сделал(а) снимок экрана",
    "joined_telegram": "присоединился(ась) к Telegram", "group_call": "видеочат",
    "invite_to_group_call": "пригласил(а) в видеочат {members}",
    "set_messages_ttl": "установил(а) автоудаление: {period} с", "edit_chat_theme": "изменил(а) тему чата {emoticon}",
    "join_group_by_request": "принят(а) в группу по заявке", "topic_created": "создал(а) тему «{title}»",
    "topic_edit": "изменил(а) тему", "suggest_profile_photo": "предложил(а) фото профиля",
    "boost_apply": "забустил(а) группу", "send_premium_gift": "подарил(а) Telegram Premium",
    "send_stars_gift": "подарил(а) {amount_whole} звёзд", "giveaway_launch": "запустил(а) розыгрыш",
    "giveaway_results": "итоги розыгрыша: {winners} победителей", "set_chat_wallpaper": "установил(а) обои",
    "set_same_chat_wallpaper": "установил(а) такие же обои", "requested_peer": "поделился(ась) чатом",
    "send_webview_data": "данные из веб-приложения: {text}", "proximity_reached": "{from} рядом с {to} ({distance} м)",
    "group_call_scheduled": "запланировал(а) видеочат на {schedule_date}",
    "refunded_payment": "возврат платежа {amount} {currency}", "gift_code_prize": "получил(а) подарочный код Premium",
    "stars_prize": "выиграл(а) {stars} звёзд", "paid_messages_refund": "возврат {stars_count} звёзд за сообщения",
    "paid_messages_price_change": "цена сообщения: {price_stars} звёзд",
    "todo_completions": "отметил(а) пункты списка", "todo_append_tasks": "добавил(а) пункты в список",
    "suggest_birthday": "предложил(а) дату рождения {day}.{month}",
    "attach_menu_bot_allowed": "разрешил(а) бота в меню", "web_app_bot_allowed": "разрешил(а) веб-приложение",
    "allow_sending_messages": "разрешил(а) боту писать", "send_passport_values": "передал(а) данные Telegram Passport",
}


def service_text_ru(j):
    a = j.get("action")
    actor = j.get("actor") or ""
    if a is None:
        return j.get("information_text", "")
    if a in ("phone_call", "conference_call"):
        reason = {"missed": "пропущенный", "busy": "занято", "hangup": "", "disconnect": "разъединено"}.get(
            j.get("discard_reason", ""), "")
        d = j.get("duration_seconds")
        return f"📞 {'Групповой звонок' if a == 'conference_call' else 'Звонок'}" + \
            (f" · {fmt_duration(d)}" if d else "") + (f" ({reason})" if reason else "")
    if a == "send_star_gift":
        s = f"🎁 {actor} отправил(а) подарок"
        if j.get("stars"):
            s += f" · {j['stars']} ⭐"
        if j.get("gift_text"):
            s += f" — «{j['gift_text']}»"
        return s
    tpl = SERVICE_RU.get(a)
    if tpl is None:
        return f"{actor}: {a}"
    vals = dict(j)
    if isinstance(vals.get("members"), list):
        vals["members"] = ", ".join(m or "Удалённый аккаунт" for m in vals["members"])
    try:
        body = tpl.format(**{k: (v if v is not None else "") for k, v in vals.items()})
    except (KeyError, IndexError):
        body = tpl
    return f"{actor} {body}".strip() if a not in ("group_call", "proximity_reached", "migrate_from_group",
                                                  "giveaway_results", "refunded_payment") else body


# ---------- финальная сборка записи ----------

def finalize_record(rec, ce_paths):
    """Подставляет реальные пути файлов (или строки Telegram «File not included…»)."""
    j = rec["j"]
    mi = rec.get("media")
    if mi:
        val = mi["path"] if mi.get("status") == "ok" and mi.get("path") else SKIP_PATH.get(mi.get("status"), UNAVAILABLE)
        for k in ("photo", "file"):
            if j.get(k) == FILE_PH:
                j[k] = val
        if j.get("thumbnail") == THUMB_PH:
            th = mi.get("thumb")
            if th:
                j["thumbnail"], j["thumbnail_file_size"] = th
            else:
                j.pop("thumbnail", None)
                j.pop("thumbnail_file_size", None)

    def ce(v):
        if isinstance(v, str) and v.startswith(CE_PH):
            return ce_paths.get(int(v[len(CE_PH):]), UNAVAILABLE)
        return v

    for part in (j.get("text") if isinstance(j.get("text"), list) else []):
        if isinstance(part, dict) and "document_id" in part:
            part["document_id"] = ce(part["document_id"])
    for part in j.get("text_entities", []):
        if "document_id" in part:
            part["document_id"] = ce(part["document_id"])
    for r in j.get("reactions", []) or []:
        if "document_id" in r:
            r["document_id"] = ce(r["document_id"])
    return j


def chat_type(entity, me_id=None):
    n = cname(entity)
    if n == "User":
        if getattr(entity, "is_self", False):
            return "saved_messages"
        return "bot_chat" if getattr(entity, "bot", False) else "personal_chat"
    if n in ("Chat", "ChatForbidden"):
        return "private_group"
    if getattr(entity, "broadcast", False):
        return "public_channel" if getattr(entity, "username", None) else "private_channel"
    return "public_supergroup" if getattr(entity, "username", None) else "private_supergroup"


def write_json(out_dir, entity, title, records):
    ct = chat_type(entity)
    top = {}
    if ct != "saved_messages":
        top["name"] = title
    top["type"] = ct
    top["id"] = entity.id
    top["messages"] = [r["j"] for r in records]
    tmp = os.path.join(out_dir, "result.json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(top, f, ensure_ascii=False, indent=1)
    os.replace(tmp, os.path.join(out_dir, "result.json"))


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
.via{font-weight:400;color:#70777b}
.text{word-wrap:break-word;overflow-wrap:anywhere}
.message.service{padding:12px 16px}
.message.service .body{margin:0;text-align:center}
.message.service .details{display:inline-block;background:#f1f3f5;border-radius:12px;padding:3px 12px}
.message.service img.photo{margin:8px auto 0}
.forwarded{border-left:2px solid #65aadd;padding-left:10px;margin:2px 0 4px}
.forwarded .from_name{color:#3a83c5}
.reply_to{border-left:2px solid #65aadd;padding-left:8px;margin:2px 0 6px;font-size:12px;line-height:16px}
.reply_to .rname{color:#3a83c5;font-weight:600}
.media_wrap{padding:4px 0}
.photo{max-width:320px;max-height:320px;border-radius:8px;display:block}
.video,.gif{max-width:360px;max-height:360px;border-radius:8px;display:block;background:#000}
.round{width:220px;height:220px;border-radius:50%;object-fit:cover;background:#000;display:block}
.sticker{max-width:180px;max-height:180px;display:block}
img.emoji{width:20px;height:20px;vertical-align:-4px}
audio{width:320px;max-width:100%}
.media_caption{font-size:12px;color:#70777b;padding-top:2px}
.media_file,.media_missing,.media_other,.media_webpage,.media_poll,.media_card{display:inline-flex;flex-direction:column;padding:8px 12px;border-radius:8px;background:#f1f3f5;color:#000;max-width:420px}
.media_file{flex-direction:row;align-items:center;gap:12px;color:#000}
.media_file .icon{width:42px;height:42px;border-radius:50%;background:#4f9cd9;color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px;flex:none;overflow:hidden}
.media_file .icon img{width:42px;height:42px;object-fit:cover}
.media_webpage{border-left:3px solid #65aadd;border-radius:4px}
.dice{font-size:28px;line-height:34px}
.poll_answer{padding-top:4px;min-width:260px}
.poll_bar{height:4px;background:#65aadd;border-radius:2px;margin-top:2px}
.reactions{display:flex;flex-wrap:wrap;gap:4px;padding-top:4px}
.reaction{display:inline-flex;align-items:center;gap:4px;background:#eef4fb;border-radius:12px;padding:2px 8px;font-size:12px}
.reaction img{width:18px;height:18px}
.buttons{display:grid;gap:4px;padding-top:6px;max-width:420px}
.buttons .row{display:flex;gap:4px}
.buttons .btn{flex:1;text-align:center;background:#eef4fb;border-radius:6px;padding:5px 8px;font-size:12px;color:#168acd}
.signature{font-size:12px;color:#70777b;padding-top:2px}
.pagination{display:block;text-align:center;padding:14px;margin:6px 16px;border-radius:8px;background:#f1f3f5;font-weight:600}
blockquote{border-left:2px solid #65aadd;margin:4px 0;padding-left:8px}
pre,code{font-family:Consolas,Menlo,monospace;background:#f5f5f5;border-radius:4px;padding:0 3px}
pre{padding:6px 8px;white-space:pre-wrap}
.spoiler{background:#c8ccd0;color:transparent;border-radius:3px;cursor:pointer}
.spoiler:hover{background:transparent;color:inherit}
"""


def page_name(n):
    return "messages.html" if n == 1 else f"messages{n}.html"


def color_idx(key):
    m = re.search(r"\d+", key or "")
    return int(m.group()) % 7 if m else 0


def is_path(v):
    return isinstance(v, str) and v and not v.startswith("(")


def url_of(rel):
    return quote(rel)


def text_html(entities):
    out = []
    for p in entities or []:
        t = p.get("type")
        s = esc(p.get("text", "")).replace("\n", "<br>")
        if t == "plain":
            out.append(s)
        elif t == "bold":
            out.append(f"<b>{s}</b>")
        elif t == "italic":
            out.append(f"<i>{s}</i>")
        elif t == "underline":
            out.append(f"<u>{s}</u>")
        elif t == "strikethrough":
            out.append(f"<s>{s}</s>")
        elif t == "code":
            out.append(f"<code>{s}</code>")
        elif t == "pre":
            out.append(f"<pre>{s}</pre>")
        elif t == "spoiler":
            out.append(f"<span class='spoiler'>{s}</span>")
        elif t == "blockquote":
            out.append(f"<blockquote>{s}</blockquote>")
        elif t == "link":
            href = p.get("text", "")
            href = href if re.match(r"^[a-z]+://", href, re.I) else "http://" + href
            out.append(f"<a href='{esc(href)}' target='_blank'>{s}</a>")
        elif t == "text_link":
            out.append(f"<a href='{esc(p.get('href', ''))}' target='_blank'>{s}</a>")
        elif t == "email":
            out.append(f"<a href='mailto:{esc(p.get('text', ''))}'>{s}</a>")
        elif t == "phone":
            out.append(f"<a href='tel:{esc(p.get('text', ''))}'>{s}</a>")
        elif t == "mention":
            out.append(f"<a href='https://t.me/{esc(p.get('text', '').lstrip('@'))}' target='_blank'>{s}</a>")
        elif t == "custom_emoji":
            d = p.get("document_id")
            if is_path(d) and d.lower().endswith((".webp", ".png", ".jpg")):
                out.append(f"<img class='emoji' src='{url_of(d)}' alt='{s}'>")
            else:
                out.append(s)
        else:
            out.append(s)
    return "".join(out)


def preview_of(j):
    if j.get("type") == "service":
        return service_text_ru(j)
    t = j.get("text")
    s = t if isinstance(t, str) else "".join(x if isinstance(x, str) else x.get("text", "") for x in t or [])
    s = s.strip().replace("\n", " ")[:80]
    if s:
        return s
    mt = j.get("media_type")
    return {"sticker": "Стикер " + j.get("sticker_emoji", ""), "voice_message": "Голосовое сообщение",
            "video_message": "Видеосообщение", "animation": "GIF", "video_file": "Видео",
            "audio_file": "Аудио"}.get(mt) or ("Фото" if "photo" in j else "Файл" if "file" in j else
                                               "Опрос" if "poll" in j else "Контакт" if "contact_information" in j
                                               else "Геопозиция" if "location_information" in j else "Сообщение")


def media_html(j, extra=""):
    parts = []
    ru = {NOT_INCLUDED: "Не скачано: этот тип отключён в настройках экспорта",
          TOO_BIG: f"Не скачано: больше лимита {SIZE_LIMIT_MB} МБ",
          UNAVAILABLE: "Файл недоступен — запустите экспорт ещё раз, чтобы докачать"}
    missing = lambda label, v: (f"<div class='media_missing'><div class='bold'>{esc(label)}</div>"
                                f"<div class='details'>{esc(ru.get(v, v))}</div></div>")
    if "photo" in j:
        v = j["photo"]
        if is_path(v):
            parts.append(f"<a href='{url_of(v)}' target='_blank'><img class='photo' src='{url_of(v)}' loading='lazy'></a>")
        else:
            parts.append(missing(f"Фото · {human(j.get('photo_file_size'))}", v))
    if "file" in j:
        v, mt = j["file"], j.get("media_type")
        th = j.get("thumbnail") if is_path(j.get("thumbnail")) else None
        info = " · ".join(x for x in [fmt_duration(j.get("duration_seconds")), human(j.get("file_size"))] if x)
        label = {"sticker": "Стикер", "video_message": "Видеосообщение", "voice_message": "Голосовое сообщение",
                 "animation": "GIF", "video_file": "Видео", "audio_file": "Аудио"}.get(mt, "Файл")
        if not is_path(v):
            parts.append(missing(f"{label} · {info}" if info else label, v))
        else:
            u = url_of(v)
            low = v.lower()
            if mt == "sticker":
                if low.endswith(".tgs"):
                    parts.append(f"<img class='sticker' src='{url_of(th)}'>" if th else
                                 f"<div class='media_other'>{esc(j.get('sticker_emoji', ''))} Стикер</div>")
                elif low.endswith(".webm"):
                    parts.append(f"<video class='sticker' src='{u}' autoplay loop muted playsinline></video>")
                else:
                    parts.append(f"<img class='sticker' src='{u}' loading='lazy'>")
            elif mt == "video_message":
                parts.append(f"<video class='round' src='{u}' controls preload='metadata'></video>")
            elif mt == "voice_message":
                parts.append(f"<audio src='{u}' controls preload='none'></audio>"
                             f"<div class='media_caption'>Голосовое · {esc(info)}</div>")
            elif mt == "animation":
                parts.append(f"<video class='gif' src='{u}' autoplay loop muted playsinline preload='metadata'></video>")
            elif mt == "video_file":
                poster = f" poster='{url_of(th)}'" if th else ""
                parts.append(f"<video class='video' src='{u}' controls preload='none'{poster}></video>"
                             f"<div class='media_caption'>Видео · {esc(info)}</div>")
            elif mt == "audio_file":
                nm = " — ".join(x for x in [j.get("performer"), j.get("title")] if x) or j.get("file_name", "")
                parts.append(f"<div class='media_card'><div class='bold'>🎵 {esc(nm)}</div>"
                             f"<audio src='{u}' controls preload='none'></audio>"
                             f"<div class='details'>{esc(info)}</div></div>")
            else:
                icon = f"<img src='{url_of(th)}'>" if th else "📄"
                parts.append(f"<a class='media_file' href='{u}' target='_blank'><div class='icon'>{icon}</div>"
                             f"<div><div class='bold'>{esc(j.get('file_name') or v.split('/')[-1])}</div>"
                             f"<div class='details'>{esc(info)}</div></div></a>")
    if "contact_information" in j:
        c = j["contact_information"]
        nm = " ".join(x for x in [c.get("first_name"), c.get("last_name")] if x)
        vc = f"<a href='{url_of(j['contact_vcard'])}'>vCard</a>" if is_path(j.get("contact_vcard")) else ""
        parts.append(f"<div class='media_card'><div class='bold'>👤 {esc(nm)}</div>"
                     f"<div class='details'>{esc(c.get('phone_number'))} {vc}</div></div>")
    if "location_information" in j:
        li = j["location_information"]
        title = j.get("place_name") or ("Трансляция геопозиции" if "live_location_period_seconds" in j else "Геопозиция")
        if li:
            url = f"https://maps.google.com/maps?q={li['latitude']},{li['longitude']}"
            parts.append(f"<a class='media_card' href='{url}' target='_blank'><div class='bold'>📍 {esc(title)}</div>"
                         f"<div class='details'>{esc(j.get('address', ''))} {li['latitude']:.6f}, {li['longitude']:.6f}</div></a>")
        else:
            parts.append(f"<div class='media_card'>📍 {esc(title)}</div>")
    if "poll" in j:
        p = j["poll"]
        tot = p.get("total_voters") or 0
        rows = "".join(
            f"<div class='poll_answer'>{'✔ ' if a.get('chosen') else ''}{esc(a['text'])}"
            f"<span class='details'> — {a.get('voters', 0)}</span>"
            f"<div class='poll_bar' style='width:{(a.get('voters', 0) / tot * 100) if tot else 0:.0f}%'></div></div>"
            for a in p.get("answers", []))
        parts.append(f"<div class='media_poll'><div class='bold'>📊 {esc(p.get('question'))}</div>{rows}"
                     f"<div class='details'>{tot} голосов{' · опрос завершён' if p.get('closed') else ''}</div></div>")
    if "todo_list" in j:
        t = j["todo_list"]
        rows = "".join(f"<div class='poll_answer'>☐ {esc(a['text'])}</div>" for a in t.get("answers", []))
        parts.append(f"<div class='media_poll'><div class='bold'>📝 {esc(t.get('title'))}</div>{rows}</div>")
    if "game_title" in j:
        parts.append(f"<div class='media_card'><div class='bold'>🎮 {esc(j['game_title'])}</div>"
                     f"<div class='details'>{esc(j.get('game_description'))}</div></div>")
    if "invoice_information" in j:
        iv = j["invoice_information"]
        parts.append(f"<div class='media_card'><div class='bold'>🧾 {esc(iv.get('title'))}</div>"
                     f"<div class='details'>{esc(iv.get('description'))} · {iv.get('amount')} {esc(iv.get('currency'))}</div></div>")
    if "giveaway_information" in j:
        g = j["giveaway_information"]
        parts.append(f"<div class='media_card'><div class='bold'>🎉 Розыгрыш: {g.get('quantity')} призов</div>"
                     f"<div class='details'>до {esc(g.get('until_date'))}</div></div>")
    if "giveaway_results" in j:
        g = j["giveaway_results"]
        parts.append(f"<div class='media_card'><div class='bold'>🏆 Итоги розыгрыша</div>"
                     f"<div class='details'>победителей: {g.get('winners_count')}</div></div>")
    if extra:
        parts.append(extra)
    return "".join(parts)


def reactions_html(j):
    rs = j.get("reactions")
    if not rs:
        return ""
    out = []
    for r in rs:
        who = ", ".join(x.get("from") or "?" for x in r.get("recent", []))
        if r["type"] == "emoji":
            icon = esc(r.get("emoji"))
        elif r["type"] == "custom_emoji" and is_path(r.get("document_id")) and \
                r["document_id"].lower().endswith((".webp", ".png", ".jpg")):
            icon = f"<img src='{url_of(r['document_id'])}'>"
        elif r["type"] == "paid":
            icon = "⭐"
        else:
            icon = "✨"
        out.append(f"<span class='reaction' title='{esc(who)}'>{icon} {r.get('count', 0)}</span>")
    return f"<div class='reactions'>{''.join(out)}</div>"


def buttons_html(j):
    rows = j.get("inline_bot_buttons")
    if not rows:
        return ""
    html_rows = []
    for row in rows:
        cells = []
        for b in row:
            d = b.get("data", "")
            if b.get("type") in ("url", "web_view", "simple_web_view", "auth") and d:
                cells.append(f"<a class='btn' href='{esc(d)}' target='_blank'>{esc(b.get('text'))}</a>")
            else:
                cells.append(f"<span class='btn'>{esc(b.get('text'))}</span>")
        html_rows.append(f"<div class='row'>{''.join(cells)}</div>")
    return f"<div class='buttons'>{''.join(html_rows)}</div>"


def render_html(out_dir, title, subtitle, records):
    css_dir = os.path.join(out_dir, "css")
    os.makedirs(css_dir, exist_ok=True)
    with open(os.path.join(css_dir, "style.css"), "w", encoding="utf-8") as f:
        f.write(CSS)

    pages = [records[i:i + MESSAGES_PER_PAGE] for i in range(0, len(records), MESSAGES_PER_PAGE)] or [[]]
    page_of, previews = {}, {}
    for pi, chunk in enumerate(pages, 1):
        for r in chunk:
            page_of[r["key"]] = pi
            previews[r["key"]] = (r["j"].get("from") or r["j"].get("actor"), preview_of(r["j"]))

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
            j = r["j"]
            d = r["date"]
            if d.date() != last_day:
                out.append(f"<div class='message service'><div class='body'>"
                           f"<div class='details'>{d.day} {MONTHS[d.month - 1]} {d.year}</div></div></div>")
                last_day = d.date()
                prev = None

            def link_to(key):
                rp = page_of[key]
                return f"#message{key}" if rp == pi else f"{page_name(rp)}#message{key}"

            if j.get("type") == "service":
                link = f" <a href='{link_to(r['reply_key'])}'>→</a>" if r.get("reply_key") in page_of else ""
                photo = ""
                if is_path(j.get("photo")):
                    photo = f"<img class='photo' src='{url_of(j['photo'])}'>"
                out.append(f"<div class='message service' id='message{r['key']}'><div class='body'>"
                           f"<div class='details'>{esc(service_text_ru(j))}{link}</div>{photo}</div></div>")
                prev = None
                continue

            joined = (prev is not None and "forwarded_from" not in j and "forwarded_from" not in prev["j"]
                      and prev["j"].get("from_id") == j.get("from_id")
                      and (d - prev["date"]).total_seconds() < 900)
            ci = color_idx(j.get("from_id"))
            z = d.strftime("%z")
            full_dt = d.strftime("%d.%m.%Y %H:%M:%S") + f" UTC{z[:3]}:{z[3:]}"
            name = j.get("from") or "Удалённый аккаунт"
            p = [f"<div class='message default clearfix{' joined' if joined else ''}' id='message{r['key']}'>"]
            if not joined:
                p.append(f"<div class='pull_left'><div class='userpic c{ci}'>{esc(initials(name))}</div></div>")
            p.append("<div class='body'>")
            p.append(f"<div class='pull_right date details' title='{full_dt}'>{d.strftime('%H:%M')}</div>")
            if not joined:
                via = f" <span class='via'>via {esc(j['via_bot'])}</span>" if j.get("via_bot") else ""
                p.append(f"<div class='from_name n{ci}'>{esc(name)}{via}</div>")
            if r.get("reply_key"):
                rk = r["reply_key"]
                if rk in page_of:
                    rname, snip = previews.get(rk, (None, ""))
                    p.append(f"<a class='reply_to' href='{link_to(rk)}' style='display:block'>"
                             f"<div class='rname'>{esc(rname or 'Сообщение')}</div>"
                             f"<div class='details'>{esc(snip)}</div></a>")
                else:
                    p.append("<div class='reply_to details'>В ответ на сообщение, которого нет в экспорте</div>")
            inner = []
            mh = media_html(j, r.get("extra_html", ""))
            if mh:
                inner.append(f"<div class='media_wrap clearfix'>{mh}</div>")
            th = text_html(j.get("text_entities"))
            if th:
                inner.append(f"<div class='text'>{th}</div>")
            if "forwarded_from" in j or "saved_from" in j:
                src = j.get("forwarded_from") or j.get("saved_from")
                p.append(f"<div class='forwarded'><div class='from_name'>Переслано от {esc(src)}</div>"
                         + "".join(inner) + "</div>")
            else:
                p.extend(inner)
            p.append(buttons_html(j))
            p.append(reactions_html(j))
            sig = []
            if j.get("author"):
                sig.append(esc(j["author"]))
            if j.get("edited"):
                sig.append("изменено")
            if sig:
                p.append(f"<div class='signature'>{' · '.join(sig)}</div>")
            p.append("</div></div>")
            out.append("".join(p))
            prev = r
        if pi < len(pages):
            out.append(f"<a class='pagination' href='{page_name(pi + 1)}'>↓ Следующие сообщения</a>")
        out.append("</div></body></html>")
        with open(os.path.join(out_dir, page_name(pi)), "w", encoding="utf-8") as f:
            f.write("\n".join(out))
    return len(pages)


async def build_record(msg, key, prefix, title, names, namer, out_dir):
    """Сообщение Telethon → запись в формате Telegram Desktop (+ служебные поля для HTML)."""
    names.remember(getattr(msg, "sender", None))
    j = {"id": msg.id}
    is_service = getattr(msg, "action", None) is not None
    j["type"] = "service" if is_service else "message"
    j["date"], j["date_unixtime"] = tg_date(msg.date)
    if getattr(msg, "edit_date", None):
        j["edited"], j["edited_unixtime"] = tg_date(msg.edit_date)
    from_peer = getattr(msg, "from_id", None) or getattr(msg, "peer_id", None)
    sender_name = ent_name(getattr(msg, "sender", None))
    if sender_name is None and from_peer is not None:
        sender_name = await names.name(from_peer)
    has_photo = False
    custom = []
    extra = ""
    if is_service:
        j["actor"] = sender_name
        j["actor_id"] = ent_key(from_peer)
        has_photo = await service_fields(j, msg, names)
    else:
        j["from"] = sender_name
        j["from_id"] = ent_key(from_peer)
        if getattr(msg, "post_author", None):
            j["author"] = msg.post_author
        fwd = getattr(msg, "fwd_from", None)
        if fwd is not None:
            fname = None
            if getattr(fwd, "from_id", None) is not None:
                fname = await names.name(fwd.from_id)
            j["forwarded_from"] = fname or getattr(fwd, "from_name", None)
            if getattr(fwd, "from_id", None) is not None:
                j["forwarded_from_id"] = ent_key(fwd.from_id)
            if getattr(fwd, "saved_from_peer", None) is not None:
                j["saved_from"] = await names.name(fwd.saved_from_peer)
        rt = getattr(msg, "reply_to", None)
        if rt is not None and getattr(rt, "reply_to_msg_id", None):
            j["reply_to_message_id"] = rt.reply_to_msg_id
            if getattr(rt, "reply_to_peer_id", None) is not None:
                j["reply_to_peer_id"] = ent_key(rt.reply_to_peer_id)
        vb = getattr(msg, "via_bot_id", None)
        if vb:
            bot = getattr(msg, "via_bot", None)
            un = getattr(bot, "username", None)
            if un is None:
                try:
                    un = getattr(await names.client.get_entity(vb), "username", None)
                except Exception:
                    un = None
            if un:
                j["via_bot"] = "@" + un
        m = msg.media
        n = cname(m)
        if n == "MessageMediaPhoto" and cname(getattr(m, "photo", None)) == "Photo":
            photo_fields(j, m.photo, getattr(m, "spoiler", False), getattr(m, "ttl_seconds", None))
        elif n == "MessageMediaDocument" and cname(getattr(m, "document", None)) == "Document":
            document_fields(j, m.document, getattr(m, "spoiler", False), getattr(m, "ttl_seconds", None))
        elif m is not None:
            extra = other_media_fields(j, msg, namer, out_dir, key)
    # пустые значения Telegram не пишет (кроме from=null у удалённых аккаунтов)
    for k in [k for k, v in j.items() if v is None and k not in ("from", "actor", "location_information")]:
        del j[k]
    text, ents, c1 = build_text(getattr(msg, "message", "") or "", getattr(msg, "entities", None))
    custom += c1
    j["text"], j["text_entities"] = text, ents
    btns = build_buttons(msg)
    if btns:
        j["inline_bot_buttons"] = btns
    reacts, c2 = await build_reactions(msg, names)
    custom += c2
    if reacts:
        j["reactions"] = reacts
    rt = getattr(msg, "reply_to", None)
    reply = getattr(rt, "reply_to_msg_id", None) if rt is not None else None
    if rt is not None and getattr(rt, "forum_topic", False) and not getattr(rt, "reply_to_top_id", None):
        reply = None   # в форумах это «начало темы», а не настоящий ответ
    if rt is not None and getattr(rt, "reply_to_peer_id", None) is not None:
        reply = None   # ответ на сообщение из другого чата
    return {"key": key, "j": j, "date": msg.date.astimezone(), "custom": custom, "extra_html": extra,
            "reply_key": f"{prefix}{reply}" if reply else None, "has_photo": has_photo}



def types_ns(**kw):
    import types as _t
    return _t.SimpleNamespace(**kw)


def old_target_path(out_dir, msg, kind, prefix=""):
    """Имя файла в предыдущей версии программы — чтобы не перекачивать уже скачанное."""
    f = msg.file
    name = getattr(f, "name", None)
    ext = getattr(f, "ext", None) or ""
    base = f"{prefix}{msg.id}_{name}" if name else f"{prefix}{msg.id}{ext}"
    return os.path.join(out_dir, kind, safe(base))


# =============================== ядро экспорта ===============================

STALL_TIMEOUT = 120


class Progress:
    """Общий счётчик байтов: пишет фоновый поток, читает окно."""

    def __init__(self):
        self.reset(0)

    def reset(self, total, n=0):
        self.total = total
        self.n = n

    def update(self, delta):
        self.n += delta


CHUNK = 512 * 1024      # размер куска; докачка продолжается с границы куска
SEGMENT_MIN = 4 * CHUNK  # файлы больше этого качаются в несколько потоков
THREADS_PER_FILE = 4


def _chunk_len(i, size):
    return min(CHUNK, size - i * CHUNK)


def _load_meta(path, size):
    try:
        with open(path + ".part.json", encoding="utf-8") as f:
            m = json.load(f)
        if m.get("size") == size:
            return set(m.get("done", []))
    except Exception:
        pass
    return None


def part_bytes(path, size=0):
    """Сколько байт недокачанного файла уже лежит на диске."""
    done = _load_meta(path, size) if size else None
    if done is not None:
        return sum(_chunk_len(i, size) for i in done if i * CHUNK < size)
    try:
        return os.path.getsize(path + ".part") // CHUNK * CHUNK
    except OSError:
        return 0


def is_complete(path, size):
    try:
        return os.path.exists(path) and (not size or os.path.getsize(path) == size)
    except OSError:
        return False


async def _watch(task, state):
    """Ждёт задачу; если STALL_TIMEOUT секунд не пришло ни одного байта — считает зависшей."""
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=5)
            if not task.done() and time.monotonic() - state["t"] > STALL_TIMEOUT:
                raise TimeoutError(f"нет прогресса {STALL_TIMEOUT} с")
        task.result()
    except BaseException:
        if not task.done():
            task.cancel()
            await asyncio.wait({task}, timeout=10)
        raise


async def download_one(client, msg, path, size, bar, obj=None):
    """Качает файл в path.part с ДОКАЧКОЙ после любого сбоя.
    Большие файлы качаются кусками в THREADS_PER_FILE потоков одновременно
    (прогресс по кускам хранится в path.part.json)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part, meta_path = path + ".part", path + ".part.json"
    media = obj if obj is not None else media_object(msg)
    if media is None:
        raise RuntimeError("файл недоступен")
    state = {"t": time.monotonic()}

    done = _load_meta(path, size) if size else None
    if done is None and os.path.exists(meta_path):
        # прогресс от другой версии файла (размер не совпал) — начинаем этот файл заново
        if os.path.exists(part):
            bar.update(-(os.path.getsize(part) // CHUNK * CHUNK))
        for p_ in (part, meta_path):
            try:
                os.remove(p_)
            except OSError:
                pass
    segmented = size and (done is not None or (size > SEGMENT_MIN and THREADS_PER_FILE > 1))

    if segmented:
        if done is None:   # был обычный .part от последовательной загрузки — его полные куски засчитываем
            existing = os.path.getsize(part) if os.path.exists(part) else 0
            done = set(range(existing // CHUNK))
            if existing % CHUNK:
                bar.update(-(existing % CHUNK))
        nchunks = (size + CHUNK - 1) // CHUNK
        todo = collections.deque(i for i in range(nchunks) if i not in done)
        mode = "r+b" if os.path.exists(part) else "w+b"
        f = open(part, mode)
        last_save = [0.0]

        def save_meta(force=False):
            if force or time.monotonic() - last_save[0] > 2:
                # сначала данные гарантированно на диск, потом отметка «кусок готов» —
                # иначе при сбое отметка могла бы опередить сами данные
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
                tmp = meta_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as mf:
                    json.dump({"size": size, "done": sorted(done)}, mf)
                os.replace(tmp, meta_path)
                last_save[0] = time.monotonic()

        async def worker():
            while todo:
                i = todo.popleft()
                data = b""
                async for chunk in client.iter_download(media, offset=i * CHUNK, request_size=CHUNK,
                                                        limit=1, file_size=size):
                    data += chunk
                need = _chunk_len(i, size)
                if len(data) < need:
                    todo.appendleft(i)
                    raise RuntimeError(f"кусок {i} пришёл не полностью")
                f.seek(i * CHUNK)
                f.write(data[:need])
                done.add(i)
                bar.update(need)
                state["t"] = time.monotonic()
                save_meta()

        async def run():
            ts = [asyncio.create_task(worker()) for _ in range(min(THREADS_PER_FILE, len(todo)) or 1)]
            try:
                fin, pend = await asyncio.wait(ts, return_when=asyncio.FIRST_EXCEPTION)
            except BaseException:
                for t in ts:
                    t.cancel()
                await asyncio.wait(ts)
                raise
            for t in pend:          # один поток упал — останавливаем остальные
                t.cancel()
            if pend:
                await asyncio.wait(pend)
            for t in fin:
                t.result()

        task = asyncio.create_task(run())
        try:
            await _watch(task, state)
        finally:
            try:
                save_meta(force=True)   # сначала сохраняем прогресс (данные + отметки), потом закрываем
            except Exception:
                pass
            try:
                f.close()
            except Exception:
                pass
        if len(done) < nchunks:
            raise RuntimeError("файл скачан не полностью, будет докачан")
        if os.path.getsize(part) < size:
            raise RuntimeError("размер файла не совпал, будет докачан")
        with open(part, "r+b") as f2:
            f2.truncate(size)
        os.replace(part, path)
        try:
            os.remove(meta_path)
        except OSError:
            pass
        return path

    # --- последовательная загрузка (мелкие файлы) ---
    existing = os.path.getsize(part) if os.path.exists(part) else 0
    offset = existing // CHUNK * CHUNK
    if offset != existing:                      # обрезаем недописанный хвост
        with open(part, "r+b") as f:
            f.truncate(offset)
        bar.update(offset - existing)

    async def run_seq():
        if size and offset >= size:
            return
        with open(part, "ab") as f:
            async for chunk in client.iter_download(media, offset=offset, request_size=CHUNK,
                                                    file_size=size or None):
                f.write(chunk)
                f.flush()
                bar.update(len(chunk))
                state["t"] = time.monotonic()

    await _watch(asyncio.create_task(run_seq()), state)
    got = os.path.getsize(part) if os.path.exists(part) else 0
    if got == 0 or (size and got < size):
        raise RuntimeError(f"файл скачан не полностью ({human(got)} из {human(size)}), будет докачан")
    os.replace(part, path)
    return path


def proxy_target(cfg):
    """Куда реально идёт подключение: (вид, сервер, порт, секрет/логин) — ссылка важнее полей, как в make_client."""
    ptype = cfg.get("proxy_type") or ""
    host, port = str(cfg.get("proxy_host") or ""), str(cfg.get("proxy_port") or "")
    if ptype == "mtproto":
        secret = cfg.get("proxy_secret") or ""
        link = (cfg.get("proxy_link") or "").strip()
        if link:
            try:
                q = parse_qs(urlparse(link.replace("tg://proxy?", "https://t.me/proxy?")).query)
                host = q.get("server", [host])[0]
                port = q.get("port", [port])[0]
                secret = q.get("secret", [secret])[0]
            except Exception:
                pass
        return ("mtproto", host, port, (secret or "").strip())
    if ptype == "socks5":
        return ("socks5", host, port, cfg.get("proxy_user") or "")
    return ("", "", "", "")


def proxy_label(cfg):
    t = proxy_target(cfg)
    if t[0] == "mtproto":
        return f"MTProto {t[1]}:{t[2]}"
    if t[0] == "socks5":
        return f"SOCKS5 {t[1]}:{t[2]}"
    return "без прокси"


def make_client(cfg, log, session=None, quick=False):
    kwargs = dict(connection_retries=10, retry_delay=3, request_retries=10, timeout=30,
                  flood_sleep_threshold=120)
    if quick:      # диагностика: не ждать минутами, а быстро сказать «не работает»
        kwargs.update(connection_retries=1, retry_delay=1, request_retries=1, timeout=12, auto_reconnect=False)
    ptype = cfg["proxy_type"]
    if ptype == "mtproto":
        host, port, secret = cfg["proxy_host"], cfg["proxy_port"], cfg["proxy_secret"]
        if cfg["proxy_link"].strip():
            link = cfg["proxy_link"].strip().replace("tg://proxy?", "https://t.me/proxy?")
            q = parse_qs(urlparse(link).query)
            host = q.get("server", [host])[0]
            port = q.get("port", [port])[0]
            secret = q.get("secret", [secret])[0]
        secret = (secret or "").strip()
        if not secret:
            raise ValueError("Не указан секрет прокси: вставьте ссылку tg://proxy... или секрет.")
        if secret.lower().startswith("ee"):
            raise ValueError("Секрет начинается с «ee» — это режим Fake TLS, он не поддерживается.\n"
                             "Выключите Fake TLS в настройках tg-ws-proxy (нужен секрет с «dd») "
                             "и скопируйте ссылку заново.")
        log(f"Подключение через MTProto-прокси {host}:{port}")
        kwargs["connection"] = connection.ConnectionTcpMTProxyRandomizedIntermediate
        kwargs["proxy"] = (host, int(port), secret)
    elif ptype == "socks5":
        try:
            import python_socks  # noqa: F401
        except ImportError:
            raise ValueError("Для SOCKS5 нужен пакет python-socks: pip install python-socks[asyncio]")
        log(f"Подключение через SOCKS5 {cfg['proxy_host']}:{cfg['proxy_port']}")
        kwargs["proxy"] = {
            "proxy_type": "socks5", "addr": cfg["proxy_host"], "port": int(cfg["proxy_port"]),
            "username": cfg["proxy_user"] or None, "password": cfg["proxy_pass"] or None, "rdns": True,
        }
    else:
        log("Подключение напрямую (без прокси)")
    return TelegramClient(session if session is not None else SESSION_PATH, int(cfg["api_id"]),
                          cfg["api_hash"].strip(), **kwargs)


class StopRequested(Exception):
    pass


class SwitchToQR(Exception):
    """Код по номеру не пришёл — пользователь выбрал вход по QR-коду."""


def folder_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def build_report(title, out_dir, records, sess, cfg, ok_total, want_total, not_ok, labels, pages, ce_count):
    import datetime as _dt
    now = time.time()
    took = now - sess.get("started", now)
    secs = sess.get("dl_seconds", 0) or 0
    b = sess.get("bytes", 0)
    speed = b / secs if secs > 0 else 0
    n_msgs = sum(1 for r in records if r["j"].get("type") == "message")
    n_serv = sum(1 for r in records if r["j"].get("type") == "service")
    n_react = sum(1 for r in records if r["j"].get("reactions"))
    first = records[0]["date"] if records else None
    last = records[-1]["date"] if records else None

    by = {}
    for r in records:
        mi, k = r.get("media"), r.get("kind")
        if not mi or not k:
            continue
        d = by.setdefault(k, {"total": 0, "ok": 0, "ok_bytes": 0, "other": collections.Counter()})
        d["total"] += 1
        if mi["status"] == "ok":
            d["ok"] += 1
            try:
                d["ok_bytes"] += os.path.getsize(abs_path(out_dir, mi["path"]))
            except OSError:
                pass
        else:
            d["other"][mi["status"]] += 1
    complete = ok_total >= want_total
    L = []
    L.append(("✅ Экспорт завершён полностью" if complete else "⚠ Экспорт завершён, но скачано не всё"))
    L.append(f"Чат: {title}")
    L.append(f"Папка: {out_dir}")
    L.append(f"Завершено: {_dt.datetime.now():%d.%m.%Y %H:%M}")
    L.append("")
    L.append("ОБЩЕЕ")
    L.append(f"  Время работы: {fmt_duration(took) or '0:00'}" +
             (f" (автоперезапусков после сбоев: {sess['restarts']})" if sess.get("restarts") else ""))
    L.append(f"  Скачано за этот запуск: {human(b)}" + (f", средняя скорость {human(speed)}/с" if speed else ""))
    L.append(f"  Сообщений: {len(records)} (обычных {n_msgs}, служебных {n_serv}, с реакциями {n_react})")
    if first and last:
        L.append(f"  Период: {first:%d.%m.%Y} — {last:%d.%m.%Y}")
    L.append(f"  Файлов на диске: {ok_total} из {want_total}")
    L.append(f"  Размер папки экспорта: {human(folder_size(out_dir))}, свободно на диске: {human(free_space(out_dir))}")
    L.append("")
    L.append("ПО ТИПАМ ФАЙЛОВ")
    for k, label in TYPE_LABELS.items():
        d = by.get(k)
        if not d:
            continue
        line = f"  {label}: {d['ok']} из {d['total']} скачано ({human(d['ok_bytes'])})"
        rest = ", ".join(f"{labels.get(s, s)}: {n}" for s, n in d["other"].items())
        L.append(line + (f" — {rest}" if rest else ""))
    if ce_count:
        L.append(f"  Кастомные эмодзи: {ce_count}")
    if not by:
        L.append("  файлов в чате нет")
    failed = sess.get("failed", {})
    still = {k: v for k, v in failed.items()
             if not any(r["key"] == k and r.get("media") and r["media"]["status"] == "ok" for r in records)}
    if still:
        L.append("")
        L.append(f"НЕ УДАЛОСЬ СКАЧАТЬ ({len(still)})")
        for k, (rel, kind, size, reason) in list(still.items())[:200]:
            L.append(f"  • {rel} — {human(size)} — {reason}")
        if len(still) > 200:
            L.append(f"  … и ещё {len(still) - 200} (полный список в failed.txt)")
    L.append("")
    L.append("РЕЗУЛЬТАТ")
    if pages:
        L.append(f"  HTML: messages.html ({pages} стр.)")
    if cfg.get("make_json"):
        L.append("  JSON: result.json (формат Telegram Desktop)")
    L.append("  Полный лог: export.log")
    if not complete:
        L.append("")
        L.append("Чтобы докачать недостающее, нажмите «Докачать недостающее» или «Продолжить» в «Моих экспортах» — скачанное повторно не загружается.")
    return {"text": "\n".join(L), "complete": complete, "out_dir": out_dir,
            "has_failed": bool(still), "html": bool(pages)}


def is_permanent_error(e):
    """Ошибки Telegram, при которых автоматический повтор бессмысленен (нет доступа и т.п.).
    Сетевые сбои и ошибки сервера Telegram (5xx) — временные."""
    try:
        from telethon.errors import RPCError
    except ImportError:
        return False
    if not isinstance(e, RPCError):
        return False
    code = getattr(e, "code", None)
    return code is not None and 400 <= int(code) < 500 and int(code) != 420


CODE_WHERE = {
    "SentCodeTypeApp": "в приложение Telegram на другом вашем устройстве — "
                       "в чат «Telegram» (с синей галочкой), а не по SMS",
    "SentCodeTypeSms": "по SMS",
    "SentCodeTypeCall": "голосовым звонком",
    "SentCodeTypeFlashCall": "звонком (код — последние цифры номера)",
    "SentCodeTypeMissedCall": "пропущенным звонком (код — последние цифры номера звонившего)",
    "SentCodeTypeEmailCode": "на вашу почту, привязанную к Telegram",
    "SentCodeTypeFragmentSms": "через Fragment (для анонимных номеров)",
    "SentCodeTypeFirebaseSms": "по SMS — но Telegram отправляет такое SMS только официальным приложениям "
                               "(после проверки Google/Apple), поэтому сюда оно, скорее всего, не придёт",
    "SentCodeTypeSmsWord": "по SMS (кодовое слово)",
    "SentCodeTypeSmsPhrase": "по SMS (кодовая фраза)",
    "SentCodeTypeSetUpEmailRequired": "— Telegram требует сначала привязать почту в официальном приложении",
}


def describe_code(sent, log):
    t = type(getattr(sent, "type", None)).__name__
    where = CODE_WHERE.get(t, t)
    nxt = getattr(sent, "next_type", None)
    extra = ""
    if nxt is not None:
        extra = f" Если не придёт — можно запросить повторно: {CODE_WHERE.get(type(nxt).__name__, type(nxt).__name__)}"
        if getattr(sent, "timeout", None):
            extra += f" (через {sent.timeout} с)"
        extra += "."
    log(f"Код отправлен {where}.{extra}")
    return where


def now_ago(t):
    return time.monotonic() - t


def fmt_when(secs):
    """Через сколько-то секунд → «22.11.2026 20:41 (сегодня)»."""
    import datetime as _dt
    fin = _dt.datetime.now() + _dt.timedelta(seconds=max(0, secs))
    today = _dt.date.today()
    day = "сегодня" if fin.date() == today else \
        "завтра" if fin.date() == today + _dt.timedelta(days=1) else ""
    return f"{fin:%d.%m.%Y %H:%M}" + (f" ({day})" if day else "")


def fmt_left(left):
    d, rem = divmod(int(left), 86400)
    h, rem = divmod(rem, 3600)
    m, sec = divmod(rem, 60)
    return (f"~{d} д {h} ч" if d else f"~{h} ч {m} мин" if h else
            f"~{m} мин" if m else f"~{max(sec, 1)} с")


def rec_dump(rec):
    d = dict(rec)
    d["date"] = rec["date"].isoformat()
    return json.dumps(d, ensure_ascii=False)


def rec_load(line):
    import datetime as _dt
    d = json.loads(line)
    d["date"] = _dt.datetime.fromisoformat(d["date"])
    return d


def iter_records(path):
    """Строки records.jsonl (или сжатого снимка records.jsonl.gz) → записи."""
    import gzip
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                try:
                    yield rec_load(line)
                except Exception:
                    pass


VERSIONS_DIR = "versions"


def list_snapshots(out_dir):
    """Сохранённые версии сканирования чата, новые сверху."""
    res = []
    d = os.path.join(out_dir, VERSIONS_DIR)
    try:
        names = os.listdir(d)
    except OSError:
        return res
    for n in names:
        mp = os.path.join(d, n, "meta.json")
        if n.endswith(".tmp") or not os.path.exists(mp):
            continue
        try:
            with open(mp, encoding="utf-8") as f:
                m = json.load(f)
            m["id"] = n
            res.append(m)
        except Exception:
            pass
    res.sort(key=lambda m: m["id"], reverse=True)
    return res


def save_snapshot(out_dir, recs_path, nbytes, meta):
    """Сжатая копия просканированной истории (records.jsonl) + описание — «версия» чата на эту дату."""
    import gzip
    import datetime as _dt
    now = _dt.datetime.now()
    vid = f"{now:%Y-%m-%d_%H-%M-%S}"
    n = 1
    while os.path.exists(os.path.join(out_dir, VERSIONS_DIR, vid)):
        n += 1
        vid = f"{now:%Y-%m-%d_%H-%M-%S}_{n}"
    d = os.path.join(out_dir, VERSIONS_DIR, vid)
    tmp = d + ".tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp, exist_ok=True)
    left = nbytes
    with open(recs_path, "rb") as src, \
            gzip.open(os.path.join(tmp, "records.jsonl.gz"), "wb", compresslevel=5) as dst:
        while left > 0:
            buf = src.read(min(left, 4 * 1024 * 1024))
            if not buf:
                break
            dst.write(buf)
            left -= len(buf)
    meta = dict(meta, created=f"{now:%d.%m.%Y %H:%M}")
    with open(os.path.join(tmp, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False)
    os.replace(tmp, d)
    return vid, meta["created"]


_CHAT_DIRS = {}


def find_chat_dirs(out_root, chat):
    """Папки экспорта чата по его ID из списка чатов (там лежит .chat_id).
    Карта «ID → папки» строится один раз и обновляется, только когда в папке экспорта что-то добавилось."""
    try:
        from telethon.utils import resolve_id
        real = str(resolve_id(int(chat))[0])
    except Exception:
        return []
    try:
        mt = os.path.getmtime(out_root)
    except OSError:
        return []
    hit = _CHAT_DIRS.get(out_root)
    if not hit or hit[0] != mt or real not in hit[1]:   # новый чат мог появиться внутри уже известной папки
        index = {}
        try:
            for n in os.listdir(out_root):
                try:
                    with open(os.path.join(out_root, n, ".chat_id"), encoding="utf-8") as f:
                        index.setdefault(f.read().strip(), []).append(os.path.join(out_root, n))
                except OSError:
                    pass
        except OSError:
            pass
        hit = _CHAT_DIRS[out_root] = (mt, index)
    return list(hit[1].get(real, []))


STATUS_ORDER = {"running": 0, "scan": 1, "partial": 2, "nodl": 3, "nohtml": 4, "old": 5,
                "failed": 6, "empty": 7, "done": 8}


def chat_status(d, types, limit_mb):
    """Состояние экспорта чата по файлам в его папке (без обращения к Telegram)."""
    r = {"dir": d, "name": os.path.basename(d), "raw_id": None, "peer": None, "msgs": 0, "msg_total": 0,
         "want": 0, "done": 0, "want_bytes": 0, "done_bytes": 0, "failed": 0, "versions": 0,
         "html": os.path.exists(os.path.join(d, "messages.html")), "updated": 0.0, "code": "empty", "status": ""}
    for fn, k in ((".chat_id", "raw_id"), (".chat_peer", "peer")):
        try:
            with open(os.path.join(d, fn), encoding="utf-8") as f:
                r[k] = f.read().strip() or None
        except OSError:
            pass
    for fn in ("export.log", "scan_cache.json", "done.txt", "messages.html"):
        try:
            r["updated"] = max(r["updated"], os.path.getmtime(os.path.join(d, fn)))
        except OSError:
            pass
    r["versions"] = len(list_snapshots(d))
    cache = None
    try:
        with open(os.path.join(d, "scan_cache.json"), encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass
    done = set()
    try:
        with open(os.path.join(d, "done.txt"), encoding="utf-8") as f:
            done = {x.strip() for x in f if x.strip()}
    except OSError:
        pass
    if cache is None:
        if r["html"]:
            r["code"], r["status"] = "done", "✅ Готово"
        else:
            r["code"], r["status"] = "empty", "Нет данных сканирования"
        return r
    try:
        v = json.loads(cache.get("sig", "{}")).get("v")
    except Exception:
        v = None
    r["msgs"] = cache.get("total_msgs", 0)
    r["msg_total"] = cache.get("msg_total", 0) or 0
    limit = int(limit_mb) * 1024 * 1024 if int(limit_mb or 0) > 0 else float("inf")
    media = cache.get("media") or cache.get("wanted") or {}
    wanted = {k: x for k, x in media.items() if types.get(x[0]) and x[1] <= limit}
    r["want"] = len(wanted)
    r["want_bytes"] = sum(x[1] for x in wanted.values())
    got = [k for k in wanted if k in done]
    r["done"] = len(got)
    r["done_bytes"] = sum(wanted[k][1] for k in got)
    try:
        with open(os.path.join(d, "failed.txt"), encoding="utf-8") as f:
            r["failed"] = len({ln.split("\t", 1)[0] for ln in f if ln.strip()} - done)
    except OSError:
        pass
    if v not in (4, 5):
        r["code"], r["status"] = "old", "Старая версия программы — нужно сканирование"
    elif not cache.get("scan_done"):
        tot = f" из {r['msg_total']:,}".replace(",", " ") if r["msg_total"] else ""
        r["code"], r["status"] = "scan", f"⏸ Сканирование не завершено ({r['msgs']:,}".replace(",", " ") + tot + ")"
    elif r["done"] < r["want"]:
        if r["done"] == 0:
            r["code"], r["status"] = "nodl", "Просканирован, файлы не скачаны"
        else:
            r["code"], r["status"] = "partial", f"⏳ Скачано не всё ({r['want'] - r['done']} осталось)"
    elif not r["html"]:
        r["code"], r["status"] = "nohtml", "Файлы скачаны, HTML не собран"
    else:
        r["code"], r["status"] = "done", "✅ Готово"
    return r


def friendly_error(err):
    for k, t in (("ChannelPrivate", "нет доступа (закрытый чат или вас удалили)"),
                 ("ChatAdminRequired", "нужны права администратора"),
                 ("ChannelInvalid", "чат не найден"), ("PeerIdInvalid", "чат не найден"),
                 ("UserBannedInChannel", "вы заблокированы в этом чате")):
        if k in err:
            return t
    return err[:80]


_STATUS_CACHE = {}


def _status_key(d, types, limit_mb):
    """Отпечаток папки по времени изменения нескольких файлов: если ничего не менялось — пересчитывать не нужно."""
    sig = [json.dumps(types, sort_keys=True), limit_mb]
    for fn in ("scan_cache.json", "done.txt", "failed.txt", "messages.html", "export.log", VERSIONS_DIR):
        try:
            sig.append(os.path.getmtime(os.path.join(d, fn)))
        except OSError:
            sig.append(None)
    return tuple(sig)


def chat_status_cached(d, types, limit_mb):
    key = _status_key(d, types, limit_mb)
    hit = _STATUS_CACHE.get(d)
    if hit and hit[0] == key:
        return dict(hit[1])
    row = chat_status(d, types, limit_mb)
    _STATUS_CACHE[d] = (key, dict(row))
    return row


def scan_exports(out_root, types, limit_mb):
    """Все чаты в папке экспорта + чаты из «экспорта всех», которые завершились ошибкой.
    Неизменившиеся папки берутся из кэша — большой scan_cache.json не перечитывается каждые 20 секунд."""
    rows, seen = [], set()
    try:
        names = os.listdir(out_root)
    except OSError:
        names = []
    for n in names:
        d = os.path.join(out_root, n)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, ".chat_id")):
            try:
                row = chat_status_cached(d, types, limit_mb)
                rows.append(row)
                seen.add(os.path.normcase(os.path.abspath(d)))
            except Exception:
                pass
    try:
        with open(os.path.join(out_root, "_all_chats_state.json"), encoding="utf-8") as f:
            st = json.load(f).get("chats", {})
        for cid, e in st.items():
            od = e.get("out_dir")
            if e.get("status") == "failed" and not (od and os.path.normcase(os.path.abspath(od)) in seen):
                rows.append({"dir": None, "name": e.get("name", cid), "raw_id": None, "peer": cid,
                             "msgs": 0, "msg_total": 0, "want": 0, "done": 0, "want_bytes": 0, "done_bytes": 0,
                             "failed": 0, "versions": 0, "html": False, "updated": 0.0, "code": "failed",
                             "status": "❌ " + friendly_error(e.get("error") or "ошибка")})
    except Exception:
        pass
    return rows


def fallback_record(msg, key, prefix, err):
    """Если сообщение не удалось разобрать — сохраняем хотя бы дату и текст, а не останавливаем экспорт."""
    j = {"id": msg.id, "type": "message"}
    j["date"], j["date_unixtime"] = tg_date(msg.date)
    j["from"] = None
    t = getattr(msg, "message", "") or ""
    j["text"], j["text_entities"] = t, ([{"type": "plain", "text": t}] if t else [])
    return {"key": key, "j": j, "date": msg.date.astimezone(), "custom": [],
            "extra_html": f"<div class='media_other details'>⚠ сообщение сохранено не полностью: {esc(str(err))}</div>",
            "reply_key": None, "has_photo": False}


class FloodWaitLog(logging.Handler):
    """Telethon сам пережидает короткие паузы, которые требует Telegram, но молча.
    Перехватываем его сообщение и показываем паузу в окне — чтобы было видно, что программа не зависла."""

    def __init__(self, worker):
        super().__init__(logging.INFO)
        self.worker = worker
        self.last = -1e9

    def emit(self, record):
        try:
            text = record.getMessage()
            if re.search(r"connecting failed|Connection closed|closed the connection|Can't connect|"
                         r"Connection reset|timed out|Error while connecting", text, re.I):
                self.worker.net_issue(text)
                return
            if re.search(r"Connection to .* complete", text):
                self.worker.net_ok()
                return
            m = re.search(r"Sleeping for (\d+)s", text)
            if m:
                secs = int(m.group(1))
                self.worker.note_pause(secs)
                self.worker.pause_phase(secs)
                if time.monotonic() - self.last > 60:
                    self.worker.log(f"⏸ Telegram попросил паузу {secs} с (ограничение частоты запросов). "
                                    "Программа подождёт и продолжит сама.")
                    self.last = time.monotonic()
        except Exception:
            pass


class Worker:
    """Фоновый поток с asyncio-циклом, в котором живёт клиент Telegram."""

    def __init__(self, ui_queue):
        self.q = ui_queue
        self.loop = asyncio.new_event_loop()
        self.client = None
        self.progress = Progress()
        tl = logging.getLogger("telethon")
        tl.setLevel(logging.INFO)
        tl.propagate = False
        tl.addHandler(FloodWaitLog(self))
        self.stats = {"ok": 0, "failed": 0, "total_files": 0, "running": False,
                      "stage": "", "msg_n": 0, "msg_total": 0,
                      "chat_i": 0, "chat_total": 0, "chat_name": "", "disk_low": 0}
        self.logf = None
        self.alogf = None
        self.scan_info = None
        threading.Thread(target=self._run, daemon=True).start()

    # ---- связь ----
    def net_issue(self, detail=""):
        """Telethon не может связаться с Telegram — говорим об этом понятно, а не молча ждём."""
        now = time.monotonic()
        self.net_issue_at = now
        self.phase("📡 Нет связи с Telegram — проверьте, что включён VPN или запущен прокси, и что на странице «Подключение» выбран нужный вид прокси. Программа продолжит сама, когда связь появится.")
        if now - getattr(self, "_net_last_log", -1e9) > 60:
            self._net_last_log = now
            self.log("📡 Не удаётся связаться с Telegram (" + str(detail)[:120] + "). Похоже, нет интернета, "
                     "выключен VPN или не запущен прокси. Программа продолжит сама, когда связь появится.")

    def net_ok(self):
        """Связь восстановилась — убираем предупреждение сразу, а не через минуту."""
        if now_ago(getattr(self, "net_issue_at", -1e9)) < 600:
            self.log("📡 Связь с Telegram восстановлена.")
        self.net_issue_at = -1e9
        self.restore_phase()

    def recheck_now(self):
        """Кнопка «Проверить снова»: не ждать паузу между попытками, а проверить связь сразу."""
        def fire():
            ev = getattr(self, "_recheck_evt", None)
            if ev is not None:
                ev.set()
        self.loop.call_soon_threadsafe(fire)

    # ---- паузы Telegram и прогноз окончания сканирования ----
    def scan_begin(self, n0):
        self.scan_info = {"t0": time.monotonic(), "n0": n0, "paused": 0.0, "pause_until": 0.0,
                          "pauses": collections.deque(maxlen=12)}

    def note_pause(self, secs):
        si = self.scan_info
        if si is None or self.stats.get("stage") != "scan":
            return
        si["pauses"].append((self.stats.get("msg_n", 0), secs))
        si["paused"] += secs
        si["pause_until"] = time.monotonic() + secs

    def scan_eta(self):
        """Сколько секунд осталось сканировать — с учётом пауз, которые делает Telegram.
        Скорость считается только по времени без пауз, а паузы добавляются отдельно:
        «сколько ещё будет пауз» × «средняя длина последних пауз». Поэтому во время паузы
        прогноз не пропадает и не скачет."""
        si, st = self.scan_info, self.stats
        if si is None or st.get("stage") != "scan":
            return None
        n, total = st.get("msg_n", 0), st.get("msg_total", 0)
        done = n - si["n0"]
        if not total or total <= n or done < 300:
            return None
        now = time.monotonic()
        in_pause = max(0.0, si["pause_until"] - now)
        active = (now - si["t0"]) - (si["paused"] - in_pause)
        if active <= 1:
            return None
        rate = done / active
        P = list(si["pauses"])
        if len(P) >= 2:
            per = (P[-1][0] - P[0][0]) / (len(P) - 1)       # сообщений между паузами
        else:
            per = 3000                                       # так обычно делает Telegram
        last = P[-6:]
        avg_pause = sum(s for _, s in last) / len(last) if last else 15
        per = max(per, 300)
        rem = total - n
        return rem / rate + rem / per * avg_pause + in_pause

    async def _is_online(self):
        try:
            if not self.client.is_connected():
                await asyncio.wait_for(self.client.connect(), 60)
            await asyncio.wait_for(self.client.get_me(), 60)
            return True
        except (asyncio.CancelledError, StopRequested):
            raise
        except Exception:
            return False

    async def _wait_online(self):
        """Ждёт, пока вернётся связь с Telegram (интернет, VPN или прокси), сколько бы это ни заняло."""
        delay = 15
        self._recheck_evt = asyncio.Event()
        while not await self._is_online():
            self.net_issue_at = time.monotonic()
            self.log(f"📡 Нет связи с Telegram. Проверю снова через {delay} с — "
                     "продолжу сам, как только связь появится.")
            self.phase(f"📡 Нет связи с Telegram — следующая проверка через {delay} с (или нажмите «Проверить снова»)")
            try:
                await asyncio.wait_for(self._recheck_evt.wait(), delay)
                delay = 15                      # проверили по кнопке — дальше снова с короткой паузы
            except asyncio.TimeoutError:
                delay = min(delay * 2, 300)
            self._recheck_evt.clear()
        self.net_ok()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def submit(self, coro):
        return asyncio.run_coroutine_threadsafe(self._guard(coro), self.loop)

    async def _guard(self, coro):
        try:
            return await coro
        except (asyncio.CancelledError, StopRequested):
            self.log("⏹ Остановлено. При следующем запуске загрузка продолжится с места остановки.")
        except Exception as e:
            msg = str(e)
            if "database is locked" in msg:
                msg = "Файл входа (tg_export_session.session) занят: похоже, программа запущена дважды. Закройте лишние окна программы (или перезагрузите компьютер) и запустите её снова."
            self.log(f"❌ Ошибка: {msg}")
            self.q.put(("error", msg))
        finally:
            self.stats["running"] = False
            if self.logf:
                try:
                    self.logf.close()
                except Exception:
                    pass
                self.logf = None
            self.q.put(("idle",))

    # ---- общение с окном ----
    def log(self, text):
        self.q.put(("log", text))
        lf = self.logf
        if lf:
            try:
                lf.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text.replace("\n", "\n    ") + "\n")
            except Exception:
                pass

    PAUSE_PREFIXES = ("⏸ Пауза по требованию", "📡 Нет связи")

    def phase(self, text):
        self._phase_now = text
        if not text.startswith(self.PAUSE_PREFIXES):
            self._phase_base = text            # к этой надписи вернёмся после паузы / обрыва связи
        self.q.put(("phase", text))

    def restore_phase(self, only_if=None):
        """Пауза закончилась или связь вернулась — убираем устаревшую надпись о ней."""
        now_t = getattr(self, "_phase_now", None)
        if now_t is None or not now_t.startswith(self.PAUSE_PREFIXES):
            return
        if only_if is not None and now_t != only_if:
            return
        base = getattr(self, "_phase_base", None)
        if base:
            self.phase(base)

    def pause_phase(self, secs):
        text = f"⏸ Пауза по требованию Telegram: {secs} с — ограничение частоты запросов, это нормально"
        self.phase(text)
        try:
            self.loop.call_soon_threadsafe(self.loop.call_later, secs + 0.5,
                                           lambda: self.restore_phase(only_if=text))
        except RuntimeError:
            pass

    async def ask(self, kind, title, prompt, **kw):
        fut = self.loop.create_future()
        self.q.put(("ask", kind, title, prompt, kw, fut))
        return await fut

    # ---- вход ----
    async def connect(self, cfg):
        self.conn_target, self.conn_via = proxy_target(cfg), proxy_label(cfg)
        self.phase(f"Подключение ({self.conn_via})...")
        if self.client:
            try:      # старое соединение могло зависнуть на неработающем прокси — не ждём его бесконечно
                await asyncio.wait_for(self.client.disconnect(), 10)
            except Exception:
                pass
            self.client = None
        client = make_client(cfg, self.log)
        try:
            await client.connect()
            while not await client.is_user_authorized():
                if await self.login_qr(client):
                    break
                try:
                    await self.login_phone(client, cfg)
                    break
                except SwitchToQR:
                    self.log("Перехожу ко входу по QR-коду.")
        except BaseException:
            # подключение отменили или оно не удалось — закрываем его, иначе файл входа останется занят
            try:
                await asyncio.wait_for(asyncio.shield(client.disconnect()), 10)
            except BaseException:
                pass
            try:
                client.session.close()
            except Exception:
                pass
            raise
        self.client = client
        me = await client.get_me()
        self.log(f"✅ Вход выполнен: {ent_name(me)}")
        if getattr(me, "premium", False):
            self.log("Аккаунт с Telegram Premium — скорость загрузки не ограничивается Telegram.")
        else:
            self.log("ℹ Аккаунт без Telegram Premium: Telegram ограничивает скорость загрузки файлов "
                     "для таких аккаунтов. Многопоточность частично помогает, но потолок задаёт сервер.")
        try:
            import cryptg  # noqa: F401
            self.log("Ускоритель шифрования cryptg: включён.")
        except ImportError:
            self.log("⚠ Ускоритель cryptg НЕ установлен — шифрование идёт медленно и может ограничивать "
                     "скорость. Установите: pip install cryptg (или перезапустите через «Запуск.bat»).")
        self.q.put(("logged_in", ent_name(me)))
        self.phase("Загружаю список чатов...")
        dialogs = []
        async for d in client.iter_dialogs():
            kind = "Канал" if (d.is_channel and not d.is_group) else ("Группа" if d.is_group else "Личный")
            dialogs.append((d.id, d.name or "(без названия)", kind))
        self.q.put(("dialogs", dialogs))
        self.log(f"Найдено чатов: {len(dialogs)}")
        self.phase("Готово к экспорту")

    async def ask_password(self, client):
        while True:
            pw = await self.ask("password", "Облачный пароль",
                                "Введите облачный пароль (двухэтапная аутентификация):")
            if not pw:
                raise StopRequested()
            try:
                await client.sign_in(password=pw)
                return
            except Exception as e:
                self.log(f"Пароль не подошёл: {e}")

    async def login_qr(self, client):
        """Вход по QR-коду. Возвращает False, если пользователь выбрал вход по номеру."""
        try:
            import qrcode  # noqa: F401
        except ImportError:
            self.log("Пакет qrcode не установлен (pip install qrcode) — вход по QR недоступен, "
                     "используется вход по номеру.")
            return False
        self.phase("Вход по QR-коду...")
        qr = await client.qr_login()
        self.log("Отсканируйте QR-код в приложении Telegram на телефоне: "
                 "Настройки → Устройства → Подключить устройство.")
        while True:
            choice = self.loop.create_future()
            self.q.put(("qr_show", qr.url, choice))
            wait_task = asyncio.ensure_future(qr.wait())
            done, _ = await asyncio.wait({wait_task, choice}, return_when=asyncio.FIRST_COMPLETED)
            if choice in done:
                wait_task.cancel()
                self.q.put(("qr_close",))
                if choice.result() == "phone":
                    return False
                raise StopRequested()
            try:
                wait_task.result()
                self.q.put(("qr_close",))
                return True
            except asyncio.TimeoutError:
                await qr.recreate()          # код истёк — показываем новый
            except SessionPasswordNeededError:
                self.q.put(("qr_close",))
                await self.ask_password(client)
                return True
            except Exception:
                self.q.put(("qr_close",))
                raise

    async def login_phone(self, client, cfg):
        phone = await self.ask("text", "Вход в Telegram", "Номер телефона (в формате +7...):",
                               initial=cfg.get("phone", ""))
        if not phone:
            raise StopRequested()
        phone = re.sub(r"[^\d+]", "", phone)
        if not phone.startswith("+"):
            phone = "+" + phone
        self.q.put(("save_phone", phone))
        self.log(f"Запрашиваю код для {phone}...")
        sent = await client.send_code_request(phone)
        where = describe_code(sent, self.log)
        while True:
            code = await self.ask(
                "text", "Код подтверждения",
                f"Код отправлен: {where}\n\n"
                "Обычно Telegram присылает код не по SMS, а в чат «Telegram» (с синей галочкой) в приложении "
                "на телефоне или компьютере, где вы уже вошли.\n\n"
                "Введите код. Оставьте поле пустым и нажмите OK — код будет запрошен повторно "
                "(другим способом, если Telegram позволит). «Отмена» — войти по QR-коду.")
            if code is None:
                if await self.ask("yesno", "Код не пришёл?",
                                  "Войти по QR-коду? Это надёжнее: код по номеру Telegram часто не отправляет "
                                  "сторонним программам.\n\n«Нет» — отменить вход."):
                    raise SwitchToQR()
                raise StopRequested()
            code = re.sub(r"\D", "", code)
            if not code:
                try:
                    sent = await client.send_code_request(phone)
                    where = describe_code(sent, self.log)
                except Exception as e:
                    self.log(f"Повторно запросить код не удалось: {e}")
                continue
            try:
                await client.sign_in(phone, code)
                return
            except PhoneCodeInvalidError:
                self.log("Неверный код, попробуйте ещё раз.")
            except SessionPasswordNeededError:
                await self.ask_password(client)
                return

    async def count_sources(self, entity):
        """Сколько сообщений в чате (одним запросом, без перебора истории) + старая история группы."""
        client = self.client
        total = 0
        r = await client.get_messages(entity, limit=0)
        total += getattr(r, "total", 0) or 0
        if getattr(entity, "megagroup", False):
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                full = await client(GetFullChannelRequest(entity))
                old_id = getattr(full.full_chat, "migrated_from_chat_id", None)
                if old_id:
                    try:
                        old = await client.get_entity(old_id * -1)
                    except Exception:
                        from telethon.tl.types import InputPeerChat
                        old = InputPeerChat(old_id)
                    r2 = await client.get_messages(old, limit=0)
                    total += getattr(r2, "total", 0) or 0
            except Exception:
                pass
        return total

    def request_count(self, chat):
        """Посчитать сообщения выбранного чата в фоне (не мешает другим операциям)."""
        async def job():
            try:
                target = int(chat) if str(chat).lstrip("-").isdigit() else chat
                ent = await self.client.get_entity(target)
                n = await self.count_sources(ent)
                self.q.put(("chat_count", str(chat), n))
            except Exception as e:
                self.q.put(("chat_count", str(chat), None))
        if self.client:
            asyncio.run_coroutine_threadsafe(job(), self.loop)

    async def count_many(self, ids):
        """Сообщений в каждом из чатов — по одному запросу на чат, с прогрессом и возможностью остановить."""
        self.count_stop = False
        n = len(ids)
        for i, chat in enumerate(ids, 1):
            if self.count_stop or not self.client:
                break
            try:
                target = int(chat) if str(chat).lstrip("-").isdigit() else chat
                r = await self.client.get_messages(target, limit=0)
                self.q.put(("chat_count", str(chat), getattr(r, "total", 0) or 0))
            except Exception:
                self.q.put(("chat_count", str(chat), None))
            self.q.put(("count_progress", i, n))
        self.q.put(("count_progress", n, n))

    async def _test_media(self, chats):
        """Файл для замера скорости: самое большое видео/документ из первых подходящих чатов."""
        from telethon.tl.types import InputMessagesFilterVideo, InputMessagesFilterDocument
        best = None
        for chat in chats[:6]:
            for flt in (InputMessagesFilterVideo, InputMessagesFilterDocument):
                try:
                    target = int(chat) if str(chat).lstrip("-").isdigit() else chat
                    async for m in self.client.iter_messages(target, limit=15, filter=flt()):
                        obj = media_object(m)
                        sz = media_size(m, obj) if obj is not None else 0
                        if sz and (best is None or sz > best[1]):
                            best = (obj, sz)
                except Exception:
                    pass
            if best and best[1] >= 8 * 1024 * 1024:
                break
        return best

    async def diagnose(self, variants, chats, reuse_media=False):
        """Проверка вариантов подключения: время подключения, пинг (5 замеров) и скорость скачивания
        настоящего файла (до 8 с). Работает отдельным временным соединением — текущий экспорт не прерывается."""
        import random
        import statistics
        from telethon.sessions import StringSession
        from telethon.tl.functions import PingRequest
        auth = ""
        if self.client:
            try:
                auth = StringSession.save(self.client.session)
            except Exception:
                auth = ""
        media = getattr(self, "_diag_media", None) if reuse_media else None
        if media is None and self.client and auth:
            self.q.put(("diag_note", "Ищу файл для замера скорости…"))
            try:
                media = await asyncio.wait_for(self._test_media(chats), 60)
            except asyncio.TimeoutError:
                media = None
            self._diag_media = media      # на шаге «прокси» VPN уже выключен — файл ищем заранее
        self.q.put(("diag_note", "" if media else
                    "Подходящего файла для замера скорости не нашлось — проверяю только подключение и пинг."))
        for label, cfg in variants:
            res = {"label": label, "ok": False, "connect": None, "ping": None, "speed": None, "error": ""}
            self.q.put(("diag", label, "running", dict(res)))
            client = None
            try:
                client = make_client(cfg, lambda *_: None, session=StringSession(auth), quick=True)
                t0 = time.monotonic()
                await asyncio.wait_for(client.connect(), 25)
                res["connect"] = time.monotonic() - t0
                pings = []
                for _ in range(5):
                    t = time.monotonic()
                    await asyncio.wait_for(client(PingRequest(ping_id=random.getrandbits(63))), 15)
                    pings.append((time.monotonic() - t) * 1000)
                res["ping"] = statistics.median(pings)
                if media is not None:
                    got, t = 0, time.monotonic()

                    async def pull():
                        nonlocal got
                        async for chunk in client.iter_download(media[0], request_size=512 * 1024,
                                                                file_size=media[1]):
                            got += len(chunk)
                            if time.monotonic() - t > 8 or got >= 24 * 1024 * 1024:
                                break
                    try:
                        await asyncio.wait_for(pull(), 20)
                    except asyncio.TimeoutError:
                        pass
                    dt = max(0.001, time.monotonic() - t)
                    res["speed"] = got / dt if got else 0.0
                res["ok"] = True
            except (asyncio.CancelledError, StopRequested):
                raise
            except Exception as e:
                res["error"] = (str(e) or type(e).__name__)[:120]
            finally:
                if client is not None:
                    try:
                        await asyncio.wait_for(client.disconnect(), 5)
                    except Exception:
                        pass
            self.q.put(("diag", label, "done", res))
        self.q.put(("diag_done",))

    async def logout(self):
        if self.client:
            try:
                await self.client.log_out()
            except Exception:
                pass
            self.client = None
        for ext in ("", ".session"):
            try:
                os.remove(SESSION_PATH + ext)
            except OSError:
                pass
        self.log("Вы вышли из аккаунта.")
        self.q.put(("logged_out",))

    # ---- экспорт ----
    async def export_auto(self, cfg, chat, version=None):
        """Экспорт с автоматическим перезапуском после обрыва связи и других сбоев.
        Каждый перезапуск продолжает с места остановки (готовые файлы не качаются,
        недокачанные докачиваются с места обрыва)."""
        self.stats.update(chat_i=0, chat_total=0, chat_name="")
        self.session = {"started": time.time(), "restarts": 0, "bytes": 0, "dl_seconds": 0.0,
                        "failed": {}, "chat": chat}
        from contextlib import AsyncExitStack
        stack = AsyncExitStack()
        self.hist = None
        try:
            await self._open_takeout(stack, cfg)
            await self._export_loop(cfg, chat, version=version)
        finally:
            self.hist = None
            try:
                await stack.aclose()
            except Exception:
                pass

    async def _open_takeout(self, stack, cfg):
        """Режим экспорта Telegram (takeout) — им пользуется Telegram Desktop.
        В нём у запросов истории мягче ограничения частоты, поэтому меньше пауз."""
        if not cfg.get("use_takeout", True):
            return
        self.phase("Открываю режим экспорта Telegram...")
        await self._close_stale_takeout()
        try:
            tk_ = self.client.takeout(finalize=True, users=True, chats=True, megagroups=True,
                                      channels=True, files=True,
                                      max_file_size=(int(cfg["size_limit_mb"]) or 4096) * 1024 * 1024)
            self.hist = await stack.enter_async_context(tk_)
            try:   # проверяем настоящим запросом: режим действительно работает
                await asyncio.wait_for(self.hist.get_messages("me", limit=1), 30)
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                if "takeout" in (cname(e) + str(e)).lower():
                    raise
            self.q.put(("takeout", {"status": "on", "checked": time.time()}))
            self.log("⚡ Включён режим экспорта Telegram: меньше пауз при чтении истории. "
                     "На других ваших устройствах Telegram может показать уведомление о запросе экспорта — это он.")
        except Exception as e:
            self.hist = None
            secs = getattr(e, "seconds", None)
            if cname(e) == "TakeoutInitDelayError" and secs:
                import datetime as _dt
                when = _dt.datetime.now() + _dt.timedelta(seconds=secs)
                self.q.put(("takeout", {"status": "delayed", "until": time.time() + secs, "checked": time.time()}))
                self.log(f"ℹ Режим экспорта Telegram пока недоступен: в целях безопасности Telegram разрешит его "
                         f"с {when:%d.%m.%Y %H:%M} (обычно через сутки после входа на новом устройстве). "
                         "Чтобы не ждать: в Telegram на телефоне нажмите Allow (Разрешить) в сообщении "
                         "«Data export request». "
                         "Экспорт идёт в обычном режиме — Telegram будет делать короткие паузы каждые ~3000 сообщений.")
            else:
                self.q.put(("takeout", {"status": "failed", "error": str(e)[:200], "checked": time.time()}))
                self.log(f"ℹ Режим экспорта Telegram не включился ({e}). Экспорт идёт в обычном режиме.")

    async def _close_stale_takeout(self):
        """Если прошлый сеанс режима экспорта не был закрыт (программу закрыли посреди экспорта
        или в момент закрытия пропала связь), его номер остаётся в файле входа, и Telegram/Telethon
        отказываются открывать новый. Закрываем старый; если не вышло — просто забываем его."""
        sess = getattr(self.client, "session", None)
        if sess is None or getattr(sess, "takeout_id", None) is None:
            return
        try:
            await asyncio.wait_for(self.client.end_takeout(success=False), 30)
        except Exception:
            pass
        if getattr(sess, "takeout_id", None) is not None:
            try:
                sess.takeout_id = None
                sess.save()
            except Exception:
                pass
        self.log("ℹ Закрыт незавершённый прошлый сеанс режима экспорта Telegram.")

    async def check_takeout(self, cfg):
        """«Проверить сейчас»: открыть режим экспорта и сразу закрыть — узнать, разрешил ли его Telegram."""
        from contextlib import AsyncExitStack
        stack = AsyncExitStack()
        try:
            await self._open_takeout(stack, dict(cfg, use_takeout=True))
        finally:
            self.hist = None
            try:
                await stack.aclose()
            except Exception:
                pass
            self.q.put(("takeout_checked",))

    async def _export_loop(self, cfg, chat, report_window=True, max_attempts=100, confirm=True, version=None,
                           **kw):
        """Повторяет экспорт чата после сбоев; каждый повтор продолжает с места остановки.
        Пока нет связи с Telegram — просто ждёт её, попытки при этом не тратятся."""
        attempt = 0
        first = True
        while True:
            try:
                return await self.export(cfg, chat, confirm=(confirm and first), report_window=report_window,
                                         version=version, **kw)
            except (StopRequested, ValueError, asyncio.CancelledError):
                raise
            except FloodWaitError as e:
                first = False
                self.note_pause(e.seconds)
                self.log(f"⏸ Telegram просит подождать {e.seconds} с ({fmt_left(e.seconds)}) — "
                         f"подожду и продолжу с того же места. Продолжу примерно {fmt_when(e.seconds)}.")
                self.phase(f"⏸ Пауза по требованию Telegram: {e.seconds} с")
                await asyncio.sleep(e.seconds + 1)
                continue
            except Exception as e:
                first = False
                if is_permanent_error(e):
                    raise   # нет доступа к чату и т.п. — повтор не поможет
                self.stats["running"] = True
                if not await self._is_online():
                    self.log(f"⚠ Сбой: {e!r} — похоже, пропала связь с Telegram.")
                    await self._wait_online()
                    self.log("📡 Связь восстановлена — продолжаю с места остановки.")
                    continue
                attempt += 1
                self.session["restarts"] = self.session.get("restarts", 0) + 1
                if attempt > max_attempts:
                    raise
                delay = min(15 * attempt, 300)
                self.log(f"⚠ Сбой: {e!r}\nАвтоматически продолжу через {delay} с "
                         f"(попытка {attempt} из {max_attempts}). Уже скачанное не потеряется.")
                self.phase(f"Сбой — повтор через {delay} с...")
                await asyncio.sleep(delay)

    # ---- экспорт всех чатов ----
    def alog(self, text):
        """Запись в общий журнал «всех чатов» (и в окно)."""
        self.log(text)
        f = self.alogf
        if f:
            try:
                f.write(time.strftime("[%Y-%m-%d %H:%M:%S] ") + text.replace("\n", "\n    ") + "\n")
            except Exception:
                pass

    async def export_all(self, cfg, chats):
        """Экспорт нескольких чатов. Состояние хранится в _all_chats_state.json: после сбоя, «Стоп» или
        перезапуска продолжает с того чата, где остановился (а внутри чата — с того сообщения/файла).
        Ошибка в одном чате не останавливает остальные.
        cfg["multi_mode"]: "sequential" — каждый чат сканируется и сразу скачивается;
                           "scan_first" — сначала сканируются все, потом (после общей сводки) скачиваются.
        cfg["auto_download"]: False — перед скачиванием показать сводку и спросить."""
        from contextlib import AsyncExitStack
        import datetime as _dt
        root = cfg["out_root"]
        os.makedirs(root, exist_ok=True)
        state_path = os.path.join(root, "_all_chats_state.json")
        state = {"chats": {}}
        try:
            with open(state_path, encoding="utf-8") as f:
                state = json.load(f)
            state.setdefault("chats", {})
        except Exception:
            pass

        def save_state():
            try:
                with open(state_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(state, f, ensure_ascii=False, indent=1)
                os.replace(state_path + ".tmp", state_path)
            except Exception as e:
                self.log(f"Не удалось сохранить состояние: {e}")

        self.alogf = open(os.path.join(root, "_all_chats.log"), "a", encoding="utf-8", buffering=1)
        started = time.time()
        skip = cfg.get("all_skip_done", True)
        mode = cfg.get("multi_mode", "sequential")
        auto = cfg.get("auto_download", True)
        todo = [c for c in chats
                if not (skip and state["chats"].get(str(c[0]), {}).get("status") == "done")]
        skipped = len(chats) - len(todo)
        self.alog(f"━━━ Экспорт чатов: выбрано {len(chats)}"
                  + (f", уже готовы и пропускаются: {skipped}" if skipped else "")
                  + f", к экспорту: {len(todo)}; порядок: "
                  + ("сначала сканировать все, потом скачивать" if mode == "scan_first" else "по очереди")
                  + ("" if auto else "; перед скачиванием — сводка") + " ━━━")
        stack = AsyncExitStack()
        self.hist = None
        stopped = False

        async def run_list(lst, label, **kw):
            """Проходит по чатам (с одним повтором тех, где был временный сбой). Возвращает {cid: результат}."""
            results = {}
            rounds = [lst]
            for rnd in (1, 2):
                retry = []
                cur = rounds[-1]
                for i, (cid, name, kind) in enumerate(cur, 1):
                    self.stats.update(chat_i=i, chat_total=len(cur), chat_name=name)
                    self.alog(f"━━ {label} {i} из {len(cur)}" + (" (повтор)" if rnd == 2 else "") +
                              f": {name} [{kind}]")
                    self.session = {"started": time.time(), "restarts": 0, "bytes": 0, "dl_seconds": 0.0,
                                    "failed": {}, "chat": cid}
                    ent = state["chats"].setdefault(str(cid), {})
                    ent.update(name=name, kind=kind, status="in_progress",
                               at=f"{_dt.datetime.now():%d.%m.%Y %H:%M}")
                    save_state()
                    try:
                        res = await self._export_loop(cfg, str(cid), report_window=False, max_attempts=5, **kw)
                        results[cid] = res
                        if res.get("scan_only"):
                            ent.update(status="scanned", out_dir=res["out_dir"], msgs=res["msgs"],
                                       files_ok=res["ok"], files_want=res["want"], error="")
                            self.alog(f"🔎 {name}: сообщений {res['msgs']}, к скачиванию файлов {res['files']} "
                                      f"({human(res['need'])})")
                        else:
                            ent.update(status="done" if res["complete"] else "partial",
                                       out_dir=res["out_dir"], msgs=res["msgs"],
                                       files_ok=res["ok"], files_want=res["want"], error="")
                            self.alog(("✅" if res["complete"] else "⚠") +
                                      f" {name}: сообщений {res['msgs']}, файлов {res['ok']} из {res['want']}")
                    except (StopRequested, asyncio.CancelledError):
                        ent["status"] = "stopped"
                        save_state()
                        raise
                    except Exception as e:
                        perm = is_permanent_error(e) or isinstance(e, ValueError)
                        ent.update(status="failed", error=f"{type(e).__name__}: {e}")
                        self.alog(f"❌ {name}: не удалось ({e}). Перехожу к следующему чату"
                                  + ("." if perm else " — в конце попробую этот чат ещё раз."))
                        if not perm:
                            retry.append((cid, name, kind))
                    save_state()
                if not retry or rnd == 2:
                    break
                self.alog(f"Повторяю чаты, где были сбои: {len(retry)}")
                rounds.append(retry)
            return results

        try:
            await self._open_takeout(stack, cfg)
            if mode == "scan_first":
                scanned = await run_list(todo, "Сканирование чата", confirm=False, scan_only=True)
                ready = [c for c in todo if c[0] in scanned]
                files = sum(r["files"] for r in scanned.values())
                need = sum(r["need"] for r in scanned.values())
                lines = [f"Все чаты просканированы: {len(scanned)} из {len(todo)}.", "",
                         f"К скачиванию: {files} файлов, {human(need)}."]
                big = sorted(scanned.items(), key=lambda kv: -kv[1]["need"])
                names = {c[0]: c[1] for c in todo}
                for cid, r in big[:15]:
                    if r["files"]:
                        lines.append(f"  {names.get(cid, cid)}: {r['files']} файлов, {human(r['need'])}")
                if sum(1 for _, r in big if r["files"]) > 15:
                    lines.append("  …")
                free = free_space(root)
                reserve = int(cfg["disk_reserve_mb"]) * 1024 * 1024
                lines.append("")
                lines.append(f"Свободно на диске: {human(free)}.")
                if need + reserve > free:
                    lines.append(f"⚠ Места не хватит: нужно ещё ~{human(need + reserve - free)}. Скачивание начнётся; "
                                 "когда место кончится, оно встанет на паузу до тех пор, пока вы его не освободите.")
                summary = "\n".join(lines)
                self.alog(summary)
                go = True
                if files and not auto:
                    go = await self.ask("yesno", "Скачать файлы всех чатов?", summary + "\n\nНачать скачивание?")
                if go:
                    self.alog("▶ Скачиваю файлы чатов по очереди.")
                    await run_list(ready, "Скачивание чата", confirm=False)
                else:
                    self.alog("⏹ Скачивание файлов отложено. Чаты просканированы — продолжить можно в «Моих экспортах».")
            else:
                await run_list(todo, "Чат", confirm=not auto, decline="skip_files")
        except (StopRequested, asyncio.CancelledError):
            stopped = True
            raise
        finally:
            self.hist = None
            try:
                await stack.aclose()
            except Exception:
                pass
            if not stopped:
                rep = self._all_report(root, state, chats, started)
                self.q.put(("all_finished", root, rep))
            f, self.alogf = self.alogf, None
            try:
                f.close()
            except Exception:
                pass

    def _all_report(self, root, state, chats, started):
        import datetime as _dt
        sel = [state["chats"].get(str(c[0]), {"name": c[1], "status": "not_started"}) for c in chats]
        cnt = collections.Counter(e.get("status") for e in sel)
        L = ["✅ Все чаты экспортированы полностью" if cnt.get("done", 0) == len(sel)
             else "⚠ Экспорт всех чатов завершён, но не всё скачано"]
        L.append(f"Папка: {root}")
        L.append(f"Завершено: {_dt.datetime.now():%d.%m.%Y %H:%M}, время работы: "
                 f"{fmt_duration(time.time() - started) or '0:00'}")
        L.append("")
        L.append(f"Чатов выбрано: {len(sel)}")
        L.append(f"  полностью готовы: {cnt.get('done', 0)}")
        if cnt.get("partial"):
            L.append(f"  тексты сохранены, но не все файлы скачаны: {cnt['partial']}")
        if cnt.get("failed"):
            L.append(f"  не удалось экспортировать: {cnt['failed']}")
        if cnt.get("scanned"):
            L.append(f"  просканированы, файлы ещё не скачаны: {cnt['scanned']}")
        bad = [e for e in sel if e.get("status") in ("failed", "partial")]
        if bad:
            L.append("")
            L.append("ТРЕБУЮТ ВНИМАНИЯ")
            for e in bad[:300]:
                if e["status"] == "failed":
                    L.append(f"  ❌ {e.get('name')} — {e.get('error', '')}")
                else:
                    L.append(f"  ⚠ {e.get('name')} — файлов {e.get('files_ok', 0)} из {e.get('files_want', 0)}")
        L.append("")
        L.append("Список всех чатов со ссылками: index.html. Журнал: _all_chats.log")
        if bad:
            L.append("Запустите «Экспорт всех чатов» ещё раз — готовые чаты пропустятся, "
                     "остальные продолжатся с места остановки.")
        text = "\n".join(L)
        try:
            with open(os.path.join(root, "_all_chats_report.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass
        has_index = False
        try:
            rows = []
            for c in chats:
                e = state["chats"].get(str(c[0]), {})
                od = e.get("out_dir")
                st_ = {"done": "✅", "partial": "⚠", "failed": "❌"}.get(e.get("status"), "…")
                link = esc(c[1])
                if od and os.path.exists(os.path.join(od, "messages.html")):
                    rel = os.path.relpath(os.path.join(od, "messages.html"), root).replace("\\", "/")
                    link = f"<a href='{quote(rel)}'>{esc(c[1])}</a>"
                rows.append(f"<tr><td>{st_}</td><td>{link}</td><td>{esc(c[2])}</td>"
                            f"<td>{e.get('msgs', '')}</td><td>{e.get('files_ok', '')}"
                            f"{'/' + str(e.get('files_want')) if e.get('files_want') else ''}</td></tr>")
            with open(os.path.join(root, "index.html"), "w", encoding="utf-8") as f:
                f.write("<!DOCTYPE html><html><head><meta charset='utf-8'><title>Экспорт Telegram</title>"
                        "<style>body{font-family:Segoe UI,Arial,sans-serif;margin:24px}"
                        "table{border-collapse:collapse}td,th{padding:6px 12px;border-bottom:1px solid #e5e5e5;"
                        "text-align:left}a{color:#168acd;text-decoration:none}</style></head><body>"
                        f"<h2>Экспорт Telegram — {len(chats)} чатов</h2><table>"
                        "<tr><th></th><th>Чат</th><th>Тип</th><th>Сообщений</th><th>Файлов</th></tr>"
                        + "".join(rows) + "</table></body></html>")
            has_index = True
        except Exception:
            pass
        return {"text": text, "complete": cnt.get("done", 0) == len(sel), "out_dir": root,
                "has_failed": False, "html": has_index, "html_file": "index.html", "all": True}

    async def export(self, cfg, chat, confirm=True, report_window=True, version=None, scan_only=False,
                     decline="stop"):
        """version: None — досканировать новые сообщения; "full" — пересканировать всю историю
        (получится новая версия); иначе — id сохранённой версии: экспорт из неё без чтения истории."""
        global SIZE_LIMIT_MB, MESSAGES_PER_PAGE, MISSING_REASON, STALL_TIMEOUT, THREADS_PER_FILE
        SIZE_LIMIT_MB = int(cfg["size_limit_mb"])
        MESSAGES_PER_PAGE = int(cfg["messages_per_page"])
        STALL_TIMEOUT = int(cfg["stall_timeout"])
        THREADS_PER_FILE = max(1, int(cfg.get("threads_per_file", 4)))
        MISSING_REASON = build_missing_reason()
        types = cfg["types"]
        retries = int(cfg["retries"])
        reserve = int(cfg["disk_reserve_mb"]) * 1024 * 1024
        limit = SIZE_LIMIT_MB * 1024 * 1024 if SIZE_LIMIT_MB > 0 else float("inf")   # 0 — без ограничения

        if not self.client:
            raise ValueError("Сначала подключитесь к Telegram (страница «Подключение»).")
        client = self.client
        hist = getattr(self, "hist", None) or client   # история — через режим экспорта Telegram, если он открыт
        self.stats.update(ok=0, failed=0, total_files=0, running=True, bytes_before=0)
        self.progress.reset(0)

        target = int(chat) if str(chat).lstrip("-").isdigit() else chat
        entity = await client.get_entity(target)
        title = ent_name(entity)

        # папка чата; два разных чата с одинаковым названием не смешиваются
        out_dir = os.path.join(cfg["out_root"], safe(title, 60))
        try:
            with open(os.path.join(out_dir, ".chat_id"), encoding="utf-8") as f:
                if f.read().strip() != str(entity.id):
                    out_dir = os.path.join(cfg["out_root"], safe(f"{title} ({entity.id})", 80))
        except OSError:
            pass
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, ".chat_id"), "w", encoding="utf-8") as f:
            f.write(str(entity.id))
        try:
            from telethon.utils import get_peer_id
            with open(os.path.join(out_dir, ".chat_peer"), "w", encoding="utf-8") as f:
                f.write(str(get_peer_id(entity)))
        except Exception:
            pass
        self.current_dir = out_dir
        self.q.put(("out_dir", out_dir))
        if self.logf:
            try:
                self.logf.close()
            except Exception:
                pass
        self.logf = open(os.path.join(out_dir, "export.log"), "a", encoding="utf-8", buffering=1)
        self.log(f"Чат: {title}\nПапка: {out_dir}")

        # старая история группы (до преобразования в супергруппу) — Telegram Desktop экспортирует и её
        sources = []
        if getattr(entity, "megagroup", False):
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                from telethon.tl.types import InputPeerChat
                full = await client(GetFullChannelRequest(entity))
                old_id = getattr(full.full_chat, "migrated_from_chat_id", None)
                if old_id:
                    try:
                        old = await client.get_entity(old_id * -1)
                    except Exception:
                        old = InputPeerChat(old_id)
                    sources.append((old, "m"))
                    self.log("Группа была преобразована в супергруппу — экспортирую и историю "
                             "до преобразования.")
            except Exception as e:
                self.log(f"Не удалось проверить старую историю группы: {e}")
        sources.append((entity, ""))

        namer = Namer(os.path.join(out_dir, "names.json"))

        self.phase("Считаю сообщения...")
        try:
            msg_total = await self.count_sources(entity)
        except Exception:
            msg_total = 0
        self.stats.update(stage="scan", msg_n=0, msg_total=msg_total)
        self.q.put(("chat_count", str(chat), msg_total))
        if msg_total:
            self.log(f"Сообщений в чате: {msg_total}")

        def eligibility(msg):
            """(obj, kind, size, status): status = 'want' | 'disabled' | 'too_big' | None (нет файла)."""
            obj = media_object(msg)
            if obj is None:
                return None, None, 0, None
            kind = media_kind(msg)
            size = media_size(msg, obj)
            if not types.get(kind):
                return obj, kind, size, "disabled"
            if size > limit:
                return obj, kind, size, "too_big"
            return obj, kind, size, "want"

        def place(msg, key, obj, kind, prefix):
            """Путь файла как у Telegram; уже скачанное прошлой версией программы — переносим, а не качаем."""
            rel = namer.assign(key, obj, msg.date)
            path = abs_path(out_dir, rel)
            if not os.path.exists(path) and kind and getattr(msg, "action", None) is None:
                try:
                    old = old_target_path(out_dir, msg, kind, prefix)
                except Exception:
                    old = None
                if old and old != path and (os.path.exists(old) or os.path.exists(old + ".part")):
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    for suf in ("", ".part", ".part.json"):
                        if os.path.exists(old + suf) and not os.path.exists(path + suf):
                            try:
                                os.replace(old + suf, path + suf)
                            except OSError:
                                pass
            return rel, path

        # --- 1. сканирование: история читается ОДИН раз, каждое сообщение сразу пишется в records.jsonl.
        # Каждые ~15 с сохраняется контрольная точка — после любого сбоя сканирование
        # продолжается с последнего сохранённого сообщения, а не с начала.
        # Сканирование не зависит от галочек типов и лимита размера: размер каждого файла запоминается,
        # а что качать — решается уже по текущим настройкам.
        # После каждого завершённого сканирования сохраняется сжатый снимок (versions/<дата>) —
        # из любого снимка потом можно сделать экспорт.
        cache_path = os.path.join(out_dir, "scan_cache.json")
        recs_path = os.path.join(out_dir, "records.jsonl")
        src_names = [p for _, p in sources]

        def want_status(kind, size):
            if not types.get(kind):
                return "disabled"
            if size > limit:
                return "too_big"
            return "want"

        snap_meta = None
        if version not in (None, "full"):
            snap_meta = next((m for m in list_snapshots(out_dir) if m["id"] == version), None)
            if snap_meta is None:
                raise ValueError(f"Сохранённая версия «{version}» не найдена в папке "
                                 f"{os.path.join(out_dir, VERSIONS_DIR)}.")

        if snap_meta is not None:
            media = {k: tuple(v) for k, v in snap_meta["media"].items()}
            total_msgs = snap_meta["total_msgs"]
            recs_source = os.path.join(out_dir, VERSIONS_DIR, snap_meta["id"], "records.jsonl.gz")
            version_label = snap_meta["created"]
            self.stats.update(msg_n=total_msgs, msg_total=total_msgs, stage="prepare")
            self.log(f"📁 Экспорт из сохранённой версии от {version_label}: {total_msgs} сообщений. "
                     "История из Telegram заново не читается; файлы качаются по этой версии "
                     "(файлы сообщений, удалённых с тех пор, Telegram уже не отдаст).")
        else:
            recs_source = recs_path
            media, total_msgs, last_ids, recs_off, scan_id, cache = {}, 0, {}, 0, None, {}
            try:
                with open(cache_path, encoding="utf-8") as f:
                    cache = json.load(f)
                csig = json.loads(cache.get("sig", "{}"))
                ok = csig.get("sources") == src_names and (
                    csig.get("v") == 5 or
                    (csig.get("v") == 4 and csig.get("types") == types and csig.get("limit") == SIZE_LIMIT_MB))
                if ok and os.path.exists(recs_path) and os.path.getsize(recs_path) >= cache["records_offset"]:
                    media = {k: tuple(v) for k, v in (cache.get("media") or cache.get("wanted") or {}).items()}
                    total_msgs = cache["total_msgs"]
                    last_ids = cache["last_ids"]
                    recs_off = cache["records_offset"]
                    scan_id = cache.get("scan_id")
                else:
                    cache = {}
                    if csig.get("v") == 4:
                        self.log("Изменились типы файлов или лимит размера — история сканируется заново "
                                 "(в этой версии программы такое больше не понадобится).")
            except Exception:
                cache = {}

            def snapshot_if_new(c, offset):
                """Снимок текущего records.jsonl, если такого ещё нет."""
                snaps = list_snapshots(out_dir)
                if snaps and snaps[0].get("scan_id") == c.get("scan_id") \
                        and snaps[0].get("records_offset") == offset:
                    return None
                return save_snapshot(out_dir, recs_path, offset, {
                    "scan_id": c.get("scan_id"), "records_offset": offset, "total_msgs": c["total_msgs"],
                    "media": c.get("media") or c.get("wanted") or {}, "title": title})

            if version == "full" and cache and cache.get("scan_done"):
                # перед полным пересканированием сохраняем то, что уже есть, — старая версия не пропадёт
                try:
                    r = await asyncio.to_thread(snapshot_if_new, cache, recs_off)
                    if r:
                        self.log(f"💾 Сохранена версия от {r[1]} (до пересканирования).")
                except Exception as e:
                    self.log(f"⚠ Не удалось сохранить текущую версию перед пересканированием: {e}")
                media, total_msgs, last_ids, recs_off, scan_id = {}, 0, {}, 0, None
                self.log("🔄 Полное пересканирование: вся история читается заново — получится новая версия "
                         "(с правками и без удалённых сообщений). Уже скачанные файлы повторно не качаются.")
            elif recs_off and cache.get("scan_done"):
                self.log(f"История уже просканирована ({total_msgs} сообщений) — проверяю только новые.")
            elif recs_off:
                self.log(f"Продолжаю сканирование с места остановки: уже {total_msgs} сообщений"
                         + (f" из {msg_total}" if msg_total else "") + ".")
            if not recs_off:
                media, total_msgs, last_ids = {}, 0, {}
            if not scan_id:
                scan_id = time.strftime("%Y%m%d%H%M%S")
            sig = json.dumps({"v": 5, "sources": src_names}, sort_keys=True)

            recs_f = open(recs_path, "r+b" if os.path.exists(recs_path) else "w+b")
            recs_f.truncate(recs_off)
            recs_f.seek(recs_off)
            scan_state = {"done": False}

            def cache_dict():
                return {"sig": sig, "media": media, "total_msgs": total_msgs, "last_ids": last_ids,
                        "records_offset": recs_f.tell(), "scan_done": scan_state["done"], "scan_id": scan_id,
                        "msg_total": msg_total}

            def checkpoint():
                recs_f.flush()
                try:
                    os.fsync(recs_f.fileno())
                except OSError:
                    pass
                namer.save()
                with open(cache_path + ".tmp", "w", encoding="utf-8") as f:
                    json.dump(cache_dict(), f, ensure_ascii=False)
                os.replace(cache_path + ".tmp", cache_path)

            names = Names(client)
            names.remember(entity)
            self.stats["msg_n"] = total_msgs
            self.scan_begin(total_msgs)
            last_cp = last_eta_log = time.monotonic()
            bad_msgs = 0
            final = None
            try:
                for src, prefix in sources:
                    last = last_ids.get(prefix, 0)
                    async for msg in hist.iter_messages(src, reverse=True, min_id=last, wait_time=0):
                        key = f"{prefix}{msg.id}"
                        try:
                            rec = await build_record(msg, key, prefix, title, names, namer, out_dir)
                        except (asyncio.CancelledError, StopRequested):
                            raise
                        except Exception as e:
                            bad_msgs += 1
                            if bad_msgs <= 20:
                                self.log(f"⚠ Сообщение {key} разобрано не полностью ({e!r}) — "
                                         "сохраняю текст и иду дальше.")
                            rec = fallback_record(msg, key, prefix, e)
                        try:
                            obj, kind, size, status = eligibility(msg)
                        except Exception:
                            obj, kind, size, status = None, None, 0, None
                        media_info = None
                        if obj is not None:
                            rel, _ = place(msg, key, obj, kind, prefix)
                            media_info = {"status": status, "path": None, "thumb": None, "rel": rel, "size": size}
                            media[key] = (kind, size, rel)
                        rec["media"] = media_info
                        rec["kind"] = kind
                        recs_f.write((rec_dump(rec) + "\n").encode("utf-8"))
                        total_msgs += 1
                        last = max(last, msg.id)
                        last_ids[prefix] = last
                        self.stats["msg_n"] = total_msgs
                        now = time.monotonic()
                        if total_msgs % 200 == 0:
                            eta = self.scan_eta()
                            self.phase(f"Сканирую историю: {total_msgs}" + (f" из {msg_total}" if msg_total else "")
                                       + (f" · закончу ≈ {fmt_when(eta)}" if eta else "") + "...")
                            if eta and now - last_eta_log > 600:
                                self.log(f"Сканирование: {total_msgs} из {msg_total}. Закончится примерно "
                                         f"{fmt_when(eta)} (осталось {fmt_left(eta)}, с учётом пауз Telegram).")
                                last_eta_log = now
                        if now - last_cp > 15:
                            checkpoint()
                            last_cp = now
                    last_ids[prefix] = last
                scan_state["done"] = True
            finally:
                try:
                    checkpoint()
                    final = cache_dict()
                except Exception as e:
                    self.log(f"Не удалось сохранить прогресс сканирования: {e}")
                recs_f.close()
            self.log(f"Сканирование завершено: {total_msgs} сообщений.")
            self.stats["stage"] = "prepare"      # дальше — сохранение версии, сводка, проверка скачанного
            self.phase(f"Просканировано сообщений: {total_msgs}")
            version_label = None
            if final:
                try:
                    self.phase("Сохраняю версию сканирования...")
                    r = await asyncio.to_thread(snapshot_if_new, final, final["records_offset"])
                    if r:
                        self.log(f"💾 Сохранена версия сканирования от {r[1]} — из неё можно сделать экспорт "
                                 "в любой момент (поле «Режим» на странице «Экспорт»).")
                        self.q.put(("versions_changed",))
                        version_label = r[1]
                except Exception as e:
                    self.log(f"⚠ Не удалось сохранить версию сканирования: {e}")

        wanted = {k: v for k, v in media.items() if want_status(v[0], v[1]) == "want"}
        too_big_n = sum(1 for v in media.values() if want_status(v[0], v[1]) == "too_big")

        # готово = есть в done.txt И файл реально лежит на диске целиком
        done_path = os.path.join(out_dir, "done.txt")
        done = set()
        if os.path.exists(done_path):
            with open(done_path, encoding="utf-8") as f:
                done = {x.strip() for x in f if x.strip()}

        def finished(size, path):
            return is_complete(path, size) and not os.path.exists(path + ".part") \
                and not os.path.exists(path + ".part.json")

        new_done = [k for k, (kd, sz, rel) in wanted.items()
                    if k not in done and finished(sz, abs_path(out_dir, rel))]
        if new_done:
            done.update(new_done)
            with open(done_path, "a", encoding="utf-8") as f:
                f.write("".join(f"{k}\n" for k in new_done))
        todo = {k: v for k, v in wanted.items()
                if not (k in done and is_complete(abs_path(out_dir, v[2]), v[1]))}
        todo_bytes = sum(v[1] for v in todo.values())
        partial = {k: part_bytes(abs_path(out_dir, v[2]), v[1]) for k, v in todo.items()}
        partial_bytes = sum(partial.values())

        lines = [f"Сообщений в чате: {total_msgs}", "", "Медиа к скачиванию:"]
        for kind, label in TYPE_LABELS.items():
            if not types.get(kind):
                continue
            items = [v[1] for v in wanted.values() if v[0] == kind]
            left = [v[1] for v in todo.values() if v[0] == kind]
            if items:
                lines.append(f"  {label}: {len(items)} шт., {human(sum(items))} "
                             f"(осталось {len(left)}, {human(sum(left))})")
        if too_big_n:
            lines.append(f"  Больше лимита {SIZE_LIMIT_MB} МБ (пропускаются): {too_big_n} шт.")
        need = todo_bytes - partial_bytes
        lines += ["", f"Уже скачано ранее: {len(wanted) - len(todo)} файлов",
                  f"Осталось скачать: {len(todo)} файлов, {human(need)}"]
        if partial_bytes:
            lines.append(f"Недокачанных файлов: {sum(1 for v in partial.values() if v)} "
                         f"({human(partial_bytes)} уже скачано) — будут докачаны с места обрыва")
        free = free_space(out_dir)
        lines.append(f"Свободно на диске: {human(free)}")
        if need + reserve <= free:
            lines.append(f"Места хватает (останется ~{human(free - need)}).")
        else:
            lines.append(f"\n⚠ МЕСТА НЕ ХВАТИТ: нужно ещё ~{human(need + reserve - free)}. Скачивание начнётся; "
                         "когда место кончится, оно встанет на паузу и продолжится само, как только вы освободите место.")
        summary = "\n".join(lines)
        self.log(summary)
        if scan_only:          # режим «сначала сканировать все чаты»: файлы — позже, после общей сводки
            self.phase(f"Просканирован: {title}")
            return {"scan_only": True, "title": title, "out_dir": out_dir, "msgs": total_msgs,
                    "files": len(todo), "need": max(0, need), "complete": not todo,
                    "ok": len(wanted) - len(todo), "want": len(wanted)}
        if todo and confirm and not cfg.get("auto_download", True):
            question = summary + ("\n\nСкачать файлы этого чата?\n«Нет» — пропустить файлы этого чата "
                                  "(тексты и HTML сохранятся, файлы можно докачать позже)."
                                  if decline == "skip_files" else "\n\nНачать?")
            if not await self.ask("yesno", "Начать загрузку?", question):
                if decline != "skip_files":
                    raise StopRequested()
                self.log("⏭ Файлы этого чата пропущены по вашему выбору — их можно докачать позже.")
                todo, todo_bytes, partial_bytes = {}, 0, 0
        elif todo and confirm:
            self.log("▶ Скачивание начинается сразу (настройка «Сразу скачивать после сканирования»).")

        # --- 2. загрузка файлов: сообщения с файлами запрашиваются по id пачками по 100,
        # историю заново читать не нужно ---
        st = {"pending": 0, "disk_full": False}
        self.stats["disk_low"] = 0
        status_of = {}
        self.stats.update(ok=len(wanted) - len(todo), failed=0, total_files=len(wanted),
                          stage="download", msg_n=total_msgs, msg_total=total_msgs)
        self.progress.reset(todo_bytes, partial_bytes)
        # уже скачанное в прошлые запуски — чтобы прогресс после обрыва считался по всему чату, а не с 0%
        self.stats["bytes_before"] = max(0, sum(v[1] for v in wanted.values()) - todo_bytes)
        sess = getattr(self, "session", None) or {"started": time.time(), "restarts": 0, "bytes": 0,
                                                    "dl_seconds": 0.0, "failed": {}}
        self.session = sess
        dl_start, n_start = time.monotonic(), partial_bytes
        bar = self.progress
        tasks = set()
        sem = asyncio.Semaphore(max(1, int(cfg["parallel"])))
        donef = open(done_path, "a", encoding="utf-8")
        failf = open(os.path.join(out_dir, "failed.txt"), "a", encoding="utf-8")
        if todo:
            self.phase("Загрузка...")

        async def get_thumb(obj, rel):
            if cname(obj) != "Document":
                return
            try:
                tp = abs_path(out_dir, rel) + "_thumb.jpg"
                if not os.path.exists(tp):
                    await download_thumb(client, obj, tp)
            except Exception:
                pass   # миниатюра не обязательна

        def give_up(key, rel, kind, size, path, reason, status="failed"):
            status_of[key] = status
            self.stats["failed"] += 1
            bar.total -= size
            bar.update(-part_bytes(path, size))  # недокачанная часть ждёт следующего запуска
            if status == "failed":
                self.log(f"Пропущен файл (сообщение {key}, {human(size)}): {reason}")
                failf.write(f"{key}\t{kind}\t{size}\t{reason!r}\n")
                failf.flush()
                sess["failed"][key] = (rel, kind, size, str(reason) or "ошибка")

        async def wait_for_space(need_now):
            """Места не хватает — скачивание на паузе, пока его не освободят (проверка раз в 5 с:
            один системный вызов, диск не сканируется). Освободили — продолжаем сами."""
            while free_space(out_dir) < need_now + st["pending"] + reserve:
                if st["pending"] > 0 and tasks:          # сначала дождёмся файлов, которые уже качаются
                    await asyncio.wait(list(tasks), timeout=5, return_when=asyncio.FIRST_COMPLETED)
                    continue
                lack = need_now + reserve - free_space(out_dir)
                self.stats["disk_low"] = max(1, lack)
                if not st["disk_full"]:
                    st["disk_full"] = True
                    self.log(f"⛔ Не хватает места на диске: нужно ещё {human(lack)}. Скачивание на паузе — "
                             "освободите место, и оно продолжится само.")
                self.phase(f"⛔ Не хватает места на диске — освободите ещё {human(lack)}, скачивание продолжится само.")
                await asyncio.sleep(5)
            if st["disk_full"]:
                st["disk_full"] = False
                self.stats["disk_low"] = 0
                self.log("✅ Место на диске появилось — продолжаю скачивание.")
                self.phase("Загрузка...")

        async def fetch(msg, src, key, rel, path, size, kind):
            try:
                attempt = 0
                refreshed = False
                t0 = time.monotonic()
                had = part_bytes(path, size)
                while True:
                    try:
                        await download_one(client, msg, path, size, bar)
                        dt_ = max(0.001, time.monotonic() - t0)
                        extra = f", докачан с {human(had)}" if had else ""
                        took = f"{max(1, round(dt_))} с" if dt_ < 60 else fmt_duration(dt_)
                        self.log(f"✓ {rel} — {human(size)} за {took} "
                                 f"({human((size - had) / dt_)}/с{extra})")
                        status_of[key] = "ok"
                        done.add(key)
                        self.stats["ok"] += 1
                        donef.write(f"{key}\n")
                        donef.flush()
                        await get_thumb(media_object(msg), rel)
                        break
                    except FloodWaitError as e:
                        self.log(f"⏸ Telegram просит подождать {e.seconds} с...")
                        await asyncio.sleep(e.seconds + 1)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        if cname(e) == "FileReferenceExpiredError" and refreshed:
                            # сообщение уже перечитано, а Telegram всё равно не отдаёт файл —
                            # самоуничтожающееся фото/видео или файл удалён; повторять бесполезно
                            give_up(key, rel, kind, size, path, "Telegram больше не отдаёт этот файл "
                                    "(самоуничтожающееся фото/видео или файл удалён)")
                            break
                        attempt += 1
                        if attempt <= retries:
                            self.log(f"Повтор {attempt}/{retries} для сообщения {key} "
                                     f"(продолжу с {human(part_bytes(path, size))}): {e}")
                            await asyncio.sleep(3)
                            if not await self._is_online():     # нет связи — ждём её, попытку не тратим
                                await self._wait_online()
                                attempt -= 1
                            try:   # обновляем сообщение — у старого могла истечь ссылка на файл
                                fresh = await client.get_messages(src, ids=msg.id)
                                refreshed = True
                                if fresh is not None and media_object(fresh) is not None:
                                    msg = fresh
                            except Exception:
                                pass
                            continue
                        give_up(key, rel, kind, size, path, e)
                        break
            finally:
                st["pending"] -= size
                sem.release()

        groups = collections.defaultdict(list)
        for k in todo:
            prefix = "m" if k.startswith("m") else ""
            groups[prefix].append(int(k[len(prefix):]))
        src_of = {p: s for s, p in sources}
        try:
            for prefix, ids in groups.items():
                src = src_of.get(prefix)
                ids.sort()
                for i in range(0, len(ids), 100):
                    batch = ids[i:i + 100]
                    msgs = await client.get_messages(src, ids=batch)
                    if not isinstance(msgs, list):
                        msgs = [msgs]
                    for mid, msg in zip(batch, msgs):
                        key = f"{prefix}{mid}"
                        kind, size, rel = todo[key]
                        path = abs_path(out_dir, rel)
                        if msg is None or media_object(msg) is None:
                            give_up(key, rel, kind, size, path, "сообщение удалено или файл больше недоступен")
                            continue
                        await wait_for_space(size - part_bytes(path, size))
                        await sem.acquire()
                        st["pending"] += size
                        t = asyncio.create_task(fetch(msg, src, key, rel, path, size, kind))
                        tasks.add(t)
                        t.add_done_callback(tasks.discard)
            if tasks:
                self.phase("Докачиваю последние файлы...")
                await asyncio.gather(*list(tasks))
        finally:
            self.stats["disk_low"] = 0
            pend = [t for t in tasks if not t.done()]
            for t in pend:
                t.cancel()
            if pend:   # ждём, пока загрузки корректно остановятся и сохранят недокачанные части
                await asyncio.wait(pend, timeout=30)
            donef.close()
            failf.close()
            namer.save()
            sess["bytes"] += max(0, bar.n - n_start)
            sess["dl_seconds"] += time.monotonic() - dl_start

        # --- сообщения из records.jsonl + итоговые статусы файлов ---
        self.stats["stage"] = "finish"
        self.phase("Собираю сообщения...")
        records = list(iter_records(recs_source))
        for r in records:
            mi = r.get("media")
            if not mi:
                continue
            rel = mi.pop("rel", None)
            rsize = mi.pop("size", None)
            if rsize is not None and r.get("kind"):
                mi["status"] = want_status(r["kind"], rsize)   # по текущим галочкам и лимиту
            if mi.get("status") != "want":
                continue
            key = r["key"]
            size = media.get(key, (None, rsize or 0, rel))[1]
            p = abs_path(out_dir, rel) if rel else ""
            if rel and key in done and is_complete(p, size):
                mi["status"], mi["path"] = "ok", rel
                tp = p + "_thumb.jpg"
                if os.path.exists(tp):
                    mi["thumb"] = (rel + "_thumb.jpg", os.path.getsize(tp))
            else:
                mi["status"] = status_of.get(key) if status_of.get(key) in ("failed", "no_space") else "missing"

        # --- 3. кастомные эмодзи (в тексте и реакциях) — как у Telegram, в папку stickers ---
        ce_ids = sorted({d for r in records for d in r["custom"]})
        ce_paths = {}
        if ce_ids:
            if not types.get("stickers"):
                ce_paths = {d: NOT_INCLUDED for d in ce_ids}
            else:
                self.phase(f"Кастомные эмодзи: {len(ce_ids)}...")
                try:
                    from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest
                    docs = []
                    for i in range(0, len(ce_ids), 100):
                        docs += await client(GetCustomEmojiDocumentsRequest(document_id=ce_ids[i:i + 100]))
                except Exception as e:
                    self.log(f"Не удалось получить кастомные эмодзи: {e}")
                    docs = []
                for doc in docs:
                    rel = namer.assign(f"ce{doc.id}", doc, None, folder="stickers")
                    path = abs_path(out_dir, rel)
                    try:
                        if not is_complete(path, doc.size):
                            await download_one(client, None, path, doc.size, Progress(), obj=doc)
                        ce_paths[doc.id] = rel
                    except Exception:
                        ce_paths[doc.id] = UNAVAILABLE
                namer.save()

        # --- 4. итоговая проверка: всё ли, что должно было скачаться, лежит на диске ---
        self.phase("Проверяю скачанное...")
        for r in records:
            mi = r.get("media")
            if mi and mi.get("status") == "ok" and not os.path.exists(abs_path(out_dir, mi["path"])):
                mi["status"] = "missing"
            finalize_record(r, ce_paths)
        not_ok = collections.Counter(r["media"]["status"] for r in records
                                     if r.get("media") and r["media"]["status"] != "ok")
        ok_total = sum(1 for r in records if r.get("media") and r["media"]["status"] == "ok")

        # --- 5. HTML / JSON (ошибка здесь не должна терять скачанное — просто пишем в лог) ---
        self.phase("Создаю HTML и JSON...")
        pages = 0
        if cfg["make_html"]:
            try:
                pages = render_html(out_dir, title, f"{len(records)} сообщений"
                                    + (f" · версия от {version_label}" if version_label else ""), records)
                self.log(f"HTML: {pages} стр. → {os.path.join(out_dir, 'messages.html')}")
            except Exception as e:
                self.log(f"❌ Не удалось создать HTML: {e!r}")
        if cfg["make_json"]:
            try:
                write_json(out_dir, entity, title, records)
                self.log(f"JSON → {os.path.join(out_dir, 'result.json')}")
            except Exception as e:
                self.log(f"❌ Не удалось создать JSON: {e!r}")

        want_total = ok_total + sum(not_ok.get(k, 0) for k in ("failed", "no_space", "missing"))
        rep = [f"✅ Готово. Сообщений: {len(records)}. Файлов на диске: {ok_total} из {want_total}."]
        labels = {"failed": "не удалось скачать (в failed.txt)", "no_space": "не хватило места",
                  "missing": "не найдено на диске", "too_big": f"больше лимита {SIZE_LIMIT_MB} МБ",
                  "disabled": "тип отключён в настройках"}
        for k, n in not_ok.items():
            rep.append(f"  • {labels.get(k, k)}: {n}")
        if ok_total < want_total:
            rep.append("Нажмите «Начать экспорт» ещё раз — недостающие файлы будут докачаны.")
        self.log("\n".join(rep))
        self.phase("Готово" if ok_total >= want_total else "Готово, но не всё скачано")

        report = build_report(
            title=title, out_dir=out_dir, records=records, sess=sess, cfg=cfg,
            ok_total=ok_total, want_total=want_total, not_ok=not_ok, labels=labels,
            pages=pages, ce_count=len(ce_ids))
        try:
            with open(os.path.join(out_dir, "report.txt"), "w", encoding="utf-8") as f:
                f.write(report["text"])
        except Exception:
            pass
        self.q.put(("finished", out_dir, report if report_window else None))
        return {"complete": ok_total >= want_total, "ok": ok_total, "want": want_total,
                "msgs": len(records), "out_dir": out_dir, "title": title}


# =================================== окно ===================================

def load_cfg():
    cfg = json.loads(json.dumps(DEFAULTS))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            saved = json.load(f)
        for k, v in saved.items():
            if k == "types" and isinstance(v, dict):
                cfg["types"].update(v)
            elif k in cfg:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def open_path(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception as e:
        messagebox.showerror("Ошибка", str(e))


# =================================== язык ===================================
# Весь текст окна проходит через tr(): надписи и кнопки (через перехват настроек Tk), таблицы, заголовки окон,
# сообщения, журнал и статусы. Русский — исходный язык; английский — словарь точных фраз + шаблоны для фраз
# с числами и именами. Имена чатов и прочие данные не переводятся.

LANG = "ru"
_TR_CACHE = {}
_REC = os.environ.get("TG_I18N_RECORD")
_CYR = re.compile("[А-Яа-яЁё]")


def system_lang():
    """Язык Windows: русский (и близкие: украинский, белорусский, казахский) или английский."""
    try:
        import ctypes
        lid = ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF
        return "ru" if lid in (0x19, 0x22, 0x23, 0x3F) else "en"
    except Exception:
        import locale
        loc = (locale.getlocale()[0] or "").lower()
        return "ru" if loc.startswith(("ru", "russian", "uk", "be", "kk")) else "en"


def set_language(choice):
    global LANG
    LANG = system_lang() if choice in (None, "", "system") else choice
    if LANG not in ("ru", "en"):
        LANG = "en"
    _TR_CACHE.clear()


def tr(text):
    if not isinstance(text, str) or not text:
        return text
    if _REC:
        try:
            with open(_REC, "a", encoding="utf-8") as f:
                f.write(json.dumps(text, ensure_ascii=False) + "\n")
        except Exception:
            pass
    if LANG == "ru" or not _CYR.search(text):
        return text
    hit = _TR_CACHE.get(text)
    if hit is not None:
        return hit
    out = EN.get(text)
    if out is None:
        stripped = text.strip()
        if stripped != text and stripped in EN:
            out = text.replace(stripped, EN[stripped])
        elif "\n" in text:
            out = "\n".join(tr(line) for line in text.split("\n"))
        else:
            out = _tr_pattern(text)
    if len(_TR_CACHE) > 20000:
        _TR_CACHE.clear()
    _TR_CACHE[text] = out
    return out


def _tr_pattern(text):
    for rx, rep in EN_RE:                 # сначала шаблоны для всей строки (в т. ч. со значком в начале)
        mm = rx.fullmatch(text)
        if mm:
            return _tr_units(rep(mm) if callable(rep) else mm.expand(rep))
    # ведущие значки (⏸ ✅ ⚠ 📡 …) и отступы сохраняем, переводим остальное
    m = re.match(r"^([^\wА-Яа-яЁё«(\"'№~\-−+]+)(.*)$", text, re.S)
    if m and m.group(2) and m.group(2) != text:
        inner = tr(m.group(2))
        if inner != m.group(2):
            return m.group(1) + inner
    return _tr_units(text)


_UNITS = (("ТБ", "TB"), ("ГБ", "GB"), ("МБ", "MB"), ("КБ", "KB"), ("Б", "B"))


def _tr_units(text):
    """Единицы и короткие обозначения внутри чисел: «4.2 МБ/с», «~1 ч 2 мин», «12 с»."""
    out = text
    for ru, en in _UNITS:
        out = re.sub(r"(\d) " + ru + r"(?![А-Яа-яЁё])", r"\1 " + en, out)
    out = re.sub(r"(?<=[B\d])/с\b", "/s", out)
    out = re.sub(r"(\d+) д (\d+) ч", r"\1 d \2 h", out)
    out = re.sub(r"(\d+) ч (\d+) мин", r"\1 h \2 min", out)
    out = re.sub(r"(\d+) ч\b", r"\1 h", out)
    out = re.sub(r"(\d+) мин\b", r"\1 min", out)
    out = re.sub(r"(\d+) с\b", r"\1 s", out)
    out = re.sub(r"(\d+) мс\b", r"\1 ms", out)
    out = out.replace("(сегодня)", "(today)").replace("(завтра)", "(tomorrow)")
    return out


def T(m, i):
    """Перевести группу шаблона (для вложенных фраз)."""
    return tr(m.group(i))


def install_i18n():
    """Перехват текста в Tk: надписи, кнопки, пункты меню, текст на холсте, заголовки окон, таблицы, сообщения."""
    if getattr(tk.Misc, "_i18n", False):
        return
    tk.Misc._i18n = True
    orig_options = tk.Misc._options

    def _options(self, cnf, kw=None):
        def fix(d):
            if isinstance(d, dict) and any(k in d for k in ("text", "label")):
                d = dict(d)
                for k in ("text", "label"):
                    if isinstance(d.get(k), str):
                        d[k] = tr(d[k])
            return d
        return orig_options(self, fix(cnf), fix(kw))
    tk.Misc._options = _options

    orig_title = tk.Wm.wm_title

    def wm_title(self, string=None):
        return orig_title(self, tr(string) if isinstance(string, str) else string)
    tk.Wm.wm_title = tk.Wm.title = wm_title

    orig_heading = ttk.Treeview.heading

    def heading(self, column, option=None, **kw):
        if isinstance(kw.get("text"), str):
            kw["text"] = tr(kw["text"])
        return orig_heading(self, column, option, **kw)
    ttk.Treeview.heading = heading

    def fix_values(kw):
        v = kw.get("values")
        if isinstance(v, (list, tuple)):
            kw["values"] = [tr(x) if isinstance(x, str) else x for x in v]
        return kw
    orig_insert, orig_item, orig_set = ttk.Treeview.insert, ttk.Treeview.item, ttk.Treeview.set

    def insert(self, parent, index, iid=None, **kw):
        return orig_insert(self, parent, index, iid, **fix_values(kw))

    def item(self, item_, option=None, **kw):
        return orig_item(self, item_, option, **fix_values(kw))

    def set_(self, item_, column=None, value=None):
        return orig_set(self, item_, column, tr(value) if isinstance(value, str) else value)
    ttk.Treeview.insert, ttk.Treeview.item, ttk.Treeview.set = insert, item, set_

    for name in ("showinfo", "showwarning", "showerror", "askyesno", "askokcancel", "askyesnocancel"):
        f = getattr(messagebox, name, None)
        if f is None:
            continue

        def wrap(f=f):
            def g(title=None, message=None, **kw):
                return f(tr(title) if isinstance(title, str) else title,
                         tr(message) if isinstance(message, str) else message, **kw)
            return g
        setattr(messagebox, name, wrap())


EN = {}      # точные фразы (заполняется ниже)
EN_RE = []   # шаблоны (заполняется ниже)

# ----------------------------- английский: точные фразы -----------------------------
EN.update({
    # навигация и общие
    "Экспорт": "Export", "Мои экспорты": "My exports", "Подключение": "Connection", "Настройки": "Settings",
    "Аккаунт": "Account", "Не подключено": "Not connected", "Подключение…": "Connecting…",
    "Подключение...": "Connecting...", "Отмена": "Cancel", "Закрыть": "Close", "Копировать": "Copy",
    "Скопировано": "Copied", "Продолжить?": "Continue?", "Готово": "Done", "Обзор…": "Browse…",
    "Изменить": "Change", "Открыть": "Open", "Папку": "Folder", "Отчёт": "Report", "HTML": "HTML",
    "Открыть папку": "Open folder", "Открыть HTML": "Open HTML", "Открыть список чатов": "Open chat list",
    "Ошибка": "Error", "Папка": "Folder", "Журнал": "Log", "Язык": "Language",
    # экспорт
    "Экспорт всех чатов…": "Export all chats…", "Экспорт всех чатов": "Export all chats",
    "Снять отметки": "Clear selection", "Посчитать сообщения": "Count messages", "Остановить": "Stop",
    "Найти чат: название, ID, @username или ссылка t.me": "Find chat: name, ID, @username or t.me link",
    "Чат": "Chat", "Тип": "Type", "Сообщений": "Messages", "Режим": "Mode", "Порядок": "Order",
    "По очереди": "One by one", "Сначала сканировать все": "Scan all first",
    "По очереди: скан → скачивание": "One by one: scan → download",
    "Что скачивать": "What to download", "Фото": "Photos", "Видео": "Videos", "Голосовые": "Voice",
    "Кружочки": "Video notes", "Стикеры": "Stickers", "GIF": "GIF", "Файлы": "Files",
    "Голосовые сообщения": "Voice messages", "Видеосообщения (кружочки)": "Video messages (round)",
    "Максимальный размер файла": "Maximum file size", "МБ": "MB", "Без ограничения": "No limit",
    "После сканирования": "After scanning", "Сразу скачивать": "Download right away",
    "Показать сводку": "Show summary", "Начать экспорт": "Start export",
    "Чат не выбран": "No chat selected", "Выберите чат в списке слева.": "Select a chat in the list on the left.",
    "Чат будет найден при запуске экспорта.": "The chat will be looked up when the export starts.",
    "Продолжить с места остановки": "Continue where it stopped",
    "Пересканировать всю историю заново": "Rescan the whole history",
    "Новый экспорт: вся история": "New export: whole history",
    "Список чатов пуст": "Chat list is empty",
    "Подключитесь к Telegram, чтобы выбрать чат для экспорта.": "Connect to Telegram to choose a chat to export.",
    "Перейти к подключению": "Go to Connection", "Ничего не найдено": "Nothing found",
    "Измените запрос. Чат не из списка можно указать по @username, ссылке t.me или ID.":
        "Change the query. A chat that is not in the list can be given by @username, t.me link or ID.",
    "Этого чата нет в списке": "This chat is not in the list",
    "Загружаю список чатов…": "Loading chat list…", "Это займёт несколько секунд.": "This takes a few seconds.",
    "Нет ответа от Telegram": "No response from Telegram",
    "Проверьте, что включён VPN или запущен прокси.": "Check that the VPN is on or the proxy is running.",
    "посчитать": "count", "считаю…": "counting…", "Личный": "Private", "Группа": "Group", "Канал": "Channel",
    "Перед скачиванием покажу, сколько файлов и места нужно.":
        "Before downloading I'll show how many files and how much space are needed.",
    "Сначала все чаты сканируются, потом скачиваются файлы. Перед скачиванием — одна общая сводка по всем чатам.":
        "All chats are scanned first, then files are downloaded. Before downloading — one summary for all chats.",
    "Сначала все чаты сканируются, потом скачиваются файлы. Скачивание начнётся сразу.":
        "All chats are scanned first, then files are downloaded. Downloading starts right away.",
    "Каждый чат сканируется и сразу скачивается, затем следующий. ":
        "Each chat is scanned and downloaded, then the next one. ",
    "Каждый чат сканируется и сразу скачивается, затем следующий. Перед скачиванием каждого чата — сводка; «Нет» пропустит файлы этого чата.":
        "Each chat is scanned and downloaded, then the next one. Before each chat's download — a summary; «No» skips that chat's files.",
    "Каждый чат сохранится в отдельную папку. Ошибка в одном чате не остановит остальные, а после «Остановить» экспорт продолжится с того же места.":
        "Each chat is saved to its own folder. An error in one chat won't stop the others, and after «Stop» the export continues from the same place.",
    "Какие чаты": "Which chats", "Пропускать чаты, которые уже полностью готовы": "Skip chats that are already complete",
    "Не выбран ни один вид чатов": "No chat type selected",
    # мои экспорты
    "Все": "All", "Незавершённые": "Unfinished", "Готовые": "Complete", "Обновить": "Refresh",
    "Продолжить": "Continue", "Начать заново": "Start over", "Версии…": "Versions…",
    "Продолжить незавершённые": "Continue unfinished", "Состояние": "Status", "Прогресс": "Progress",
    "Скорость": "Speed", "Осталось": "Remaining", "Объём файлов": "Files size", "Обновлён": "Updated",
    "Версий": "Versions", "Общее": "General", "Проверить снова": "Check again",
    "Настройки подключения": "Connection settings", "Сообщения": "Messages", "Завершится": "Finishes",
    "Скачано": "Downloaded", "Пропущено": "Skipped", "Чаты": "Chats", "Свободно на диске": "Free disk space",
    "Здесь появится ход экспорта: прогресс, скорость, прогноз окончания и журнал.":
        "Export progress will appear here: progress, speed, estimated finish and the log.",
    "5 мин": "5 min", "15 мин": "15 min", "1 час": "1 hour", "Открыть файл журнала": "Open log file",
    "Пока нет экспортов": "No exports yet",
    "Здесь появятся чаты, которые вы экспортируете, с их состоянием и действиями.":
        "Chats you export will appear here with their status and actions.",
    "Выбрать чат для экспорта": "Choose a chat to export", "Нет чатов в этой категории": "No chats in this category",
    "Выберите другой фильтр вверху.": "Choose another filter above.", "Обновляю список…": "Refreshing…",
    "Идёт": "In progress", "Идёт: сканирование": "In progress: scanning", "Идёт: скачивание": "In progress: downloading",
    "Идёт: сборка HTML и JSON": "In progress: building HTML and JSON", "пауза": "paused", "пауза…": "paused…",
    "сейчас": "now", "Готово": "Done", "Скачано не всё": "Not everything downloaded",
    "Сканирование не завершено": "Scan not finished", "Просканирован, файлы не скачаны": "Scanned, files not downloaded",
    "Файлы скачаны, HTML не собран": "Files downloaded, HTML not built", "Нет данных сканирования": "No scan data",
    "Старая версия программы — нужно сканирование": "Old program version — needs scanning",
    "нет доступа": "no access", "нет доступа (закрытый чат или вас удалили)": "no access (private chat or you were removed)",
    "нужны права администратора": "admin rights required", "чат не найден": "chat not found",
    "вы заблокированы в этом чате": "you are banned in this chat", "Открыть HTML": "Open HTML",
    "Выберите версию в поле «Режим» и нажмите «Начать экспорт».":
        "Choose a version in the «Mode» field and press «Start export».",
    "Пересканировать всё заново?": "Rescan everything?",
    "Вся история чата будет прочитана заново — на большом чате это займёт столько же времени, сколько первое сканирование.":
        "The whole chat history will be read again — for a big chat this takes as long as the first scan.",
    "Текущая версия сохранится и останется в списке «Режим». Уже скачанные файлы повторно не качаются.":
        "The current version is kept in the «Mode» list. Already downloaded files are not downloaded again.",
    "Продолжить экспорт": "Continue export", "Продолжить незавершённые": "Continue unfinished",
    "Каждый продолжится с места остановки; ошибки в отдельных чатах не остановят остальные.":
        "Each one continues where it stopped; errors in some chats won't stop the others.",
    "Не удалось определить ID выбранных чатов.": "Couldn't determine the IDs of the selected chats.",
    "Не удалось определить ID чата.": "Couldn't determine the chat ID.", "Нет чатов": "No chats",
    # подключение
    "Подключиться": "Connect", "Переподключиться": "Reconnect", "Выйти из аккаунта": "Log out",
    "Показать hash": "Show hash", "Скрыть hash": "Hide hash", "Где взять API ID и hash →": "Where to get API ID and hash →",
    "Прокси": "Proxy", "Без прокси": "No proxy", "MTProto": "MTProto",
    "Ссылка": "Link", "Сервер": "Server", "Порт": "Port", "Секрет": "Secret", "Логин": "Login", "Пароль": "Password",
    "Проверка связи": "Connection test",
    "Сравните варианты: проверьте с включённым VPN, потом выключите VPN, запустите прокси и проверьте снова. Покажу пинг и скорость скачивания и подскажу, где быстрее.":
        "Compare options: test with the VPN on, then turn the VPN off, start the proxy and test again. I'll show ping and download speed and tell you which is faster.",
    "Проверить подключение": "Test connection", "Когда": "When", "Вариант": "Option", "Пинг": "Ping", "Итог": "Result",
    "Выбрать этот вариант": "Use this option", "Без прокси / VPN": "No proxy / VPN", "проверяю…": "testing…",
    "работает": "works", "не подключилось": "failed to connect",
    "Проверяю… это займёт до полуминуты на вариант.": "Testing… up to half a minute per option.",
    "Ищу файл для замера скорости…": "Looking for a file to measure speed…",
    "Подходящего файла для замера скорости не нашлось — проверяю только подключение и пинг.":
        "No suitable file to measure speed — testing connection and ping only.",
    "Готово. Можно переключить VPN/прокси и проверить ещё раз.": "Done. You can switch VPN/proxy and test again.",
    "Ни один вариант не подключился — проверьте интернет, VPN или прокси.":
        "No option connected — check the internet, VPN or proxy.",
    "Вариант выбран. Нажмите «Подключиться»/«Переподключиться», чтобы применить его.":
        "Option selected. Press «Connect»/«Reconnect» to apply it.",
    "Нужны API ID и API hash": "API ID and API hash are needed",
    "Для проверки связи заполните API ID и API hash.": "Fill in API ID and API hash to test the connection.",
    "Заполните API ID (число) и API hash.": "Fill in API ID (a number) and API hash.",
    "Их можно получить на my.telegram.org → API development tools.": "Get them at my.telegram.org → API development tools.",
    "Вход по QR-коду или по номеру телефона. Нужны API ID и API hash — их выдают бесплатно на my.telegram.org.":
        "Log in with a QR code or phone number. API ID and API hash are needed — get them free at my.telegram.org.",
    "Сессия сохранена — при следующем запуске программа подключится сама.":
        "Session saved — next time the program will connect by itself.",
    "Если откроется окно с QR-кодом, отсканируйте его в Telegram на телефоне.":
        "If a QR code window opens, scan it in Telegram on your phone.",
    "Подключение напрямую. Подходит, если VPN включён на весь компьютер.":
        "Direct connection. Use it if a VPN is on for the whole computer.",
    "Вставьте ссылку из tg-ws-proxy (значок в трее → «Скопировать ссылку») или заполните сервер, порт и секрет вручную.":
        "Paste the link from tg-ws-proxy (tray icon → «Copy link») or fill in server, port and secret manually.",
    "Вставьте ссылку на прокси (tg://proxy?… или https://t.me/proxy?…) или заполните сервер, порт и секрет вручную. Подойдёт любой MTProto-прокси, в том числе локальный tg-ws-proxy.":
        "Paste a proxy link (tg://proxy?… or https://t.me/proxy?…) or fill in server, port and secret manually. Any MTProto proxy works, including a local tg-ws-proxy.",
    "«Проверить подключение» — пинг и скорость выбранного сейчас способа. «Диагностика» сравнит способы по шагам: сначала с включённым VPN, потом попросит выключить VPN и проверит прокси, и подскажет, где быстрее.":
        "“Check connection” measures ping and speed of the currently selected method. “Diagnostics” compares methods step by step: first with the VPN on, then asks you to turn the VPN off and tests the proxy, and suggests the fastest.",
    "Диагностика…": "Diagnostics…",
    "Диагностика — шаг 1 из 2": "Diagnostics — step 1 of 2", "Диагностика — шаг 2 из 2": "Diagnostics — step 2 of 2",
    "Локальный SOCKS5-порт VPN-клиента или обычный SOCKS5-прокси.": "A VPN client's local SOCKS5 port or a regular SOCKS5 proxy.",
    "Нажмите «Подключиться», чтобы войти в Telegram.": "Press «Connect» to log in to Telegram.",
    "Укажите API ID и API hash и нажмите «Подключиться».": "Enter API ID and API hash and press «Connect».",
    "Подключаюсь к Telegram…": "Connecting to Telegram…", "Проверяю связь с Telegram…": "Checking connection to Telegram…",
    "Переподключиться?": "Reconnect?",
    "Экспорт ненадолго приостановится: программа переподключится с текущими настройками и продолжит с того же места.":
        "The export will pause briefly: the program reconnects with the current settings and continues from the same place.",
    "Выйти из аккаунта Telegram в этой программе?": "Log out of Telegram in this program?",
    "Чтобы снова экспортировать чаты, понадобится войти заново.": "To export chats again you'll need to log in again.",
    "Нужно подключение": "Connection required",
    "Сначала подключитесь к Telegram на странице «Подключение».": "Connect to Telegram on the «Connection» page first.",
    "Сначала подключитесь к Telegram.": "Connect to Telegram first.",
    "Подключитесь к Telegram и дождитесь загрузки списка чатов.": "Connect to Telegram and wait for the chat list.",
    "Вход по QR-коду": "QR code login",
    "Откройте Telegram на телефоне: Настройки → Устройства → Подключить устройство, и наведите камеру на код.":
        "Open Telegram on your phone: Settings → Devices → Link Desktop Device, and point the camera at the code.",
    "Код обновляется сам, когда истекает.": "The code refreshes by itself when it expires.",
    "Войти по номеру телефона": "Log in with phone number",
    "Вход в Telegram": "Telegram login", "Номер телефона (в формате +7...):": "Phone number (e.g. +1...):",
    "Код подтверждения": "Confirmation code", "Облачный пароль": "Cloud password",
    "Введите облачный пароль (двухэтапная аутентификация):": "Enter your cloud password (two-step verification):",
    "Введите код. Оставьте поле пустым и нажмите OK,": "Enter the code. Leave it empty and press OK",
    "чтобы запросить код повторно (другим способом, если Telegram позволит).": "to request the code again (another way, if Telegram allows).",
    "чтобы запросить код повторно.": "to request the code again.",
    "Введите код": "Enter the code", "Введите пароль": "Enter the password",
    "Неверный код, попробуйте ещё раз.": "Wrong code, try again.",
    # настройки
    "Сохранение": "Saving", "Папка экспорта": "Export folder", "Форматы": "Formats",
    "HTML — для просмотра в браузере": "HTML — to view in a browser", "JSON — формат Telegram Desktop": "JSON — Telegram Desktop format",
    "Скорость и надёжность": "Speed and reliability", "Файлов одновременно": "Files at once", "оптимально 3–6": "3–6 is optimal",
    "Потоков на большой файл": "Threads per large file", "для файлов больше 2 МБ": "for files over 2 MB",
    "Зависшим считать через, с": "Treat as stuck after, s", "если не пришло ни одного байта": "if not a single byte arrived",
    "Повторов перед пропуском": "Retries before skipping", "потом файл пропускается и попадает в отчёт": "then the file is skipped and listed in the report",
    "Язык программы": "Program language", "Как в системе": "System default", "Русский": "Русский", "English": "English",
    "Оформление": "Appearance", "Тема": "Theme", "Светлая": "Light", "Тёмная": "Dark",
    "Язык изменится после перезапуска программы.": "The language will change after the program restarts.",
    "Чтобы сменить язык, программу нужно перезапустить. Перезапустить сейчас?": "The program must restart to change the language. Restart now?",
    "Проверьте настройки": "Check the settings",
    "Выберите папку, куда сохранять экспорт.": "Choose the folder to save the export to.",
    "Выберите чат": "Choose a chat",
    "Выберите чат в списке или введите его @username, ссылку или ID.": "Choose a chat in the list or enter its @username, link or ID.",
    "Без файлов": "No files", "Не выбран ни один тип файлов. Экспортировать только текст?": "No file types selected. Export text only?",
    "Файл журнала появится в папке чата после начала экспорта.": "The log file appears in the chat folder after the export starts.",
    "Папка ещё не создана.": "The folder doesn't exist yet.", "HTML ещё не создан — он появляется в конце экспорта.": "HTML isn't built yet — it appears at the end of the export.",
    "Выход": "Exit", "Идёт работа. Остановить и выйти?": "Work in progress. Stop and exit?",
    "При следующем запуске работа продолжится с того же места.": "Next time it will continue from the same place.",
    "Посчитать сообщения": "Count messages",
    "Можно продолжать работу, подсчёт идёт в фоне и его можно остановить.": "You can keep working; counting runs in the background and can be stopped.",
    "Начать?": "Start?",
    # отчёт
    "Отчёт об экспорте": "Export report", "Докачать недостающее": "Download missing files", "Список пропущенных": "Skipped files",
    "Экспорт завершён полностью": "Export completed", "Экспорт завершён, но скачано не всё": "Export finished, but not everything was downloaded",
    "Все чаты экспортированы полностью": "All chats exported completely",
    "Экспорт всех чатов завершён, но не всё скачано": "Exporting all chats finished, but not everything was downloaded",
    "ОБЩЕЕ": "SUMMARY", "ПО ТИПАМ ФАЙЛОВ": "BY FILE TYPE", "РЕЗУЛЬТАТ": "RESULT", "ТРЕБУЮТ ВНИМАНИЯ": "NEED ATTENTION",
    "Медиа к скачиванию:": "Media to download:", "  файлов в чате нет": "  no files in the chat",
    "Полный лог: export.log": "Full log: export.log", "JSON: result.json (формат Telegram Desktop)": "JSON: result.json (Telegram Desktop format)",
    "Чтобы докачать недостающее, нажмите «Докачать недостающее» или «Продолжить» в «Моих экспортах» — скачанное повторно не загружается.":
        "To get the missing files, press «Download missing files» or «Continue» in «My exports» — downloaded files are not fetched again.",
    "Запустите «Экспорт всех чатов» ещё раз — готовые чаты пропустятся, остальные продолжатся с места остановки.":
        "Run «Export all chats» again — complete chats are skipped, the rest continue where they stopped.",
    "Нажмите «Начать экспорт» ещё раз — недостающие файлы будут докачаны.": "Press «Start export» again — missing files will be downloaded.",
    "Списoк всех чатов со ссылками: index.html. Журнал: _all_chats.log": "List of all chats with links: index.html. Log: _all_chats.log",
    "Список всех чатов со ссылками: index.html. Журнал: _all_chats.log": "List of all chats with links: index.html. Log: _all_chats.log",
    # статусы и журнал
    "ℹ Закрыт незавершённый прошлый сеанс режима экспорта Telegram.": "ℹ Closed an unfinished previous Telegram export mode session.",
    "Идёт: проверка новых сообщений": "In progress: checking for new messages",
    "Идёт: подготовка к скачиванию": "In progress: preparing download",
    "Ждёт связи с Telegram": "Waiting for Telegram connection",
    "Вырезать": "Cut", "Вставить": "Paste", "Выделить всё": "Select all",
    "Telegram больше не отдаёт этот файл (самоуничтожающееся фото/видео или файл удалён)":
        "Telegram no longer provides this file (self-destructing photo/video or deleted file)",
    "Здесь — настройки самой программы. Всё, что касается экспорта (папка, форматы, скорость скачивания), — на странице «Экспорт».":
        "Settings of the program itself. Everything about exporting (folder, formats, download speed) is on the Export page.",
    "Дополнительно: форматы, скорость, повторы…": "More: formats, speed, retries…",
    "Скрыть дополнительные настройки": "Hide extra settings",
    "Форматы": "Formats", "Скорость и надёжность": "Speed and reliability",
    "Файлов одновременно (3–6)": "Files at once (3–6)", "Потоков на файл больше 2 МБ": "Threads per file over 2 MB",
    "Зависший — файл, по которому за это время не пришло ни одного байта. После всех повторов файл пропускается и попадает в отчёт; его можно докачать позже.":
        "Stalled means not a single byte arrived during this time. After all retries the file is skipped and listed in the report; it can be downloaded later.",
    "Проверка связи — шаг 1 из 2": "Connection test — step 1 of 2",
    "Проверка связи — шаг 2 из 2": "Connection test — step 2 of 2",
    "Шаг 1: VPN.\n\nВключите VPN (если пользуетесь им) и нажмите «ОК» — проверю подключение без прокси.\n\nПотом попрошу выключить VPN и проверю прокси: VPN и прокси одновременно мешают друг другу, поэтому замеры идут по очереди.":
        "Step 1: VPN.\n\nTurn on your VPN (if you use one) and press OK — I will test the connection without a proxy.\n\nThen I will ask you to turn the VPN off and test the proxy: VPN and proxy interfere with each other, so they are measured one after another.",
    "Проверка идёт по шагам: сначала с включённым VPN, потом программа попросит выключить VPN и проверит прокси. Покажу пинг и скорость скачивания и подскажу, где быстрее.":
        "The test runs in steps: first with the VPN on, then the program asks you to turn the VPN off and tests the proxy. It shows ping and download speed and suggests the fastest option.",
    "VPN / без прокси": "VPN / no proxy",
    "Чтобы не ждать: в Telegram на телефоне откройте сообщение «Data export request» от Telegram, нажмите Allow (Разрешить), затем «Проверить сейчас».":
        "No need to wait: on your phone open the “Data export request” message from Telegram, press Allow, then “Check now”.",
    "Когда программа впервые просит этот режим, Telegram присылает в чат «Telegram» сообщение «Data export request» — это запрос этой программы. Нажмите в нём Allow (Разрешить) на телефоне — режим разрешится сразу (сообщение станет «Accepted»). Если не нажимать, Telegram разрешит его сам примерно через сутки.":
        "When the program first asks for this mode, Telegram sends a “Data export request” message to the “Telegram” chat — that is this program's request. Press Allow in it on your phone and the mode is allowed right away (the message becomes “Accepted”). Otherwise Telegram allows it by itself in about a day.",
    "сейчас": "now",
    "Версии сканирования": "Scan versions", "Версии сканирования…": "Scan versions…",
    "Снимок истории сохраняется после каждого завершённого сканирования. Из любой версии можно собрать экспорт — в нём будут и сообщения, которые потом удалили. HTML и JSON в папке чата будут пересобраны по выбранной версии; уже скачанные файлы не качаются заново (файлы удалённых сообщений Telegram может уже не отдать).":
        "A snapshot of the history is saved after every completed scan. You can build an export from any version — it includes messages deleted later. HTML and JSON in the chat folder are rebuilt from the chosen version; files already downloaded are not downloaded again (Telegram may no longer provide files of deleted messages).",
    "Сканирование": "Scan", "Файлов": "Files", "Занимает": "Size on disk",
    "Версий пока нет — первая появится после завершённого сканирования.": "No versions yet — the first one appears after a completed scan.",
    "Экспорт из версии": "Export from version", "Экспорт из этой версии": "Export from this version",
    "Открыть папку версии": "Open version folder",
    "Идёт экспорт — дождитесь окончания.": "An export is running — wait until it finishes.",
    "Переподключаюсь с текущими настройками…": "Reconnecting with the current settings…",
    "без прокси": "no proxy",
    "Файл входа (tg_export_session.session) занят: похоже, программа запущена дважды. Закройте лишние окна программы (или перезагрузите компьютер) и запустите её снова.":
        "The sign-in file (tg_export_session.session) is busy: the program seems to be running twice. Close the extra program windows (or restart the computer) and start it again.",
    "Программа уже запущена — её окно открыто (возможно, свёрнуто на панели задач).":
        "The program is already running — its window is open (maybe minimized to the taskbar).",
    "по SMS — но Telegram отправляет такое SMS только официальным приложениям (после проверки Google/Apple), поэтому сюда оно, скорее всего, не придёт":
        "by SMS — but Telegram sends such SMS only to official apps (after a Google/Apple check), so it will most likely not arrive here",
    "Перехожу ко входу по QR-коду.": "Switching to QR-code login.",
    "Обычно Telegram присылает код не по SMS, а в чат «Telegram» (с синей галочкой) в приложении на телефоне или компьютере, где вы уже вошли.":
        "Usually Telegram sends the code not by SMS but to the “Telegram” chat (blue check mark) in the app on a phone or computer where you are already logged in.",
    "Введите код. Оставьте поле пустым и нажмите OK — код будет запрошен повторно (другим способом, если Telegram позволит). «Отмена» — войти по QR-коду.":
        "Enter the code. Leave the field empty and press OK to request the code again (another way, if Telegram allows). “Cancel” — log in with a QR code.",
    "Код не пришёл?": "No code?",
    "Войти по QR-коду? Это надёжнее: код по номеру Telegram часто не отправляет сторонним программам.\n\n«Нет» — отменить вход.":
        "Log in with a QR code? It is more reliable: Telegram often doesn't send phone codes to third-party apps.\n\n“No” cancels the login.",
    "Открываю режим экспорта Telegram...": "Opening Telegram export mode...", "Считаю сообщения...": "Counting messages...",
    "Загрузка...": "Downloading...", "Докачиваю последние файлы...": "Finishing the last files...",
    "Собираю сообщения...": "Collecting messages...", "Проверяю скачанное...": "Checking downloaded files...",
    "Создаю HTML и JSON...": "Building HTML and JSON...", "Сохраняю версию сканирования...": "Saving the scan version...",
    "Готово, но не всё скачано": "Done, but not everything downloaded", "Останавливаю…": "Stopping…",
    "Готово к экспорту": "Ready to export", "Загружаю список чатов...": "Loading chat list...", "Вход по QR-коду...": "QR code login...",
    "Вы вышли из аккаунта.": "You have logged out.",
    "Найдена сохранённая сессия — подключаюсь автоматически...": "Saved session found — connecting automatically...",
    "Подключение напрямую (без прокси)": "Connecting directly (no proxy)",
    "Аккаунт с Telegram Premium — скорость загрузки не ограничивается Telegram.": "Telegram Premium account — Telegram doesn't limit download speed.",
    "ℹ Аккаунт без Telegram Premium: Telegram ограничивает скорость загрузки файлов для таких аккаунтов. Многопоточность частично помогает, но потолок задаёт сервер.":
        "ℹ No Telegram Premium: Telegram limits download speed for such accounts. Multiple threads help partly, but the server sets the ceiling.",
    "Ускоритель шифрования cryptg: включён.": "cryptg encryption accelerator: on.",
    "Отсканируйте QR-код в приложении Telegram на телефоне: Настройки → Устройства → Подключить устройство.":
        "Scan the QR code in Telegram on your phone: Settings → Devices → Link Desktop Device.",
    "⚡ Включён режим экспорта Telegram: меньше пауз при чтении истории. На других ваших устройствах Telegram может показать уведомление о запросе экспорта — это он.":
        "⚡ Telegram export mode is on: fewer pauses while reading history. Telegram may show a data export notification on your other devices — that's this.",
    "Связь с Telegram восстановлена.": "Connection to Telegram restored.",
    "Переподключаюсь к Telegram...": "Reconnecting to Telegram...",
    "📡 Связь восстановлена — продолжаю с места остановки.": "📡 Connection restored — continuing where it stopped.",
    "Остановлено. При следующем запуске загрузка продолжится с места остановки.": "Stopped. Next time downloading continues where it stopped.",
    "▶ Продолжаю экспорт после переподключения.": "▶ Continuing the export after reconnecting.",
    "Скачивание начинается сразу (настройка «Сразу скачивать после сканирования»).": "Downloading starts right away (the «Download right away» setting).",
    "Группа была преобразована в супергруппу — экспортирую и историю до преобразования.":
        "The group was converted to a supergroup — exporting its earlier history too.",
    "Изменились типы файлов или лимит размера — история сканируется заново (в этой версии программы такое больше не понадобится).":
        "File types or size limit changed — the history is scanned again (this version won't need that anymore).",
    "⏭ Файлы этого чата пропущены по вашему выбору — их можно докачать позже.": "⏭ This chat's files were skipped as you chose — you can download them later.",
    "✅ Место на диске появилось — продолжаю скачивание.": "✅ Disk space is available — continuing the download.",
    "Скачать файлы всех чатов?": "Download files of all chats?", "Начать загрузку?": "Start downloading?",
    "Начать скачивание?": "Start downloading?",
    "▶ Скачиваю файлы чатов по очереди.": "▶ Downloading chats' files one by one.",
    "⏹ Скачивание файлов отложено. Чаты просканированы — продолжить можно в «Моих экспортах».":
        "⏹ File download postponed. Chats are scanned — you can continue in «My exports».",
    "Проверка не удалась": "Test failed",
    "нет прогресса": "no progress",
    "проверьте, что включён VPN или запущен прокси, и что на странице «Подключение» выбран нужный вид прокси. Ничего не потеряется: программа продолжит сама, когда связь появится.":
        "check that the VPN is on or the proxy is running, and that the right proxy type is selected on the «Connection» page. Nothing will be lost: the program continues by itself once the connection is back.",
})

# ----------------------------- английский: шаблоны -----------------------------
_R = re.compile


def _u(f):
    """Шаблон-функция + перевод единиц в результате."""
    return lambda m: _tr_units(f(m))


EN_RE.extend([
    (_R(r"Подключено: (.+)"), r"Connected: \1"),
    (_R(r"Выбрано чатов: (\d+)"), r"Chats selected: \1"),
    (_R(r"Экспортировать выбранные \((\d+)\)"), r"Export selected (\1)"),
    (_R(r"Сохранение: (.+)"), lambda m: f"Saving to: {tr(m.group(1))}"),
    (_R(r"(Личный|Группа|Канал) · (.+)"), lambda m: f"{tr(m.group(1))} · {tr(m.group(2))}"),
    (_R(r"([\d ]+) сообщений"), _u(lambda m: f"{m.group(1)} messages")),
    (_R(r"считаю сообщения…"), "counting messages…"),
    (_R(r"не удалось посчитать сообщения"), "couldn't count messages"),
    (_R(r"Экспорт пойдёт по адресу «(.+)» — нажмите «Начать экспорт»\."),
     r"The export will use «\1» — press «Start export»."),
    (_R(r"Личные · (\d+)"), r"Private · \1"), (_R(r"Группы · (\d+)"), r"Groups · \1"), (_R(r"Каналы · (\d+)"), r"Channels · \1"),
    (_R(r"Будет экспортировано чатов: (\d+)"), r"Chats to export: \1"),
    (_R(r"Папка: (.+)"), r"Folder: \1"), (_R(r"Чат: (.+)"), r"Chat: \1"),
    (_R(r"(\d+) чатов · готово (\d+) · незавершённых (\d+)"), r"\1 chats · \2 complete · \3 unfinished"),
    (_R(r"Мои экспорты  ·  (\d+%)"), r"My exports  ·  \1"), (_R(r"Мои экспорты  ·  идёт"), "My exports  ·  running"),
    (_R(r"Мои экспорты  ·  нет места"), "My exports  ·  no space"),
    (_R(r"(Чат|Тип|Сообщений) ([▲▼])"), lambda m: f"{tr(m.group(1))} {m.group(2)}"),
    (_R(r"Считаю сообщения: (\d+) из (\d+)"), r"Counting messages: \1 of \2"),
    (_R(r"Нужно посчитать (\d+) чатов — это \d+ запросов к Telegram, примерно (\d+) мин\. \(Telegram может просить паузы\.\)"),
     r"\1 chats to count — that's \1 requests to Telegram, about \2 min. (Telegram may ask for pauses.)"),
    (_R(r"Из версии от (.+) · ([\d ]+) сообщ\."), r"From version of \1 · \2 msgs"),
    (_R(r"(.+) \(скан, с паузами\)"), _u(lambda m: f"{m.group(1)} (scan, incl. pauses)")),
    (_R(r"скан: (.+)"), _u(lambda m: f"scan: {m.group(1)}")),
    (_R(r"просканировано ([\d ]+)"), r"scanned \1"),
    (_R(r"([\d ]+) из ([\d ]+) \((\d+%)\)( · .+)?"),
     _u(lambda m: f"{m.group(1)} of {m.group(2)} ({m.group(3)})" + (tr(m.group(4)[3:]).join([" · ", ""]) if m.group(4) else ""))),
    (_R(r"(.+) / (.+) \((\d+%)\)"), _u(lambda m: f"{m.group(1)} / {m.group(2)} ({m.group(3)})")),
    (_R(r"(.+)/с · ср\. (.+)/с"), _u(lambda m: f"{m.group(1)}/s · avg {m.group(2)}/s")),
    (_R(r"([\d ]+) сообщ\./с"), r"\1 msg/s"), (_R(r"([\d ]+)/с"), r"\1/s"),
    (_R(r"([\d ]+) из ([\d ]+)"), r"\1 of \2"),
    (_R(r"(\d+) из (\d+): (.+)"), r"\1 of \2: \3"),
    (_R(r"(.+) · чат (\d+) из (\d+)"), r"\1 · chat \2 of \3"),
    (_R(r"найдено (.+)"), _u(lambda m: f"found {m.group(1)}")),
    (_R(r"([\d.,]+ (?:Б|КБ|МБ|ГБ|ТБ)) из ([\d.,]+ (?:Б|КБ|МБ|ГБ|ТБ))"), _u(lambda m: f"{m.group(1)} of {m.group(2)}")),
    (_R(r"(.+) — ([\d.,]+ (?:Б|КБ|МБ|ГБ|ТБ)) — (.+)"), _u(lambda m: f"{m.group(1)} — {m.group(2)} — {tr(m.group(3))}")),
    (_R(r"−(\d+) мин"), r"−\1 min"), (_R(r"−1 ч"), "−1 h"), (_R(r"−30 мин"), "−30 min"),
    (_R(r"Скорость (скачивания|сканирования, сообщений в секунду)  ·  сейчас (.+)  ·  в среднем (.+)  ·  максимум (.+)"),
     _u(lambda m: ("Download speed" if m.group(1) == "скачивания" else "Scan speed, messages per second")
        + f"  ·  now {m.group(2)}  ·  average {m.group(3)}  ·  peak {m.group(4)}")),
    (_R(r"Скорость появится, когда начнётся сканирование или скачивание"), "Speed appears once scanning or downloading starts"),
    (_R(r"Быстрее всего: (.+) \((.+)\) — (.+)"),
     _u(lambda m: f"Fastest: {tr(m.group(1))} ({m.group(2)}) — " + m.group(3).replace("пинг", "ping"))),
    (_R(r"Telegram не отвечает уже (.+?) — (.+)"), lambda m: f"Telegram hasn't responded for {_tr_units(m.group(1))} — {tr(m.group(2))}"),
    (_R(r"Не хватает места на диске: освободите ещё (.+)\. Скачивание на паузе и продолжится само, как только место появится\."),
     _u(lambda m: f"Not enough disk space: free up {m.group(1)} more. Downloading is paused and continues by itself once there's space.")),
    # длинные подсказки о связи
    (_R(r"(Нет связи с Telegram|Telegram долго не отвечает) — проверьте, что включён VPN или запущен прокси, и что на странице «Подключение» выбран нужный вид прокси\.(.*)"),
     lambda m: ("No connection to Telegram" if m.group(1).startswith("Нет") else "Telegram isn't responding")
        + " — check that the VPN is on or the proxy is running, and that the right proxy type is selected on the «Connection» page."
        + {"": "", " Программа продолжит сама, когда связь появится.": " The program will continue by itself once the connection is back.",
           " Ничего не потеряется: программа продолжит сама, когда связь появится.": " Nothing will be lost: the program continues by itself once the connection is back.",
           " Можно нажать «Отмена», поменять настройки и подключиться снова.": " You can press «Cancel», change the settings and connect again."}.get(m.group(2), tr(m.group(2)))),
    (_R(r"Нет связи с Telegram"), "No connection to Telegram"),
    (_R(r"Нет связи с Telegram\. Проверю снова через (\d+) с — продолжу сам, как только связь появится\."),
     r"No connection to Telegram. Checking again in \1 s — I'll continue once it's back."),
    (_R(r"Нет связи с Telegram — следующая проверка через (\d+) с \(или нажмите «Проверить снова»\)"),
     r"No connection to Telegram — next check in \1 s (or press «Check again»)"),
    (_R(r"Не удаётся связаться с Telegram \((.+)\)\. Похоже, нет интернета, выключен VPN или не запущен прокси\. Программа продолжит сама, когда связь появится\."),
     r"Can't reach Telegram (\1). Looks like there's no internet, the VPN is off or the proxy isn't running. The program will continue by itself once it's back."),
    (_R(r"Не хватает места на диске — освободите ещё (.+), скачивание продолжится само\."),
     _u(lambda m: f"Not enough disk space — free up {m.group(1)} more, downloading will continue by itself.")),
    (_R(r"Не хватает места на диске: нужно ещё (.+)\. Скачивание на паузе — освободите место, и оно продолжится само\."),
     _u(lambda m: f"Not enough disk space: {m.group(1)} more needed. Downloading is paused — free up space and it continues by itself.")),
    # журнал экспорта
    (_R(r"Сообщений в чате: ([\d ]+)"), r"Messages in chat: \1"),
    (_R(r"Сканирую историю: (\d+)( из \d+)?( · закончу ≈ (.+))?\.\.\."),
     _u(lambda m: f"Scanning history: {m.group(1)}" + (m.group(2) or "").replace(" из ", " of ")
        + (f" · done ≈ {m.group(4)}" if m.group(3) else "") + "...")),
    (_R(r"Сканирование: (\d+) из (\d+)\. Закончится примерно (.+) \(осталось (.+), с учётом пауз Telegram\)\."),
     _u(lambda m: f"Scanning: {m.group(1)} of {m.group(2)}. Will finish around {m.group(3)} ({m.group(4)} left, incl. Telegram pauses).")),
    (_R(r"Сканирование завершено: (\d+) сообщений\."), r"Scan finished: \1 messages."),
    (_R(r"Сохранённые версии сканирования: (\d+)…"), r"Saved scan versions: \1…"),
    (_R(r"Подключение \((.+)\)\.\.\."), r"Connecting (\1)..."),
    (_R(r"Подключение… \((.+)\)"), r"Connecting… (\1)"),
    (_R(r"Подключено: (.+) \((без прокси|MTProto .+|SOCKS5 .+)\)"), r"Connected: \1 (\2)"),
    (_R(r"Настройки прокси изменены, но ещё не применены \(сейчас подключение: без прокси\)\. Нажмите «Переподключиться»\."),
     r"Proxy settings changed but not applied yet (current connection: no proxy). Press “Reconnect”."),
    (_R(r"Настройки прокси изменены, но ещё не применены \(сейчас подключение: (.+)\)\. Нажмите «Переподключиться»\."),
     r"Proxy settings changed but not applied yet (current connection: \1). Press “Reconnect”."),
    (_R(r"Подключение \(без прокси\)\.\.\."), r"Connecting (no proxy)..."),
    (_R(r"Подключение… \(без прокси\)"), r"Connecting… (no proxy)"),
    (_R(r"Подключено: (.+) \(без прокси\)"), r"Connected: \1 (no proxy)"),
    (_R(r"Шаг 2: прокси \((.+)\)\.\n\nТеперь ВЫКЛЮЧИТЕ VPN и запустите прокси, затем нажмите «ОК»\."),
     r"Step 2: proxy (\1).\n\nNow TURN OFF the VPN and start the proxy, then press OK."),
    (_R(r"Собрать экспорт из версии от (.+)\?\n\nHTML и JSON в папке чата будут пересобраны по этой версии\. Сама версия и остальные версии не изменятся\."),
     r"Build an export from the version of \1?\n\nHTML and JSON in the chat folder will be rebuilt from this version. The version itself and other versions stay unchanged."),
    (_R(r"(.+) · последняя"), r"\1 · latest"),
    (_R(r"Просканировано сообщений: (\d+)"), r"Messages scanned: \1"),
    (_R(r"Просканирован: (.+)"), r"Scanned: \1"),
    (_R(r"История уже просканирована \((\d+) сообщений\) — проверяю только новые\."),
     r"History already scanned (\1 messages) — checking new ones only."),
    (_R(r"Продолжаю сканирование с места остановки: уже (\d+) сообщений( из \d+)?\."),
     lambda m: f"Continuing the scan where it stopped: {m.group(1)} messages already" + (m.group(2) or "").replace(" из ", " of ") + "."),
    (_R(r"Сохранена версия сканирования от (.+) — из неё можно сделать экспорт в любой момент \(поле «Режим» на странице «Экспорт»\)\."),
     r"Scan version of \1 saved — you can export from it any time («Mode» field on the «Export» page)."),
    (_R(r"Сохранена версия от (.+) \(до пересканирования\)\."), r"Version of \1 saved (before rescanning)."),
    (_R(r"Полное пересканирование: .+"), "Full rescan: the whole history is read again — this makes a new version (with edits and without deleted messages). Downloaded files are not fetched again."),
    (_R(r"Экспорт из сохранённой версии от (.+): (\d+) сообщений\..+"),
     r"Exporting from the saved version of \1: \2 messages. History isn't read from Telegram again; files are downloaded for this version (files of messages deleted since can't be fetched)."),
    (_R(r"(.+): (\d+) шт\., (.+) \(осталось (\d+), (.+)\)"),
     _u(lambda m: f"{tr(m.group(1))}: {m.group(2)} pcs, {m.group(3)} ({m.group(4)} left, {m.group(5)})")),
    (_R(r"Больше лимита (\d+) МБ \(пропускаются\): (\d+) шт\."), r"Over the \1 MB limit (skipped): \2 pcs"),
    (_R(r"Уже скачано ранее: (\d+) файлов"), r"Already downloaded: \1 files"),
    (_R(r"Осталось скачать: (\d+) файлов, (.+)"), _u(lambda m: f"Left to download: {m.group(1)} files, {m.group(2)}")),
    (_R(r"Недокачанных файлов: (\d+) \((.+) уже скачано\) — будут докачаны с места обрыва"),
     _u(lambda m: f"Partly downloaded files: {m.group(1)} ({m.group(2)} already) — they'll resume where they stopped")),
    (_R(r"Свободно на диске: (.+)\.?"), _u(lambda m: f"Free disk space: {m.group(1)}")),
    (_R(r"Места хватает \(останется ~(.+)\)\."), _u(lambda m: f"Enough space (~{m.group(1)} will remain).")),
    (_R(r"⚠ МЕСТА НЕ ХВАТИТ: нужно ещё ~(.+)\. .+"),
     _u(lambda m: f"⚠ NOT ENOUGH SPACE: ~{m.group(1)} more needed. Downloading will start; when space runs out it pauses and continues by itself once you free up space.")),
    (_R(r"Скачать файлы этого чата\?"), "Download this chat's files?"),
    (_R(r"«Нет» — пропустить файлы этого чата \(тексты и HTML сохранятся, файлы можно докачать позже\)\."),
     "«No» — skip this chat's files (texts and HTML are kept, files can be downloaded later)."),
    (_R(r"(.+) — (.+) за (.+) \((.+)/с(, докачан с (.+))?\)"),
     _u(lambda m: f"{m.group(1)} — {m.group(2)} in {m.group(3)} ({m.group(4)}/s" + (f", resumed from {m.group(6)}" if m.group(5) else "") + ")")),
    (_R(r"Пропущен файл \(сообщение (.+), (.+)\): (.+)"), _u(lambda m: f"File skipped (message {m.group(1)}, {m.group(2)}): {tr(m.group(3))}")),
    (_R(r"Повтор (\d+)/(\d+) для сообщения (.+) \(продолжу с (.+)\): (.+)"),
     _u(lambda m: f"Retry {m.group(1)}/{m.group(2)} for message {m.group(3)} (resuming from {m.group(4)}): {tr(m.group(5))}")),
    (_R(r"сообщение удалено или файл больше недоступен"), "message deleted or file no longer available"),
    (_R(r"нет прогресса (\d+) с"), r"no progress for \1 s"),
    (_R(r"HTML: (\d+) стр\. → (.+)"), r"HTML: \1 pages → \2"), (_R(r"JSON → (.+)"), r"JSON → \1"),
    (_R(r"Готово\. Сообщений: (\d+)\. Файлов на диске: (\d+) из (\d+)\."), r"Done. Messages: \1. Files on disk: \2 of \3."),
    (_R(r"• (.+): (\d+)"), lambda m: f"• {tr(m.group(1))}: {m.group(2)}"),
    (_R(r"не удалось скачать \(в failed\.txt\)"), "failed to download (see failed.txt)"),
    (_R(r"не хватило места"), "not enough space"), (_R(r"не найдено на диске"), "not found on disk"),
    (_R(r"больше лимита (\d+) МБ"), r"over the \1 MB limit"), (_R(r"тип отключён в настройках"), "type disabled in settings"),
    (_R(r"Завершено: (.+), время работы: (.+)"), r"Finished: \1, running time: \2"),
    (_R(r"Завершено: (.+)"), r"Finished: \1"), (_R(r"Время работы: (.+?)( \(автоперезапусков после сбоев: (\d+)\))?"),
                                               lambda m: f"Running time: {m.group(1)}" + (f" (auto-restarts after failures: {m.group(3)})" if m.group(2) else "")),
    (_R(r"Скачано за этот запуск: (.+?)(, средняя скорость (.+))?"),
     _u(lambda m: f"Downloaded this run: {m.group(1)}" + (f", average speed {m.group(3)}" if m.group(2) else ""))),
    (_R(r"Сообщений: (\d+) \(обычных (\d+), служебных (\d+), с реакциями (\d+)\)"),
     r"Messages: \1 (regular \2, service \3, with reactions \4)"),
    (_R(r"Период: (.+)"), r"Period: \1"), (_R(r"Файлов на диске: (\d+) из (\d+)"), r"Files on disk: \1 of \2"),
    (_R(r"Размер папки экспорта: (.+), свободно на диске: (.+)"), _u(lambda m: f"Export folder size: {m.group(1)}, free disk space: {m.group(2)}")),
    (_R(r"(.+): (\d+) из (\d+) скачано \((.+)\)( — (.+))?"),
     _u(lambda m: f"{tr(m.group(1))}: {m.group(2)} of {m.group(3)} downloaded ({m.group(4)})" + (f" — {tr(m.group(6))}" if m.group(5) else ""))),
    (_R(r"НЕ УДАЛОСЬ СКАЧАТЬ \((\d+)\)"), r"FAILED TO DOWNLOAD (\1)"),
    (_R(r"… и ещё (\d+) \(полный список в failed\.txt\)"), r"… and \1 more (full list in failed.txt)"),
    (_R(r"HTML: messages\.html \((\d+) стр\.\)"), r"HTML: messages.html (\1 pages)"),
    (_R(r"Кастомные эмодзи: (\d+)"), r"Custom emoji: \1"),
    (_R(r"Чатов выбрано: (\d+)"), r"Chats selected: \1"), (_R(r"полностью готовы: (\d+)"), r"complete: \1"),
    (_R(r"тексты сохранены, но не все файлы скачаны: (\d+)"), r"texts saved, but not all files downloaded: \1"),
    (_R(r"не удалось экспортировать: (\d+)"), r"failed to export: \1"),
    (_R(r"просканированы, файлы ещё не скачаны: (\d+)"), r"scanned, files not downloaded yet: \1"),
    (_R(r"(.+) — файлов (\d+) из (\d+)"), r"\1 — files \2 of \3"),
    # экспорт нескольких чатов
    (_R(r"Экспорт (?:чатов|всех чатов): выбрано (\d+)(, уже готовы и пропускаются: (\d+))?, к экспорту: (\d+)(.*)"),
     lambda m: f"Exporting chats: selected {m.group(1)}" + (f", already complete and skipped: {m.group(3)}" if m.group(2) else "")
        + f", to export: {m.group(4)}" + m.group(5).replace("; порядок: сначала сканировать все, потом скачивать", "; order: scan all first, then download")
        .replace("; порядок: по очереди", "; order: one by one").replace("; перед скачиванием — сводка", "; summary before downloading")),
    (_R(r"(Чат|Сканирование чата|Скачивание чата) (\d+) из (\d+)( \(повтор\))?: (.+) \[(.+)\]"),
     lambda m: f"{dict(zip(('Чат', 'Сканирование чата', 'Скачивание чата'), ('Chat', 'Scanning chat', 'Downloading chat')))[m.group(1)]} "
        f"{m.group(2)} of {m.group(3)}" + (" (retry)" if m.group(4) else "") + f": {m.group(5)} [{tr(m.group(6))}]"),
    (_R(r"(.+): сообщений (\d+), файлов (\d+) из (\d+)"), r"\1: messages \2, files \3 of \4"),
    (_R(r"(.+): сообщений (\d+), к скачиванию файлов (\d+) \((.+)\)"), _u(lambda m: f"{m.group(1)}: messages {m.group(2)}, files to download {m.group(3)} ({m.group(4)})")),
    (_R(r"(.+): не удалось \((.+)\)\. Перехожу к следующему чату(\.| — в конце попробую этот чат ещё раз\.)"),
     lambda m: f"{m.group(1)}: failed ({m.group(2)}). Moving on to the next chat" + ("." if m.group(3) == "." else " — I'll retry this chat at the end.")),
    (_R(r"Повторяю чаты, где были сбои: (\d+)"), r"Retrying chats that failed: \1"),
    (_R(r"Все чаты просканированы: (\d+) из (\d+)\."), r"All chats scanned: \1 of \2."),
    (_R(r"К скачиванию: (\d+) файлов, (.+)\."), _u(lambda m: f"To download: {m.group(1)} files, {m.group(2)}.")),
    (_R(r"(.+): (\d+) файлов, (.+)"), _u(lambda m: f"{m.group(1)}: {m.group(2)} files, {m.group(3)}")),
    (_R(r"⚠ Места не хватит: нужно ещё ~(.+?)\..+"),
     _u(lambda m: f"⚠ Not enough space: ~{m.group(1)} more needed. Downloading starts; when space runs out it pauses until you free some up.")),
    # вход и прочее
    (_R(r"✅ Вход выполнен: (.+)"), r"✅ Logged in: \1"), (_R(r"Найдено чатов: (\d+)"), r"Chats found: \1"),
    (_R(r"Подключение через MTProto-прокси (.+)"), r"Connecting via MTProto proxy \1"),
    (_R(r"Подключение через SOCKS5 (.+)"), r"Connecting via SOCKS5 \1"),
    (_R(r"Код отправлен: (.+)"), lambda m: f"Code sent: {tr(m.group(1))}"),
    (_R(r"Код отправлен (.+?)\.( Если не придёт.+)?"), lambda m: f"Code sent {tr(m.group(1))}." + (" If it doesn't arrive, you can request it again." if m.group(2) else "")),
    (_R(r"Запрашиваю код для (.+)\.\.\."), r"Requesting a code for \1..."),
    (_R(r"Пароль не подошёл: (.+)"), r"Wrong password: \1"),
    (_R(r"в приложение Telegram на другом вашем устройстве.*"), "to the Telegram app on your other device (the «Telegram» chat with a blue check), not by SMS"),
    (_R(r"по SMS"), "by SMS"), (_R(r"на вашу почту, привязанную к Telegram"), "to the email linked to Telegram"),
    (_R(r"ℹ Режим экспорта Telegram пока недоступен: в целях безопасности Telegram разрешит его с (.+?) \(.+"),
     r"ℹ Telegram export mode isn't available yet: for security Telegram will allow it from \1 (usually a day after logging in on a new device). Exporting in normal mode — Telegram will pause briefly every ~3000 messages."),
    (_R(r"ℹ Режим экспорта Telegram не включился \((.+)\)\. Экспорт идёт в обычном режиме\."),
     r"ℹ Telegram export mode didn't turn on (\1). Exporting in normal mode."),
    (_R(r"⏸ Telegram попросил паузу (\d+) с \(ограничение частоты запросов\)\. Программа подождёт и продолжит сама\."),
     r"⏸ Telegram asked for a \1 s pause (rate limit). The program will wait and continue by itself."),
    (_R(r"⏸ Пауза по требованию Telegram: (\d+) с — ограничение частоты запросов, это нормально"),
     r"⏸ Pause requested by Telegram: \1 s — rate limit, this is normal"),
    (_R(r"⏸ Пауза по требованию Telegram: (\d+) с"), r"⏸ Pause requested by Telegram: \1 s"),
    (_R(r"⏸ Telegram просит подождать (\d+) с.*"), r"⏸ Telegram asks to wait \1 s — waiting and continuing from the same place."),
    (_R(r"⚠ Сбой: (.+?) — похоже, пропала связь с Telegram\."), r"⚠ Failure: \1 — looks like the connection to Telegram was lost."),
    (_R(r"⚠ Сбой: (.+)"), r"⚠ Failure: \1"),
    (_R(r"Автоматически продолжу через (\d+) с \(попытка (\d+) из (\d+)\)\. Уже скачанное не потеряется\."),
     r"Will continue automatically in \1 s (attempt \2 of \3). Nothing downloaded will be lost."),
    (_R(r"Сбой — повтор через (\d+) с\.\.\."), r"Failure — retrying in \1 s..."),
    (_R(r"❌ Ошибка: (.+)"), r"❌ Error: \1"),
    (_R(r"❌ Не удалось создать (HTML|JSON): (.+)"), r"❌ Couldn't build \1: \2"),
    (_R(r"⚠ Сообщение (.+) разобрано не полностью \((.+)\) — сохраняю текст и иду дальше\."),
     r"⚠ Message \1 was only partly parsed (\2) — keeping its text and moving on."),
    (_R(r"Не удалось сохранить (.+): (.+)"), r"Couldn't save \1: \2"),
    (_R(r"Проверка не удалась: (.+)"), r"Test failed: \1"),
    (_R(r"В поле «(.+)» должно быть целое число\."), lambda m: f"The «{tr(m.group(1))}» field must be a whole number."),
    (_R(r"Продолжить экспорт выбранных чатов \((\d+)\) по очереди\?"), r"Continue exporting the selected chats (\1) one by one?"),
    (_R(r"Продолжить все незавершённые чаты \((\d+)\) по очереди\?"), r"Continue all unfinished chats (\1) one by one?"),
    (_R(r"Сохранённая версия «(.+)» не найдена в папке (.+)\."), r"Saved version «\1» wasn't found in \2."),
])

# ----------------------------- дополнения: ошибки, причины, вход -----------------------------
EN.update({
    "(без названия)": "(untitled)", "Удалённый аккаунт": "Deleted account", "папка не выбрана": "no folder selected",
    "ошибка": "error", "файл недоступен": "file unavailable",
    "файл скачан не полностью, будет докачан": "file downloaded only partly, will be resumed",
    "размер файла не совпал, будет докачан": "file size mismatch, will be resumed",
    "Для SOCKS5 нужен пакет python-socks: pip install python-socks[asyncio]":
        "SOCKS5 needs the python-socks package: pip install python-socks[asyncio]",
    "Не указан секрет прокси: вставьте ссылку tg://proxy... или секрет.": "Proxy secret is missing: paste a tg://proxy... link or the secret.",
    "Секрет начинается с «ee» — это режим Fake TLS, он не поддерживается.": "The secret starts with «ee» — that's Fake TLS mode, which isn't supported.",
    "Выключите Fake TLS в настройках tg-ws-proxy (нужен секрет с «dd») и скопируйте ссылку заново.":
        "Turn off Fake TLS in tg-ws-proxy settings (a «dd» secret is needed) and copy the link again.",
    "Пакет qrcode не установлен (pip install qrcode) — вход по QR недоступен, используется вход по номеру.":
        "The qrcode package isn't installed (pip install qrcode) — QR login is unavailable, using phone login.",
    "⚠ Ускоритель cryptg НЕ установлен — шифрование идёт медленно и может ограничивать скорость. Установите: pip install cryptg (или перезапустите через «Запуск.bat»).":
        "⚠ The cryptg accelerator is NOT installed — encryption is slow and may limit speed. Install it: pip install cryptg (or restart via «Запуск.bat»).",
    "Сначала подключитесь к Telegram (страница «Подключение»).": "Connect to Telegram first («Connection» page).",
    "голосовым звонком": "by a voice call", "звонком (код — последние цифры номера)": "by a call (the code is the last digits of the number)",
    "пропущенным звонком (код — последние цифры номера звонившего)": "by a missed call (the code is the last digits of the caller's number)",
    "через Fragment (для анонимных номеров)": "via Fragment (for anonymous numbers)",
    "по SMS (кодовое слово)": "by SMS (code word)", "по SMS (кодовая фраза)": "by SMS (code phrase)",
    "— Telegram требует сначала привязать почту в официальном приложении": "— Telegram requires linking an email in the official app first",
    "в приложение Telegram на другом вашем устройстве — в чат «Telegram» (с синей галочкой), а не по SMS":
        "to the Telegram app on your other device — the «Telegram» chat (with a blue check), not by SMS",
    "Нет": "No", "Да": "Yes",
})
EN_RE.extend([
    (_R(r"файл скачан не полностью \((.+) из (.+)\), будет докачан"), _u(lambda m: f"file downloaded only partly ({m.group(1)} of {m.group(2)}), will be resumed")),
    (_R(r"кусок (\d+) пришёл не полностью"), r"chunk \1 arrived incomplete"),
    (_R(r"Повторно запросить код не удалось: (.+)"), r"Couldn't request the code again: \1"),
    (_R(r"Не удалось получить кастомные эмодзи: (.+)"), r"Couldn't get custom emoji: \1"),
    (_R(r"Не удалось проверить старую историю группы: (.+)"), r"Couldn't check the group's earlier history: \1"),
    (_R(r"Не удалось сохранить версию сканирования: (.+)"), r"Couldn't save the scan version: \1"),
    (_R(r"Не удалось сохранить текущую версию перед пересканированием: (.+)"), r"Couldn't save the current version before rescanning: \1"),
    (_R(r"Не удалось сохранить прогресс сканирования: (.+)"), r"Couldn't save scan progress: \1"),
    (_R(r"Не удалось сохранить настройки: (.+)"), r"Couldn't save settings: \1"),
    (_R(r"Не удалось сохранить состояние: (.+)"), r"Couldn't save state: \1"),
    (_R(r"Переподключиться пока не удалось: (.+)"), r"Couldn't reconnect yet: \1"),
    (_R(r"Не удалось связаться.*"), "Couldn't reach Telegram."),
])

EN_RE.extend([
    (_R(r"([^:\s][^:]*): (\d+)"), lambda m: f"{tr(m.group(1))}: {m.group(2)}"),
])

EN.update({
    "Режим экспорта Telegram": "Telegram export mode", "Подробнее": "Details", "Подробнее…": "Details…", "Скрыть": "Hide",
    "Проверить сейчас": "Check now", "Проверяю…": "Checking…",
    "⚡ Режим экспорта Telegram включён — при сканировании истории меньше пауз.":
        "⚡ Telegram export mode is on — fewer pauses while scanning history.",
    "✅ Срок ожидания режима экспорта Telegram прошёл — он включится сам при следующем экспорте. Можно проверить сейчас.":
        "✅ The waiting period for Telegram export mode is over — it turns on by itself with the next export. You can check now.",
    "Режим экспорта Telegram ещё не проверялся — программа попробует включить его при первом экспорте.":
        "Telegram export mode hasn't been checked yet — the program will try to turn it on with the first export.",
    "Плашку можно вернуть: сведения о режиме экспорта — на странице «Подключение».":
        "The notice is hidden: export mode details are always on the «Connection» page.",
    "Это тот же режим, которым пользуется Telegram Desktop при «Экспорте данных». В нём Telegram мягче ограничивает частоту запросов, поэтому при чтении истории (сканировании) меньше пауз.":
        "This is the same mode Telegram Desktop uses for «Export data». In it Telegram limits request rate less strictly, so there are fewer pauses while reading (scanning) history.",
    "Когда программа впервые просит этот режим, Telegram в целях безопасности откладывает его примерно на сутки после входа на новом устройстве и присылает на ваши другие устройства уведомление о запросе экспорта. Это запрос этой программы — подтверждение в Telegram и есть то самое разрешение.":
        "When the program first asks for this mode, Telegram, for security, delays it for about a day after logging in on a new device and sends an export request notification to your other devices. That's this program's request — confirming it in Telegram is the permission.",
    "Программа сама включает режим при каждом запуске экспорта. Пока Telegram его не разрешил, экспорт идёт в обычном режиме — ничего делать не нужно.":
        "The program turns the mode on by itself every time an export starts. Until Telegram allows it, the export runs in normal mode — nothing to do.",
    "На скорость скачивания файлов режим не влияет — только на паузы при сканировании истории.":
        "The mode doesn't affect file download speed — only the pauses while scanning history.",
})
EN_RE.extend([
    (_R(r"ℹ Режим экспорта Telegram станет доступен (.+)\. До этого экспорт идёт в обычном режиме \(Telegram делает короткие паузы при сканировании\)\."),
     r"ℹ Telegram export mode becomes available on \1. Until then the export runs in normal mode (Telegram makes short pauses while scanning)."),
    (_R(r"ℹ Режим экспорта Telegram не включился: (.*)\. Экспорт идёт в обычном режиме\."),
     r"ℹ Telegram export mode didn't turn on: \1. Exporting in normal mode."),
])


def _literal_len(rx):
    return len(re.sub(r"\(.*?\)|\\.|[.+*?\[\]^$|{}]", "", rx.pattern))


EN_RE.sort(key=lambda p: -_literal_len(p[0]))     # сначала самые конкретные шаблоны, общие «… из …» — в конце


# =================================== оформление ===================================
# Своя дизайн-система поверх встроенной темы ttk «clam»: всё рисуется цветами и шрифтами
# (без картинок), поэтому интерфейс чётко масштабируется при 125–200% и не требует новых пакетов.

PALETTES = {
    "light": dict(
        bg="#FFFFFF", side="#F4F5F7", field="#FFFFFF", hover="#EDEFF2", border="#E1E4E8",
        border_strong="#C8CDD4", text="#1F2328", muted="#646B76", faint="#9AA1AB",
        accent="#2F6FDE", accent_hover="#2560C7", accent_text="#FFFFFF", accent_soft="#E5EDFB",
        success="#1E8449", warning="#9A6410", danger="#C73B3B", track="#E6E8EB"),
    "dark": dict(
        bg="#1E1F22", side="#18191B", field="#26282C", hover="#2E3035", border="#33363B",
        border_strong="#474B53", text="#E6E7E9", muted="#9CA2AB", faint="#6B717A",
        accent="#4C8BF5", accent_hover="#6A9FF7", accent_text="#FFFFFF", accent_soft="#22344E",
        success="#46B675", warning="#D6A23F", danger="#E3625C", track="#34373C"),
}


def system_theme():
    """Тема Windows для приложений: светлая или тёмная."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return "light" if winreg.QueryValueEx(k, "AppsUseLightTheme")[0] else "dark"
    except Exception:
        return "light"


def set_titlebar_theme(win, dark):
    """Тёмный/светлый заголовок окна Windows 10/11 — чтобы он не выбивался из темы."""
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(val), ctypes.sizeof(val)) == 0:
                break
    except Exception:
        pass


def plain(text):
    """Текст без ведущих эмодзи и значков — для таблиц и заголовков."""
    return re.sub(r"^[^\w«(\"'№]+", "", text or "").strip()


class UI:
    """Палитра, шрифты, размеры и стили всех компонентов."""

    def __init__(self, root):
        import tkinter.font as tkfont
        self.root = root
        try:
            self.k = float(root.winfo_fpixels("1i")) / 96.0
        except Exception:
            self.k = 1.0
        self.style = ttk.Style(root)
        self.style.theme_use("clam")
        fams = set(tkfont.families(root))
        base = next((f for f in ("Segoe UI", "Helvetica Neue", "Helvetica", "Arial", "DejaVu Sans") if f in fams),
                    "TkDefaultFont")
        semi = next((f for f in ("Segoe UI Semibold",) if f in fams), None)
        mono = next((f for f in ("Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New")
                     if f in fams), "TkFixedFont")

        def font(size, bold=False):
            if bold and semi:
                return tkfont.Font(root, family=semi, size=size)
            return tkfont.Font(root, family=base, size=size, weight="bold" if bold else "normal")

        self.f_body = font(10)
        self.f_body_b = font(10, True)
        self.f_small = font(9)
        self.f_small_b = font(9, True)
        self.f_h2 = font(12, True)
        self.f_title = font(17, True)
        self.f_mono = tkfont.Font(root, family=mono, size=9)
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont",
                     "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name).configure(family=base, size=10 if name != "TkSmallCaptionFont" else 9)
            except Exception:
                pass
        self.row_h = int(self.f_body.metrics("linespace") * 1.95)
        self.mode = "light"
        self.c = PALETTES["light"]
        self._imgs = {}
        self._elements_made = False

    def s(self, px):
        """Пиксели дизайна (при 100%) → реальные пиксели экрана."""
        return max(1, int(round(px * self.k)))

    # ---- элементы, нарисованные кодом (галочка, стрелка списка, ползунок) ----
    # Рисуются сразу в нужном масштабе со сглаживанием — чётко при любом масштабе экрана, без файлов картинок.
    @staticmethod
    def _rgb(hexcol):
        return tuple(int(hexcol[i:i + 2], 16) for i in (1, 3, 5))

    @staticmethod
    def _cov(d):
        return 0.0 if d >= 0.5 else (1.0 if d <= -0.5 else 0.5 - d)

    @staticmethod
    def _rrect(cx, cy, hw, hh, r, inset=0.0):
        """Расстояние до скруглённого прямоугольника (отрицательное — внутри)."""
        hw, hh, r = hw - inset, hh - inset, max(0.0, r - inset)

        def d(x, y):
            qx, qy = abs(x - cx) - (hw - r), abs(y - cy) - (hh - r)
            ox, oy = max(qx, 0.0), max(qy, 0.0)
            return (ox * ox + oy * oy) ** 0.5 + min(max(qx, qy), 0.0) - r
        return d

    @staticmethod
    def _polyline(pts, width):
        """Расстояние до ломаной линии заданной толщины."""
        segs = list(zip(pts, pts[1:]))

        def d(x, y):
            best = 1e9
            for (ax, ay), (bx, by) in segs:
                vx, vy, wx, wy = bx - ax, by - ay, x - ax, y - ay
                t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy)))
                dx, dy = wx - t * vx, wy - t * vy
                best = min(best, (dx * dx + dy * dy) ** 0.5)
            return best - width / 2
        return d

    def _paint(self, name, w, h, bg, layers, transparent=False):
        """layers: [(цвет, функция-расстояние)] поверх фона bg. Картинка переиспользуется при смене темы."""
        img = self._imgs.get(name)
        if img is None:
            img = self._imgs[name] = tk.PhotoImage(master=self.root, width=w, height=h)
        base = self._rgb(bg)
        cols = [(self._rgb(c), f) for c, f in layers]
        rows, clear = [], []
        for y in range(h):
            row = []
            for x in range(w):
                r, g, b = base
                px, py = x + 0.5, y + 0.5
                hit = False
                for (cr, cg, cb), f in cols:
                    a = self._cov(f(px, py))
                    if a:
                        hit = True
                        r, g, b = r + (cr - r) * a, g + (cg - g) * a, b + (cb - b) * a
                if transparent and not hit:
                    clear.append((x, y))
                row.append("#%02x%02x%02x" % (int(r + 0.5), int(g + 0.5), int(b + 0.5)))
            rows.append("{" + " ".join(row) + "}")
        img.put(" ".join(rows), to=(0, 0))
        for x, y in clear:
            img.transparency_set(x, y, True)
        return img

    def _draw_elements(self):
        c, k = self.c, self.k
        # флажок: квадрат со скруглением, при выборе — заливка акцентом и белая галочка
        sz, gap = self.s(16), self.s(8)
        half, rad, bw = sz / 2 - 0.5, 3.5 * k, max(1.0, 1.0 * k)
        cx = cy = sz / 2
        box = self._rrect(cx, cy, half, half, rad)
        inner = self._rrect(cx, cy, half, half, rad, inset=bw)
        tick = self._polyline([(sz * .26, sz * .52), (sz * .43, sz * .69), (sz * .75, sz * .34)], 1.9 * k)

        def check(name, border, fill, mark=None):
            layers = [(border, box), (fill, inner)]
            if mark:
                layers.append((mark, lambda x, y: max(tick(x, y), inner(x, y))))
            return self._paint(name, sz + gap, sz, c["bg"], layers)
        imgs = {
            "off": check("cb_off", c["border_strong"], c["field"]),
            "off_h": check("cb_off_h", c["accent"], c["field"]),
            "on": check("cb_on", c["accent"], c["accent"], c["accent_text"]),
            "on_h": check("cb_on_h", c["accent_hover"], c["accent_hover"], c["accent_text"]),
            "off_d": check("cb_off_d", c["border"], c["bg"]),
            "on_d": check("cb_on_d", c["border_strong"], c["border_strong"], c["bg"]),
        }
        # флажки для строк таблицы (фон прозрачный)
        self.tree_cb = {
            "off": self._paint("tcb_off", sz, sz, c["bg"], [(c["border_strong"], box), (c["field"], inner)], True),
            "on": self._paint("tcb_on", sz, sz, c["bg"], [(c["accent"], box), (c["accent"], inner),
                                                          (c["accent_text"], lambda x, y: max(tick(x, y), inner(x, y)))],
                              True),
        }
        # стрелка выпадающего списка
        aw, ah = self.s(28), self.s(14)
        ax, ay = aw - self.s(13), ah / 2
        chev = self._polyline([(ax - 4.2 * k, ay - 2.1 * k), (ax, ay + 2.1 * k), (ax + 4.2 * k, ay - 2.1 * k)], 1.5 * k)
        arrow = self._paint("cb_arrow", aw, ah, c["field"], [(c["muted"], chev)])
        arrow_d = self._paint("cb_arrow_d", aw, ah, c["bg"], [(c["faint"], chev)])
        # ползунок: тонкая дорожка и круглая ручка
        kd = self.s(16)
        knob_f = self._rrect(kd / 2, kd / 2, kd / 2 - 0.5, kd / 2 - 0.5, kd / 2)
        knob = self._paint("sc_knob", kd, kd, c["bg"], [(c["accent"], knob_f)])
        knob_h = self._paint("sc_knob_h", kd, kd, c["bg"], [(c["accent_hover"], knob_f)])
        tw = self.s(16)
        trough = self._paint("sc_trough", tw, kd, c["bg"],
                             [(c["track"], self._rrect(tw / 2, kd / 2, tw / 2 - 0.5, 2.0 * k, 2.0 * k))])

        if self._elements_made:
            return
        self._elements_made = True
        st = self.style
        st.element_create("Ui.Checkbutton.indicator", "image", imgs["off"],
                          ("disabled", "selected", imgs["on_d"]), ("disabled", imgs["off_d"]),
                          ("selected", "active", imgs["on_h"]), ("selected", imgs["on"]),
                          ("active", imgs["off_h"]), sticky="")
        st.element_create("Ui.Combobox.downarrow", "image", arrow, ("disabled", arrow_d), sticky="")
        st.element_create("Ui.Scale.slider", "image", knob, ("active", knob_h), ("pressed", knob_h), sticky="")
        st.element_create("Ui.Scale.trough", "image", trough, border=(self.s(7), 0), sticky="ew")
        for name in ("TCheckbutton", "Side.TCheckbutton"):
            st.layout(name, [("Checkbutton.padding", {"sticky": "nswe", "children": [
                ("Ui.Checkbutton.indicator", {"side": "left", "sticky": ""}),
                ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                    ("Checkbutton.label", {"sticky": "nswe"})]})]})])
        st.layout("TCombobox", [("Combobox.field", {"sticky": "nswe", "children": [
            ("Ui.Combobox.downarrow", {"side": "right", "sticky": "ns"}),
            ("Combobox.padding", {"expand": "1", "sticky": "nswe", "children": [
                ("Combobox.textarea", {"sticky": "nswe"})]})]})])
        st.layout("Horizontal.TScale", [("Ui.Scale.trough", {"sticky": "nswe", "children": [
            ("Ui.Scale.slider", {"side": "left", "sticky": ""})]})])

    def apply(self, mode):
        self.mode = mode if mode in PALETTES else "light"
        c = self.c = PALETTES[self.mode]
        S, st, r = self.s, self.style, self.root
        r.configure(background=c["bg"])
        st.configure(".", background=c["bg"], foreground=c["text"], bordercolor=c["border"],
                     lightcolor=c["bg"], darkcolor=c["bg"], troughcolor=c["track"], focuscolor=c["accent"],
                     selectbackground=c["accent_soft"], selectforeground=c["text"], fieldbackground=c["field"],
                     insertcolor=c["text"], font=self.f_body, relief="flat", borderwidth=1)
        st.map(".", foreground=[("disabled", c["faint"])])

        # поверхности и текст
        for pre, bg in (("", c["bg"]), ("Side.", c["side"])):
            st.configure(f"{pre}TFrame", background=bg)
            st.configure(f"{pre}TLabel", background=bg, foreground=c["text"], font=self.f_body)
            st.configure(f"{pre}Muted.TLabel", background=bg, foreground=c["muted"], font=self.f_body)
            st.configure(f"{pre}Small.TLabel", background=bg, foreground=c["muted"], font=self.f_small)
            st.configure(f"{pre}Value.TLabel", background=bg, foreground=c["text"], font=self.f_body_b)
            st.configure(f"{pre}Section.TLabel", background=bg, foreground=c["text"], font=self.f_body_b)
            st.configure(f"{pre}Link.TButton", background=bg, foreground=c["accent"], bordercolor=bg,
                         lightcolor=bg, darkcolor=bg, padding=(S(6), S(4)), font=self.f_body, focusthickness=1)
            st.map(f"{pre}Link.TButton", background=[("active", bg), ("pressed", bg)],
                   lightcolor=[("active", bg)], darkcolor=[("active", bg)],
                   foreground=[("disabled", c["faint"]), ("active", c["accent_hover"])],
                   bordercolor=[("focus", c["accent"]), ("!focus", bg)])
            st.configure(f"{pre}TCheckbutton", background=bg, foreground=c["text"], font=self.f_body,
                         indicatorbackground=c["field"], indicatorforeground=c["accent_text"],
                         upperbordercolor=c["border_strong"], lowerbordercolor=c["border_strong"],
                         indicatorsize=S(14), indicatormargin=(0, 0, S(8), 0), padding=(0, S(3)),
                         focusthickness=1)
            st.map(f"{pre}TCheckbutton", background=[("active", bg)],
                   indicatorbackground=[("selected", "disabled", c["border_strong"]), ("selected", c["accent"]),
                                        ("active", c["hover"])],
                   upperbordercolor=[("selected", c["accent"]), ("active", c["accent"])],
                   lowerbordercolor=[("selected", c["accent"]), ("active", c["accent"])],
                   foreground=[("disabled", c["faint"])])
        st.configure("Title.TLabel", font=self.f_title, foreground=c["text"])
        st.configure("H2.TLabel", font=self.f_h2, foreground=c["text"])
        st.configure("Success.TLabel", foreground=c["success"])
        st.configure("Warning.TLabel", foreground=c["warning"])
        st.configure("Danger.TLabel", foreground=c["danger"])
        st.configure("SuccessH2.TLabel", font=self.f_h2, foreground=c["success"])
        st.configure("WarningH2.TLabel", font=self.f_h2, foreground=c["warning"])
        st.configure("Brand.TLabel", background=c["side"], foreground=c["text"], font=self.f_h2)
        st.configure("DotOn.TLabel", background=c["side"], foreground=c["success"], font=self.f_small)
        st.configure("DotOff.TLabel", background=c["side"], foreground=c["faint"], font=self.f_small)
        st.configure("DotBusy.TLabel", background=c["side"], foreground=c["warning"], font=self.f_small)

        # кнопки: обычная, главная (акцент), опасная, навигация, сегменты
        pad = (S(14), S(6))

        def button(name, bg, fg, border, hover, pressed=None, fg_hover=None):
            st.configure(name, background=bg, foreground=fg, bordercolor=border, lightcolor=bg, darkcolor=bg,
                         padding=pad, font=self.f_body, focusthickness=1, focuscolor=c["accent"], anchor="center",
                         width=0)     # ширина по тексту (а не минимум в 11 символов, как в теме clam)
            st.map(name,
                   background=[("disabled", c["bg"] if bg != c["accent"] else c["track"]),
                               ("pressed", pressed or hover), ("active", hover)],
                   lightcolor=[("disabled", c["bg"] if bg != c["accent"] else c["track"]),
                               ("pressed", pressed or hover), ("active", hover)],
                   darkcolor=[("disabled", c["bg"] if bg != c["accent"] else c["track"]),
                              ("pressed", pressed or hover), ("active", hover)],
                   foreground=[("disabled", c["faint"])] + ([("active", fg_hover)] if fg_hover else []),
                   bordercolor=[("disabled", c["border"] if bg != c["accent"] else c["track"]),
                                ("focus", c["accent"])])
        button("TButton", c["field"], c["text"], c["border_strong"], c["hover"])
        button("Accent.TButton", c["accent"], c["accent_text"], c["accent"], c["accent_hover"])
        st.map("Accent.TButton", bordercolor=[("disabled", c["track"]), ("focus", c["text"]), ("!focus", c["accent"])])
        button("Danger.TButton", c["field"], c["danger"], c["border_strong"], c["hover"])

        st.configure("Nav.TButton", background=c["side"], foreground=c["text"], bordercolor=c["side"],
                     lightcolor=c["side"], darkcolor=c["side"], padding=(S(12), S(8)), anchor="w",
                     font=self.f_body, focusthickness=0)
        st.map("Nav.TButton",
               background=[("selected", c["accent_soft"]), ("active", c["hover"])],
               lightcolor=[("selected", c["accent_soft"]), ("active", c["hover"])],
               darkcolor=[("selected", c["accent_soft"]), ("active", c["hover"])],
               bordercolor=[("selected", c["accent_soft"]), ("active", c["hover"])],
               foreground=[("selected", c["accent"])])
        button("Seg.TButton", c["field"], c["muted"], c["border_strong"], c["hover"])
        st.configure("Seg.TButton", padding=(S(12), S(5)))
        st.map("Seg.TButton",
               background=[("selected", c["accent_soft"]), ("active", c["hover"])],
               lightcolor=[("selected", c["accent_soft"]), ("active", c["hover"])],
               darkcolor=[("selected", c["accent_soft"]), ("active", c["hover"])],
               bordercolor=[("selected", c["accent"]), ("focus", c["accent"])],
               foreground=[("selected", c["accent"]), ("disabled", c["faint"])])

        # поля ввода
        fpad = (S(8), S(5))
        for name in ("TEntry", "TCombobox", "TSpinbox"):
            st.configure(name, fieldbackground=c["field"], foreground=c["text"], background=c["field"],
                         bordercolor=c["border_strong"], lightcolor=c["field"], darkcolor=c["field"],
                         insertcolor=c["text"], padding=fpad, arrowcolor=c["muted"], arrowsize=S(12),
                         selectbackground=c["accent_soft"], selectforeground=c["text"])
            st.map(name,
                   bordercolor=[("focus", c["accent"]), ("hover", c["muted"])],
                   lightcolor=[("focus", c["field"])],
                   fieldbackground=[("readonly", c["field"]), ("disabled", c["bg"])],
                   foreground=[("disabled", c["faint"])],
                   background=[("active", c["hover"]), ("pressed", c["hover"])],
                   arrowcolor=[("disabled", c["faint"])],
                   selectbackground=[("readonly", c["field"])],
                   selectforeground=[("readonly", c["text"])])
        r.option_add("*TCombobox*Listbox.background", c["field"])
        r.option_add("*TCombobox*Listbox.foreground", c["text"])
        r.option_add("*TCombobox*Listbox.selectBackground", c["accent_soft"])
        r.option_add("*TCombobox*Listbox.selectForeground", c["text"])
        r.option_add("*TCombobox*Listbox.font", self.f_body)
        r.option_add("*TCombobox*Listbox.borderWidth", 0)

        # ползунок, прогресс, полосы прокрутки, разделители
        st.configure("Horizontal.TScale", background=c["accent"], troughcolor=c["track"], bordercolor=c["accent"],
                     lightcolor=c["accent"], darkcolor=c["accent"], sliderlength=S(16), gripcount=0)
        st.map("Horizontal.TScale", background=[("active", c["accent_hover"])])
        for name, trough in (("Horizontal.TProgressbar", c["track"]), ("Side.Horizontal.TProgressbar", c["track"])):
            st.configure(name, background=c["accent"], troughcolor=trough, bordercolor=trough,
                         lightcolor=c["accent"], darkcolor=c["accent"], thickness=S(6))
        for name in ("Vertical.TScrollbar", "Horizontal.TScrollbar"):
            st.configure(name, background=c["track"], troughcolor=c["bg"], bordercolor=c["bg"],
                         lightcolor=c["track"], darkcolor=c["track"], arrowcolor=c["muted"], gripcount=0,
                         arrowsize=S(12))
            st.map(name, background=[("active", c["border_strong"]), ("pressed", c["border_strong"])],
                   lightcolor=[("active", c["border_strong"])], darkcolor=[("active", c["border_strong"])])
        st.configure("TSeparator", background=c["border"])

        # таблицы
        st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
        st.configure("Treeview", background=c["bg"], fieldbackground=c["bg"], foreground=c["text"],
                     rowheight=self.row_h, borderwidth=0, font=self.f_body)
        st.map("Treeview", background=[("selected", "focus", c["accent_soft"]), ("selected", c["hover"])],
               foreground=[("selected", c["text"])])
        st.configure("Treeview.Heading", background=c["bg"], foreground=c["muted"], font=self.f_small_b,
                     bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"], relief="flat",
                     padding=(S(8), S(7)))
        st.map("Treeview.Heading", background=[("active", c["hover"])], lightcolor=[("active", c["hover"])])
        st.configure("Treeview.Heading", darkcolor=c["border"])
        st.configure("TPanedwindow", background=c["bg"])
        st.configure("Sash", sashthickness=S(10), gripcount=0, background=c["bg"], lightcolor=c["bg"],
                     darkcolor=c["bg"], bordercolor=c["bg"])
        self._draw_elements()


class Segmented(ttk.Frame):
    """Переключатель из нескольких кнопок (вместо радиокнопок): видно все варианты и выбранный."""

    def __init__(self, parent, var, options, command=None, style="TFrame", gap=4, k=1.0):
        super().__init__(parent, style=style)
        self.var, self.command, self.buttons = var, command, {}
        for i, (value, text) in enumerate(options):
            b = ttk.Button(self, text=text, style="Seg.TButton", command=lambda v=value: self.pick(v))
            b.pack(side="left", padx=(0 if i == 0 else max(1, int(gap * k)), 0))
            self.buttons[value] = b
        var.trace_add("write", lambda *a: self.refresh())
        self.refresh()

    def pick(self, value):
        self.var.set(value)
        if self.command:
            self.command()

    def refresh(self):
        cur = self.var.get()
        for v, b in self.buttons.items():
            b.state(["selected"] if v == cur else ["!selected"])


class App:
    NAV = (("export", "Экспорт"), ("exports", "Мои экспорты"), ("conn", "Подключение"), ("settings", "Настройки"))
    VERSION_NEW = "Продолжить с места остановки"
    VERSION_FULL = "Пересканировать всю историю заново"
    EX_FILTERS = ("Все", "Незавершённые", "Готовые")
    MAX_LOG_LINES = 3000  # в окне держим только последние строки; полный лог — в export.log
    STAT_NAMES = (("msgs", "Сообщения"), ("eta", "Осталось"), ("finish", "Завершится"),
                  ("bytes", "Скачано"), ("speed", "Скорость"), ("files", "Файлы"),
                  ("failed", "Пропущено"), ("chats", "Чаты"), ("disk", "Свободно на диске"))

    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.worker = Worker(self.q)
        self.cfg = load_cfg()
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                if "language" not in json.load(f):
                    self.cfg["language"] = "ru"      # программа уже стояла на русском — не меняем язык без спроса
        except (OSError, ValueError):
            pass
        set_language(self.cfg.get("language", "system"))
        install_i18n()
        self.dialogs = []
        self.dialog_map = {}
        self.out_dir = None
        self.samples = collections.deque()
        self.msg_samples = collections.deque()
        self.counts = {}
        self.busy = False
        self.stoppable = False
        self.job = None
        self.dl_eta = None
        self.ex_all_rows = []
        self.ex_rows = []
        self.ex_gen = 0
        self.ex_last = 0.0
        self.account = None
        self.connecting = False
        self.page = None
        self.version_ids = []
        self.themed = []     # не-ttk виджеты и функции их окраски (перекрашиваются при смене темы)
        self.combos = []
        self.trees = []
        self.report_win = None
        self.qr_win = None
        self.tree_cols = {}
        self._sash_done = False
        self._select_running = False
        self.speed_hist = collections.deque(maxlen=4000)    # (время, байт/с) — для графика скорости
        self.scan_hist = collections.deque(maxlen=4000)     # (время, сообщений/с)
        self.graph_mode = "download"
        self.live = None
        self.ex_running_iid = None
        self.ui = UI(root)
        self.S = self.ui.s
        self.load_counts()
        root.title("Telegram Chat Exporter")
        self._make_vars()
        self.ui.apply(self.theme_mode())
        self._build()
        self.apply_theme_tk()
        self._bind_keys()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._place_window()
        self.update_proxy_state()
        self.refresh_versions()
        self.update_chat_header()
        self.update_dest()
        self.update_account()
        self.fill_dialogs()
        self.update_activity()
        self.update_takeout_ui()
        self.root.after(200, self.poll)
        self.root.after(1000, self.tick)
        self.root.after(80, lambda: set_titlebar_theme(self.root, self.ui.mode == "dark"))
        has_creds = bool(self.cfg["api_id"] and self.cfg["api_hash"])
        if has_creds and os.path.exists(SESSION_PATH + ".session"):
            self.show_page("export")
            self.set_phase("Подключаюсь к Telegram…")
            self.log("Найдена сохранённая сессия — подключаюсь автоматически...")
            self.root.after(300, self.on_connect)
        else:
            self.show_page("conn")
            self.set_phase("Нажмите «Подключиться», чтобы войти в Telegram." if has_creds else
                           "Укажите API ID и API hash и нажмите «Подключиться».")

    # ---------- переменные (все настройки из tg_export_settings.json) ----------
    def _make_vars(self):
        c = self.cfg
        self.v_api_id = tk.StringVar(value=c["api_id"])
        self.v_api_hash = tk.StringVar(value=c["api_hash"])
        self.v_ptype = tk.StringVar(value=c["proxy_type"])
        self.v_plink = tk.StringVar(value=c["proxy_link"])
        self.v_phost = tk.StringVar(value=c["proxy_host"])
        self.v_pport = tk.StringVar(value=str(c["proxy_port"]))
        self.v_psecret = tk.StringVar(value=c["proxy_secret"])
        self.v_puser = tk.StringVar(value=c["proxy_user"])
        self.v_ppass = tk.StringVar(value=c["proxy_pass"])
        self.v_types = {k: tk.BooleanVar(value=c["types"].get(k, True)) for k in TYPE_LABELS}
        self.v_limit = tk.IntVar(value=int(c["size_limit_mb"]) or 4000)
        self.v_unlimited = tk.BooleanVar(value=int(c["size_limit_mb"] or 0) == 0)
        self.v_html = tk.BooleanVar(value=c["make_html"])
        self.v_json = tk.BooleanVar(value=c["make_json"])
        self.v_auto = tk.BooleanVar(value=c.get("auto_download", True))
        self.v_out = tk.StringVar(value=c["out_root"])
        self.v_parallel = tk.IntVar(value=int(c["parallel"]))
        self.v_threads = tk.IntVar(value=int(c.get("threads_per_file", 4)))
        self.v_stall = tk.IntVar(value=int(c["stall_timeout"]))
        self.v_retries = tk.IntVar(value=int(c["retries"]))
        self.v_chat = tk.StringVar(value=c.get("last_chat", ""))
        self.v_search = tk.StringVar()
        self.v_version = tk.StringVar()
        self.v_all_kinds = {k: tk.BooleanVar(value=c.get("all_kinds", {}).get(k, True))
                            for k in ("Личный", "Группа", "Канал")}
        self.v_all_skip = tk.BooleanVar(value=c.get("all_skip_done", True))
        self.v_theme = tk.StringVar(value=c.get("theme", "system"))
        self.v_language = tk.StringVar(value=c.get("language", "system"))
        self.v_multi_mode = tk.StringVar(value=c.get("multi_mode", "sequential"))
        self.v_details_tab = tk.StringVar(value=c.get("details_tab", "general"))
        self.v_graph_period = tk.StringVar(value=str(c.get("graph_period", "300")))
        self.v_ex_filter = tk.StringVar(value=self.EX_FILTERS[0])
        self.v_plink.trace_add("write", lambda *a: self.fill_from_link())
        for v in (self.v_ptype, self.v_plink, self.v_phost, self.v_pport, self.v_psecret, self.v_puser):
            v.trace_add("write", lambda *a: hasattr(self, "l_conn_hint") and self.root.after_idle(self.update_account))
        self.v_manual = tk.StringVar()      # ручной ввод: @username, ссылка или ID (если чата нет в списке)
        self.v_manual.trace_add("write", lambda *a: self.on_manual_chat())
        self.v_search.trace_add("write", lambda *a: self.fill_dialogs())
        self.v_chat.trace_add("write", lambda *a: (self.refresh_versions(), self.update_chat_header()))
        self.v_out.trace_add("write", lambda *a: (self.refresh_versions(), self.update_dest()))

    def theme_mode(self):
        t = self.v_theme.get() if hasattr(self, "v_theme") else self.cfg.get("theme", "system")
        return system_theme() if t == "system" else t

    # ---------- общие элементы ----------
    def theme_tk(self, widget, fn):
        """Регистрирует не-ttk виджет: fn(палитра) красит его сейчас и при каждой смене темы."""
        self.themed.append((widget, fn))
        try:
            fn(self.ui.c)
        except tk.TclError:
            pass
        return widget

    def hline(self, parent, **grid):
        f = tk.Frame(parent, height=1, bd=0, highlightthickness=0)
        self.theme_tk(f, lambda c, w=f: w.configure(bg=c["border"]))
        return f

    def vline(self, parent):
        f = tk.Frame(parent, width=1, bd=0, highlightthickness=0)
        self.theme_tk(f, lambda c, w=f: w.configure(bg=c["border"]))
        return f

    def section(self, parent, text, row, top=18, col=0, colspan=1, style="Section.TLabel"):
        l = ttk.Label(parent, text=text, style=style)
        l.grid(row=row, column=col, columnspan=colspan, sticky="w", pady=(self.S(top), self.S(6)))
        return l

    def placeholder(self, entry, var, text):
        """Подсказка внутри пустого поля (исчезает, как только что-то введено)."""
        lbl = tk.Label(entry, text=text, bd=0, padx=0, pady=0, cursor="xterm", font=self.ui.f_body, anchor="w")
        self.theme_tk(lbl, lambda c, w=lbl: w.configure(bg=c["field"], fg=c["faint"]))
        lbl.bind("<Button-1>", lambda e: entry.focus_set())

        def upd(*a):
            if var.get():
                lbl.place_forget()
            else:
                lbl.place(x=self.S(9), rely=0.5, anchor="w")
        var.trace_add("write", upd)
        upd()
        return lbl

    def autoscroll(self, sb):
        """Полоса прокрутки видна только тогда, когда содержимое не помещается."""
        def setter(lo, hi):
            try:
                if float(lo) <= 0.0 and float(hi) >= 1.0:
                    sb.grid_remove()
                else:
                    sb.grid()
                sb.set(lo, hi)
            except tk.TclError:
                pass
        return setter

    def make_tree(self, parent, cols, selectmode, hscroll=False, height=6, key=None, checkbox=False):
        """Таблица с колонками фиксированной ширины: ширину можно менять, перетаскивая границу заголовка,
        она запоминается. Лишнее место забирает пустая последняя колонка — колонки не разъезжаются."""
        S = self.S
        box = tk.Frame(parent, bd=0, highlightthickness=1)
        self.theme_tk(box, lambda c, w=box: w.configure(bg=c["bg"], highlightbackground=c["border"],
                                                        highlightcolor=c["border"]))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        names = [x[0] for x in cols]
        tv = ttk.Treeview(box, columns=names + ["_pad"], show="tree headings" if checkbox else "headings",
                          selectmode=selectmode, height=height)
        if checkbox:
            tv.column("#0", width=S(40), minwidth=S(40), stretch=False, anchor="center")
        saved = ((self.cfg.get("col_widths") or {}).get(key) or {}) if key else {}
        for k, t, w, a, _stretch in cols:
            tv.heading(k, text=t, anchor=a)
            try:
                w = int(saved.get(k, w))
            except (TypeError, ValueError):
                pass
            tv.column(k, width=max(S(48), w), minwidth=S(48), anchor=a, stretch=False)
        tv.heading("_pad", text="")
        tv.column("_pad", width=S(8), minwidth=0, stretch=True)
        vs = ttk.Scrollbar(box, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=self.autoscroll(vs))
        tv.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        if hscroll:
            hs = ttk.Scrollbar(box, orient="horizontal", command=tv.xview)
            tv.configure(xscrollcommand=self.autoscroll(hs))
            hs.grid(row=1, column=0, sticky="ew")
        self.trees.append(tv)
        if key:
            self.tree_cols[key] = (tv, names)
        return box, tv

    def empty_state(self, box):
        """Пустое состояние поверх таблицы: заголовок, пояснение и (необязательно) кнопка."""
        S = self.S
        f = ttk.Frame(box)
        f.title = ttk.Label(f, text="", style="Value.TLabel", justify="center")
        f.title.pack()
        f.text = ttk.Label(f, text="", style="Muted.TLabel", wraplength=S(340), justify="center")
        f.text.pack(pady=(S(4), 0))
        f.button = ttk.Button(f, text="")
        f.shown = None

        def show(title, text, button=None):
            f.title.configure(text=title)
            f.text.configure(text=text)
            if button:
                f.button.configure(text=button[0], command=button[1])
                f.button.pack(pady=(S(14), 0))
            else:
                f.button.pack_forget()
            f.place(relx=0.5, rely=0.45, anchor="center")
            f.shown = title

        def hide():
            f.place_forget()
            f.shown = None
        f.show, f.hide = show, hide
        return f

    def field_row(self, parent, row, label, var, width=40, show=None):
        S = self.S
        l = ttk.Label(parent, text=label, style="Muted.TLabel")
        l.grid(row=row, column=0, sticky="w", pady=S(3), padx=(0, S(16)))
        e = ttk.Entry(parent, textvariable=var, width=width, show=show or "")
        e.grid(row=row, column=1, sticky="w", pady=S(3))
        return l, e

    # ---------- каркас окна ----------
    def _build(self):
        root = self.root
        root.columnconfigure(2, weight=1)
        root.rowconfigure(0, weight=1)
        self._build_sidebar()
        self.vline(root).grid(row=0, column=1, sticky="ns")
        main = ttk.Frame(root)
        main.grid(row=0, column=2, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=1)
        self.main = self.pages_host = main
        self.pages = {}
        self._build_export_page()
        self._build_exports_page()
        self._build_conn_page()
        self._build_settings_page()

    def _build_sidebar(self):
        S = self.S
        sb = ttk.Frame(self.root, style="Side.TFrame", padding=(S(12), S(18), S(12), S(16)))
        sb.grid(row=0, column=0, sticky="nsw")
        self.sidebar = sb
        sb.columnconfigure(0, minsize=S(188), weight=1)
        sb.rowconfigure(10, weight=1)
        ttk.Label(sb, text="Telegram Exporter", style="Brand.TLabel").grid(
            row=0, column=0, sticky="w", padx=S(12), pady=(0, S(20)))
        self.nav = {}
        for i, (key, title) in enumerate(self.NAV, start=1):
            b = ttk.Button(sb, text=title, style="Nav.TButton", takefocus=0,
                           command=lambda k=key: self.show_page(k))
            b.grid(row=i, column=0, sticky="ew", pady=(0, S(2)))
            self.nav[key] = b
        acc = ttk.Frame(sb, style="Side.TFrame")
        acc.grid(row=11, column=0, sticky="ew", padx=S(12))
        ttk.Label(acc, text="Аккаунт", style="Side.Small.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        self.l_acc_dot = ttk.Label(acc, text="●", style="DotOff.TLabel")
        self.l_acc_dot.grid(row=1, column=0, sticky="w")
        self.l_acc_name = ttk.Label(acc, text="Не подключено", style="Side.TLabel", cursor="hand2")
        self.l_acc_name.grid(row=1, column=1, sticky="w", padx=(S(6), 0))
        for w in (self.l_acc_dot, self.l_acc_name):
            w.bind("<Button-1>", lambda e: self.show_page("conn"))

    def _page(self, key, title):
        S = self.S
        p = ttk.Frame(self.pages_host, padding=(S(28), S(22), S(28), S(20)))
        p.grid(row=0, column=0, sticky="nsew")
        p.grid_remove()
        p.columnconfigure(0, weight=1)
        p.rowconfigure(1, weight=1)
        head = ttk.Frame(p)
        head.grid(row=0, column=0, sticky="ew", pady=(0, S(18)))
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text=title, style="Title.TLabel").grid(row=0, column=0, sticky="w")
        sub = ttk.Label(head, text="", style="Muted.TLabel")
        sub.grid(row=1, column=0, sticky="w", pady=(S(2), 0))
        sub.grid_remove()
        actions = ttk.Frame(head)
        actions.grid(row=0, column=1, rowspan=2, sticky="e")
        body = ttk.Frame(p)
        body.grid(row=1, column=0, sticky="nsew")
        self.pages[key] = p
        return body, actions, sub

    @staticmethod
    def set_sub(label, text):
        label.configure(text=text)
        if text:
            label.grid()
        else:
            label.grid_remove()

    def show_page(self, key):
        if key not in self.pages:
            return
        for k, p in self.pages.items():
            if k == key:
                p.grid()
            else:
                p.grid_remove()
        for k, b in self.nav.items():
            b.state(["selected"] if k == key else ["!selected"])
        changed = self.page != key
        self.page = key
        # страница не сжимается меньше своего содержимого — при нехватке места сжимается журнал
        self.main.rowconfigure(0, minsize=getattr(self, "page_min", {}).get(key, 0))
        if changed and key == "exports":
            self.refresh_exports()
            self.root.after_idle(self._init_sash)

    # ---------- страница «Экспорт» ----------
    def _build_export_page(self):
        S = self.S
        body, actions, sub = self._page("export", "Экспорт")
        self._build_takeout_notice(actions.master)
        self.b_all = ttk.Button(actions, text="Экспорт всех чатов…", command=self.on_start_all)
        self.b_all.pack(side="right")
        self.b_uncheck = ttk.Button(actions, text="Снять отметки", style="Link.TButton",
                                    command=lambda: self.set_checks(set()))
        body.columnconfigure(0, weight=1)
        body.columnconfigure(2, minsize=S(330))
        body.rowconfigure(0, weight=1)

        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        sr = ttk.Frame(left)
        sr.grid(row=0, column=0, sticky="ew", pady=(0, S(10)))
        sr.columnconfigure(0, weight=1)
        self.e_search = ttk.Entry(sr, textvariable=self.v_search)
        self.e_search.grid(row=0, column=0, sticky="ew")
        self.b_count_all = ttk.Button(sr, text="Посчитать сообщения", style="Link.TButton", command=self.count_all)
        self.b_count_all.grid(row=0, column=1, sticky="e", padx=(S(10), 0))
        self.count_bar = ttk.Frame(left)
        self.count_bar.columnconfigure(1, weight=1)
        self.l_count_prog = ttk.Label(self.count_bar, text="", style="Small.TLabel")
        self.l_count_prog.grid(row=0, column=0, sticky="w", padx=(0, S(10)))
        self.pb_count = ttk.Progressbar(self.count_bar, mode="determinate", maximum=1)
        self.pb_count.grid(row=0, column=1, sticky="ew")
        ttk.Button(self.count_bar, text="Остановить", style="Link.TButton", command=self.stop_counting).grid(
            row=0, column=2, sticky="e", padx=(S(8), 0))
        self.placeholder(self.e_search, self.v_search, "Найти чат: название, ID, @username или ссылка t.me")
        box, tv = self.make_tree(left, (("name", "Чат", S(340), "w", False), ("kind", "Тип", S(110), "w", False),
                                        ("msgs", "Сообщений", S(130), "e", False)),
                                 selectmode="extended", key="chats", checkbox=True)
        self.chat_sort = (None, False)
        for col in ("name", "kind", "msgs"):
            tv.heading(col, command=lambda c=col: self.sort_chats(c))
        self.count_bar.grid(row=3, column=0, sticky="ew", pady=(S(8), 0))
        self.count_bar.grid_remove()
        tv.heading("#0", image=self.ui.tree_cb["off"], command=self.toggle_all_checks)
        tv.bind("<Button-1>", self.on_chat_click)
        tv.bind("<space>", lambda e: (self.toggle_checks(self.tv.selection()), "break")[1])
        box.grid(row=0 + 1, column=0, sticky="nsew")
        self.tv = tv
        tv.bind("<<TreeviewSelect>>", self.on_pick_chat)
        self.checked = set()
        tv.bind("<Return>", lambda e: self.b_start.focus_set())
        self.chat_empty = self.empty_state(box)

        self.vline(body).grid(row=0, column=1, sticky="ns", padx=S(24))

        right_outer = ttk.Frame(body)
        right_outer.grid(row=0, column=2, sticky="nsew")
        right_outer.columnconfigure(0, weight=1)
        right_outer.rowconfigure(0, weight=1)
        sc_area, right = self.scroll_area(right_outer, S(340))
        sc_area.grid(row=0, column=0, sticky="nsew")
        right.columnconfigure(0, weight=1)
        self.l_chat_title = ttk.Label(right, text="", style="H2.TLabel", wraplength=S(330), justify="left")
        self.l_chat_title.grid(row=0, column=0, sticky="w")
        self.l_count = ttk.Label(right, text="", style="Muted.TLabel", wraplength=S(330), justify="left")
        self.l_count.grid(row=1, column=0, sticky="w", pady=(S(2), 0))

        self.l_mode_sec = self.section(right, "Режим", 2, top=20)
        self.vf_version = ttk.Frame(right)
        self.vf_version.grid(row=3, column=0, sticky="ew")
        self.cb_version = ttk.Combobox(self.vf_version, textvariable=self.v_version, state="readonly")
        self.cb_version.pack(fill="x")
        self.b_chat_versions = ttk.Button(self.vf_version, text="", style="Link.TButton",
                                          command=lambda: self._chat_ver_row and self.show_versions(self._chat_ver_row))
        self.combos.append(self.cb_version)
        self.l_order_sec = self.section(right, "Порядок", 2, top=20)
        self.seg_order = Segmented(right, self.v_multi_mode, [("sequential", "По очереди"),
                                                             ("scan_first", "Сначала сканировать все")], k=self.ui.k)
        self.seg_order.grid(row=3, column=0, sticky="w")
        self.l_order_sec.grid_remove()
        self.seg_order.grid_remove()

        self.section(right, "Что скачивать", 4)
        tf = ttk.Frame(right)
        tf.grid(row=5, column=0, sticky="ew")
        tf.columnconfigure(0, weight=1)
        tf.columnconfigure(1, weight=1)
        short = {"voice_messages": "Голосовые", "video_messages": "Кружочки"}
        for i, (k, label) in enumerate(TYPE_LABELS.items()):
            ttk.Checkbutton(tf, text=short.get(k, label), variable=self.v_types[k]).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, S(12)))

        lf = ttk.Frame(right)
        lf.grid(row=6, column=0, sticky="ew", pady=(S(18), S(6)))
        lf.columnconfigure(0, weight=1)
        ttk.Label(lf, text="Максимальный размер файла", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.sp_limit = ttk.Spinbox(lf, from_=1, to=4096, textvariable=self.v_limit, width=6, increment=50)
        self.sp_limit.grid(row=0, column=1, sticky="e")
        ttk.Label(lf, text="МБ", style="Muted.TLabel").grid(row=0, column=2, sticky="e", padx=(S(6), 0))
        sf_ = ttk.Frame(right)
        sf_.grid(row=7, column=0, sticky="ew")
        sf_.columnconfigure(0, weight=1)
        self.sc_limit = ttk.Scale(sf_, from_=1, to=4096, variable=self.v_limit, orient="horizontal",
                                  command=lambda v: self.v_limit.set(int(float(v))))
        self.sc_limit.grid(row=0, column=0, sticky="ew")
        ttk.Checkbutton(sf_, text="Без ограничения", variable=self.v_unlimited,
                        command=self.update_limit_state).grid(row=0, column=1, sticky="e", padx=(S(14), 0))
        self.update_limit_state()

        self.section(right, "После сканирования", 8)
        Segmented(right, self.v_auto, [(True, "Сразу скачивать"), (False, "Показать сводку")],
                  k=self.ui.k).grid(row=9, column=0, sticky="w")
        self.l_order_hint = ttk.Label(right, text="", style="Small.TLabel", wraplength=S(330), justify="left")
        self.l_order_hint.grid(row=10, column=0, sticky="w", pady=(S(6), 0))
        self.v_multi_mode.trace_add("write", lambda *a: self.update_chat_header())
        self.v_auto.trace_add("write", lambda *a: self.update_chat_header())

        # дополнительные настройки экспорта (раньше были на странице «Настройки»); папка — внизу, «Изменить»
        self.b_more = ttk.Button(right, text="", style="Link.TButton", command=self.toggle_more)
        self.b_more.grid(row=11, column=0, sticky="w", pady=(S(8), 0))
        more = ttk.Frame(right)
        more.grid(row=12, column=0, sticky="ew", pady=(S(6), 0))
        more.columnconfigure(0, weight=1)
        self.more_frame = more
        ttk.Label(more, text="Форматы", style="Section.TLabel").grid(row=2, column=0, columnspan=2, sticky="w",
                                                                     pady=(0, S(2)))
        ttk.Checkbutton(more, text="HTML — для просмотра в браузере", variable=self.v_html).grid(
            row=3, column=0, columnspan=2, sticky="w")
        ttk.Checkbutton(more, text="JSON — формат Telegram Desktop", variable=self.v_json).grid(
            row=4, column=0, columnspan=2, sticky="w")
        ttk.Label(more, text="Скорость и надёжность", style="Section.TLabel").grid(
            row=5, column=0, columnspan=2, sticky="w", pady=(S(14), S(2)))
        for i, (label, var, lo, hi) in enumerate((
                ("Файлов одновременно (3–6)", self.v_parallel, 1, 16),
                ("Потоков на файл больше 2 МБ", self.v_threads, 1, 8),
                ("Зависшим считать через, с", self.v_stall, 30, 1800),
                ("Повторов перед пропуском", self.v_retries, 0, 10))):
            ttk.Label(more, text=label, style="Muted.TLabel").grid(row=6 + i, column=0, sticky="w")
            ttk.Spinbox(more, from_=lo, to=hi, textvariable=var, width=6).grid(row=6 + i, column=1, sticky="e",
                                                                                pady=S(3))
        ttk.Label(more, text="Зависший — файл, по которому за это время не пришло ни одного байта. После всех "
                             "повторов файл пропускается и попадает в отчёт; его можно докачать позже.",
                  style="Small.TLabel", wraplength=S(320), justify="left").grid(row=10, column=0, columnspan=2,
                                                                                 sticky="w", pady=(S(6), S(4)))
        self.toggle_more(init=True)

        dest = ttk.Frame(right_outer)
        dest.grid(row=21, column=0, sticky="ew", pady=(S(16), S(8)))
        dest.columnconfigure(0, weight=1)
        self.l_dest = ttk.Label(dest, text="", style="Small.TLabel")
        self.l_dest.grid(row=0, column=0, sticky="w")
        ttk.Button(dest, text="Изменить", style="Link.TButton", command=self.pick_folder).grid(
            row=0, column=1, sticky="e")
        self.b_start = ttk.Button(right_outer, text="Начать экспорт", style="Accent.TButton", command=self.on_start)
        self.b_start.grid(row=22, column=0, sticky="ew", ipady=S(3))

    def open_more(self):
        """Показать «Дополнительно» на странице «Экспорт» (форматы, скорость, повторы)."""
        self.show_page("export")
        if not self.cfg.get("export_more", False):
            self.toggle_more()

    def toggle_more(self, init=False):
        if not init:
            self.cfg["export_more"] = not self.cfg.get("export_more", False)
            self.save_cfg()
        opened = bool(self.cfg.get("export_more", False))
        self.b_more.configure(text="Скрыть дополнительные настройки" if opened else
                              "Дополнительно: форматы, скорость, повторы…")
        if opened:
            self.more_frame.grid()
        else:
            self.more_frame.grid_remove()

    def scroll_area(self, parent, width):
        """Прокручиваемая область: полоса прокрутки появляется, только если содержимое не помещается."""
        holder = ttk.Frame(parent)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        cv = tk.Canvas(holder, highlightthickness=0, bd=0, width=width, height=self.S(240))
        self.theme_tk(cv, lambda c, w=cv: w.configure(bg=c["bg"]))
        cv.grid(row=0, column=0, sticky="nsew")
        inner = ttk.Frame(cv)
        win = cv.create_window(0, 0, window=inner, anchor="nw")
        sb = ttk.Scrollbar(holder, orient="vertical", command=cv.yview)

        def sync(_e=None):
            cv.configure(scrollregion=(0, 0, inner.winfo_reqwidth(), inner.winfo_reqheight()))
            need = inner.winfo_reqheight() > cv.winfo_height() + 1
            if need and not sb.winfo_ismapped():
                sb.grid(row=0, column=1, sticky="ns", padx=(self.S(6), 0))
            elif not need and sb.winfo_ismapped():
                sb.grid_remove()
                cv.yview_moveto(0)
        inner.bind("<Configure>", sync, add="+")
        cv.bind("<Configure>", lambda e: (cv.itemconfigure(win, width=e.width), sync()), add="+")
        cv.configure(yscrollcommand=sb.set)

        def wheel(e):
            w = e.widget
            if not isinstance(w, tk.Misc) or not str(w).startswith(str(cv)):
                return None
            if isinstance(w, ttk.Combobox) or not sb.winfo_ismapped():
                return None
            cv.yview_scroll(-1 if e.delta > 0 else 1, "units")
            return None
        self.root.bind_all("<MouseWheel>", wheel, add="+")
        return holder, inner

    def update_limit_state(self):
        st = "disabled" if self.v_unlimited.get() else "normal"
        self.sp_limit.configure(state=st)
        self.sc_limit.state(["disabled"] if self.v_unlimited.get() else ["!disabled"])

    # ---------- режим экспорта Telegram (разрешение на экспорт) ----------
    def _build_takeout_notice(self, head):
        """Плашка о режиме экспорта: её можно скрыть, но сведения всегда есть в «Подключении»."""
        S = self.S
        f = tk.Frame(head, bd=0, highlightthickness=1)
        self.theme_tk(f, lambda c, w=f: w.configure(bg=c["accent_soft"], highlightbackground=c["accent_soft"],
                                                    highlightcolor=c["accent_soft"]))
        f.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(S(12), 0))
        f.columnconfigure(0, weight=1)
        self.tko_label = tk.Label(f, text="", anchor="w", justify="left", font=self.ui.f_body, bd=0)
        self.theme_tk(self.tko_label, lambda c, w=self.tko_label: w.configure(bg=c["accent_soft"], fg=c["text"]))
        self.tko_label.grid(row=0, column=0, sticky="ew", padx=(S(12), S(8)), pady=S(8))
        f.bind("<Configure>", lambda e: self.tko_label.configure(wraplength=max(S(300), e.width - S(240))))
        links = []
        for col, text, cmd in ((1, "Подробнее", self.show_takeout_info), (2, "Скрыть", self.hide_takeout_notice)):
            l = tk.Label(f, text=text, cursor="hand2", font=self.ui.f_body, bd=0)
            self.theme_tk(l, lambda c, w=l: w.configure(bg=c["accent_soft"], fg=c["accent"]))
            l.grid(row=0, column=col, sticky="e", padx=(S(4), S(12) if col == 2 else S(8)))
            l.bind("<Button-1>", lambda e, c=cmd: c())
            links.append(l)
        self.tko_notice = f
        f.grid_remove()

    TKO_ALLOW = ("Чтобы не ждать: в Telegram на телефоне откройте сообщение «Data export request» от Telegram, "
                 "нажмите Allow (Разрешить), затем «Проверить сейчас».")

    def takeout_state(self):
        st = dict(self.cfg.get("takeout") or {})
        if st.get("status") == "delayed" and st.get("until", 0) <= time.time():
            st["status"] = "ready"            # срок прошёл — включится при следующем экспорте
        return st

    def takeout_text(self, short=False):
        import datetime as _dt
        st = self.takeout_state()
        s = st.get("status")
        if s == "on":
            return "⚡ Режим экспорта Telegram включён — при сканировании истории меньше пауз."
        if s == "delayed":
            when = _dt.datetime.fromtimestamp(st["until"]).strftime("%d.%m.%Y %H:%M")
            return (f"ℹ Режим экспорта Telegram станет доступен {when}. До этого экспорт идёт в обычном режиме "
                    "(Telegram делает короткие паузы при сканировании).")
        if s == "ready":
            return ("✅ Срок ожидания режима экспорта Telegram прошёл — он включится сам при следующем экспорте. "
                    "Можно проверить сейчас.")
        if s == "failed":
            return f"ℹ Режим экспорта Telegram не включился: {st.get('error', '')}. Экспорт идёт в обычном режиме."
        return "Режим экспорта Telegram ещё не проверялся — программа попробует включить его при первом экспорте."

    def update_takeout_ui(self):
        st = self.takeout_state()
        sig = f"{st.get('status')}|{int(st.get('until', 0))}"
        show = st.get("status") in ("on", "delayed", "ready", "failed") and self.cfg.get("takeout_hidden") != sig
        self.tko_label.configure(text=self.takeout_text())
        if show:
            self.tko_notice.grid()
        else:
            self.tko_notice.grid_remove()
        if hasattr(self, "l_tko_status"):
            self.l_tko_status.configure(text=self.takeout_text() + (
                "\n" + self.TKO_ALLOW if st.get("status") == "delayed" else ""))
            ready = self.worker.client is not None and not self.busy
            self.b_tko_check.configure(state="normal" if ready else "disabled")

    def hide_takeout_notice(self):
        st = self.takeout_state()
        self.cfg["takeout_hidden"] = f"{st.get('status')}|{int(st.get('until', 0))}"
        self.save_cfg()
        self.update_takeout_ui()
        self.set_phase("Плашку можно вернуть: сведения о режиме экспорта — на странице «Подключение».")

    def on_takeout_event(self, state):
        self.cfg["takeout"] = state
        self.save_cfg()
        self.update_takeout_ui()

    def check_takeout(self):
        if not self.worker.client or self.busy:
            return
        self.b_tko_check.configure(state="disabled")
        self.l_tko_status.configure(text="Проверяю…")
        cfg = json.loads(json.dumps(self.collect_cfg()))

        async def run():
            try:
                await self.worker.check_takeout(cfg)
            except Exception as e:
                self.q.put(("takeout", {"status": "failed", "error": str(e)[:200], "checked": time.time()}))
                self.q.put(("takeout_checked",))
        asyncio.run_coroutine_threadsafe(run(), self.worker.loop)

    def show_takeout_info(self):
        S = self.S
        d, body, btns = self._modal("Режим экспорта Telegram")
        st = ttk.Label(body, text=self.takeout_text(), style="Value.TLabel", wraplength=S(520), justify="left")
        st.pack(anchor="w")
        for text in (
                "Это тот же режим, которым пользуется Telegram Desktop при «Экспорте данных». В нём Telegram мягче "
                "ограничивает частоту запросов, поэтому при чтении истории (сканировании) меньше пауз.",
                "Когда программа впервые просит этот режим, Telegram присылает в чат «Telegram» сообщение "
                "«Data export request» — это запрос этой программы. Нажмите в нём Allow (Разрешить) на телефоне — "
                "режим разрешится сразу (сообщение станет «Accepted»). Если не нажимать, Telegram разрешит его сам "
                "примерно через сутки.",
                "Программа сама включает режим при каждом запуске экспорта. Пока Telegram его не разрешил, экспорт "
                "идёт в обычном режиме — ничего делать не нужно.",
                "На скорость скачивания файлов режим не влияет — только на паузы при сканировании истории."):
            ttk.Label(body, text=text, style="Muted.TLabel", wraplength=S(520), justify="left").pack(
                anchor="w", pady=(S(10), 0))
        ttk.Button(btns, text="Закрыть", command=d.destroy).pack(side="right")
        if self.worker.client and not self.busy:
            def check():
                d.destroy()
                self.show_page("conn")
                self.check_takeout()
            ttk.Button(btns, text="Проверить сейчас", command=check).pack(side="right", padx=(0, S(8)))
        d.bind("<Escape>", lambda e: d.destroy())
        self._show_modal(d)

    def update_dest(self):
        if not hasattr(self, "l_dest"):
            return
        p = self.v_out.get().strip() or "папка не выбрана"
        if len(p) > 38:
            p = "…" + p[-37:]
        self.l_dest.configure(text=f"Сохранение: {p}")

    def update_chat_header(self):
        if not hasattr(self, "l_chat_title"):
            return
        multi = len(getattr(self, "checked", ())) >= 2
        for w in (self.l_mode_sec, self.vf_version):
            w.grid_remove() if multi else w.grid()
        for w in (self.l_order_sec, self.seg_order):
            w.grid() if multi else w.grid_remove()
        n = len(getattr(self, "checked", ()))
        if n:
            self.b_uncheck.pack(side="right", padx=(0, self.S(12)))
        else:
            self.b_uncheck.pack_forget()
        self.b_start.configure(text=f"Экспортировать выбранные ({n})" if n >= 2 else "Начать экспорт")
        auto = bool(self.v_auto.get())
        if multi:
            names = [self.dialog_map[i][1] for i in self.checked if i in self.dialog_map]
            self.l_chat_title.configure(text=f"Выбрано чатов: {n}")
            self.l_count.configure(text=", ".join(sorted(names)[:4]) + (f" и ещё {n - 4}" if n > 4 else ""))
            if self.v_multi_mode.get() == "scan_first":
                hint = ("Сначала все чаты сканируются, потом скачиваются файлы. "
                        + ("Скачивание начнётся сразу." if auto else "Перед скачиванием — одна общая сводка по всем чатам."))
            else:
                hint = ("Каждый чат сканируется и сразу скачивается, затем следующий. "
                        + ("" if auto else "Перед скачиванием каждого чата — сводка; «Нет» пропустит файлы этого чата."))
            self.l_order_hint.configure(text=hint)
            return
        self.l_order_hint.configure(text="" if auto else "Перед скачиванием покажу, сколько файлов и места нужно.")
        chat = self.v_chat.get().strip()
        if not chat:
            self.l_chat_title.configure(text="Чат не выбран")
            self.l_count.configure(text="Выберите чат в списке слева.")
            return
        d = self.dialog_map.get(chat)
        n = self.counts.get(chat, "?")
        if d:
            self.l_chat_title.configure(text=d[1])
            if n == "?":
                cnt = "считаю сообщения…"
            elif n is None:
                cnt = "не удалось посчитать сообщения"
            else:
                cnt = f"{n:,} сообщений".replace(",", " ")
            self.l_count.configure(text=f"{d[2]} · {cnt}")
        else:
            self.l_chat_title.configure(text=chat)
            self.l_count.configure(text="Чат будет найден при запуске экспорта." if n in ("?", None)
                                   else f"{n:,} сообщений".replace(",", " "))

    def refresh_versions(self):
        """Режим: продолжить / пересканировать / экспорт из сохранённой версии сканирования."""
        if not hasattr(self, "cb_version"):
            return
        chat = self.v_chat.get().strip()
        try:
            dirs = find_chat_dirs(self.v_out.get().strip(), chat)
        except Exception:
            dirs = []
        started = any(os.path.exists(os.path.join(d, "scan_cache.json")) for d in dirs)
        if chat.lstrip("-").isdigit() and not started:
            opts = [("Новый экспорт: вся история", None)]       # продолжать ещё нечего
        else:
            opts = [(self.VERSION_NEW, None), (self.VERSION_FULL, "full")]
        # версии сканирования — в отдельном окне чата; здесь только ссылка на него
        def nsnap(dd):
            vd = os.path.join(dd, VERSIONS_DIR)
            try:
                return sum(1 for n in os.listdir(vd)
                           if not n.endswith(".tmp") and os.path.exists(os.path.join(vd, n, "meta.json")))
            except OSError:
                return 0
        nver, vdir = 0, None
        for d in dirs:
            n = nsnap(d)
            if n > nver:
                nver, vdir = n, d
        if nver:
            name = self.dialog_map.get(chat, (None, os.path.basename(vdir)))[1] if hasattr(self, "dialog_map") \
                else os.path.basename(vdir)
            self._chat_ver_row = {"dir": vdir, "name": name, "peer": chat, "raw_id": None}
            self.b_chat_versions.configure(text=f"Сохранённые версии сканирования: {nver}…")
            self.b_chat_versions.pack(anchor="w", pady=(self.S(4), 0))
        else:
            self._chat_ver_row = None
            self.b_chat_versions.pack_forget()
        cur = self.v_version.get()
        labels = [tr(o[0]) for o in opts]
        self.version_ids = [o[1] for o in opts]
        self.cb_version.configure(values=labels)
        self.v_version.set(cur if cur in labels else labels[0])

    def selected_version(self):
        try:
            i = self.cb_version.current()
            return self.version_ids[i] if i >= 0 else None
        except Exception:
            return None

    # ---------- страница «Мои экспорты» ----------
    def _build_exports_page(self):
        """Как в торрент-клиентах: сверху список экспортов (текущий обновляется в своей строке),
        снизу — панель деталей: общее, график скорости, журнал. Границу между ними можно двигать."""
        S = self.S
        body, actions, sub = self._page("exports", "Мои экспорты")
        self.l_ex_sum = sub
        Segmented(actions, self.v_ex_filter, [(f, f) for f in self.EX_FILTERS], command=self.fill_exports,
                  k=self.ui.k).pack(side="left")
        ttk.Button(actions, text="Обновить", command=self.refresh_exports).pack(side="left", padx=(S(12), 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        pw = ttk.Panedwindow(body, orient="vertical")
        pw.grid(row=0, column=0, sticky="nsew")
        self.ex_pw = pw
        top = ttk.Frame(pw)
        top.columnconfigure(0, weight=1)
        top.rowconfigure(1, weight=1)

        bar = ttk.Frame(top)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, S(10)))
        self.ex_btns = {}
        for key, text, cmd, style in (("cont", "Продолжить", self.ex_continue, "Accent.TButton"),
                                      ("full", "Начать заново", self.ex_restart, "TButton"),
                                      ("ver", "Версии…", self.ex_versions, "TButton"),
                                      ("html", "HTML", lambda: self.ex_open("html"), "TButton"),
                                      ("dir", "Папку", lambda: self.ex_open("dir"), "TButton"),
                                      ("rep", "Отчёт", lambda: self.ex_open("report"), "TButton")):
            if key == "html":
                self.vline(bar).pack(side="left", fill="y", padx=(S(4), S(14)), pady=S(4))
                ttk.Label(bar, text="Открыть", style="Muted.TLabel").pack(side="left", padx=(0, S(8)))
            b = ttk.Button(bar, text=text, command=cmd, style=style)
            b.pack(side="left", padx=(0, S(8)))
            self.ex_btns[key] = b
        self.b_ex_all = ttk.Button(bar, text="Продолжить незавершённые", command=self.ex_continue_unfinished)
        self.b_ex_all.pack(side="right")

        fnt = self.ui.f_body

        def W(sample):
            return fnt.measure(sample) + S(26)
        cols = (("name", "Чат", W("Длинное название чата"), "w", False),
                ("status", "Состояние", W("Сканирование не завершено"), "w", False),
                ("pct", "Прогресс", W("Прогресс"), "e", False),
                ("speed", "Скорость", W("999 сообщ./с"), "e", False),
                ("eta", "Осталось", W("~23 ч 59 мин"), "e", False),
                ("files", "Файлы", W("41784 / 41784 ⚠9"), "e", False),
                ("size", "Объём файлов", W("999.9 ГБ из 999.9 ГБ"), "e", False),
                ("msgs", "Сообщений", W("354 176 / 354 176"), "e", False),
                ("upd", "Обновлён", W("22.11 19:39"), "center", False),
                ("ver", "Версий", W("Версий"), "center", False))
        box, tv = self.make_tree(top, cols, selectmode="extended", hscroll=True, key="exports")
        box.grid(row=1, column=0, sticky="nsew")
        tv.bind("<<TreeviewSelect>>", lambda e: self.update_ex_buttons())
        tv.bind("<Double-1>", self.ex_double)
        tv.bind("<Return>", lambda e: self.ex_continue())
        tv.bind("<Button-3>", self.ex_menu_popup)
        self.ex_tv = tv
        self.ex_empty = self.empty_state(box)

        self.details = self._build_details(pw)
        pw.add(top, weight=3)
        pw.add(self.details, weight=1)

        m = tk.Menu(self.root, tearoff=0)
        self.theme_tk(m, lambda c, w=m: w.configure(bg=c["field"], fg=c["text"], activebackground=c["accent_soft"],
                                                    activeforeground=c["text"], disabledforeground=c["faint"],
                                                    bd=0, relief="flat", font=self.ui.f_body, activeborderwidth=0))
        self.ex_menu_items = [("cont", "Продолжить"), ("full", "Начать заново"), ("ver", "Версии сканирования…"), None,
                              ("html", "Открыть HTML"), ("dir", "Открыть папку"), ("rep", "Отчёт")]
        for it in self.ex_menu_items:
            if it is None:
                m.add_separator()
            else:
                m.add_command(label=it[1], command=lambda k=it[0]: self.ex_btns[k].invoke())
        self.ex_menu = m
        self.update_ex_buttons()

    def _build_details(self, parent):
        """Панель деталей текущего экспорта: «Общее», «Скорость» (график), «Журнал»."""
        S = self.S
        d = ttk.Frame(parent)
        d.columnconfigure(0, weight=1)
        d.rowconfigure(2, weight=1)
        self.hline(d).grid(row=0, column=0, sticky="ew", pady=(S(6), S(12)))
        head = ttk.Frame(d)
        head.grid(row=1, column=0, sticky="ew")
        head.columnconfigure(1, weight=1)
        Segmented(head, self.v_details_tab, [("general", "Общее"), ("speed", "Скорость"), ("log", "Журнал")],
                  command=self.show_details_tab, k=self.ui.k).grid(row=0, column=0, sticky="w")
        self.l_job = ttk.Label(head, text="", style="Muted.TLabel")
        self.l_job.grid(row=0, column=1, sticky="w", padx=(S(16), 0))
        self.b_stop = ttk.Button(head, text="Остановить", style="Danger.TButton", command=self.on_stop)
        self.b_stop.grid(row=0, column=2, sticky="e")
        stack = ttk.Frame(d)
        stack.grid(row=2, column=0, sticky="nsew", pady=(S(14), 0))
        stack.columnconfigure(0, weight=1)
        stack.rowconfigure(0, weight=1)

        g = ttk.Frame(stack)
        g.grid(row=0, column=0, sticky="nsew")
        g.columnconfigure(0, weight=1)
        self.l_phase = ttk.Label(g, text="", style="Value.TLabel", justify="left")
        self.l_phase.grid(row=0, column=0, sticky="w")
        g.bind("<Configure>", lambda e: self.l_phase.configure(wraplength=max(S(240), e.width - S(8))), add="+")
        self.net_warn = ttk.Frame(g)
        self.l_net_warn = ttk.Label(self.net_warn, text="", style="Warning.TLabel", justify="left")
        self.l_net_warn.pack(side="left")
        ttk.Button(self.net_warn, text="Проверить снова", style="Link.TButton",
                   command=self.recheck).pack(side="left", padx=(S(8), 0))
        ttk.Button(self.net_warn, text="Настройки подключения", style="Link.TButton",
                   command=lambda: self.show_page("conn")).pack(side="left", padx=(S(4), 0))
        self.net_warn.grid(row=5, column=0, sticky="w", pady=(S(6), 0))
        self.net_warn.grid_remove()
        g.bind("<Configure>", lambda e: self.l_net_warn.configure(wraplength=max(S(240), e.width - S(260))), add="+")
        self.disk_warn = ttk.Frame(g)
        self.l_disk_warn = ttk.Label(self.disk_warn, text="", style="Danger.TLabel", justify="left")
        self.l_disk_warn.pack(side="left")
        ttk.Button(self.disk_warn, text="Открыть папку", style="Link.TButton",
                   command=self.open_folder).pack(side="left", padx=(S(8), 0))
        self.disk_warn.grid(row=1, column=0, sticky="w", pady=(S(6), 0))
        self.disk_warn.grid_remove()
        self.pb = ttk.Progressbar(g, mode="determinate", maximum=1000)
        self.pb.grid(row=2, column=0, sticky="ew", pady=(S(10), S(10)))
        sf = ttk.Frame(g)
        sf.grid(row=3, column=0, sticky="ew")
        self.stats_frame = sf
        self.stat = {}
        self.stat_cells = []
        self._stat_cols = 0
        for k, label in self.STAT_NAMES:
            lab = ttk.Label(sf, text=label, style="Small.TLabel")
            v = ttk.Label(sf, text="—")
            self.stat[k] = v
            self.stat_cells.append((lab, v))
        self.layout_stats(3)
        g.bind("<Configure>", lambda e: self.layout_stats(3 if e.width >= S(900) else 2), add="+")
        self.l_idle = ttk.Label(g, text="Здесь появится ход экспорта: прогресс, скорость, прогноз окончания и журнал.",
                                style="Muted.TLabel")
        self.l_idle.grid(row=4, column=0, sticky="w", pady=(S(4), 0))

        sp = ttk.Frame(stack)
        sp.grid(row=0, column=0, sticky="nsew")
        sp.columnconfigure(0, weight=1)
        sp.rowconfigure(1, weight=1)
        self.l_graph = ttk.Label(sp, text="", style="Muted.TLabel")
        self.l_graph.grid(row=0, column=0, sticky="w")
        Segmented(sp, self.v_graph_period, [("300", "5 мин"), ("900", "15 мин"), ("3600", "1 час")],
                  command=self.draw_graph, k=self.ui.k).grid(row=0, column=1, sticky="e")
        self.graph = tk.Canvas(sp, height=S(150), highlightthickness=0, bd=0)
        self.theme_tk(self.graph, lambda c, w=self.graph: w.configure(bg=c["bg"]))
        self.graph.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(S(10), 0))
        self.graph.bind("<Configure>", lambda e: self.draw_graph())
        self.graph.bind("<Motion>", self.graph_hover)
        self.graph.bind("<Leave>", self.graph_hover)
        self._graph_pts = []
        self._ghover_x = None
        self._graph_unit = str

        lg = ttk.Frame(stack)
        lg.grid(row=0, column=0, sticky="nsew")
        lg.columnconfigure(0, weight=1)
        lg.rowconfigure(0, weight=1)
        box = tk.Frame(lg, bd=0, highlightthickness=1)
        self.theme_tk(box, lambda c, w=box: w.configure(bg=c["field"], highlightbackground=c["border"],
                                                        highlightcolor=c["border"]))
        box.grid(row=0, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        self.logbox = tk.Text(box, height=6, wrap="word", bd=0, relief="flat", padx=S(10), pady=S(8),
                              font=self.ui.f_mono, state="disabled", highlightthickness=0, cursor="arrow")
        self.theme_tk(self.logbox, lambda c, w=self.logbox: w.configure(
            bg=c["field"], fg=c["text"], insertbackground=c["text"], selectbackground=c["accent_soft"],
            selectforeground=c["text"]))
        ls = ttk.Scrollbar(box, orient="vertical", command=self.logbox.yview)
        self.logbox.configure(yscrollcommand=self.autoscroll(ls))
        self.logbox.grid(row=0, column=0, sticky="nsew")
        ls.grid(row=0, column=1, sticky="ns")
        ttk.Button(lg, text="Открыть файл журнала", style="Link.TButton", command=self.open_log).grid(
            row=1, column=0, sticky="e", pady=(S(4), 0))
        self.details_frames = {"general": g, "speed": sp, "log": lg}
        self.show_details_tab()
        return d

    def show_details_tab(self):
        cur = self.v_details_tab.get()
        if cur not in self.details_frames:
            cur = "general"
            self.v_details_tab.set(cur)
        for k, f in self.details_frames.items():
            if k == cur:
                f.grid()
            else:
                f.grid_remove()
        if cur == "log":
            self.logbox.see("end")
        elif cur == "speed":
            self.root.after_idle(self.draw_graph)

    @staticmethod
    def _nice_ceil(v):
        if v <= 0:
            return 1.0
        import math
        e = 10 ** math.floor(math.log10(v))
        for m in (1, 2, 2.5, 5, 10):
            if v <= m * e:
                return m * e
        return 10 * e

    def draw_graph(self):
        """График скорости за выбранный период: скачивание (байт/с) или сканирование (сообщений/с)."""
        if not hasattr(self, "graph") or self.v_details_tab.get() != "speed":
            return
        cv, c, S = self.graph, self.ui.c, self.S
        w, h = cv.winfo_width(), cv.winfo_height()
        if w < S(80) or h < S(30):
            return
        cv.delete("all")
        try:
            period = int(self.v_graph_period.get())
        except ValueError:
            period = 300
        now = time.monotonic()
        scan = self.graph_mode == "scan"
        hist = self.scan_hist if scan else self.speed_hist
        data = [(t, v) for t, v in hist if now - t <= period]
        unit = (lambda v: f"{v:,.0f}/с".replace(",", " ")) if scan else (lambda v: f"{human(v)}/с")
        vals = [v for _, v in data]
        if vals:
            cur = vals[-1]
            avg = sum(vals) / len(vals)
            title = "Скорость сканирования, сообщений в секунду" if scan else "Скорость скачивания"
            self.l_graph.configure(text=f"{title}  ·  сейчас {unit(cur)}  ·  в среднем {unit(avg)}  ·  "
                                        f"максимум {unit(max(vals))}")
        else:
            self.l_graph.configure(text="Скорость появится, когда начнётся сканирование или скачивание")
        fnt = self.ui.f_small
        pad_l = fnt.measure("999.9 МБ/с") + S(14)
        pad_r, pad_t, pad_b = S(6), S(6), fnt.metrics("linespace") + S(8)
        gw, gh = w - pad_l - pad_r, h - pad_t - pad_b
        if gw < 10 or gh < 6:
            return
        vmax = self._nice_ceil(max(vals) * 1.1 if vals and max(vals) > 0 else 1.0)
        lines = max(1, min(4, int(gh // (fnt.metrics("linespace") * 1.6))))
        for i in range(lines + 1):    # горизонтальные линии и подписи значений (без наложения)
            y = pad_t + gh * i / lines
            cv.create_line(pad_l, y, pad_l + gw, y, fill=c["border"])
            cv.create_text(pad_l - S(8), y, text=unit(vmax * (lines - i) / lines), anchor="e", fill=c["muted"],
                           font=fnt)
        mins = period // 60
        for frac, txt in ((0.0, f"−{mins} мин" if mins < 60 else "−1 ч"), (0.5, f"−{mins // 2} мин" if mins < 60
                                                                            else "−30 мин"), (1.0, "сейчас")):
            cv.create_text(pad_l + gw * frac, pad_t + gh + S(4), text=txt, anchor="n" if 0 < frac < 1 else
                           ("nw" if frac == 0 else "ne"), fill=c["muted"], font=fnt)
        self._graph_pts = []
        self._graph_box = (pad_l, pad_t, gw, gh)
        if len(data) < 2:
            return
        buckets = {}                  # не больше одной точки на пиксель ширины
        for t, v in data:
            x = int(gw * (1 - (now - t) / period))
            buckets.setdefault(x, []).append((t, v))
        pts = []
        for x in sorted(buckets):
            v = sum(b[1] for b in buckets[x]) / len(buckets[x])
            t = sum(b[0] for b in buckets[x]) / len(buckets[x])
            pts.append((pad_l + x, pad_t + gh * (1 - min(v, vmax) / vmax)))
            self._graph_pts.append((pad_l + x, pts[-1][1], v, t))
        base = pad_t + gh
        poly = [(pts[0][0], base)] + pts + [(pts[-1][0], base)]
        cv.create_polygon(*[k for p in poly for k in p], fill=c["accent_soft"], outline="")
        cv.create_line(*[k for p in pts for k in p], fill=c["accent"], width=max(1, S(2)))
        self._graph_unit = unit
        self.draw_graph_hover()

    def graph_hover(self, e):
        """Наведение курсора на график — скорость в этой точке и когда это было."""
        self._ghover_x = None if str(e.type) in ("Leave", "8") else e.x
        self.draw_graph_hover()

    def draw_graph_hover(self):
        cv, c, S = self.graph, self.ui.c, self.S
        cv.delete("hover")
        x = self._ghover_x
        pts = self._graph_pts
        if x is None or not pts:
            return
        px, py, v, t = min(pts, key=lambda p: abs(p[0] - x))
        if abs(px - x) > S(40):
            return
        pad_l, pad_t, gw, gh = self._graph_box
        cv.create_line(px, pad_t, px, pad_t + gh, fill=c["muted"], dash=(3, 3), tags="hover")
        r = max(3, S(4))
        cv.create_oval(px - r, py - r, px + r, py + r, fill=c["accent"], outline=c["bg"], width=max(1, S(2)),
                       tags="hover")
        ago = max(0, time.monotonic() - t)
        when = "сейчас" if ago < 5 else (f"{int(ago)} с назад" if ago < 60 else
                                         f"{int(ago // 60)} мин {int(ago % 60):02d} с назад")
        text = f"{self._graph_unit(v)} · {when}"
        fnt = self.ui.f_small
        tw = fnt.measure(text) + S(16)
        th = fnt.metrics("linespace") + S(8)
        bx = px + S(10) if px + S(10) + tw <= pad_l + gw else px - S(10) - tw
        by = max(pad_t, min(py - th - S(8), pad_t + gh - th))
        cv.create_rectangle(bx, by, bx + tw, by + th, fill=c["field"], outline=c["border_strong"], tags="hover")
        cv.create_text(bx + S(8), by + th / 2, text=text, anchor="w", fill=c["text"], font=fnt, tags="hover")

    QUIET_PHASES = ("⏸", "⛔", "Пауза", "Сохраняю", "Создаю", "Собираю", "Проверяю", "Докачиваю", "Кастомные")
    NET_HINT = "проверьте, что включён VPN или запущен прокси, и что на странице «Подключение» выбран нужный вид прокси"

    def recheck(self):
        """«Проверить снова»: сразу проверить связь, не дожидаясь следующей попытки."""
        self.worker.recheck_now()
        self._wd_t = time.monotonic()
        self.set_phase("Проверяю связь с Telegram…")

    def watchdog(self, now, st, p):
        """Если долго ничего не меняется — скорее всего нет связи; подсказываем, что проверить."""
        sig = (st.get("stage"), st.get("msg_n"), p.n, st.get("ok"))
        if sig != getattr(self, "_wd_sig", None):
            if getattr(self, "_wd_sig", None) is not None and now - getattr(self.worker, "net_issue_at", -1e9) > 3:
                self.worker.net_issue_at = -1e9      # работа пошла — связь есть, предупреждение снимаем
            self._wd_sig, self._wd_t = sig, now
        if self.l_phase.cget("text") != getattr(self, "_wd_phase", None):
            self._wd_phase, self._wd_t = self.l_phase.cget("text"), now
        stalled = now - getattr(self, "_wd_t", now)
        si = getattr(self.worker, "scan_info", None) or {}
        net_recent = now - getattr(self.worker, "net_issue_at", -1e9) < 90
        quiet = self.l_phase.cget("text").startswith(self.QUIET_PHASES) or si.get("pause_until", 0) > now             or st.get("stage") == "finish"
        warn = ""
        if self.busy and self.stoppable and not self.connecting and (net_recent or (stalled > 45 and not quiet)):
            secs = int(stalled)
            warn = ("Нет связи с Telegram" if net_recent else f"Telegram не отвечает уже {fmt_left(secs)[1:]}") +                 f" — {self.NET_HINT}. Ничего не потеряется: программа продолжит сама, когда связь появится."
        if warn:
            self.l_net_warn.configure(text=warn)
            self.net_warn.grid()
        else:
            self.net_warn.grid_remove()
        # подключение: долго нет ответа
        if self.connecting and self.busy and now - getattr(self, "connect_t", now) > 20 and not self.account:
            msg = "Telegram долго не отвечает — " + self.NET_HINT + ". Можно нажать «Отмена», поменять настройки и подключиться снова."
            if self.l_conn_hint.cget("text") != msg:
                self.l_conn_hint.configure(text=msg, style="Warning.TLabel")
            if self.chat_empty.shown and self.chat_empty.shown.startswith(("Загружаю", "Нет ответа")):
                self.chat_empty.show("Нет ответа от Telegram", "Проверьте, что включён VPN или запущен прокси.",
                                     ("Перейти к подключению", lambda: self.show_page("conn")))

    def update_live_row(self):
        """Строка текущего экспорта в таблице обновляется каждую секунду — как в торрент-клиентах."""
        iid = getattr(self, "ex_running_iid", None)
        live = getattr(self, "live", None)
        if not iid or not live or not self.ex_tv.exists(iid):
            return
        for col, val in live.items():
            try:
                if self.ex_tv.set(iid, col) != val:
                    self.ex_tv.set(iid, col, val)
            except tk.TclError:
                pass

    def _init_sash(self):
        """Начальная высота панели деталей (или сохранённая с прошлого раза)."""
        if self._sash_done:
            return
        pw = self.ex_pw
        h = pw.winfo_height()
        if h < self.S(120):
            self.root.after(100, self._init_sash)
            return
        try:
            dh = int(self.cfg.get("details_h") or 0)
        except (TypeError, ValueError):
            dh = 0
        dh = dh or max(self.details.winfo_reqheight(), int(h * 0.42))
        dh = max(self.S(150), min(dh, h - self.S(170)))
        try:
            pw.sashpos(0, h - dh)
            self._sash_done = True
        except tk.TclError:
            pass

    def ex_menu_popup(self, e):
        iid = self.ex_tv.identify_row(e.y)
        if not iid:
            return
        if iid not in self.ex_tv.selection():
            self.ex_tv.selection_set(iid)
        self.update_ex_buttons()
        idx = 0
        for it in self.ex_menu_items:
            if it is not None:
                st = str(self.ex_btns[it[0]].cget("state"))
                self.ex_menu.entryconfigure(idx, state="disabled" if st == "disabled" else "normal")
            idx += 1
        try:
            self.ex_menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.ex_menu.grab_release()

    def refresh_exports(self):
        """Пересобирает список в фоне (папок может быть много — окно не подвисает)."""
        try:
            cfg = self.collect_cfg()
        except (tk.TclError, ValueError):
            return
        self.ex_gen += 1
        gen = self.ex_gen
        root_dir, types, lim = cfg["out_root"], dict(cfg["types"]), cfg["size_limit_mb"]
        if not self.ex_all_rows:
            self.set_sub(self.l_ex_sum, "Обновляю список…")

        def job():
            try:
                rows = scan_exports(root_dir, types, lim)
            except Exception:
                rows = []
            self.q.put(("exports_rows", gen, rows))
        threading.Thread(target=job, daemon=True).start()

    def peer_of(self, row):
        """ID чата для экспорта: из .chat_peer, иначе по списку чатов, иначе как есть."""
        if row.get("peer"):
            return str(row["peer"])
        raw = row.get("raw_id")
        if raw:
            try:
                from telethon.utils import resolve_id
                for did, _n, _k in self.dialogs:
                    if str(resolve_id(int(did))[0]) == str(raw):
                        return str(did)
            except Exception:
                pass
        return str(raw) if raw else None

    def fill_exports(self):
        tv = self.ex_tv
        sel = {self.ex_rows[int(i)]["dir"] or self.ex_rows[int(i)]["name"] for i in tv.selection()
               if int(i) < len(self.ex_rows)}
        tv.delete(*tv.get_children())
        flt = self.v_ex_filter.get()
        running_dir = getattr(self.worker, "current_dir", None) if self.worker.stats.get("running") else None
        rows = []
        for r in self.ex_all_rows:
            r = dict(r)
            r["status"] = re.sub(r"\s*\([^)]*\)\s*$", "", plain(r["status"]))   # числа — в соседних колонках
            if running_dir and r["dir"] and os.path.normcase(r["dir"]) == os.path.normcase(running_dir):
                r["code"], r["status"] = "running", "Идёт"
            if flt == "Незавершённые" and r["code"] == "done":
                continue
            if flt == "Готовые" and r["code"] != "done":
                continue
            rows.append(r)
        rows.sort(key=lambda r: (STATUS_ORDER.get(r["code"], 9), -r["updated"]))
        self.ex_rows = rows
        self.ex_running_iid = None
        import datetime as _dt
        for i, r in enumerate(rows):
            msgs = f"{r['msgs']:,}".replace(",", " ") if r["msgs"] else ""
            if r["msg_total"] and r["code"] in ("scan", "running") and r["msgs"] < r["msg_total"]:
                msgs += f" / {r['msg_total']:,}".replace(",", " ")
            files = f"{r['done']} / {r['want']}" if r["want"] else ("—" if r["dir"] else "")
            if r["failed"]:
                files += f" ⚠{r['failed']}"
            if not r["want_bytes"]:
                size = ""
            elif r["code"] == "done":
                size = human(r["want_bytes"])
            elif r["code"] == "scan":
                size = f"найдено {human(r['want_bytes'])}"        # ещё ищем — это не скачанное
            else:
                size = f"{human(r['done_bytes'])} из {human(r['want_bytes'])}"
            upd = _dt.datetime.fromtimestamp(r["updated"]).strftime("%d.%m %H:%M") if r["updated"] else ""
            if r["code"] == "scan" and r["msg_total"]:
                pct = f"{min(100, r['msgs'] * 100 // r['msg_total'])}%"
            elif r["code"] == "done":
                pct = "100%"
            elif r["want"]:
                pct = f"{r['done'] * 100 // r['want']}%"
            else:
                pct = ""
            tag = r["code"] if r["code"] in ("running", "failed") else ""
            iid = tv.insert("", "end", iid=str(i), tags=(tag,),
                            values=(r["name"], r["status"], pct, "", "", files, size, msgs, upd, r["versions"] or ""))
            if r["code"] == "running":
                self.ex_running_iid = iid
                self.ex_running_want = r["want_bytes"]
                self.ex_running_failed = r["failed"]
            if (r["dir"] or r["name"]) in sel:
                tv.selection_add(iid)
        if self._select_running and self.ex_running_iid:
            tv.selection_set(self.ex_running_iid)
            tv.see(self.ex_running_iid)
            self._select_running = False
        allr = self.ex_all_rows
        cnt = collections.Counter("done" if r["code"] == "done" else "todo" for r in allr)
        self.set_sub(self.l_ex_sum, f"{len(allr)} чатов · готово {cnt.get('done', 0)} · "
                                    f"незавершённых {cnt.get('todo', 0)}" if allr else "")
        if not allr:
            self.ex_empty.show("Пока нет экспортов",
                               "Здесь появятся чаты, которые вы экспортируете, с их состоянием и действиями.",
                               ("Выбрать чат для экспорта", lambda: self.show_page("export")))
        elif not rows:
            self.ex_empty.show("Нет чатов в этой категории", "Выберите другой фильтр вверху.")
        else:
            self.ex_empty.hide()
        self.update_live_row()
        self.update_ex_buttons()

    def ex_selected(self):
        return [self.ex_rows[int(i)] for i in self.ex_tv.selection() if int(i) < len(self.ex_rows)]

    def update_ex_buttons(self):
        if not hasattr(self, "ex_btns"):
            return
        sel = self.ex_selected()
        one = sel[0] if len(sel) == 1 else None
        st = {
            "cont": bool(sel) and not self.busy,
            "full": bool(one and one["dir"]) and not self.busy,
            "ver": bool(one and one["versions"]),
            "html": bool(one and one["html"]),
            "dir": bool(one and one["dir"]),
            "rep": bool(one and one["dir"] and os.path.exists(os.path.join(one["dir"], "report.txt"))),
        }
        for k, b in self.ex_btns.items():
            b.configure(state="normal" if st[k] else "disabled")
        todo = [r for r in self.ex_all_rows if r["code"] not in ("done",)]
        self.b_ex_all.configure(state="normal" if todo and not self.busy else "disabled")

    def _ready_to_export(self, cfg):
        if self.busy:
            return False
        if not self.worker.client:
            messagebox.showinfo("Нужно подключение", "Сначала подключитесь к Telegram на странице «Подключение».",
                                parent=self.root)
            self.show_page("conn")
            return False
        if not cfg["out_root"]:
            messagebox.showinfo("Папка", "Выберите папку, куда сохранять экспорт.", parent=self.root)
            self.show_page("export")
            self.pick_folder()
            return False
        return True

    def _launch_many(self, cfg, rows):
        chats = []
        names = {str(d[0]): (d[1], d[2]) for d in self.dialogs}
        for r in rows:
            pid = self.peer_of(r)
            if pid:
                nm, kind = names.get(pid, (r["name"], ""))
                chats.append((pid, nm, kind))
        if not chats:
            messagebox.showinfo("Нет чатов", "Не удалось определить ID выбранных чатов.", parent=self.root)
            return
        cfg = json.loads(json.dumps(cfg))
        cfg["all_skip_done"] = False
        self._begin_job()
        self.last_job = ("all", cfg, chats, None)
        self.job = self.worker.submit(self.worker.export_all(cfg, chats))

    def ex_continue(self):
        cfg = self.collect_cfg()
        sel = self.ex_selected()
        if not sel or not self._ready_to_export(cfg):
            return
        if len(sel) > 1:
            if messagebox.askyesno("Продолжить экспорт", f"Продолжить экспорт выбранных чатов ({len(sel)}) "
                                   "по очереди?", parent=self.root):
                self._launch_many(cfg, sel)
            return
        pid = self.peer_of(sel[0])
        if not pid:
            messagebox.showinfo("Чат", "Не удалось определить ID чата.", parent=self.root)
            return
        self.start_single(cfg, pid, None)

    def ex_restart(self):
        cfg = self.collect_cfg()
        sel = self.ex_selected()
        if len(sel) != 1 or not self._ready_to_export(cfg):
            return
        pid = self.peer_of(sel[0])
        if pid:
            self.start_single(cfg, pid, "full")

    def ex_continue_unfinished(self):
        cfg = self.collect_cfg()
        rows = [r for r in self.ex_all_rows if r["code"] not in ("done",)]
        if not rows or not self._ready_to_export(cfg):
            return
        if messagebox.askyesno("Продолжить незавершённые",
                               f"Продолжить все незавершённые чаты ({len(rows)}) по очереди?\n\n"
                               "Каждый продолжится с места остановки; ошибки в отдельных чатах не остановят "
                               "остальные.", parent=self.root):
            self._launch_many(cfg, rows)

    def ex_versions(self):
        sel = self.ex_selected()
        if len(sel) == 1 and sel[0].get("dir"):
            self.show_versions(sel[0])

    def show_versions(self, row):
        """Окно «Версии сканирования» чата: снимки истории по датам; из любого можно собрать экспорт."""
        S = self.S
        snaps = list_snapshots(row["dir"]) if row.get("dir") else []
        d, body, btns = self._modal("Версии сканирования")
        ttk.Label(body, text=row.get("name") or os.path.basename(row["dir"]), style="Value.TLabel").pack(anchor="w")
        ttk.Label(body, text="Снимок истории сохраняется после каждого завершённого сканирования. Из любой версии "
                             "можно собрать экспорт — в нём будут и сообщения, которые потом удалили. HTML и JSON "
                             "в папке чата будут пересобраны по выбранной версии; уже скачанные файлы не качаются "
                             "заново (файлы удалённых сообщений Telegram может уже не отдать).",
                  style="Muted.TLabel", wraplength=S(620), justify="left").pack(anchor="w", pady=(S(6), S(12)))
        fnt = self.ui.f_body

        def W(sample):
            return fnt.measure(sample) + S(26)
        box, tv = self.make_tree(body, (("when", "Сканирование", W("23.11.2026 02:28 · последняя"), "w", False),
                                        ("msgs", "Сообщений", W(tr("Сообщений")) + S(12), "e", False),
                                        ("files", "Файлов", W(tr("Файлов")) + S(12), "e", False),
                                        ("size", "Объём файлов", W(tr("Объём файлов")) + S(12), "e", False),
                                        ("disk", "Занимает", W(tr("Занимает")) + S(12), "e", False)),
                                 selectmode="browse", height=min(8, max(3, len(snaps))), key="versions")
        box.pack(fill="both", expand=True)
        for i, m in enumerate(snaps):
            media = m.get("media") or {}
            try:
                nbytes = sum(int(v[1]) for v in media.values())
            except Exception:
                nbytes = 0
            try:
                disk = os.path.getsize(os.path.join(row["dir"], VERSIONS_DIR, m["id"], "records.jsonl.gz"))
            except OSError:
                disk = 0
            when = m.get("created", m["id"]) + (" · последняя" if i == 0 else "")
            tv.insert("", "end", iid=m["id"], values=(when, f"{m.get('total_msgs', 0):,}".replace(",", " "),
                                                      f"{len(media):,}".replace(",", " "),
                                                      human(nbytes) if nbytes else "—", human(disk) if disk else "—"))
        if snaps:
            tv.selection_set(snaps[0]["id"])
            tv.focus(snaps[0]["id"])
        else:
            ttk.Label(body, text="Версий пока нет — первая появится после завершённого сканирования.",
                      style="Muted.TLabel").pack(anchor="w", pady=(S(8), 0))

        def chosen():
            s = tv.selection()
            return s[0] if s else None

        def go():
            vid = chosen()
            if not vid:
                return
            cfg = self.collect_cfg()
            if not self._ready_to_export(cfg):
                return
            pid = self.peer_of(row)
            if not pid:
                messagebox.showinfo("Чат", "Не удалось определить ID чата.", parent=d)
                return
            label = tv.set(vid, "when").split(" · ")[0]
            if not messagebox.askyesno("Экспорт из версии",
                                       f"Собрать экспорт из версии от {label}?\n\nHTML и JSON в папке чата "
                                       "будут пересобраны по этой версии. Сама версия и остальные версии "
                                       "не изменятся.", parent=d):
                return
            d.destroy()
            self.start_single(cfg, pid, vid)

        def open_dir():
            vid = chosen()
            p = os.path.join(row["dir"], VERSIONS_DIR, vid) if vid else os.path.join(row["dir"], VERSIONS_DIR)
            if os.path.exists(p):
                open_path(p)

        ttk.Button(btns, text="Закрыть", command=d.destroy).pack(side="right")
        b_go = ttk.Button(btns, text="Экспорт из этой версии", style="Accent.TButton", command=go)
        b_go.pack(side="right", padx=(0, S(8)))
        b_dir = ttk.Button(btns, text="Открыть папку версии", command=open_dir)
        b_dir.pack(side="left")
        if not snaps:
            b_go.configure(state="disabled")
            b_dir.configure(state="disabled")
        elif self.busy:
            b_go.configure(state="disabled")
            ttk.Label(btns, text="Идёт экспорт — дождитесь окончания.", style="Small.TLabel").pack(
                side="left", padx=(S(12), 0))
        tv.bind("<Double-1>", lambda e: go() if tv.identify_region(e.x, e.y) == "cell" else None)
        tv.bind("<Return>", lambda e: go())
        d.bind("<Escape>", lambda e: d.destroy())
        d.resizable(True, True)
        self._show_modal(d)

    def ex_double(self, e):
        """Двойной щелчок: по колонке «Версий» — окно версий, по остальным — открыть HTML."""
        tv = self.ex_tv
        if tv.identify_region(e.x, e.y) != "cell":
            return
        try:
            col = tv.column(tv.identify_column(e.x), "id")
        except tk.TclError:
            col = ""
        if col == "ver":
            self.ex_versions()
        else:
            self.ex_open("html")

    def ex_open(self, what):
        sel = self.ex_selected()
        if len(sel) != 1 or not sel[0]["dir"]:
            return
        d = sel[0]["dir"]
        p = {"html": os.path.join(d, "messages.html"), "dir": d, "report": os.path.join(d, "report.txt")}[what]
        if os.path.exists(p):
            open_path(p)

    # ---------- страница «Подключение» ----------
    def _build_conn_page(self):
        S = self.S
        body, actions, sub = self._page("conn", "Подключение")
        col = ttk.Frame(body)
        col.grid(row=0, column=0, sticky="nw")
        col.columnconfigure(1, weight=1)
        self.vline(body).grid(row=0, column=1, sticky="ns", padx=S(36))
        pcol = ttk.Frame(body)        # справа — прокси: как подключаться; слева — кто входит
        pcol.grid(row=0, column=2, sticky="nw")

        self.section(col, "Аккаунт", 0, top=0, colspan=2)
        self.l_conn_status = ttk.Label(col, text="", style="Value.TLabel")
        self.l_conn_status.grid(row=1, column=0, columnspan=2, sticky="w")
        self.l_conn_hint = ttk.Label(col, text="", style="Muted.TLabel", wraplength=S(420), justify="left")
        self.l_conn_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(S(2), 0))
        holder = ttk.Frame(col)
        holder.grid(row=3, column=0, columnspan=2, sticky="w", pady=(S(12), 0))
        bf = ttk.Frame(holder)
        bf.pack(side="top", anchor="w")
        self.b_connect = ttk.Button(bf, text="Подключиться", style="Accent.TButton", command=self.on_connect)
        self.b_connect.pack(side="left")
        self.b_logout = ttk.Button(bf, text="Выйти из аккаунта", command=self.on_logout)
        self.b_logout.pack(side="left", padx=(S(8), 0))
        self.b_conn_cancel = ttk.Button(bf, text="Отмена", command=self.on_stop)
        # ход подключения — отдельной строкой под кнопками, с переносом: длинный текст не раздвигает колонку
        self.l_conn_phase = ttk.Label(holder, text="", style="Small.TLabel", wraplength=S(420), justify="left")
        self.l_conn_phase.pack(side="top", anchor="w", pady=(S(8), 0))

        self.section(col, "Telegram API", 4, top=24, colspan=2)
        self.field_row(col, 5, "API ID", self.v_api_id, width=20)
        _, self.e_hash = self.field_row(col, 6, "API hash", self.v_api_hash, width=40, show="•")
        hf = ttk.Frame(col)
        hf.grid(row=7, column=1, sticky="w")
        self.b_hash = ttk.Button(hf, text="Показать hash", style="Link.TButton", command=self.toggle_hash)
        self.b_hash.pack(side="left", padx=(0, S(8)))
        ttk.Button(hf, text="Где взять API ID и hash →", style="Link.TButton",
                   command=lambda: webbrowser.open("https://my.telegram.org/apps")).pack(side="left")

        self.section(col, "Режим экспорта Telegram", 8, top=24, colspan=2)
        self.l_tko_status = ttk.Label(col, text="", style="Muted.TLabel", wraplength=S(420), justify="left")
        self.l_tko_status.grid(row=9, column=0, columnspan=2, sticky="w")
        tb = ttk.Frame(col)
        tb.grid(row=10, column=0, columnspan=2, sticky="w", pady=(S(8), 0))
        ttk.Button(tb, text="Подробнее…", command=self.show_takeout_info).pack(side="left")
        self.b_tko_check = ttk.Button(tb, text="Проверить сейчас", command=self.check_takeout)
        self.b_tko_check.pack(side="left", padx=(S(8), 0))

        self.section(pcol, "Прокси", 0, top=0, colspan=2)
        Segmented(pcol, self.v_ptype, [("", "Без прокси"), ("mtproto", "MTProto"),
                                       ("socks5", "SOCKS5")], command=self.update_proxy_state,
                  k=self.ui.k).grid(row=1, column=0, columnspan=2, sticky="w")
        self.l_proxy_hint = ttk.Label(pcol, text="", style="Muted.TLabel", wraplength=S(440), justify="left")
        self.l_proxy_hint.grid(row=2, column=0, columnspan=2, sticky="w", pady=(S(8), S(4)))
        self.proxy_rows = {}
        for i, (key, label, var, width, show) in enumerate((
                ("link", "Ссылка", self.v_plink, 44, None),
                ("host", "Сервер", self.v_phost, 26, None),
                ("port", "Порт", self.v_pport, 10, None),
                ("secret", "Секрет", self.v_psecret, 44, None),
                ("user", "Логин", self.v_puser, 26, None),
                ("pass", "Пароль", self.v_ppass, 26, "•"))):
            self.proxy_rows[key] = self.field_row(pcol, 3 + i, label, var, width=width, show=show)

        self.section(pcol, "Проверка связи", 9, top=24, colspan=2)
        ttk.Label(pcol, text="«Проверить подключение» — пинг и скорость выбранного сейчас способа. "
                             "«Диагностика» сравнит способы по шагам: сначала с включённым VPN, потом попросит "
                             "выключить VPN и проверит прокси, и подскажет, где быстрее.",
                  style="Muted.TLabel", wraplength=S(460), justify="left").grid(row=10, column=0, columnspan=2,
                                                                                  sticky="w")
        db = ttk.Frame(pcol)
        db.grid(row=11, column=0, columnspan=2, sticky="w", pady=(S(10), S(8)))
        self.b_check = ttk.Button(db, text="Проверить подключение", command=self.on_check_current)
        self.b_check.pack(side="left")
        self.b_diag = ttk.Button(db, text="Диагностика…", command=self.on_diagnose)
        self.b_diag.pack(side="left", padx=(S(8), 0))
        self.l_diag_note = ttk.Label(db, text="", style="Small.TLabel", wraplength=S(300), justify="left")
        self.l_diag_note.pack(side="left", padx=(S(12), 0))
        fnt = self.ui.f_body

        def W(sample):
            return fnt.measure(sample) + S(22)
        dbox, self.diag_tv = self.make_tree(pcol, (
            ("when", "Когда", W("23:59"), "w", False), ("var", "Вариант", W("MTProto 127.0.0.1:1443"), "w", False),
            ("ping", "Пинг", W("9999 мс"), "e", False), ("speed", "Скорость", W("99.9 МБ/с"), "e", False),
            ("res", "Итог", W("не подключилось"), "w", False)), selectmode="browse", height=4, key="diag")
        dbox.grid(row=12, column=0, columnspan=2, sticky="ew")
        bb = ttk.Frame(pcol)
        bb.grid(row=13, column=0, columnspan=2, sticky="w", pady=(S(8), 0))
        self.l_diag_best = ttk.Label(bb, text="", style="Success.TLabel", wraplength=S(420), justify="left")
        self.l_diag_best.pack(side="left")
        self.b_diag_use = ttk.Button(bb, text="Выбрать этот вариант", style="Link.TButton", command=self.use_best)
        self.diag_rows = {}
        self.diag_best = None

    def fill_from_link(self):
        """Ссылка tg://proxy?server=…&port=…&secret=… (или https://t.me/proxy?…) → поля сервер, порт, секрет."""
        link = self.v_plink.get().strip()
        if not link:
            return
        try:
            q = parse_qs(urlparse(link.replace("tg://proxy?", "https://t.me/proxy?")).query)
        except Exception:
            return
        for key, var in (("server", self.v_phost), ("port", self.v_pport), ("secret", self.v_psecret)):
            val = (q.get(key) or [""])[0].strip()
            if val and var.get() != val:
                var.set(val)
        if q.get("server") and self.v_ptype.get() != "mtproto" and (q.get("secret") or [""])[0]:
            self.v_ptype.set("mtproto")
            self.update_proxy_state()

    def diag_variants(self, cfg):
        """Что проверять: без прокси (напрямую или через VPN) и настроенные прокси."""
        out = [("VPN / без прокси", "", dict(cfg, proxy_type=""))]
        if cfg.get("proxy_link") or cfg.get("proxy_secret"):
            out.append((f"MTProto {cfg.get('proxy_host')}:{cfg.get('proxy_port')}", "mtproto",
                        dict(cfg, proxy_type="mtproto")))
        if cfg.get("proxy_type") == "socks5" or cfg.get("proxy_user"):
            out.append((f"SOCKS5 {cfg.get('proxy_host')}:{cfg.get('proxy_port')}", "socks5",
                        dict(cfg, proxy_type="socks5")))
        return out

    def _diag_cfg(self):
        try:
            cfg = self.collect_cfg()
            int(cfg["api_id"])
            if not cfg["api_hash"]:
                raise ValueError
            return cfg
        except (ValueError, tk.TclError):
            messagebox.showwarning("Нужны API ID и API hash", "Для проверки связи заполните API ID и API hash.",
                                   parent=self.root)
            return None

    def on_check_current(self):
        """Проверить только тот способ подключения, который выбран сейчас."""
        cfg = self._diag_cfg()
        if cfg is None:
            return
        kind = cfg.get("proxy_type") or ""
        variants = [v for v in self.diag_variants(cfg) if v[1] == kind]
        if not variants:
            label = {"mtproto": "MTProto", "socks5": "SOCKS5"}.get(kind, kind)
            variants = [(f"{label} {cfg.get('proxy_host')}:{cfg.get('proxy_port')}", kind, dict(cfg))]
        self.diag_kind = {label: k for label, k, _ in variants}
        self._diag_chats = [c for c in [self.v_chat.get().strip()] if c] + [str(d[0]) for d in self.dialogs[:8]]
        self._diag_next = None
        self.run_diag(variants)

    def on_diagnose(self):
        try:
            cfg = self.collect_cfg()
            int(cfg["api_id"])
            if not cfg["api_hash"]:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showwarning("Нужны API ID и API hash", "Для проверки связи заполните API ID и API hash.",
                                   parent=self.root)
            return
        variants = self.diag_variants(cfg)
        self.diag_kind = {label: kind for label, kind, _ in variants}
        direct = [v for v in variants if v[1] == ""]
        proxies = [v for v in variants if v[1] != ""]
        self._diag_chats = [c for c in [self.v_chat.get().strip()] if c] + [str(d[0]) for d in self.dialogs[:8]]
        self._diag_next = proxies
        if proxies:
            if not messagebox.askokcancel(
                    "Диагностика — шаг 1 из 2",
                    "Шаг 1: VPN.\n\nВключите VPN (если пользуетесь им) и нажмите «ОК» — проверю подключение "
                    "без прокси.\n\nПотом попрошу выключить VPN и проверю прокси: VPN и прокси одновременно "
                    "мешают друг другу, поэтому замеры идут по очереди.", parent=self.root):
                return
        self.run_diag(direct)

    def run_diag(self, variants, reuse_media=False):
        self.b_diag.configure(state="disabled")
        self.b_check.configure(state="disabled")
        self.l_diag_note.configure(text="Проверяю… это займёт до полуминуты на вариант.")
        coro = self.worker.diagnose([(label, c) for label, _, c in variants], self._diag_chats,
                                    reuse_media=reuse_media)

        async def safe():
            try:
                await coro
            except Exception as e:
                self.q.put(("diag_note", f"Проверка не удалась: {e}"))
                self.q.put(("diag_done",))
        asyncio.run_coroutine_threadsafe(safe(), self.worker.loop)

    def on_diag_event(self, item):
        kind = item[0]
        if kind == "diag_note":
            self.l_diag_note.configure(text=item[1])
            return
        if kind == "diag_done":
            nxt, self._diag_next = getattr(self, "_diag_next", None), None
            if nxt and not self.l_diag_note.cget("text").startswith("Проверка не"):
                names = ", ".join(label for label, _, _ in nxt)
                if messagebox.askokcancel(
                        "Диагностика — шаг 2 из 2",
                        f"Шаг 2: прокси ({names}).\n\nТеперь ВЫКЛЮЧИТЕ VPN и запустите прокси"
                        ", затем нажмите «ОК».", parent=self.root):
                    self.run_diag(nxt, reuse_media=True)
                    return
            self.b_diag.configure(state="normal")
            self.b_check.configure(state="normal")
            if not self.l_diag_note.cget("text").startswith("Проверка не"):
                self.l_diag_note.configure(text="Готово. Можно переключить VPN/прокси и проверить ещё раз.")
            self.pick_best()
            return
        _, label, state, res = item
        tv = self.diag_tv
        if state == "running":
            iid = f"{label}|{time.time()}"
            self.diag_rows[label] = iid
            tv.insert("", 0, iid=iid, values=(time.strftime("%H:%M"), label, "", "", "проверяю…"))
            return
        iid = self.diag_rows.get(label)
        if not iid or not tv.exists(iid):
            return
        ping = f"{res['ping']:.0f} мс" if res.get("ping") is not None else "—"
        speed = f"{human(res['speed'])}/с" if res.get("speed") else ("—" if res.get("ok") else "")
        verdict = "работает" if res.get("ok") else "не подключилось"
        tv.item(iid, values=(time.strftime("%H:%M"), label, ping, speed, verdict),
                tags=(() if res.get("ok") else ("failed",)))
        res["kind"] = getattr(self, "diag_kind", {}).get(label, "")
        res["when"] = time.strftime("%H:%M")
        self.diag_results = [r for r in getattr(self, "diag_results", []) if r["iid"] != iid] + [dict(res, iid=iid)]

    def pick_best(self):
        ok = [r for r in getattr(self, "diag_results", []) if r.get("ok")]
        if not ok:
            self.l_diag_best.configure(text="Ни один вариант не подключился — проверьте интернет, VPN или прокси.",
                                       style="Danger.TLabel")
            self.b_diag_use.pack_forget()
            return
        with_speed = [r for r in ok if r.get("speed")]
        best = max(with_speed, key=lambda r: r["speed"]) if with_speed else min(ok, key=lambda r: r["ping"] or 1e9)
        self.diag_best = best
        what = f"{human(best['speed'])}/с, пинг {best['ping']:.0f} мс" if best.get("speed") else \
            f"пинг {best['ping']:.0f} мс"
        self.l_diag_best.configure(text=f"Быстрее всего: {best['label']} ({best['when']}) — {what}",
                                   style="Success.TLabel")
        if best.get("kind") != self.v_ptype.get():
            self.b_diag_use.pack(side="left", padx=(self.S(8), 0))
        else:
            self.b_diag_use.pack_forget()

    def use_best(self):
        if self.diag_best:
            self.v_ptype.set(self.diag_best.get("kind", ""))
            self.update_proxy_state()
            self.save_cfg()
            self.b_diag_use.pack_forget()
            self.l_diag_note.configure(text="Вариант выбран. Нажмите «Подключиться»/«Переподключиться», "
                                            "чтобы применить его.")

    def toggle_hash(self):
        hidden = str(self.e_hash.cget("show")) != ""
        self.e_hash.configure(show="" if hidden else "•")
        self.b_hash.configure(text="Скрыть hash" if hidden else "Показать hash")

    PROXY_HINTS = {
        "": "Подключение напрямую. Подходит, если VPN включён на весь компьютер.",
        "mtproto": "Вставьте ссылку на прокси (tg://proxy?… или https://t.me/proxy?…) или заполните сервер, "
                   "порт и секрет вручную. Подойдёт любой MTProto-прокси, в том числе локальный tg-ws-proxy.",
        "socks5": "Локальный SOCKS5-порт VPN-клиента или обычный SOCKS5-прокси.",
    }

    def update_proxy_state(self):
        t = self.v_ptype.get()
        shown = {"": set(), "mtproto": {"link", "host", "port", "secret"},
                 "socks5": {"host", "port", "user", "pass"}}.get(t, set())
        for k, (l, e) in self.proxy_rows.items():
            if k in shown:
                l.grid()
                e.grid()
            else:
                l.grid_remove()
                e.grid_remove()
        self.l_proxy_hint.configure(text=self.PROXY_HINTS.get(t, ""))
        if t == "socks5" and self.v_pport.get() == "1443":
            self.v_pport.set("1080")
        if t == "mtproto" and self.v_pport.get() == "1080":
            self.v_pport.set("1443")

    def proxy_changed(self):
        """Настройки прокси в полях отличаются от тех, с которыми идёт (или шло) подключение."""
        used = getattr(self.worker, "conn_target", None)
        if used is None:
            return False
        try:
            return proxy_target(self.collect_cfg()) != used
        except Exception:
            return False

    def update_account(self):
        S = self.S
        via = getattr(self.worker, "conn_via", None)
        if self.connecting and self.busy:
            dot, name = "DotBusy.TLabel", "Подключение…"
            status = f"Подключение… ({via})" if via else "Подключение…"
            hint = "Если откроется окно с QR-кодом, отсканируйте его в Telegram на телефоне."
        elif self.account:
            dot, name = "DotOn.TLabel", self.account
            status = f"Подключено: {self.account}" + (f" ({via})" if via else "")
            hint = "Сессия сохранена — при следующем запуске программа подключится сама."
        else:
            dot, name = "DotOff.TLabel", "Не подключено"
            status = "Не подключено"
            hint = ("Вход по QR-коду или по номеру телефона. Нужны API ID и API hash — "
                    "их выдают бесплатно на my.telegram.org.")
        self.l_acc_dot.configure(style=dot)
        self.l_acc_name.configure(text=name)
        self.l_conn_status.configure(text=status, style="Value.TLabel")
        hint_style = "Muted.TLabel"
        if (self.account or self.connecting) and self.proxy_changed():
            hint = ("Настройки прокси изменены, но ещё не применены (сейчас подключение: "
                    f"{via}). Нажмите «Переподключиться».")
            hint_style = "Warning.TLabel"
        self.l_conn_hint.configure(text=hint, style=hint_style)
        exporting = self.busy and self.stoppable and not self.connecting
        reconnect = self.account or (self.connecting and self.busy)
        self.b_connect.configure(text="Переподключиться" if reconnect else "Подключиться",
                                 state="normal" if (not self.busy or exporting or self.connecting) else "disabled")
        can_logout = (self.account or os.path.exists(SESSION_PATH + ".session")) and not self.busy
        self.b_logout.configure(state="normal" if can_logout else "disabled")
        if self.connecting and self.busy:
            self.b_conn_cancel.pack(side="left", padx=(S(8), 0))
        else:
            self.b_conn_cancel.pack_forget()
            self.l_conn_phase.configure(text="")

    # ---------- страница «Настройки» ----------
    def _build_settings_page(self):
        S = self.S
        body, actions, sub = self._page("settings", "Настройки")
        col = ttk.Frame(body)
        col.grid(row=0, column=0, sticky="nw")

        ttk.Label(col, text="Здесь — настройки самой программы. Всё, что касается экспорта (папка, форматы, "
                            "скорость скачивания), — на странице «Экспорт».", style="Muted.TLabel",
                  wraplength=S(560), justify="left").grid(row=0, column=0, columnspan=3, sticky="w")

        self.section(col, "Язык", 11, top=28, colspan=3)
        ttk.Label(col, text="Язык программы", style="Muted.TLabel").grid(row=12, column=0, sticky="w",
                                                                       padx=(0, S(16)))
        Segmented(col, self.v_language, [("system", "Как в системе"), ("ru", "Русский"), ("en", "English")],
                  command=self.on_language, k=self.ui.k).grid(row=12, column=1, columnspan=2, sticky="w", pady=S(4))

        self.section(col, "Оформление", 9, top=24, colspan=3)
        ttk.Label(col, text="Тема", style="Muted.TLabel").grid(row=10, column=0, sticky="w", padx=(0, S(16)))
        Segmented(col, self.v_theme, [("light", "Светлая"), ("dark", "Тёмная"), ("system", "Как в системе")],
                  command=self.on_theme, k=self.ui.k).grid(row=10, column=1, columnspan=2, sticky="w", pady=S(4))

    def on_language(self):
        self.save_cfg()
        new = system_lang() if self.v_language.get() == "system" else self.v_language.get()
        if new == LANG:
            return
        if self.busy:
            messagebox.showinfo("Язык", "Язык изменится после перезапуска программы.", parent=self.root)
            return
        if messagebox.askyesno("Язык", "Чтобы сменить язык, программу нужно перезапустить. Перезапустить сейчас?",
                               parent=self.root):
            self.on_close(restart=True)

    def on_theme(self):
        self.ui.apply(self.theme_mode())
        self.apply_theme_tk()
        self.save_cfg()

    def apply_theme_tk(self):
        c = self.ui.c
        alive = []
        for w, fn in self.themed:
            try:
                if w.winfo_exists():
                    fn(c)
                    alive.append((w, fn))
            except tk.TclError:
                pass
        self.themed = alive
        self.trees = [tv for tv in self.trees if tv.winfo_exists()]   # таблицы закрытых окон — убираем
        for tv in self.trees:
            tv.tag_configure("failed", foreground=c["danger"])
            tv.tag_configure("running", foreground=c["accent"])
        for cb in self.combos:      # выпадающие списки создаются один раз — перекрашиваем вручную
            try:
                pd = cb.tk.call("ttk::combobox::PopdownWindow", cb)
                cb.tk.call(f"{pd}.f.l", "configure", "-background", c["field"], "-foreground", c["text"],
                           "-selectbackground", c["accent_soft"], "-selectforeground", c["text"],
                           "-borderwidth", 0)
            except tk.TclError:
                pass
        dark = self.ui.mode == "dark"
        for w in [self.root] + [x for x in (self.report_win, self.qr_win) if x is not None]:
            try:
                if w.winfo_exists():
                    w.configure(background=c["bg"])
                    set_titlebar_theme(w, dark)
            except tk.TclError:
                pass

    # ---------- панель активности и журнал ----------
    def layout_stats(self, ncols):
        """Показатели в 3 колонки на широком окне и в 2 — на узком (чтобы значения не обрезались)."""
        if ncols == self._stat_cols:
            return
        self._stat_cols = ncols
        S, sf = self.S, self.stats_frame
        for col in range(6):
            sf.columnconfigure(col, weight=0)
        for i, (lab, v) in enumerate(self.stat_cells):
            r, col = divmod(i, ncols)
            lab.grid(row=r, column=col * 2, sticky="w", padx=(0, S(10)), pady=S(2))
            v.grid(row=r, column=col * 2 + 1, sticky="w", padx=(0, S(20)), pady=S(2))
            sf.columnconfigure(col * 2 + 1, weight=1)

    def set_phase(self, text):
        self.l_phase.configure(text=text)
        if self.connecting and hasattr(self, "l_conn_phase"):
            self.l_conn_phase.configure(text=text.split(" — ")[0])
            self.update_account()          # статус «Подключение… (через что)»

    def update_activity(self):
        """Прогресс и показатели видны только во время работы или после неё — без пустых «—» в покое."""
        st = self.worker.stats
        active = bool(st.get("running")) or self.out_dir is not None
        for w in (self.pb, self.stats_frame):
            if active:
                w.grid()
            else:
                w.grid_remove()
        if active:
            self.l_idle.grid_remove()
        else:
            self.l_idle.grid()
        if self.busy and self.stoppable and not self.connecting:
            self.b_stop.grid()
        else:
            self.b_stop.grid_remove()
        job = ""
        if st.get("chat_total"):
            job = f"{st.get('chat_name', '')} · чат {st['chat_i']} из {st['chat_total']}"
        elif self.out_dir:
            job = os.path.basename(self.out_dir)
        if self.l_job.cget("text") != job:
            self.l_job.configure(text=job)

    def _bind_keys(self):
        r = self.root
        for i, (key, _t) in enumerate(self.NAV, start=1):
            r.bind_all(f"<Control-Key-{i}>", lambda e, k=key: self.show_page(k))
        r.bind_all("<Control-KeyPress>", self._ctrl_key, add="+")
        for cls in ("TEntry", "Entry", "Text", "TCombobox", "TSpinbox"):
            r.bind_class(cls, "<Button-3>", self._edit_menu, add="+")
        r.bind_all("<F5>", lambda e: self.refresh_exports() if self.page == "exports" else None)

    def _ctrl_key(self, e):
        # по коду клавиши, а не символу — работает и в русской раскладке
        kc = getattr(e, "keycode", 0)
        w = e.widget
        if isinstance(w, (tk.Entry, tk.Text)):
            ks = (getattr(e, "keysym", "") or "").lower()
            clip = {67: ("c", "<<Copy>>"), 86: ("v", "<<Paste>>"), 88: ("x", "<<Cut>>")}
            if kc in clip:
                if ks != clip[kc][0]:            # не латинская раскладка — Tk сам не поймёт
                    w.event_generate(clip[kc][1])
                return "break" if ks != clip[kc][0] else None
            if kc == 65:                          # Ctrl+A — выделить всё (в любой раскладке)
                self._select_all(w)
                return "break"
        if kc == 70:          # Ctrl+F — поиск чата
            self.show_page("export")
            self.e_search.focus_set()
            self.e_search.select_range(0, "end")
            return "break"
        if kc == 76:          # Ctrl+L — журнал
            self.show_page("exports")
            self.v_details_tab.set("log")
            self.show_details_tab()
            return "break"
        if kc == 13 and self.page == "export" and not self.busy:   # Ctrl+Enter — начать экспорт
            self.on_start()
            return "break"
        return None

    @staticmethod
    def _select_all(w):
        try:
            if isinstance(w, tk.Text):
                w.tag_add("sel", "1.0", "end-1c")
                w.mark_set("insert", "end-1c")
            else:
                w.selection_range(0, "end")
                w.icursor("end")
        except tk.TclError:
            pass

    def _edit_menu(self, e):
        """Меню правой кнопки в полях ввода и журнале: вырезать / копировать / вставить / выделить всё."""
        w = e.widget
        if not isinstance(w, (tk.Entry, tk.Text)):
            return
        try:
            w.focus_set()
        except tk.TclError:
            pass
        readonly = isinstance(w, tk.Text) and str(w.cget("state")) == "disabled"
        try:
            readonly = readonly or str(w.cget("state")) in ("disabled", "readonly")
        except tk.TclError:
            pass
        m = tk.Menu(self.root, tearoff=0)
        c = self.ui.c
        m.configure(background=c["field"], foreground=c["text"], activebackground=c["accent_soft"],
                    activeforeground=c["text"], disabledforeground=c["faint"], relief="flat", bd=1)
        st = "disabled" if readonly else "normal"
        m.add_command(label="Вырезать", state=st, command=lambda: w.event_generate("<<Cut>>"))
        m.add_command(label="Копировать", command=lambda: w.event_generate("<<Copy>>"))
        m.add_command(label="Вставить", state=st, command=lambda: w.event_generate("<<Paste>>"))
        m.add_separator()
        m.add_command(label="Выделить всё", command=lambda: self._select_all(w))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def _place_window(self):
        r, S = self.root, self.S
        self.page_min = {}
        need_w = page_h = 0
        for key, p in self.pages.items():      # окно должно вмещать самую крупную страницу
            p.grid()
            r.update_idletasks()
            self.page_min[key] = p.winfo_reqheight()
            page_h = max(page_h, p.winfo_reqheight())
            if key != "exports":               # широкая таблица экспортов прокручивается по горизонтали
                need_w = max(need_w, p.winfo_reqwidth())
            p.grid_remove()
        min_w = self.sidebar.winfo_reqwidth() + 1 + need_w
        min_h = page_h
        sw, sh = r.winfo_screenwidth(), r.winfo_screenheight()
        w = min(max(S(1300), min_w), int(sw * 0.94))
        h = min(max(S(800), min_h + S(40)), int(sh * 0.9))
        r.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 3)}")
        r.minsize(min(min_w, w), min(min_h, h))
        set_titlebar_theme(r, self.ui.mode == "dark")

    def log(self, text):
        self.flush_logs([time.strftime("[%H:%M:%S] ") + tr(text) + "\n"])

    def flush_logs(self, lines):
        """Добавляет строки в окно одной операцией (а не по одной) — окно не тормозит."""
        if not lines:
            return
        box = self.logbox
        at_bottom = box.yview()[1] > 0.995
        box.configure(state="normal")
        box.insert("end", "".join(lines))
        total = int(box.index("end-1c").split(".")[0])
        if total > self.MAX_LOG_LINES:
            box.delete("1.0", f"{total - self.MAX_LOG_LINES}.0")
        box.configure(state="disabled")
        if at_bottom:            # автопрокрутка, только если вы не листаете лог вверх
            box.see("end")

    def open_log(self):
        p = os.path.join(self.out_dir or "", "export.log")
        if self.out_dir and os.path.exists(p):
            open_path(p)
        else:
            messagebox.showinfo("Журнал", "Файл журнала появится в папке чата после начала экспорта.",
                                parent=self.root)

    # ---------- настройки ----------
    def collect_cfg(self):
        c = self.cfg
        widths = dict(c.get("col_widths") or {})
        for key, (tv, names) in self.tree_cols.items():
            try:
                widths[key] = {n: int(tv.column(n, "width")) for n in names}
            except tk.TclError:
                pass
        details_h = c.get("details_h", 0)
        if self._sash_done:
            try:
                details_h = max(0, self.ex_pw.winfo_height() - self.ex_pw.sashpos(0))
            except tk.TclError:
                pass
        c.update(
            api_id=self.v_api_id.get().strip(), api_hash=self.v_api_hash.get().strip(),
            proxy_type=self.v_ptype.get(), proxy_link=self.v_plink.get().strip(),
            proxy_host=self.v_phost.get().strip(), proxy_port=self.v_pport.get().strip(),
            proxy_secret=self.v_psecret.get().strip(), proxy_user=self.v_puser.get().strip(),
            proxy_pass=self.v_ppass.get(),
            types={k: v.get() for k, v in self.v_types.items()},
            size_limit_mb=0 if self.v_unlimited.get() else max(1, min(4096, int(self.v_limit.get()))),
            out_root=self.v_out.get().strip(),
            parallel=int(self.v_parallel.get()), threads_per_file=int(self.v_threads.get()),
            stall_timeout=int(self.v_stall.get()),
            retries=int(self.v_retries.get()), make_html=self.v_html.get(), make_json=self.v_json.get(),
            last_chat=self.v_chat.get().strip(),
            auto_download=self.v_auto.get(),
            all_kinds={k: v.get() for k, v in self.v_all_kinds.items()},
            all_skip_done=self.v_all_skip.get(),
            theme=self.v_theme.get(),
            col_widths=widths, details_tab=self.v_details_tab.get(), details_h=details_h,
            multi_mode=self.v_multi_mode.get(), language=self.v_language.get(),
            graph_period=self.v_graph_period.get(),
        )
        return c

    def save_cfg(self):
        try:
            self.collect_cfg()
        except (tk.TclError, ValueError):
            pass
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:
            self.log(f"Не удалось сохранить настройки: {e}")

    def bad_number_setting(self):
        """Проверка числовых полей настроек — понятное сообщение вместо падения."""
        for var, name in ((self.v_limit, "Максимальный размер файла"),
                          (self.v_parallel, "Файлов одновременно"), (self.v_threads, "Потоков на большой файл"),
                          (self.v_stall, "Зависшим считать через"), (self.v_retries, "Повторов перед пропуском")):
            try:
                int(var.get())
            except (tk.TclError, ValueError):
                messagebox.showwarning("Проверьте настройки", f"В поле «{name}» должно быть целое число.",
                                       parent=self.root)
                if var is self.v_limit:
                    self.show_page("export")
                else:
                    self.open_more()
                return True
        return False

    # ---------- состояние «занято» ----------
    def set_busy(self, busy, stoppable=False):
        self.busy = busy
        if hasattr(self, "b_tko_check"):
            self.root.after_idle(self.update_takeout_ui)
        self.stoppable = busy and stoppable
        state = "disabled" if busy else "normal"
        self.b_start.configure(state=state)
        self.b_all.configure(state=state)
        self.update_ex_buttons()
        self.update_account()
        self.update_activity()

    def _begin_job(self):
        """Запуск экспорта: переходим в «Мои экспорты» — там видно ход работы."""
        self.save_cfg()
        self.samples.clear()
        self.msg_samples.clear()
        self.speed_hist.clear()
        self.scan_hist.clear()
        self.dl_eta = None
        self._select_running = True
        self.set_busy(True, stoppable=True)
        self.show_page("exports")
        self.refresh_exports()

    def on_connect(self):
        if self.busy and self.connecting:
            # подключение ещё идёт (возможно, через неработающий прокси) — начать заново с текущими настройками
            self._restart_connect = True
            self.set_phase("Переподключаюсь с текущими настройками…")
            if self.job and not self.job.done():
                self.job.cancel()
            return
        if self.busy and self.stoppable and not self.connecting:
            # идёт экспорт: приостановить, переподключиться с новыми настройками и продолжить с того же места
            if not messagebox.askyesno(
                    "Переподключиться?", "Экспорт ненадолго приостановится: программа переподключится с текущими "
                    "настройками и продолжит с того же места.\n\nПродолжить?", parent=self.root):
                return
            self._reconnect_after_stop = True
            self.on_stop()
            return
        if self.busy:
            return
        try:
            cfg = self.collect_cfg()
            int(cfg["api_id"])
            if not cfg["api_hash"]:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showwarning("Нужны API ID и API hash",
                                   "Заполните API ID (число) и API hash.\n"
                                   "Их можно получить на my.telegram.org → API development tools.", parent=self.root)
            self.show_page("conn")
            return
        self.save_cfg()
        self.connecting = True
        self.connect_t = time.monotonic()
        self.set_busy(True, stoppable=True)
        self.update_chat_empty()
        self.job = self.worker.submit(self.worker.connect(dict(cfg)))

    def on_logout(self):
        if self.busy:
            return
        if messagebox.askyesno("Выйти из аккаунта", "Выйти из аккаунта Telegram в этой программе?\n\n"
                               "Чтобы снова экспортировать чаты, понадобится войти заново.", parent=self.root):
            self.set_busy(True)
            self.job = self.worker.submit(self.worker.logout())

    def on_start(self):
        if self.busy:
            return
        if self.bad_number_setting():
            return
        cfg = self.collect_cfg()
        if not self.worker.client:
            messagebox.showinfo("Нужно подключение", "Сначала подключитесь к Telegram на странице «Подключение».",
                                parent=self.root)
            self.show_page("conn")
            return
        if not cfg["last_chat"] and not self.checked:
            messagebox.showinfo("Выберите чат", "Выберите чат в списке или введите его @username, ссылку или ID.",
                                parent=self.root)
            self.show_page("export")
            self.e_search.focus_set()
            return
        if not any(cfg["types"].values()):
            if not messagebox.askyesno("Без файлов", "Не выбран ни один тип файлов. Экспортировать только текст?",
                                       parent=self.root):
                return
        if not cfg["out_root"]:
            messagebox.showinfo("Папка", "Выберите папку, куда сохранять экспорт.", parent=self.root)
            self.show_page("export")
            self.pick_folder()
            return
        if len(self.checked) >= 2:        # несколько отмеченных чатов
            chats = [self.dialog_map[i] for i in self.checked if i in self.dialog_map]
            order = {str(d[0]): n for n, d in enumerate(self.dialogs)}
            chats.sort(key=lambda d: order.get(str(d[0]), 0))
            cfg2 = json.loads(json.dumps(cfg))
            cfg2["all_skip_done"] = False
            self._begin_job()
            self.last_job = ("all", cfg2, chats, None)
            self.job = self.worker.submit(self.worker.export_all(cfg2, chats))
            return
        if len(self.checked) == 1:
            only = next(iter(self.checked))
            if only != cfg["last_chat"]:
                self.start_single(cfg, only, None)
                return
        self.start_single(cfg, cfg["last_chat"], self.selected_version())

    def start_single(self, cfg, chat, version):
        if self.busy:
            return
        if version == "full" and not messagebox.askyesno(
                "Пересканировать всё заново?",
                "Вся история чата будет прочитана заново — на большом чате это займёт столько же времени, "
                "сколько первое сканирование.\n\nТекущая версия сохранится и останется в списке «Режим». "
                "Уже скачанные файлы повторно не качаются.\n\nПродолжить?", parent=self.root):
            return
        self._begin_job()
        self.last_job = ("single", json.loads(json.dumps(cfg)), str(chat), version)
        self.job = self.worker.submit(self.worker.export_auto(json.loads(json.dumps(cfg)), str(chat),
                                                              version=version))

    def on_start_all(self):
        """Окно «Экспорт всех чатов»: какие чаты, пропуск готовых, подтверждение — в одном месте."""
        if self.busy:
            return
        if self.bad_number_setting():
            return
        cfg = self.collect_cfg()
        if not self.worker.client or not self.dialogs:
            messagebox.showinfo("Нужно подключение", "Подключитесь к Telegram и дождитесь загрузки списка чатов.",
                                parent=self.root)
            self.show_page("conn")
            return
        if not cfg["out_root"]:
            messagebox.showinfo("Папка", "Выберите папку, куда сохранять экспорт.", parent=self.root)
            self.show_page("export")
            self.pick_folder()
            return
        S = self.S
        kinds = collections.Counter(d[2] for d in self.dialogs)
        d, body, btns = self._modal("Экспорт всех чатов")
        ttk.Label(body, text="Каждый чат сохранится в отдельную папку. Ошибка в одном чате не остановит "
                             "остальные, а после «Остановить» экспорт продолжится с того же места.",
                  style="Muted.TLabel", wraplength=S(440),
                  justify="left").pack(anchor="w")
        ttk.Label(body, text="Какие чаты", style="Section.TLabel").pack(anchor="w", pady=(S(16), S(4)))
        for k, label in (("Личный", "Личные"), ("Группа", "Группы"), ("Канал", "Каналы")):
            ttk.Checkbutton(body, text=f"{label} · {kinds.get(k, 0)}", variable=self.v_all_kinds[k]).pack(anchor="w")
        ttk.Checkbutton(body, text="Пропускать чаты, которые уже полностью готовы",
                        variable=self.v_all_skip).pack(anchor="w", pady=(S(10), 0))
        ttk.Label(body, text="Порядок", style="Section.TLabel").pack(anchor="w", pady=(S(16), S(4)))
        Segmented(body, self.v_multi_mode, [("sequential", "По очереди: скан → скачивание"),
                                            ("scan_first", "Сначала сканировать все")], k=self.ui.k).pack(anchor="w")
        ttk.Label(body, text="После сканирования", style="Section.TLabel").pack(anchor="w", pady=(S(16), S(4)))
        Segmented(body, self.v_auto, [(True, "Сразу скачивать"), (False, "Показать сводку")],
                  k=self.ui.k).pack(anchor="w")
        summary = ttk.Label(body, text="", style="Value.TLabel")
        summary.pack(anchor="w", pady=(S(16), 0))
        ttk.Label(body, text=f"Папка: {cfg['out_root']}", style="Small.TLabel", wraplength=S(440)).pack(anchor="w")

        def chosen():
            return [x for x in self.dialogs if (self.v_all_kinds.get(x[2]) is None or self.v_all_kinds[x[2]].get())]

        def upd(*a):
            n = len(chosen())
            summary.configure(text=f"Будет экспортировано чатов: {n}" if n else "Не выбран ни один вид чатов",
                              style="Value.TLabel" if n else "Danger.TLabel")
            b_go.configure(state="normal" if n else "disabled")

        def go():
            chats = chosen()
            if not chats:
                return
            d.destroy()
            cfg2 = json.loads(json.dumps(self.collect_cfg()))
            self._begin_job()
            self.last_job = ("all", cfg2, list(chats), None)
            self.job = self.worker.submit(self.worker.export_all(cfg2, list(chats)))

        ttk.Button(btns, text="Отмена", command=d.destroy).pack(side="right")
        b_go = ttk.Button(btns, text="Начать экспорт", style="Accent.TButton", command=go)
        b_go.pack(side="right", padx=(0, S(8)))
        traces = [(v, v.trace_add("write", upd)) for v in self.v_all_kinds.values()]
        upd()
        d.bind("<Escape>", lambda e: d.destroy())
        d.bind("<Return>", lambda e: go())
        b_go.focus_set()
        self._show_modal(d)
        for v, t in traces:
            try:
                v.trace_remove("write", t)
            except tk.TclError:
                pass

    def relaunch_last_job(self):
        """После переподключения продолжаем тот же экспорт с места остановки."""
        kind, cfg, target, version = self.last_job
        cfg = dict(cfg, **{k: self.cfg[k] for k in ("proxy_type", "proxy_link", "proxy_host", "proxy_port",
                                                    "proxy_secret", "proxy_user", "proxy_pass")})
        self.log("▶ Продолжаю экспорт после переподключения.")
        self._begin_job()
        if kind == "single":
            self.job = self.worker.submit(self.worker.export_auto(cfg, target,
                                                                  version=None if version == "full" else version))
        else:
            self.job = self.worker.submit(self.worker.export_all(cfg, target))

    def on_stop(self):
        if self.job and not self.job.done():
            self.set_phase("Останавливаю…")
            self.job.cancel()

    # ---------- галочки в списке чатов ----------
    def on_chat_click(self, e):
        if self.tv.identify_region(e.x, e.y) == "cell" and self.tv.identify_column(e.x) == "#3":
            iid = self.tv.identify_row(e.y)          # клик по «посчитать» — узнать число сообщений этого чата
            if iid and iid not in self.counts:
                self.counts[iid] = "?"
                self.tv.set(iid, "msgs", "считаю…")
                self.worker.request_count(iid)
        if self.tv.identify_region(e.x, e.y) == "tree":      # клик по квадратику — отметить, не выделяя
            iid = self.tv.identify_row(e.y)
            if iid:
                self.toggle_checks([iid])
            return "break"
        return None

    def toggle_checks(self, iids):
        iids = [i for i in iids if i]
        if not iids:
            return
        new = set(self.checked)
        if all(i in new for i in iids):
            new.difference_update(iids)
        else:
            new.update(iids)
        self.set_checks(new)

    def toggle_all_checks(self):
        vis = set(self.tv.get_children())
        self.set_checks(self.checked - vis if vis and vis <= self.checked else self.checked | vis)

    def set_checks(self, new):
        self.checked = set(new)
        for iid in self.tv.get_children():
            self.tv.item(iid, image=self.ui.tree_cb["on" if iid in self.checked else "off"])
        vis = set(self.tv.get_children())
        self.tv.heading("#0", image=self.ui.tree_cb["on" if vis and vis <= self.checked else "off"])
        self.update_chat_header()

    def on_pick_chat(self, _e=None):
        sel = self.tv.selection()
        if sel:
            chat = sel[0]
            if self.v_manual.get():
                self._picking = True
                self.v_manual.set("")
                self._picking = False
            if chat != self.v_chat.get():
                self.v_chat.set(chat)
            if chat not in self.counts:
                self.worker.request_count(chat)
            self.update_chat_header()

    def on_manual_chat(self):
        """Чат, введённый вручную, заменяет выбранный в списке."""
        if getattr(self, "_picking", False):
            return
        txt = self.v_manual.get().strip()
        if txt:
            if self.tv.selection():
                self.tv.selection_remove(*self.tv.selection())
            self.v_chat.set(txt)

    def show_count(self, chat):
        if self.tv.exists(chat):
            self.tv.set(chat, "msgs", self.count_text(chat))
        if chat == self.v_chat.get():
            self.update_chat_header()
        if not getattr(self, "_counts_save_pending", False):     # сохраняем не чаще раза в 5 с
            self._counts_save_pending = True

            def later():
                self._counts_save_pending = False
                self.save_counts()
            self.root.after(5000, later)

    def count_text(self, iid):
        n = self.counts.get(iid, "")
        if n == "":
            return "посчитать"
        if n == "?":
            return "считаю…"
        if n is None:
            return "—"
        return f"{n:,}".replace(",", " ")

    def sort_chats(self, col):
        cur, rev = self.chat_sort
        self.chat_sort = (col, not rev if cur == col else (col == "msgs"))
        for c, t in (("name", "Чат"), ("kind", "Тип"), ("msgs", "Сообщений")):
            mark = (" ▼" if self.chat_sort[1] else " ▲") if c == col else ""
            self.tv.heading(c, text=t + mark)
        self.fill_dialogs()

    def sorted_dialogs(self):
        col, rev = self.chat_sort
        if not col:
            return self.dialogs
        if col == "name":
            key = lambda d: d[1].lower()
        elif col == "kind":
            key = lambda d: (d[2], d[1].lower())
        else:
            key = lambda d: (self.counts.get(str(d[0])) if isinstance(self.counts.get(str(d[0])), int) else -1)
        return sorted(self.dialogs, key=key, reverse=rev)

    def count_all(self):
        """Сообщений во всех чатах списка — по одному запросу на чат, с прогрессом."""
        if not self.worker.client:
            messagebox.showinfo("Нужно подключение", "Сначала подключитесь к Telegram.", parent=self.root)
            return
        ids = [str(d[0]) for d in self.dialogs if not isinstance(self.counts.get(str(d[0])), int)]
        if not ids:
            return
        mins = max(1, round(len(ids) * 0.4 / 60))
        if len(ids) > 30 and not messagebox.askyesno(
                "Посчитать сообщения", f"Нужно посчитать {len(ids)} чатов — это {len(ids)} запросов к Telegram, "
                f"примерно {mins} мин. (Telegram может просить паузы.)\n\nМожно продолжать работу, "
                "подсчёт идёт в фоне и его можно остановить.\n\nНачать?", parent=self.root):
            return
        for i in ids:
            self.counts[i] = "?"
        self.fill_dialogs()
        self.pb_count.configure(maximum=len(ids), value=0)
        self.l_count_prog.configure(text=f"Считаю сообщения: 0 из {len(ids)}")
        self.count_bar.grid()
        self.b_count_all.configure(state="disabled")
        asyncio.run_coroutine_threadsafe(self.worker.count_many(ids), self.worker.loop)

    def stop_counting(self):
        self.worker.count_stop = True

    def on_count_progress(self, i, n):
        self.pb_count.configure(maximum=max(1, n), value=i)
        self.l_count_prog.configure(text=f"Считаю сообщения: {i} из {n}")
        if i >= n or getattr(self.worker, "count_stop", False):
            self.count_bar.grid_remove()
            self.b_count_all.configure(state="normal")
            for k in [k for k, v in self.counts.items() if v == "?"]:
                del self.counts[k]
            self.fill_dialogs()
            self.save_counts()

    COUNTS_PATH = os.path.join(APP_DIR, "chat_counts.json")

    def load_counts(self):
        try:
            with open(self.COUNTS_PATH, encoding="utf-8") as f:
                self.counts.update({k: v for k, v in json.load(f).items() if isinstance(v, int)})
        except Exception:
            pass

    def save_counts(self):
        try:
            with open(self.COUNTS_PATH, "w", encoding="utf-8") as f:
                json.dump({k: v for k, v in self.counts.items() if isinstance(v, int)}, f)
        except Exception:
            pass

    def fill_dialogs(self):
        s = self.v_search.get().lower().strip()
        tv = self.tv
        tv.delete(*tv.get_children())
        cur = self.v_chat.get()
        for did, name, kind in self.sorted_dialogs():
            if not s or s in name.lower() or s in str(did):
                iid = str(did)
                if tv.exists(iid):
                    continue
                tv.insert("", "end", iid=iid, values=(name, kind, self.count_text(iid)),
                          image=self.ui.tree_cb["on" if iid in self.checked else "off"])
                if iid == cur:
                    tv.selection_set(iid)
                    tv.see(iid)
        # одно поле: если в списке такого чата нет, а введён @username, ссылка или ID — экспортируем по нему
        raw = self.v_search.get().strip()
        if not tv.get_children() and self.looks_like_chat(raw):
            if self.v_manual.get() != raw:
                self.v_manual.set(raw)
        elif self.v_manual.get() and self.v_manual.get() != raw:
            self._picking = True
            self.v_manual.set("")
            self._picking = False
        self.update_chat_empty()

    @staticmethod
    def looks_like_chat(text):
        return bool(text) and (text.startswith("@") or "t.me/" in text or "telegram.me/" in text
                               or re.fullmatch(r"-?\d{5,}", text) is not None)

    def update_chat_empty(self):
        if not hasattr(self, "chat_empty"):
            return
        if self.tv.get_children():
            self.chat_empty.hide()
        elif self.looks_like_chat(self.v_search.get().strip()):
            self.chat_empty.show("Этого чата нет в списке",
                                 f"Экспорт пойдёт по адресу «{self.v_search.get().strip()}» — нажмите «Начать экспорт».")
        elif self.dialogs:
            self.chat_empty.show("Ничего не найдено",
                                 "Измените запрос. Чат не из списка можно указать по @username, ссылке t.me или ID.")
        elif self.connecting:
            self.chat_empty.show("Загружаю список чатов…", "Это займёт несколько секунд.")
        else:
            self.chat_empty.show("Список чатов пуст", "Подключитесь к Telegram, чтобы выбрать чат для экспорта.",
                                 ("Перейти к подключению", lambda: self.show_page("conn")))

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.v_out.get() or os.path.expanduser("~"), parent=self.root)
        if d:
            self.v_out.set(d)
            self.save_cfg()

    def open_folder(self):
        p = self.out_dir or self.v_out.get()
        if p and os.path.isdir(p):
            open_path(p)
        else:
            messagebox.showinfo("Папка", "Папка ещё не создана.", parent=self.root)

    def open_html(self):
        p = os.path.join(self.out_dir or "", "messages.html")
        if self.out_dir and os.path.exists(p):
            open_path(p)
        else:
            messagebox.showinfo("HTML", "HTML ещё не создан — он появляется в конце экспорта.", parent=self.root)

    def answer(self, fut, value):
        def setres():
            if not fut.done():
                fut.set_result(value)
        self.worker.loop.call_soon_threadsafe(setres)

    # ---------- диалоги ----------
    def _modal(self, title):
        S, c = self.S, self.ui.c
        d = tk.Toplevel(self.root)
        d.withdraw()
        d.title(title)
        d.transient(self.root)
        d.resizable(False, False)
        d.configure(background=c["bg"])
        outer = ttk.Frame(d, padding=(S(24), S(20), S(24), S(18)))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text=title, style="H2.TLabel").pack(anchor="w", pady=(0, S(10)))
        body = ttk.Frame(outer)
        body.pack(fill="both", expand=True)
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(S(22), 0))
        return d, body, btns

    def _center(self, d):
        d.update_idletasks()
        r = self.root
        w, h = d.winfo_reqwidth(), d.winfo_reqheight()
        x = r.winfo_rootx() + max(0, (r.winfo_width() - w) // 2)
        y = r.winfo_rooty() + max(0, (r.winfo_height() - h) // 3)
        d.geometry(f"+{x}+{y}")

    def _show_modal(self, d, wait=True):
        self._center(d)
        d.deiconify()
        set_titlebar_theme(d, self.ui.mode == "dark")
        try:
            d.grab_set()
        except tk.TclError:
            pass
        if wait:
            self.root.wait_window(d)

    def ask_text(self, title, prompt, initial="", password=False):
        """Ввод номера, кода или пароля. None — отмена, строка (возможно пустая) — OK."""
        S = self.S
        d, body, btns = self._modal(title)
        ttk.Label(body, text=prompt, wraplength=S(420), justify="left").pack(anchor="w")
        var = tk.StringVar(value=initial or "")
        e = ttk.Entry(body, textvariable=var, show="•" if password else "", width=36)
        e.pack(fill="x", pady=(S(14), 0))
        res = {"v": None}

        def ok(*_):
            res["v"] = var.get()
            d.destroy()

        ttk.Button(btns, text="Отмена", command=d.destroy).pack(side="right")
        ttk.Button(btns, text="OK", style="Accent.TButton", command=ok, width=10).pack(side="right", padx=(0, S(8)))
        d.bind("<Return>", ok)
        d.bind("<Escape>", lambda e_: d.destroy())
        d.protocol("WM_DELETE_WINDOW", d.destroy)
        d.after(50, lambda: (e.focus_set(), e.icursor("end")))
        self._show_modal(d)
        return res["v"]

    def show_report(self, rep):
        S = self.S
        w = self.report_win
        if w is not None and w.winfo_exists():
            w.destroy()
        head, _, rest = tr(rep["text"]).partition("\n")
        d = tk.Toplevel(self.root)
        d.withdraw()
        d.title("Отчёт об экспорте")
        d.transient(self.root)
        d.configure(background=self.ui.c["bg"])
        self.report_win = d
        outer = ttk.Frame(d, padding=(S(24), S(20), S(24), S(18)))
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text=plain(head), style="SuccessH2.TLabel" if rep["complete"] else "WarningH2.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, S(12)))
        box = tk.Frame(outer, bd=0, highlightthickness=1)
        self.theme_tk(box, lambda c, w_=box: w_.configure(bg=c["field"], highlightbackground=c["border"],
                                                          highlightcolor=c["border"]))
        box.grid(row=1, column=0, sticky="nsew")
        box.columnconfigure(0, weight=1)
        box.rowconfigure(0, weight=1)
        t = tk.Text(box, wrap="word", bd=0, relief="flat", padx=S(14), pady=S(12), font=self.ui.f_body,
                    highlightthickness=0, width=70, height=20)
        self.theme_tk(t, lambda c, w_=t: w_.configure(bg=c["field"], fg=c["text"], selectbackground=c["accent_soft"],
                                                      selectforeground=c["text"], insertbackground=c["text"]))
        sb = ttk.Scrollbar(box, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=self.autoscroll(sb))
        t.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        t.insert("1.0", rest.strip("\n"))
        t.configure(state="disabled")

        bf = ttk.Frame(outer)
        bf.grid(row=2, column=0, sticky="ew", pady=(S(18), 0))
        out = rep["out_dir"]
        primary_set = False
        if not rep["complete"]:
            def again():
                d.destroy()
                (self.on_start_all if rep.get("all") else self.on_start)()
            ttk.Button(bf, text="Докачать недостающее", style="Accent.TButton", command=again).pack(
                side="left", padx=(0, S(8)))
            primary_set = True
        if rep.get("html"):
            hf = os.path.join(out, rep.get("html_file", "messages.html"))
            ttk.Button(bf, text="Открыть список чатов" if rep.get("all") else "Открыть HTML",
                       style="TButton" if primary_set else "Accent.TButton",
                       command=lambda: open_path(hf)).pack(side="left", padx=(0, S(8)))
        ttk.Button(bf, text="Открыть папку", command=lambda: open_path(out)).pack(side="left", padx=(0, S(8)))
        if rep.get("has_failed"):
            ttk.Button(bf, text="Список пропущенных",
                       command=lambda: open_path(os.path.join(out, "failed.txt"))).pack(side="left", padx=(0, S(8)))

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(rep["text"])
            b_copy.configure(text="Скопировано")
            d.after(1500, lambda: b_copy.winfo_exists() and b_copy.configure(text="Копировать"))
        ttk.Button(bf, text="Закрыть", command=d.destroy).pack(side="right")
        b_copy = ttk.Button(bf, text="Копировать", command=copy)
        b_copy.pack(side="right", padx=(0, S(8)))
        d.bind("<Escape>", lambda e: d.destroy())
        d.minsize(S(560), S(360))
        self._center(d)
        d.deiconify()
        set_titlebar_theme(d, self.ui.mode == "dark")
        d.lift()
        d.focus_force()

    def show_qr(self, url, choice):
        import qrcode
        S = self.S
        self.qr_choice = choice
        size = S(240)
        if self.qr_win is None or not self.qr_win.winfo_exists():
            d, body, btns = self._modal("Вход по QR-коду")
            ttk.Label(body, text="Откройте Telegram на телефоне: Настройки → Устройства → Подключить устройство, "
                                 "и наведите камеру на код.", style="Muted.TLabel", wraplength=S(320),
                      justify="left").pack(anchor="w")
            holder = tk.Frame(body, bd=0, highlightthickness=1, bg="#FFFFFF")
            self.theme_tk(holder, lambda c, w_=holder: w_.configure(highlightbackground=c["border"],
                                                                    highlightcolor=c["border"]))
            holder.pack(pady=(S(16), S(8)))
            self.qr_canvas = tk.Canvas(holder, width=size, height=size, bg="#FFFFFF", highlightthickness=0, bd=0)
            self.qr_canvas.pack(padx=S(12), pady=S(12))
            ttk.Label(body, text="Код обновляется сам, когда истекает.", style="Small.TLabel").pack()
            ttk.Button(btns, text="Отмена", command=lambda: self.answer(self.qr_choice, "cancel")).pack(side="right")
            ttk.Button(btns, text="Войти по номеру телефона",
                       command=lambda: self.answer(self.qr_choice, "phone")).pack(side="left")
            d.protocol("WM_DELETE_WINDOW", lambda: self.answer(self.qr_choice, "cancel"))
            d.bind("<Escape>", lambda e: self.answer(self.qr_choice, "cancel"))
            self.qr_win = d
            self._show_modal(d, wait=False)
        q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(url)
        q.make(fit=True)
        m = q.get_matrix()
        c = self.qr_canvas
        c.delete("all")
        cell = max(1, size // len(m))
        off = (size - cell * len(m)) // 2
        for y, row in enumerate(m):
            for x, v in enumerate(row):
                if v:
                    c.create_rectangle(off + x * cell, off + y * cell, off + (x + 1) * cell,
                                       off + (y + 1) * cell, fill="#000000", outline="#000000")
        self.qr_win.lift()

    def close_qr(self):
        w = self.qr_win
        if w is not None and w.winfo_exists():
            try:
                w.grab_release()
            except tk.TclError:
                pass
            w.destroy()
        self.qr_win = None

    # ---------- события от фонового потока ----------
    def poll(self):
        pending_logs = []
        handled = 0
        try:
            while handled < 5000:          # за один раз не больше 5000 событий — окно остаётся отзывчивым
                item = self.q.get_nowait()
                handled += 1
                kind = item[0]
                if kind == "log":
                    pending_logs.append(time.strftime("[%H:%M:%S] ") + tr(item[1]) + "\n")
                    continue
                self.flush_logs(pending_logs)
                pending_logs = []
                if kind == "phase":
                    self.set_phase(item[1])
                elif kind == "ask":
                    _, akind, title, prompt, kw, fut = item
                    if akind == "yesno":
                        val = messagebox.askyesno(title, prompt, parent=self.root)
                    else:
                        val = self.ask_text(title, prompt, initial=kw.get("initial", ""),
                                            password=(akind == "password"))
                    self.answer(fut, val)
                elif kind == "qr_show":
                    self.show_qr(item[1], item[2])
                elif kind == "qr_close":
                    self.close_qr()
                elif kind == "save_phone":
                    self.cfg["phone"] = item[1]
                    self.save_cfg()
                elif kind == "logged_in":
                    self.account = item[1]
                    self.update_account()
                    self.update_takeout_ui()
                elif kind == "logged_out":
                    self.account = None
                    self.dialogs = []
                    self.dialog_map = {}
                    self.fill_dialogs()
                    self.update_account()
                elif kind == "takeout":
                    self.on_takeout_event(item[1])
                elif kind == "takeout_checked":
                    self.update_takeout_ui()
                elif kind in ("diag", "diag_note", "diag_done"):
                    self.on_diag_event(item)
                elif kind == "count_progress":
                    self.on_count_progress(item[1], item[2])
                elif kind == "chat_count":
                    self.counts[item[1]] = item[2]
                    self.show_count(item[1])
                elif kind == "dialogs":
                    self.dialogs = item[1]
                    self.dialog_map = {str(d[0]): d for d in self.dialogs}
                    self.connecting = False
                    self.fill_dialogs()
                    self.update_chat_header()
                    cur = self.v_chat.get().strip()
                    if cur and cur not in self.dialog_map and not self.v_manual.get():
                        self.v_search.set(cur)      # прошлый чат был введён вручную — показываем его в поиске
                    if self.page == "conn":
                        self.show_page("export")
                    if self.v_chat.get() and self.v_chat.get() not in self.counts:
                        self.worker.request_count(self.v_chat.get())
                elif kind == "out_dir":
                    self.out_dir = item[1]
                    self.update_activity()
                    if self.page == "exports":
                        self.refresh_exports()
                elif kind == "error":
                    messagebox.showerror("Ошибка", item[1], parent=self.root)
                elif kind == "finished":
                    self.out_dir = item[1]
                    self.refresh_versions()
                    self.update_activity()
                    if len(item) > 2 and item[2]:
                        self.show_report(item[2])
                elif kind == "versions_changed":
                    self.refresh_versions()
                elif kind == "exports_rows":
                    if item[1] == self.ex_gen:
                        self.ex_all_rows = item[2]
                        self.fill_exports()
                elif kind == "all_finished":
                    self.out_dir = item[1]
                    self.update_activity()
                    self.show_report(item[2])
                elif kind == "idle":
                    was_connecting = self.connecting
                    self.connecting = False
                    self.set_busy(False)
                    if getattr(self, "_restart_connect", False):
                        self._restart_connect = False
                        self.root.after(300, self.on_connect)
                    elif getattr(self, "_reconnect_after_stop", False):
                        self._reconnect_after_stop = False
                        self._relaunch_after_connect = True
                        self.root.after(300, self.on_connect)
                    elif was_connecting and getattr(self, "_relaunch_after_connect", False):
                        self._relaunch_after_connect = False
                        if self.account and getattr(self, "last_job", None):
                            self.root.after(300, self.relaunch_last_job)
                    self.update_chat_empty()
                    if self.page == "exports":
                        self.refresh_exports()
        except queue.Empty:
            pass
        self.flush_logs(pending_logs)
        self.root.after(250, self.poll)

    def tick(self):
        p = self.worker.progress
        st = self.worker.stats
        now = time.monotonic()
        live = {}

        # прогресс по сообщениям (сканирование)
        mn, mt, stage = st.get("msg_n", 0), st.get("msg_total", 0), st.get("stage")
        if st["running"] and stage == "scan":
            if self.msg_samples and mn < self.msg_samples[-1][1]:
                self.msg_samples.clear()
            self.msg_samples.append((now, mn))
            while self.msg_samples and now - self.msg_samples[0][0] > 30:
                self.msg_samples.popleft()

            def mrate_over(window):
                pts = [x for x in self.msg_samples if now - x[0] <= window]
                if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
                    return 0.0
                return max(0.0, (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]))
            mrate = mrate_over(30)
            self.scan_hist.append((now, mrate_over(3.5)))
            self.graph_mode = "scan"
            if mt:
                pct = min(1.0, mn / mt)
                self.stat["msgs"].configure(
                    text=f"{mn:,} из {mt:,} ({pct * 100:.0f}%)".replace(",", " ")
                    + (f" · {mrate:,.0f}/с".replace(",", " ") if mrate else ("" if mn >= mt else " · пауза")))
                live["pct"] = f"{pct * 100:.0f}%"
            else:
                self.stat["msgs"].configure(text=f"просканировано {mn:,}".replace(",", " "))
            self.pb["value"] = (min(1.0, mn / mt) * 1000) if mt else 0
            eta = self.worker.scan_eta()
            if eta is not None:
                self.stat["eta"].configure(text=fmt_left(eta) + " (скан, с паузами)")
                self.stat["finish"].configure(text="скан: " + fmt_when(eta))
                live["eta"] = fmt_left(eta)
            else:
                self.stat["eta"].configure(text="считаю…" if mt else "—")
                self.stat["finish"].configure(text="считаю…" if mt else "—")
                live["eta"] = ""
            done_scan = bool(mt) and mn >= mt          # всё прочитано — проверяются только новые сообщения
            live["status"] = "Идёт: проверка новых сообщений" if done_scan else "Идёт: сканирование"
            live["speed"] = f"{mrate:,.0f} сообщ./с".replace(",", " ") if mrate else ("" if done_scan else "пауза")
        elif mt:
            self.stat["msgs"].configure(text=f"{mn:,} из {mt:,}".replace(",", " "))
        if st["running"] and stage == "download" and p.total <= 0:
            self.pb["value"] = 1000
        if st["running"] and stage == "download" and p.total > 0:
            if self.samples and p.n < self.samples[-1][1]:   # счётчик сбросился (перезапуск) — начинаем замер заново
                self.samples.clear()
                self.dl_eta = None
            self.samples.append((now, p.n))
            while self.samples and now - self.samples[0][0] > 600:   # храним 10 минут
                self.samples.popleft()

            def rate(window):
                pts = [x for x in self.samples if now - x[0] <= window]
                if len(pts) < 2 or pts[-1][0] <= pts[0][0]:
                    return 0.0
                return max(0.0, (pts[-1][1] - pts[0][1]) / (pts[-1][0] - pts[0][0]))

            cur, avg = rate(10), rate(120)
            self.speed_hist.append((now, rate(3.5)))
            self.graph_mode = "download"
            if avg <= 0:
                avg = rate(600)    # долгая пауза — берём среднюю за 10 минут, а не «—»
            before = st.get("bytes_before", 0) or 0          # скачано в прошлые запуски
            n_all, t_all = before + p.n, before + p.total     # прогресс — по всему чату, а не по остатку
            pct = min(1.0, n_all / t_all) if t_all else 0
            self.pb["value"] = pct * 1000
            self.stat["bytes"].configure(text=f"{human(n_all)} / {human(t_all)} ({pct * 100:.0f}%)")
            self.stat["speed"].configure(text=f"{human(cur)}/с · ср. {human(avg)}/с" if avg else "пауза…")
            live.update(status="Идёт: скачивание", pct=f"{pct * 100:.0f}%",
                        speed=f"{human(cur)}/с" if cur else "пауза",
                        size=f"{human(n_all)} из {human(t_all)}")
            if avg > 0 and p.total > p.n:
                left = (p.total - p.n) / avg
                self.dl_eta = left
                self.stat["eta"].configure(text=fmt_left(left) + ("" if cur > 0 else " (пауза)"))
                self.stat["finish"].configure(text=fmt_when(left))
                live["eta"] = fmt_left(left)
            elif self.dl_eta is not None and p.total > p.n:
                # данных пока нет (пауза Telegram, переподключение) — показываем последний прогноз
                self.stat["eta"].configure(text=fmt_left(self.dl_eta) + " (пауза)")
                live["eta"] = fmt_left(self.dl_eta)
            elif p.total > p.n:
                self.stat["eta"].configure(text="считаю…")
                self.stat["finish"].configure(text="считаю…")
                live["eta"] = ""
        if st["running"] and stage == "finish":
            live.update(status="Идёт: сборка HTML и JSON", speed="", eta="")
        if st["running"] and stage == "prepare":
            live.update(status="Идёт: подготовка к скачиванию", speed="", eta="")
            self.stat["eta"].configure(text="—")
            self.stat["finish"].configure(text="—")
        if st["running"] and now - getattr(self.worker, "net_issue_at", -1e9) < 90:
            live.update(status="Ждёт связи с Telegram", speed="")
        if st.get("chat_total"):
            nm = st.get("chat_name", "")
            nm = nm if len(nm) <= 22 else nm[:21] + "…"
            self.stat["chats"].configure(text=f"{st['chat_i']} из {st['chat_total']}: {nm}")
        elif st["running"]:
            self.stat["chats"].configure(text="—")
        if st["total_files"]:
            self.stat["files"].configure(text=f"{st['ok']} из {st['total_files']}")
            self.stat["failed"].configure(text=str(st["failed"]))
            if st["running"] and stage in ("download", "finish"):
                fl = max(st["failed"], getattr(self, "ex_running_failed", 0) or 0)
                live["files"] = f"{st['ok']} / {st['total_files']}" + (f" ⚠{fl}" if fl else "")
        try:
            if now - getattr(self, "_disk_t", -1e9) < 5:
                raise StopIteration
            self._disk_t = now
            path = self.out_dir or self.v_out.get()
            while path and not os.path.exists(path):
                parent = os.path.dirname(path)
                if parent == path:
                    break
                path = parent
            if path and os.path.exists(path):
                self.stat["disk"].configure(text=human(free_space(path)))
        except Exception:
            pass
        self.live = live if st["running"] else None
        self.watchdog(now, st, p)
        low = st.get("disk_low", 0) if st["running"] else 0
        if low:
            self.l_disk_warn.configure(text=f"Не хватает места на диске: освободите ещё {human(low)}. "
                                            "Скачивание на паузе и продолжится само, как только место появится.")
            self.disk_warn.grid()
        else:
            self.disk_warn.grid_remove()
        # пункт меню «Мои экспорты» показывает ход работы с любой страницы
        nav = f"Мои экспорты  ·  {int(self.pb['value']) // 10}%" if st["running"] and stage in ("scan", "download") \
            else ("Мои экспорты  ·  идёт" if st["running"] and stage else "Мои экспорты")
        if st["running"] and st.get("disk_low"):
            nav = "Мои экспорты  ·  нет места"
        if self.nav["exports"].cget("text") != nav:
            self.nav["exports"].configure(text=nav)
        self.update_activity()
        if self.page == "exports":
            self.update_live_row()
            if self.v_details_tab.get() == "speed":
                self.draw_graph()
        # страница «Мои экспорты» открыта во время работы — обновляем её раз в 20 с
        try:
            if st["running"] and self.page == "exports" and now - self.ex_last > 20:
                self.ex_last = now
                self.refresh_exports()
        except Exception:
            pass
        self.root.after(1000, self.tick)

    def on_close(self, restart=False):
        if self.busy and not messagebox.askyesno(
                "Выход", "Идёт работа. Остановить и выйти?\n\nПри следующем запуске работа продолжится с того же "
                "места.", parent=self.root):
            return
        self.save_cfg()
        self.save_counts()
        if self.job and not self.job.done():
            self.job.cancel()
        try:
            if self.worker.client:
                asyncio.run_coroutine_threadsafe(self.worker.client.disconnect(), self.worker.loop).result(3)
        except Exception:
            pass
        self.root.destroy()
        if restart:
            try:
                subprocess.Popen([sys.executable] + [a for a in sys.argv if a != "--restarted"] + ["--restarted"],
                                 cwd=APP_DIR)
            except Exception:
                pass


ICON_PATH = os.path.join(APP_DIR, "app_icon.ico")


def set_app_icon(root):
    """Иконка окна и панели задач (файл app_icon.ico рядом с программой; нет файла — обычная)."""
    if not os.path.exists(ICON_PATH):
        return
    try:
        root.iconbitmap(default=ICON_PATH)       # default — и для всех диалогов программы
    except tk.TclError:
        pass


_INSTANCE = None


def single_instance(wait=0.0):
    """Одна копия программы: две копии одновременно не могут работать с одним файлом входа в Telegram
    («database is locked»). True — можно запускаться."""
    global _INSTANCE
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = ctypes.c_void_p
        deadline = time.time() + wait
        while True:
            h = k32.CreateMutexW(None, False, "Local\\TelegramExporter.SingleInstance")
            if k32.GetLastError() != 183:          # ERROR_ALREADY_EXISTS
                _INSTANCE = h
                return True
            k32.CloseHandle(ctypes.c_void_p(h))
            if time.time() >= deadline:
                return False
            time.sleep(0.3)
    except Exception:
        return True


def show_other_instance():
    """Вывести на передний план уже открытое окно программы."""
    try:
        import ctypes
        u32 = ctypes.windll.user32
        hwnd = u32.FindWindowW(None, "Telegram Chat Exporter")
        if hwnd:
            u32.ShowWindow(hwnd, 9)                # SW_RESTORE
            u32.SetForegroundWindow(hwnd)
            return True
    except Exception:
        pass
    return False


def main():
    # после перезапуска (смена языка) старая копия ещё закрывается — подождём её
    if not single_instance(wait=15 if "--restarted" in sys.argv else 0):
        if not show_other_instance():
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo("Telegram Exporter", "Программа уже запущена — её окно открыто (возможно, свёрнуто "
                                                     "на панели задач).")
            r.destroy()
        return
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # чёткий шрифт на Windows
        # своя «кнопка» на панели задач — с иконкой программы, а не Python
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("TelegramExporter.App")
    except Exception:
        pass
    root = tk.Tk()
    root.withdraw()            # окно появляется уже собранным — без мигания при построении
    set_app_icon(root)
    App(root)
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
