#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Ambil berita (Google News / Wikipedia) → proses dengan Gemini
Hasil:
- Judul baru hasil rewrite
- Hashtag (20+)
- Gambar thumbnail dari berita (kalau ada)
"""

import os, re, json, uuid, asyncio, requests, feedparser
from datetime import datetime, timezone
from typing import List, Dict, Tuple

from dotenv import load_dotenv
load_dotenv()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()
if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak valid.")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY kosong. Fitur AI bisa gagal.")

from telegram import (
    Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    CallbackQueryHandler
)
from telegram.error import BadRequest
import google.generativeai as genai
genai.configure(api_key=GEMINI_API_KEY)

# ====== pilih model ======
def pick_gemini_model() -> str:
    for m in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try: _ = genai.GenerativeModel(m); return m
        except: continue
    return "gemini-1.5-flash"
GEMINI_MODEL = pick_gemini_model()

# ====== sumber berita ======
def fetch_google_news(topic: str, lang="id", region="ID", limit=5):
    q = requests.utils.quote(topic)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid={region}:{lang}"
    feed = feedparser.parse(url)
    items=[]
    for e in feed.entries[:limit]:
        img=""
        if "media_content" in e and e.media_content:
            img=e.media_content[0].get("url","")
        items.append({
            "title": getattr(e,"title",""),
            "url": getattr(e,"link",""),
            "published": getattr(e,"published",""),
            "image": img
        })
    return items

def fetch_wikipedia_facts(lang="id", limit=3):
    base = f"https://{lang}.wikipedia.org/api/rest_v1/page/random/summary"
    out=[]
    for _ in range(limit):
        try:
            r=requests.get(base,timeout=10)
            if r.status_code==200:
                d=r.json()
                url=(d.get("content_urls",{}).get("desktop",{}) or {}).get("page","")
                thumb=d.get("thumbnail",{}).get("source","")
                if d.get("title") and d.get("extract") and url:
                    out.append({"title": d["title"], "summary": d["extract"], "url": url, "image": thumb})
        except: pass
    return out

# ====== Gemini rewrite ======
def gemini_rewrite(mode, lang, sources, nvar=3):
    locale = "Bahasa Indonesia" if lang=="id" else "English"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_blob=json.dumps(sources,ensure_ascii=False,indent=2)
    prompt=f"""
You are a social media assistant. Today: {now_str}.
Language: {locale}.
Given these sources (news or facts): {src_blob}

TASK: Create {nvar} different social-media posts. 
Each must include:
- A short catchy rewritten title (max 75 chars).
- 20 hashtags (lowercase, no spaces, with `#`).

Return strict JSON:
{{"posts":[{{"title":"...","hashtags":["#.."]}}]}}
"""
    try:
        model=genai.GenerativeModel(GEMINI_MODEL)
        resp=model.generate_content(prompt)
        text=(resp.text or "").strip()
        text=re.sub(r"^```(?:json)?|```$","",text,flags=re.MULTILINE).strip()
        data=json.loads(text)
        return data.get("posts",[])
    except Exception as e:
        return [{"title":"Konten Menarik","hashtags":["#info","#viral"]}]

# ====== UI ======
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb=[
        [InlineKeyboardButton("📰 News",callback_data="mode:news"),
         InlineKeyboardButton("📘 Facts",callback_data="mode:facts")]
    ]
    await update.message.reply_text(
        "👋 Pilih mode untuk ambil berita unik/fakta.",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    cid=q.message.chat_id
    data=q.data.split(":")
    try:
        mode=data[1]
        await q.edit_message_text("⏳ Mengambil sumber...")
        if mode=="news":
            sources=fetch_google_news("viral", lang="id", region="ID", limit=3)
        else:
            sources=fetch_wikipedia_facts("id",limit=3)
        if not sources:
            return await context.bot.send_message(cid,"❌ Tidak ada sumber.")
        posts=gemini_rewrite(mode,"id",sources,3)
        for s in sources:
            cap=f"**{posts[0]['title']}**\n\n" + " ".join(posts[0]["hashtags"])
            if s.get("image"):
                await context.bot.send_photo(cid,photo=s["image"],caption=cap,parse_mode="Markdown")
            else:
                await context.bot.send_message(cid,cap,parse_mode="Markdown")
    except Exception as e:
        await context.bot.send_message(cid,f"❌ Error: {e}")

# ====== main ======
def main():
    app=Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    print(f"Bot running... (Gemini {GEMINI_MODEL})")
    app.run_polling(close_loop=False)

if __name__=="__main__":
    main()
