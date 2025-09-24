#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Theme → AI Image + Facebook Caption (Title + Description + Hashtags)
- Pilih tema & gaya → bot generate gambar via Gemini Image (fallback: ilustrasi abstrak)
- Gemini Text buat judul (<=75 chars), deskripsi singkat, dan 20 hashtag (FB-friendly)
- Kirim ke Telegram sebagai FOTO + caption siap tempel
- UI tombol: Tema, Gaya, Bahasa, Random, Generate, Reset

Env:
  TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
"""

import os, re, json, html, base64, tempfile, asyncio, uuid
from typing import Dict, Tuple, List, Optional
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()
if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak valid. Cek .env (TELEGRAM_BOT_TOKEN=XXX:YYY).")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY kosong. Fitur AI bisa gagal.")

import numpy as np
from PIL import Image, ImageDraw
import requests

# Telegram
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest

# Gemini
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

TEXT_MODEL = pick_gemini_text_model()

# Kandidat model image (akan dicoba satu per satu)
IMAGE_MODELS = [
    "gemini-2.5-flash-image-preview",  # image-gen preview (terbaru, region-dependent)
    "imagen-3.0-generate-001",         # Imagen via Gemini API (nama bisa berbeda per akun/region)
    "imagen-3.0",
]

# ====== STATE ======
CONF: Dict[int, Dict] = {}  # per chat: {"theme":..., "style":..., "lang":...}

THEMES = [
    "Alam Estetik", "Kota Modern", "Teknologi Futuristik", "Musik & Instrument",
    "Kuliner Menggoda", "Hewan Lucu", "Motivasi/Quotes", "Olahraga Aksi",
    "Luar Angkasa", "Minimalist Abstract"
]
STYLES = [
    "Photorealistic", "Digital Art", "Watercolor", "Cyberpunk Neon", "Flat Minimalist"
]
LANGS = ["id", "en"]

DEF_THEME = "Alam Estetik"
DEF_STYLE = "Photorealistic"
DEF_LANG  = "id"

# ====== HELPERS ======
def init_chat(cid: int):
    CONF[cid] = CONF.get(cid, {})
    CONF[cid].setdefault("theme", DEF_THEME)
    CONF[cid].setdefault("style", DEF_STYLE)
    CONF[cid].setdefault("lang",  DEF_LANG)

def label(cur, val, txt): return f"✅ {txt}" if cur == val else txt

def esc(s: str) -> str:
    return html.escape(s or "", quote=False)

def build_menu(cid: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = CONF[cid]
    t, g, l = st["theme"], st["style"], st["lang"]

    text = (
        "🎨 **Generator Gambar + Caption FB (Gemini)**\n\n"
        f"- Tema: `{t}`\n"
        f"- Gaya: `{g}`\n"
        f"- Bahasa: `{l}`\n\n"
        "Klik **Generate** untuk membuat *gambar + judul + deskripsi + hashtag* siap upload Facebook."
        "\nJika model image tidak tersedia, bot membuat ilustrasi fallback (abstrak estetik)."
    )

    # Baris tema (2×5 tombol)
    theme_rows = []
    row = []
    for i, th in enumerate(THEMES, 1):
        row.append(InlineKeyboardButton(label(t, th, th), callback_data=f"set:theme:{th}"))
        if i % 2 == 0:
            theme_rows.append(row); row = []
    if row: theme_rows.append(row)

    style_row = [
        InlineKeyboardButton(label(g, s, s), callback_data=f"set:style:{s}") for s in STYLES
    ]
    lang_row = [
        InlineKeyboardButton(label(l, "id", "🇮🇩 ID"), callback_data="set:lang:id"),
        InlineKeyboardButton(label(l, "en", "🇬🇧 EN"), callback_data="set:lang:en"),
    ]
    action_row = [
        InlineKeyboardButton("🎲 Random Tema", callback_data="random"),
        InlineKeyboardButton("▶️ Generate",   callback_data="go"),
        InlineKeyboardButton("🔁 Reset",      callback_data="reset"),
    ]

    kb = theme_rows + [style_row, lang_row, action_row]
    return text, InlineKeyboardMarkup(kb)

def fb_caption_html(title: str, description: str, hashtags: List[str]) -> str:
    hs = [h if h.startswith("#") else f"#{h}" for h in hashtags]
    return f"<b>{esc(title)}</b>\n\n{esc(description)}\n\n" + " ".join(hs)

# ====== GEMINI TEXT (title + description + hashtags) ======
def gemini_make_caption(theme: str, style: str, lang: str) -> Dict:
    locale = "Bahasa Indonesia" if lang == "id" else "English"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    prompt = f"""
You are a social media assistant. Today: {now}.
Language: {locale}.

Create a Facebook post package for the theme:
Theme: "{theme}"
Visual style: "{style}"

Return STRICT JSON with:
{{
  "title": "<catchy <=75 chars>",
  "description": "<1-2 short paragraphs, warm & positive, no emojis>",
  "hashtags": ["#...", "... 20 items total, lowercase, no spaces; include ~5 Indonesia-specific if locale Indonesian"]
}}
Avoid clickbait and keep it brand-safe & factual.
"""

    try:
        model = genai.GenerativeModel(TEXT_MODEL)
        resp = model.generate_content(prompt)
        txt = (resp.text or "").strip()
        txt = re.sub(r"^```(?:json)?|```$", "", txt, flags=re.MULTILINE).strip()
        data = json.loads(txt)
        # minimal guard
        if not isinstance(data.get("hashtags"), list):
            data["hashtags"] = ["#inspirasi", "#viral", "#today", "#info", "#kreatif"] * 4
        data["hashtags"] = data["hashtags"][:20]
        data.setdefault("title", f"{theme} {style}")
        data.setdefault("description", "Konten menarik bertema visual yang memanjakan mata.")
        return data
    except Exception:
        return {
            "title": f"{theme} {style}",
            "description": "Visual estetik yang menenangkan, cocok untuk isi feed harianmu.",
            "hashtags": [
                "#inspirasi", "#estetik", "#visual", "#kreatif", "#viralindonesia",
                "#exploreindonesia", "#keindahan", "#senja", "#vibes", "#wallpaper",
                "#fotografi", "#digitalart", "#art", "#desain", "#minimalis",
                "#mood", "#relax", "#healing", "#trending", "#today"
            ]
        }

# ====== GEMINI IMAGE (text-to-image) + FALLBACK ======
def image_resp_to_bytes(resp) -> Optional[bytes]:
    try:
        cands = getattr(resp, "candidates", None) or []
        for c in cands:
            content = getattr(c, "content", None)
            if not content: continue
            parts = getattr(content, "parts", []) or []
            for p in parts:
                inline = getattr(p, "inline_data", None) or getattr(p, "inlineData", None)
                if inline and getattr(inline, "data", None):
                    return base64.b64decode(inline.data)
        # fallback pola lain
        parts = getattr(resp, "parts", []) or []
        for p in parts:
            inline = getattr(p, "inline_data", None) or getattr(p, "inlineData", None)
            if inline and getattr(inline, "data", None):
                return base64.b64decode(inline.data)
    except Exception:
        pass
    return None

def build_image_prompt(theme: str, style: str, lang: str) -> str:
    locale = "Bahasa Indonesia" if lang == "id" else "English"
    return f"""
Create a beautiful social-media friendly image for:
Theme: {theme}
Style: {style}
Guidelines:
- vertical-friendly cropping, center composition
- clean background, no text, no watermark, brand-safe
- high contrast, vibrant yet natural color
- resolution 1024x1024
Language context: {locale}.
"""

def gemini_generate_image(theme: str, style: str, lang: str) -> Optional[str]:
    prompt = build_image_prompt(theme, style, lang)
    for model_name in IMAGE_MODELS:
        try:
            model = genai.GenerativeModel(
                model_name,
                generation_config={"response_modalities": ["IMAGE"]},
            )
            resp = model.generate_content(prompt)
            img = image_resp_to_bytes(resp)
            if not img and getattr(resp, "text", None) and "data:image" in resp.text:
                b64 = resp.text.split("base64,")[-1].split('"')[0].strip()
                img = base64.b64decode(b64)
            if img:
                fd, path = tempfile.mkstemp(prefix="gemimg_", suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(img)
                return path
        except Exception:
            continue
    return None

def fallback_image(theme: str, style: str) -> str:
    """Ilustrasi abstrak estetik bila image model tidak tersedia."""
    W, H = 1024, 1024
    img = Image.new("RGB", (W, H), (10, 10, 14))
    dr = ImageDraw.Draw(img)

    # gradient diagonal
    grad = Image.new("RGB", (W, H))
    for y in range(H):
        r = int(40 + 60 * (y / H))
        g = int(50 + 80 * (1 - y / H))
        b = int(120 + 100 * (y / H))
        dr.line([(0, y), (W, y)], fill=(r, g, b))
    # overlay bentuk bulat lembut
    rng = np.random.default_rng()
    for _ in range(24):
        x, y = rng.integers(0, W), rng.integers(0, H)
        rad = int(rng.integers(60, 180))
        color = (int(rng.integers(80, 200)), int(rng.integers(60, 180)), int(rng.integers(120, 240)))
        ImageDraw.Draw(img).ellipse([(x-rad, y-rad), (x+rad, y+rad)], fill=color+(90,))

    # blend
    img = Image.blend(img, grad, 0.35)

    # simpan
    fp = os.path.join(tempfile.gettempdir(), f"fallback_{uuid.uuid4().hex}.jpg")
    img.save(fp, "JPEG", quality=92)
    return fp

# ====== TELEGRAM UI ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    init_chat(cid)
    await update.message.reply_text(
        "👋 Selamat datang!\n"
        "Pilih *Tema*, *Gaya*, dan *Bahasa*, lalu klik **Generate**.\n"
        "Bot akan membuat **gambar + judul + deskripsi + hashtag** siap upload Facebook.\n"
        "Jika image model tidak tersedia, bot membuat ilustrasi fallback (abstrak estetik).",
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
    if data[0] == "set":
        _, key, val = data
        if key == "theme":
            CONF[cid]["theme"] = val
        elif key == "style":
            CONF[cid]["style"] = val
        elif key == "lang":
            CONF[cid]["lang"] = val
        text, kb = build_menu(cid)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except BadRequest:
            await q.edit_message_reply_markup(reply_markup=kb)
        return

    if data[0] == "random":
        import random
        CONF[cid]["theme"] = random.choice(THEMES)
        CONF[cid]["style"] = random.choice(STYLES)
        text, kb = build_menu(cid)
        try:
            await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        except BadRequest:
            await q.edit_message_reply_markup(reply_markup=kb)
        return

    if data[0] == "reset":
        CONF.pop(cid, None)
        init_chat(cid)
        text, kb = build_menu(cid)
        await q.edit_message_text("✅ Reset. Silakan pilih tema & gaya lagi.")
        await context.bot.send_message(cid, text, reply_markup=kb, parse_mode="Markdown")
        return

    if data[0] == "go":
        st = CONF[cid]
        theme, style, lang = st["theme"], st["style"], st["lang"]
        await q.edit_message_text("⏳ Membuat gambar & caption… (Gemini)")

        # 1) Caption
        cap = gemini_make_caption(theme, style, lang)
        caption_html = fb_caption_html(cap["title"], cap["description"], cap["hashtags"])

        # 2) Image
        path = gemini_generate_image(theme, style, lang)
        if not path:
            path = fallback_image(theme, style)

        # 3) Kirim
        try:
            with open(path, "rb") as f:
                await context.bot.send_photo(
                    cid,
                    photo=InputFile(f, filename="image.jpg"),
                    caption=caption_html,
                    parse_mode="HTML",
                )
        except Exception as e:
            await context.bot.send_message(cid, f"❌ Gagal kirim gambar: {e}")
            await context.bot.send_message(cid, caption_html, parse_mode="HTML")

        # Kirim juga paket teks terpisah (biar mudah disalin)
        await context.bot.send_message(
            cid,
            f"<b>Judul</b>: {esc(cap['title'])}\n\n"
            f"<b>Deskripsi</b>:\n{esc(cap['description'])}\n\n"
            f"<b>Hashtag</b>:\n" + " ".join([h if h.startswith('#') else f'#{h}' for h in cap['hashtags']]),
            parse_mode="HTML",
            disable_web_page_preview=True
        )
        return

# ====== MAIN ======
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_cb))
    print(f"Bot ready. Text model: {TEXT_MODEL}; Image fallbacks: {', '.join(IMAGE_MODELS)}")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
