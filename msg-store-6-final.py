import logging
import json
import os
import re
import asyncio
from typing import Dict, List, Optional
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, Message
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    CallbackContext,
    ContextTypes
)

# Configuration
TOKEN = "" #add your bot token
DATA_DIR = "message-store"
os.makedirs(DATA_DIR, exist_ok=True)

# File paths
MESSAGE_STORE_PATH = os.path.join(DATA_DIR, "message_store.json")
MESSAGE_BATCH_PATH = os.path.join(DATA_DIR, "message_batch.json")
STATS_PATH = os.path.join(DATA_DIR, "stats.json")
BATCHES_PATH = os.path.join(DATA_DIR, "batches.json")

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

class FileManager:
    @staticmethod
    def load_data(filepath: str, default=None):
        if default is None:
            default = {}
        try:
            if os.path.exists(filepath):
                with open(filepath, "r") as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Error loading {filepath}: {e}")
        return default

    @staticmethod
    def save_data(filepath: str, data):
        try:
            with open(filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving {filepath}: {e}")

class BotDatabase:
    def __init__(self):
        self._cache = {}
        self._cache_timeout = 300  # 5 minutes
        self._last_save = {}
        self.message_store = FileManager.load_data(MESSAGE_STORE_PATH)
        self.message_batch = FileManager.load_data(MESSAGE_BATCH_PATH)
        self.stats = FileManager.load_data(STATS_PATH, {
            "views": {},
            "users": {},
            "batch_views": {},
            "message_types": {
                "text": 0,
                "photo": 0,
                "video": 0,
                "document": 0,
                "audio": 0,
                "voice": 0,
                "sticker": 0,
                "animation": 0
            }
        })
        self.batches = FileManager.load_data(BATCHES_PATH)
        self.subscriptions = FileManager.load_data(os.path.join(DATA_DIR, "subscriptions.json"), {})
        self.user_profiles = FileManager.load_data(os.path.join(DATA_DIR, "user_profiles.json"), {})

    def _should_save(self, filepath: str) -> bool:
        current_time = datetime.now().timestamp()
        if filepath not in self._last_save:
            self._last_save[filepath] = current_time
            return True
        if current_time - self._last_save[filepath] > 5:  # Save if more than 5 seconds passed
            self._last_save[filepath] = current_time
            return True
        return False

    def save_all(self):
        if self._should_save(MESSAGE_STORE_PATH):
            FileManager.save_data(MESSAGE_STORE_PATH, self.message_store)
        if self._should_save(MESSAGE_BATCH_PATH):
            FileManager.save_data(MESSAGE_BATCH_PATH, self.message_batch)
        if self._should_save(STATS_PATH):
            FileManager.save_data(STATS_PATH, self.stats)
        if self._should_save(BATCHES_PATH):
            FileManager.save_data(BATCHES_PATH, self.batches)
        if self._should_save(os.path.join(DATA_DIR, "subscriptions.json")):
            FileManager.save_data(os.path.join(DATA_DIR, "subscriptions.json"), self.subscriptions)
        if self._should_save(os.path.join(DATA_DIR, "user_profiles.json")):
            FileManager.save_data(os.path.join(DATA_DIR, "user_profiles.json"), self.user_profiles)

    def get_cached(self, key: str, filepath: str):
        current_time = datetime.now().timestamp()
        if key in self._cache:
            timestamp, value = self._cache[key]
            if current_time - timestamp < self._cache_timeout:
                return value
        return None

    def set_cached(self, key: str, value):
        self._cache[key] = (datetime.now().timestamp(), value)

    def get_batch_key(self, batch_name: str) -> str:
        """Get the actual batch key from the database, case-insensitive"""
        batch_name = batch_name.lower()
        for key in self.batches:
            if key.lower() == batch_name:
                return key
        return batch_name

    def is_subscribed(self, user_id: int, batch_name: str) -> bool:
        """Check if a user is subscribed to a batch"""
        return str(user_id) in self.subscriptions.get(batch_name, {})

    def subscribe(self, user_id: int, batch_name: str):
        """Subscribe a user to a batch"""
        if batch_name not in self.subscriptions:
            self.subscriptions[batch_name] = {}
        self.subscriptions[batch_name][str(user_id)] = datetime.now().isoformat()
        self.save_all()

    def unsubscribe(self, user_id: int, batch_name: str):
        """Unsubscribe a user from a batch"""
        if batch_name in self.subscriptions and str(user_id) in self.subscriptions[batch_name]:
            del self.subscriptions[batch_name][str(user_id)]
            if not self.subscriptions[batch_name]:
                del self.subscriptions[batch_name]
            self.save_all()

    def get_subscribers(self, batch_name: str) -> List[int]:
        """Get list of user IDs subscribed to a batch"""
        return [int(uid) for uid in self.subscriptions.get(batch_name, {}).keys()]

    def update_user_profile(self, user_id: int, username: str, first_name: str):
        """Update or create user profile"""
        self.user_profiles[str(user_id)] = {
            "username": username,
            "first_name": first_name,
            "last_updated": datetime.now().isoformat()
        }
        self.save_all()

    def get_user_profile(self, user_id: int) -> dict:
        """Get user profile"""
        return self.user_profiles.get(str(user_id), {})

    def get_user_subscriptions(self, user_id: int) -> List[str]:
        """Get list of batch names the user is subscribed to"""
        subscribed_batches = []
        for batch_name, subscribers in self.subscriptions.items():
            if str(user_id) in subscribers:
                subscribed_batches.append(batch_name)
        return subscribed_batches

db = BotDatabase()

# ======================
# Core Bot Functionality
# ======================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Check if this is a shared batch link
    if context.args and context.args[0].startswith("batch_"):
        share_token = context.args[0]
        # Extract batch name and sharer info from token (format: batch_name_sharer_id_timestamp)
        parts = share_token.split("_")
        if len(parts) >= 3:
            batch_name = "_".join(parts[1:-2])  # Handle batch names that might contain underscores
            sharer_id = parts[-2]
            
            if batch_name in db.batches:
                batch = db.batches[batch_name]
                
                # Get sharer info
                sharer_info = None
                if "share_tokens" in batch and share_token in batch["share_tokens"]:
                    sharer_info = batch["share_tokens"][share_token]
                
                # Show batch info with sharer information
                context.args = [batch_name]
                
                # If this is a shared link, show who shared it
                if sharer_info:
                    sharer_name = sharer_info.get("sharer_name", "Someone")
                    shared_at = datetime.fromisoformat(sharer_info["shared_at"]).strftime("%B %d, %Y at %I:%M %p")
                    
                    await update.message.reply_text(
                        f"🔗 <b>Shared Batch</b>\n\n"
                        f"This batch was shared with you by <b>{sharer_name}</b>\n"
                        f"Shared on: {shared_at}\n\n"
                        f"Click below to view the batch contents:",
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("📱 View Batch", callback_data=f"batch_{batch_name}")
                        ]])
                    )
                
                return await batch_info(update, context)
            else:
                await update.message.reply_text(
                    "❌ The shared batch no longer exists or has been deleted.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Start", callback_data="cmd_start")
                    ]])
                )
                return

    # Regular start command
    keyboard = [[InlineKeyboardButton("📚 Help", callback_data="cmd_help")]]
    
    await update.message.reply_text(
        "🎓 <b>Welcome to Premium Batch Bot</b>\n"
        "🤝 Powered by <b>NEELAXMI × CBSEIANSS</b>\n\n"
        "Your all-in-one platform to access structured, high-quality educational content:\n\n"
        "📦 <b>What You Get:</b>\n"
        "• 🎥 Premium Video Lectures – Delivered in perfect sequence\n"
        "• 📁 Study Files, Notes & Documents – Instantly downloadable\n"
        "• 🗂️ Organized Course Batches – Sorted and serial-wise\n"
        "• 🔍 Smart Search – Quickly find topics or lessons\n"
        "• 📊 Track views, top users, and manage batches with ease\n\n"
        "🧠 <b>Mastermind Behind This:</b> <b>Saksham</b> & <b>Tanmay</b>\n"
        "🚀 Let's upgrade your learning experience — the premium way.\n\n"
        "Use /help to see available commands.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = """
📚 <b>Main Commands</b>:
/search - Search for files (add keyword after command)
/search_batch - Search for batches (add keyword after command)
/search_teacher - Search for batches by teacher name
/search_date - Search content in a batch by date
/userstats - Top active users

📦 <b>Batch Commands</b>:
/createbatch - Create new batch (add name and optional description)
/addtobatch - Add new contents to batch (add batch name)
/listbatches - List all batches
/batchinfo - Get batch details (add batch name)
/editbatch - Edit batch description (add name and new description)
/done - Finish adding to batch

💡 <b>Batch Management</b>:
• Use /batchinfo to view batch details
• Click "Edit Description" to modify batch info
• Click "Delete Batch" to remove a batch
• Only batch creators can edit or delete their batches

📝 <b>Supported Message Types</b>:
• Text Messages
• Photos & Videos
• Documents & Files
• Voice Messages
• Stickers & Animations
"""
    await update.message.reply_text(help_text, parse_mode="HTML")

# ==================
# Message Management
# ==================

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    user = message.from_user

    # Update user profile
    db.update_user_profile(user.id, user.username, user.first_name)

    # Show typing indicator
    await context.bot.send_chat_action(chat_id=message.chat_id, action="typing")

    # Handle date search input
    if 'date_search_batch' in context.user_data:
        batch_name = context.user_data.pop('date_search_batch')
        date_str = message.text.strip()

        # Validate date format
        try:
            search_date = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return await message.reply_text(
                "❌ Invalid date format!\n"
                "Please use YYYY-MM-DD format.\n"
                "Example: 2024-03-20\n\n"
                "Try again or use /search_date to start over."
            )

        # Get batch messages
        batch = db.batches[batch_name]
        messages = batch.get("messages", [])
        
        # Filter messages by date
        date_matches = []
        for msg_key in messages:
            if msg_key in db.message_batch:
                msg = db.message_batch[msg_key]
                msg_date = datetime.fromisoformat(msg["date"]).date()
                if msg_date == search_date:
                    date_matches.append((msg_key, msg))

        if not date_matches:
            return await message.reply_text(
                f"ℹ️ No messages found in batch '{batch_name}' for date {date_str}.\n"
                "Try a different date or use /batchinfo to see all messages.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Try Another Date", callback_data=f"search_date_{batch_name}"),
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        # Build message
        msg = f"📅 <b>Messages from {date_str} in batch '{batch_name}'</b>\n\n"
        msg += f"Found {len(date_matches)} messages:\n\n"

        # Build keyboard
        keyboard = []
        for msg_key, message_data in date_matches:
            preview = message_data.get("text", message_data.get("caption", f"[{message_data['type'].upper()}]"))[:30]
            keyboard.append([
                InlineKeyboardButton(
                    f"{message_data['type'].upper()}: {preview}...",
                    callback_data=f"msg_{msg_key}"
                )
            ])

        # Add navigation buttons
        keyboard.append([
            InlineKeyboardButton("🔄 Search Another Date", callback_data=f"search_date_{batch_name}"),
            InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{batch_name}")
        ])
        keyboard.append([InlineKeyboardButton("📋 Back to Batches", callback_data="cmd_listbatches")])

        await message.reply_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Handle banner picture setting
    if 'setting_banner' in context.user_data:
        batch_name = context.user_data.pop('setting_banner')
        if batch_name not in db.batches:
            return await message.reply_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            return await message.reply_text(
                "❌ Only the batch creator can set the banner!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        if not message.photo:
            return await message.reply_text(
                "❌ Please send a photo to set as banner!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"back_to_batch_{batch_name}")
                ]])
            )

        # Get the highest quality photo
        photo = message.photo[-1]
        batch["banner_pic"] = photo.file_id
        db.save_all()

        await message.reply_text(
            f"✅ Banner picture set for batch '{batch_name}'!",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📱 View Batch Info", callback_data=f"back_to_batch_{batch_name}")
            ]])
        )
        return

    # Handle batch description editing
    if 'editing_batch' in context.user_data:
        batch_name = context.user_data.pop('editing_batch')
        if batch_name not in db.batches:
            return await message.reply_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            return await message.reply_text(
                "❌ Only the batch creator can edit it!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        new_description = message.text.strip()
        if not new_description:
            return await message.reply_text(
                "❌ Description cannot be empty! Please try again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"batch_{batch_name}")
                ]])
            )

        batch["description"] = new_description
        db.save_all()

        await message.reply_text(
            f"✅ Batch '{batch_name}' updated!\n"
            f"New description: {new_description}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{batch_name}")
            ]])
        )
        return

    # Handle teacher name editing
    if 'editing_teacher' in context.user_data:
        batch_name = context.user_data.pop('editing_teacher')
        if batch_name not in db.batches:
            return await message.reply_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            return await message.reply_text(
                "❌ Only the batch creator can edit it!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        new_teacher = message.text.strip()
        if not new_teacher:
            return await message.reply_text(
                "❌ Teacher name cannot be empty! Please try again.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"batch_{batch_name}")
                ]])
            )

        batch["teacher_name"] = new_teacher
        db.save_all()

        await message.reply_text(
            f"✅ Batch '{batch_name}' updated!\n"
            f"New teacher: {new_teacher}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{batch_name}")
            ]])
        )
        return

    # Handle message storage
    if 'current_batch' in context.user_data:
        batch_name = context.user_data['current_batch']
        await _add_to_batch(message, batch_name, context)
    else:
        await _store_message(message)

async def _store_message(message: Message):
    message_key = f"msg_{int(datetime.now().timestamp())}"
    message_data = await _extract_message_data(message)
    
    if not message_data:
        return

    # Store message data
    db.message_store[message_key] = message_data
    db.stats["message_types"][message_data["type"]] = db.stats["message_types"].get(message_data["type"], 0) + 1
    
    # Save in background
    asyncio.create_task(_save_db_async())
    
    # Send response immediately
    await message.reply_text(
        f"✅ Message saved successfully!\n"
        f"Type: {message_data['type'].upper()}\n"
        f"Content: {message_data.get('text', 'Media content')[:50]}..."
    )

async def _add_to_batch(message: Message, batch_name: str, context: ContextTypes.DEFAULT_TYPE = None):
    if batch_name not in db.batches:
        await message.reply_text("❌ Batch no longer exists!")
        return

    message_key = f"msg_{int(datetime.now().timestamp())}"
    message_data = await _extract_message_data(message)
    
    if not message_data:
        return

    # Store message data
    db.message_batch[message_key] = {
        **message_data,
        "batch": batch_name
    }

    if "messages" not in db.batches[batch_name]:
        db.batches[batch_name]["messages"] = []
    db.batches[batch_name]["messages"].append(message_key)
    
    # Update last_updated timestamp
    db.batches[batch_name]["last_updated"] = datetime.now().isoformat()

    db.stats["message_types"][message_data["type"]] += 1
    
    # Save in background
    asyncio.create_task(_save_db_async())
    
    # Notify subscribers
    subscribers = db.get_subscribers(batch_name)
    if subscribers and context:
        # Send notifications to all subscribers
        for user_id in subscribers:
            try:
                # Send the actual message based on its type
                if message_data["type"] == "text":
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🔔 <b>New message in batch '{batch_name}'</b>\n\n{message_data['text']}",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "photo":
                    await context.bot.send_photo(
                        chat_id=user_id,
                        photo=message_data["file_id"],
                        caption=f"🔔 <b>New photo in batch '{batch_name}'</b>\n\n{message_data.get('caption', '')}",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "video":
                    await context.bot.send_video(
                        chat_id=user_id,
                        video=message_data["file_id"],
                        caption=f"🔔 <b>New video in batch '{batch_name}'</b>\n\n{message_data.get('caption', '')}",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "document":
                    await context.bot.send_document(
                        chat_id=user_id,
                        document=message_data["file_id"],
                        caption=f"🔔 <b>New document in batch '{batch_name}'</b>\n\n{message_data.get('caption', '')}",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "voice":
                    await context.bot.send_voice(
                        chat_id=user_id,
                        voice=message_data["file_id"],
                        caption=f"🔔 <b>New voice message in batch '{batch_name}'</b>",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "audio":
                    await context.bot.send_audio(
                        chat_id=user_id,
                        audio=message_data["file_id"],
                        caption=f"🔔 <b>New audio in batch '{batch_name}'</b>\n\n{message_data.get('title', '')}",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "sticker":
                    await context.bot.send_sticker(
                        chat_id=user_id,
                        sticker=message_data["file_id"]
                    )
                    await context.bot.send_message(
                        chat_id=user_id,
                        text=f"🔔 <b>New sticker in batch '{batch_name}'</b>",
                        parse_mode="HTML"
                    )
                elif message_data["type"] == "animation":
                    await context.bot.send_animation(
                        chat_id=user_id,
                        animation=message_data["file_id"],
                        caption=f"🔔 <b>New animation in batch '{batch_name}'</b>\n\n{message_data.get('caption', '')}",
                        parse_mode="HTML"
                    )
            except Exception as e:
                logger.error(f"Error sending notification to user {user_id}: {e}")
    
    # Send response immediately
    await message.reply_text(
        f"✅ Added message to batch '{batch_name}'!\n"
        f"Type: {message_data['type'].upper()}\n"
        f"Content: {message_data.get('text', 'Media content')[:50]}...\n\n"
        "Send more messages or /done when finished."
    )

async def _extract_message_data(message: Message) -> Optional[Dict]:
    user = message.from_user
    message_data = {
        "type": "text",
        "user_id": user.id,
        "username": user.username,
        "first_name": user.first_name,
        "date": datetime.now().isoformat()
    }

    # Handle text messages
    if message.text:
        message_data["text"] = message.text
        return message_data

    # Handle photos
    if message.photo:
        message_data["type"] = "photo"
        message_data["file_id"] = message.photo[-1].file_id
        message_data["caption"] = message.caption
        return message_data

    # Handle videos
    if message.video:
        message_data["type"] = "video"
        message_data["file_id"] = message.video.file_id
        message_data["caption"] = message.caption
        return message_data

    # Handle documents
    if message.document:
        message_data["type"] = "document"
        message_data["file_id"] = message.document.file_id
        message_data["file_name"] = message.document.file_name
        message_data["caption"] = message.caption
        return message_data

    # Handle voice messages
    if message.voice:
        message_data["type"] = "voice"
        message_data["file_id"] = message.voice.file_id
        return message_data

    # Handle audio
    if message.audio:
        message_data["type"] = "audio"
        message_data["file_id"] = message.audio.file_id
        message_data["title"] = message.audio.title
        return message_data

    # Handle stickers
    if message.sticker:
        message_data["type"] = "sticker"
        message_data["file_id"] = message.sticker.file_id
        return message_data

    # Handle animations
    if message.animation:
        message_data["type"] = "animation"
        message_data["file_id"] = message.animation.file_id
        message_data["caption"] = message.caption
        return message_data

    return None

async def _save_db_async():
    """Save database changes asynchronously"""
    try:
        db.save_all()
    except Exception as e:
        logger.error(f"Error saving database: {e}")

# ==================
# Batch Management
# ==================

async def create_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not context.args:
            return await update.message.reply_text(
                "ℹ️ Usage: /createbatch <name> <teacher_name> [description]\n"
                "Example: /createbatch Math101 JohnSmith Math class notes\n\n"
                "After creating the batch, you can add a banner picture by sending an image with caption /setbanner <batch_name>"
            )

        args = context.args
        if len(args) < 2:
            return await update.message.reply_text(
                "❌ Error: Both batch name and teacher name are required!\n\n"
                "Usage: /createbatch <name> <teacher_name> [description]\n"
                "Example: /createbatch Math101 JohnSmith Math class notes"
            )

        batch_name = args[0].strip()
        teacher_name = args[1].strip()
        
        # Validate batch name and teacher name
        if not batch_name or not teacher_name:
            return await update.message.reply_text(
                "❌ Error: Batch name and teacher name cannot be empty!\n\n"
                "Usage: /createbatch <name> <teacher_name> [description]\n"
                "Example: /createbatch Math101 JohnSmith Math class notes"
            )

        description = " ".join(args[2:]).strip() if len(args) > 2 else ""

        # Check if batch exists (case-insensitive)
        existing_batch = db.get_batch_key(batch_name)
        if existing_batch in db.batches:
            return await update.message.reply_text(
                f"⚠️ Batch '{existing_batch}' already exists!\n"
                "Please use a different batch name."
            )

        # Create the batch
        db.batches[batch_name] = {
            "description": description,
            "teacher_name": teacher_name,
            "created_by": update.message.from_user.id,
            "created_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),  # Add last_updated field
            "messages": [],
            "banner_pic": None,  # Add banner picture field
            "message_types": {
                "text": 0,
                "photo": 0,
                "video": 0,
                "document": 0,
                "audio": 0,
                "voice": 0,
                "sticker": 0,
                "animation": 0
            }
        }
        db.save_all()

        await update.message.reply_text(
            f"✅ Message batch '{batch_name}' created!\n"
            f"Teacher: {teacher_name}\n"
            f"Description: {description or 'None'}\n\n"
            "Use /addtobatch to add messages.\n"
            "To add a banner picture, send an image with caption /setbanner {batch_name}"
        )

    except Exception as e:
        logger.error(f"Error creating batch: {e}")
        await update.message.reply_text(
            "❌ An error occurred while creating the batch.\n"
            "Please try again with the correct format:\n\n"
            "Usage: /createbatch <name> <teacher_name> [description]\n"
            "Example: /createbatch Math101 JohnSmith Math class notes"
        )

async def set_banner(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.photo:
        return await update.message.reply_text(
            "❌ Please send a photo with the caption /setbanner <batch_name>"
        )

    if not context.args:
        return await update.message.reply_text(
            "❌ Please specify the batch name in the caption.\n"
            "Example: Send a photo with caption '/setbanner Math101'"
        )

    batch_name = " ".join(context.args)
    actual_batch = db.get_batch_key(batch_name)
    
    if actual_batch not in db.batches:
        return await update.message.reply_text("❌ Batch not found!")

    batch = db.batches[actual_batch]
    if update.message.from_user.id != batch["created_by"]:
        return await update.message.reply_text("❌ Only the batch creator can set the banner!")

    # Get the highest quality photo
    photo = update.message.photo[-1]
    batch["banner_pic"] = photo.file_id
    db.save_all()

    await update.message.reply_text(
        f"✅ Banner picture set for batch '{actual_batch}'!",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📱 View Batch Info", callback_data=f"back_to_batch_{actual_batch}")
        ]])
    )

async def add_to_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("ℹ️ Usage: /addtobatch <name>")

    batch_name = " ".join(context.args)
    actual_batch = db.get_batch_key(batch_name)
    if actual_batch not in db.batches:
        return await update.message.reply_text("❌ Batch doesn't exist!")

    context.user_data['current_batch'] = actual_batch
    await update.message.reply_text(
        f"🔄 Ready to add messages to '{actual_batch}'.\n"
        "Send me messages now or /done when finished."
    )

async def done_adding(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if 'current_batch' not in context.user_data:
        return await update.message.reply_text("⚠️ Not currently adding to any batch!")

    batch_name = context.user_data.pop('current_batch')
    count = len(db.batches[batch_name]["messages"])
    await update.message.reply_text(
        f"✅ Finished adding to '{batch_name}'!\n"
        f"Total messages: {count}\n"
        f"Use /batchinfo {batch_name} for details."
    )

async def batch_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        # Get the message object - either from direct command or callback query
        message = update.message or update.callback_query.message
        if not message:
            logger.error("No message object found in update")
            return

        # Get batch name from args or callback data
        batch_name = None
        if context.args:
            batch_name = " ".join(context.args)
        elif update.callback_query and update.callback_query.data.startswith("back_to_batch_"):
            batch_name = update.callback_query.data[13:].lstrip('_')

        if not batch_name:
            if update.message:
                await update.message.reply_text(
                    "ℹ️ Usage: /batchinfo <name>\n"
                    "Example: /batchinfo Math101"
                )
            return

        actual_batch = db.get_batch_key(batch_name)
        if actual_batch not in db.batches:
            # Try to find similar batch names
            similar_batches = [
                name for name in db.batches
                if name.lower().startswith(batch_name.lower()) or
                   batch_name.lower() in name.lower()
            ][:5]

            msg = f"❌ Batch '{batch_name}' not found!"
            if similar_batches:
                msg += "\n\nDid you mean:\n" + "\n".join(f"• {name}" for name in similar_batches)
            
            if update.message:
                await update.message.reply_text(msg)
            else:
                await update.callback_query.edit_message_text(msg)
            return

        batch = db.batches[actual_batch]
        
        try:
            creator = await context.bot.get_chat(batch["created_by"])
        except Exception as e:
            logger.error(f"Error getting creator info: {e}")
            creator = None

        # Count message types
        message_types = {"text": 0, "photo": 0, "video": 0, "document": 0, 
                        "audio": 0, "voice": 0, "sticker": 0, "animation": 0}
        for msg_key in batch["messages"]:
            if msg_key in db.message_batch:
                msg_type = db.message_batch[msg_key].get("type", "text")
                if msg_type in message_types:
                    message_types[msg_type] += 1

        # Format dates with better error handling
        formatted_date = "Unknown"
        formatted_last_updated = "Unknown"

        try:
            # Format creation date
            if "created_at" in batch and batch["created_at"]:
                created_at = datetime.fromisoformat(batch['created_at'])
                formatted_date = created_at.strftime("%B %d, %Y at %I:%M %p")
            else:
                # If created_at is missing, try to get it from the first message
                if batch["messages"]:
                    first_msg_key = batch["messages"][0]
                    if first_msg_key in db.message_batch:
                        first_msg = db.message_batch[first_msg_key]
                        created_at = datetime.fromisoformat(first_msg["date"])
                        formatted_date = created_at.strftime("%B %d, %Y at %I:%M %p")
                        # Update the batch with the correct creation date
                        batch["created_at"] = first_msg["date"]
                        db.save_all()
        except Exception as e:
            logger.error(f"Error formatting creation date: {e}")

        try:
            # Format last updated date
            if "last_updated" in batch and batch["last_updated"]:
                last_updated = datetime.fromisoformat(batch['last_updated'])
                formatted_last_updated = last_updated.strftime("%B %d, %Y at %I:%M %p")
            else:
                # If last_updated is missing, try to get it from the last message
                if batch["messages"]:
                    last_msg_key = batch["messages"][-1]
                    if last_msg_key in db.message_batch:
                        last_msg = db.message_batch[last_msg_key]
                        last_updated = datetime.fromisoformat(last_msg["date"])
                        formatted_last_updated = last_updated.strftime("%B %d, %Y at %I:%M %p")
                        # Update the batch with the correct last_updated date
                        batch["last_updated"] = last_msg["date"]
                        db.save_all()
        except Exception as e:
            logger.error(f"Error formatting last updated date: {e}")

        # Build message
        msg = f"📱 <b>Batch Information</b>\n\n"
        msg += f"📚 <b>Name:</b> {actual_batch}\n"
        msg += f"👨‍🏫 <b>Teacher:</b> {batch.get('teacher_name', 'Not specified')}\n"
        msg += f"📝 <b>Description:</b> {batch.get('description', 'No description')}\n\n"
        msg += f"👤 <b>Created by:</b> {creator.first_name if creator else 'Unknown'}\n"
        msg += f"📅 <b>Created on:</b> {formatted_date}\n"
        msg += f"🔄 <b>Last Updated:</b> {formatted_last_updated}\n"
        msg += f"📨 <b>Total Messages:</b> {len(batch['messages'])}\n"
        msg += f"👁️ <b>Views:</b> {db.stats['batch_views'].get(actual_batch, 0)}\n\n"
        
        # Add message types if there are any messages
        if any(count > 0 for count in message_types.values()):
            msg += f"📊 <b>Message Types:</b>\n"
            for msg_type, count in message_types.items():
                if count > 0:
                    msg += f"• {msg_type.title()}: {count}\n"

        # Build keyboard
        keyboard = []
        
        # Add view messages button
        if batch["messages"]:
            keyboard.append([InlineKeyboardButton("📱 View Messages", callback_data=f"batch_{actual_batch}")])
        
        # Add share button
        keyboard.append([InlineKeyboardButton("🔗 Share Batch", callback_data=f"share_{actual_batch}")])
        
        # Add edit buttons if user is the creator
        if message.from_user.id == batch["created_by"]:
            keyboard.extend([
                [InlineKeyboardButton("✏️ Edit Description", callback_data=f"edit_desc_{actual_batch}")],
                [InlineKeyboardButton("👨‍🏫 Edit Teacher", callback_data=f"edit_teacher_{actual_batch}")],
                [InlineKeyboardButton("🖼️ Set Banner", callback_data=f"set_banner_{actual_batch}")],
                [InlineKeyboardButton("🗑️ Delete Batch", callback_data=f"delete_batch_{actual_batch}")]
            ])

        # Add navigation buttons
        keyboard.append([InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")])

        try:
            # If there's a banner picture, send it as a new message
            if batch.get("banner_pic"):
                if update.message:
                    await update.message.reply_photo(
                        batch["banner_pic"],
                        caption=msg,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await update.callback_query.message.reply_photo(
                        batch["banner_pic"],
                        caption=msg,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                    # Delete the old message
                    await update.callback_query.message.delete()
            else:
                if update.message:
                    await update.message.reply_text(
                        msg, 
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
                else:
                    await update.callback_query.edit_message_text(
                        msg,
                        parse_mode="HTML",
                        reply_markup=InlineKeyboardMarkup(keyboard)
                    )
        except Exception as e:
            logger.error(f"Error sending/editing message: {e}")
            # Fallback to simple text message if photo fails
            if update.message:
                await update.message.reply_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                await update.callback_query.edit_message_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )

    except Exception as e:
        logger.error(f"Error in batch_info: {e}")
        error_msg = "❌ An error occurred while fetching batch information.\nPlease try again or contact support if the issue persists."
        if update.message:
            await update.message.reply_text(error_msg)
        elif update.callback_query:
            try:
                await update.callback_query.edit_message_text(error_msg)
            except Exception as e:
                logger.error(f"Error editing message: {e}")
                await update.callback_query.message.reply_text(error_msg)

async def edit_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "ℹ️ Usage: /editbatch <name> <new description>\n"
            "Example: /editbatch MeetingNotes Updated meeting notes from today"
        )

    batch_name = context.args[0]
    actual_batch = db.get_batch_key(batch_name)
    new_description = " ".join(context.args[1:])

    if actual_batch not in db.batches:
        return await update.message.reply_text("❌ Batch not found!")

    batch = db.batches[actual_batch]
    if update.message.from_user.id != batch["created_by"]:
        return await update.message.reply_text("❌ Only the batch creator can edit it!")

    batch["description"] = new_description
    db.save_all()

    await update.message.reply_text(
        f"✅ Batch '{actual_batch}' updated!\n"
        f"New description: {new_description}"
    )

# ==================
# Message Delivery
# ==================

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data
    user = query.from_user

    # Handle search date batch selection
    if data.startswith("search_date_"):
        batch_name = data[12:]  # Remove "search_date_" prefix
        if batch_name not in db.batches:
            return await query.edit_message_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="cmd_help")
                ]])
            )

        # Store selected batch in user data
        context.user_data['date_search_batch'] = batch_name
        
        # Ask for date input
        await query.edit_message_text(
            f"📅 <b>Searching messages in batch '{batch_name}'</b>\n\n"
            "Please enter the date in YYYY-MM-DD format.\n"
            "Example: 2024-03-20\n\n"
            "Or click Cancel to go back.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="cmd_help")
            ]])
        )
        return

    # Handle help command
    if data == "cmd_help":
        help_text = """
📚 <b>Main Commands</b>:
/search - Search for messages (add keyword after command)
/search_batch - Search for message batches (add keyword after command)
/search_teacher - Search for batches by teacher name
/search_date - Search messages in a batch by date
/topmessages - Most viewed messages
/userstats - Top active users

📦 <b>Batch Commands</b>:
/createbatch - Create new message batch (add name and optional description)
/addtobatch - Add messages to batch (add batch name)
/listbatches - List all message batches
/batchinfo - Get batch details (add batch name)
/editbatch - Edit batch description (add name and new description)
/done - Finish adding to batch

💡 <b>Batch Management</b>:
• Use /batchinfo to view batch details
• Click "Edit Description" to modify batch info
• Click "Delete Batch" to remove a batch
• Only batch creators can edit or delete their batches

📝 <b>Supported Message Types</b>:
• Text Messages
• Photos & Videos
• Documents & Files
• Voice Messages
• Stickers & Animations
"""
        keyboard = [[InlineKeyboardButton("🔙 Back to Start", callback_data="cmd_start")]]
        await query.edit_message_text(help_text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    # Handle back to start
    if data == "cmd_start":
        keyboard = [[InlineKeyboardButton("📚 Help", callback_data="cmd_help")]]
        await query.edit_message_text(
            "🎓 <b>Welcome to Premium Batch Bot</b>\n"
            "🤝 Powered by <b>NEELAXMI × CBSEIANSS</b>\n\n"
            "Your all-in-one platform to access structured, high-quality educational content:\n\n"
            "📦 <b>What You Get:</b>\n"
            "• 🎥 Premium Video Lectures – Delivered in perfect sequence\n"
            "• 📁 Study Files, Notes & Documents – Instantly downloadable\n"
            "• 🗂️ Organized Course Batches – Sorted and serial-wise\n"
            "• 🔍 Smart Search – Quickly find topics or lessons\n"
            "• 📊 Track views, top users, and manage batches with ease\n\n"
            "🧠 <b>Mastermind Behind This:</b> <b>Saksham</b> & <b>Tanmay</b>\n"
            "🚀 Let's upgrade your learning experience — the premium way.\n\n"
            "Use /help to see available commands.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return

    # Handle pagination
    if data.startswith("page_"):
        try:
            # Format: page_batchname_pagenumber
            parts = data.split("_")
            batch_name = "_".join(parts[1:-1])  # Handle batch names that might contain underscores
            page = int(parts[-1])
            return await _show_batch_messages(query, batch_name, user, page)
        except Exception as e:
            logger.error(f"Error handling pagination: {e}")
            return await query.edit_message_text(
                "❌ Error navigating pages.\n"
                "Please try again or contact support if the issue persists.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

    # Handle batch selection
    if data.startswith("batch_"):
        batch_name = data[6:].lstrip('_')  # Remove "batch_" prefix and any leading underscore
        if data == "batch_info":  # Special case for back to batch info
            return await batch_info(update, context)
        return await _show_batch_messages(query, batch_name, user, 0)  # Start from page 0

    # Handle message view
    if data.startswith("msg_"):
        message_key = data[4:]
        return await _show_message(query, message_key, user)

    # Handle batch editing
    if data.startswith("edit_desc_"):
        batch_name = data[10:].lstrip('_')  # Remove "edit_desc_" prefix and any leading underscore
        if batch_name not in db.batches:
            try:
                await query.edit_message_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            try:
                await query.edit_message_text(
                    "❌ Only the batch creator can edit it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                await query.message.reply_text(
                    "❌ Only the batch creator can edit it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        context.user_data['editing_batch'] = batch_name
        try:
            await query.edit_message_text(
                f"✏️ Editing description for batch '{batch_name}'\n"
                f"Current description: {batch.get('description', 'No description')}\n\n"
                "Please send the new description in your next message.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"back_to_batch_{batch_name}")
                ]])
            )
        except Exception as e:
            await query.message.reply_text(
                f"✏️ Editing description for batch '{batch_name}'\n"
                f"Current description: {batch.get('description', 'No description')}\n\n"
                "Please send the new description in your next message.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"back_to_batch_{batch_name}")
                ]])
            )
        return

    # Handle teacher name editing
    if data.startswith("edit_teacher_"):
        batch_name = data[12:].lstrip('_')  # Remove "edit_teacher_" prefix and any leading underscore
        logger.info(f"Editing teacher for batch: {batch_name}")
        if batch_name not in db.batches:
            logger.error(f"Batch not found: {batch_name}")
            try:
                await query.edit_message_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                await query.message.reply_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            try:
                await query.edit_message_text(
                    "❌ Only the batch creator can edit it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                await query.message.reply_text(
                    "❌ Only the batch creator can edit it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        context.user_data['editing_teacher'] = batch_name
        try:
            await query.edit_message_text(
                f"👨‍🏫 Editing teacher name for batch '{batch_name}'\n"
                f"Current teacher: {batch.get('teacher_name', 'Not specified')}\n\n"
                "Please send the new teacher name in your next message.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"back_to_batch_{batch_name}")
                ]])
            )
        except Exception as e:
            await query.message.reply_text(
                f"👨‍🏫 Editing teacher name for batch '{batch_name}'\n"
                f"Current teacher: {batch.get('teacher_name', 'Not specified')}\n\n"
                "Please send the new teacher name in your next message.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Cancel", callback_data=f"back_to_batch_{batch_name}")
                ]])
            )
        return

    # Handle set banner button
    if data.startswith("set_banner_"):
        batch_name = data[11:].lstrip('_')  # Remove "set_banner_" prefix
        if batch_name not in db.batches:
            return await query.edit_message_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )
        
        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            return await query.edit_message_text(
                "❌ Only the batch creator can set the banner!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        context.user_data['setting_banner'] = batch_name
        await query.edit_message_text(
            f"🖼️ Please send a photo to set as banner for batch '{batch_name}'.\n"
            "The photo should be clear and representative of the batch content.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Cancel", callback_data=f"back_to_batch_{batch_name}")
            ]])
        )
        return

    # Handle subscription
    if data.startswith("sub_"):
        batch_name = data[4:]
        if batch_name in db.batches:
            db.subscribe(user.id, batch_name)
            await query.answer("✅ Subscribed to batch notifications!", show_alert=True)
            # Refresh the batch list
            return await list_batches(update, context)
        return

    # Handle unsubscription
    if data.startswith("unsub_"):
        batch_name = data[6:]
        if batch_name in db.batches:
            db.unsubscribe(user.id, batch_name)
            await query.answer("✅ Unsubscribed from batch notifications!", show_alert=True)
            # Refresh the batch list
            return await list_batches(update, context)
        return

    # Handle list batches command
    if data == "cmd_listbatches":
        return await list_batches(update, context)

    # Handle batch deletion confirmation
    if data.startswith("confirm_delete_"):
        batch_name = data[14:].lstrip('_')  # Remove "confirm_delete_" prefix and any leading underscore
        if batch_name not in db.batches:
            try:
                await query.edit_message_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        
        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            try:
                await query.edit_message_text(
                    "❌ Only the batch creator can delete it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Only the batch creator can delete it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return

        try:
            # Delete batch and its messages
            for msg_key in batch.get("messages", []):
                if msg_key in db.message_batch:
                    del db.message_batch[msg_key]
            del db.batches[batch_name]
            db.save_all()

            try:
                await query.edit_message_text(
                    f"✅ Batch '{batch_name}' has been deleted!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    f"✅ Batch '{batch_name}' has been deleted!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
        except Exception as e:
            logger.error(f"Error deleting batch: {e}")
            try:
                await query.edit_message_text(
                    "❌ Error deleting batch. Please try again.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Error deleting batch. Please try again.",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )

    # Handle batch deletion request
    if data.startswith("delete_batch_"):
        batch_name = data[12:].lstrip('_')  # Remove "delete_batch_" prefix and any leading underscore
        if batch_name not in db.batches:
            try:
                await query.edit_message_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        
        batch = db.batches[batch_name]
        if user.id != batch["created_by"]:
            try:
                await query.edit_message_text(
                    "❌ Only the batch creator can delete it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Only the batch creator can delete it!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return

        # Show confirmation dialog
        keyboard = [
            [
                InlineKeyboardButton("✅ Yes, Delete", callback_data=f"confirm_delete_{batch_name}"),
                InlineKeyboardButton("❌ No, Cancel", callback_data=f"back_to_batch_{batch_name}")
            ]
        ]
        
        try:
            await query.edit_message_text(
                f"⚠️ Are you sure you want to delete batch '{batch_name}'?\n"
                f"This will delete all messages in this batch and cannot be undone!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            # If editing fails (e.g., message has media), send a new message
            await query.message.reply_text(
                f"⚠️ Are you sure you want to delete batch '{batch_name}'?\n"
                f"This will delete all messages in this batch and cannot be undone!",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    # Handle back to batch info
    if data.startswith("back_to_batch_"):
        batch_name = data[13:].lstrip('_')  # Remove "back_to_batch_" prefix
        if batch_name not in db.batches:
            try:
                await query.edit_message_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await query.message.reply_text(
                    "❌ Batch no longer exists!",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                    ]])
                )
            return
        return await batch_info(update, context)
    
    # Handle profile command
    if data == "cmd_profile":
        # Update user profile
        db.update_user_profile(user.id, user.username, user.first_name)
        
        # Get user profile and subscriptions
        profile = db.get_user_profile(user.id)
        subscribed_batches = db.get_user_subscriptions(user.id)
        
        # Build profile message
        msg = f"👤 <b>User Profile</b>\n\n"
        msg += f"🆔 <b>User ID:</b> {user.id}\n"
        msg += f"👤 <b>Name:</b> {user.first_name}\n"
        if user.username:
            msg += f"📝 <b>Username:</b> @{user.username}\n"
        
        # Add subscription information
        if subscribed_batches:
            msg += f"\n📚 <b>Subscribed Batches ({len(subscribed_batches)}):</b>\n"
            for batch_name in subscribed_batches:
                if batch_name in db.batches:
                    batch = db.batches[batch_name]
                    msg += f"• {batch_name} ({len(batch['messages'])} messages)\n"
        else:
            msg += "\n📚 <b>No subscribed batches</b>\n"
            msg += "Use /listbatches to discover and subscribe to batches!"
        
        # Add stats if available
        user_views = db.stats["users"].get(str(user.id), 0)
        if user_views > 0:
            msg += f"\n📊 <b>Total Views:</b> {user_views}\n"
        
        # Create keyboard
        keyboard = [
            [InlineKeyboardButton("📚 View All Batches", callback_data="cmd_listbatches")],
            [InlineKeyboardButton("🔄 Refresh Profile", callback_data="cmd_profile")]
        ]
        
        try:
            await query.edit_message_text(
                msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            # If editing fails (e.g., message has media), send a new message
            await query.message.reply_text(
                msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            try:
                await query.message.delete()
            except Exception as e:
                logger.error(f"Error deleting old message: {e}")

    # Handle share button
    if data.startswith("share_"):
        batch_name = data[6:].lstrip('_')  # Remove "share_" prefix
        if batch_name not in db.batches:
            return await query.edit_message_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        # Generate a unique share token with sharer info
        sharer_id = query.from_user.id
        sharer_name = query.from_user.first_name
        share_token = f"batch_{batch_name}_{sharer_id}_{int(datetime.now().timestamp())}"

        # Store the share token and sharer info in the batch data
        if "share_tokens" not in db.batches[batch_name]:
            db.batches[batch_name]["share_tokens"] = {}
        
        db.batches[batch_name]["share_tokens"][share_token] = {
            "sharer_id": sharer_id,
            "sharer_name": sharer_name,
            "shared_at": datetime.now().isoformat()
        }
        db.save_all()

        # Create the share link
        share_link = f"https://t.me/{context.bot.username}?start={share_token}"

        # Get batch info
        batch = db.batches[batch_name]
        teacher = batch.get("teacher_name", "Not specified")
        msg_count = len(batch.get("messages", []))
        description = batch.get("description", "No description")

        # Create message with batch info and share link
        msg = f"🔗 <b>Share Batch</b>\n\n"
        msg += f"📚 <b>Batch:</b> {batch_name}\n"
        msg += f"👨‍🏫 <b>Teacher:</b> {teacher}\n"
        msg += f"📝 <b>Description:</b> {description}\n"
        msg += f"📨 <b>Messages:</b> {msg_count}\n\n"
        msg += f"🔗 <b>Share Link:</b>\n<code>{share_link}</code>\n\n"
        msg += "Click the link above to share this batch with others.\n"
        msg += "The link will give access to view all messages in this batch."

        # Create keyboard
        keyboard = [
            [InlineKeyboardButton("📱 View Batch", callback_data=f"batch_{batch_name}")],
            [InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{batch_name}")]
        ]

        # If there's a banner picture, send it with the share message
        if batch.get("banner_pic"):
            await query.message.reply_photo(
                batch["banner_pic"],
                caption=msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
            # Delete the old message
            await query.message.delete()
        else:
            await query.edit_message_text(
                msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        return

async def _show_batch_messages(query, batch_name: str, user, page: int = 0):
    try:
        if batch_name not in db.batches:
            return await query.edit_message_text(
                "❌ Batch no longer exists!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
                ]])
            )

        messages = db.batches[batch_name]["messages"]
        if not messages:
            return await query.edit_message_text(
                "ℹ️ This batch is empty.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{batch_name}")
                ]])
            )

        # Track batch view
        db.stats["batch_views"][batch_name] = db.stats["batch_views"].get(batch_name, 0) + 1
        db.save_all()

        # Get batch info for the header
        batch = db.batches[batch_name]
        
        # Format last updated date
        try:
            last_updated = datetime.fromisoformat(batch['last_updated'])
            formatted_last_updated = last_updated.strftime("%B %d, %Y at %I:%M %p")
        except Exception as e:
            logger.error(f"Error formatting last updated date: {e}")
            formatted_last_updated = "Unknown"

        # Get last message timestamp
        last_message_time = None
        try:
            if messages:
                last_msg_key = messages[-1]
                if last_msg_key in db.message_batch:
                    last_msg = db.message_batch[last_msg_key]
                    last_message_time = datetime.fromisoformat(last_msg["date"])
                    # Convert to IST (UTC+5:30)
                    last_message_time = last_message_time.replace(tzinfo=None) + timedelta(hours=5, minutes=30)
                    last_message_time = last_message_time.strftime("%B %d, %Y at %I:%M %p IST")
        except Exception as e:
            logger.error(f"Error getting last message time: {e}")

        header_msg = f"📱 <b>Messages in '{batch_name}'</b>\n"
        header_msg += f"👨‍🏫 Teacher: {batch.get('teacher_name', 'Not specified')}\n"
        header_msg += f"📝 Description: {batch.get('description', 'No description')}\n"
        # header_msg += f"🔄 Last Updated: {formatted_last_updated}\n"
        if last_message_time:
            header_msg += f"⏰ Last Updated: {last_message_time}\n"
        header_msg += f"📨 Total messages: {len(messages)}\n"

        # Pagination settings
        MESSAGES_PER_PAGE = 8
        total_pages = (len(messages) + MESSAGES_PER_PAGE - 1) // MESSAGES_PER_PAGE
        start_idx = page * MESSAGES_PER_PAGE
        end_idx = min(start_idx + MESSAGES_PER_PAGE, len(messages))
        
        # Add page info to header
        header_msg += f"📄 Page {page + 1} of {total_pages}\n"

        keyboard = []
        for msg_key in messages[start_idx:end_idx]:
            if msg_key in db.message_batch:
                msg = db.message_batch[msg_key]
                msg_type = msg.get("type", "unknown").upper()
                
                # Get preview text based on message type
                preview = ""
                if msg_type == "TEXT":
                    preview = str(msg.get("text", ""))[:30]
                elif msg_type in ["PHOTO", "VIDEO", "DOCUMENT", "ANIMATION"]:
                    caption = msg.get("caption")
                    preview = str(caption)[:30] if caption else f"[{msg_type}]"
                elif msg_type == "AUDIO":
                    title = msg.get("title")
                    preview = str(title)[:30] if title else f"[{msg_type}]"
                else:
                    preview = f"[{msg_type}]"
                
                # Add sender name to preview
                sender = msg.get("first_name", "Unknown")
                button_text = f"{msg_type}: {preview}... (by {sender})"
                keyboard.append([
                    InlineKeyboardButton(
                        button_text,
                        callback_data=f"msg_{msg_key}"
                    )
                ])

        # Add navigation buttons
        nav_buttons = []
        
        # Add page navigation if there are multiple pages
        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton("⬅️ Previous", callback_data=f"page_{batch_name}_{page-1}"))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"page_{batch_name}_{page+1}"))
            if nav_row:
                nav_buttons.append(nav_row)

        # Add back buttons
        nav_buttons.append([
            InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{batch_name}"),
            InlineKeyboardButton("📋 Back to Batches", callback_data="cmd_listbatches")
        ])

        keyboard.extend(nav_buttons)

        try:
            # If there's a banner picture, send it as a new message
            if batch.get("banner_pic"):
                await query.message.reply_photo(
                    batch["banner_pic"],
                    caption=header_msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                # Delete the old message
                await query.message.delete()
            else:
                await query.edit_message_text(
                    header_msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception as e:
            logger.error(f"Error sending/editing message: {e}")
            # Fallback to simple text message if photo fails
            await query.edit_message_text(
                header_msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception as e:
        logger.error(f"Error showing batch messages: {e}")
        await query.edit_message_text(
            "❌ An error occurred while fetching messages.\n"
            "Please try again or contact support if the issue persists.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")
            ]])
        )

async def _show_message(query, message_key: str, user):
    try:
        # Check both message stores
        if message_key in db.message_store:
            message_data = db.message_store[message_key]
        elif message_key in db.message_batch:
            message_data = db.message_batch[message_key]
        else:
            return await query.edit_message_text(
                "❌ Message no longer available!",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="cmd_listbatches")
                ]])
            )

        # Track view stats
        db.stats["views"][message_key] = db.stats["views"].get(message_key, 0) + 1
        db.stats["users"][str(user.id)] = db.stats["users"].get(str(user.id), 0) + 1
        db.save_all()

        # Store the original message ID for cleanup
        original_message_id = query.message.message_id
        sent_messages = []

        try:
            # Send the message based on its type
            if message_data["type"] == "text":
                sent_message = await query.message.reply_text(
                    f"📝 {message_data['text']}\n\n"
                    f"👤 From: {message_data['first_name']}\n"
                    f"⏱️ This message will be deleted in 5 minutes"
                )
                sent_messages.append(sent_message)
            elif message_data["type"] == "photo":
                    sent_message = await query.message.reply_photo(
                        message_data["file_id"],
                        caption=f"📸 {message_data.get('caption', '')}\n\n"
                               f"👤 From: {message_data['first_name']}\n"
                               f"⏱️ This message will be deleted in 5 minutes"
                    )
                    sent_messages.append(sent_message)
            elif message_data["type"] == "video":
                    sent_message = await query.message.reply_video(
                        message_data["file_id"],
                        caption=f"🎥 {message_data.get('caption', '')}\n\n"
                               f"👤 From: {message_data['first_name']}\n"
                               f"⏱️ This message will be deleted in 5 minutes"
                    )
                    sent_messages.append(sent_message)
            elif message_data["type"] == "document":
                    sent_message = await query.message.reply_document(
                        message_data["file_id"],
                        caption=f"📄 {message_data.get('file_name', '')}\n"
                               f"{message_data.get('caption', '')}\n\n"
                               f"👤 From: {message_data['first_name']}\n"
                               f"⏱️ This message will be deleted in 5 minutes"
                    )
                    sent_messages.append(sent_message)
            elif message_data["type"] == "voice":
                    sent_message = await query.message.reply_voice(
                        message_data["file_id"],
                        caption=f"🎤 Voice Message\n\n"
                               f"👤 From: {message_data['first_name']}\n"
                               f"⏱️ This message will be deleted in 5 minutes"
                    )
                    sent_messages.append(sent_message)
            elif message_data["type"] == "audio":
                    sent_message = await query.message.reply_audio(
                        message_data["file_id"],
                        caption=f"🎵 {message_data.get('title', 'Audio')}\n\n"
                               f"👤 From: {message_data['first_name']}\n"
                               f"⏱️ This message will be deleted in 5 minutes"
                    )
                    sent_messages.append(sent_message)
            elif message_data["type"] == "sticker":
                sent_message = await query.message.reply_sticker(
                    message_data["file_id"]
                )
                sent_messages.append(sent_message)
            elif message_data["type"] == "animation":
                    sent_message = await query.message.reply_animation(
                        message_data["file_id"],
                        caption=f"🎬 {message_data.get('caption', '')}\n\n"
                               f"👤 From: {message_data['first_name']}\n"
                               f"⏱️ This message will be deleted in 5 minutes"
                    )
                    sent_messages.append(sent_message)

            # Store message IDs for cleanup
            message_ids = {
                'original': original_message_id,
                'sent': [msg.message_id for msg in sent_messages],
                'chat_id': query.message.chat_id
            }

            # Start cleanup task
            asyncio.create_task(_cleanup_messages(message_ids))

        except Exception as e:
            logger.error(f"Error sending message: {e}")
            await query.edit_message_text(
                "❌ Error retrieving message content. The file might have expired.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔙 Back", callback_data="cmd_listbatches")
                ]])
            )

    except Exception as e:
        logger.error(f"Error in _show_message: {e}")
        await query.edit_message_text(
            "❌ An error occurred while retrieving the message.\n"
            "Please try again or contact support if the issue persists.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="cmd_listbatches")
            ]])
        )

async def _cleanup_messages(message_ids: dict):
    """Clean up messages after 5 minutes"""
    try:
        # Wait for 5 minutes
        await asyncio.sleep(300)  # 300 seconds = 5 minutes
        
        # Get the bot instance
        bot = Application.get_current().bot
        
        # Delete all related messages
        for msg_id in message_ids['sent']:
            try:
                await bot.delete_message(
                    chat_id=message_ids['chat_id'],
                    message_id=msg_id
                )
            except Exception as e:
                logger.error(f"Error deleting sent message {msg_id}: {e}")
        
        # Delete the original message
        try:
            await bot.delete_message(
                chat_id=message_ids['chat_id'],
                message_id=message_ids['original']
            )
        except Exception as e:
            logger.error(f"Error deleting original message: {e}")
            
    except Exception as e:
        logger.error(f"Error in cleanup task: {e}")

# ==================
# Statistics
# ==================

async def top_messages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.stats["views"]:
        return await update.message.reply_text("ℹ️ No message data yet.")

    top = sorted(
        db.stats["views"].items(),
        key=lambda x: x[1],
        reverse=True
    )[:10]

    msg = "🏆 <b>Top 10 Messages</b>:\n\n"
    for i, (msg_key, count) in enumerate(top):
        if msg_key in db.message_store:
            message = db.message_store[msg_key]
        elif msg_key in db.message_batch:
            message = db.message_batch[msg_key]
        else:
            continue

        preview = message.get("text", f"[{message['type'].upper()}]")[:30]
        msg += f"{i+1}. {preview}... - {count} views\n"

    await update.message.reply_text(msg, parse_mode="HTML")

async def user_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not db.stats["users"]:
        return await update.message.reply_text("ℹ️ No user data yet.")

    top_users = sorted(
        db.stats["users"].items(),
        key=lambda x: x[1],
        reverse=True
    )[:10]

    msg = "🏆 <b>Top 10 Users</b>:\n\n"
    for i, (user_id, count) in enumerate(top_users):
        try:
            user = await context.bot.get_chat(int(user_id))
            name = user.username or user.first_name
            msg += f"{i+1}. @{name} - {count} views\n"
        except:
            msg += f"{i+1}. User {user_id} - {count} views\n"

    await update.message.reply_text(msg, parse_mode="HTML")

# ==================
# Search Functionality
# ==================

async def search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text("ℹ️ Usage: /search <query>")

    query = " ".join(context.args).lower()
    # Search in both message stores
    normal_results = {
        k: v for k, v in db.message_store.items()
        if ("text" in v and query in v["text"].lower()) or
           ("caption" in v and query in v["caption"].lower()) or
           ("file_name" in v and query in v["file_name"].lower())
    }

    if not normal_results:
        return await update.message.reply_text("🔍 No messages found. Try /search_batch for messages in batches.")

    keyboard = []
    for k, v in normal_results.items():
        preview = v.get("text", v.get("caption", f"[{v['type'].upper()}]"))[:30]
        keyboard.append([
            InlineKeyboardButton(
                f"{v['type'].upper()}: {preview}...",
                callback_data=f"msg_{k}"
            )
        ])

    await update.message.reply_text(
        f"📱 Found {len(normal_results)} messages:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def search_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "ℹ️ Usage: /search_batch <query>\n"
            "You can search by:\n"
            "• Batch name\n"
            "• Teacher name\n"
            "• Description\n\n"
            "Example: /search_batch John"
        )

    query = " ".join(context.args).lower()

    # Search in batch names, descriptions, and teacher names
    matches = {
        name: data for name, data in db.batches.items()
        if query in name.lower() or
           query in data.get("description", "").lower() or
           query in data.get("teacher_name", "").lower()
    }

    if not matches:
        # Suggest similar
        suggestions = []
        for name, data in db.batches.items():
            teacher = data.get("teacher_name", "").lower()
            desc = data.get("description", "").lower()
            if (query in name.lower() or
                query in teacher or
                query in desc):
                suggestions.append((name, data))
        
        suggestions = sorted(suggestions, key=lambda x: (
            query in x[0].lower(),  # Exact batch name match first
            query in x[1].get("teacher_name", "").lower(),  # Then teacher name match
            query in x[1].get("description", "").lower()  # Then description match
        ), reverse=True)[:5]

        msg = "🔍 No exact matches found."
        if suggestions:
            msg += "\n\nDid you mean:\n"
            for name, data in suggestions:
                teacher = data.get("teacher_name", "Not specified")
                msg += f"• {name} (Teacher: {teacher})\n"
        return await update.message.reply_text(msg)

    # Group matches by type (batch name, teacher, description)
    batch_name_matches = []
    teacher_matches = []
    desc_matches = []

    for name, data in matches.items():
        teacher = data.get("teacher_name", "Not specified")
        msg_count = len(data.get("messages", []))
        entry = (name, teacher, msg_count)
        
        if query in name.lower():
            batch_name_matches.append(entry)
        elif query in teacher.lower():
            teacher_matches.append(entry)
        else:
            desc_matches.append(entry)

    # Build the message
    msg = "🔍 Search Results:\n\n"
    
    if batch_name_matches:
        msg += "📚 <b>Batch Name Matches:</b>\n"
        for name, teacher, count in batch_name_matches:
            msg += f"• {name} (Teacher: {teacher}, Messages: {count})\n"
        msg += "\n"
    
    if teacher_matches:
        msg += "👨‍🏫 <b>Teacher Name Matches:</b>\n"
        for name, teacher, count in teacher_matches:
            msg += f"• {name} (Teacher: {teacher}, Messages: {count})\n"
        msg += "\n"
    
    if desc_matches:
        msg += "📝 <b>Description Matches:</b>\n"
        for name, teacher, count in desc_matches:
            msg += f"• {name} (Teacher: {teacher}, Messages: {count})\n"

    # Build keyboard
    keyboard = []
    # Create a set to avoid duplicate buttons
    added_batches = set()
    
    # Add buttons for all matches
    for match_list in [batch_name_matches, teacher_matches, desc_matches]:
        for name, _, _ in match_list:
            if name not in added_batches:
                keyboard.append([
                    InlineKeyboardButton(
                        f"📱 View {name}",
                        callback_data=f"batch_{name}"
                    )
                ])
                added_batches.add(name)

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")])

    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def search_teacher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "ℹ️ Usage: /search_teacher <teacher_name>\n"
            "Example: /search_teacher John"
        )

    query = " ".join(context.args).lower()

    # Search for batches by teacher name
    matches = {
        name: data for name, data in db.batches.items()
        if query in data.get("teacher_name", "").lower()
    }

    if not matches:
        # Try to find similar teacher names
        similar_teachers = set()
        for data in db.batches.values():
            teacher = data.get("teacher_name", "").lower()
            if any(word in teacher for word in query.split()):
                similar_teachers.add(teacher)

        msg = f"👨‍🏫 No batches found for teacher '{query}'."
        if similar_teachers:
            msg += "\n\nSimilar teacher names found:\n"
            for teacher in sorted(similar_teachers)[:5]:
                msg += f"• {teacher}\n"
        return await update.message.reply_text(msg)

    # Sort matches by number of messages
    sorted_matches = sorted(
        matches.items(),
        key=lambda x: len(x[1].get("messages", [])),
        reverse=True
    )

    # Build the message
    msg = f"👨‍🏫 <b>Batches by Teacher</b>\n\n"
    for name, data in sorted_matches:
        teacher = data.get("teacher_name", "Not specified")
        msg_count = len(data.get("messages", []))
        created_at = datetime.fromisoformat(data['created_at']).strftime("%B %d, %Y")
        msg += f"📚 <b>{name}</b>\n"
        msg += f"• Messages: {msg_count}\n"
        msg += f"• Created: {created_at}\n"
        msg += f"• Description: {data.get('description', 'No description')}\n\n"

    # Build keyboard
    keyboard = []
    for name, _ in sorted_matches:
        keyboard.append([
            InlineKeyboardButton(
                f"📱 View {name}",
                callback_data=f"batch_{name}"
            )
        ])

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back to Batches", callback_data="cmd_listbatches")])

    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def list_batches(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        if not db.batches:
            if update.message:
                await update.message.reply_text("ℹ️ No batches created yet.")
            else:
                await update.callback_query.edit_message_text("ℹ️ No batches created yet.")
            return

        # Sort batches by number of messages
        batches = sorted(
            db.batches.items(),
            key=lambda x: len(x[1]["messages"]),
            reverse=True
        )

        # Create header message
        msg = "📦 <b>Available Batches</b>\n\n"
        msg += "Click on a batch to view its contents.\n"
        msg += "Use 🔔 to subscribe for notifications when new contents are added.\n\n"

        # Create keyboard with batch buttons
        keyboard = []
        current_row = []
        
        for name, data in batches:
            teacher = data.get("teacher_name", "Not specified")
            msg_count = len(data["messages"])
            button_text = f"📚 {name} ({msg_count})"
            
            # Add batch button to current row
            current_row.append(InlineKeyboardButton(
                button_text,
                callback_data=f"batch_{name}"
            ))
            
            # Add subscribe/unsubscribe button
            user_id = update.effective_user.id
            is_subscribed = db.is_subscribed(user_id, name)
            sub_button = InlineKeyboardButton(
                "🔔 Subscribe" if not is_subscribed else "🔕 Unsubscribe",
                callback_data=f"sub_{name}" if not is_subscribed else f"unsub_{name}"
            )
            current_row.append(sub_button)
            
            # If row is full (2 batch buttons), add row to keyboard
            if len(current_row) == 2:
                keyboard.append(current_row)
                current_row = []

        # Add any remaining buttons
        if current_row:
            keyboard.append(current_row)

        # Add refresh button at the bottom
        keyboard.append([InlineKeyboardButton("🔄 Refresh List", callback_data="cmd_listbatches")])
        
        if update.message:
            await update.message.reply_text(
                msg,
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            try:
                await update.callback_query.edit_message_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            except Exception as e:
                # If editing fails (e.g., message has media), send a new message
                await update.callback_query.message.reply_text(
                    msg,
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
                # Try to delete the old message
                try:
                    await update.callback_query.message.delete()
                except Exception as e:
                    logger.error(f"Error deleting old message: {e}")

    except Exception as e:
        logger.error(f"Error in list_batches: {e}")
        error_msg = "❌ An error occurred while listing batches.\nPlease try again or contact support if the issue persists."
        if update.message:
            await update.message.reply_text(error_msg)
        elif update.callback_query:
            try:
                await update.callback_query.edit_message_text(error_msg)
            except Exception as e:
                await update.callback_query.message.reply_text(error_msg)

async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user profile with subscribed batches"""
    user = update.effective_user
    
    # Update user profile
    db.update_user_profile(user.id, user.username, user.first_name)
    
    # Get user profile and subscriptions
    profile = db.get_user_profile(user.id)
    subscribed_batches = db.get_user_subscriptions(user.id)
    
    # Build profile message
    msg = f"👤 <b>User Profile</b>\n\n"
    msg += f"🆔 <b>User ID:</b> {user.id}\n"
    msg += f"👤 <b>Name:</b> {user.first_name}\n"
    if user.username:
        msg += f"📝 <b>Username:</b> @{user.username}\n"
    
    # Add subscription information
    if subscribed_batches:
        msg += f"\n📚 <b>Subscribed Batches ({len(subscribed_batches)}):</b>\n"
        for batch_name in subscribed_batches:
            if batch_name in db.batches:
                batch = db.batches[batch_name]
                msg += f"• {batch_name} ({len(batch['messages'])} messages)\n"
    else:
        msg += "\n📚 <b>No subscribed batches</b>\n"
        msg += "Use /listbatches to discover and subscribe to batches!"
    
    # Add stats if available
    user_views = db.stats["users"].get(str(user.id), 0)
    if user_views > 0:
        msg += f"\n📊 <b>Total Views:</b> {user_views}\n"
    
    # Create keyboard
    keyboard = [
        [InlineKeyboardButton("📚 View All Batches", callback_data="cmd_listbatches")],
        [InlineKeyboardButton("🔄 Refresh Profile", callback_data="cmd_profile")]
    ]
    
    await update.message.reply_text(
        msg,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def search_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Search messages in a batch by date"""
    if not db.batches:
        return await update.message.reply_text(
            "ℹ️ No batches available to search.\n"
            "Create a batch first using /createbatch"
        )

    # Create keyboard with batch buttons
    keyboard = []
    for name, data in db.batches.items():
        teacher = data.get("teacher_name", "Not specified")
        msg_count = len(data.get("messages", []))
        button_text = f"📚 {name} ({msg_count} messages)"
        keyboard.append([InlineKeyboardButton(button_text, callback_data=f"search_date_{name}")])

    # Add back button
    keyboard.append([InlineKeyboardButton("🔙 Back", callback_data="cmd_help")])

    await update.message.reply_text(
        "📅 <b>Select a batch to search by date:</b>\n\n"
        "Choose a batch from the list below to search its messages by date.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def findmsg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Find a message using its message ID"""
    if not context.args:
        return await update.message.reply_text(
            "ℹ️ Usage: /findmsg <message_id>\n"
            "Example: /findmsg msg_1234567890"
        )

    message_id = context.args[0].strip()
    
    # Log the search attempt
    logger.info(f"Searching for message ID: {message_id}")
    
    # Reload message stores to ensure we have latest data
    try:
        db.message_store = FileManager.load_data(MESSAGE_STORE_PATH)
        db.message_batch = FileManager.load_data(MESSAGE_BATCH_PATH)
        logger.info(f"Message store size: {len(db.message_store)}")
        logger.info(f"Message batch size: {len(db.message_batch)}")
    except Exception as e:
        logger.error(f"Error loading message stores: {e}")
        return await update.message.reply_text(
            "❌ Error accessing message database.\n"
            "Please try again later or contact support."
        )
    
    # Check both message stores
    message_data = None
    if message_id in db.message_store:
        message_data = db.message_store[message_id]
        logger.info(f"Found message in message_store: {message_id}")
    elif message_id in db.message_batch:
        message_data = db.message_batch[message_id]
        logger.info(f"Found message in message_batch: {message_id}")
    else:
        logger.warning(f"Message not found: {message_id}")
        return await update.message.reply_text(
            "❌ Message not found!\n"
            "Please check the message ID and try again.\n\n"
            "Tip: Make sure you're using the correct message ID format (e.g., msg_1234567890)"
        )

    # Track view stats
    db.stats["views"][message_id] = db.stats["views"].get(message_id, 0) + 1
    db.stats["users"][str(update.effective_user.id)] = db.stats["users"].get(str(update.effective_user.id), 0) + 1
    db.save_all()

    # Store the original message ID for cleanup
    original_message_id = update.message.message_id
    sent_messages = []

    try:
        # Log message type and data
        logger.info(f"Message type: {message_data.get('type', 'unknown')}")
        
        # Send the message based on its type
        if message_data["type"] == "text":
            sent_message = await update.message.reply_text(
                f"📝 {message_data['text']}\n\n"
                f"👤 From: {message_data['first_name']}\n"
                f"⏱️ This message will be deleted in 5 minutes"
            )
            sent_messages.append(sent_message)
        elif message_data["type"] == "photo":
            sent_message = await update.message.reply_photo(
                    message_data["file_id"],
                    caption=f"📸 {message_data.get('caption', '')}\n\n"
                           f"👤 From: {message_data['first_name']}\n"
                           f"⏱️ This message will be deleted in 5 minutes"
                )
            sent_messages.append(sent_message)
        elif message_data["type"] == "video":
            sent_message = await update.message.reply_video(
                    message_data["file_id"],
                    caption=f"🎥 {message_data.get('caption', '')}\n\n"
                           f"👤 From: {message_data['first_name']}\n"
                           f"⏱️ This message will be deleted in 5 minutes"
                )
            sent_messages.append(sent_message)
        elif message_data["type"] == "document":
            sent_message = await update.message.reply_document(
                    message_data["file_id"],
                    caption=f"📄 {message_data.get('file_name', '')}\n"
                           f"{message_data.get('caption', '')}\n\n"
                           f"👤 From: {message_data['first_name']}\n"
                           f"⏱️ This message will be deleted in 5 minutes"
                )
            sent_messages.append(sent_message)
        elif message_data["type"] == "voice":
            sent_message = await update.message.reply_voice(
                    message_data["file_id"],
                    caption=f"🎤 Voice Message\n\n"
                           f"👤 From: {message_data['first_name']}\n"
                           f"⏱️ This message will be deleted in 5 minutes"
                )
            sent_messages.append(sent_message)
        elif message_data["type"] == "audio":
            sent_message = await update.message.reply_audio(
                    message_data["file_id"],
                    caption=f"🎵 {message_data.get('title', 'Audio')}\n\n"
                           f"👤 From: {message_data['first_name']}\n"
                           f"⏱️ This message will be deleted in 5 minutes"
                )
            sent_messages.append(sent_message)
        elif message_data["type"] == "sticker":
            sent_message = await update.message.reply_sticker(
                message_data["file_id"]
            )
            sent_messages.append(sent_message)
        elif message_data["type"] == "animation":
            sent_message = await update.message.reply_animation(
                    message_data["file_id"],
                    caption=f"🎬 {message_data.get('caption', '')}\n\n"
                           f"👤 From: {message_data['first_name']}\n"
                           f"⏱️ This message will be deleted in 5 minutes"
                )
            sent_messages.append(sent_message)
        else:
            logger.error(f"Unknown message type: {message_data.get('type', 'unknown')}")
            return await update.message.reply_text(
                "❌ Unknown message type. Please contact support."
            )

        # Store message IDs for cleanup
        message_ids = {
            'original': original_message_id,
            'sent': [msg.message_id for msg in sent_messages],
            'chat_id': update.message.chat_id
        }

        # Start cleanup task
        asyncio.create_task(_cleanup_messages(message_ids))

    except Exception as e:
        logger.error(f"Error sending message: {e}")
        await update.message.reply_text(
            "❌ Error retrieving message content. The file might have expired.\n"
            "Please try again or contact support if the issue persists.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔙 Back", callback_data="cmd_listbatches")
            ]])
    )

async def share_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        return await update.message.reply_text(
            "ℹ️ Usage: /sharebatch <batch_name>\n"
            "Example: /sharebatch Math101"
        )

    batch_name = " ".join(context.args)
    actual_batch = db.get_batch_key(batch_name)
    if actual_batch not in db.batches:
        return await update.message.reply_text("❌ Batch not found!")

    # Generate a unique share token
    share_token = f"batch_{actual_batch}_{int(datetime.now().timestamp())}"

    # Store the share token in the batch data
    if "share_tokens" not in db.batches[actual_batch]:
        db.batches[actual_batch]["share_tokens"] = []
    db.batches[actual_batch]["share_tokens"].append(share_token)
    db.save_all()

    # Create the share link
    share_link = f"https://t.me/{context.bot.username}?start={share_token}"

    # Get batch info
    batch = db.batches[actual_batch]
    teacher = batch.get("teacher_name", "Not specified")
    msg_count = len(batch.get("messages", []))
    description = batch.get("description", "No description")

    # Create message with batch info and share link
    msg = f"🔗 <b>Share Batch</b>\n\n"
    msg += f"📚 <b>Batch:</b> {actual_batch}\n"
    msg += f"👨‍🏫 <b>Teacher:</b> {teacher}\n"
    msg += f"📝 <b>Description:</b> {description}\n"
    msg += f"📨 <b>Messages:</b> {msg_count}\n\n"
    msg += f"🔗 <b>Share Link:</b>\n<code>{share_link}</code>\n\n"
    msg += "Click the link above to share this batch with others.\n"
    msg += "The link will give access to view all messages in this batch."

    # Create keyboard
    keyboard = [
        [InlineKeyboardButton("📱 View Batch", callback_data=f"batch_{actual_batch}")],
        [InlineKeyboardButton("🔙 Back to Batch Info", callback_data=f"back_to_batch_{actual_batch}")]
    ]

    # If there's a banner picture, send it with the share message
    if batch.get("banner_pic"):
        await update.message.reply_photo(
            batch["banner_pic"],
            caption=msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.message.reply_text(
            msg,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ==================
# Bot Setup
# ==================

def main():
    app = Application.builder().token(TOKEN).build()

    # Core commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("search", search))
    app.add_handler(CommandHandler("search_batch", search_batch))
    app.add_handler(CommandHandler("search_teacher", search_teacher))
    app.add_handler(CommandHandler("search_date", search_date))
    app.add_handler(CommandHandler("topmessages", top_messages))
    app.add_handler(CommandHandler("userstats", user_stats))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("findmsg", findmsg))
    app.add_handler(CommandHandler("sharebatch", share_batch))  # Add new share command

    # Batch commands
    app.add_handler(CommandHandler("createbatch", create_batch))
    app.add_handler(CommandHandler("addtobatch", add_to_batch))
    app.add_handler(CommandHandler("done", done_adding))
    app.add_handler(CommandHandler("listbatches", list_batches))
    app.add_handler(CommandHandler("batchinfo", batch_info))
    app.add_handler(CommandHandler("editbatch", edit_batch))
    app.add_handler(CommandHandler("setbanner", set_banner))

    # Handlers
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
