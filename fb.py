#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Viral News / Fakta Unik -> Video Overlay (Shorts) + FULL Button UI
- Tanpa ImageMagick (render teks via Pillow → ImageClip)
- Queue render + carousel judul/hashtag
"""

import os, re, json, uuid, tempfile, asyncio, textwrap, math
from datetime import datetime, timezone
from typing import List, Dict, Tuple

# ====== ENV ======
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()

if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak ditemukan/invalid. Cek file .env (KEY=VALUE).")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY kosong. Fitur AI bisa gagal.")

# ====== Imports lib ======
import feedparser, requests
from PIL import Image, ImageDraw, ImageFont
from moviepy.editor import (
    VideoFileClip, CompositeVideoClip, ColorClip, ImageClip
)
from telegram import (
    Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
)
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
# per chat: video_path, mode, lang, dur, variants
SESSION: Dict[int, Dict[str, str]] = {}
JOB_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue()
WORKER_STARTED = False

# ====== Defaults ======
DEF_MODE = "news"     # or "facts"
DEF_LANG = "id"       # "id"/"en"
DEF_DUR  = 60         # 20..90
DEF_VAR  = 4          # 3..5

# ---------- Sumber ----------
def fetch_google_news(topic: str, lang="id", region="ID", limit=5):
    q = requests.utils.quote(topic)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid={region}:{lang}"
    feed = feedparser.parse(url)
    return [{"title": getattr(e,"title",""), "url": getattr(e,"link",""), "published": getattr(e,"published","")}
            for e in feed.entries[:limit]]

def fetch_wikipedia_facts(lang="id", limit=3):
    base = f"https://{lang}.wikipedia.org/api/rest_v1/page/random/summary"
    out=[]
    for _ in range(limit):
        try:
            r=requests.get(base,timeout=10)
            if r.status_code==200:
                d=r.json()
                url=(d.get("content_urls",{}).get("desktop",{}) or {}).get("page","")
                if d.get("title") and d.get("extract") and url:
                    out.append({"title": d["title"], "summary": d["extract"], "url": url})
        except: pass
    return out

# ---------- Gemini ----------
def gemini_overlay_and_carousel(mode, lang, sources, dur, nvar):
    locale = "Bahasa Indonesia" if lang=="id" else "English"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src_text=[]
    if mode=="news":
        for i,s in enumerate(sources,1):
            src_text.append(f"{i}. {s.get('title')}\n   {s.get('url')}\n   {s.get('published','')}")
    else:
        for i,s in enumerate(sources,1):
            snippet=(s.get("summary") or "")
            if len(snippet)>300: snippet=snippet[:300]+"..."
            src_text.append(f"{i}. {s.get('title')}\n   {s.get('url')}\n   {snippet}")
    src_blob="\n".join(src_text) if src_text else "(no sources)"
    prompt=f"""
You are an assistant for short vertical videos. Today: {now_str}. Write in {locale}.
TASK A: Overlay script for {dur}s vertical video → 3–6 short lines (~360 chars), factual, safe, no hashtags.
TASK B: {nvar} variants for Facebook → title (<=75 chars) + 20 hashtags (lowercase; if locale is Indonesian include 5 Indonesia-specific).
Also output a short credits line (1–2 source domains).
SOURCES:
{src_blob}
Return strict JSON:
{{"overlay_script":"Line1\\nLine2...","credits":"Sumber: ...","variants":[{{"title":"...","hashtags":["#.."]}}]}}
"""
    try:
        model=genai.GenerativeModel(GEMINI_MODEL)
        resp=model.generate_content(prompt)
        text=(resp.text or "").strip()
        text=re.sub(r"^```(?:json)?|```$","",text,flags=re.MULTILINE).strip()
        data=json.loads(text)
        # guards
        if not isinstance(data.get("variants",[]), list):
            data["variants"]=[]
        data["variants"]=data["variants"][:max(1,min(5,nvar))]
        data.setdefault("overlay_script","Info menarik hari ini.")
        data.setdefault("credits","")
        if not data["variants"]:
            data["variants"]=[{"title":"Konten Menarik","hashtags":["#info","#viral"]}]
        return data
    except Exception:
        return {"overlay_script":"Info menarik hari ini.","credits":"",
                "variants":[{"title":"Konten Menarik","hashtags":["#info","#viral"]}]}

# ---------- UTIL TEKS PIL (tanpa ImageMagick) ----------
def _find_font_path() -> str:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]
    for p in candidates:
        if os.path.exists(p): return p
    return ""  # Pillow default bitmap font fallback

def _wrap_pil(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> List[str]:
    lines=[]
    for para in text.splitlines():
        words=para.strip().split()
        if not words: lines.append(""); continue
        cur=words[0]
        for w in words[1:]:
            test=f"{cur} {w}"
            if draw.textlength(test, font=font) <= max_width:
                cur=test
            else:
                lines.append(cur)
                cur=w
        lines.append(cur)
    return lines

def _text_image(text: str, box_w: int, box_h: int, fontsize: int, align: str="center",
                stroke_w: int=2, fill=(255,255,255,255), stroke_fill=(0,0,0,255)) -> Image.Image:
    """
    Buat gambar RGBA berukuran (box_w x box_h) berisi text wrap+stroke.
    """
    img = Image.new("RGBA", (box_w, box_h), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    font_path = _find_font_path()
    try:
        font = ImageFont.truetype(font_path, fontsize) if font_path else ImageFont.load_default()
    except:
        font = ImageFont.load_default()

    lines = _wrap_pil(draw, text, font, box_w)
    line_h = int(fontsize * 1.25)
    total_h = line_h * len(lines)
    y = max(0, (box_h - total_h)//2)

    for line in lines:
        w = int(draw.textlength(line, font=font))
        if align == "center":
            x = max(0, (box_w - w)//2)
        elif align == "right":
            x = max(0, box_w - w)
        else:
            x = 0
        # stroke (outline)
        if stroke_w > 0:
            for dx in range(-stroke_w, stroke_w+1):
                for dy in range(-stroke_w, stroke_w+1):
                    if dx==0 and dy==0: continue
                    draw.text((x+dx, y+dy), line, font=font, fill=stroke_fill)
        # fill
        draw.text((x, y), line, font=font, fill=fill)
        y += line_h

    return img

# ---------- RENDER VIDEO DENGAN OVERLAY ----------
def _tc_height_guess(panel_h:int)->int: return int(panel_h*0.1)

def render_video_with_overlay(bg_path: str, overlay_lines: List[str],
                              credits: str, out_path: str, target_duration: int = 60):
    """
    Render tanpa ImageMagick:
      - panel semi transparan (ColorClip)
      - teks overlay & credits digambar via PIL → ImageClip
    """
    clip = VideoFileClip(bg_path)
    duration = min(float(clip.duration), float(target_duration))
    sub = clip.subclip(0, duration)
    W, H = sub.size

    # Panel gelap
    panel_h = int(H * 0.32)
    panel_y = int(H * 0.6)
    panel = (ColorClip(size=(W, panel_h), color=(0, 0, 0))
             .set_opacity(0.35).set_duration(sub.duration)
             .set_position(("center", panel_y - panel_h // 2)))

    # Teks overlay (gabung lines)
    joined = "\n".join(overlay_lines)
    fontsize = max(28, int(H * 0.04))
    text_box_w = int(W * 0.88)
    text_box_h = panel_h - int(panel_h*0.2)
    text_img = _text_image(joined, text_box_w, text_box_h, fontsize, align="center",
                           stroke_w=max(1, fontsize//14))

    text_clip = (ImageClip(text_img)
                 .set_duration(sub.duration)
                 .set_position(("center", panel_y - _tc_height_guess(panel_h))))

    # Credits kecil kiri bawah
    credit_font = max(20, int(H * 0.025))
    cred_box_w, cred_box_h = int(W * 0.5), int(H * 0.06)
    cred_img = _text_image(credits, cred_box_w, cred_box_h, credit_font, align="left",
                           stroke_w=max(1, credit_font//10))
    cred_clip = (ImageClip(cred_img)
                 .set_duration(sub.duration)
                 .set_position((int(W*0.02), int(H*0.94) - cred_box_h)))

    final = CompositeVideoClip([sub, panel, text_clip, cred_clip])
    final.write_videofile(out_path, codec="libx264", audio_codec="aac",
                          fps=30, threads=0, preset="medium", bitrate="3500k")

# ---------- UI / Buttons ----------
def _init_defaults(chat_id:int):
    SESSION[chat_id] = SESSION.get(chat_id, {})
    SESSION[chat_id].update({"mode": DEF_MODE, "lang": DEF_LANG, "dur": DEF_DUR, "variants": DEF_VAR})

def _label(cur, val, txt):
    return f"✅ {txt}" if cur == val else txt

def _build_menu(chat_id:int) -> Tuple[str, InlineKeyboardMarkup]:
    st = SESSION.get(chat_id, {})
    mode = st.get("mode", DEF_MODE)
    lang = st.get("lang", DEF_LANG)
    dur = int(st.get("dur", DEF_DUR))
    var = int(st.get("variants", DEF_VAR))

    text = (f"🎛 **Pengaturan**\n"
            f"- Mode: `{mode}`\n"
            f"- Bahasa: `{lang}`\n"
            f"- Durasi: `{dur}s`\n"
            f"- Variants: `{var}`\n\n"
            f"Tekan tombol untuk mengubah, lalu **Render ▶️**.")

    kb = [
        [InlineKeyboardButton(_label(mode,"news","📰 News"),  callback_data="set:mode:news"),
         InlineKeyboardButton(_label(mode,"facts","📘 Facts"), callback_data="set:mode:facts")],
        [InlineKeyboardButton(_label(lang,"id","🇮🇩 ID"),      callback_data="set:lang:id"),
         InlineKeyboardButton(_label(lang,"en","🇬🇧 EN"),      callback_data="set:lang:en")],
        [InlineKeyboardButton(_label(dur,20,"20s"), callback_data="set:dur:20"),
         InlineKeyboardButton(_label(dur,45,"45s"), callback_data="set:dur:45"),
         InlineKeyboardButton(_label(dur,60,"60s"), callback_data="set:dur:60"),
         InlineKeyboardButton(_label(dur,90,"90s"), callback_data="set:dur:90")],
        [InlineKeyboardButton(_label(var,3,"3 var"), callback_data="set:var:3"),
         InlineKeyboardButton(_label(var,4,"4 var"), callback_data="set:var:4"),
         InlineKeyboardButton(_label(var,5,"5 var"), callback_data="set:var:5")],
        [InlineKeyboardButton("▶️ Render", callback_data="go"),
         InlineKeyboardButton("🔁 Reset",  callback_data="reset")]
    ]
    return text, InlineKeyboardMarkup(kb)

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    # reset state (kembali ke awal)
    SESSION.pop(chat_id, None)
    await update.message.reply_text("👋 Halo! Kirim video MP4 untuk dijadikan background.\n"
                                    "Setelah terkirim, kamu akan dapat menu tombol.")

async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    vid = update.message.video or update.message.document
    if not vid:
        return await update.message.reply_text("Kirim file video MP4 ya.")
    os.makedirs("data", exist_ok=True)
    f = await context.bot.get_file(vid.file_id)
    local = os.path.join("data", f"{chat_id}_{uuid.uuid4().hex}.mp4")
    await f.download_to_drive(local)
    _init_defaults(chat_id)
    SESSION[chat_id]["video_path"] = local

    text, kb = _build_menu(chat_id)
    await update.message.reply_text("✅ Video disimpan.")
    await update.message.reply_text(text, reply_markup=kb, parse_mode="Markdown")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    chat_id = q.message.chat_id

    # pastikan video ada
    if not SESSION.get(chat_id) or not os.path.exists(SESSION[chat_id].get("video_path","")):
        return await q.edit_message_text("❌ Belum ada video. Kirim video MP4 dulu.")

    data = q.data.split(":")
    if data[0] == "set":
        _, key, val = data
        if key == "mode": SESSION[chat_id]["mode"] = val
        elif key == "lang": SESSION[chat_id]["lang"] = val
        elif key == "dur":  SESSION[chat_id]["dur"]  = max(20, min(90, int(val)))
        elif key == "var":  SESSION[chat_id]["variants"] = max(3, min(5, int(val)))
        text, kb = _build_menu(chat_id)
        await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if data[0] == "reset":
        # kembali ke awal menu (tetap simpan video)
        _init_defaults(chat_id)
        text, kb = _build_menu(chat_id)
        await q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
        return

    if data[0] == "go":
        st = SESSION[chat_id]
        job = {
            "chat_id": chat_id,
            "bg_path": st["video_path"],
            "mode": st["mode"],
            "lang": st["lang"],
            "dur":  int(st["dur"]),
            "variants": int(st["variants"]),
        }
        await JOB_QUEUE.put(job)
        await q.edit_message_text(
            f"🧾 Job ditambahkan: {job['mode']}/{job['lang']}, dur={job['dur']}s, variants={job['variants']}\n"
            f"Menunggu giliran di antrian..."
        )

        global WORKER_STARTED
        if not WORKER_STARTED:
            WORKER_STARTED = True
            asyncio.create_task(worker(context.application))
        return

async def worker(app: Application):
    while True:
        job = await JOB_QUEUE.get()
        chat_id = job["chat_id"]
        try:
            await app.bot.send_message(chat_id, "⏳ Memproses job: sumber → Gemini → render...")

            # 1) sumber
            if job["mode"] == "news":
                news=[]
                for t in ["trending","viral","breaking","unik","teknologi","hiburan"]:
                    try:
                        news += fetch_google_news(
                            t,
                            lang = "id" if job["lang"]=="id" else "en",
                            region = "ID" if job["lang"]=="id" else "US",
                            limit=2
                        )
                    except: pass
                # unique by url
                sources=[]; seen=set()
                for n in news:
                    u=n.get("url")
                    if u and u not in seen:
                        seen.add(u); sources.append(n)
                sources = sources[:5]
            else:
                sources = fetch_wikipedia_facts(job["lang"], limit=3)

            if not sources:
                await app.bot.send_message(chat_id, "❗Gagal mengambil sumber. Coba lagi nanti.")
                continue

            # 2) Gemini
            data = gemini_overlay_and_carousel(job["mode"], job["lang"], sources, job["dur"], job["variants"])
            overlay = [ln.strip() for ln in (data.get("overlay_script","").splitlines()) if ln.strip()]
            if len(overlay) < 3: overlay += [""]*(3-len(overlay))
            overlay = overlay[:6]
            credits = data.get("credits","")
            variants = data.get("variants",[])

            # 3) Render
            outdir = tempfile.mkdtemp(prefix="out_")
            out_video = os.path.join(outdir, f"short_{uuid.uuid4().hex}.mp4")
            render_video_with_overlay(job["bg_path"], overlay, credits, out_video, job["dur"])

            # 4) Caption variants file
            capfile = os.path.join(outdir, "caption_variants.txt")
            with open(capfile, "w", encoding="utf-8") as f:
                for i, v in enumerate(variants, 1):
                    t = (v.get("title") or "").strip()
                    hs = v.get("hashtags", []) or []
                    hs = [h if h.startswith("#") else "#"+h for h in hs]
                    f.write(f"[{i}] {t}\n")
                    f.write(" ".join(hs) + "\n\n")
                if credits: f.write(credits.strip() + "\n")

            # 5) Kirim hasil
            caption_title = (variants[0].get("title") if variants else "Konten Menarik")[:1000]
            await app.bot.send_video(chat_id, video=InputFile(out_video), caption=caption_title)
            await app.bot.send_document(chat_id, document=InputFile(capfile), caption="Judul & hashtag (3–5 variasi)")

        except Exception as e:
            try:
                await app.bot.send_message(chat_id, f"❌ Error: {e}")
            except: pass
        finally:
            JOB_QUEUE.task_done()

# ---------- Main ----------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, save_video))
    app.add_handler(CallbackQueryHandler(on_button))
    print(f"Bot running... (Gemini model: {GEMINI_MODEL})")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
