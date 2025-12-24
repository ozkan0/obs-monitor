import os
import time
import requests
import re
import threading
from flask import Flask
from bs4 import BeautifulSoup
from datetime import datetime
import telebot

# --- CONFIGURATION ---
TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_BOT_TOKEN_HERE")
bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)

# MSKU Constants
BASE_URL = "https://obs.mu.edu.tr"
INDEX_URL = "https://obs.mu.edu.tr/oibs/std/index.aspx?curOp=0"
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Referer': 'https://obs.mu.edu.tr/oibs/std/index.aspx'
}

users_db = {}

def find_grade_link(html):
    """Parses sidebar to find the 'gkm' security link for grades."""
    soup = BeautifulSoup(html, 'html.parser')
    links = soup.find_all('a')
    for link in links:
        if "Not Listesi" in link.get_text():
            onclick = link.get('onclick', '')
            match = re.search(r"'(.*?)'", onclick)
            if match:
                path = match.group(1)
                return f"{BASE_URL}{path}" if path.startswith('/') else f"{BASE_URL}/oibs/std/{path}"
    return None

def fetch_grades(chat_id):
    """Hits the server for a specific user and returns (snapshot, display_text)."""
    user = users_db.get(chat_id)
    if not user: return "ERROR", None

    cookies = {
        'ASP.NET_SessionId': user['sid'],
        '__RequestVerificationToken': user['token']
    }

    try:
        s = requests.Session()
        res = s.get(INDEX_URL, cookies=cookies, headers=HEADERS, timeout=15)
        if "login.aspx" in res.url: return "EXPIRED", None

        real_url = find_grade_link(res.text)
        if not real_url: return "URL_ERR", None

        g_res = s.get(real_url, cookies=cookies, headers=HEADERS, timeout=15)
        soup = BeautifulSoup(g_res.text, 'html.parser')
        rows = soup.find_all('tr')

        snap, display = "", "🎓 *GÜNCEL NOTLARINIZ*\n" + "-"*15 + "\n"
        found = False

        for r in rows:
            c = [td.get_text(strip=True) for td in r.find_all(['td', 'th'])]
            if len(c) < 8 or "Ders Adı" in c[2]: continue
            
            course, exams, grade = c[2], c[4], (c[6] if c[6] else "--")
            display += f"📘 *{course}*\n📝 {exams} | Not: *{grade}*\n\n"
            snap += f"{course}:{exams}|"
            found = True

        return (snap, display) if found else ("EMPTY", "Henüz girilmiş notunuz yok.")
    except Exception as e:
        print(f"Error for {chat_id}: {e}")
        return "CONN_ERR", None

# --- BOT COMMANDS ---

@bot.message_handler(commands=['start'])
def welcome(m):
    msg = (
        "👋 *MSKU Not Takip Botuna Hoşgeldin!*\n\n"
        "Kurulum için:\n"
        "1. OBS'ye tarayıcıdan gir.\n"
        "2. F12 (veya Web Inspector) ile şu iki veriyi al:\n"
        "   `ASP.NET_SessionId` ve `__RequestVerificationToken` (L29 ile başlayan)\n\n"
        "3. Bot'a şu şekilde gönder:\n"
        "`/setup [SessionID] [Token]`"
    )
    bot.reply_to(m, msg, parse_mode="Markdown")

@bot.message_handler(commands=['setup'])
def setup_user(m):
    try:
        parts = m.text.split()
        if len(parts) < 3:
            return bot.reply_to(m, "❌ Hata! Kullanım: `/setup [SessionID] [Token]`")
        
        users_db[m.chat.id] = {
            'sid': parts[1],
            'token': parts[2],
            'last_snap': "",
            'active': True
        }
        
        bot.reply_to(m, "⏳ Tokenlar kaydedildi. İlk kontrol yapılıyor...")
        snap, text = fetch_grades(m.chat.id)
        
        if snap == "EXPIRED":
            bot.send_message(m.chat.id, "❌ Gönderdiğin tokenlar geçersiz. Lütfen tekrar al.")
            users_db[m.chat.id]['active'] = False
        else:
            users_db[m.chat.id]['last_snap'] = snap
            bot.send_message(m.chat.id, text, parse_mode="Markdown")
            
    except Exception as e:
        bot.reply_to(m, f"❌ Beklenmedik hata: {e}")

@bot.message_handler(commands=['notlar'])
def show_now(m):
    snap, text = fetch_grades(m.chat.id)
    if snap == "EXPIRED":
        bot.reply_to(m, "⚠️ Oturumun kapandı. Lütfen yeni tokenları `/setup` ile gönder.")
    elif text:
        bot.reply_to(m, text, parse_mode="Markdown")
    else:
        bot.reply_to(m, "⚠️ Henüz bir kurulum yapmadın. `/setup` komutunu kullan.")

# --- BACKGROUND THREADS ---

def monitor_loop():
    """Loops through all active users every 7.5 minutes."""
    while True:
        time.sleep(450)
        for chat_id in list(users_db.keys()):
            user = users_db[chat_id]
            if not user['active']: continue

            snap, text = fetch_grades(chat_id)
            
            if snap == "EXPIRED":
                bot.send_message(chat_id, "🛑 *OTURUM DÜŞTÜ!*\n10 dakika işlem yapmadığın için veya token süresi dolduğu için takip durdu. Yeniden `/setup` yapmalısın.")
                user['active'] = False
            elif snap and snap != user['last_snap'] and len(snap) > 10:
                bot.send_message(chat_id, "🔔 *YENİ NOT GİRİLDİ!*\n\n" + text, parse_mode="Markdown")
                user['last_snap'] = snap
            else:
                print(f"Heartbeat for {chat_id}: OK")

# --- WEB SERVER ---
@app.route('/')
def home():
    return f"MSKU Bot Active. Users: {len(users_db)}"

if __name__ == "__main__":
    threading.Thread(target=monitor_loop, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(), daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)