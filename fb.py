import os
import requests
import google.generativeai as genai
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from moviepy.editor import *

# --- KONFIGURASI ---
TELEGRAM_BOT_TOKEN = "8326980628:AAFaamFAEozoEHEyX57lluFzHOsyMspDaJo"
GOOGLE_AI_API_KEY = "AIzaSyC-gwRbQc4ugBP532oVavmjtsBeOWgieCc"
# -----------------

# Konfigurasi Google AI
genai.configure(api_key=GOOGLE_AI_API_KEY)

# Fungsi-fungsi dari skrip sebelumnya (sedikit dimodifikasi)
def get_unique_fact():
    try:
        response = requests.get("https://uselessfacts.jsph.pl/random.json?language=en")
        response.raise_for_status()
        return response.json()['text']
    except requests.exceptions.RequestException:
        return "Fakta unik tidak ditemukan saat ini."

def generate_title_and_hashtags(fact):
    try:
        model = genai.GenerativeModel('gemini-pro')
        prompt = f"""
        Buatkan saya judul yang menarik dan 3 hashtag yang relevan untuk video Facebook Pro (FB Pro).
        Fakta unik di video: "{fact}"
        Format:
        Judul: [Judul di sini]
        Hashtag: [Hashtag di sini]
        """
        response = model.generate_content(prompt)
        return response.text
    except Exception:
        return "Judul: Gagal Membuat Judul\nHashtag: #error"

def create_video(video_path, fact_text):
    try:
        video_clip = VideoFileClip(video_path)
        duration = min(video_clip.duration, 10.0)
        video_clip = video_clip.subclip(0, duration)
        
        w, h = video_clip.size
        
        text_clip = TextClip(fact_text, fontsize=45, color='white', font='Arial-Bold',
                             method='caption', size=(w*0.9, None), bg_color='black')
        
        text_bg = ColorClip(size=(int(w*0.95), int(text_clip.h + 20)), color=(0,0,0)).set_opacity(0.6)
        final_text = CompositeVideoClip([text_bg, text_clip.set_position("center")]).set_position("center").set_duration(duration)
        
        final_video = CompositeVideoClip([video_clip, final_text])
        
        output_filename = "hasil_video.mp4"
        final_video.write_videofile(output_filename, codec='libx24', audio_codec='aac')
        
        video_clip.close() # Penting untuk menutup file
        return output_filename
    except Exception as e:
        print(f"Error creating video: {e}")
        return None

# Fungsi untuk handler bot
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk perintah /start"""
    await update.message.reply_text(
        "Halo! Kirimkan saya sebuah video (kurang dari 20MB) "
        "dan saya akan mengubahnya menjadi video fakta unik untuk Anda."
    )

async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk pesan video"""
    message = update.message
    await message.reply_text("Video diterima. Mohon tunggu, sedang memproses... ⏳")

    try:
        video_file = await message.video.get_file()
        input_path = "input_video.mp4"
        await video_file.download_to_drive(input_path)
        
        # 1. Dapatkan fakta
        fact = get_unique_fact()
        await message.reply_text(f"Fakta ditemukan: \"{fact}\"")
        
        # 2. Buat video
        await message.reply_text("Membuat video baru...")
        output_path = create_video(input_path, fact)
        
        if output_path and os.path.exists(output_path):
            # 3. Kirim video hasil
            await message.reply_text("Video selesai! Mengirimkan hasil...")
            with open(output_path, 'rb') as video:
                await context.bot.send_video(chat_id=message.chat_id, video=video)
            
            # 4. Buat dan kirim judul & hashtag
            ai_content = generate_title_and_hashtags(fact)
            await message.reply_text(f"Berikut rekomendasi judul dan hashtag untuk Anda:\n\n{ai_content}")
            
            # 5. Hapus file sementara
            os.remove(input_path)
            os.remove(output_path)
        else:
            await message.reply_text("Maaf, terjadi kesalahan saat membuat video. 😔")

    except Exception as e:
        print(f"Error handle_video: {e}")
        await message.reply_text("Oops, terjadi error. Silakan coba lagi.")

def main():
    """Fungsi utama untuk menjalankan bot."""
    print("Bot dimulai...")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Daftarkan handler
    application.add_handler(CommandHandler("start", start))
    application.add_handler(MessageHandler(filters.VIDEO, handle_video))

    # Mulai bot
    application.run_polling()

if __name__ == "__main__":
    main()
