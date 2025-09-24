#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Viral News / Fakta Unik -> Video Overlay (Shorts)
- UI tombol (mode/bahasa/variants, durasi ikut background)
- Tanpa ImageMagick (Pillow -> ImageClip)
- Auto-resize 9:16 (default 1080x1920)
- Kompatibel Pillow 10+ (shim Resampling)
- Pre-normalize input ke H.264/AAC (yuv420p 30fps) dengan ffmpeg
- Output streamable (yuv420p + faststart) + audio
"""

import os, re, json, uuid, tempfile, asyncio, subprocess, shutil
from datetime import datetime, timezone
from typing import List, Dict, Tuple

from dotenv import load_dotenv
load_dotenv()
TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()
if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak valid. Cek .env (TELEGRAM_BOT_TOKEN=XXX:YYY).")
if not GEMINI_API_KEY:
    print("⚠️ GEMINI_API_KEY kosong. Fitur AI bisa gagal.")

# ====== libs ======
import feedparser, requests, numpy as np
from PIL import Image, ImageDraw, ImageFont
# Pillow 10 shim (ANTIALIAS dihapus)
try:
    from PIL.Image import Resampling
    if not hasattr(Image, "ANTIALIAS"):
        Image.ANTIALIAS = Resampling.LANCZOS
        Image.LANCZOS   = Resampling.LANCZOS
        Image.BICUBIC   = Resampling.BICUBIC
        Image.BILINEAR  = Resampling.BILINEAR
        Image.NEAREST   = Resampling.NEAREST
except Exception:
    pass

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
genai.configure(api_key=GEMINI_API_KEY)

def pick_gemini_model() -> str:
    for m in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try: _ = genai.GenerativeModel(m); return m
        except: continue
    return "gemini-1.5-flash"
GEMINI_MODEL = pick_gemini_model()

# ====== state ======
SESSION: Dict[int, Dict[str, str]] = {}
JOB_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue()
WORKER_STARTED = False

# ====== defaults & canvas ======
DEF_MODE = "news"
DEF_LANG = "id"
DEF_VAR  = 4
CANVAS_W = 1080
CANVAS_H = 1920

# ---------- helpers sumber ----------
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

# ---------- gemini ----------
def gemini_overlay_and_carousel(mode, lang, sources, nvar):
    locale = "Bahasa Indonesia" if lang=="id" else "English"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    src=[]
    if mode=="news":
        for i,s in enumerate(sources,1):
            src.append(f"{i}. {s.get('title')}\n   {s.get('url')}\n   {s.get('published','')}")
    else:
        for i,s in enumerate(sources,1):
            t=(s.get("summary") or "")
            if len(t)>300: t=t[:300]+"..."
            src.append(f"{i}. {s.get('title')}\n   {s.get('url')}\n   {t}")
    src_blob="\n".join(src) if src else "(no sources)"
    prompt=f"""
You are an assistant for short vertical videos. Today: {now_str}. Write in {locale}.
TASK A: Overlay script for a vertical video (3–6 short lines, ~360 chars, factual, safe, no hashtags).
TASK B: {nvar} Facebook metadata variants: title (<=75 chars) + 20 hashtags (lowercase; add 5 Indonesia-specific if locale Indonesian).
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

# ---------- teks (Pillow) ----------
def _find_font_path() -> str:
    for p in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
    ]:
        if os.path.exists(p): return p
    return ""

from PIL import ImageDraw, ImageFont, Image
def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int):
    lines=[]
    for para in text.splitlines():
        words=para.strip().split()
        if not words: lines.append(""); continue
        cur=words[0]
        for w in words[1:]:
            t=f"{cur} {w}"
            if draw.textlength(t,font=font)<=max_w: cur=t
            else: lines.append(cur); cur=w
        lines.append(cur)
    return lines

def text_image(text: str, w: int, h: int, fs: int, align="center", stroke=2):
    img=Image.new("RGBA",(w,h),(0,0,0,0))
    dr=ImageDraw.Draw(img)
    fp=_find_font_path()
    try: font=ImageFont.truetype(fp,fs) if fp else ImageFont.load_default()
    except: font=ImageFont.load_default()
    lines=_wrap(dr,text,font,w)
    line_h=int(fs*1.25); total=line_h*len(lines); y=max(0,(h-total)//2)
    for line in lines:
        tw=int(dr.textlength(line,font=font))
        x=max(0,(w-tw)//2) if align=="center" else (w-tw if align=="right" else 0)
        if stroke>0:
            for dx in range(-stroke,stroke+1):
                for dy in range(-stroke,stroke+1):
                    if dx==0 and dy==0: continue
                    dr.text((x+dx,y+dy),line,font=font,fill=(0,0,0,255))
        dr.text((x,y),line,font=font,fill=(255,255,255,255))
        y+=line_h
    return img

# ---------- ffmpeg helpers ----------
def have_ffmpeg()->bool:
    return shutil.which("ffmpeg") is not None

def normalize_video(inp_path:str)->str:
    """
    Transcode ke H.264 yuv420p + AAC 44.1kHz + 30fps + faststart
    agar sumber selalu kompatibel. Kembalikan path output sementara.
    """
    if not have_ffmpeg():
        return inp_path
    out_path=os.path.join(tempfile.mkdtemp(prefix="norm_"), "norm.mp4")
    cmd=[
        "ffmpeg","-y","-i",inp_path,
        "-c:v","libx264","-pix_fmt","yuv420p","-r","30",
        "-c:a","aac","-ar","44100","-b:a","128k",
        "-movflags","+faststart",
        out_path
    ]
    subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    if os.path.exists(out_path) and os.path.getsize(out_path)>0:
        return out_path
    return inp_path  # fallback

# ---------- canvas fit ----------
def fit_to_canvas(clip: VideoFileClip, W: int, H: int) -> CompositeVideoClip:
    vw, vh = clip.size
    scale = min(W/vw, H/vh)
    nw, nh = int(vw*scale), int(vh*scale)
    resized = clip.resize((nw, nh))
    x = (W-nw)//2; y = (H-nh)//2
    bg = ColorClip((W,H), color=(0,0,0)).set_duration(clip.duration)
    return CompositeVideoClip([bg, resized.set_position((x,y))])

def render(bg_path:str, overlay_lines:List[str], credits:str, out_path:str)->float:
    """
    Durasi ikut source, audio dibawa. Output H.264+AAC streamable.
    Return: duration (sec)
    """
    # normalize input
    src_norm = normalize_video(bg_path)

    clip = VideoFileClip(src_norm, audio=True)
    duration=float(clip.duration)
    fps=int(round(getattr(clip,"fps",30) or 30))

    base = fit_to_canvas(clip, CANVAS_W, CANVAS_H)
    # panel
    panel_h=int(CANVAS_H*0.32); panel_y=int(CANVAS_H*0.6)
    panel=(ColorClip(size=(CANVAS_W,panel_h), color=(0,0,0))
           .set_opacity(0.35).set_duration(base.duration)
           .set_position(("center", panel_y - panel_h//2)))
    # text
    joined="\n".join(overlay_lines)
    fs=max(28,int(CANVAS_H*0.04))
    t_w=int(CANVAS_W*0.88); t_h=panel_h-int(panel_h*0.2)
    t_img = text_image(joined, t_w, t_h, fs, "center", stroke=max(1,fs//14))
    txt = (ImageClip(np.array(t_img)).set_duration(base.duration)
           .set_position(("center", panel_y - int(panel_h*0.1))))
    # credits
    c_fs=max(20,int(CANVAS_H*0.025))
    c_w, c_h = int(CANVAS_W*0.5), int(CANVAS_H*0.06)
    c_img = text_image(credits, c_w, c_h, c_fs, "left", stroke=max(1,c_fs//10))
    cred = (ImageClip(np.array(c_img)).set_duration(base.duration)
            .set_position((int(CANVAS_W*0.02), int(CANVAS_H*0.94)-c_h)))

    final = CompositeVideoClip([base, panel, txt, cred])
    if base.audio is not None:
        final = final.set_audio(base.audio)

    final.write_videofile(
        out_path,
        codec="libx264",
        audio_codec="aac",
        audio=True,
        fps=fps,
        threads=2,
        preset="medium",
        bitrate="3500k",
        ffmpeg_params=["-pix_fmt","yuv420p","-movflags","+faststart"],
        temp_audiofile=os.path.join(tempfile.gettempdir(), f"tmp_audio_{uuid.uuid4().hex}.m4a"),
        remove_temp=True,
        logger=None,
    )

    # tutup & flush
    try: final.close()
    except: pass
    try: base.close()
    except: pass
    try: clip.close()
    except: pass

    if not os.path.exists(out_path) or os.path.getsize(out_path)==0:
        raise RuntimeError("Render gagal: file output kosong.")
    return duration

# ---------- UI ----------
def _init_defaults(chat_id:int):
    SESSION[chat_id]=SESSION.get(chat_id,{})
    SESSION[chat_id].update({"mode":DEF_MODE,"lang":DEF_LANG,"variants":DEF_VAR})

def _label(cur,val,txt): return f"✅ {txt}" if cur==val else txt

def _menu(chat_id:int)->Tuple[str,InlineKeyboardMarkup]:
    st=SESSION.get(chat_id,{})
    mode=st.get("mode",DEF_MODE); lang=st.get("lang",DEF_LANG); var=int(st.get("variants",DEF_VAR))
    text=(f"🎛 **Pengaturan**\n"
          f"- Mode: `{mode}`\n"
          f"- Bahasa: `{lang}`\n"
          f"- Variants judul/hashtag: `{var}`\n"
          f"- Canvas: `{CANVAS_W}x{CANVAS_H}` (9:16)\n"
          f"- Durasi output: **mengikuti video background**\n\n"
          f"Tekan tombol untuk mengubah, lalu **Render ▶️**.")
    kb=[
        [InlineKeyboardButton(_label(mode,"news","📰 News"),callback_data="set:mode:news"),
         InlineKeyboardButton(_label(mode,"facts","📘 Facts"),callback_data="set:mode:facts")],
        [InlineKeyboardButton(_label(lang,"id","🇮🇩 ID"),callback_data="set:lang:id"),
         InlineKeyboardButton(_label(lang,"en","🇬🇧 EN"),callback_data="set:lang:en")],
        [InlineKeyboardButton(_label(var,3,"3 var"),callback_data="set:var:3"),
         InlineKeyboardButton(_label(var,4,"4 var"),callback_data="set:var:4"),
         InlineKeyboardButton(_label(var,5,"5 var"),callback_data="set:var:5")],
        [InlineKeyboardButton("▶️ Render",callback_data="go"),
         InlineKeyboardButton("🔁 Reset", callback_data="reset")]
    ]
    return text, InlineKeyboardMarkup(kb)

# ---------- Telegram handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    SESSION.pop(cid, None)
    await update.message.reply_text(
        "👋 Kirim video MP4 (Shorts/Reels). Bot akan normalize codec, resize 9:16 "
        f"({CANVAS_W}x{CANVAS_H}), dan **durasi mengikuti video**. Setelah terkirim, muncul menu tombol."
    )

async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cid=update.effective_chat.id
    vid=update.message.video or update.message.document
    if not vid: return await update.message.reply_text("Kirim file video MP4 ya.")
    os.makedirs("data",exist_ok=True)
    tgfile=await context.bot.get_file(vid.file_id)
    local=os.path.join("data",f"{cid}_{uuid.uuid4().hex}.mp4")
    await tgfile.download_to_drive(local)
    _init_defaults(cid)
    SESSION[cid]["video_path"]=local
    text,kb=_menu(cid)
    await update.message.reply_text("✅ Video disimpan.")
    await update.message.reply_text(text,reply_markup=kb,parse_mode="Markdown")

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q=update.callback_query; await q.answer()
    cid=q.message.chat_id
    if not SESSION.get(cid) or not os.path.exists(SESSION[cid].get("video_path","")):
        try: return await q.edit_message_text("❌ Belum ada video. Kirim video MP4 dulu.")
        except: return
    data=q.data.split(":")
    try:
        if data[0]=="set":
            _,k,v=data
            if k=="mode": SESSION[cid]["mode"]=v
            elif k=="lang": SESSION[cid]["lang"]=v
            elif k=="var": SESSION[cid]["variants"]=max(3,min(5,int(v)))
            text,kb=_menu(cid)
            if q.message.text!=text: await q.edit_message_text(text,reply_markup=kb,parse_mode="Markdown")
            else: await q.edit_message_reply_markup(reply_markup=kb)
            return
        if data[0]=="reset":
            _init_defaults(cid)
            text,kb=_menu(cid)
            if q.message.text!=text: await q.edit_message_text(text,reply_markup=kb,parse_mode="Markdown")
            else: await q.edit_message_reply_markup(reply_markup=kb)
            return
        if data[0]=="go":
            st=SESSION[cid]
            job={"chat_id":cid,"bg_path":st["video_path"],"mode":st["mode"],
                 "lang":st["lang"],"variants":int(st["variants"])}
            await JOB_QUEUE.put(job)
            await q.edit_message_text(
                f"🧾 Job ditambahkan: {job['mode']}/{job['lang']}, variants={job['variants']}. "
                "Durasi mengikuti background. Menunggu giliran…"
            )
            global WORKER_STARTED
            if not WORKER_STARTED:
                WORKER_STARTED=True
                asyncio.create_task(worker(context.application))
            return
    except Exception as e:
        try: await q.edit_message_text(f"❌ Error: {e}")
        except: pass

async def worker(app: Application):
    while True:
        job=await JOB_QUEUE.get()
        cid=job["chat_id"]
        try:
            await app.bot.send_message(cid,"⏳ Memproses: ambil sumber → Gemini → render…")
            # sumber
            if job["mode"]=="news":
                news=[]
                for t in ["trending","viral","breaking","unik","teknologi","hiburan"]:
                    try: news+=fetch_google_news(t, lang="id" if job["lang"]=="id" else "en",
                                                region="ID" if job["lang"]=="id" else "US", limit=2)
                    except: pass
                sources=[]; seen=set()
                for n in news:
                    u=n.get("url")
                    if u and u not in seen: seen.add(u); sources.append(n)
                sources=sources[:5]
            else:
                sources=fetch_wikipedia_facts(job["lang"],limit=3)
            if not sources:
                await app.bot.send_message(cid,"❗Gagal mengambil sumber. Coba lagi.")
                continue

            data=gemini_overlay_and_carousel(job["mode"], job["lang"], sources, job["variants"])
            overlay=[ln.strip() for ln in (data.get("overlay_script","").splitlines()) if ln.strip()]
            if len(overlay)<3: overlay+=[""]*(3-len(overlay))
            overlay=overlay[:6]
            credits=data.get("credits","")
            variants=data.get("variants",[])

            outdir=tempfile.mkdtemp(prefix="out_")
            out_video=os.path.join(outdir,f"short_{uuid.uuid4().hex}.mp4")
            duration = render(job["bg_path"], overlay, credits, out_video)

            cap=os.path.join(outdir,"caption_variants.txt")
            with open(cap,"w",encoding="utf-8") as f:
                for i,v in enumerate(variants,1):
                    t=(v.get("title") or "").strip()
                    hs=[h if h.startswith("#") else "#"+h for h in (v.get("hashtags",[]) or [])]
                    f.write(f"[{i}] {t}\n"); f.write(" ".join(hs)+"\n\n")
                if credits: f.write(credits.strip()+"\n")

            caption=(variants[0].get("title") if variants else "Konten Menarik")[:1000]
            await app.bot.send_video(
                cid,
                video=InputFile(out_video, filename="output.mp4"),
                caption=caption,
                width=CANVAS_W, height=CANVAS_H,
                duration=int(round(duration)),
                supports_streaming=True,
            )
            await app.bot.send_document(
                cid,
                document=InputFile(cap, filename="caption_variants.txt"),
                caption="Judul & hashtag (3–5 variasi)"
            )
        except Exception as e:
            try: await app.bot.send_message(cid,f"❌ Error: {e}")
            except: pass
        finally:
            JOB_QUEUE.task_done()

# ---------- main ----------
def main():
    app=Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, save_video))
    app.add_handler(CallbackQueryHandler(on_button))
    print(f"Bot running... (Gemini {GEMINI_MODEL}; canvas {CANVAS_W}x{CANVAS_H}; durasi ikut background)")
    app.run_polling(close_loop=False)

if __name__=="__main__":
    main()
