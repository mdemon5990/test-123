import os
import sys
import time
import json
import logging
import asyncio
import aiohttp
import threading
import shutil
import nest_asyncio
import urllib.parse
import urllib.request
import urllib.error
from copy import deepcopy
from datetime import datetime, timedelta
import pytz

import telegram
from telegram import (
    Update, 
    ReplyKeyboardMarkup, 
    InlineKeyboardButton, 
    InlineKeyboardMarkup, 
    ForceReply,
    ReplyKeyboardRemove
)
from telegram.ext import (
    Application, 
    CommandHandler, 
    MessageHandler, 
    CallbackQueryHandler, 
    filters, 
    ContextTypes
)
from telegram.constants import ParseMode
from telegram.error import InvalidToken, TelegramError, RetryAfter
from telegram.request import HTTPXRequest

nest_asyncio.apply()

# ==========================================
# ⚙️ LOGGING SETUP
# ==========================================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger("VIP_SMS_Master")

# ==========================================
# ⚙️ CONFIGURATION (আপনার ইনফো)
# ==========================================
MASTER_BOT_TOKEN = "8820442516:AAGF73UUIMnRnP8y8kbxRsPTLdevncc3pSA"
MAIN_ADMIN = "7034779471"
GLOBAL_LOG_CHANNEL = "@ffxemon"
TIMEZONE = pytz.timezone("Asia/Dhaka")

FREE_SMS_DEFAULT_API = "https://demosoftpp.com/SMS/api.php?key=AJGJJVG26SDD243SFFF&number=[NUMBER]&msg=[MSG]"
FIREBASE_URL = "https://airavattest-e7835-default-rtdb.firebaseio.com/vip_database.json"

active_clones = {}  

# ==========================================
# 💾 FIREBASE + LOCAL DATABASE CLASS (100% Anti Data Loss)
# ==========================================
class JSONDatabase:
    def __init__(self, filename="master_database.json"):
        self.filename = filename
        self.backup_filename = filename + ".bak"
        self.lock = threading.RLock()
        self.data = {"clones": {}, "global_config": {}}
        self.load()

    def load(self):
        with self.lock:
            loaded_from_firebase = False
            # 1. Try Loading from Firebase (Master Truth)
            try:
                req = urllib.request.Request(FIREBASE_URL)
                with urllib.request.urlopen(req, timeout=15) as response:
                    fb_data = json.loads(response.read().decode('utf-8'))
                    if fb_data:
                        self.data.update(fb_data)
                        loaded_from_firebase = True
                        logger.info("✅ Database successfully synced and restored from Firebase!")
            except Exception as e:
                logger.warning(f"⚠️ Firebase load failed, falling back to local. Error: {e}")

            # 2. Fallback to Local/Backup if Firebase fails
            if not loaded_from_firebase:
                loaded = False
                if os.path.exists(self.filename):
                    try:
                        with open(self.filename, 'r', encoding='utf-8') as f:
                            loaded_data = json.load(f)
                            self.data.update(loaded_data)
                            loaded = True
                    except Exception: pass
                
                if not loaded and os.path.exists(self.backup_filename):
                    try:
                        with open(self.backup_filename, 'r', encoding='utf-8') as f:
                            loaded_data = json.load(f)
                            self.data.update(loaded_data)
                    except Exception: pass

            if not os.path.exists(self.filename):
                self.save_local()

    def save_local(self):
        try:
            if os.path.exists(self.filename):
                shutil.copy2(self.filename, self.backup_filename)
            temp_file = self.filename + ".tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, self.filename)
        except Exception as e:
            logger.error(f"Local DB Save Error: {e}")

    def _push_to_firebase(self):
        try:
            req = urllib.request.Request(FIREBASE_URL, data=json.dumps(self.data).encode('utf-8'), method='PUT')
            req.add_header('Content-Type', 'application/json')
            with urllib.request.urlopen(req, timeout=15) as response:
                pass
        except Exception as e:
            logger.error(f"Firebase Sync Error: {e}")

    def save_internal(self):
        self.save_local()
        # Background Firebase Sync (Prevents Bot Lag)
        threading.Thread(target=self._push_to_firebase, daemon=True).start()

    def get_val(self, token, key, default=None):
        with self.lock:
            namespace = f"bot_{token[:10].replace(':', '_')}"
            if namespace not in self.data: return default
            val = self.data[namespace].get(key, default)
            return deepcopy(val) if isinstance(val, (dict, list)) else val

    def set_val(self, token, key, value):
        with self.lock:
            namespace = f"bot_{token[:10].replace(':', '_')}"
            if namespace not in self.data: self.data[namespace] = {}
            self.data[namespace][key] = deepcopy(value) if isinstance(value, (dict, list)) else value
            self.save_internal()

    def delete_val(self, token, key):
        with self.lock:
            namespace = f"bot_{token[:10].replace(':', '_')}"
            if namespace in self.data and key in self.data[namespace]:
                del self.data[namespace][key]
                self.save_internal()

db = JSONDatabase()

# ==========================================
# 🛠 HELPER FUNCTIONS
# ==========================================
def escape_html(text):
    if not text: return "N/A"
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def resolve_user_id(token, input_text):
    input_text = str(input_text).strip()
    if input_text.isdigit(): return input_text
    if input_text.startswith('@'): username = input_text[1:].lower()
    else: username = input_text.lower()
    return db.get_val(token, f"user_map_{username}")

async def is_bot_admin(token, user_id):
    uid = str(user_id)
    if uid == MAIN_ADMIN: return True
    owner_id = str(db.get_val(token, "bot_owner_id", MAIN_ADMIN))
    return uid == owner_id

async def check_joined_channel(context: ContextTypes.DEFAULT_TYPE, user_id, channel_username):
    if not channel_username or channel_username.lower() == "none": return True
    try:
        member = await context.bot.get_chat_member(chat_id=channel_username, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception: return False

def get_force_channels(token):
    f_chans = db.get_val(token, "force_channels", [])
    old_chan = db.get_val(token, "force_channel", "none")
    if old_chan and old_chan.lower() != "none" and not f_chans:
        return [{"id": old_chan, "link": f"https://t.me/{old_chan.replace('@', '')}"}]
    return f_chans

async def get_unjoined_channels(context: ContextTypes.DEFAULT_TYPE, token, user_id):
    channels = get_force_channels(token)
    not_joined = []
    for ch in channels:
        cid = ch['id']
        try:
            member = await context.bot.get_chat_member(chat_id=cid, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                not_joined.append(ch)
        except Exception:
            not_joined.append(ch)
    return not_joined

def get_bd_time_now(): return datetime.now(TIMEZONE)

def format_timestamp(ts):
    if not ts or ts == "life": return "LifeTime ♾️"
    try:
        dt = datetime.fromtimestamp(ts / 1000, TIMEZONE)
        return dt.strftime("%d/%m/%Y %I:%M %p")
    except: return "Unknown"

# ==========================================
# 🎛 KEYBOARDS
# ==========================================
async def user_keyboard(token, user_id):
    keyboard = [
        ["🚀 Send SMS", "👤 My Profile"],
        ["👥 Referral", "💰 Buy Credits"],
        ["🎁 Redeem Code", "☎️ Support"]
    ]
    if await is_bot_admin(token, user_id): keyboard.append(["👑 Admin Panel"])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_keyboard(is_master=False):
    keyboard = [
        [InlineKeyboardButton("📊 Bot Statistics", callback_data="adm_show_stats"), InlineKeyboardButton("🏆 Top Users", callback_data="adm_show_top")],
        [InlineKeyboardButton("🚫 Block", callback_data="adm_block"), InlineKeyboardButton("✅ Unblock", callback_data="adm_unblock"), InlineKeyboardButton("⚠️ Warn", callback_data="adm_warn")],
        [InlineKeyboardButton("🎁 Give Credits", callback_data="adm_give_menu"), InlineKeyboardButton("✂️ Take Credits", callback_data="adm_take_menu")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="adm_bc_menu"), InlineKeyboardButton("🔍 User Info", callback_data="adm_user_info")],
        [InlineKeyboardButton("🎟️ Promo Creator", callback_data="adm_promo_menu"), InlineKeyboardButton("🗑️ Delete Promo", callback_data="adm_delpromo_menu")],
        [InlineKeyboardButton("💵 Set Prices", callback_data="adm_price_menu"), InlineKeyboardButton("🔗 Set Custom API", callback_data="adm_api_menu")],
        [InlineKeyboardButton("📢 Force Channel", callback_data="adm_fc_menu"), InlineKeyboardButton("🛑 Maintenance", callback_data="adm_maint_menu")],
        [InlineKeyboardButton("🤖 My Bot Info", callback_data="adm_bot_info"), InlineKeyboardButton("🛠️ Bot Settings", callback_data="adm_view_settings")]
    ]
    if is_master:
        keyboard.append([
            InlineKeyboardButton("🛡️ Setup New Bot", callback_data="master_setup_bot"),
            InlineKeyboardButton("🌐 Cloned Bots List", callback_data="master_list_bots")
        ])
        keyboard.append([InlineKeyboardButton("🎁 Free SMS Setup", callback_data="master_free_sms_menu")])
        keyboard.append([InlineKeyboardButton("⚡ Super Master Panel", callback_data="ultimate_master_power")])
    return InlineKeyboardMarkup(keyboard)

# ==========================================
# 📡 ADVANCED LOGGING SYSTEM
# ==========================================
async def send_new_user_log(context, token, user, user_id):
    try:
        username_str = escape_html(f"@{user.username}" if user.username else "N/A")
        name_str = escape_html(user.first_name + (" " + user.last_name if user.last_name else ""))
        bot_name = escape_html(context.bot.username)
        bot_type = "MASTER BOT 👑" if token == MASTER_BOT_TOKEN else "CLONE BOT 🤖"
        total_users = len(db.get_val(token, "all_users", []))
        
        log_text = (
            f"🆕 <b>NEW USER JOINED ({bot_type})</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 <b>Name:</b> <a href='tg://user?id={user_id}'>{name_str}</a>\n"
            f"🔗 <b>Username:</b> {username_str}\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"💬 <b>Chat ID:</b> <code>{user_id}</code>\n"
            f"📊 <b>Total Bot Users:</b> <code>{total_users}</code>\n"
            f"🤖 <b>Bot Source:</b> @{bot_name}"
        )
        await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_text, parse_mode=ParseMode.HTML)
    except Exception: pass

# ==========================================
# 🚀 COMMAND HANDLERS
# ==========================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.bot.token
    user = update.effective_user
    user_id = str(user.id)
    args = context.args

    db.set_val(token, f"step_{user_id}", None)
    db.set_val(token, f"adm_step_{user_id}", None)
    db.set_val(token, f"processing_sms_{user_id}", False)

    bot_expiry = db.get_val(token, "bot_expiry_time", "life")
    if bot_expiry != "life" and user_id != MAIN_ADMIN:
        if int(time.time() * 1000) > bot_expiry:
            await update.message.reply_text("⚠️ *Bot Expired*\n\nএই বটের লাইসেন্স মেয়াদ শেষ হয়ে গেছে। দয়া করে মেইন এডমিনের সাথে যোগাযোগ করুন।", parse_mode=ParseMode.MARKDOWN)
            return

    if db.get_val(token, "is_suspended", False) and user_id != MAIN_ADMIN:
        await update.message.reply_text("🚫 *BOT SUSPENDED!*\n\nএই বটটির অ্যাক্সেস মেইন এডমিন কর্তৃক বাতিল করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)
        return

    if db.get_val(token, f"blocked_{user_id}", False):
        await update.message.reply_text("⚠️ *দুঃখিত! আপনাকে এই বট থেকে ব্লক করা হয়েছে।*", parse_mode=ParseMode.MARKDOWN)
        return

    if args and len(args) > 0: db.set_val(token, f"temp_ref_{user_id}", args[0])

    user_list = db.get_val(token, "all_users", [])
    if user_id not in user_list:
        user_list.append(user_id)
        db.set_val(token, "all_users", user_list)
    
    username = user.username.lower() if user.username else ""
    if username: db.set_val(token, f"user_map_{username}", user_id)

    leaderboard = db.get_val(token, "leaderboard", [])
    full_name = escape_html(user.first_name + (" " + user.last_name if user.last_name else ""))
    found = False
    for item in leaderboard:
        if item.get("id") == user_id:
            item["name"] = full_name
            found = True
            break
    if not found: leaderboard.append({"id": user_id, "name": full_name, "sent": db.get_val(token, f"sms_sent_{user_id}", 0)})
    db.set_val(token, "leaderboard", leaderboard)

    if not await is_bot_admin(token, user_id):
        last_check = db.get_val(token, f"last_join_check_{user_id}", 0)
        current_time = time.time()
        
        if current_time - last_check > 86400: 
            unjoined = await get_unjoined_channels(context, token, user.id)
            if unjoined:
                kb = []
                for i, ch in enumerate(unjoined): kb.append([InlineKeyboardButton(f"📢 Join Channel {i+1}", url=ch['link'])])
                kb.append([InlineKeyboardButton("✅ Check Joined", callback_data="check_joined_member")])
                
                await update.message.reply_text(
                    "📢 *চ্যানেলে জয়েন করা বাধ্যতামূলক!*\n\nবটটি ব্যবহার করতে হলে আমাদের অফিশিয়াল চ্যানেলে জয়েন করুন। জয়েন না করলে বটের কোনো ফিচার কাজ করবে না।",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode=ParseMode.MARKDOWN
                )
                return
            else: db.set_val(token, f"last_join_check_{user_id}", current_time)

    if not db.get_val(token, f"registered_{user_id}", False):
        db.set_val(token, f"registered_{user_id}", True)
        db.set_val(token, f"credits_{user_id}", 1) 
        db.set_val(token, f"role_{user_id}", "Free User 👤")
        db.set_val(token, f"refers_{user_id}", 0)
        db.set_val(token, f"sms_sent_{user_id}", 0)
        db.set_val(token, f"expiry_time_{user_id}", int(time.time() * 1000) + (7 * 24 * 60 * 60 * 1000)) 
        db.set_val(token, f"join_date_{user_id}", get_bd_time_now().strftime("%Y-%m-%d %H:%M:%S"))

        await send_new_user_log(context, token, user, user_id)

        ref_id = db.get_val(token, f"temp_ref_{user_id}")
        if ref_id and ref_id != user_id:
            if db.get_val(token, f"registered_{ref_id}", False):
                r_credits = db.get_val(token, f"credits_{ref_id}", 0)
                r_refers = db.get_val(token, f"refers_{ref_id}", 0)
                db.set_val(token, f"credits_{ref_id}", r_credits + 1)
                db.set_val(token, f"refers_{ref_id}", r_refers + 1)
                
                ref_expiry = db.get_val(token, f"expiry_time_{ref_id}", 0)
                new_expiry = max(ref_expiry, int(time.time() * 1000)) + (3 * 24 * 60 * 60 * 1000) 
                db.set_val(token, f"expiry_time_{ref_id}", new_expiry)
                
                try:
                    await context.bot.send_message(chat_id=ref_id, text="👥 *আপনার ইনভাইট লিংকের মাধ্যমে নতুন একজন জয়েন করেছে!*\n🎁 আপনি পেয়েছেন *১ ক্রেডিট বোনাস* এবং ৩ দিন মেয়াদ বৃদ্ধি পেয়েছে।", parse_mode=ParseMode.MARKDOWN)
                except Exception: pass

        welcome_msg = "🎉 *স্বাগতম!*\nবটে যুক্ত হওয়ার জন্য আপনাকে উপহারস্বরূপ *১টি SMS ক্রেডিট ফ্রি* দেওয়া হলো (মেয়াদ ৭ দিন)! 🎁\n\n🚀 *Welcome to VIP SMS Sender!*\n\nএখানে আপনি যেকোনো নাম্বারে ইনস্ট্যান্ট SMS পাঠাতে পারবেন।\n\n👇 নিচের বাটনগুলো ব্যবহার করুন:"
    else:
        welcome_msg = "🚀 *Welcome back to VIP SMS Sender!*\n\nএখানে আপনি যেকোনো নাম্বারে ইনস্ট্যান্ট SMS পাঠাতে পারবেন।\n\n👇 নিচের বাটনগুলো ব্যবহার করুন:"

    kb = await user_keyboard(token, user_id)
    await update.message.reply_text(welcome_msg, reply_markup=kb, parse_mode=ParseMode.MARKDOWN)

# ==========================================
# 💬 MESSAGE ROUTER
# ==========================================
async def handle_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.bot.token
    user = update.effective_user
    user_id = str(user.id)
    msg = update.message.text.strip() if update.message.text else ""

    bot_expiry = db.get_val(token, "bot_expiry_time", "life")
    if bot_expiry != "life" and user_id != MAIN_ADMIN:
        if int(time.time() * 1000) > bot_expiry:
            await update.message.reply_text("⚠️ *Bot Expired*\n\nএই বটের লাইসেন্স মেয়াদ শেষ হয়ে গেছে। দয়া করে মেইন এডমিনের সাথে যোগাযোগ করুন।", parse_mode=ParseMode.MARKDOWN)
            return

    if db.get_val(token, "is_suspended", False) and user_id != MAIN_ADMIN: return
    if db.get_val(token, f"blocked_{user_id}", False): return

    credits = db.get_val(token, f"credits_{user_id}", 0)
    expiry = db.get_val(token, f"expiry_time_{user_id}", 0)

    if credits <= 0 and expiry > 0:
        db.set_val(token, f"expiry_time_{user_id}", 0)
        expiry = 0

    if expiry > 0 and int(time.time() * 1000) > expiry:
        if credits > 0:
            db.set_val(token, f"credits_{user_id}", 0)
            db.set_val(token, f"expiry_time_{user_id}", 0)
            await update.message.reply_text("⚠️ *আপনার টোকেনগুলোর মেয়াদ শেষ হয়ে গেছে!* ব্যালেন্স 0 করা হয়েছে।")
            credits = 0

    if msg == "👑 Admin Panel":
        if await is_bot_admin(token, user_id):
            is_master = (user_id == MAIN_ADMIN)
            await update.message.reply_text(
                "👑 *VIP SMS Admin Panel*\n\nসবগুলো অপশন কন্ট্রোল করার জন্য নিচের বাটনগুলো ব্যবহার করুন:",
                reply_markup=admin_keyboard(is_master),
                parse_mode=ParseMode.MARKDOWN
            )
        return

    elif msg == "🚀 Send SMS":
        if db.get_val(token, "is_maintenance", False) and not await is_bot_admin(token, user_id):
            await update.message.reply_text("⚠️ *System Maintenance* ⚠️\n\nবর্তমানে SMS সার্ভার মেইনটেনেন্সে আছে। কিছুক্ষণ পরে আবার চেষ্টা করুন।")
            return

        last_sms_time = db.get_val(token, f"last_sms_{user_id}", 0)
        if not await is_bot_admin(token, user_id) and (time.time() - last_sms_time < 5):
            await update.message.reply_text("⚠️ *খুব দ্রুত মেসেজ পাঠাচ্ছেন!* দয়া করে ৫ সেকেন্ড অপেক্ষা করুন।")
            return

        if db.get_val(token, f"processing_sms_{user_id}", False):
            await update.message.reply_text("⚠️ *আপনার আগের রিকোয়েস্টটি এখনো প্রসেস হচ্ছে, দয়া করে অপেক্ষা করুন!*")
            return

        if token == MASTER_BOT_TOKEN:
            free_enabled = db.get_val(token, "free_sms_enabled", False)
            kb = []
            if free_enabled or user_id == MAIN_ADMIN:
                kb.append([InlineKeyboardButton("🆓 Free SMS", callback_data="flow_free_sms")])
            kb.append([InlineKeyboardButton("💳 Paid SMS", callback_data="flow_paid_sms")])
            await update.message.reply_text("✨ *কিভাবে SMS পাঠাতে চান তা নির্বাচন করুন:*", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
            return
        else:
            if credits < 1:
                await update.message.reply_text("❌ *দুঃখিত, আপনার পর্যাপ্ত ক্রেডিট নেই!* ক্রেডিট কিনুন বা কোড রিডিম করুন।")
                return
            db.set_val(token, f"step_{user_id}", 'get_phone_num_paid')
            await update.message.reply_text("📱 *নাম্বার দিন (Paid SMS):*\n_Bulk SMS এর জন্য নাম্বারগুলো কমা (,) দিয়ে দিন। (সর্বোচ্চ ৫টি)_", parse_mode=ParseMode.MARKDOWN)
            return

    elif msg == "👤 My Profile":
        name = escape_html(user.first_name if user.first_name else "User")
        role = escape_html(db.get_val(token, f"role_{user_id}", "Free User 👤"))
        refers = db.get_val(token, f"refers_{user_id}", 0)
        joined = db.get_val(token, f"join_date_{user_id}", "Unknown")
        
        expiry_text = format_timestamp(expiry)
        profile_msg = (
            f"👤 <b>My Profile</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
            f"👤 <b>Name:</b> {name}\n"
            f"🔰 <b>Role:</b> {role}\n"
            f"💳 <b>Credits:</b> <code>{credits} SMS</code>\n"
            f"⏳ <b>মেয়াদ:</b> <code>{expiry_text}</code>\n"
            f"👥 <b>Refers:</b> <code>{refers}</code>\n"
            f"📅 <b>Joined:</b> <code>{joined}</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(profile_msg, parse_mode=ParseMode.HTML)
        return

    elif msg == "👥 Referral":
        ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
        refers = db.get_val(token, f"refers_{user_id}", 0)
        ref_msg = (f"👥 *Referral Program*\n━━━━━━━━━━━━━━━━━━\nআপনার বন্ধুদের ইনভাইট করে জিতে নিন *১টি ফ্রি SMS ক্রেডিট (মেয়াদ ৩ দিন)*! 🎁\n\n🔗 *আপনার রেফারেল লিংক:* \n`{ref_link}`\n\n📊 *মোট সফল রেফার:* `{refers}` জন")
        await update.message.reply_text(ref_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    elif msg == "💰 Buy Credits":
        prices = db.get_val(token, "sms_prices", {"100": 25, "200": 50, "500": 115, "1000": 210})
        support_user = db.get_val(token, "support_username", "[@EMON_x_4](https://t.me/EMON_x_4)")
        buy_msg = (
            f"💰 *Buy SMS Credits*\n━━━━━━━━━━━━━━━━━━\n"
            f"💠 *100 SMS* - {prices.get('100', 25)} Tk (মেয়াদ: 30 দিন)\n"
            f"💠 *200 SMS* - {prices.get('200', 50)} Tk (মেয়াদ: 30 দিন)\n"
            f"💠 *500 SMS* - {prices.get('500', 115)} Tk (মেয়াদ: 30 দিন)\n"
            f"💠 *1000 SMS* - {prices.get('1000', 210)} Tk (মেয়াদ: 60 দিন)\n"
            f"━━━━━━━━━━━━━━━━━━\n\n📩 *Contact Admin:* {support_user}"
        )
        await update.message.reply_text(buy_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    elif msg == "🎁 Redeem Code":
        db.set_val(token, f"step_{user_id}", 'get_redeem_code')
        await update.message.reply_text("🎁 *আপনার Redeem Code টি এখানে লিখুন:*", parse_mode=ParseMode.MARKDOWN)
        return

    elif msg == "☎️ Support":
        support_user = db.get_val(token, "support_username", "[@EMON_x_4](https://t.me/EMON_x_4)")
        help_msg = (
            f"☎️ *Support & Help Desk*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"যেকোনো প্রয়োজনে এডমিনের সাথে যোগাযোগ করুন:\n"
            f"👉 {support_user}\n\n"
            f"📝 *কিভাবে Bulk SMS করবেন:*\n"
            f"🚀 `Send SMS` এ ক্লিক করে নাম্বার দেওয়ার সময় একসাথে অনেকগুলো নাম্বার কমা (`,`) দিয়ে লিখতে পারেন। (সর্বোচ্চ ৫টি)\n\n"
            f"⏰ *কিভাবে Scheduled SMS করবেন:*\n"
            f"ভবিষ্যতের কোনো সময়ে মেসেজ পাঠাতে চাইলে নিচের কমান্ডটি ব্যবহার করুন:\n"
            f"`/schedule [নাম্বার] [মিনিট] [মেসেজ]`\n"
            f"উদাহরণ: `/schedule 017XXXXX 60 Happy Birthday`\n"
            f"এটি ৬০ মিনিট পর এসএমএস চলে যাবে।\n\n"
            f"💡 _(কারো যদি ঠিক এইরকম একটি পাওয়ারফুল বট লাগে, এডমিন ফিচার ও সকল কন্ট্রোল প্যানেলসহ, সম্পূর্ণ সেটআপ আমরাই করে দেবো! বিস্তারিত জানতে ইনবক্স করুন।)_"
        )
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    step = db.get_val(token, f"step_{user_id}")
    if not step: return

    # ------------------ NUMBER INPUT FLOW ------------------
    if step.startswith('get_phone_num'):
        is_free_flow = step.endswith('_free')
        
        if is_free_flow:
            if token != MASTER_BOT_TOKEN:
                db.set_val(token, f"step_{user_id}", None)
                return
            free_enabled = db.get_val(token, "free_sms_enabled", False)
            if not free_enabled and user_id != MAIN_ADMIN:
                await update.message.reply_text("❌ *দুঃখিত, এডমিন কর্তৃক Free SMS বর্তমানে বন্ধ রাখা হয়েছে।*", parse_mode=ParseMode.MARKDOWN)
                db.set_val(token, f"step_{user_id}", None)
                return

        raw_nums = msg.split(",")
        valid_nums = []
        for n in raw_nums:
            cleaned = n.strip()
            if len(cleaned) >= 11 and cleaned.isdigit(): valid_nums.append(cleaned)
                
        if not valid_nums:
            await update.message.reply_text("❌ *ভুল নাম্বার!* সঠিক মোবাইল নাম্বারটি দিন (যেমন: 01xxxxxxxx)।")
            return

        if len(valid_nums) > 5:
            await update.message.reply_text("❌ *দুঃখিত!* আপনি একসাথে সর্বোচ্চ ৫টি নাম্বারে SMS পাঠাতে পারবেন।")
            return

        if is_free_flow:
            free_limit = db.get_val(token, "free_sms_limit", 100)
            my_free_usage = db.get_val(token, f"free_usage_{user_id}", 0)
            if user_id != MAIN_ADMIN and (my_free_usage + len(valid_nums) > free_limit):
                await update.message.reply_text(f"❌ *ফ্রি লিমিট শেষ!* আপনি ইতিমধ্যে {my_free_usage} টি ফ্রি SMS পাঠিয়েছেন। সর্বোচ্চ লিমিট: {free_limit}।\n\nঅনুগ্রহ করে Paid SMS অপশন ব্যবহার করুন।")
                db.set_val(token, f"step_{user_id}", None)
                return
                
            global_pool = db.get_val(token, "global_free_pool", "Unlimited")
            if global_pool != "Unlimited" and isinstance(global_pool, int):
                if global_pool < len(valid_nums):
                    await update.message.reply_text(f"❌ *গ্লোবাল ফ্রি লিমিট শেষ!* বটের ফ্রি SMS কোটা শেষ হয়ে গেছে।\nদয়া করে Paid SMS অপশন ব্যবহার করুন।")
                    db.set_val(token, f"step_{user_id}", None)
                    return
        else:
            if credits < len(valid_nums):
                await update.message.reply_text(f"❌ *দুঃখিত!* আপনার ব্যালেন্সে মাত্র {credits} টোকেন আছে কিন্তু আপনি {len(valid_nums)} টি নাম্বার দিয়েছেন।")
                return

        db.set_val(token, f"sms_nums_{user_id}", valid_nums)
        db.set_val(token, f"step_{user_id}", f'get_sms_body_{"free" if is_free_flow else "paid"}')
        
        await update.message.reply_text(f"📝 *Enter Message ({len(valid_nums)} numbers selected):*", parse_mode=ParseMode.MARKDOWN)
        return

    # ------------------ SMS BODY FLOW (WITH STRICT LOCK) ------------------
    elif step.startswith('get_sms_body'):
        is_free_flow = step.endswith('_free')
        db.set_val(token, f"step_{user_id}", None)
        valid_nums = db.get_val(token, f"sms_nums_{user_id}", [])
        if not valid_nums: return

        if db.get_val(token, f"processing_sms_{user_id}", False):
            await update.message.reply_text("⚠️ *আগের প্রসেস চলছে...*")
            return

        db.set_val(token, f"processing_sms_{user_id}", True)

        try:
            credits = db.get_val(token, f"credits_{user_id}", 0)
            
            if is_free_flow:
                if token != MASTER_BOT_TOKEN: return
                free_enabled = db.get_val(token, "free_sms_enabled", False)
                if not free_enabled and user_id != MAIN_ADMIN:
                    await update.message.reply_text("❌ *Free SMS বর্তমানে বন্ধ আছে!*")
                    return
                    
                free_limit = db.get_val(token, "free_sms_limit", 100)
                my_free_usage = db.get_val(token, f"free_usage_{user_id}", 0)
                if user_id != MAIN_ADMIN and (my_free_usage + len(valid_nums) > free_limit):
                    await update.message.reply_text("❌ *ফ্রি লিমিট শেষ!*")
                    return
                    
                global_pool = db.get_val(token, "global_free_pool", "Unlimited")
                if global_pool != "Unlimited" and isinstance(global_pool, int):
                    if global_pool < len(valid_nums):
                        await update.message.reply_text("❌ *গ্লোবাল ফ্রি লিমিট শেষ!* দয়া করে Paid SMS ব্যবহার করুন।")
                        return
            else:
                if credits < len(valid_nums):
                    await update.message.reply_text(f"❌ *দুঃখিত!* আপনার ব্যালেন্সে পর্যাপ্ত টোকেন নেই। বর্তমান ব্যালেন্স: {credits}")
                    return

                # Clone Bot Global Quota Check
                if token != MASTER_BOT_TOKEN:
                    global_used = db.get_val(token, "used_global_tokens", 0)
                    allocated = db.get_val(token, "allocated_global_tokens", 0)
                    if global_used + len(valid_nums) > allocated:
                        await update.message.reply_text("❌ *Bot Quota Exceeded!* এই বটের সার্ভার লিমিট শেষ হয়ে গেছে। দয়া করে বটের এডমিনের সাথে যোগাযোগ করুন।")
                        return

            db.set_val(token, f"last_sms_{user_id}", time.time())
            progress_msg = await update.message.reply_text("⚙️ *SMS Delivery Status:*\n\n[░░░░░░░░░░] 0% complete")
            
            await asyncio.sleep(0.2)
            await progress_msg.edit_text("⚙️ *SMS Delivery Status:*\n\n[███░░░░░░░] 30% complete")
            await asyncio.sleep(0.2)
            await progress_msg.edit_text("⚙️ *SMS Delivery Status:*\n\n[██████████] 100% complete")

            success_count = 0
            encoded_msg = urllib.parse.quote_plus(msg)

            async with aiohttp.ClientSession() as session:
                for num in valid_nums:
                    if is_free_flow:
                        api_url = FREE_SMS_DEFAULT_API.replace("[NUMBER]", num).replace("[MSG]", encoded_msg)
                        custom_kw = ""
                    else:
                        custom_api = db.get_val(token, "custom_api_url", "")
                        custom_kw = db.get_val(token, "custom_api_success", "")
                        
                        if custom_api:
                            api_url = custom_api.replace("[NUMBER]", num).replace("[MSG]", encoded_msg)
                        else:
                            api_url = f"https://demosoftpp.com/SMS/api.php?key=EMONX4&number={num}&msg={encoded_msg}"
                    
                    try:
                        async with session.get(api_url, timeout=15) as response:
                            res_content = await response.text()
                            is_success = False
                            
                            try:
                                res_json = json.loads(res_content)
                                if "response" in res_json and isinstance(res_json["response"], list) and len(res_json["response"]) > 0:
                                    if res_json["response"][0].get("status") == 0: is_success = True
                            except json.JSONDecodeError: pass
                                
                            if not is_success and response.status == 200:
                                res_lower = res_content.lower()
                                if custom_kw:
                                    if custom_kw.lower() in res_lower:
                                        is_success = True
                                else:
                                    if not any(x in res_lower for x in ["error", "fail", "invalid", "limit", "insufficient", "wrong", "bad"]):
                                        is_success = True
                            
                            if is_success: success_count += 1
                    except Exception as e:
                        logger.error(f"API Send Error for {num}: {e}")

            try: await progress_msg.delete()
            except: pass

            if success_count > 0:
                if is_free_flow:
                    my_free_usage = db.get_val(token, f"free_usage_{user_id}", 0)
                    db.set_val(token, f"free_usage_{user_id}", my_free_usage + success_count)
                    
                    global_pool = db.get_val(token, "global_free_pool", "Unlimited")
                    if global_pool != "Unlimited" and isinstance(global_pool, int):
                        db.set_val(token, "global_free_pool", max(0, global_pool - success_count))
                else:
                    credits -= success_count
                    db.set_val(token, f"credits_{user_id}", credits)
                    if credits <= 0: db.set_val(token, f"expiry_time_{user_id}", 0)

                global_used = db.get_val(token, "used_global_tokens", 0)
                db.set_val(token, "used_global_tokens", global_used + success_count)

                sms_sent = db.get_val(token, f"sms_sent_{user_id}", 0) + success_count
                db.set_val(token, f"sms_sent_{user_id}", sms_sent)

                cost_text = "🎁 `FREE MODE`" if is_free_flow else f"`{success_count} Credit(s)`"
                success_msg = (
                    f"✨ *SMS SENT SUCCESSFULLY* ✨\n"
                    f"💳 *Cost:* {cost_text}\n"
                    f"🔋 *Remaining Bal:* `{credits} Credits`"
                )
                if is_free_flow:
                    success_msg += "\n\n💡 _Note: This is a normal free SMS. Buy Paid SMS for premium speed, unlimited features & better delivery!_"
                
                await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN)

                try:
                    username_str = escape_html(f"@{user.username}" if user.username else "N/A")
                    role_str = escape_html(db.get_val(token, f"role_{user_id}", "Free User 👤"))
                    safe_msg_body = escape_html(msg)
                    bot_name = escape_html(context.bot.username)
                    bot_type = "MASTER BOT 👑" if token == MASTER_BOT_TOKEN else "CLONE BOT 🤖"

                    if is_free_flow: sms_type = "FREE SMS 🎁"
                    else:
                        last_source = db.get_val(token, f"last_credit_source_{user_id}", "PAID SMS 💳")
                        sms_type = last_source

                    log_msg = (
                        f"📲 <b>NEW SMS DELIVERED ({bot_type})</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"👤 <b>User Name:</b> <a href='tg://user?id={user_id}'>{escape_html(user.first_name)}</a>\n"
                        f"🔗 <b>Username:</b> {username_str}\n"
                        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                        f"💬 <b>Chat ID:</b> <code>{user_id}</code>\n"
                        f"🔰 <b>Role:</b> {role_str}\n"
                        f"📊 <b>Total SMS Sent:</b> <code>{sms_sent}</code>\n"
                        f"🔋 <b>Balance Remaining:</b> <code>{credits}</code>\n"
                        f"🤖 <b>Bot Source:</b> @{bot_name}\n\n"
                        f"🎯 <b>Target Number(s):</b> <code>{', '.join(valid_nums[:success_count])}</code>\n"
                        f"💬 <b>Message Text:</b> {safe_msg_body}\n"
                        f"⚙️ <b>Type:</b> <b>{sms_type}</b>"
                    )
                    await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_msg, parse_mode=ParseMode.HTML)
                except Exception as e:
                    logger.error(f"Global Log send Error: {e}")
            else:
                await update.message.reply_text("⚠️ *SMS ব্যর্থ হয়েছে!* সার্ভার ব্যস্ত আছে বা কোনো সঠিক রিকোয়েস্ট প্রসেস করা যায়নি। আপনার কোনো ক্রেডিট কাটা হয়নি।")

        finally:
            db.set_val(token, f"processing_sms_{user_id}", False)


    elif step == 'get_redeem_code':
        db.set_val(token, f"step_{user_id}", None)
        promo_codes = db.get_val(token, "promo_codes", {})
        
        if msg in promo_codes:
            data = promo_codes[msg]
            if data.get("uses", 0) >= data.get("max_uses", 0):
                await update.message.reply_text("❌ *দুঃখিত!* এই কোডটির লিমিট শেষ হয়ে গেছে।")
                return
            if db.get_val(token, f"used_code_{msg}_{user_id}", False):
                await update.message.reply_text("❌ *দুঃখিত!* আপনি ইতিমধ্যে এই কোডটি একবার ব্যবহার করেছেন।")
                return

            new_creds = credits + data.get("amount", 0)
            db.set_val(token, f"credits_{user_id}", new_creds)
            db.set_val(token, f"used_code_{msg}_{user_id}", True)
            db.set_val(token, f"last_credit_source_{user_id}", "PROMO SMS 🎟️")

            validity_days = data.get("validity_days", 0)
            if validity_days > 0:
                new_expiry = max(expiry, int(time.time() * 1000)) + (validity_days * 24 * 60 * 60 * 1000)
                db.set_val(token, f"expiry_time_{user_id}", new_expiry)
                
            db.set_val(token, f"role_{user_id}", "Premium User 🌟")

            data["uses"] = data.get("uses", 0) + 1
            promo_codes[msg] = data
            db.set_val(token, "promo_codes", promo_codes)

            exp_text = f"{validity_days} Days" if validity_days > 0 else "LifeTime ♾️"
            await update.message.reply_text(
                f"🎉 *Promo Code successfully redeemed!*\n━━━━━━━━━━━━━━━━━━\n"
                f"➕ `{data.get('amount')}` Credits added to your account.\n"
                f"⏳ *Validity Extended:* `{exp_text}`\n"
                f"💳 *Current Balance:* `{new_creds}` SMS", 
                parse_mode=ParseMode.MARKDOWN
            )
            
            try:
                username_str = escape_html(f"@{user.username}" if user.username else "N/A")
                log_msg = (
                    f"🎟️ <b>PROMO CODE REDEEMED</b>\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👤 <b>User:</b> <a href='tg://user?id={user_id}'>{escape_html(user.first_name)}</a>\n"
                    f"🔗 <b>Username:</b> {username_str}\n"
                    f"🆔 <b>User ID:</b> <code>{user_id}</code>\n"
                    f"🔑 <b>Code Used:</b> <code>{msg}</code>\n"
                    f"💰 <b>Reward:</b> <code>{data.get('amount')}</code> Credits\n"
                    f"📊 <b>Code Usage:</b> <code>{data.get('uses')}/{data.get('max_uses')}</code>\n"
                    f"🤖 <b>Bot Source:</b> @{context.bot.username}"
                )
                await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_msg, parse_mode=ParseMode.HTML)
            except Exception: pass
            
        else:
            await update.message.reply_text("❌ *অকার্যকর রিডিম কোড!* সঠিক কোড দিন।")

# ==========================================
# 🔘 INLINE BUTTONS
# ==========================================
async def inline_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = str(user.id)
    token = context.bot.token

    await query.answer()

    bot_expiry = db.get_val(token, "bot_expiry_time", "life")
    if bot_expiry != "life" and user_id != MAIN_ADMIN:
        if int(time.time() * 1000) > bot_expiry:
            await context.bot.send_message(chat_id=user_id, text="⚠️ *Bot Expired*\n\nএই বটের লাইসেন্স মেয়াদ শেষ হয়ে গেছে।", parse_mode=ParseMode.MARKDOWN)
            return

    if data == "flow_free_sms":
        if token != MASTER_BOT_TOKEN: return
        free_enabled = db.get_val(token, "free_sms_enabled", False)
        if not free_enabled and user_id != MAIN_ADMIN:
            await context.bot.send_message(chat_id=user_id, text="❌ *Free SMS বর্তমানে বন্ধ আছে!*", parse_mode=ParseMode.MARKDOWN)
            return
        db.set_val(token, f"step_{user_id}", 'get_phone_num_free')
        await context.bot.send_message(chat_id=user_id, text="📱 *নাম্বার দিন (Free SMS):*\n_Bulk SMS এর জন্য নাম্বারগুলো কমা (,) দিয়ে দিন। (সর্বোচ্চ ৫টি)_", parse_mode=ParseMode.MARKDOWN)
        return
        
    elif data == "flow_paid_sms":
        credits = db.get_val(token, f"credits_{user_id}", 0)
        if credits < 1:
            await context.bot.send_message(chat_id=user_id, text="❌ *দুঃখিত, আপনার পর্যাপ্ত ক্রেডিট নেই!*", parse_mode=ParseMode.MARKDOWN)
            return
        db.set_val(token, f"step_{user_id}", 'get_phone_num_paid')
        await context.bot.send_message(chat_id=user_id, text="📱 *নাম্বার দিন (Paid SMS):*\n_Bulk SMS এর জন্য নাম্বারগুলো কমা (,) দিয়ে দিন। (সর্বোচ্চ ৫টি)_", parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_show_stats":
        if not await is_bot_admin(token, user_id): return
        total_users = len(db.get_val(token, "all_users", []))
        global_used = db.get_val(token, "used_global_tokens", 0)
        stats_msg = (
            f"📊 <b>Bot Live Statistics (Admin)</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👥 <b>Total Users:</b> <code>{total_users}</code>\n"
            f"🛡️ <b>Server Status:</b> <code>Online 🟢</code>\n"
            f"🚀 <b>Total SMS Served:</b> <code>{global_used}</code>\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await context.bot.send_message(chat_id=user_id, text=stats_msg, parse_mode=ParseMode.HTML)
        return

    elif data == "adm_show_top":
        if not await is_bot_admin(token, user_id): return
        leaderboard = db.get_val(token, "leaderboard", [])
        top_msg = "🏆 <b>Top SMS Users (Admin)</b>\n━━━━━━━━━━━━━━━━━━\n"
        if not leaderboard: top_msg += "এখনো কোনো ডাটা নেই!\n"
        else:
            for item in leaderboard:
                item['current_credits'] = db.get_val(token, f"credits_{item.get('id')}", 0)
            sorted_board = sorted([i for i in leaderboard if i.get('current_credits', 0) > 0], key=lambda x: x.get('current_credits', 0), reverse=True)[:10]
            if not sorted_board: top_msg += "সবাই ক্রেডিট শেষ করে ফেলেছে! 😅\n"
            else:
                emojis = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
                for i, item in enumerate(sorted_board):
                    emoji = emojis[i] if i < len(emojis) else "🏅"
                    safe_name = escape_html(item.get('name', 'User'))
                    u_id = item.get('id')
                    top_msg += f"{emoji} <a href='tg://user?id={u_id}'>{safe_name}</a>\n   └ 💳 Bal: <code>{item.get('current_credits')}</code>\n\n"
        await context.bot.send_message(chat_id=user_id, text=top_msg, parse_mode=ParseMode.HTML)
        return

    elif data == "master_free_sms_menu" and user_id == MAIN_ADMIN:
        free_enabled = db.get_val(token, "free_sms_enabled", False)
        free_limit = db.get_val(token, "free_sms_limit", 100)
        global_pool = db.get_val(token, "global_free_pool", "Unlimited")
        
        status = "🟢 চালু (ON)" if free_enabled else "🔴 বন্ধ (OFF)"
        msg = (
            f"🎁 *Free SMS Settings*\n\n"
            f"বর্তমান অবস্থা: {status}\n"
            f"লিমিট প্রতি ইউজার: `{free_limit}` SMS\n"
            f"গ্লোবাল ফ্রি লিমিট: `{global_pool}` SMS\n\n"
            f"_যেকোনো অপশন পরিবর্তন করতে নিচের বাটনে ক্লিক করুন।_"
        )
        kb = [
            [InlineKeyboardButton("Toggle ON/OFF", callback_data="master_free_toggle")],
            [InlineKeyboardButton("Set Per User Limit", callback_data="master_free_setlimit"), InlineKeyboardButton("Set Global Limit", callback_data="master_free_setglobal")],
            [InlineKeyboardButton("🔄 Reset All Free Usage", callback_data="master_reset_free")],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data="back_to_admin_main")]
        ]
        await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "master_free_toggle" and user_id == MAIN_ADMIN:
        current = db.get_val(token, "free_sms_enabled", False)
        db.set_val(token, "free_sms_enabled", not current)
        await context.bot.send_message(chat_id=user_id, text=f"✅ Free SMS এখন **{'চালু' if not current else 'বন্ধ'}** করা হয়েছে!", parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "master_free_setlimit" and user_id == MAIN_ADMIN:
        db.set_val(token, f"adm_step_{user_id}", 'set_free_limit')
        await context.bot.send_message(chat_id=user_id, text="📝 *প্রতি ইউজারের জন্য Free SMS লিমিট লিখুন (যেমন: 100):*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return
        
    elif data == "master_free_setglobal" and user_id == MAIN_ADMIN:
        db.set_val(token, f"adm_step_{user_id}", 'set_global_free_limit')
        await context.bot.send_message(chat_id=user_id, text="📝 *গ্লোবাল ফ্রি লিমিট লিখুন (যেমন: 20):*\n(আনলিমিটেড করতে চাইলে লিখুন `Unlimited`)", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "master_reset_free" and user_id == MAIN_ADMIN:
        users = db.get_val(token, "all_users", [])
        for u in users:
            db.set_val(token, f"free_usage_{u}", 0)
        await context.bot.send_message(chat_id=user_id, text="✅ *সবার Free SMS লিমিট 0 তে রিসেট করা হয়েছে!*", parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "ultimate_master_power" and user_id == MAIN_ADMIN:
        msg = "⚡ *SUPER MASTER PANEL*\n\nআপনি এই বটের সর্বোচ্চ ক্ষমতার অধিকারী। নিচের অপশনগুলো দিয়ে পুরো গ্লোবাল সিস্টেম কন্ট্রোল করুন:"
        kb = [
            [InlineKeyboardButton("🌐 Global Broadcast (ALL BOTS)", callback_data="master_global_bc")],
            [InlineKeyboardButton("📊 Advanced Global Stats", callback_data="master_global_stats")],
            [InlineKeyboardButton("🔙 Back to Admin", callback_data="back_to_admin_main")]
        ]
        await context.bot.send_message(chat_id=user_id, text=msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.MARKDOWN)
        return
        
    elif data == "master_global_bc" and user_id == MAIN_ADMIN:
        db.set_val(token, f"adm_step_{user_id}", 'super_global_broadcast')
        await context.bot.send_message(chat_id=user_id, text="🌐 *Global Broadcast:*\nযে মেসেজ বা মিডিয়াটি পাঠাবেন তা মাস্টার বট এবং সমস্ত ক্লোন বটের সব ইউজারের কাছে পৌঁছে যাবে!", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return
        
    elif data == "master_global_stats" and user_id == MAIN_ADMIN:
        m_users = len(db.get_val(token, "all_users", []))
        total_clones = len(db.data.get("clones", {}))
        total_c_users = 0
        for c_token in db.data.get("clones", {}).keys():
            total_c_users += len(db.get_val(c_token, "all_users", []))
            
        msg = (
            f"📊 <b>ADVANCED GLOBAL STATS</b>\n━━━━━━━━━━━━━━━━━━\n"
            f"👑 <b>Master Bot Users:</b> <code>{m_users}</code>\n"
            f"🤖 <b>Total Clone Bots:</b> <code>{total_clones}</code>\n"
            f"👥 <b>Total Clone Users:</b> <code>{total_c_users}</code>\n"
            f"🌐 <b>Total Ecosystem Users:</b> <code>{m_users + total_c_users}</code>"
        )
        await context.bot.send_message(chat_id=user_id, text=msg, parse_mode=ParseMode.HTML)
        return

    elif data == "check_joined_member":
        unjoined = await get_unjoined_channels(context, token, query.from_user.id)
        if not unjoined:
            db.set_val(token, f"last_join_check_{user_id}", time.time())
            try: await query.message.delete()
            except: pass
            
            if not db.get_val(token, f"registered_{user_id}", False):
                db.set_val(token, f"registered_{user_id}", True)
                db.set_val(token, f"credits_{user_id}", 1)
                db.set_val(token, f"role_{user_id}", "Free User 👤")
                db.set_val(token, f"refers_{user_id}", 0)
                db.set_val(token, f"sms_sent_{user_id}", 0)
                db.set_val(token, f"expiry_time_{user_id}", int(time.time() * 1000) + (7 * 24 * 60 * 60 * 1000))
                db.set_val(token, f"join_date_{user_id}", get_bd_time_now().strftime("%Y-%m-%d %H:%M:%S"))
                await send_new_user_log(context, token, query.from_user, user_id)
                
                ref_id = db.get_val(token, f"temp_ref_{user_id}")
                if ref_id and ref_id != user_id and db.get_val(token, f"registered_{ref_id}", False):
                    r_credits = db.get_val(token, f"credits_{ref_id}", 0)
                    db.set_val(token, f"credits_{ref_id}", r_credits + 1)
                    db.set_val(token, f"refers_{ref_id}", db.get_val(token, f"refers_{ref_id}", 0) + 1)
                    try:
                        await context.bot.send_message(chat_id=ref_id, text="👥 *নতুন একজন জয়েন করেছে!*\n🎁 আপনি ১ ক্রেডিট পেয়েছেন।", parse_mode=ParseMode.MARKDOWN)
                    except Exception: pass
                    
            kb = await user_keyboard(token, user_id)
            await context.bot.send_message(chat_id=query.from_user.id, text="✅ জয়েন করা সফল হয়েছে! এখন আপনি বট ব্যবহার করতে পারবেন।", reply_markup=kb, parse_mode=ParseMode.MARKDOWN)
        else:
            await context.bot.send_message(chat_id=query.from_user.id, text="❌ আপনি এখনো সকল চ্যানেলে জয়েন করেননি!")
        return

    elif data == "adm_block":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'block_user')
        await context.bot.send_message(chat_id=user_id, text="🚫 *যে ইউজারকে ব্লক করতে চান তার ID, নাম বা @username দিন:*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_unblock":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'unblock_user')
        await context.bot.send_message(chat_id=user_id, text="✅ *যে ইউজারকে আনব্লক করতে চান তার ID, নাম বা @username দিন:*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_warn":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'warn_user')
        await context.bot.send_message(chat_id=user_id, text="⚠️ *ইউজার ID/@username এবং ওয়ার্নিং মেসেজ দিন:*\n\nবিন্যাস: `[ID বা @username] [Message]`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_give_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'give_credits')
        await context.bot.send_message(chat_id=user_id, text="📝 *ইউজার আইডি/@username, টোকেন ও মেয়াদ (দিন) দিন:*\n\nবিন্যাস: `[ID] [Credits] [Days]`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_take_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'take_credits')
        await context.bot.send_message(chat_id=user_id, text="📝 *ইউজার আইডি/@username ও কাটার পরিমাণ দিন:*\n\nবিন্যাস: `[ID] [Credits]`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_bc_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'broadcast_msg')
        await context.bot.send_message(chat_id=user_id, text="📝 *যে মেসেজটি ব্রডকাস্ট করতে চান তা দিন (ছবি, ভিডিও, টেক্সট সব সাপোর্ট করে):*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_user_info":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'user_info_lookup')
        await context.bot.send_message(chat_id=user_id, text="🔍 *যেকোনো ইউজারের ID, নাম বা @username দিন:*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_promo_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'create_promo')
        await context.bot.send_message(chat_id=user_id, text="🎟️ *নতুন প্রোমো কোড তৈরি:*\n\nবিন্যাস: `[কোড] [টোকেন] [সর্বোচ্চ_ইউজার] [মেয়াদ_দিন]`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_delpromo_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'delete_promo')
        await context.bot.send_message(chat_id=user_id, text="🗑️ *যে প্রোমো কোডটি ডিলিট করতে চান তা লিখুন:*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_price_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'set_pkg_prices')
        await context.bot.send_message(chat_id=user_id, text="💵 *প্যাকেজের দাম সেট করুন (100, 200, 500, 1000 SMS)।*\n\nবিন্যাস: `[100] [200] [500] [1000]`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_api_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'set_custom_api')
        current_api = db.get_val(token, "custom_api_url", "Not Set")
        if not current_api: current_api = "Not Set"
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🔗 *Custom API Settings*\n\nবর্তমান API: `{current_api}`\n\nনতুন API লিংক দিন।\n⚠️ *অবশ্যই লিংকের শেষে Number ও Message এর জায়গায় `[NUMBER]` এবং `[MSG]` দিবেন।*\n\n💡 API এর Success Keyword দিতে চাইলে `|` দিয়ে লিখুন।\nউদাহরণ: `https://api.com/send?num=[NUMBER]&msg=[MSG] | 200 ok`\n\nAPI রিমুভ করতে চাইলে লিখুন: `DELETE`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )
        return

    elif data == "adm_fc_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'set_force_channel')
        await context.bot.send_message(
            chat_id=user_id, 
            text="📢 *ফোর্স জয়েন সেট করুন (সর্বোচ্চ ৩টি):*\n\nবিন্যাস: `@Channel1, @Channel2`\nবা প্রাইভেট হলে: `-100xxx|https://link, @Channel2`", 
            reply_markup=ForceReply(selective=True), 
            parse_mode=ParseMode.MARKDOWN
        )
        return

    elif data == "adm_maint_menu":
        if not await is_bot_admin(token, user_id): return
        is_maint = db.get_val(token, "is_maintenance", False)
        new_state = not is_maint
        db.set_val(token, "is_maintenance", new_state)
        status_text = "🟢 চালু (ON)" if new_state else "🔴 বন্ধ (OFF)"
        try:
            await query.edit_message_text(f"✅ *মেইনটেনেন্স মোড সফলভাবে আপডেট হয়েছে!*\n\nবর্তমান অবস্থা: {status_text}", reply_markup=admin_keyboard(user_id == MAIN_ADMIN))
        except: pass
        if not new_state:
            db.set_val(token, f"adm_step_{user_id}", 'maint_compensation')
            await context.bot.send_message(chat_id=user_id, text="🎁 *ইউজার ক্ষতিপূরণ মেয়াদ (ঘণ্টা):*\nবট অফ থাকার জন্য কত ঘণ্টার মেয়াদ বাড়াতে চান? (না চাইলে 0 লিখুন)", reply_markup=ForceReply(selective=True))
        return

    elif data == "adm_bot_info":
        if not await is_bot_admin(token, user_id): return
        owner_id = str(db.get_val(token, "bot_owner_id", MAIN_ADMIN))
        used = db.get_val(token, "used_global_tokens", 0)
        limit = db.get_val(token, "allocated_global_tokens", "Unlimited")
        expiry = db.get_val(token, "bot_expiry_time", "life")
        info_msg = (
            f"🤖 *MY BOT INFORMATION*\n━━━━━━━━━━━━━━━━━━\n"
            f"👑 *Owner ID:* `{owner_id}`\n⏳ *License Expiry:* `{format_timestamp(expiry)}`\n"
            f"💬 *Tokens Used:* `{used}`\n📊 *Total Quota Limit:* `{limit}`\n"
        )
        await context.bot.send_message(chat_id=user_id, text=info_msg, parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_view_settings":
        if not await is_bot_admin(token, user_id): return
        sup_user = db.get_val(token, "support_username", "[@EMON_x_4](https://t.me/EMON_x_4)")
        
        chans = get_force_channels(token)
        force_chan_str = ", ".join([c['id'] for c in chans]) if chans else "None"
        
        custom_api = db.get_val(token, "custom_api_url", "Not Set")
        if not custom_api: custom_api = "Not Set"
        maint = db.get_val(token, "is_maintenance", False)
        prices = db.get_val(token, "sms_prices", {"100": 25, "200": 50, "500": 115, "1000": 210})

        setting_msg = (
            f"🛠 *BOT CONFIGURATIONS*\n━━━━━━━━━━━━━━━━━\n"
            f"☎️ Support: {sup_user}\n📢 Force Join: `{force_chan_str}`\n"
            f"🛑 Maintenance: `{'ON 🟢' if maint else 'OFF 🔴'}`\n"
            f"🔗 Custom API: `{custom_api}`\n\n💰 *Prices:*\n"
            f"100 SMS = {prices.get('100')} Tk\n200 SMS = {prices.get('200')} Tk\n"
            f"500 SMS = {prices.get('500')} Tk\n1000 SMS = {prices.get('1000')} Tk"
        )
        try: await query.edit_message_text(setting_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_keyboard(user_id == MAIN_ADMIN))
        except: pass
        return

    elif data == "master_setup_bot" and user_id == MAIN_ADMIN:
        db.set_val(token, f"adm_step_{user_id}", 'setup_clone_bot')
        await context.bot.send_message(
            chat_id=user_id,
            text="🛡️ *SETUP NEW CLONE BOT*\n━━━━━━━━━━━━━━━━━━\n"
                 "দয়া করে নতুন বটের তথ্যগুলো নিচের বিন্যাসে দিন:\n\n"
                 "বিন্যাস: `[BotToken] [OwnerID] [Days] [MaxTokens]`\n\n"
                 "উদাহরণ: `12345:abc 7034779471 30 5000`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )
        return
    
    elif data == "master_list_bots" and user_id == MAIN_ADMIN:
        with db.lock:
            clones = db.data.get("clones", {})
        if not clones:
            await context.bot.send_message(chat_id=user_id, text="❌ কোনো ক্লোন বট রেকর্ড সিস্টেমে নেই!")
            return

        kb = []
        for tkn, info in clones.items():
            safe_user = escape_html(info.get('username', 'Bot'))
            kb.append([InlineKeyboardButton(f"🤖 @{safe_user} ({info.get('owner')})", callback_data=f"manage_bot_{tkn[:10]}")] )
        kb.append([InlineKeyboardButton("🔙 Back to Admin", callback_data="back_to_admin_main")])
        
        try:
            await query.edit_message_text("🌐 <b>SELECT A BOT TO MANAGE:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        except: pass
        return
        
    elif data == "back_to_admin_main":
        if not await is_bot_admin(token, user_id): return
        try:
            await query.edit_message_text("👑 *VIP SMS Admin Panel*\n\nসবগুলো অপশন কন্ট্রোল করার জন্য নিচের বাটনগুলো ব্যবহার করুন:", reply_markup=admin_keyboard(user_id == MAIN_ADMIN), parse_mode=ParseMode.MARKDOWN)
        except: pass
        return

    elif data.startswith("manage_bot_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[2]
        with db.lock:
            clones = db.data.get("clones", {})
        target_token = None
        for tkn in clones.keys():
            if tkn.startswith(partial_token):
                target_token = tkn
                break
                
        if not target_token:
            await context.bot.send_message(chat_id=user_id, text="❌ Bot not found!")
            return

        info = clones[target_token]
        safe_user = escape_html(info.get('username', 'Bot'))
        used = db.get_val(target_token, "used_global_tokens", 0)
        bot_expiry = db.get_val(target_token, "bot_expiry_time", "life")
        exp_text = format_timestamp(bot_expiry)
        is_susp = db.get_val(target_token, "is_suspended", False)
        status = "🔴 Suspended" if is_susp else "🟢 Active"

        manage_msg = (
            f"🤖 <b>Bot:</b> @{safe_user}\n"
            f"👑 <b>Owner:</b> <code>{info.get('owner')}</code>\n"
            f"⏳ <b>License Expiry:</b> <code>{exp_text}</code>\n"
            f"💬 <b>Token Quota:</b> <code>{used}/{info.get('quota')}</code>\n"
            f"🛡️ <b>Status:</b> {status}\n\n"
            f"নিচের বাটনগুলো দিয়ে এই ক্লোন বটের লাইসেন্স ম্যানেজ করুন:"
        )
        kb = [
            [InlineKeyboardButton("➕ Add Quota & Days", callback_data=f"extboth_{partial_token}"), InlineKeyboardButton("⏳ Add Days Only", callback_data=f"extdays_{partial_token}")],
            [InlineKeyboardButton("➕ Add Quota", callback_data=f"addquota_{partial_token}"), InlineKeyboardButton("➖ Remove Quota", callback_data=f"remquota_{partial_token}")],
            [InlineKeyboardButton("🚫 Suspend/Unsuspend", callback_data=f"togglesusp_{partial_token}"), InlineKeyboardButton("🗑️ Remove Bot", callback_data=f"delbot_{partial_token}")],
            [InlineKeyboardButton("🔙 Back to List", callback_data="master_list_bots")]
        ]
        try:
            await query.edit_message_text(manage_msg, reply_markup=InlineKeyboardMarkup(kb), parse_mode=ParseMode.HTML)
        except: pass
        return

    elif data.startswith("extboth_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        db.set_val(token, f"adm_step_{user_id}", f'extboth_proc_{partial_token}')
        await context.bot.send_message(chat_id=user_id, text="📝 *নতুন মেয়াদ ও কোটা দিন:*\n\nবিন্যাস: `[Days] [Quota]`\nউদাহরণ: `30 5000`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return
    
    elif data.startswith("extdays_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        db.set_val(token, f"adm_step_{user_id}", f'extdays_proc_{partial_token}')
        await context.bot.send_message(chat_id=user_id, text="📝 *শুধু বৃদ্ধির মেয়াদ (দিন) দিন:*\n\nউদাহরণ: `30`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return
        
    elif data.startswith("addquota_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        db.set_val(token, f"adm_step_{user_id}", f'addquota_proc_{partial_token}')
        await context.bot.send_message(chat_id=user_id, text="📝 *কত কোটা যোগ করতে চান? (Number):*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data.startswith("remquota_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        db.set_val(token, f"adm_step_{user_id}", f'remquota_proc_{partial_token}')
        await context.bot.send_message(chat_id=user_id, text="📝 *কত কোটা কাটতে চান? (Number):*", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data.startswith("togglesusp_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        with db.lock:
            clones = db.data.get("clones", {})
        target_token = None
        for tkn in clones.keys():
            if tkn.startswith(partial_token):
                target_token = tkn
                break
        if target_token:
            current = db.get_val(target_token, "is_suspended", False)
            db.set_val(target_token, "is_suspended", not current)
            try:
                await query.edit_message_text(
                    f"✅ Bot is now {'🔴 Suspended' if not current else '🟢 Active'}.",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="master_list_bots")]])
                )
            except: pass
        return
    
    elif data.startswith("delbot_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        target_token = None
        
        with db.lock:
            for tkn in db.data.get("clones", {}).keys():
                if tkn.startswith(partial_token):
                    target_token = tkn
                    break
            
            if target_token:
                del db.data["clones"][target_token]
                db.save_internal()

        if target_token:
            if target_token in active_clones:
                app_to_stop = active_clones[target_token]
                asyncio.create_task(app_to_stop.updater.stop())
                asyncio.create_task(app_to_stop.stop())
                del active_clones[target_token]

            try:
                await query.edit_message_text("✅ *ক্লোন বটটি সফলভাবে রিমুভ এবং ডিলিট করা হয়েছে!*", parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="master_list_bots")]]))
            except: pass
        return

# ==========================================
# 🛡️ ASYNC BROADCAST TASK (Crash Free & Cross Bot Fix)
# ==========================================
async def background_broadcast(app_context, admin_id, original_msg_id, users_list, status_msg):
    sent_count = 0
    for u in users_list:
        try:
            await app_context.bot.copy_message(chat_id=u, from_chat_id=admin_id, message_id=original_msg_id)
            sent_count += 1
            await asyncio.sleep(0.05)
        except Exception: pass
    try: await status_msg.edit_text(f"✅ *ব্রডকাস্ট সফলভাবে সম্পন্ন হয়েছে!* ({sent_count} জনের কাছে পৌঁছেছে)", parse_mode=ParseMode.MARKDOWN)
    except: pass

async def background_super_broadcast(master_app_context, admin_id, original_msg: telegram.Message, status_msg):
    total_sent = 0
    original_msg_id = original_msg.message_id
    
    # 1. Broadast to Master Bot users directly
    m_users = db.get_val(MASTER_BOT_TOKEN, "all_users", [])
    for u in m_users:
        try:
            await master_app_context.bot.copy_message(chat_id=u, from_chat_id=admin_id, message_id=original_msg_id)
            total_sent += 1
        except Exception: pass
        await asyncio.sleep(0.05)

    # 2. Extract media payload if exists for Clone bots
    # Because cross-bot copy_message doesn't work!
    media_bytes = None
    media_type = None
    html_text = original_msg.text_html or original_msg.caption_html or ""
    
    try:
        if original_msg.photo:
            f = await original_msg.photo[-1].get_file()
            media_bytes = await f.download_as_bytearray()
            media_type = 'photo'
        elif original_msg.video:
            f = await original_msg.video.get_file()
            media_bytes = await f.download_as_bytearray()
            media_type = 'video'
        elif original_msg.document:
            f = await original_msg.document.get_file()
            media_bytes = await f.download_as_bytearray()
            media_type = 'document'
        elif original_msg.audio:
            f = await original_msg.audio.get_file()
            media_bytes = await f.download_as_bytearray()
            media_type = 'audio'
        elif original_msg.animation:
            f = await original_msg.animation.get_file()
            media_bytes = await f.download_as_bytearray()
            media_type = 'animation'
        elif original_msg.voice:
            f = await original_msg.voice.get_file()
            media_bytes = await f.download_as_bytearray()
            media_type = 'voice'
    except Exception as e:
        logger.error(f"Media download failed for super broadcast: {e}")

    # 3. Broadcast to all Clone Bots
    for c_token, app in list(active_clones.items()):
        if c_token == MASTER_BOT_TOKEN: continue

        c_users = db.get_val(c_token, "all_users", [])
        if not c_users: continue

        if media_type and media_bytes:
            # Re-upload once per clone bot to generate valid file_id
            owner_id = db.get_val(c_token, "bot_owner_id", MAIN_ADMIN)
            clone_msg_id = None
            try:
                sent_msg = None
                if media_type == 'photo':
                    sent_msg = await app.bot.send_photo(chat_id=owner_id, photo=media_bytes, caption=html_text, parse_mode=ParseMode.HTML)
                elif media_type == 'video':
                    sent_msg = await app.bot.send_video(chat_id=owner_id, video=media_bytes, caption=html_text, parse_mode=ParseMode.HTML)
                elif media_type == 'document':
                    sent_msg = await app.bot.send_document(chat_id=owner_id, document=media_bytes, caption=html_text, parse_mode=ParseMode.HTML)
                elif media_type == 'audio':
                    sent_msg = await app.bot.send_audio(chat_id=owner_id, audio=media_bytes, caption=html_text, parse_mode=ParseMode.HTML)
                elif media_type == 'animation':
                    sent_msg = await app.bot.send_animation(chat_id=owner_id, animation=media_bytes, caption=html_text, parse_mode=ParseMode.HTML)
                elif media_type == 'voice':
                    sent_msg = await app.bot.send_voice(chat_id=owner_id, voice=media_bytes, caption=html_text, parse_mode=ParseMode.HTML)
                
                if sent_msg:
                    clone_msg_id = sent_msg.message_id
            except Exception as e:
                logger.error(f"Clone media upload failed: {e}")

            # Now forward locally within the clone bot
            for cu in c_users:
                if clone_msg_id:
                    try:
                        await app.bot.copy_message(chat_id=cu, from_chat_id=owner_id, message_id=clone_msg_id)
                        total_sent += 1
                    except Exception: pass
                else:
                    if html_text: # text fallback
                        try:
                            await app.bot.send_message(chat_id=cu, text=html_text, parse_mode=ParseMode.HTML)
                            total_sent += 1
                        except Exception: pass
                await asyncio.sleep(0.05)

        else:
            # Text Only Broadcast
            if html_text:
                for cu in c_users:
                    try:
                        await app.bot.send_message(chat_id=cu, text=html_text, parse_mode=ParseMode.HTML)
                        total_sent += 1
                    except Exception: pass
                    await asyncio.sleep(0.05)
                    
    try: await status_msg.edit_text(f"✅ *Global Broadcast সফলভাবে সম্পন্ন হয়েছে!* (মোট {total_sent} জনের কাছে পৌঁছেছে)", parse_mode=ParseMode.MARKDOWN)
    except: pass


# ==========================================
# 🛡️ ADMIN MEDIA & TEXT REPLIES ROUTER
# ==========================================
async def handle_admin_replies(update: Update, context: ContextTypes.DEFAULT_TYPE, adm_step: str):
    token = context.bot.token
    user_id = str(update.effective_user.id)
    
    try:
        # 1. Handle Broadcasts (Can be Media or Text)
        if adm_step == 'broadcast_msg':
            db.set_val(token, f"adm_step_{user_id}", None)
            users = db.get_val(token, "all_users", [])
            m = await update.message.reply_text(f"⏳ *ব্রডকাস্ট শুরু হচ্ছে...* ({len(users)} users)\n_ব্যাকগ্রাউন্ডে পাঠানো হচ্ছে..._", parse_mode=ParseMode.MARKDOWN)
            asyncio.create_task(background_broadcast(context, user_id, update.message.message_id, users, m))
            return
            
        elif adm_step == 'super_global_broadcast':
            db.set_val(token, f"adm_step_{user_id}", None)
            m = await update.message.reply_text(f"⏳ *Global Broadcast (All Bots) শুরু হচ্ছে...*\n_এটি ব্যাকগ্রাউন্ডে প্রসেস হবে, আপনি বট ব্যবহার করতে পারেন।_", parse_mode=ParseMode.MARKDOWN)
            asyncio.create_task(background_super_broadcast(context, user_id, update.message, m))
            return

        # 2. Text Only Commands Below
        msg = update.message.text.strip() if update.message.text else ""
        if not msg:
            await update.message.reply_text("❌ দয়া করে টেক্সট ইনপুট দিন।")
            return

        if adm_step == 'block_user':
            db.set_val(token, f"adm_step_{user_id}", None)
            target_id = resolve_user_id(token, msg)
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID ব্যবহার করুন।")
                return
            db.set_val(token, f"blocked_{target_id}", True)
            await update.message.reply_text(f"✅ ইউজার `{target_id}` কে সফলভাবে ব্লক করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)

        elif adm_step == 'unblock_user':
            db.set_val(token, f"adm_step_{user_id}", None)
            target_id = resolve_user_id(token, msg)
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID ব্যবহার করুন।")
                return
            db.set_val(token, f"blocked_{target_id}", False)
            await update.message.reply_text(f"✅ ইউজার `{target_id}` কে আনব্লক করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)

        elif adm_step == 'warn_user':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split(" ", 1)
            if len(parts) < 2:
                await update.message.reply_text("❌ ভুল ফরম্যাট! `[ID] [Message]`")
                return
            target_id = resolve_user_id(token, parts[0])
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি!")
                return
            try:
                await context.bot.send_message(chat_id=target_id, text=f"⚠️ *ADMIN WARNING:*\n\n{parts[1]}", parse_mode=ParseMode.MARKDOWN)
                await update.message.reply_text(f"✅ ইউজার `{parts[0]}` কে ওয়ার্নিং পাঠানো হয়েছে।", parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await update.message.reply_text(f"❌ ওয়ার্নিং পাঠানো ব্যর্থ হয়েছে। Error: {e}")

        elif adm_step == 'give_credits':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) < 2:
                await update.message.reply_text("❌ ভুল ফরম্যাট! `[ID] [Credits] [Days]`")
                return
            target_id = resolve_user_id(token, parts[0])
            if not target_id:
                await update.message.reply_text("❌ এই ইউজারনেমটি ডাটাবেসে পাওয়া যায়নি। দয়া করে সঠিক Numeric ID দিন।")
                return
            try:
                amt = int(parts[1])
                days = int(parts[2]) if len(parts) > 2 else 30
                curr = db.get_val(token, f"credits_{target_id}", 0)
                
                new_balance = curr + amt
                db.set_val(token, f"credits_{target_id}", new_balance) 
                db.set_val(token, f"last_credit_source_{target_id}", "PAID SMS 💳") 
                
                curr_exp = db.get_val(token, f"expiry_time_{target_id}", 0)
                if curr_exp < int(time.time() * 1000): curr_exp = int(time.time() * 1000)
                
                new_expiry = curr_exp + (days * 24 * 60 * 60 * 1000)
                db.set_val(token, f"expiry_time_{target_id}", new_expiry) 
                
                db.set_val(token, f"role_{target_id}", "Premium User 🌟")

                await update.message.reply_text(f"✅ *ক্রেডিট ও মেয়াদ দেওয়া সফল হয়েছে!*\n\nUser: `{parts[0]}`\nCredits: `{amt}`\nDays: `{days}`\nRole updated to *Premium User 🌟*", parse_mode=ParseMode.MARKDOWN)
                try:
                    await context.bot.send_message(chat_id=target_id, text=f"🎁 *আপনি এডমিন কর্তৃক `{amt}` ক্রেডিট পেয়েছেন!*\n💳 *বর্তমান ব্যালেন্স:* `{new_balance}` SMS\n🌟 আপনি এখন একজন *Premium User*", parse_mode=ParseMode.MARKDOWN)
                except: pass
            except ValueError:
                 await update.message.reply_text("❌ ভুল ইনপুট! ক্রেডিট এবং দিন সংখ্যা হতে হবে।")

        elif adm_step == 'take_credits':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) < 2:
                await update.message.reply_text("❌ ভুল ফরম্যাট! `[ID] [Credits]`")
                return
            target_id = resolve_user_id(token, parts[0])
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID দিন।")
                return
            try:
                amt = int(parts[1])
                curr = db.get_val(token, f"credits_{target_id}", 0)
                final_creds = max(0, curr - amt)
                db.set_val(token, f"credits_{target_id}", final_creds)
                if final_creds <= 0: db.set_val(token, f"expiry_time_{target_id}", 0)
                await update.message.reply_text(f"✅ *টোকেন কেটে নেওয়া সফল হয়েছে!*\n\nDeducted: `{amt}`\nNew Balance: `{final_creds}`", parse_mode=ParseMode.MARKDOWN)
                try: await context.bot.send_message(chat_id=target_id, text=f"⚠️ এডমিন আপনার একাউন্ট থেকে `{amt}` ক্রেডিট কেটে নিয়েছেন!\nবর্তমান ব্যালেন্স: `{final_creds}`", parse_mode=ParseMode.MARKDOWN)
                except: pass
            except ValueError:
                await update.message.reply_text("❌ ভুল ফরম্যাট! টোকেন পরিমাণ সংখ্যা হতে হবে।")

        elif adm_step == 'user_info_lookup':
            db.set_val(token, f"adm_step_{user_id}", None)
            target_id = resolve_user_id(token, msg)
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম!")
                return
            c_creds = db.get_val(token, f"credits_{target_id}", 0)
            c_exp = db.get_val(token, f"expiry_time_{target_id}", 0)
            c_role = escape_html(db.get_val(token, f"role_{target_id}", "Free User 👤"))
            c_refers = db.get_val(token, f"refers_{target_id}", 0)
            c_sent = db.get_val(token, f"sms_sent_{target_id}", 0)
            c_joined = db.get_val(token, f"join_date_{target_id}", "Unknown")
            info_msg = (
                f"👤 <b>USER INFORMATION</b>\n━━━━━━━━━━━━━━━━━━\n"
                f"🆔 <b>ID:</b> <code>{target_id}</code>\n🔰 <b>Role:</b> {c_role}\n💳 <b>Credits:</b> <code>{c_creds}</code>\n"
                f"⏳ <b>Expiry:</b> <code>{format_timestamp(c_exp)}</code>\n👥 <b>Refers:</b> <code>{c_refers}</code>\n"
                f"💬 <b>Total SMS:</b> <code>{c_sent}</code>\n📅 <b>Joined:</b> <code>{c_joined}</code>\n"
                f"🔗 <b>Profile Link:</b> <a href='tg://user?id={target_id}'>View Profile</a>"
            )
            await update.message.reply_text(info_msg, parse_mode=ParseMode.HTML)

        elif adm_step == 'maint_compensation':
            db.set_val(token, f"adm_step_{user_id}", None)
            comp_hours = int(msg)
            if comp_hours > 0:
                users = db.get_val(token, "all_users", [])
                await update.message.reply_text("⏳ *ক্ষতিপূরণ প্রোসেস করা হচ্ছে...*")
                adjusted = 0
                for u in users:
                    curr_exp = db.get_val(token, f"expiry_time_{u}", 0)
                    if curr_exp > 0:
                        db.set_val(token, f"expiry_time_{u}", curr_exp + (comp_hours * 60 * 60 * 1000))
                        adjusted += 1
                await update.message.reply_text(f"✅ *ক্ষতিপূরণ দেওয়া সফল হয়েছে!* {adjusted} জনের মেয়াদ বেড়েছে।")

        elif adm_step == 'create_promo':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) != 4:
                await update.message.reply_text("❌ *ভুল ফরম্যাট!* `[কোড] [টোকেন] [সর্বোচ্চ_ইউজার] [মেয়াদ_দিন]`", parse_mode=ParseMode.MARKDOWN)
                return
            try:
                code, amt, max_use, days = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
                promo_codes = db.get_val(token, "promo_codes", {})
                promo_codes[code] = {"amount": amt, "max_uses": max_use, "uses": 0, "validity_days": days}
                db.set_val(token, "promo_codes", promo_codes)
                
                success_msg = (
                    f"✅ *New Promo Code Created!*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 *Code:* `{code}`\n"
                    f"💰 *Credits:* `{amt}`\n"
                    f"👥 *Limit:* `{max_use}` Users\n"
                    f"⏳ *Validity:* `{days}` Days\n"
                    f"🤖 *Bot:* @{context.bot.username}\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN)
                
                try:
                    log_text = (
                        f"🎟️ <b>NEW PROMO CODE CREATED</b>\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🔑 <b>Code:</b> <code>{code}</code>\n"
                        f"💰 <b>Credits:</b> <code>{amt}</code>\n"
                        f"👥 <b>Limit:</b> <code>{max_use}</code> Users\n"
                        f"⏳ <b>Validity:</b> <code>{days}</code> Days\n"
                        f"🤖 <b>Bot Source:</b> @{context.bot.username}"
                    )
                    await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_text, parse_mode=ParseMode.HTML)
                except Exception: pass
            except ValueError:
                await update.message.reply_text("❌ ভুল ইনপুট! সংখ্যা (Number) দিন।")

        elif adm_step == 'delete_promo':
            db.set_val(token, f"adm_step_{user_id}", None)
            promo_codes = db.get_val(token, "promo_codes", {})
            if msg in promo_codes:
                del promo_codes[msg]
                db.set_val(token, "promo_codes", promo_codes)
                await update.message.reply_text(f"✅ প্রোমো কোড `{msg}` ডিলিট করা হয়েছে।")
            else:
                await update.message.reply_text("❌ এই প্রোমো কোডটি সিস্টেমে নেই!")

        elif adm_step == 'set_pkg_prices':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) >= 4:
                try:
                    prices = {"100": int(parts[0]), "200": int(parts[1]), "500": int(parts[2]), "1000": int(parts[3])}
                    db.set_val(token, "sms_prices", prices)
                    await update.message.reply_text("✅ *প্যাকেজের নতুন দাম সফলভাবে সেট করা হয়েছে!*", parse_mode=ParseMode.MARKDOWN)
                except ValueError:
                    await update.message.reply_text("❌ *ভুল ইনপুট!* দাম সংখ্যা হতে হবে।")
            else:
                await update.message.reply_text("❌ *ভুল ফরম্যাট!* ৪টি দাম স্পেস দিয়ে লিখুন।")

        elif adm_step == 'set_custom_api':
            db.set_val(token, f"adm_step_{user_id}", None)
            if msg.upper() == 'DELETE':
                db.delete_val(token, "custom_api_url")
                db.delete_val(token, "custom_api_success")
                await update.message.reply_text("✅ *Custom API রিমুভ করা হয়েছে! এখন ডিফল্ট API কাজ করবে।*", parse_mode=ParseMode.MARKDOWN)
            else:
                parts = msg.split('|')
                api_url = parts[0].strip()
                success_kw = parts[1].strip().lower() if len(parts) > 1 else ""
                
                if "[NUMBER]" not in api_url or "[MSG]" not in api_url:
                    await update.message.reply_text("❌ *API লিংকে [NUMBER] এবং [MSG] থাকা বাধ্যতামূলক!*")
                else:
                    db.set_val(token, "custom_api_url", api_url)
                    db.set_val(token, "custom_api_success", success_kw)
                    await update.message.reply_text(f"✅ *Custom API সফলভাবে সেট করা হয়েছে!*\n\n🔗 API: `{api_url}`\n✅ Keyword: `{success_kw if success_kw else 'Default'}`", parse_mode=ParseMode.MARKDOWN)
                    
                    try:
                        log_text = f"🔗 <b>CUSTOM API UPDATED</b>\nBot: @{context.bot.username}\nAPI: <code>{api_url}</code>\nKeyword: <code>{success_kw}</code>"
                        await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_text, parse_mode=ParseMode.HTML)
                    except Exception: pass

        elif adm_step == 'set_force_channel':
            db.set_val(token, f"adm_step_{user_id}", None)
            channels = []
            for p in msg.split(','):
                p = p.strip()
                if not p: continue
                if '|' in p:
                    cid, lnk = p.split('|', 1)
                    channels.append({"id": cid.strip(), "link": lnk.strip()})
                else:
                    cid = p
                    lnk = f"https://t.me/{cid.replace('@', '')}"
                    channels.append({"id": cid, "link": lnk})
                    
            db.set_val(token, "force_channels", channels[:3]) 
            db.delete_val(token, "force_channel") 
            await update.message.reply_text(f"✅ ফোর্স জয়েন চ্যানেল আপডেট করা হয়েছে! মোট {len(channels[:3])} টি চ্যানেল/গ্রুপ যুক্ত হয়েছে।")

        elif adm_step == 'set_free_limit':
            db.set_val(token, f"adm_step_{user_id}", None)
            limit = int(msg)
            db.set_val(token, "free_sms_limit", limit)
            await update.message.reply_text(f"✅ Free SMS (Per User) লিমিট সফলভাবে `{limit}` সেট করা হয়েছে!", parse_mode=ParseMode.MARKDOWN)
            
        elif adm_step == 'set_global_free_limit':
            db.set_val(token, f"adm_step_{user_id}", None)
            if msg.lower() == "unlimited":
                db.set_val(token, "global_free_pool", "Unlimited")
                await update.message.reply_text(f"✅ গ্লোবাল Free SMS লিমিট সফলভাবে `Unlimited` সেট করা হয়েছে!", parse_mode=ParseMode.MARKDOWN)
            else:
                try:
                    limit = int(msg)
                    db.set_val(token, "global_free_pool", limit)
                    await update.message.reply_text(f"✅ গ্লোবাল Free SMS লিমিট সফলভাবে `{limit}` সেট করা হয়েছে!", parse_mode=ParseMode.MARKDOWN)
                except ValueError:
                    await update.message.reply_text("❌ *ভুল ইনপুট!* সংখ্যা দিন।")

        elif adm_step == 'setup_clone_bot' and user_id == MAIN_ADMIN:
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            c_token, owner_id, days, max_tokens = parts[0], parts[1], int(parts[2]), int(parts[3])
            
            await update.message.reply_text("⏳ *বট টোকেন ভেরিফাই করা হচ্ছে...*")
            db.set_val(c_token, "bot_owner_id", owner_id)
            db.set_val(c_token, "allocated_global_tokens", max_tokens)
            db.set_val(c_token, "used_global_tokens", 0)
            db.set_val(c_token, "is_suspended", False)
            db.set_val(c_token, "force_channels", [])
            db.set_val(c_token, "support_username", f"[Admin](tg://user?id={owner_id})")
            exp_time = "life" if days == 0 else int(time.time() * 1000) + (days * 24 * 60 * 60 * 1000)
            db.set_val(c_token, "bot_expiry_time", exp_time)

            success, username_res = await boot_clone_instance(c_token)
            if success:
                with db.lock:
                    if "clones" not in db.data: db.data["clones"] = {}
                    db.data["clones"][c_token] = {"owner": owner_id, "username": username_res, "quota": max_tokens}
                    db.save_internal()
                await update.message.reply_text(f"✅ *বট সেটআপ সফল!* 🤖 `@{username_res}`")
            else:
                await update.message.reply_text(f"❌ *বট সেটআপ ব্যর্থ হয়েছে!*\nError: {username_res}")

        elif adm_step.startswith('extboth_proc_') and user_id == MAIN_ADMIN:
            partial_token = adm_step.split("_")[2]
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            days, quota = int(parts[0]), int(parts[1])
            target_token = None
            with db.lock:
                for tkn in db.data.get("clones", {}).keys():
                    if tkn.startswith(partial_token): target_token = tkn; break
            if target_token:
                curr_quota = db.get_val(target_token, "allocated_global_tokens", 0)
                db.set_val(target_token, "allocated_global_tokens", curr_quota + quota)
                curr_exp = db.get_val(target_token, "bot_expiry_time", 0)
                if curr_exp == "life": curr_exp = int(time.time() * 1000)
                db.set_val(target_token, "bot_expiry_time", max(curr_exp, int(time.time() * 1000)) + (days * 24 * 60 * 60 * 1000))
                with db.lock:
                    db.data["clones"][target_token]["quota"] = curr_quota + quota
                    db.save_internal()
                await update.message.reply_text(f"✅ *কোটা এবং মেয়াদ সফলভাবে বাড়ানো হয়েছে!*\nAdded {quota} Quota and {days} Days.")

        elif adm_step.startswith('extdays_proc_') and user_id == MAIN_ADMIN:
            partial_token = adm_step.split("_")[2]
            db.set_val(token, f"adm_step_{user_id}", None)
            days = int(msg)
            target_token = None
            with db.lock:
                for tkn in db.data.get("clones", {}).keys():
                    if tkn.startswith(partial_token): target_token = tkn; break
            if target_token:
                curr_exp = db.get_val(target_token, "bot_expiry_time", 0)
                if curr_exp == "life": curr_exp = int(time.time() * 1000)
                db.set_val(target_token, "bot_expiry_time", max(curr_exp, int(time.time() * 1000)) + (days * 24 * 60 * 60 * 1000))
                await update.message.reply_text(f"✅ *মেয়াদ সফলভাবে বাড়ানো হয়েছে!*\nAdded {days} Days to License.")
                
        elif adm_step.startswith('addquota_proc_') and user_id == MAIN_ADMIN:
            partial_token = adm_step.split("_")[2]
            db.set_val(token, f"adm_step_{user_id}", None)
            try:
                quota = int(msg)
                target_token = None
                with db.lock:
                    for tkn in db.data.get("clones", {}).keys():
                        if tkn.startswith(partial_token): target_token = tkn; break
                if target_token:
                    curr_quota = db.get_val(target_token, "allocated_global_tokens", 0)
                    db.set_val(target_token, "allocated_global_tokens", curr_quota + quota)
                    with db.lock:
                        db.data["clones"][target_token]["quota"] = curr_quota + quota
                        db.save_internal()
                    await update.message.reply_text(f"✅ *সফলভাবে {quota} কোটা যোগ করা হয়েছে!*")
            except ValueError:
                await update.message.reply_text("❌ ইনপুট সংখ্যা হতে হবে।")

        elif adm_step.startswith('remquota_proc_') and user_id == MAIN_ADMIN:
            partial_token = adm_step.split("_")[2]
            db.set_val(token, f"adm_step_{user_id}", None)
            try:
                quota = int(msg)
                target_token = None
                with db.lock:
                    for tkn in db.data.get("clones", {}).keys():
                        if tkn.startswith(partial_token): target_token = tkn; break
                if target_token:
                    curr_quota = db.get_val(target_token, "allocated_global_tokens", 0)
                    final = max(0, curr_quota - quota)
                    db.set_val(target_token, "allocated_global_tokens", final)
                    with db.lock:
                        db.data["clones"][target_token]["quota"] = final
                        db.save_internal()
                    await update.message.reply_text(f"✅ *সফলভাবে {quota} কোটা কমানো হয়েছে!*")
            except ValueError:
                await update.message.reply_text("❌ ইনপুট সংখ্যা হতে হবে।")

    except Exception as e:
        await update.message.reply_text(f"❌ *প্রোসেস ব্যর্থ হয়েছে! Error: {e}*")


async def unified_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message: return
    token = context.bot.token
    user_id = str(update.effective_user.id)
    
    bot_expiry = db.get_val(token, "bot_expiry_time", "life")
    if bot_expiry != "life" and user_id != MAIN_ADMIN:
        if int(time.time() * 1000) > bot_expiry:
            return

    if await is_bot_admin(token, user_id):
        adm_step = db.get_val(token, f"adm_step_{user_id}")
        if adm_step:
            return await handle_admin_replies(update, context, adm_step)

    if not update.message.text: return
    msg = update.message.text.strip()

    buttons = ["🚀 Send SMS", "👤 My Profile", "👥 Referral", "💰 Buy Credits", "🎁 Redeem Code", "☎️ Support", "👑 Admin Panel"]
    if msg in buttons:
        db.set_val(token, f"step_{user_id}", None)
        db.set_val(token, f"adm_step_{user_id}", None)
        return await handle_user_messages(update, context)

    return await handle_user_messages(update, context)

# ==========================================
# ⏰ SCHEDULED SMS ENGINE
# ==========================================
async def scheduled_worker(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    token, user_id, num, msg = data.get("token"), data.get("uid"), data.get("num"), data.get("msg")

    credits = db.get_val(token, f"credits_{user_id}", 0)
    if credits >= 1:
        
        # Checking Clone Quota if it's not Master
        if token != MASTER_BOT_TOKEN:
            global_used = db.get_val(token, "used_global_tokens", 0)
            allocated = db.get_val(token, "allocated_global_tokens", 0)
            if global_used + 1 > allocated:
                await context.bot.send_message(chat_id=user_id, text="❌ *শিডিউল SMS ফেইল্ড!* বটের সার্ভার লিমিট শেষ।")
                return

        custom_api = db.get_val(token, "custom_api_url", "")
        custom_kw = db.get_val(token, "custom_api_success", "")
        encoded_msg = urllib.parse.quote_plus(msg)
        
        if custom_api:
            api_url = custom_api.replace("[NUMBER]", num).replace("[MSG]", encoded_msg)
        else:
            api_url = f"https://demosoftpp.com/SMS/api.php?key=EMONX4&number={num}&msg={encoded_msg}"
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(api_url, timeout=15) as response:
                    res_content = await response.text()
                    is_success = False
                    try:
                        res_json = json.loads(res_content)
                        if "response" in res_json and isinstance(res_json["response"], list) and len(res_json["response"]) > 0:
                            if res_json["response"][0].get("status") == 0: is_success = True
                    except json.JSONDecodeError: pass
                        
                    if not is_success and response.status == 200:
                        res_lower = res_content.lower()
                        if custom_kw:
                            if custom_kw.lower() in res_lower:
                                is_success = True
                        else:
                            if not any(x in res_lower for x in ["error", "fail", "invalid", "limit", "insufficient", "wrong", "bad"]):
                                is_success = True

                    if is_success:
                        db.set_val(token, f"credits_{user_id}", credits - 1)
                        if credits - 1 <= 0: db.set_val(token, f"expiry_time_{user_id}", 0)
                        g_used = db.get_val(token, "used_global_tokens", 0)
                        db.set_val(token, "used_global_tokens", g_used + 1)
                        await context.bot.send_message(chat_id=user_id, text=f"⏰ *আপনার শিডিউল করা মেসেজটি সফলভাবে পাঠানো হয়েছে!* (`{num}`) ১ ক্রেডিট কাটা হয়েছে।", parse_mode=ParseMode.MARKDOWN)
                    else:
                        await context.bot.send_message(chat_id=user_id, text=f"❌ *শিডিউল SMS ফেইল্ড!* সার্ভার এরর।")
            except Exception:
                await context.bot.send_message(chat_id=user_id, text="❌ *শিডিউল SMS ফেইল্ড!* কানেকশন এরর।")
    else:
        await context.bot.send_message(chat_id=user_id, text="❌ *শিডিউল SMS ফেইল্ড!* আপনার একাউন্টে পর্যাপ্ত ক্রেডিট ছিল ছিল না।")

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.bot.token
    user_id = str(update.effective_user.id)
    args = context.args
    
    bot_expiry = db.get_val(token, "bot_expiry_time", "life")
    if bot_expiry != "life" and user_id != MAIN_ADMIN:
        if int(time.time() * 1000) > bot_expiry:
            return

    if len(args) < 3:
        await update.message.reply_text("⚠️ *ব্যবহার বিধি:* `/schedule [নাম্বার] [মিনিট] [মেসেজ]`\nউদাহরণ: `/schedule 017XXXXXX 60 Happy Birthday`", parse_mode=ParseMode.MARKDOWN)
        return

    target_num, mins_str = args[0], args[1]
    text_content = " ".join(args[2:])

    if not target_num.isdigit() or len(target_num) < 11:
        await update.message.reply_text("❌ সঠিক নাম্বার দিন।")
        return
    if not mins_str.isdigit() or int(mins_str) < 1:
        await update.message.reply_text("❌ সঠিক সময় (মিনিট) দিন।")
        return

    credits = db.get_val(token, f"credits_{user_id}", 0)
    if credits < 1:
        await update.message.reply_text("❌ আপনার পর্যাপ্ত টোকেন নেই।")
        return

    context.job_queue.run_once(
        scheduled_worker, 
        when=int(mins_str) * 60, 
        data={"token": token, "uid": user_id, "num": target_num, "msg": text_content}
    )
    await update.message.reply_text(f"⏰ *আপনার SMS টি {mins_str} মিনিট পর পাঠানো হবে।* টোকেন তখন কাটা হবে।")

# ==========================================
# ⚙️ BOOT AND ENGINE (NETWORK PROTECTED)
# ==========================================
async def boot_clone_instance(token):
    if token == MASTER_BOT_TOKEN:
        return False, "এটি মাস্টার বটের টোকেন!"
    if token in active_clones:
        return False, "বটটি ইতিমধ্যে ব্যাকগ্রাউন্ডে সচল আছে।"
    
    try:
        req = HTTPXRequest(connection_pool_size=10, read_timeout=60.0, write_timeout=60.0, connect_timeout=60.0, pool_timeout=60.0)
        app = Application.builder().token(token).request(req).build()
        
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("schedule", schedule_command))
        app.add_handler(CallbackQueryHandler(inline_callback_router))

        app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, unified_message_handler))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True, timeout=30, read_timeout=45)

        active_clones[token] = app
        return True, app.bot.username
    except InvalidToken:
        return False, "ভুল বা অকার্যকর বটের টোকেন!"
    except Exception as e:
        return False, f"স্টার্টআপ এরর: {e}"

async def start_master_engine():
    logger.info("Initializing Master Clone SMS Engine with Firebase DB...")
    
    req = HTTPXRequest(connection_pool_size=20, read_timeout=60.0, write_timeout=60.0, connect_timeout=60.0, pool_timeout=60.0)
    app = Application.builder().token(MASTER_BOT_TOKEN).request(req).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CallbackQueryHandler(inline_callback_router))
    
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, unified_message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True, timeout=30, read_timeout=45)
    active_clones[MASTER_BOT_TOKEN] = app
    
    logger.info(f"Master Bot @{app.bot.username} is fully online and ready!")

    with db.lock:
        clones = db.data.get("clones", {})
        
    for token in list(clones.keys()):
        if db.get_val(token, "is_suspended", False):
            continue
        
        success, res_msg = await boot_clone_instance(token)
        if success:
            logger.info(f"Cloned Bot @{res_msg} successfully restarted.")
        else:
            logger.error(f"Cloned Bot startup failed for token: {token[:10]}... Error: {res_msg}")

    while True:
        await asyncio.sleep(3600)

if __name__ == '__main__':
    try:
        asyncio.run(start_master_engine())
    except KeyboardInterrupt:
        logger.info("Master engine stopped manually by user.")
