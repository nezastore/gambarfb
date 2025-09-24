#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: News/Facts → Caption Variants (judul + hashtag) + Image
- Tanpa video/overlay
- Sumber: Google News (mode "news") / Wikipedia Random Summary (mode "facts")
- Gemini menghasilkan 3–5 varian caption per artikel
- Kirim gambar (thumbnail dari artikel jika ada; kalau tidak, generate ilustrasi via Gemini)
- Carousel varian dengan tombol Prev/Next (edit caption foto yang sama)
- Tombol untuk melihat semua varian & Reset
"""

import os, re, json, uuid, asyncio, feedparser, requests, html, base64, tempfile
from datetime import datetime, timezone
from typing import List, Dict, Tuple, Optional

from dotenv import load_dotenv
load_dotenv()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()
if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak valid. Cek .env (TELEGRAM_BOT_TOKEN=XXX:YYY).")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY kosong. Fitur AI bisa gagal.")

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes, CallbackQueryHandler
)
from telegram.error import BadRequest

# ===== Gemini =====
import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)

def pick_gemini_text_model() -> str:
    for m in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try:
            _ = genai.GenerativeModel(m)
            return m
        except Exception:
            continue
    return "gemini-1.5-flash"

# urutan kandidat model image yg umum tersedia (akan dicoba satu per satu)
IMAGE_MODELS = [
    # Gemini 2.5 image (preview) — pengganti 2.0 image-gen yang deprecated
    "gemini-2.5-flash-image-preview",
    # Model image via Gemini API (alias Imagen family)
    "imagen-3.0-generate-001",
    "imagen-3.0",
]

TEXT_MODEL = pick_gemini_text_model()

# ====== STATE ======
CHAT_CONF: Dict[int, Dict] = {}  # {chat_id: {"mode","lang","variants","count"}}
MSG_STATE: Dict[Tuple[int, int], Dict] = {}  # {(chat_id, message_id): {"variants":[{title,hashtags}], "index":0, "url":str}}

# ====== DEFAULTS ======
DEF_MODE = "news"
DEF_LANG = "id"
DEF_VAR  = 4     # jumlah varian caption
DEF_CNT  = 3     # jumlah artikel per fetch

# ==========================
# Fetch Sumber
# ==========================
def fetch_google_news(query: str, lang="id", region="ID", limit=5) -> List[Dict]:
    q = requests.utils.quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid={region}:{lang}"
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries[:limit]:
        img = ""
        try:
            if "media_content" in e and e.media_content:
                img = e.media_content[0].get("url", "") or img
            if not img and "media_thumbnail" in e and e.media_thumbnail:
                img = e.media_thumbnail[0].get("url", "") or img
        except Exception:
            pass
        items.append({
            "title": getattr(e, "title", ""),
            "url": getattr(e, "link", ""),
            "published": getattr(e, "published", ""),
            "image": img
        })
    return items

def fetch_wikipedia_facts(lang="id", limit=3) -> List[Dict]:
    base = f"https://{lang}.wikipedia.org/api/rest_v1/page/random/summary"
    out = []
    for _ in range(limit):
        try:
            r = requests.get(base, timeout=10)
            if r.status_code == 200:
                d = r.json()
                url = (d.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
                thumb = d.get("thumbnail", {}).get("source", "")
                if d.get("title") and d.get("extract") and url:
                    out.append({
                        "title": d["title"],
                        "summary": d["extract"],
                        "url": url,
                        "image": thumb
                    })
        except Exception:
            pass
    return out

# ==========================
# Gemini caption variants (per artikel)
# ==========================
def gemini_variants_for_item(item: Dict, lang: str = "id", nvar: int = 4) -> List[Dict]:
    """
    item: {
      news:  title, url, published, image
      facts: title, summary, url, image
    }
    Return: [{"title": "...", "hashtags": ["#a", ...]}, ...] (panjang nvar)
    """
    locale = "Bahasa Indonesia" if lang == "id" else "English"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_blob = json.dumps(item, ensure_ascii=False, indent=2)

    prompt = f"""
You are a social media assistant. Today: {now_str}.
Language: {locale}.

Given this source (one article):
{src_blob}

Write {nvar} different social-media caption variants for Facebook/Shorts:
- Each variant must include:
  - "title": a catchy rewritten headline (<= 75 characters)
  - "hashtags": 20 hashtags, lowercase, with '#', no spaces. If language is Indonesian, include ~5 Indonesia-specific tags.

Return STRICT JSON:
{{
  "variants": [
    {{"title":"...","hashtags":["#...", "#...", "..."]}}
  ]
}}
"""
    try:
        model = genai.GenerativeModel(TEXT_MODEL)
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
        data = json.loads(text)
        arr = data.get("variants", [])
        arr = [v for v in arr if isinstance(v, dict) and v.get("title") and isinstance(v.get("hashtags"), list)]
        # clamp
        arr = arr[:max(1, min(5, nvar))]
        if not arr:
            raise ValueError("no variants")
        return arr
    except Exception:
        return [{
            "title": "Konten Menarik",
            "hashtags": ["#info", "#viral", "#update", "#fakta", "#unik", "#trending",
                         "#today", "#berita", "#news", "#shorts", "#reels", "#tiktok",
                         "#explore", "#viralindonesia", "#indonesia", "#wow", "#keren",
                         "#inspirasi", "#hiburan", "#edukasi"]
        }]

# ==========================
# Gemini image generation (fallback ketika item tidak punya image)
# ==========================
def extract_image_bytes_from_response(resp) -> Optional[bytes]:
    """
    Cari part image (inline_data) dari response Gemini dan kembalikan bytes-nya.
    Format SDK bisa berbeda-beda; fungsi ini berusaha robust.
    """
    try:
        # pola umum: resp.candidates[0].content.parts[*].inline_data
        cands = getattr(resp, "candidates", None) or []
        for c in cands:
            content = getattr(c, "content", None)
            if not content: continue
            parts = getattr(content, "parts", []) or []
            for p in parts:
                inline = getattr(p, "inline_data", None) or getattr(p, "inlineData", None)
                if inline and getattr(inline, "data", None):
                    return base64.b64decode(inline.data)
        # beberapa SDK menaruh langsung di resp.parts
        parts = getattr(resp, "parts", []) or []
        for p in parts:
            inline = getattr(p, "inline_data", None) or getattr(p, "inlineData", None)
            if inline and getattr(inline, "data", None):
                return base64.b64decode(inline.data)
    except Exception:
        pass
    return None

def build_image_prompt_from_item(item: Dict, lang: str) -> str:
    locale = "Bahasa Indonesia" if lang == "id" else "English"
    title = item.get("title") or ""
    summ  = item.get("summary") or ""
    topic = title if title else summ[:140]
    desc = f"""
Create a high-quality illustrative social-media thumbnail relevant to this topic:
"{topic}"

Style: news feature / documentary photo, clean composition, high contrast, no text, no watermark, vertical-friendly.
Output: 1024x1024, photorealistic or illustrative depending on topic.

Language context: {locale}.
"""
    return desc

def gemini_generate_image_file(item: Dict, lang: str = "id") -> Optional[str]:
    """
    Coba generate image menggunakan beberapa model image Gemini/Imagen.
    Return path file PNG/JPG sementara, atau None jika gagal.
    """
    prompt = build_image_prompt_from_item(item, lang)
    for model_name in IMAGE_MODELS:
        try:
            model = genai.GenerativeModel(
                model_name,
                # beberapa SDK butuh config modal image
                generation_config={
                    "response_modalities": ["IMAGE"],  # arahkan supaya balikan gambar
                }
            )
            resp = model.generate_content(prompt)
            img_bytes = extract_image_bytes_from_response(resp)
            if not img_bytes:
                # beberapa model balikan link base64 di resp.text (jarang)
                if getattr(resp, "text", None) and "data:image" in resp.text:
                    b64 = resp.text.split("base64,")[-1].split('"')[0].strip()
                    img_bytes = base64.b64decode(b64)
            if img_bytes:
                fd, path = tempfile.mkstemp(prefix="gemimg_", suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(img_bytes)
                return path
        except Exception:
            continue
    return None

# ==========================
# Helpers UI
# ==========================
def init_chat(chat_id: int):
    CHAT_CONF[chat_id] = CHAT_CONF.get(chat_id, {})
    CHAT_CONF[chat_id].setdefault("mode", DEF_MODE)
    CHAT_CONF[chat_id].setdefault("lang", DEF_LANG)
    CHAT_CONF[chat_id].setdefault("variants", DEF_VAR)
    CHAT_CONF[chat_id].setdefault("count", DEF_CNT)

def label(cur, val, txt):
    return f"✅ {txt}" if cur == val else txt

def build_menu(chat_id: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = CHAT_CONF.get(chat_id, {})
    mode = st.get("mode", DEF_MODE)
    lang = st.get("lang", DEF_LANG)
    nvar = int(st.get("variants", DEF_VAR))
    cnt  = int(st.get("count", DEF_CNT))

    text = (
        "🗞️ **Generator Caption + Gambar (Gemini)**\n\n"
        f"- Mode: `{mode}`\n"
        f"- Bahasa: `{lang}`\n"
        f"- Varian per artikel: `{nvar}`\n"
        f"- Jumlah artikel: `{cnt}`\n\n"
        "Klik *Ambil & Buat* untuk men-generate caption + gambar.\n"
        "Jika sumber tidak punya gambar, bot akan membuat ilustrasi via Gemini."
    )

    kb = [
        [InlineKeyboardButton(label(mode, "news", "📰 News"),  callback_data="conf:mode:news"),
         InlineKeyboardButton(label(mode, "facts","📘 Facts"), callback_data="conf:mode:facts")],
        [InlineKeyboardButton(label(lang, "id", "🇮🇩 ID"),     callback_data="conf:lang:id"),
         InlineKeyboardButton(label(lang, "en", "🇬🇧 EN"),     callback_data="conf:lang:en")],
        [InlineKeyboardButton(label(nvar, 3, "3 var"), callback_data="conf:var:3"),
         InlineKeyboardButton(label(nvar, 4, "4 var"), callback_data="conf:var:4"),
         InlineKeyboardButton(label(nvar, 5, "5 var"), callback_data="conf:var:5")],
        [InlineKeyboardButton(label(cnt, 1, "1 artikel"), callback_data="conf:cnt:1"),
         InlineKeyboardButton(label(cnt, 3, "3 artikel"), callback_data="conf:cnt:3"),
         InlineKeyboardButton(label(cnt, 5, "5 artikel"), callback_data="conf:cnt:5")],
        [InlineKeyboardButton("▶️ Ambil & Buat", callback_data="go"),
         InlineKeyboardButton("🔁 Reset",         callback_data="reset")]
    ]
    return text, InlineKeyboardMarkup(kb)

def escape_html(s: str) -> str:
    return html.escape(s or "", quote=False)

def build_caption_html(title: str, hashtags: List[str], url: str) -> str:
    hs = [h if h.startswith("#") else f"#{h}" for h in hashtags]
    body = f"<b>{escape_html(title)}</b>\n\n" + " ".join(hs)
    if url:
        body += f"\n\n<a href=\"{escape_html(url)}\">Sumber</a>"
    return body

def kb_for_variants() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Prev", callback_data="nav:prev"),
         InlineKeyboardButton("Next ➡️", callback_data="nav:next")],
        [InlineKeyboardButton("📋 Semua Varian", callback_data="nav:all")],
        [InlineKeyboardButton("🔁 Reset", callback_data="reset")]
    ])

# ==========================
# Handlers
# ==========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    init_chat(cid)
    await update.message.reply_text(
        "👋 Selamat datang!\nBot ini akan mengambil berita/fakta, "
        "membuat beberapa varian judul + hashtag dengan Gemini, dan mengirim gambar + caption.\n"
        "Kalau sumber tidak punya gambar, bot akan membuat ilustrasi otomatis via Gemini.\n\n"
        "Atur preferensi di bawah ini:",
        disable_web_page_preview=True
    )
    text, kb = build_menu(cid)
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

async def on_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = q.message.chat_id
    init_chat(cid)
    data = (q.data or "").split(":")

    # Konfigurasi
    if data[0] == "conf":
        _, key, val = data
        if key == "mode":
            CHAT_CONF[cid]["mode"] = val
        elif key == "lang":
            CHAT_CONF[cid]["lang"] = val
        elif key == "var":
            CHAT_CONF[cid]["variants"] = max(3, min(5, int(val)))
        elif key == "cnt":
            CHAT_CONF[cid]["count"] = max(1, min(5, int(val)))
        text, kb = build_menu(cid)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except BadRequest:
            await q.edit_message_reply_markup(reply_markup=kb)
        return

    # Reset
    if data[0] == "reset":
        CHAT_CONF.pop(cid, None)
        for k in [k for k in list(MSG_STATE.keys()) if k[0] == cid]:
            MSG_STATE.pop(k, None)
        init_chat(cid)
        text, kb = build_menu(cid)
        await q.edit_message_text("✅ Reset. Silakan atur lagi.")
        await context.bot.send_message(cid, text, reply_markup=kb, parse_mode="Markdown")
        return

    # Ambil & Buat
    if data[0] == "go":
        st = CHAT_CONF[cid]
        mode = st["mode"]; lang = st["lang"]; nvar = int(st["variants"]); cnt = int(st["count"])
        await q.edit_message_text("⏳ Mengambil sumber & membuat caption/gambar dengan Gemini…")

        # Ambil sumber
        sources: List[Dict] = []
        try:
            if mode == "news":
                bundle = []
                for topic in ["trending", "viral", "breaking", "unik", "teknologi", "hiburan"]:
                    try:
                        bundle += fetch_google_news(topic, lang=("id" if lang=="id" else "en"),
                                                    region=("ID" if lang=="id" else "US"), limit=3)
                    except Exception:
                        pass
                # Dedup by URL
                seen = set()
                for it in bundle:
                    u = it.get("url")
                    if u and u not in seen:
                        seen.add(u); sources.append(it)
                sources = sources[:cnt]
            else:
                sources = fetch_wikipedia_facts(lang=("id" if lang=="id" else "en"), limit=cnt)
        except Exception as e:
            await context.bot.send_message(cid, f"❌ Gagal ambil sumber: {e}")
            return

        if not sources:
            await context.bot.send_message(cid, "😕 Tidak ditemukan sumber yang cocok. Coba lagi.")
            return

        # Kirim tiap artikel (gambar + varian-1) dengan tombol carousel varian
        for item in sources:
            try:
                # 1) Buat varian caption
                variants = gemini_variants_for_item(item, lang=lang, nvar=nvar)
                v0 = variants[0]
                caption_html = build_caption_html(v0.get("title", "Konten Menarik"),
                                                  v0.get("hashtags", []),
                                                  item.get("url", ""))

                # 2) Pilih gambar: thumbnail asli kalau ada, kalau tidak → generate via Gemini
                img_path = None
                if item.get("image"):
                    # download dulu supaya stabil ketika send_photo
                    try:
                        r = requests.get(item["image"], timeout=10)
                        if r.status_code == 200:
                            fd, img_path = tempfile.mkstemp(prefix="newsimg_", suffix=".jpg")
                            with os.fdopen(fd, "wb") as f:
                                f.write(r.content)
                    except Exception:
                        img_path = None

                if not img_path:
                    # fallback: generate image via Gemini
                    gen_path = gemini_generate_image_file(item, lang=lang)
                    img_path = gen_path  # bisa None jika gagal

                # 3) Kirim: foto jika ada file; kalau tidak ada sama sekali → kirim caption teks
                if img_path and os.path.exists(img_path):
                    with open(img_path, "rb") as f:
                        sent = await context.bot.send_photo(
                            cid, photo=InputFile(f, filename="image.png"),
                            caption=caption_html, parse_mode="HTML",
                            reply_markup=kb_for_variants()
                        )
                    mid = sent.message_id
                else:
                    sent = await context.bot.send_message(
                        cid, text=caption_html, parse_mode="HTML",
                        reply_markup=kb_for_variants(),
                        disable_web_page_preview=False
                    )
                    mid = sent.message_id

                # 4) Simpan state varian untuk tombol Prev/Next
                MSG_STATE[(cid, mid)] = {
                    "variants": variants,
                    "index": 0,
                    "url": item.get("url", "")
                }

                # Info sumber (opsional)
                src_title = item.get("title") or "(tanpa judul)"
                await context.bot.send_message(
                    cid,
                    f"📰 <b>Sumber</b>: {html.escape(src_title)}\n<a href=\"{html.escape(item.get('url',''))}\">Link</a>",
                    parse_mode="HTML",
                    disable_web_page_preview=False
                )

            except Exception as e:
                await context.bot.send_message(cid, f"❌ Gagal proses artikel: {e}")

        return

    # Navigasi varian di satu pesan
    if data[0] == "nav":
        key = (cid, q.message.message_id)
        if key not in MSG_STATE:
            await q.answer("State tidak ditemukan. Kirim /start lagi.", show_alert=True)
            return
        st = MSG_STATE[key]
        arr = st["variants"]
        idx = st["index"]
        url = st.get("url","")

        action = data[1]
        if action == "prev":
            idx = (idx - 1) % len(arr)
        elif action == "next":
            idx = (idx + 1) % len(arr)
        elif action == "all":
            # kirim semua varian dalam pesan teks
            lines = []
            for i, v in enumerate(arr, 1):
                hs = [h if h.startswith("#") else f"#{h}" for h in v.get("hashtags", [])]
                lines.append(f"<b>Varian {i}</b>\n{html.escape(v.get('title',''))}\n" + " ".join(hs))
            if url:
                lines.append(f'<a href="{html.escape(url)}">Sumber</a>')
            await context.bot.send_message(cid, "\n\n".join(lines), parse_mode="HTML")
            return

        st["index"] = idx
        v = arr[idx]
        new_caption = build_caption_html(v.get("title",""), v.get("hashtags",[]), url)

        # edit caption (foto atau pesan teks)
        try:
            await q.edit_message_caption(caption=new_caption, parse_mode="HTML", reply_markup=kb_for_variants())
        except BadRequest:
            # jika bukan media (mis. pesan teks), gunakan edit_message_text
            try:
                await q.edit_message_text(text=new_caption, parse_mode="HTML", reply_markup=kb_for_variants())
            except Exception:
                pass
        return

# ==========================
# Main
# ==========================
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_cb))
    print(f"Bot running... (Text model: {TEXT_MODEL}; Image models fallback: {', '.join(IMAGE_MODELS)})")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
