#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram bot: Viral News / Fakta Unik -> Video Overlay (Shorts)
Fitur utama:
- Kirim video → disimpan sebagai background (per chat)
- /make mode=[news|facts] lang=[id|en] dur=[20..90] variants=[3..5]
- Output: 1 MP4 + caption_variants.txt (3–5 opsi judul & hashtag FB)
- Queue render: job diproses satu per satu

Deps:
  python-telegram-bot==21.6
  moviepy==1.0.3
  feedparser==6.0.11
  requests==2.32.3
  pillow==10.4.0
  google-generativeai==0.8.3
  python-dotenv==1.0.1
OS:
  ffmpeg, imagemagick, fonts-dejavu
"""

import os
import re
import json
import uuid
import tempfile
import asyncio
from datetime import datetime, timezone
from typing import List, Dict

# ====== ENV ======
from dotenv import load_dotenv
load_dotenv()  # baca .env di folder kerja

TELEGRAM_BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
GEMINI_API_KEY     = (os.getenv("GEMINI_API_KEY") or "").strip()

# Validasi awal untuk menghindari InvalidToken
if not TELEGRAM_BOT_TOKEN or ":" not in TELEGRAM_BOT_TOKEN:
    raise SystemExit("❌ TELEGRAM_BOT_TOKEN tidak ditemukan/invalid. Cek file .env (format KEY=VALUE).")
if not GEMINI_API_KEY:
    print("⚠️  GEMINI_API_KEY kosong. Fitur AI akan gagal ketika dipanggil.")

# ====== Imports lib ======
import feedparser
import requests
from moviepy.editor import VideoFileClip, TextClip, CompositeVideoClip, ColorClip
from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import google.generativeai as genai

# ====== Gemini config (dengan fallback) ======
genai.configure(api_key=GEMINI_API_KEY)

def pick_gemini_model() -> str:
    # urutan preferensi; pilih yang tersedia di akun kamu
    for m in ["gemini-2.0-flash", "gemini-1.5-flash", "gemini-1.5-pro"]:
        try:
            # panggil model minimal (tanpa konsumsi besar)
            _ = genai.GenerativeModel(m)
            return m
        except Exception:
            continue
    return "gemini-1.5-flash"  # fallback aman

GEMINI_MODEL = pick_gemini_model()

# ====== State sederhana ======
SESSION: Dict[int, Dict[str, str]] = {}   # {chat_id: {"video_path": str}}
JOB_QUEUE: "asyncio.Queue[dict]" = asyncio.Queue()
WORKER_STARTED = False

WELCOME = (
    "Halo! Kirim **video background** (MP4). Lalu pakai /make untuk membuat konten.\n\n"
    "Contoh:\n"
    "`/make mode=news lang=id dur=60 variants=4`\n"
    "`/make mode=facts lang=en dur=45 variants=3`\n\n"
    "• Sumber: Google News RSS (news) / Wikipedia (facts)\n"
    "• Hasil: 1 video + file *caption_variants.txt* berisi 3–5 pilihan judul & hashtag FB.\n"
    "• Bot memakai antrian, memproses 1 per 1 agar stabil di VPS.\n"
)

# ---------- Helpers: sumber ----------
def fetch_google_news(topic: str, lang: str = "id", region: str = "ID", limit: int = 5):
    q = requests.utils.quote(topic)
    url = f"https://news.google.com/rss/search?q={q}&hl={lang}&gl={region}&ceid={region}:{lang}"
    feed = feedparser.parse(url)
    items = []
    for entry in feed.entries[:limit]:
        items.append({
            "title": getattr(entry, "title", ""),
            "url": getattr(entry, "link", ""),
            "published": getattr(entry, "published", "")
        })
    return items

def fetch_wikipedia_facts(lang: str = "id", limit: int = 3):
    base = f"https://{lang}.wikipedia.org/api/rest_v1/page/random/summary"
    results = []
    for _ in range(limit):
        try:
            r = requests.get(base, timeout=10)
            if r.status_code == 200:
                data = r.json()
                title = data.get("title") or ""
                extract = data.get("extract") or ""
                url = (data.get("content_urls", {}).get("desktop", {}) or {}).get("page", "")
                if title and extract and url:
                    results.append({"title": title, "summary": extract, "url": url})
        except Exception:
            pass
    return results

# ---------- Gemini ----------
def gemini_overlay_and_carousel(mode: str, lang: str, sources: List[Dict],
                                target_duration: int, n_variants: int):
    locale = "Bahasa Indonesia" if lang == "id" else "English"
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Susun sumber → teks untuk prompt
    src_text = []
    if mode == "news":
        for i, s in enumerate(sources, 1):
            src_text.append(f"{i}. {s.get('title')}\n   {s.get('url')}\n   published: {s.get('published','')}")
    else:
        for i, s in enumerate(sources, 1):
            snippet = (s.get("summary") or "")
            if len(snippet) > 300:
                snippet = snippet[:300] + "..."
            src_text.append(f"{i}. {s.get('title')}\n   {s.get('url')}\n   note: {snippet}")
    src_blob = "\n".join(src_text) if src_text else "(no sources)"

    prompt = f"""
You are an assistant for short vertical videos. Today: {now_str}.
Write in {locale}.

TASK A: Create a concise on-screen overlay script for a {target_duration}s vertical video:
- 3–6 short lines, ~360 characters total; factual, safe, non-clickbait.
- Synthesize multiple sources; avoid contradictions.
- End with no hashtags.

TASK B: Create {n_variants} distinct Facebook metadata variants:
- Each variant: a title (<=75 chars) + 20 hashtags (lowercase, no spaces; include 5 Indonesia-specific if locale is Indonesian).
- Keep variants diverse (wording & angle).

Also produce a super-short credits line with 1–2 source domains (e.g., "Sumber: bbc.com, kompas.com").

SOURCES:
{src_blob}

Return strictly this JSON:
{{
  "overlay_script": "Line1\\nLine2\\nLine3...",
  "credits": "Sumber: ...",
  "variants": [
    {{"title": "...", "hashtags": ["#...", "#...", "..."]}}
  ]
}}
"""
    try:
        model = genai.GenerativeModel(GEMINI_MODEL)
        resp = model.generate_content(prompt)
        text = (resp.text or "").strip()
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()

        data = json.loads(text)
        if "variants" not in data or not isinstance(data["variants"], list):
            data["variants"] = []
        # clamp variants
        n = max(1, min(5, n_variants))
        data["variants"] = data["variants"][:n]
        # defaults
        data.setdefault("overlay_script", "Info menarik hari ini.")
        data.setdefault("credits", "")
        if not data["variants"]:
            data["variants"] = [
                {"title": "Fakta Menarik Hari Ini", "hashtags": ["#info", "#viral"]}
            ]
        return data
    except Exception as e:
        # fallback sangat minimal kalau Gemini gagal
        return {
            "overlay_script": "Info menarik hari ini.",
            "credits": "",
            "variants": [
                {"title": "Berita Viral Singkat", "hashtags": ["#berita", "#shorts"]},
                {"title": "Fakta Unik Hari Ini", "hashtags": ["#fakta", "#unik"]}
            ][:n_variants]
        }

# ---------- Render ----------
def _tc_height_guess(panel_h: int) -> int:
    return int(panel_h * 0.1)

def render_video_with_overlay(bg_path: str, overlay_lines: List[str],
                              credits: str, out_path: str, target_duration: int = 60):
    """Tambahkan panel semi-transparan + teks overlay + credits."""
    clip = VideoFileClip(bg_path)
    duration = min(float(clip.duration), float(target_duration))
    sub = clip.subclip(0, duration)
    W, H = sub.size

    panel_h = int(H * 0.32)
    panel_y = int(H * 0.6)

    panel = (ColorClip(size=(W, panel_h), color=(0, 0, 0))
             .set_opacity(0.35)
             .set_duration(sub.duration)
             .set_position(("center", panel_y - panel_h // 2)))

    joined = "\n".join(overlay_lines)
    fontsize = max(28, int(H * 0.04))

    # NOTE: TextClip method="caption" menggunakan ImageMagick.
    txt = (TextClip(
            txt=joined,
            fontsize=fontsize,
            color="white",
            font="DejaVu-Sans",
            stroke_color="black",
            stroke_width=max(1, fontsize // 14),
            method="caption",
            size=(int(W * 0.88), None),
            align="center",
        )
        .set_duration(sub.duration)
        .set_position(("center", panel_y - _tc_height_guess(panel_h))))

    credit_font = max(20, int(H * 0.025))
    credits_clip = (TextClip(
            txt=credits,
            fontsize=credit_font,
            color="white",
            font="DejaVu-Sans",
            stroke_color="black",
            stroke_width=max(1, credit_font // 10),
            method="label",
        )
        .set_duration(sub.duration)
        .set_position((int(W * 0.02), int(H * 0.94))))

    final = CompositeVideoClip([sub, panel, txt, credits_clip])
    final.write_videofile(
        out_path,
        codec="libx264",
        audio_codec="aac",
        fps=30,
        threads=0,
        preset="medium",
        bitrate="3500k",
    )

# ---------- Telegram handlers ----------
WELCOME_MSG = (
    "Halo! Kirim **video background** (MP4). Lalu jalankan:\n"
    "`/make mode=news lang=id dur=60 variants=4`\n\n"
    "Mode: `news` (Google News) atau `facts` (Wikipedia).\n"
    "Durasi: 20–90 detik. Variants: 3–5 judul+hashtag.\n"
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(WELCOME + "\n" + WELCOME_MSG, parse_mode="Markdown")

async def save_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    vid = update.message.video or update.message.document
    if not vid:
        await update.message.reply_text("Kirim file video MP4 ya.")
        return
    os.makedirs("data", exist_ok=True)
    f = await context.bot.get_file(vid.file_id)
    local = os.path.join("data", f"{chat_id}_{uuid.uuid4().hex}.mp4")
    await f.download_to_drive(local)

    SESSION.setdefault(chat_id, {})["video_path"] = local
    await update.message.reply_text("✅ Video disimpan. Jalankan `/make mode=news lang=id dur=60 variants=4`")

def parse_args(arglist: List[str]) -> Dict[str, str]:
    args = {"mode": "news", "lang": "id", "dur": "60", "variants": "4"}
    text = " ".join(arglist or [])
    for part in text.split():
        if "=" in part:
            k, v = part.split("=", 1)
            args[k.lower().strip()] = v.strip()
    return args

async def make_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global WORKER_STARTED
    chat_id = update.effective_chat.id
    args = parse_args(context.args)

    mode = args.get("mode", "news")
    lang = args.get("lang", "id")
    dur = max(20, min(90, int(args.get("dur", "60"))))
    variants = int(args.get("variants", "4"))
    variants = max(3, min(5, variants))

    sess = SESSION.get(chat_id, {})
    bg_path = sess.get("video_path")
    if not bg_path or not os.path.exists(bg_path):
        await update.message.reply_text("Belum ada video. Kirim video MP4 dulu.")
        return

    job = {
        "chat_id": chat_id,
        "bg_path": bg_path,
        "mode": mode,
        "lang": lang,
        "dur": dur,
        "variants": variants,
    }
    await JOB_QUEUE.put(job)
    pos = JOB_QUEUE.qsize()
    await update.message.reply_text(
        f"🧾 Job ditambahkan ke antrian.\n"
        f"Mode: {mode}, Lang: {lang}, Dur: {dur}s, Variants: {variants}\n"
        f"Posisi dalam antrian saat ini: {pos}"
    )

    if not WORKER_STARTED:
        WORKER_STARTED = True
        asyncio.create_task(worker(context.application))

async def worker(app: Application):
    while True:
        job = await JOB_QUEUE.get()
        chat_id = job["chat_id"]
        try:
            await app.bot.send_message(chat_id, "⏳ Memproses job: ambil sumber → Gemini → render...")

            # 1) Sumber
            if job["mode"] == "news":
                topics = ["trending", "viral", "breaking", "unik", "teknologi", "hiburan"]
                news = []
                for t in topics:
                    try:
                        part = fetch_google_news(
                            t,
                            lang="id" if job["lang"] == "id" else "en",
                            region="ID" if job["lang"] == "id" else "US",
                            limit=2,
                        )
                        news.extend(part)
                    except Exception:
                        pass
                uniq = []
                seen = set()
                for n in news:
                    u = n.get("url")
                    if u and u not in seen:
                        seen.add(u)
                        uniq.append(n)
                sources = uniq[:5] if uniq else []
            else:
                sources = fetch_wikipedia_facts(
                    lang="id" if job["lang"] == "id" else "en", limit=3
                )

            if not sources:
                await app.bot.send_message(chat_id, "❗Gagal mengambil sumber. Coba lagi nanti.")
                continue

            # 2) Gemini: overlay + carousel meta
            data = gemini_overlay_and_carousel(
                job["mode"], job["lang"], sources, job["dur"], job["variants"]
            )
            overlay_lines = [ln.strip() for ln in (data.get("overlay_script", "").splitlines()) if ln.strip()]
            if len(overlay_lines) < 3:
                overlay_lines += [""] * (3 - len(overlay_lines))
            overlay_lines = overlay_lines[:6]
            credits = data.get("credits", "")
            variants = data.get("variants", [])

            # 3) Render
            outdir = tempfile.mkdtemp(prefix="out_")
            out_video = os.path.join(outdir, f"short_{uuid.uuid4().hex}.mp4")
            render_video_with_overlay(job["bg_path"], overlay_lines, credits, out_video, job["dur"])

            # 4) Buat file carousel judul/hashtag
            variants_txt = os.path.join(outdir, "caption_variants.txt")
            with open(variants_txt, "w", encoding="utf-8") as f:
                for i, v in enumerate(variants, 1):
                    t = (v.get("title") or "").strip()
                    hs = v.get("hashtags", []) or []
                    hs = [h if h.startswith("#") else "#" + h for h in hs]
                    f.write(f"[{i}] {t}\n")
                    f.write(" ".join(hs) + "\n\n")
                if credits:
                    f.write(credits.strip() + "\n")

            # 5) Kirim hasil
            caption_title = (variants[0].get("title") if variants else "Konten Menarik")[:1000]
            await app.bot.send_video(chat_id, video=InputFile(out_video), caption=caption_title)
            await app.bot.send_document(chat_id, document=InputFile(variants_txt), caption="3–5 opsi judul & hashtag FB")

        except Exception as e:
            try:
                await app.bot.send_message(chat_id, f"❌ Job gagal: {e}")
            except:
                pass
        finally:
            JOB_QUEUE.task_done()

# ---------- Main ----------
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("make", make_cmd))
    app.add_handler(MessageHandler(filters.VIDEO | filters.Document.VIDEO, save_video))
    print(f"Bot running with queue + carousel...  (Gemini model: {GEMINI_MODEL})")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
