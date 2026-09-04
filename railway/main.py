#!/usr/bin/env python3
"""
TempMail Telegram Bot - Railway + Cloudflare Workers
Version: 3.0.0
Author: Just-TnError
"""

import asyncio
import logging
import os
import json
import sqlite3
import uuid
import secrets
import string
import time
import re
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from email.parser import BytesParser
from email.policy import default

import aiosqlite
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, ContextTypes, filters
)
from telegram.constants import ParseMode
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
import threading

# ============ CONFIGURATION ============
class Config:
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")
    ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x]
    DATABASE_PATH = os.getenv("DATABASE_PATH", "/app/data/tempmail.db")
    DOMAIN = os.getenv("DOMAIN", "")
    WEBHOOK_URL = os.getenv("WEBHOOK_URL", "")
    EMAIL_WEBHOOK_KEY = os.getenv("EMAIL_WEBHOOK_KEY", "")
    PORT = int(os.getenv("PORT", "8080"))
    RATE_LIMIT = int(os.getenv("RATE_LIMIT", "10"))
    MAX_ACCOUNTS = int(os.getenv("MAX_ACCOUNTS", "10"))
    EMAIL_EXPIRY_MINUTES = int(os.getenv("EMAIL_EXPIRY_MINUTES", "120"))
    CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL", "300"))
    MAX_MESSAGE_SIZE = int(os.getenv("MAX_MESSAGE_SIZE", "1024"))
    
    @classmethod
    def validate(cls):
        """Validate required configuration"""
        if not cls.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is required")
        if not cls.DOMAIN:
            raise ValueError("DOMAIN is required")
        if not cls.EMAIL_WEBHOOK_KEY:
            logging.warning("EMAIL_WEBHOOK_KEY not set, using random")
            cls.EMAIL_WEBHOOK_KEY = secrets.token_hex(16)

# ============ DATABASE ============
class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_db()
    
    def get_connection(self):
        return sqlite3.connect(self.db_path, check_same_thread=False)
    
    def init_db(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Users table
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    is_premium BOOLEAN DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP
                )
            ''')
            
            # Email accounts
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS email_accounts (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    email_address TEXT UNIQUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    expires_at TIMESTAMP,
                    is_active BOOLEAN DEFAULT 1,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
            ''')
            
            # Messages
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS messages (
                    id TEXT PRIMARY KEY,
                    email_account_id TEXT,
                    sender TEXT,
                    subject TEXT,
                    body TEXT,
                    html_body TEXT,
                    received_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_read BOOLEAN DEFAULT 0,
                    FOREIGN KEY (email_account_id) REFERENCES email_accounts(id)
                )
            ''')
            
            # Attachments
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS attachments (
                    id TEXT PRIMARY KEY,
                    message_id TEXT,
                    filename TEXT,
                    content_type TEXT,
                    size INTEGER,
                    FOREIGN KEY (message_id) REFERENCES messages(id)
                )
            ''')
            
            conn.commit()
            logging.info("Database initialized")

# ============ EMAIL SERVICE ============
class TempMailService:
    def __init__(self):
        self.db = Database(Config.DATABASE_PATH)
    
    def generate_email(self, user_id: int, custom_prefix: str = None) -> str:
        """Generate temporary email address"""
        if custom_prefix:
            prefix = self.sanitize_prefix(custom_prefix)
        else:
            prefix = self.generate_random_prefix()
        
        email = f"{prefix}@{Config.DOMAIN}"
        
        # Check availability
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT email_address FROM email_accounts WHERE email_address = ?", (email,))
            if cursor.fetchone():
                return self.generate_email(user_id)
        
        return email
    
    def generate_random_prefix(self, length: int = 10) -> str:
        """Generate random email prefix"""
        alphabet = string.ascii_lowercase + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))
    
    def sanitize_prefix(self, prefix: str) -> str:
        """Sanitize custom email prefix"""
        prefix = re.sub(r'[^a-zA-Z0-9._-]', '', prefix.lower())
        if not prefix:
            return self.generate_random_prefix()
        return prefix[:20]
    
    def create_email_account(self, user_id: int, custom_prefix: str = None) -> Dict:
        """Create new temporary email account"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            
            # Check account limit
            cursor.execute("""
                SELECT COUNT(*) FROM email_accounts 
                WHERE user_id = ? AND is_active = 1 AND expires_at > datetime('now')
            """, (user_id,))
            
            if cursor.fetchone()[0] >= Config.MAX_ACCOUNTS:
                raise ValueError(f"Maximum {Config.MAX_ACCOUNTS} accounts reached")
            
            email_address = self.generate_email(user_id, custom_prefix)
            account_id = str(uuid.uuid4())
            expires_at = datetime.now() + timedelta(minutes=Config.EMAIL_EXPIRY_MINUTES)
            
            cursor.execute("""
                INSERT INTO email_accounts (id, user_id, email_address, expires_at)
                VALUES (?, ?, ?, ?)
            """, (account_id, user_id, email_address, expires_at))
            
            # Register user if not exists
            cursor.execute("""
                INSERT OR IGNORE INTO users (user_id) VALUES (?)
            """, (user_id,))
            
            conn.commit()
            
            return {
                "id": account_id,
                "email": email_address,
                "expires_at": expires_at.isoformat()
            }
    
    def delete_email_account(self, account_id: str, user_id: int) -> bool:
        """Delete email account"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE email_accounts SET is_active = 0 
                WHERE id = ? AND user_id = ?
            """, (account_id, user_id))
            conn.commit()
            return cursor.rowcount > 0
    
    def get_user_accounts(self, user_id: int) -> List[Dict]:
        """Get all active email accounts for user"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, email_address, created_at, expires_at, is_active
                FROM email_accounts 
                WHERE user_id = ? AND is_active = 1 AND expires_at > datetime('now')
                ORDER BY created_at DESC
            """, (user_id,))
            
            accounts = []
            for row in cursor.fetchall():
                accounts.append({
                    "id": row[0],
                    "email": row[1],
                    "created_at": row[2],
                    "expires_at": row[3],
                    "is_active": row[4]
                })
            
            return accounts
    
    def get_messages(self, account_id: str, limit: int = 50) -> List[Dict]:
        """Get messages for email account"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, sender, subject, body, html_body, received_at, is_read
                FROM messages 
                WHERE email_account_id = ?
                ORDER BY received_at DESC
                LIMIT ?
            """, (account_id, limit))
            
            messages = []
            for row in cursor.fetchall():
                messages.append({
                    "id": row[0],
                    "sender": row[1],
                    "subject": row[2],
                    "body": row[3],
                    "html_body": row[4],
                    "received_at": row[5],
                    "is_read": row[6]
                })
            
            return messages
    
    def store_message(self, email_address: str, message_data: Dict) -> bool:
        """Store incoming message"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("""
                SELECT id FROM email_accounts 
                WHERE email_address = ? AND is_active = 1
            """, (email_address,))
            
            result = cursor.fetchone()
            if not result:
                return False
            
            account_id = result[0]
            message_id = str(uuid.uuid4())
            
            cursor.execute("""
                INSERT INTO messages (id, email_account_id, sender, subject, body, html_body)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (message_id, account_id, message_data.get("sender", ""),
                  message_data.get("subject", ""), message_data.get("body", ""),
                  message_data.get("html_body", "")))
            
            # Store attachments if any
            for attachment in message_data.get("attachments", []):
                attachment_id = str(uuid.uuid4())
                cursor.execute("""
                    INSERT INTO attachments (id, message_id, filename, content_type, size)
                    VALUES (?, ?, ?, ?, ?)
                """, (attachment_id, message_id, attachment.get("filename", ""),
                      attachment.get("contentType", ""), attachment.get("size", 0)))
            
            conn.commit()
            
            # Get user_id for notification
            cursor.execute("SELECT user_id FROM email_accounts WHERE id = ?", (account_id,))
            user_result = cursor.fetchone()
            
            if user_result:
                return user_result[0]  # Return user_id for notification
            
            return True
    
    def mark_message_read(self, message_id: str) -> bool:
        """Mark message as read"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE messages SET is_read = 1 WHERE id = ?
            """, (message_id,))
            conn.commit()
            return cursor.rowcount > 0
    
    def get_unread_count(self, account_id: str) -> int:
        """Get unread message count"""
        with self.db.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) FROM messages 
                WHERE email_account_id = ? AND is_read = 0
            """, (account_id,))
            return cursor.fetchone()[0]

# ============ WEBHOOK HANDLER ============
class WebhookHandler:
    def __init__(self, service: TempMailService, bot_app=None):
        self.service = service
        self.app = FastAPI()
        self.bot_app = bot_app
        self.setup_routes()
    
    def setup_routes(self):
        @self.app.post("/webhook/email")
        async def receive_email(request: Request):
            """Receive email from Cloudflare Worker"""
            try:
                # Verify webhook key
                email_key = request.headers.get("X-Email-Key", "")
                if email_key != Config.EMAIL_WEBHOOK_KEY:
                    logging.warning("Invalid webhook key")
                    return JSONResponse({"status": "unauthorized"}, status_code=401)
                
                data = await request.json()
                
                email_address = data.get("to", "").strip().lower()
                sender = data.get("from", "")
                subject = data.get("subject", "")
                body = data.get("text", "")
                html_body = data.get("html", "")
                
                # Store message
                result = self.service.store_message(email_address, {
                    "sender": sender,
                    "subject": subject,
                    "body": body or "No content",
                    "html_body": html_body or f"<p>{body}</p>",
                    "attachments": data.get("attachments", [])
                })
                
                if result:
                    # Send notification to user
                    user_id = result if isinstance(result, int) else None
                    if user_id and self.bot_app:
                        try:
                            await self.bot_app.send_message(
                                chat_id=user_id,
                                text=f"📬 *New Email Received*\n\n"
                                     f"*From:* {sender}\n"
                                     f"*Subject:* {subject}\n"
                                     f"*To:* {email_address}\n\n"
                                     f"_Use /inbox to read_",
                                parse_mode=ParseMode.MARKDOWN
                            )
                        except Exception as e:
                            logging.error(f"Failed to send notification: {e}")
                    
                    return JSONResponse({"status": "success"})
                else:
                    return JSONResponse({"status": "invalid_recipient"}, status_code=404)
                    
            except Exception as e:
                logging.error(f"Error processing email: {e}")
                return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
        
        @self.app.get("/health")
        async def health_check():
            return {
                "status": "healthy",
                "domain": Config.DOMAIN,
                "timestamp": datetime.now().isoformat()
            }
    
    def run(self):
        uvicorn.run(self.app, host="0.0.0.0", port=Config.PORT)

# ============ TELEGRAM BOT ============
class TempMailBot:
    def __init__(self, service: TempMailService):
        self.service = service
        self.application = None
    
    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /start command"""
        user = update.effective_user
        
        welcome_text = f"""
🛡️ *TEMPMAIL PRO* 🛡️

Welcome, {user.first_name}!

Generate disposable email addresses instantly!

*Features:*
✅ Instant email generation
✅ Custom email prefix
✅ Real-time notifications
✅ Multiple accounts
✅ Auto-cleanup

*Commands:*
📧 /new [prefix] - Create email
📋 /list - Your emails
📥 /inbox - Check messages
🗑️ /delete [email] - Delete
ℹ️ /help - Help menu

_Domain: {Config.DOMAIN}_
        """
        
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def new_email_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /new command"""
        user_id = update.effective_user.id
        args = context.args
        
        custom_prefix = args[0] if args else None
        
        try:
            account = self.service.create_email_account(user_id, custom_prefix)
            
            keyboard = [
                [InlineKeyboardButton("📥 Check Inbox", callback_data=f"inbox_{account['id']}")],
                [InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{account['id']}")]
            ]
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            message_text = f"""
📧 *New Email Created*

*Address:* `{account['email']}`
*Expires:* {account['expires_at'][:19].replace('T', ' ')}

_Forward all emails to this address._
            """
            
            await update.message.reply_text(
                message_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            
        except ValueError as e:
            await update.message.reply_text(f"❌ {e}")
    
    async def list_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /list command"""
        user_id = update.effective_user.id
        accounts = self.service.get_user_accounts(user_id)
        
        if not accounts:
            await update.message.reply_text("📭 No active email accounts.")
            return
        
        keyboard = []
        for account in accounts:
            unread = self.service.get_unread_count(account['id'])
            keyboard.append([
                InlineKeyboardButton(
                    f"📧 {account['email']} ({unread})",
                    callback_data=f"inbox_{account['id']}"
                )
            ])
        
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        message_text = "*📋 Your Active Emails:*\n\n"
        for i, account in enumerate(accounts, 1):
            expiry = account['expires_at'][:19].replace('T', ' ')
            unread = self.service.get_unread_count(account['id'])
            message_text += f"{i}. `{account['email']}`\n"
            message_text += f"   📬 Unread: {unread} | ⏰ Expires: {expiry}\n\n"
        
        await update.message.reply_text(
            message_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    
    async def inbox_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /inbox command"""
        user_id = update.effective_user.id
        accounts = self.service.get_user_accounts(user_id)
        
        if not accounts:
            await update.message.reply_text("📭 No active email accounts.")
            return
        
        if len(accounts) == 1:
            # Show inbox for single account
            messages = self.service.get_messages(accounts[0]['id'])
            if not messages:
                await update.message.reply_text(f"📭 Inbox empty for {accounts[0]['email']}")
                return
            
            for msg in messages[:5]:
                keyboard = [
                    [InlineKeyboardButton("📖 Read Full", callback_data=f"read_{msg['id']}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                preview = msg['body'][:150] + "..." if len(msg['body']) > 150 else msg['body']
                
                message_text = f"""
📬 *New Message*
*From:* {msg['sender']}
*Subject:* {msg['subject']}
*Time:* {msg['received_at']}

*Preview:*
{preview}
                """
                
                await update.message.reply_text(
                    message_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
        else:
            # Show account selection
            keyboard = []
            for account in accounts:
                unread = self.service.get_unread_count(account['id'])
                keyboard.append([
                    InlineKeyboardButton(
                        f"📧 {account['email']} ({unread} unread)",
                        callback_data=f"inbox_{account['id']}"
                    )
                ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            await update.message.reply_text(
                "*Select email to view inbox:*",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
    
    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /delete command"""
        user_id = update.effective_user.id
        args = context.args
        
        if not args:
            await update.message.reply_text("Usage: /delete [email]")
            return
        
        email = args[0]
        accounts = self.service.get_user_accounts(user_id)
        account = next((a for a in accounts if a['email'] == email), None)
        
        if not account:
            await update.message.reply_text("❌ Email not found.")
            return
        
        if self.service.delete_email_account(account['id'], user_id):
            await update.message.reply_text(f"✅ Email {email} deleted.")
        else:
            await update.message.reply_text("❌ Failed to delete email.")
    
    async def help_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle /help command"""
        help_text = """
📚 *HELP MENU*

*Commands:*
📧 /new [prefix] - Create new email
📋 /list - List your emails
📥 /inbox - Check messages
🗑️ /delete [email] - Delete email
ℹ️ /help - This menu

*How to use:*
1. Create email with /new
2. Use email for registration
3. Receive emails in Telegram
4. Delete when done

*Tips:*
- Use custom prefix: /new myname
- Emails expire after 120 minutes
- Max 10 accounts per user
        """
        
        await update.message.reply_text(
            help_text,
            parse_mode=ParseMode.MARKDOWN
        )
    
    async def button_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Handle button callbacks"""
        query = update.callback_query
        await query.answer()
        
        data = query.data
        user_id = query.from_user.id
        
        if data.startswith("inbox_"):
            account_id = data.replace("inbox_", "")
            messages = self.service.get_messages(account_id)
            
            if not messages:
                await query.edit_message_text("📭 Inbox is empty.")
                return
            
            message_text = f"*📥 Inbox ({len(messages)} messages):*\n\n"
            keyboard = []
            
            for msg in messages[:10]:
                unread_mark = "🔵" if not msg['is_read'] else "⚪"
                message_text += f"{unread_mark} {msg['subject'][:30]}\n"
                message_text += f"   From: {msg['sender'][:30]}\n\n"
                
                keyboard.append([
                    InlineKeyboardButton(
                        f"{'🔵' if not msg['is_read'] else '📖'} {msg['subject'][:30]}",
                        callback_data=f"read_{msg['id']}"
                    )
                ])
            
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await query.edit_message_text(
                message_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=reply_markup
            )
            
        elif data.startswith("read_"):
            message_id = data.replace("read_", "")
            
            with self.service.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT sender, subject, body, received_at FROM messages 
                    WHERE id = ?
                """, (message_id,))
                msg = cursor.fetchone()
            
            if msg:
                self.service.mark_message_read(message_id)
                
                # Truncate long messages
                body = msg[2]
                if len(body) > 3000:
                    body = body[:3000] + "...\n\n_(Message truncated)_"
                
                message_text = f"""
📩 *Message Details*

*From:* {msg[0]}
*Subject:* {msg[1]}
*Time:* {msg[3]}

*Content:*
{body}
                """
                
                keyboard = [
                    [InlineKeyboardButton("🗑️ Delete Message", callback_data=f"delmsg_{message_id}")]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                
                await query.edit_message_text(
                    message_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=reply_markup
                )
        
        elif data.startswith("delete_"):
            account_id = data.replace("delete_", "")
            if self.service.delete_email_account(account_id, user_id):
                await query.edit_message_text("✅ Email deleted successfully.")
            else:
                await query.edit_message_text("❌ Failed to delete email.")
        
        elif data.startswith("delmsg_"):
            message_id = data.replace("delmsg_", "")
            # Implement message deletion
            with self.service.db.get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("DELETE FROM messages WHERE id = ?", (message_id,))
                conn.commit()
            
            await query.edit_message_text("🗑️ Message deleted.")

# ============ CLEANUP SERVICE ============
class CleanupService:
    def __init__(self, service: TempMailService):
        self.service = service
        self.running = False
    
    async def cleanup_expired(self):
        """Cleanup expired emails"""
        while self.running:
            try:
                with self.service.db.get_connection() as conn:
                    cursor = conn.cursor()
                    
                    # Deactivate expired accounts
                    cursor.execute("""
                        UPDATE email_accounts SET is_active = 0 
                        WHERE expires_at < datetime('now')
                    """)
                    
                    # Delete messages from inactive accounts
                    cursor.execute("""
                        DELETE FROM messages WHERE email_account_id IN (
                            SELECT id FROM email_accounts WHERE is_active = 0
                            AND created_at < datetime('now', '-1 day')
                        )
                    """)
                    
                    conn.commit()
                    
                logging.info("Cleanup completed")
                
            except Exception as e:
                logging.error(f"Cleanup error: {e}")
            
            await asyncio.sleep(Config.CLEANUP_INTERVAL)
    
    def start(self):
        self.running = True
        asyncio.create_task(self.cleanup_expired())
    
    def stop(self):
        self.running = False

# ============ MAIN ============
def main():
    """Main entry point"""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler('/app/logs/tempmail.log'),
            logging.StreamHandler()
        ]
    )
    
    # Validate config
    Config.validate()
    
    # Initialize services
    service = TempMailService()
    bot = TempMailBot(service)
    webhook = WebhookHandler(service)
    cleanup = CleanupService(service)
    
    # Setup bot
    application = Application.builder().token(Config.BOT_TOKEN).build()
    bot.application = application
    webhook.bot_app = application.bot
    
    # Add handlers
    application.add_handler(CommandHandler("start", bot.start_command))
    application.add_handler(CommandHandler("new", bot.new_email_command))
    application.add_handler(CommandHandler("list", bot.list_command))
    application.add_handler(CommandHandler("inbox", bot.inbox_command))
    application.add_handler(CommandHandler("delete", bot.delete_command))
    application.add_handler(CommandHandler("help", bot.help_command))
    application.add_handler(CallbackQueryHandler(bot.button_handler))
    
    # Start cleanup
    cleanup.start()
    
    # Start webhook server
    webhook_thread = threading.Thread(target=webhook.run, daemon=True)
    webhook_thread.start()
    
    # Start bot
    logging.info("Starting TempMail Bot...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()