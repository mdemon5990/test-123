
import os
import sys
import time
import json
import logging
import asyncio
import aiohttp
import threading
import nest_asyncio
import urllib.parse
from datetime import datetime, timedelta
import pytz

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
from telegram.error import InvalidToken, TelegramError

# এসিঙ্ক্রোনাস ইভেন্ট লুপ ফিক্স
nest_asyncio.apply()

# লগার সেটআপ
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger("VIP_SMS_Master")

# আপনার মাস্টার বটের টোকেন এবং এডমিন আইডি
MASTER_BOT_TOKEN = "8820442516:AAGF73UUIMnRnP8y8kbxRsPTLdevncc3pSA"
MAIN_ADMIN = "7034779471"
GLOBAL_LOG_CHANNEL = "@ffxemon"
TIMEZONE = pytz.timezone("Asia/Dhaka")

active_clones = {}  

class JSONDatabase:
    def __init__(self, filename="master_database.json"):
        self.filename = filename
        self.lock = threading.Lock()
        self.data = {"clones": {}, "global_config": {}}
        self.load()

    def load(self):
        """Load data strictly once into memory to prevent race conditions."""
        with self.lock:
            if os.path.exists(self.filename):
                try:
                    with open(self.filename, 'r', encoding='utf-8') as f:
                        loaded_data = json.load(f)
                        self.data.update(loaded_data)
                except Exception as e:
                    logger.error(f"Database Load Error: {e}")
            else:
                self.save_internal()

    def save_internal(self):
        """Safely write memory data to the file."""
        try:
            temp_file = self.filename + ".tmp"
            with open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, ensure_ascii=False, indent=4)
            os.replace(temp_file, self.filename)
        except Exception as e:
            logger.error(f"Database Save Error: {e}")

    def save(self):
        with self.lock:
            self.save_internal()

    def get_val(self, token, key, default=None):
        """Read solely from memory to prevent disk-read overwrites during concurrency."""
        with self.lock:
            namespace = f"bot_{token[:10].replace(':', '_')}"
            if namespace not in self.data:
                return default
            return self.data[namespace].get(key, default)

    def set_val(self, token, key, value):
        """Write to memory and immediately sync to disk safely."""
        with self.lock:
            namespace = f"bot_{token[:10].replace(':', '_')}"
            if namespace not in self.data:
                self.data[namespace] = {}
            self.data[namespace][key] = value
            self.save_internal()

    def delete_val(self, token, key):
        with self.lock:
            namespace = f"bot_{token[:10].replace(':', '_')}"
            if namespace in self.data and key in self.data[namespace]:
                del self.data[namespace][key]
                self.save_internal()

db = JSONDatabase()

def resolve_user_id(token, input_text):
    """ইউজার আইডি, @username বা শুধু username লিখলেও আসল ID বের করে আনবে।"""
    input_text = str(input_text).strip()
    if input_text.startswith('@'):
        username = input_text[1:].lower()
    else:
        if input_text.isdigit():
            return input_text
        username = input_text.lower()
        
    mapped_id = db.get_val(token, f"user_map_{username}")
    return mapped_id

async def is_bot_admin(token, user_id):
    uid = str(user_id)
    if uid == MAIN_ADMIN:
        return True
    owner_id = str(db.get_val(token, "bot_owner_id", MAIN_ADMIN))
    return uid == owner_id

async def check_joined_channel(context: ContextTypes.DEFAULT_TYPE, user_id, channel_username):
    if not channel_username or channel_username.lower() == "none":
        return True
    try:
        member = await context.bot.get_chat_member(chat_id=channel_username, user_id=user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception:
        return False

def get_bd_time_now():
    return datetime.now(TIMEZONE)

def format_timestamp(ts):
    if not ts or ts == "life":
        return "LifeTime ♾️"
    try:
        dt = datetime.fromtimestamp(ts / 1000, TIMEZONE)
        return dt.strftime("%d/%m/%Y %I:%M %p")
    except:
        return "Unknown"

def sanitize_text(text):
    if not text: return "N/A"
    return str(text).replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '')

async def user_keyboard(token, user_id):
    keyboard = [
        ["🚀 Send SMS", "👤 My Profile"],
        ["👥 Referral", "💰 Buy Credits"],
        ["🎁 Redeem Code", "🏆 Top Users"],
        ["📊 Statistics", "☎️ Support"]
    ]
    if await is_bot_admin(token, user_id):
        keyboard.append(["👑 Admin Panel"])
        
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def admin_keyboard(is_master=False):
    keyboard = [
        [InlineKeyboardButton("🚫 Block", callback_data="adm_block"), InlineKeyboardButton("✅ Unblock", callback_data="adm_unblock"), InlineKeyboardButton("⚠️ Warn", callback_data="adm_warn")],
        [InlineKeyboardButton("🎁 Give Credits", callback_data="adm_give_menu"), InlineKeyboardButton("✂️ Take Credits", callback_data="adm_take_menu")],
        [InlineKeyboardButton("📢 Broadcast Msg", callback_data="adm_bc_menu"), InlineKeyboardButton("🔍 User Info", callback_data="adm_user_info")],
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
    return InlineKeyboardMarkup(keyboard)

async def send_new_user_log(context, token, user, user_id):
    """Send log to global channel when a new user registers"""
    try:
        username_str = f"@{user.username}" if user.username else "N/A"
        log_text = (
            f"🆕 *NEW USER JOINED*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👤 *Name:* {sanitize_text(user.first_name)}\n"
            f"🔗 *Username:* {username_str}\n"
            f"🆔 *User ID:* `{user_id}`\n"
            f"🤖 *Bot:* @{context.bot.username}"
        )
        await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_text, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        logger.error(f"Failed to send new user log: {e}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.bot.token
    user = update.effective_user
    user_id = str(user.id)
    args = context.args

    # Reset any pending steps
    db.set_val(token, f"step_{user_id}", None)
    db.set_val(token, f"adm_step_{user_id}", None)

    if db.get_val(token, "is_suspended", False) and user_id != MAIN_ADMIN:
        await update.message.reply_text(
            "🚫 *BOT SUSPENDED!*\n\nএই বটটির অ্যাক্সেস মেইন এডমিন কর্তৃক বাতিল করা হয়েছে।",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    bot_expiry = db.get_val(token, "bot_expiry_time", "life")
    if bot_expiry != "life" and user_id != MAIN_ADMIN:
        if int(time.time() * 1000) > bot_expiry:
            await update.message.reply_text(
                "⚠️ *Bot Expired*\n\nএই বটের লাইসেন্স মেয়াদ শেষ হয়ে গেছে। দয়া করে মেইন এডমিনের সাথে যোগাযোগ করুন।",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    if db.get_val(token, f"blocked_{user_id}", False):
        await update.message.reply_text("⚠️ *দুঃখিত! আপনাকে এই বট থেকে ব্লক করা হয়েছে।*", parse_mode=ParseMode.MARKDOWN)
        return

    if args and len(args) > 0:
        db.set_val(token, f"temp_ref_{user_id}", args[0])

    user_list = db.get_val(token, "all_users", [])
    if user_id not in user_list:
        user_list.append(user_id)
        db.set_val(token, "all_users", user_list)
    
    # Username mapping saved for resolve_user_id
    username = user.username.lower() if user.username else ""
    if username:
        db.set_val(token, f"user_map_{username}", user_id)

    leaderboard = db.get_val(token, "leaderboard", [])
    full_name = sanitize_text(user.first_name + (" " + user.last_name if user.last_name else ""))
    found = False
    for item in leaderboard:
        if item.get("id") == user_id:
            item["name"] = full_name
            found = True
            break
    if not found:
        leaderboard.append({"id": user_id, "name": full_name, "sent": db.get_val(token, f"sms_sent_{user_id}", 0)})
    db.set_val(token, "leaderboard", leaderboard)

    force_channel = db.get_val(token, "force_channel", "none")
    if force_channel.lower() != "none" and not await is_bot_admin(token, user_id):
        last_check = db.get_val(token, f"last_join_check_{user_id}", 0)
        current_time = time.time()
        
        if current_time - last_check > 86400: 
            joined = await check_joined_channel(context, user.id, force_channel)
            if joined:
                db.set_val(token, f"last_join_check_{user_id}", current_time)
            else:
                channel_url = force_channel.replace('@', '')
                kb = [
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channel_url}")],
                    [InlineKeyboardButton("✅ Check Joined", callback_data="check_joined_member")]
                ]
                await update.message.reply_text(
                    "📢 *চ্যানেলে জয়েন করা বাধ্যতামূলক!*\n\nবটটি ব্যবহার করতে হলে আমাদের অফিশিয়াল চ্যানেলে জয়েন করুন। জয়েন না করলে বটের কোনো ফিচার কাজ করবে না।",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode=ParseMode.MARKDOWN
                )
                return

    if not db.get_val(token, f"registered_{user_id}", False):
        db.set_val(token, f"registered_{user_id}", True)
        db.set_val(token, f"credits_{user_id}", 1) 
        db.set_val(token, f"role_{user_id}", "User 👤")
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
                    await context.bot.send_message(
                        chat_id=ref_id,
                        text="👥 *আপনার ইনভাইট লিংকের মাধ্যমে নতুন একজন জয়েন করেছে!*\n🎁 আপনি পেয়েছেন *১ ক্রেডিট বোনাস* এবং ৩ দিন মেয়াদ বৃদ্ধি পেয়েছে।",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception:
                    pass

        welcome_msg = "🎉 *স্বাগতম!*\nবটে যুক্ত হওয়ার জন্য আপনাকে উপহারস্বরূপ *১টি SMS ক্রেডিট ফ্রি* দেওয়া হলো (মেয়াদ ৭ দিন)! 🎁\n\n🚀 *Welcome to VIP SMS Sender!*\n\nএখানে আপনি যেকোনো নাম্বারে ইনস্ট্যান্ট SMS পাঠাতে পারবেন।\n\n👇 নিচের বাটনগুলো ব্যবহার করুন:"
    else:
        welcome_msg = "🚀 *Welcome back to VIP SMS Sender!*\n\nএখানে আপনি যেকোনো নাম্বারে ইনস্ট্যান্ট SMS পাঠাতে পারবেন।\n\n👇 নিচের বাটনগুলো ব্যবহার করুন:"

    kb = await user_keyboard(token, user_id)
    await update.message.reply_text(
        welcome_msg, 
        reply_markup=kb, 
        parse_mode=ParseMode.MARKDOWN
    )

async def handle_user_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.bot.token
    user = update.effective_user
    user_id = str(user.id)
    msg = update.message.text.strip()

    if db.get_val(token, "is_suspended", False) and user_id != MAIN_ADMIN:
        return

    if db.get_val(token, f"blocked_{user_id}", False):
        return

    force_channel = db.get_val(token, "force_channel", "none")
    if force_channel.lower() != "none" and not await is_bot_admin(token, user_id):
        last_check = db.get_val(token, f"last_join_check_{user_id}", 0)
        current_time = time.time()
        
        if current_time - last_check > 86400: 
            joined = await check_joined_channel(context, user.id, force_channel)
            if joined:
                db.set_val(token, f"last_join_check_{user_id}", current_time)
            else:
                channel_url = force_channel.replace('@', '')
                kb = [
                    [InlineKeyboardButton("📢 Join Channel", url=f"https://t.me/{channel_url}")],
                    [InlineKeyboardButton("✅ Check Joined", callback_data="check_joined_member")]
                ]
                await update.message.reply_text(
                    "📢 *বট ব্যবহারের জন্য জয়েন করা আবশ্যক!*",
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode=ParseMode.MARKDOWN
                )
                return

    credits = db.get_val(token, f"credits_{user_id}", 0)
    expiry = db.get_val(token, f"expiry_time_{user_id}", 0)

    if credits <= 0:
        db.set_val(token, f"expiry_time_{user_id}", 0)
        expiry = 0

    if expiry > 0 and int(time.time() * 1000) > expiry:
        if credits > 0:
            db.set_val(token, f"credits_{user_id}", 0)
            db.set_val(token, f"expiry_time_{user_id}", 0)
            await update.message.reply_text("⚠️ *আপনার টোকেনগুলোর মেয়াদ শেষ হয়ে গেছে!* ব্যালেন্স 0 করা হয়েছে।")
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
            await update.message.reply_text(
                "⚠️ *System Maintenance* ⚠️\n\nবর্তমানে SMS সার্ভার মেইনটেনেন্সে আছে। কিছুক্ষণ পরে আবার চেষ্টা করুন।"
            )
            return

        last_sms_time = db.get_val(token, f"last_sms_{user_id}", 0)
        if not await is_bot_admin(token, user_id) and (time.time() - last_sms_time < 5):
            await update.message.reply_text("⚠️ *খুব দ্রুত মেসেজ পাঠাচ্ছেন!* দয়া করে ৫ সেকেন্ড অপেক্ষা করুন।")
            return

        owner_id = str(db.get_val(token, "bot_owner_id", MAIN_ADMIN))
        if owner_id != MAIN_ADMIN:
            global_used = db.get_val(token, "used_global_tokens", 0)
            global_limit = db.get_val(token, "allocated_global_tokens", 0)
            if global_limit > 0 and global_used >= global_limit:
                await update.message.reply_text(
                    "⚠️ *BOT LIMIT REACHED!*\n\nএই বটের গ্লোবাল টোকেন লিমিট শেষ হয়ে গেছে। দয়া করে এডমিনের সাথে যোগাযোগ করুন।"
                )
                return

        free_mode_end = db.get_val(token, "free_mode_end", 0)
        is_free = (free_mode_end == "life" or (isinstance(free_mode_end, (int, float)) and free_mode_end > int(time.time() * 1000)))
        free_limit = db.get_val(token, "free_limit", 0)
        my_free_usage = db.get_val(token, f"free_usage_{user_id}", 0)

        can_send = False
        if is_free and (free_limit == 0 or my_free_usage < free_limit):
            can_send = True
        elif credits >= 1:
            can_send = True

        if not can_send:
            await update.message.reply_text("❌ *দুঃখিত, আপনার পর্যাপ্ত ক্রেডিট নেই!* অথবা ফ্রি লিমিট শেষ। ক্রেডিট কিনুন বা কোড রিডিম করুন।")
            return

        db.set_val(token, f"step_{user_id}", 'get_phone_num')
        await update.message.reply_text(
            "📱 *নাম্বার দিন (যেমন: 01xxxxxxxxx):*\n_Bulk SMS এর জন্য নাম্বারগুলো কমা (,) দিয়ে দিন। (সর্বোচ্চ ৫টি)_",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    elif msg == "👤 My Profile":
        name = sanitize_text(user.first_name if user.first_name else "User")
        role = db.get_val(token, f"role_{user_id}", "User 👤")
        refers = db.get_val(token, f"refers_{user_id}", 0)
        joined = db.get_val(token, f"join_date_{user_id}", "Unknown")
        
        expiry_text = format_timestamp(expiry)
        profile_msg = (
            f"👤 *My Profile*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 *User ID:* `{user_id}`\n"
            f"👤 *Name:* {name}\n"
            f"🔰 *Role:* {role}\n"
            f"💳 *Credits:* `{credits} SMS`\n"
            f"⏳ *মেয়াদ:* `{expiry_text}`\n"
            f"👥 *Refers:* `{refers}`\n"
            f"📅 *Joined:* `{joined}`\n"
            f"━━━━━━━━━━━━━━━━━━"
        )
        await update.message.reply_text(profile_msg, parse_mode=ParseMode.MARKDOWN)
        return

    elif msg == "👥 Referral":
        ref_link = f"https://t.me/{context.bot.username}?start={user_id}"
        refers = db.get_val(token, f"refers_{user_id}", 0)
        ref_msg = (
            f"👥 *Referral Program*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"আপনার বন্ধুদের ইনভাইট করে জিতে নিন *১টি ফ্রি SMS ক্রেডিট (মেয়াদ ৩ দিন)*! 🎁\n\n"
            f"🔗 *আপনার রেফারেল লিংক:* \n`{ref_link}`\n\n"
            f"📊 *মোট সফল রেফার:* `{refers}` জন"
        )
        await update.message.reply_text(ref_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    elif msg == "💰 Buy Credits":
        prices = db.get_val(token, "sms_prices", {"100": 25, "200": 50, "500": 115, "1000": 210})
        support_user = db.get_val(token, "support_username", "[@EMON_x_4](https://t.me/EMON_x_4)")
        
        buy_msg = (
            f"💰 *Buy SMS Credits*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"💠 *100 SMS* - {prices.get('100', 25)} Tk (মেয়াদ: 30 দিন)\n"
            f"💠 *200 SMS* - {prices.get('200', 50)} Tk (মেয়াদ: 30 দিন)\n"
            f"💠 *500 SMS* - {prices.get('500', 115)} Tk (মেয়াদ: 30 দিন)\n"
            f"💠 *1000 SMS* - {prices.get('1000', 210)} Tk (মেয়াদ: 60 দিন)\n"
            f"━━━━━━━━━━━━━━━━━━\n\n"
            f"📩 *Contact Admin:* {support_user}\n\n"
            f"_ক্রেডিট কিনতে উপরের এডমিন লিংকে মেসেজ দিন।_"
        )
        await update.message.reply_text(
            buy_msg, 
            parse_mode=ParseMode.MARKDOWN, 
            disable_web_page_preview=True
        )
        return

    elif msg == "🎁 Redeem Code":
        db.set_val(token, f"step_{user_id}", 'get_redeem_code')
        await update.message.reply_text("🎁 *আপনার Redeem Code টি এখানে লিখুন:*", parse_mode=ParseMode.MARKDOWN)
        return

    elif msg == "🏆 Top Users":
        leaderboard = db.get_val(token, "leaderboard", [])
        top_msg = "🏆 *Top SMS Users (By Credits)*\n━━━━━━━━━━━━━━━━━━\n"
        if not leaderboard:
            top_msg += "এখনো কোনো ডাটা নেই!\n"
        else:
            for item in leaderboard:
                item['current_credits'] = db.get_val(token, f"credits_{item.get('id')}", 0)
            
            filtered_board = [i for i in leaderboard if i.get('current_credits', 0) > 0]
            sorted_board = sorted(filtered_board, key=lambda x: x.get('current_credits', 0), reverse=True)[:10]

            if not sorted_board:
                top_msg += "সবাই ক্রেডিট শেষ করে ফেলেছে! 😅\n"
            else:
                emojis = ["🥇", "🥈", "🥉", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅", "🏅"]
                for i, item in enumerate(sorted_board):
                    emoji = emojis[i] if i < len(emojis) else "🏅"
                    top_msg += f"{emoji} *{item.get('name')}*\n   └ 💳 Bal: `{item.get('current_credits')}`\n\n"

        await update.message.reply_text(top_msg, parse_mode=ParseMode.MARKDOWN)
        return

    elif msg == "📊 Statistics":
        total_users = len(db.get_val(token, "all_users", []))
        global_used = db.get_val(token, "used_global_tokens", 0)
        global_limit = db.get_val(token, "allocated_global_tokens", 0)
        
        stats_msg = (
            f"📊 *Bot Live Statistics*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👥 *Total Users:* `{total_users}`\n"
            f"🛡️ *Server Status:* `Online 🟢`\n"
            f"🚀 *Total SMS Served:* `{global_used}`\n"
        )
        owner_id = str(db.get_val(token, "bot_owner_id", MAIN_ADMIN))
        if owner_id != MAIN_ADMIN:
            stats_msg += f"📊 *Bot Quota Left:* `{max(0, global_limit - global_used)}`\n"
        stats_msg += "━━━━━━━━━━━━━━━━━━"
        await update.message.reply_text(stats_msg, parse_mode=ParseMode.MARKDOWN)
        return

    elif msg == "☎️ Support":
        support_user = db.get_val(token, "support_username", "[@EMON_x_4](https://t.me/EMON_x_4)")
        help_msg = (
            f"☎️ *Support & Help Desk*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"যেকোনো প্রয়োজনে এডমিনের সাথে যোগাযোগ করুন:\n"
            f"👉 {support_user}\n\n"
            f"📝 *কিভাবে Bulk SMS করবেন:*\n"
            f"`🚀 Send SMS` এ ক্লিক করে নাম্বার দেওয়ার সময় একসাথে অনেকগুলো নাম্বার কমা (`,`) দিয়ে লিখতে পারেন। (সর্বোচ্চ ৫টি)\n"
            f"যেমন: `017XXXX, 018XXXX, 019XXXX`\n"
            f"বট একসাথে সব নাম্বারে মেসেজ পাঠিয়ে দিবে এবং সেই অনুযায়ী টোকেন কাটবে।\n\n"
            f"⏰ *কিভাবে Scheduled SMS করবেন:*\n"
            f"ভবিষ্যতের কোনো সময়ে মেসেজ পাঠাতে চাইলে নিচের কমান্ডটি ব্যবহার করুন:\n"
            f"`/schedule [নাম্বার] [মিনিট] [মেসেজ]`\n"
            f"উদাহরণ: `/schedule 017XXXXX 60 Happy Birthday`\n"
            f"(এটি ৬০ মিনিট পর মেসেজটি পাঠিয়ে দিবে)।"
        )
        await update.message.reply_text(help_msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
        return

    step = db.get_val(token, f"step_{user_id}")
    if not step:
        return

    if step == 'get_phone_num':
        raw_nums = msg.split(",")
        valid_nums = []
        for n in raw_nums:
            cleaned = n.strip()
            if len(cleaned) >= 11 and cleaned.isdigit():
                valid_nums.append(cleaned)
                
        if not valid_nums:
            await update.message.reply_text("❌ *ভুল নাম্বার!* সঠিক মোবাইল নাম্বারটি দিন (যেমন: 01xxxxxxxx)।")
            return

        if len(valid_nums) > 5:
            await update.message.reply_text("❌ *দুঃখিত!* আপনি একসাথে সর্বোচ্চ ৫টি নাম্বারে SMS পাঠাতে পারবেন।")
            return

        free_mode_end = db.get_val(token, "free_mode_end", 0)
        is_free = (free_mode_end == "life" or (isinstance(free_mode_end, (int, float)) and free_mode_end > int(time.time() * 1000)))
        
        if not is_free and credits < len(valid_nums):
            await update.message.reply_text(
                f"❌ *দুঃখিত!* আপনার ব্যালেন্সে মাত্র {credits} টোকেন আছে কিন্তু আপনি {len(valid_nums)} টি নাম্বার দিয়েছেন।"
            )
            return

        db.set_val(token, f"sms_nums_{user_id}", valid_nums)
        db.set_val(token, f"step_{user_id}", 'get_sms_body')
        
        await update.message.reply_text(
            f"📝 *Enter Message ({len(valid_nums)} numbers selected):*", 
            parse_mode=ParseMode.MARKDOWN
        )
        return

    elif step == 'get_sms_body':
        db.set_val(token, f"step_{user_id}", None)
        valid_nums = db.get_val(token, f"sms_nums_{user_id}", [])
        if not valid_nums:
            return

        free_mode_end = db.get_val(token, "free_mode_end", 0)
        is_free = (free_mode_end == "life" or (isinstance(free_mode_end, (int, float)) and free_mode_end > int(time.time() * 1000)))
        
        if not is_free and credits < len(valid_nums):
            await update.message.reply_text("❌ *দুঃখিত!* আপনার ব্যালেন্সে পর্যাপ্ত টোকেন নেই।")
            return

        db.set_val(token, f"last_sms_{user_id}", time.time())
        
        progress_msg = await update.message.reply_text("⚙️ *SMS Delivery Status:*\n\n[░░░░░░░░░░] 0% complete")
        
        await asyncio.sleep(0.2)
        await progress_msg.edit_text("⚙️ *SMS Delivery Status:*\n\n[███░░░░░░░] 30% complete")
        
        await asyncio.sleep(0.2)
        await progress_msg.edit_text("⚙️ *SMS Delivery Status:*\n\n[███████░░░] 70% complete")
        
        await asyncio.sleep(0.2)
        await progress_msg.edit_text("⚙️ *SMS Delivery Status:*\n\n[██████████] 100% complete")

        success_count = 0
        custom_api = db.get_val(token, "custom_api_url", "")
        
        encoded_msg = urllib.parse.quote_plus(msg)

        async with aiohttp.ClientSession() as session:
            for num in valid_nums:
                if custom_api:
                    api_url = custom_api.replace("[NUMBER]", num).replace("[MSG]", encoded_msg)
                else:
                    api_url = f"https://demosoftpp.com/SMS/api.php?key=EMONX4&number={num}&msg={encoded_msg}"
                
                try:
                    async with session.get(api_url, timeout=10) as response:
                        res_content = await response.text()
                        
                        is_success = False
                        
                        # Fix for API returning JSON like {"response":[{"status":0,"id":...,"msisdn":""}]}
                        try:
                            res_json = json.loads(res_content)
                            if "response" in res_json and isinstance(res_json["response"], list) and len(res_json["response"]) > 0:
                                if res_json["response"][0].get("status") == 0:
                                    is_success = True
                        except json.JSONDecodeError:
                            pass
                            
                        # Fallback for plain text responses
                        if not is_success and response.status == 200:
                            res_lower = res_content.lower()
                            if not any(x in res_lower for x in ["error", "fail", "invalid", "limit", "insufficient", "wrong", "missing", "bad"]):
                                is_success = True
                        
                        if is_success:
                            success_count += 1
                            
                except Exception as e:
                    logger.error(f"API Send Error for {num}: {e}")

        await asyncio.sleep(0.2)
        try:
            await progress_msg.delete()
        except:
            pass

        if success_count > 0:
            if not is_free:
                credits -= success_count
                db.set_val(token, f"credits_{user_id}", credits)
                if credits <= 0:
                    db.set_val(token, f"expiry_time_{user_id}", 0)

            global_used = db.get_val(token, "used_global_tokens", 0)
            db.set_val(token, "used_global_tokens", global_used + success_count)

            sms_sent = db.get_val(token, f"sms_sent_{user_id}", 0) + success_count
            db.set_val(token, f"sms_sent_{user_id}", sms_sent)

            leaderboard = db.get_val(token, "leaderboard", [])
            full_name = sanitize_text(user.first_name + (" " + user.last_name if user.last_name else ""))
            found = False
            for item in leaderboard:
                if item.get("id") == user_id:
                    item["sent"] = sms_sent
                    item["name"] = full_name
                    found = True
                    break
            if not found:
                leaderboard.append({"id": user_id, "name": full_name, "sent": sms_sent})
            
            db.set_val(token, "leaderboard", leaderboard)

            cost_text = "🎁 `FREE MODE`" if is_free else f"`{success_count} Credit(s)`"
            success_msg = (
                f"✨ *SMS SENT SUCCESSFULLY* ✨\n"
                f"💳 *Cost:* {cost_text}\n"
                f"🔋 *Remaining Bal:* `{credits} Credits`"
            )
            await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN)

            try:
                username_str = sanitize_text(f"@{user.username}" if user.username else "N/A")
                role_str = sanitize_text(db.get_val(token, f"role_{user_id}", "User 👤"))
                refers_str = db.get_val(token, f"refers_{user_id}", 0)
                joined_str = db.get_val(token, f"join_date_{user_id}", "Unknown")
                
                target_nums_str = ", ".join(valid_nums[:success_count])
                safe_msg_body = sanitize_text(msg)

                log_msg = (
                    f"📲 *NEW SMS DELIVERED LOG*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"👤 *User Profile:*\n"
                    f"🔗 *Username:* {username_str}\n"
                    f"🆔 *User ID:* `{user_id}`\n"
                    f"🔰 *Role:* {role_str}\n"
                    f"👥 *Total Refers:* `{refers_str}`\n"
                    f"📊 *Total SMS Sent:* `{sms_sent}`\n"
                    f"📅 *Joined Date:* `{joined_str}`\n"
                    f"🔋 *Balance Remaining:* `{credits}`\n\n"
                    f"🎯 *Target Number(s):* `{target_nums_str}`\n"
                    f"💬 *Message Text:* {safe_msg_body}"
                )
                await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_msg, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Global Log send Error: {e}")
        else:
            await update.message.reply_text(
                "⚠️ *SMS ব্যর্থ হয়েছে!* সার্ভার ব্যস্ত আছে বা কোনো সঠিক রিকোয়েস্ট প্রসেস করা যায়নি। আপনার কোনো ক্রেডিট কাটা হয়নি।"
            )

    elif step == 'get_redeem_code':
        db.set_val(token, f"step_{user_id}", None)
        promo_codes = db.get_val(token, "promo_codes", {})
        
        if msg in promo_codes:
            data = promo_codes[msg]
            if data.get("uses", 0) >= data.get("max_uses", 0):
                await update.message.reply_text("❌ *দুঃখিত!* এই কোডটির লিমিট শেষ হয়ে গেছে।")
                return
            if db.get_val(token, f"used_code_{msg}_{user_id}", False):
                await update.message.reply_text("❌ *দুঃখিত!* আপনি ইতিমধ্যে এই কোডটি একবার ব্যবহার করেছেন।")
                return

            new_creds = credits + data.get("amount", 0)
            db.set_val(token, f"credits_{user_id}", new_creds)
            db.set_val(token, f"used_code_{msg}_{user_id}", True)

            validity_days = data.get("validity_days", 0)
            if validity_days > 0:
                new_expiry = max(expiry, int(time.time() * 1000)) + (validity_days * 24 * 60 * 60 * 1000)
                db.set_val(token, f"expiry_time_{user_id}", new_expiry)

            data["uses"] = data.get("uses", 0) + 1
            promo_codes[msg] = data
            db.set_val(token, "promo_codes", promo_codes)

            exp_text = f"{validity_days} দিন" if validity_days > 0 else "LifeTime ♾️"
            await update.message.reply_text(
                f"🎉 *কোড সফলভাবে রিডিম হয়েছে!*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"➕ `{data.get('amount')}` ক্রেডিট আপনার একাউন্টে যোগ করা হয়েছে।\n"
                f"⏳ *মেয়াদ বেড়েছে:* `{exp_text}`\n"
                f"💳 *বর্তমান ব্যালেন্স:* `{new_creds}` SMS", 
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await update.message.reply_text("❌ *অকার্যকর রিডিম কোড!* সঠিক কোড দিন।")

async def inline_callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = str(user.id)
    token = context.bot.token

    await query.answer()

    if data == "check_joined_member":
        force_channel = db.get_val(token, "force_channel", "none")
        if force_channel.lower() != "none":
            joined = await check_joined_channel(context, query.from_user.id, force_channel)
            if joined:
                db.set_val(token, f"last_join_check_{user_id}", time.time())
                try:
                    await query.message.delete()
                except: pass
                
                if not db.get_val(token, f"registered_{user_id}", False):
                    db.set_val(token, f"registered_{user_id}", True)
                    db.set_val(token, f"credits_{user_id}", 1)
                    db.set_val(token, f"role_{user_id}", "User 👤")
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
                                await context.bot.send_message(
                                    chat_id=ref_id,
                                    text="👥 *আপনার ইনভাইট লিংকের মাধ্যমে নতুন একজন জয়েন করেছে!*\n🎁 আপনি পেয়েছেন *১ ক্রেডিট বোনাস* এবং ৩ দিন মেয়াদ বৃদ্ধি পেয়েছে।",
                                    parse_mode=ParseMode.MARKDOWN
                                )
                            except Exception:
                                pass
                    msg_text = "✅ জয়েন করা সফল হয়েছে! এখন আপনি বট ব্যবহার করতে পারবেন。\n\n🎉 *স্বাগতম!*\nউপহারস্বরূপ *১টি SMS ক্রেডিট ফ্রি* দেওয়া হলো (মেয়াদ ৭ দিন)! 🎁"
                else:
                    msg_text = "✅ জয়েন করা সফল হয়েছে! এখন আপনি বট ব্যবহার করতে পারবেন।"

                kb = await user_keyboard(token, user_id)
                await context.bot.send_message(
                    chat_id=query.from_user.id,
                    text=msg_text,
                    reply_markup=kb,
                    parse_mode=ParseMode.MARKDOWN
                )
            else:
                await context.bot.send_message(chat_id=query.from_user.id, text="❌ আপনি এখনো চ্যানেলে জয়েন করেননি! নিশ্চিত করুন যে আপনি চ্যানেলটিতে জয়েন আছেন।")
        return

    if data == "adm_block":
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
        await context.bot.send_message(chat_id=user_id, text="⚠️ *ইউজার ID/@username এবং ওয়ার্নিং মেসেজ দিন:*\n\nবিন্যাস: `[ID বা @username] [Message]`\nউদাহরণ: `@EMON_x_4 স্প্যাম করবেন না!`", reply_markup=ForceReply(selective=True), parse_mode=ParseMode.MARKDOWN)
        return

    elif data == "adm_give_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'give_credits')
        await context.bot.send_message(
            chat_id=user_id,
            text="📝 *ইউজার আইডি/@username, টোকেন ও মেয়াদ (দিন) দিন:*\n\nবিন্যাস: `[ID বা @username] [Credits] [Days]`\nউদাহরণ: `@EMON_x_4 100 30`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_take_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'take_credits')
        await context.bot.send_message(
            chat_id=user_id,
            text="📝 *ইউজার আইডি/@username ও কাটার পরিমাণ দিন:*\n\nবিন্যাস: `[ID বা @username] [Credits]`\nউদাহরণ: `@EMON_x_4 50`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_bc_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'broadcast_msg')
        await context.bot.send_message(
            chat_id=user_id,
            text="📝 *যে মেসেজটি ব্রডকাস্ট করতে চান তা লিখুন:*",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_maint_menu":
        if not await is_bot_admin(token, user_id): return
        is_maint = db.get_val(token, "is_maintenance", False)
        new_state = not is_maint
        db.set_val(token, "is_maintenance", new_state)
        
        status_text = "🟢 চালু (ON)" if new_state else "🔴 বন্ধ (OFF)"
        try:
            await query.edit_message_text(
                f"✅ *মেইনটেনেন্স মোড সফলভাবে আপডেট হয়েছে!*\n\nবর্তমান অবস্থা: {status_text}",
                reply_markup=admin_keyboard(user_id == MAIN_ADMIN)
            )
        except: pass
        
        if not new_state:
            db.set_val(token, f"adm_step_{user_id}", 'maint_compensation')
            await context.bot.send_message(
                chat_id=user_id,
                text="🎁 *ইউজার ক্ষতিপূরণ মেয়াদ (ঘণ্টা):*\nবট অফ থাকার জন্য ইউজারদের ক্ষতিপূরণ স্বরূপ কত ঘণ্টার মেয়াদ বাড়াতে চান? (মেয়াদ না বাড়াতে চাইলে 0 লিখুন)",
                reply_markup=ForceReply(selective=True)
            )

    elif data == "adm_promo_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'create_promo')
        await context.bot.send_message(
            chat_id=user_id,
            text="🎟️ *নতুন প্রোমো কোড তৈরি:*\n\nবিন্যাস: `[কোড] [টোকেন] [সর্বোচ্চ_ইউজার] [মেয়াদ_দিন]`\nউদাহরণ: `VIP500 500 50 30`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_delpromo_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'delete_promo')
        await context.bot.send_message(
            chat_id=user_id,
            text="🗑️ *যে প্রোমো কোডটি ডিলিট করতে চান তা লিখুন:*",
            reply_markup=ForceReply(selective=True)
        )

    elif data == "adm_price_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'set_pkg_prices')
        await context.bot.send_message(
            chat_id=user_id,
            text="💵 *প্যাকেজের দাম সেট করুন (100, 200, 500, 1000 SMS)।*\n\nবিন্যাস: `[100_Price] [200_Price] [500_Price] [1000_Price]`\nউদাহরণ: `25 50 115 210`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_api_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'set_custom_api')
        current_api = db.get_val(token, "custom_api_url", "Not Set")
        if not current_api: current_api = "Not Set"
        await context.bot.send_message(
            chat_id=user_id,
            text=f"🔗 *Custom API Settings*\n\n"
                 f"বর্তমান API: `{current_api}`\n\n"
                 f"নতুন API লিংক দিন। অবশ্যই লিংকে `[NUMBER]` এবং `[MSG]` থাকতে হবে।\n"
                 f"API রিমুভ করতে চাইলে লিখুন: `DELETE`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_fc_menu":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'set_force_channel')
        await context.bot.send_message(
            chat_id=user_id,
            text="📢 *ফোর্স জয়েন চ্যানেল সেট করুন (যেমন: @OfflineMogoj):*\n\n⚠️ *সতর্কতা:* বটকে অবশ্যই ওই চ্যানেলের এডমিন বানাতে হবে, না হলে চ্যানেল ভেরিফাই কাজ করবে না!",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_user_info":
        if not await is_bot_admin(token, user_id): return
        db.set_val(token, f"adm_step_{user_id}", 'user_info_lookup')
        await context.bot.send_message(
            chat_id=user_id,
            text="🔍 *যেকোনো ইউজারের ID, নাম বা @username দিন:*",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    elif data == "adm_bot_info":
        if not await is_bot_admin(token, user_id): return
        owner_id = str(db.get_val(token, "bot_owner_id", MAIN_ADMIN))
        
        used = db.get_val(token, "used_global_tokens", 0)
        limit = db.get_val(token, "allocated_global_tokens", "Unlimited")
        expiry = db.get_val(token, "bot_expiry_time", "life")
        
        info_msg = (
            f"🤖 *MY BOT INFORMATION*\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"👑 *Owner ID:* `{owner_id}`\n"
            f"⏳ *License Expiry:* `{format_timestamp(expiry)}`\n"
            f"💬 *Tokens Used:* `{used}`\n"
            f"📊 *Total Quota Limit:* `{limit}`\n"
        )
        await context.bot.send_message(chat_id=user_id, text=info_msg, parse_mode=ParseMode.MARKDOWN)

    elif data == "adm_view_settings":
        if not await is_bot_admin(token, user_id): return
        sup_user = db.get_val(token, "support_username", "[@EMON_x_4](https://t.me/EMON_x_4)")
        force_chan = db.get_val(token, "force_channel", "none")
        custom_api = db.get_val(token, "custom_api_url", "Not Set")
        if not custom_api: custom_api = "Not Set"
        maint = db.get_val(token, "is_maintenance", False)
        prices = db.get_val(token, "sms_prices", {"100": 25, "200": 50, "500": 115, "1000": 210})

        setting_msg = (
            f"🛠 *BOT CONFIGURATIONS*\n"
            f"━━━━━━━━━━━━━━━━━\n"
            f"☎️ Support: {sup_user}\n"
            f"📢 Force Join: `{force_chan}`\n"
            f"🛑 Server Maintenance: `{'ON 🟢' if maint else 'OFF 🔴'}`\n"
            f"🔗 Custom API: `{custom_api}`\n\n"
            f"💰 *Prices:*\n"
            f"100 SMS = {prices.get('100')} Tk\n"
            f"200 SMS = {prices.get('200')} Tk\n"
            f"500 SMS = {prices.get('500')} Tk\n"
            f"1000 SMS = {prices.get('1000')} Tk"
        )
        try:
            await query.edit_message_text(
                setting_msg, 
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=admin_keyboard(user_id == MAIN_ADMIN)
            )
        except: pass


    elif data == "master_setup_bot" and user_id == MAIN_ADMIN:
        db.set_val(token, f"adm_step_{user_id}", 'setup_clone_bot')
        await context.bot.send_message(
            chat_id=user_id,
            text="🛡️ *SETUP NEW CLONE BOT*\n━━━━━━━━━━━━━━━━━━\n"
                 "দয়া করে নতুন বটের তথ্যগুলো নিচের বিন্যাসে দিন:\n\n"
                 "বিন্যাস: `[BotToken] [OwnerID] [Days] [MaxTokens]`\n\n"
                 "উদাহরণ: `12345:abc 7034779471 30 5000`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    
    elif data == "master_list_bots" and user_id == MAIN_ADMIN:
        with db.lock:
            clones = db.data.get("clones", {})
        if not clones:
            await context.bot.send_message(chat_id=user_id, text="❌ কোনো ক্লোন বট রেকর্ড সিস্টেমে নেই!")
            return

        kb = []
        for tkn, info in clones.items():
            kb.append([InlineKeyboardButton(f"🤖 @{info.get('username', 'Bot')} ({info.get('owner')})", callback_data=f"manage_bot_{tkn[:10]}")] )
        
        try:
            await query.edit_message_text(
                "🌐 *SELECT A BOT TO MANAGE:*", 
                reply_markup=InlineKeyboardMarkup(kb)
            )
        except: pass


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
        used = db.get_val(target_token, "used_global_tokens", 0)
        bot_expiry = db.get_val(target_token, "bot_expiry_time", "life")
        exp_text = format_timestamp(bot_expiry)
        is_susp = db.get_val(target_token, "is_suspended", False)
        status = "🔴 Suspended" if is_susp else "🟢 Active"

        manage_msg = (
            f"🤖 *Bot:* @{info.get('username')}\n"
            f"👑 *Owner:* `{info.get('owner')}`\n"
            f"⏳ *License Expiry:* `{exp_text}`\n"
            f"💬 *Token Quota:* `{used}/{info.get('quota')}`\n"
            f"🛡️ *Status:* {status}\n\n"
            f"নিচের বাটনগুলো দিয়ে এই ক্লোন বটের লাইসেন্স ম্যানেজ করুন:"
        )
        kb = [
            [InlineKeyboardButton("➕ Add Quota & Days", callback_data=f"extboth_{partial_token}")],
            [InlineKeyboardButton("⏳ Add Days Only", callback_data=f"extdays_{partial_token}")],
            [InlineKeyboardButton("🚫 Suspend/Unsuspend", callback_data=f"togglesusp_{partial_token}"), InlineKeyboardButton("🗑️ Remove Bot", callback_data=f"delbot_{partial_token}")],
            [InlineKeyboardButton("🔙 Back to List", callback_data="master_list_bots")]
        ]
        try:
            await query.edit_message_text(
                manage_msg, 
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode=ParseMode.MARKDOWN
            )
        except: pass


    elif data.startswith("extboth_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        db.set_val(token, f"adm_step_{user_id}", f'extboth_proc_{partial_token}')
        await context.bot.send_message(
            chat_id=user_id,
            text="📝 *নতুন মেয়াদ ও কোটা দিন:*\n\nবিন্যাস: `[Days] [Quota]`\nউদাহরণ: `30 5000`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )

    
    elif data.startswith("extdays_") and user_id == MAIN_ADMIN:
        partial_token = data.split("_")[1]
        db.set_val(token, f"adm_step_{user_id}", f'extdays_proc_{partial_token}')
        await context.bot.send_message(
            chat_id=user_id,
            text="📝 *শুধু বৃদ্ধির মেয়াদ (দিন) দিন:*\n\nউদাহরণ: `30`",
            reply_markup=ForceReply(selective=True),
            parse_mode=ParseMode.MARKDOWN
        )


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
                await query.edit_message_text(
                    "✅ *ক্লোন বটটি সফলভাবে রিমুভ এবং ডিলিট করা হয়েছে!*",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to List", callback_data="master_list_bots")]])
                )
            except: pass


async def handle_admin_replies(update: Update, context: ContextTypes.DEFAULT_TYPE, adm_step: str):
    token = context.bot.token
    user_id = str(update.effective_user.id)
    msg = update.message.text.strip()

    try:
        if adm_step == 'block_user':
            db.set_val(token, f"adm_step_{user_id}", None)
            target_id_input = msg.strip()
            target_id = resolve_user_id(token, target_id_input)
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID অথবা @username দিন।")
                return
                
            db.set_val(token, f"blocked_{target_id}", True)
            await update.message.reply_text(f"✅ ইউজার `{target_id_input}` (ID: {target_id}) কে সফলভাবে ব্লক করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)

        elif adm_step == 'unblock_user':
            db.set_val(token, f"adm_step_{user_id}", None)
            target_id_input = msg.strip()
            target_id = resolve_user_id(token, target_id_input)
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID অথবা @username দিন।")
                return
                
            db.set_val(token, f"blocked_{target_id}", False)
            await update.message.reply_text(f"✅ ইউজার `{target_id_input}` (ID: {target_id}) কে সফলভাবে আনব্লক করা হয়েছে।", parse_mode=ParseMode.MARKDOWN)

        elif adm_step == 'warn_user':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split(" ", 1)
            if len(parts) < 2:
                await update.message.reply_text("❌ ভুল ফরম্যাট! সঠিক ফরম্যাট: `[ID বা @username] [Message]`")
                return
                
            target_id_input, warning_msg = parts[0], parts[1]
            target_id = resolve_user_id(token, target_id_input)
            
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID অথবা @username দিন।")
                return
                
            try:
                await context.bot.send_message(chat_id=target_id, text=f"⚠️ *ADMIN WARNING:*\n\n{warning_msg}", parse_mode=ParseMode.MARKDOWN)
                await update.message.reply_text(f"✅ ইউজার `{target_id_input}` কে ওয়ার্নিং পাঠানো হয়েছে।", parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                await update.message.reply_text(f"❌ ওয়ার্নিং পাঠানো ব্যর্থ হয়েছে। Error: {e}")

        elif adm_step == 'give_credits':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) < 2:
                await update.message.reply_text("❌ ভুল ফরম্যাট! সঠিক ফরম্যাট: `[ID বা @username] [Credits] [Days]`\nউদাহরণ: `@EMON_x_4 100 30`", parse_mode=ParseMode.MARKDOWN)
                return
            
            target_id_input = parts[0]
            target_id = resolve_user_id(token, target_id_input)
            
            if not target_id:
                await update.message.reply_text("❌ এই ইউজারনেমটি ডাটাবেসে পাওয়া যায়নি। দয়া করে সঠিক Numeric ID ব্যবহার করুন, অথবা ইউজারকে বটটি একবার /start করতে বলুন।")
                return
                
            try:
                amt = int(parts[1])
                days = int(parts[2]) if len(parts) > 2 else 30
                
                curr = db.get_val(token, f"credits_{target_id}", 0)
                db.set_val(token, f"credits_{target_id}", curr + amt)

                curr_exp = db.get_val(token, f"expiry_time_{target_id}", 0)
                if curr_exp < int(time.time() * 1000):
                    curr_exp = int(time.time() * 1000)
                new_exp = curr_exp + (days * 24 * 60 * 60 * 1000)
                db.set_val(token, f"expiry_time_{target_id}", new_exp)

                await update.message.reply_text(f"✅ *ক্রেডিট ও মেয়াদ দেওয়া সফল হয়েছে!*\n\nUser: `{target_id_input}`\nID: `{target_id}`\nCredits: `{amt}`\nDays: `{days}`", parse_mode=ParseMode.MARKDOWN)
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=f"🎁 *আপনি এডমিন কর্তৃক `{amt}` ক্রেডিট পেয়েছেন!*\n💳 *বর্তমান ব্যালেন্স:* `{curr + amt}` SMS",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except:
                    pass
            except ValueError:
                 await update.message.reply_text("❌ ভুল ইনপুট! ক্রেডিট এবং দিন সংখ্যা হতে হবে।")


        elif adm_step == 'take_credits':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) < 2:
                await update.message.reply_text("❌ ভুল ফরম্যাট! সঠিক ফরম্যাট: `[ID বা @username] [Credits]`")
                return
                
            target_id_input = parts[0]
            target_id = resolve_user_id(token, target_id_input)
            
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID অথবা @username দিন।")
                return
                
            try:
                amt = int(parts[1])
                curr = db.get_val(token, f"credits_{target_id}", 0)
                final_creds = max(0, curr - amt)
                db.set_val(token, f"credits_{target_id}", final_creds)
                
                if final_creds <= 0:
                    db.set_val(token, f"expiry_time_{target_id}", 0)

                await update.message.reply_text(f"✅ *টোকেন কেটে নেওয়া সফল হয়েছে!*\n\nUser: `{target_id_input}` (ID: `{target_id}`)\nDeducted: `{amt}`", parse_mode=ParseMode.MARKDOWN)
                try:
                    await context.bot.send_message(
                        chat_id=target_id,
                        text=f"⚠️ এডমিন আপনার একাউন্ট থেকে `{amt}` ক্রেডিট কেটে নিয়েছেন!",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except: pass
            except ValueError:
                await update.message.reply_text("❌ ভুল ফরম্যাট! টোকেন পরিমাণ সংখ্যা হতে হবে।")

        
        elif adm_step == 'broadcast_msg':
            db.set_val(token, f"adm_step_{user_id}", None)
            users = db.get_val(token, "all_users", [])
            await update.message.reply_text(f"⏳ *ব্রডকাস্ট শুরু হচ্ছে...* ({len(users)} users)")
            
            sent_count = 0
            for u in users:
                try:
                    await context.bot.send_message(chat_id=u, text=msg)
                    sent_count += 1
                    await asyncio.sleep(0.05) 
                except Exception:
                    pass
            await update.message.reply_text(f"✅ *ব্রডকাস্ট সফলভাবে সম্পন্ন হয়েছে!* ({sent_count} সফল)")

        elif adm_step == 'user_info_lookup':
            db.set_val(token, f"adm_step_{user_id}", None)
            target_id_input = msg.strip()
            target_id = resolve_user_id(token, target_id_input)
            
            if not target_id:
                await update.message.reply_text("❌ ভুল ইউজার আইডি বা ইউজারনেম! সঠিক Numeric ID অথবা @username দিন।")
                return
            
            c_creds = db.get_val(token, f"credits_{target_id}", 0)
            c_exp = db.get_val(token, f"expiry_time_{target_id}", 0)
            c_role = db.get_val(token, f"role_{target_id}", "User 👤")
            c_refers = db.get_val(token, f"refers_{target_id}", 0)
            c_sent = db.get_val(token, f"sms_sent_{target_id}", 0)
            c_joined = db.get_val(token, f"join_date_{target_id}", "Unknown")
            
            info_msg = (
                f"👤 *USER INFORMATION*\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🆔 *ID:* `{target_id}`\n"
                f"🔰 *Role:* {c_role}\n"
                f"💳 *Credits:* `{c_creds}`\n"
                f"⏳ *Expiry:* `{format_timestamp(c_exp)}`\n"
                f"👥 *Refers:* `{c_refers}`\n"
                f"💬 *Total SMS:* `{c_sent}`\n"
                f"📅 *Joined:* `{c_joined}`"
            )
            await update.message.reply_text(info_msg, parse_mode=ParseMode.MARKDOWN)

        elif adm_step == 'maint_compensation':
            db.set_val(token, f"adm_step_{user_id}", None)
            comp_hours = int(msg.strip())
            if comp_hours > 0:
                users = db.get_val(token, "all_users", [])
                await update.message.reply_text("⏳ *ক্ষতিপূরণ প্রোসেস করা হচ্ছে...*")
                
                adjusted = 0
                for u in users:
                    curr_exp = db.get_val(token, f"expiry_time_{u}", 0)
                    if curr_exp > 0:
                        db.set_val(token, f"expiry_time_{u}", curr_exp + (comp_hours * 60 * 60 * 1000))
                        adjusted += 1
                await update.message.reply_text(f"✅ *ক্ষতিপূরণ দেওয়া সফল হয়েছে!* {adjusted} জনের মেয়াদ বেড়েছে।")

        elif adm_step == 'create_promo':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) != 4:
                await update.message.reply_text("❌ *ভুল ফরম্যাট!* সঠিক ফরম্যাট: `[কোড] [টোকেন] [সর্বোচ্চ_ইউজার] [মেয়াদ_দিন]`\nউদাহরণ: `VIP500 500 50 30`", parse_mode=ParseMode.MARKDOWN)
                return
                
            try:
                code = parts[0]
                amt = int(parts[1])
                max_use = int(parts[2])
                days = int(parts[3])
                
                promo_codes = db.get_val(token, "promo_codes", {})
                if not isinstance(promo_codes, dict):
                    promo_codes = {}
                    
                promo_codes[code] = {
                    "amount": amt,
                    "max_uses": max_use,
                    "uses": 0,
                    "validity_days": days
                }
                db.set_val(token, "promo_codes", promo_codes)
                
                success_msg = (
                    f"✅ *নতুন প্রোমো কোড তৈরি হয়েছে!*\n"
                    f"━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 *কোড:* `{code}`\n"
                    f"💰 *ক্রেডিট:* `{amt}`\n"
                    f"👥 *লিমিট:* `{max_use}` জন\n"
                    f"⏳ *মেয়াদ:* `{days}` দিন\n"
                    f"━━━━━━━━━━━━━━━━━━"
                )
                await update.message.reply_text(success_msg, parse_mode=ParseMode.MARKDOWN)

                # Send log to global channel
                try:
                    log_text = (
                        f"🎟️ *NEW PROMO CODE CREATED*\n"
                        f"━━━━━━━━━━━━━━━━━━\n"
                        f"🔑 *Code:* `{code}`\n"
                        f"💰 *Credits:* `{amt}`\n"
                        f"👥 *Limit:* `{max_use}` Users\n"
                        f"⏳ *Validity:* `{days}` Days\n"
                        f"🤖 *Bot:* @{context.bot.username}"
                    )
                    await context.bot.send_message(chat_id=GLOBAL_LOG_CHANNEL, text=log_text, parse_mode=ParseMode.MARKDOWN)
                except Exception: pass

            except ValueError:
                await update.message.reply_text("❌ *ভুল ইনপুট!* টোকেন, ইউজার লিমিট এবং মেয়াদ অবশ্যই সংখ্যা (Number) হতে হবে।", parse_mode=ParseMode.MARKDOWN)

        elif adm_step == 'delete_promo':
            db.set_val(token, f"adm_step_{user_id}", None)
            promo_codes = db.get_val(token, "promo_codes", {})
            if msg in promo_codes:
                del promo_codes[msg]
                db.set_val(token, "promo_codes", promo_codes)
                await update.message.reply_text(f"✅ প্রোমো কোড `{msg}` ডিলিট করা হয়েছে।")
            else:
                await update.message.reply_text("❌ এই প্রোমো কোডটি সিস্টেমে নেই!")

        elif adm_step == 'set_pkg_prices':
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            if len(parts) >= 4:
                try:
                    prices = {
                        "100": int(parts[0]),
                        "200": int(parts[1]),
                        "500": int(parts[2]),
                        "1000": int(parts[3])
                    }
                    db.set_val(token, "sms_prices", prices)
                    await update.message.reply_text("✅ *প্যাকেজের নতুন দাম সফলভাবে সেট করা হয়েছে!*", parse_mode=ParseMode.MARKDOWN)
                except ValueError:
                    await update.message.reply_text("❌ *ভুল ইনপুট!* দাম অবশ্যই সংখ্যা (Number) হতে হবে।")
            else:
                await update.message.reply_text("❌ *ভুল ফরম্যাট!* দয়া করে ৪টি দাম স্পেস দিয়ে লিখুন।")

        elif adm_step == 'set_custom_api':
            db.set_val(token, f"adm_step_{user_id}", None)
            if msg.strip().upper() == 'DELETE':
                db.delete_val(token, "custom_api_url")
                await update.message.reply_text("✅ *Custom API রিমুভ করা হয়েছে! এখন ডিফল্ট API কাজ করবে।*", parse_mode=ParseMode.MARKDOWN)
            else:
                if "[NUMBER]" not in msg or "[MSG]" not in msg:
                     await update.message.reply_text("❌ *API তে [NUMBER] এবং [MSG] থাকা বাধ্যতামূলক!*")
                else:
                     db.set_val(token, "custom_api_url", msg.strip())
                     await update.message.reply_text("✅ *Custom API সফলভাবে সেট করা হয়েছে!*", parse_mode=ParseMode.MARKDOWN)


        elif adm_step == 'set_force_channel':
            db.set_val(token, f"adm_step_{user_id}", None)
            ch = msg.strip()
            db.set_val(token, "force_channel", ch)
            await update.message.reply_text(f"✅ ফোর্স জয়েন সিস্টেম আপডেট করা হয়েছে: `{ch}`")


        elif adm_step == 'setup_clone_bot' and user_id == MAIN_ADMIN:
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            c_token, owner_id, days, max_tokens = parts[0], parts[1], int(parts[2]), int(parts[3])
            
            await update.message.reply_text("⏳ *বট টোকেন ভেরিফাই করা হচ্ছে এবং ডেটাবেস সেট করা হচ্ছে...*")

            db.set_val(c_token, "bot_owner_id", owner_id)
            db.set_val(c_token, "allocated_global_tokens", max_tokens)
            db.set_val(c_token, "used_global_tokens", 0)
            db.set_val(c_token, "is_suspended", False)
            db.set_val(c_token, "force_channel", "none")
            
            db.set_val(c_token, "support_username", f"[Admin](tg://user?id={owner_id})")
            
            exp_time = "life" if days == 0 else int(time.time() * 1000) + (days * 24 * 60 * 60 * 1000)
            db.set_val(c_token, "bot_expiry_time", exp_time)

            success, username_res = await boot_clone_instance(c_token)
            if success:
                with db.lock:
                    if "clones" not in db.data: db.data["clones"] = {}
                    db.data["clones"][c_token] = {
                        "owner": owner_id,
                        "username": username_res,
                        "quota": max_tokens
                    }
                    db.save_internal()

                await update.message.reply_text(
                    f"✅ *বট ভেরিফাইড এবং সেটআপ সফল!*\n\n"
                    f"🤖 Username: `@{username_res}`\n"
                    f"👑 Owner ID: `{owner_id}`\n"
                    f"⏳ Expiry: `{days} Days`\n"
                    f"💬 Max Tokens: `{max_tokens}`"
                )
            else:
                await update.message.reply_text(f"❌ *বট সেটআপ ব্যর্থ হয়েছে!*\nError: {username_res}")


        elif adm_step.startswith('extboth_proc_') and user_id == MAIN_ADMIN:
            partial_token = adm_step.split("_")[2]
            db.set_val(token, f"adm_step_{user_id}", None)
            parts = msg.split()
            days, quota = int(parts[0]), int(parts[1])

            target_token = None
            with db.lock:
                clones = db.data.get("clones", {})
                for tkn in clones.keys():
                    if tkn.startswith(partial_token):
                        target_token = tkn
                        break

            if target_token:
                curr_quota = db.get_val(target_token, "allocated_global_tokens", 0)
                db.set_val(target_token, "allocated_global_tokens", curr_quota + quota)

                curr_exp = db.get_val(target_token, "bot_expiry_time", 0)
                if curr_exp == "life":
                    curr_exp = int(time.time() * 1000)
                
                new_expiry = max(curr_exp, int(time.time() * 1000)) + (days * 24 * 60 * 60 * 1000)
                db.set_val(target_token, "bot_expiry_time", new_expiry)

                with db.lock:
                    db.data["clones"][target_token]["quota"] = curr_quota + quota
                    db.save_internal()

                await update.message.reply_text(f"✅ *কোটা এবং মেয়াদ সফলভাবে বাড়ানো হয়েছে!*\nAdded {quota} Quota and {days} Days.")

        elif adm_step.startswith('extdays_proc_') and user_id == MAIN_ADMIN:
            partial_token = adm_step.split("_")[2]
            db.set_val(token, f"adm_step_{user_id}", None)
            days = int(msg.strip())

            target_token = None
            with db.lock:
                clones = db.data.get("clones", {})
                for tkn in clones.keys():
                    if tkn.startswith(partial_token):
                        target_token = tkn
                        break

            if target_token:
                curr_exp = db.get_val(target_token, "bot_expiry_time", 0)
                if curr_exp == "life":
                    curr_exp = int(time.time() * 1000)
                
                new_expiry = max(curr_exp, int(time.time() * 1000)) + (days * 24 * 60 * 60 * 1000)
                db.set_val(target_token, "bot_expiry_time", new_expiry)

                await update.message.reply_text(f"✅ *মেয়াদ সফলভাবে বাড়ানো হয়েছে!*\nAdded {days} Days to License.")

    except Exception as e:
        await update.message.reply_text(f"❌ *প্রোসেস ব্যর্থ হয়েছে!*\nঅনুগ্রহ করে সঠিক ফরম্যাট চেক করুন। Error: {e}")

async def unified_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles routing for texts to fix the force reply & stuck step bugs."""
    if not update.message or not update.message.text: return
    
    token = context.bot.token
    user_id = str(update.effective_user.id)
    msg = update.message.text.strip()

    # Clear steps if a menu button is pressed
    buttons = ["🚀 Send SMS", "👤 My Profile", "👥 Referral", "💰 Buy Credits", "🎁 Redeem Code", "🏆 Top Users", "📊 Statistics", "☎️ Support", "👑 Admin Panel"]
    if msg in buttons:
        db.set_val(token, f"step_{user_id}", None)
        db.set_val(token, f"adm_step_{user_id}", None)
        return await handle_user_messages(update, context)

    # Check if user is admin and has a pending admin step
    if await is_bot_admin(token, user_id):
        adm_step = db.get_val(token, f"adm_step_{user_id}")
        if adm_step:
            return await handle_admin_replies(update, context, adm_step)
            
    # Default to user message handling
    return await handle_user_messages(update, context)

async def scheduled_worker(context: ContextTypes.DEFAULT_TYPE):
    job = context.job
    data = job.data
    
    token = data.get("token")
    user_id = data.get("uid")
    num = data.get("num")
    msg = data.get("msg")

    credits = db.get_val(token, f"credits_{user_id}", 0)
    if credits >= 1:
        custom_api = db.get_val(token, "custom_api_url", "")
        encoded_msg = urllib.parse.quote_plus(msg)
        
        if custom_api:
            api_url = custom_api.replace("[NUMBER]", num).replace("[MSG]", encoded_msg)
        else:
            api_url = f"https://demosoftpp.com/SMS/api.php?key=EMONX4&number={num}&msg={encoded_msg}"
        
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(api_url, timeout=10) as response:
                    res_content = await response.text()
                    is_success = False
                    
                    try:
                        res_json = json.loads(res_content)
                        if "response" in res_json and isinstance(res_json["response"], list) and len(res_json["response"]) > 0:
                            if res_json["response"][0].get("status") == 0:
                                is_success = True
                    except json.JSONDecodeError:
                        pass
                        
                    if not is_success and response.status == 200:
                        res_lower = res_content.lower()
                        if not any(x in res_lower for x in ["error", "fail", "invalid", "limit", "insufficient", "wrong", "missing", "bad"]):
                            is_success = True

                    if is_success:
                        db.set_val(token, f"credits_{user_id}", credits - 1)
                        if credits - 1 <= 0:
                            db.set_val(token, f"expiry_time_{user_id}", 0)
                        
                        g_used = db.get_val(token, "used_global_tokens", 0)
                        db.set_val(token, "used_global_tokens", g_used + 1)
                        
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"⏰ *আপনার শিডিউল করা মেসেজটি সফলভাবে পাঠানো হয়েছে!* (`{num}`)\n১ ক্রেডিট কাটা হয়েছে।",
                            parse_mode=ParseMode.MARKDOWN
                        )
                    else:
                        await context.bot.send_message(
                            chat_id=user_id,
                            text=f"❌ *শিডিউল SMS ফেইল্ড!* সার্ভার থেকে সঠিক রেসপন্স পাওয়া যায়নি।"
                        )
            except Exception:
                await context.bot.send_message(chat_id=user_id, text="❌ *শিডিউল SMS ফেইল্ড!* কানেকশন এরর।")
    else:
        await context.bot.send_message(
            chat_id=user_id,
            text="❌ *শিডিউল SMS ফেইল্ড!* আপনার একাউন্টে পর্যাপ্ত ক্রেডিট ছিল ছিল না।"
        )

async def schedule_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = context.bot.token
    user_id = str(update.effective_user.id)
    args = context.args

    if len(args) < 3:
        await update.message.reply_text(
            "⚠️ *ব্যবহার বিধি:* `/schedule [নাম্বার] [মিনিট] [মেসেজ]`\n"
            "উদাহরণ: `/schedule 017XXXXXX 60 Happy Birthday`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    target_num = args[0]
    mins = int(args[1])
    text_content = " ".join(args[2:])

    if len(target_num) < 11 or not target_num.isdigit():
        await update.message.reply_text("❌ সঠিক নাম্বার দিন।")
        return

    if mins < 1:
        await update.message.reply_text("❌ সঠিক সময় (মিনিট) দিন।")
        return

    credits = db.get_val(token, f"credits_{user_id}", 0)
    if credits < 1:
        await update.message.reply_text("❌ আপনার পর্যাপ্ত টোকেন নেই।")
        return

    context.job_queue.run_once(
        scheduled_worker, 
        when=mins * 60, 
        data={
            "token": token,
            "uid": user_id,
            "num": target_num,
            "msg": text_content
        }
    )
    await update.message.reply_text(f"⏰ *আপনার SMS টি {mins} মিনিট পর পাঠানো হবে।* টোকেন তখন কাটা হবে।")


async def boot_clone_instance(token):
    if token == MASTER_BOT_TOKEN:
        return False, "এটি মাস্টার বটের টোকেন!"
    if token in active_clones:
        return False, "বটটি ইতিমধ্যে ব্যাকগ্রাউন্ডে সচল আছে।"
    
    try:
        app = Application.builder().token(token).build()
        
        app.add_handler(CommandHandler("start", start_command))
        app.add_handler(CommandHandler("schedule", schedule_command))
        app.add_handler(CallbackQueryHandler(inline_callback_router))

        # Changed to unified message handler
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_message_handler))

        await app.initialize()
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True)

        active_clones[token] = app
        return True, app.bot.username
    except InvalidToken:
        return False, "ভুল বা অকার্যকর বটের টোকেন!"
    except TelegramError as te:
        return False, f"টেলিগ্রাম এপিআই এরর: {te}"
    except Exception as e:
        return False, f"স্টার্টআপ এরর: {e}"


async def start_master_engine():
    logger.info("Initializing Master Clone SMS Engine...")
    
    app = Application.builder().token(MASTER_BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("schedule", schedule_command))
    app.add_handler(CallbackQueryHandler(inline_callback_router))
    
    # Changed to unified message handler
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unified_message_handler))

    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
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


