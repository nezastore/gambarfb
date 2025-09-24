#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Theme → AI Image + Facebook Caption (Title + Description + Hashtags)
- Pilih tema & gaya → bot generate gambar via Gemini Image (best-effort)
- Gemini Text buat judul (<=75 chars), deskripsi singkat, dan 20 hashtag (FB-friendly)
- Kirim ke Telegram sebagai FOTO + caption siap tempel
- UI tombol: Tema, Gaya, Bahasa, Random, Generate, Reset

Env:
  TELEGRAM_BOT_TOKEN=xxxx:yyyy
  GEMINI_API_KEY=your_key
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
from PIL import Image, ImageDraw, ImageFilter

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
from telegram.error import BadRequest

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

# Urutan model gambar yang dicoba (tanpa response_modalities supaya kompatibel)
IMAGE_MODELS = [
    "imagen-3.0-generate-001",   # prioritas 1 (jika available di akun/region)
    "gemini-2.0-flash",          # prioritas 2 (kadang bisa mengembalikan image)
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
def esc(s: str) -> str: return html.escape(s or "", quote=False)

def build_menu(cid: int) -> Tuple[str, InlineKeyboardMarkup]:
    st = CONF[cid]
    t, g, l = st["theme"], st["style"], st["lang"]

    text = (
        "🎨 **Generator Gambar + Caption FB (Gemini)**\n\n"
        f"- Tema: `{t}`\n"
        f"- Gaya: `{g}`\n"
        f"- Bahasa: `{l}`\n\n"
        "Klik **Generate** untuk membuat *gambar + judul + deskripsi + hashtag* siap upload Facebook.\n"
        "Urutan model gambar: imagen-3.0-generate-001 → gemini-2.0-flash → ilustrasi fallback."
    )

    theme_rows = []
    row = []
    for i, th in enumerate(THEMES, 1):
        row.append(InlineKeyboardButton(label(t, th, th), callback_data=f"set:theme:{th}"))
        if i % 2 == 0:
            theme_rows.append(row); row = []
    if row: theme_rows.append(row)

    style_row = [InlineKeyboardButton(label(g, s, s), callback_data=f"set:style:{s}") for s in STYLES]
    lang_row  = [
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

# ====== GEMINI TEXT ======
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
        if not isinstance(data.get("hashtags"), list):
            data["hashtags"] = []
        data["hashtags"] = (data["hashtags"] or [])[:20]
        data.setdefault("title", f"{theme} {style}")
        data.setdefault("description", "Visual estetik yang menenangkan, cocok untuk isi feed harianmu.")
        return data
    except Exception:
        return {
            "title": f"{theme} {style}",
            "description": "Visual estetik yang menenangkan, cocok untuk isi feed harianmu.",
            "hashtags": [
                "#inspirasi", "#estetik", "#visual", "#kreatif", "#viralindonesia",
                "#exploreindonesia", "#keindahan", "#senja", "#vibes", "#wallpaper",
                "#fotografi", "#digitalart", "#art", "#desain", "#minimalis",
                "#mood", "#relax", "#healing", "#trending", "#today", "#indonesia"
            ]
        }

# ====== GEMINI IMAGE ======
def image_resp_to_bytes(resp) -> Optional[bytes]:
    try:
        # pola umum: resp.candidates[*].content.parts[*].inline_data
        for c in getattr(resp, "candidates", []) or []:
            content = getattr(c, "content", None)
            if not content: continue
            for p in getattr(content, "parts", []) or []:
                inline = getattr(p, "inline_data", None) or getattr(p, "inlineData", None)
                if inline and getattr(inline, "data", None):
                    return base64.b64decode(inline.data)
        # fallback pola lain
        for p in getattr(resp, "parts", []) or []:
            inline = getattr(p, "inline_data", None) or getattr(p, "inlineData", None)
            if inline and getattr(inline, "data", None):
                return base64.b64decode(inline.data)
    except Exception:
        pass
    return None

def build_image_prompt(theme: str, style: str, lang: str) -> str:
    locale = "Bahasa Indonesia" if lang == "id" else "English"
    return f"""
Generate a single high-quality social-media image for:
Theme: {theme}
Style: {style}

Composition & quality:
- vertical-friendly (9:16 crop safe), centered subject, depth & cinematic lighting
- photogenic, vibrant yet natural colors, pleasing contrast
- realistic details if photorealistic; neat shapes if illustration
- NO text, NO watermark, NO borders
- resolution ~1024x1024

Language context: {locale}.
Deliver exactly ONE image.
"""

def gemini_generate_image(theme: str, style: str, lang: str) -> Optional[str]:
    """
    Best-effort image generation:
    1) imagen-3.0-generate-001 -> generate_content(prompt)
    2) gemini-2.0-flash        -> generate_content(prompt)
    Return file path if any, else None (caller will fallback).
    """
    prompt = build_image_prompt(theme, style, lang)

    # Try each model without unsupported generation_config fields
    for model_name in IMAGE_MODELS:
        try:
            model = genai.GenerativeModel(model_name)
            resp  = model.generate_content(prompt)
            img   = image_resp_to_bytes(resp)
            if img:
                fd, path = tempfile.mkstemp(prefix="gemimg_", suffix=".png")
                with os.fdopen(fd, "wb") as f:
                    f.write(img)
                return path
        except Exception as e:
            print(f"[!] {model_name} gagal: {e}")
            continue
    return None

# ====== FALLBACK IMAGE (soft gradient + bokeh) ======
def fallback_image(theme: str, style: str) -> str:
    """Ilustrasi abstrak lebih estetik (portrait)."""
    W, H = 1080, 1920
    # gradient vertikal halus
    top = np.array([36, 44, 78], dtype=np.float32)
    bottom = np.array([120, 62, 146], dtype=np.float32)
    grad = np.zeros((H, W, 3), dtype=np.uint8)
    for y in range(H):
        t = y / (H - 1)
        color = (1 - t) * top + t * bottom
        grad[y, :, :] = color
    img = Image.fromarray(grad, "RGB")

    # bokeh transparan
    rng = np.random.default_rng()
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    for _ in range(18):
        r = int(rng.integers(90, 220))
        x = int(rng.integers(-r, W + r))
        y = int(rng.integers(-r, H + r))
        col = (int(rng.integers(120, 210)), int(rng.integers(90, 190)), int(rng.integers(140, 240)), int(rng.integers(40, 90)))
        draw.ellipse([(x - r, y - r), (x + r, y + r)], fill=col)
    overlay = overlay.filter(ImageFilter.GaussianBlur(14))
    img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")

    # vignette
    vign = Image.new("L", (W, H), 0)
    dv = ImageDraw.Draw(vign)
    dv.ellipse([(-int(W*0.15), -int(H*0.05)), (int(W*1.15), int(H*1.05))], fill=255)
    vign = vign.filter(ImageFilter.GaussianBlur(150))
    img = Image.composite(img, Image.new("RGB", (W, H), (8, 8, 12)), vign)

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
        "Urutan model gambar: imagen-3.0-generate-001 → gemini-2.0-flash → ilustrasi fallback.",
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

        # 2) Image via Gemini → fallback jika gagal
        img_path = gemini_generate_image(theme, style, lang)
        used = "Gemini Image"
        if not img_path:
            img_path = fallback_image(theme, style)
            used = "Fallback"

        # 3) Kirim
        try:
            with open(img_path, "rb") as f:
                await context.bot.send_photo(
                    cid,
                    photo=InputFile(f, filename="image.jpg"),
                    caption=caption_html + f"\n\n<i>Image source: {used}</i>",
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
    print(f"Bot ready. Text model: {TEXT_MODEL}; Image order: {', '.join(IMAGE_MODELS)}")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
