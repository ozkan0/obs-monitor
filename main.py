import os
import time
import requests
import re
import threading
import json
import logging
from flask import Flask
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import telebot
from telebot import types
from supabase import create_client, Client

# --- CONFIG ---
TOKEN = os.environ.get("TELEGRAM_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")

# ADMINS
ADMIN_USERNAMES = ['midono'] 

# Setup Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(TOKEN)
app = Flask(__name__)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# MSKU Constants
BASE_URL = "https://obs.mu.edu.tr"
INDEX_URL = f"{BASE_URL}/oibs/std/index.aspx?curOp=0"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,xml;q=0.9,image/avif,webp,image/apng,*/*;q=0.8',
    'Referer': f'{BASE_URL}/oibs/std/index.aspx'
}

# Global State for Broadcasts
broadcast_queues = {}

# --- TIMEZONE HELPER (TR) ---
def now_tr():
    return (datetime.utcnow() + timedelta(hours=3)).strftime("%H:%M:%S")

# --- DATABASE HELPERS ---
def db_update_check(chat_id):
    supabase.table("users").update({"last_check": now_tr()}).eq("chat_id", chat_id).execute()

def db_save_user(chat_id, sid, token, last_snap_json="{}", active=True):
    data = {
        "chat_id": chat_id,
        "sid": sid,
        "token": token,
        "last_snap": last_snap_json,
        "active": active,
        "last_check": now_tr()
    }
    supabase.table("users").upsert(data).execute()

# --- PARSING HELPERS ---
def parse_exam_string(text):
    matches = re.findall(r'([a-zA-Z0-9İıŞşÇçĞğÜüÖö\s]+)\s*:\s*([^\s]+)', text)
    return {k.strip(): v.strip() for k, v in matches}

def clean_room_text(text):
    if not text or text == "100": return "" 
    text = " ".join(text.split())
    if len(text) > 5 and text[:len(text)//2].strip() == text[len(text)//2:].strip():
        return text[:len(text)//2].strip()
    return text

def format_grade_message(data_dict):
    if not data_dict: return "📭 Henüz not girişi yok."
    msg = "🎓 *GÜNCEL NOTLARINIZ*\n" + "—" * 12 + "\n\n"
    for course, details in data_dict.items():
        msg += f"📘 *{course}*\n"
        exams = details.get('exams', {})
        for exam_name, score in exams.items():
            icon = "⚪"
            if score.isdigit():
                icon = "🟢" if int(score) >= 50 else "🔴"
            elif score == "GR": icon = "⚠️" 
            msg += f"   └ {icon} {exam_name}: *{score}*\n"
        if details.get('letter') != '--':
            msg += f"   🏆 *Harf Notu:* `{details['letter']}`\n"
        msg += "\n"
    return msg

def detect_changes(old_json_str, new_data):
    changes = []
    if not old_json_str: return []
    try: old_data = json.loads(old_json_str)
    except: return [] 

    for course, info in new_data.items():
        if course not in old_data:
            changes.append(f"🆕 *{course}* dersi eklendi.")
            continue
        old_info = old_data[course]
        if info['letter'] != old_info.get('letter', '--'):
             changes.append(f"🏁 *{course}* harf notu: `{info['letter']}`")
        new_exams = info['exams']
        old_exams = old_info.get('exams', {})
        for exam, score in new_exams.items():
            old_score = old_exams.get(exam, '--')
            if score != old_score:
                changes.append(f"🔔 *{course}* - {exam}: *{score}* (Eski: {old_score})")
    return changes

# --- CORE SCRAPER ---
def find_menu_link(html, keyword):
    soup = BeautifulSoup(html, 'html.parser')
    keyword = keyword.lower()
    for link in soup.find_all('a'):
        text = link.get_text().lower()
        if keyword in text:
            onclick = link.get('onclick', '')
            match = re.search(r"'(.*?)'", onclick)
            if match:
                path = match.group(1)
                if "report" not in path.lower() and "print" not in path.lower():
                    return f"{BASE_URL}{path}" if path.startswith('/') else f"{BASE_URL}/oibs/std/{path}"
    
    if keyword == "not listesi":
        raw_match = re.search(r"menu_close\(this,'(/oibs/start\.aspx\?gkm=.*?)'\)", html)
        if raw_match: return f"{BASE_URL}{raw_match.group(1)}"
    return None

def fetch_grades(user_data):
    cookies = {'ASP.NET_SessionId': user_data['sid'], '__RequestVerificationToken': user_data['token']}
    try:
        s = requests.Session()
        res = s.get(INDEX_URL, cookies=cookies, headers=HEADERS, timeout=20)
        
        if "login.aspx" in res.url: return "EXPIRED", "❌ Oturum süresi dolmuş."

        real_url = find_menu_link(res.text, "not listesi")
        if not real_url: return "EXPIRED", "⚠️ Oturum verisi bozulmuş."

        g_res = s.get(real_url, cookies=cookies, headers=HEADERS, timeout=20)
        if g_res.text.startswith("%PDF"): return "PDF_ERR", "⚠️ Sistem PDF yönlendirmesi yaptı."

        soup = BeautifulSoup(g_res.text, 'html.parser')
        rows = soup.find_all('tr')
        course_data = {}
        for r in rows:
            c = [td.get_text(separator=' ', strip=True) for td in r.find_all(['td', 'th'])]
            if len(c) < 8 or "Ders Kodu" in c[2] or "Sınav Notları" in c[4]: continue
            parsed_exams = parse_exam_string(c[4])
            course_data[c[2]] = {"exams": parsed_exams, "letter": c[6] if c[6] else '--'}
        return "OK", course_data
    except Exception as e: return "CONN_ERR", str(e)

def fetch_schedule(user_data):
    cookies = {'ASP.NET_SessionId': user_data['sid'], '__RequestVerificationToken': user_data['token']}
    try:
        s = requests.Session()
        res = s.get(INDEX_URL, cookies=cookies, headers=HEADERS, timeout=20)
        if "login.aspx" in res.url: return "EXPIRED"
        real_url = find_menu_link(res.text, "sınav takvimi")
        if not real_url: return "EXPIRED"

        s_res = s.get(real_url, cookies=cookies, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(s_res.text, 'html.parser')
        rows = soup.find_all('tr')
        schedule = []
        for r in rows:
            cols = r.find_all('td')
            if len(cols) < 6: continue
            c_vals = [td.get_text(separator=' ', strip=True) for td in cols]
            date_str = c_vals[4]
            if not re.match(r'\d{2}\.\d{2}\.\d{4}', date_str): continue

            try: dt_obj = datetime.strptime(date_str, "%d.%m.%Y %H:%M")
            except: dt_obj = datetime.max 
            
            schedule.append({
                "dt": dt_obj, "date_display": date_str,
                "course": c_vals[2], "type": c_vals[3],
                "room": clean_room_text(c_vals[6] if len(c_vals) > 6 else "")
            })
            
        schedule.sort(key=lambda x: x['dt'])
        current_time = datetime.now() - timedelta(days=2) 
        upcoming = [x for x in schedule if x['dt'] > current_time or x['dt'] == datetime.max]
        
        if not upcoming: return "📅 *SINAV TAKVİMİ*\n\nYaklaşan sınav bulunamadı."
        
        msg = "📅 *YAKLAŞAN SINAVLAR*\n" + "—"*15 + "\n\n"
        for item in upcoming:
            msg += f"🔹 *{item['course']}*\n   📝 {item['type']}\n   🗓 `{item['date_display']}`\n"
            if item['room'] and len(item['room']) > 2: msg += f"   📍 {item['room']}\n"
            msg += "\n"
        return msg
    except Exception as e: return f"⚠️ Hata: {str(e)}"

# --- BROADCAST COMMANDS (ADMIN) ---
def is_admin(user):
    if not user.username: return False
    return user.username in ADMIN_USERNAMES

@bot.message_handler(commands=['broadcast'])
def start_broadcast(m):
    if not is_admin(m.from_user): return
    broadcast_queues[m.chat.id] = []
    msg = "📢 *YAYIN MODU*\n\nMesaj/Foto gönderin.\n✅ Göndermek için: `/gonder`\n❌ İptal için: `/iptal`"
    bot.send_message(m.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(commands=['iptal'])
def cancel_broadcast(m):
    if m.chat.id in broadcast_queues:
        del broadcast_queues[m.chat.id]
        bot.reply_to(m, "🗑️ Yayın iptal edildi.")

@bot.message_handler(commands=['gonder'])
def execute_broadcast(m):
    if m.chat.id not in broadcast_queues or not broadcast_queues[m.chat.id]:
        bot.reply_to(m, "⚠️ Sepet boş!")
        return
    
    queue = broadcast_queues[m.chat.id]
    users = supabase.table("users").select("chat_id").execute().data
    total_users = len(users)
    
    bot.send_message(m.chat.id, f"🚀 Yayın {total_users} kişiye gönderiliyor...")
    success, blocked = 0, 0
    
    for user in users:
        target_id = user['chat_id']
        try:
            for item in queue:
                if item.content_type == 'text':
                    bot.send_message(target_id, item.text)
                elif item.content_type == 'photo':
                    photo_id = item.photo[-1].file_id
                    caption = item.caption if item.caption else None
                    bot.send_photo(target_id, photo_id, caption=caption)
            success += 1
            time.sleep(0.2) 
        except telebot.apihelper.ApiTelegramException as e:
            if e.result_json['error_code'] == 403:
                blocked += 1
                supabase.table("users").update({"active": False}).eq("chat_id", target_id).execute()
    
    del broadcast_queues[m.chat.id]
    bot.send_message(m.chat.id, f"✅ *Bitti.*\n📨 Başarılı: {success}\n🚫 Engelli: {blocked}", parse_mode="Markdown")

# --- STANDARD USER COMMANDS ---
@bot.message_handler(commands=['start', 'help'])
def help_cmd(m):
    guide = (
        "🎓 *MSKU Öğrenci Botu*\n\n"
        "Cookie yöntemiyle çalışır. Verileriniz şifrelidir.\n\n"
        "🔹 `/setup [SID] [Token]` - Kurulum\n"
        "🔹 `/notlar` - Notlar\n"
        "🔹 `/takvim` - Sınavlar\n"
        "🔹 `/stats` - Durum"
    )
    bot.send_message(m.chat.id, guide, parse_mode="Markdown")

@bot.message_handler(commands=['setup'])
def setup(m):
    try:
        parts = m.text.split()
        if len(parts) < 3: return bot.reply_to(m, "❌ Hatalı. `/help` yazın.")
        sid, token = parts[1], parts[2]
        bot.send_chat_action(m.chat.id, 'typing')
        status, data = fetch_grades({'sid': sid, 'token': token})
        
        if status == "OK":
            db_save_user(m.chat.id, sid, token, json.dumps(data))
            bot.reply_to(m, "✅ *Kurulum Tamamlandı!*\n\n" + format_grade_message(data), parse_mode="Markdown")
        else:
            bot.reply_to(m, f"⚠️ Giriş Başarısız: {data}")
    except Exception as e: bot.reply_to(m, f"Hata: {e}")

@bot.message_handler(commands=['notlar'])
def notlar(m):
    user = supabase.table("users").select("*").eq("chat_id", m.chat.id).execute().data
    if not user: return bot.reply_to(m, "⚠️ Önce `/setup` yapın.")
    bot.send_chat_action(m.chat.id, 'typing')
    status, data = fetch_grades(user[0])
    
    if status == "OK":
        db_save_user(m.chat.id, user[0]['sid'], user[0]['token'], json.dumps(data))
        bot.send_message(m.chat.id, format_grade_message(data), parse_mode="Markdown")
    elif status == "EXPIRED":
        bot.reply_to(m, "❌ Oturum kapanmış. Tekrar `/setup` yapın.")
    else:
        bot.reply_to(m, f"⚠️ Veri alınamadı: {data}")

@bot.message_handler(commands=['takvim'])
def takvim(m):
    user = supabase.table("users").select("*").eq("chat_id", m.chat.id).execute().data
    if not user: return bot.reply_to(m, "⚠️ Önce `/setup` yapın.")
    bot.send_chat_action(m.chat.id, 'typing')
    msg = fetch_schedule(user[0])
    if msg == "EXPIRED": bot.reply_to(m, "❌ Oturum kapanmış.")
    else: bot.send_message(m.chat.id, msg, parse_mode="Markdown")

@bot.message_handler(commands=['stats'])
def stats(m):
    user = supabase.table("users").select("*").eq("chat_id", m.chat.id).execute().data
    total = supabase.table("users").select("chat_id", count="exact").execute().count
    if not user: return bot.reply_to(m, "Kayıtlı değilsiniz.")
    bot.send_message(m.chat.id, f"📊 *DURUM*\n🕒 Son: `{user[0]['last_check']}`\n👥 Toplam: `{total}`", parse_mode="Markdown")

# --- BROADCAST CONTENT CATCHER  ---
@bot.message_handler(content_types=['text', 'photo'])
def handle_broadcast_content(m):
    if m.chat.id not in broadcast_queues: return 
    if m.content_type == 'text' and m.text.startswith('/'): return 

    broadcast_queues[m.chat.id].append(m)
    item_type = "📝 Metin" if m.content_type == 'text' else "📷 Fotoğraf"
    count = len(broadcast_queues[m.chat.id])
    bot.reply_to(m, f"➕ {item_type} eklendi. (Toplam: {count})\nBitince `/gonder`.")

# --- MONITOR ---
def monitor():
    logger.info("Monitor started.")
    while True:
        time.sleep(240) 
        try:
            active_users = supabase.table("users").select("*").eq("active", True).execute().data
            if not active_users: continue
            
            for user in active_users:
                status, new_data = fetch_grades(user)
                cid = user['chat_id']
                
                if status == "EXPIRED":
                    bot.send_message(cid, "🛑 *Oturum düştü.* Tekrar `/setup` yapın.")
                    supabase.table("users").update({"active": False}).eq("chat_id", cid).execute()
                elif status == "OK":
                    changes = detect_changes(user.get('last_snap'), new_data)
                    if changes:
                        bot.send_message(cid, "📢 *NOT GİRİLDİ!*\n\n" + "\n".join(changes), parse_mode="Markdown")
                        bot.send_message(cid, format_grade_message(new_data), parse_mode="Markdown")
                        db_save_user(cid, user['sid'], user['token'], json.dumps(new_data))
                    else:
                        db_update_check(cid)
                else:
                    db_update_check(cid)
                time.sleep(2)
        except Exception as e: logger.error(f"Monitor Loop Error: {e}")

@app.route('/')
def health(): return f"Online (TR Time: {now_tr()})"

if __name__ == "__main__":
    try: bot.remove_webhook()
    except: pass
    threading.Thread(target=monitor, daemon=True).start()
    threading.Thread(target=lambda: bot.infinity_polling(skip_pending=True), daemon=True).start()
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 5000)))