#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Viral News / Fakta Unik -> Video Overlay (Shorts) + Button UI
"""

import os, re, json, uuid, tempfile, asyncio, textwrap
from datetime import datetime, timezone
from typing import List, Dict

# ====== ENV ======
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak ditemukan/invalid. Cek file .env")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY kosong. Fitur AI bisa gagal.")

# ====== Imports lib ======
import feedparser, requests
from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip, ColorClip
from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
    CallbackQueryHandler
)
import google.generativeai as genai

# ====== Gemini config ======
genai.configure(api_key=GEMINI_API_KEY)
def pick_gemini_model() -> str:
    for m in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try: _ = genai.GenerativeModel(m); return m
        except: continue
    return "gemini-1.5-flash"
GEMINI_MODEL = pick_gemini_model()

# ====== State ======
SESSION: Dict[int, Dict[str, str]] = {}   # {chat_id: {"video_path": str}}
JOB_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue()
WORKER_STARTED = False

# ---------- Helpers sumber ----------
def fetch_google_news(topic: str, lang="id", region="ID", limit=5):
    q = requests.utils.quote(topic)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid={region}:{lang}"
    feed = feedparser.parse(url)
    return [{"title": e.title, "url": e.link, "published": getattr(e,"published","")} for e in feed.entries[:limit]]

def fetch_wikipedia_facts(lang="id", limit=3):
    base = f"https://{lang}.wikipedia.org/api/rest_v1/page/random/summary"
    out=[]
    for _ in range(limit):
        try:
            r=requests.get(base,timeout=10)
            if r.status_code==200:
                d=r.json()
                if d.get("title") and d.get("extract") and d.get("content_urls",{}).get("desktop",{}).get("page"):
                    out.append({"title":d["title"],"summary":d["extract"],"url":d["content_urls"]["desktop"]["page"]})
        except: pass
    return out

# ---------- Gemini overlay ----------
def gemini_overlay_and_carousel(mode, lang, sources, dur, nvar):
    locale = "Bahasa Indonesia" if lang=="id" else "English"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_text=[]
    if mode=="news":
        for i,s in enumerate(sources,1):
            src_text.append(f"{i}. {s.get('title')}\n{s.get('url')}")
    else:
        for i,s in enumerate(sources,1):
            snippet=(s.get("summary") or "")[:300]
            src_text.append(f"{i}. {s.get('title')}\n{s.get('url')}\n{snippet}")
    src_blob="\n".join(src_text) or "(no sources)"
    prompt=f"""
Write overlay and 3–5 FB title+hashtags. Locale: {locale}, Duration {dur}s.
SOURCES:
{src_blob}
Return JSON: {{"overlay_script":"..","credits":"..","variants":[{{"title":"..","hashtags":["#.."]}}]}}
"""
    try:
        model=genai.GenerativeModel(GEMINI_MODEL)
        resp=model.generate_content(prompt)
        text=(resp.text or "").strip()
        text=re.sub(r"^```(?:json)?|```$","",text,flags=re.MULTILINE).strip()
        data=json.loads(text)
        return data
    except:
        return {"overlay_script":"Info hari ini","credits":"","variants":[{"title":"Konten Menarik","hashtags":["#info","#viral"]}]}

# ---------- Render ----------
def _tc_height_guess(ph:int)->int: return int(ph*0.1)
def render_video_with_overlay(bg, lines, credits, out, dur=60):
    from moviepy.editor import TextClip
    clip=VideoFileClip(bg); sub=clip.subclip(0,min(float(clip.duration),dur)); W,H=sub.size
    panel_h=int(H*0.32); panel_y=int(H*0.6)
    panel=(ColorClip((W,panel_h),color=(0,0,0)).set_opacity(0.35).set_duration(sub.duration).set_position(("center",panel_y-panel_h//2)))
    joined="\n".join(lines); fontsize=max(28,int(H*0.04))
    def wrap(text,wpx,fs): return "\n".join(textwrap.wrap(text,width=max(12,int(wpx/(fs*0.6)))))
    try:
        txt=(TextClip(txt=joined,fontsize=fontsize,color="white",font="DejaVu-Sans",
             stroke_color="black",stroke_width=max(1,fontsize//14),
             method="caption",size=(int(W*0.88),None),align="center")
             .set_duration(sub.duration).set_position(("center",panel_y-_tc_height_guess(panel_h))))
    except:
        txt=(TextClip(txt=wrap(joined,int(W*0.88),fontsize),fontsize=fontsize,color="white",font="DejaVu-Sans",
             stroke_color="black",stroke_width=max(1,fontsize//14),method="label")
             .set_duration(sub.duration).set_position(("center",panel_y-_tc_height_guess(panel_h))))
    credit_font=max(20,int(H*0.025))
    credits_clip=(TextClip(txt=credits,fontsize=credit_font,color="white",font="DejaVu-Sans",
                    stroke_color="black",stroke_width=max(1,credit_font//10),method="label")
                    .set_duration(sub.duration).set_position((int(W*0.02),int(H*0.94))))
    CompositeVideoClip([sub,panel,txt,credits_clip]).write_videofile(out,codec="libx264",audio_codec="aac",fps=30)

# ---------- Telegram handlers ----------
async def start(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    SESSION.pop(chat_id,None)  # reset
    await update.message.reply_text("👋 Halo! Kirim video MP4 untuk dijadikan background.")

async def save_video(update:Update, context:ContextTypes.DEFAULT_TYPE):
    chat_id=update.effective_chat.id
    vid=update.message.video or update.message.document
    if not vid: return await update.message.reply_text("Kirim file video MP4 ya.")
    os.makedirs("data",exist_ok=True)
    f=await context.bot.get_file(vid.file_id)
    local=os.path.join("data",f"{chat_id}_{uuid.uuid4().hex}.mp4")
    await f.download_to_drive(local)
    SESSION[chat_id]={"video_path":local}
    kb=[[InlineKeyboardButton("📰 News 🇮🇩",callback_data="make:news:id"),
         InlineKeyboardButton("📘 Facts 🇮🇩",callback_data="make:facts:id")],
        [InlineKeyboardButton("📰 News 🇬🇧",callback_data="make:news:en"),
         InlineKeyboardButton("📘 Facts 🇬🇧",callback_data="make:facts:en")]]
    await update.message.reply_text("✅ Video disimpan.\nPilih mode:",reply_markup=InlineKeyboardMarkup(kb))

async def button_handler(update:Update, context:ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    chat_id=q.message.chat_id
    if not SESSION.get(chat_id) or not os.path.exists(SESSION[chat_id].get("video_path","")):
        return await q.edit_message_text("❌ Belum ada video. Kirim video MP4 dulu.")
    _,mode,lang=q.data.split(":")
    job={"chat_id":chat_id,"bg_path":SESSION[chat_id]["video_path"],"mode":mode,"lang":lang,"dur":60,"variants":4}
    await JOB_QUEUE.put(job)
    await q.edit_message_text(f"🧾 Job ditambahkan: {mode}/{lang}, dur=60s, variants=4")

    global WORKER_STARTED
    if not WORKER_STARTED:
        WORKER_STARTED=True
        asyncio.create_task(worker(context.application))

async def worker(app:Application):
    while True:
        job=await JOB_QUEUE.get(); chat_id=job["chat_id"]
        try:
            await app.bot.send_message(chat_id,"⏳ Memproses job...")
            if job["mode"]=="news":
                news=[]
                for t in ["trending","viral","breaking","unik","teknologi","hiburan"]:
                    news+=fetch_google_news(t,lang=job["lang"],region="ID" if job["lang"]=="id" else "US",limit=2)
                seen=set(); sources=[]
                for n in news:
                    if n["url"] not in seen: seen.add(n["url"]); sources.append(n)
            else:
                sources=fetch_wikipedia_facts(job["lang"],limit=3)
            if not sources: 
                await app.bot.send_message(chat_id,"❗Gagal ambil sumber."); continue
            data=gemini_overlay_and_carousel(job["mode"],job["lang"],sources,job["dur"],job["variants"])
            overlay=[ln.strip() for ln in data.get("overlay_script","").splitlines() if ln.strip()][:6]
            credits=data.get("credits","")
            variants=data.get("variants",[])
            outdir=tempfile.mkdtemp(prefix="out_")
            out_video=os.path.join(outdir,f"short_{uuid.uuid4().hex}.mp4")
            render_video_with_overlay(job["bg_path"],overlay,credits,out_video,job["dur"])
            capfile=os.path.join(outdir,"caption_variants.txt")
            with open(capfile,"w",encoding="utf-8") as f:
                for i,v in enumerate(variants,1):
                    f.write(f"[{i}] {v.get('title','')}\n{' '.join(v.get('hashtags',[]))}\n\n")
                if credits: f.write(credits+"\n")
            await app.bot.send_video(chat_id,video=InputFile(out_video),caption=(variants[0].get("title") if variants else "Konten Menarik"))
            await app.bot.send_document(chat_id,document=InputFile(capfile),caption="Judul & hashtag")
        except Exception as e:
            await app.bot.send_message(chat_id,f"❌ Error: {e}")
        finally: JOB_QUEUE.task_done()

# ---------- Main ----------
def main():
    app=Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",start))
    app.add_handler(MessageHandler(filters.VIDEO|filters.Document.VIDEO,save_video))
    app.add_handler(CallbackQueryHandler(button_handler))
    print(f"Bot running... (Gemini model: {GEMINI_MODEL})")
    app.run_polling(close_loop=False)

if __name__=="__main__": main()
