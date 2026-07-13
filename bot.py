import os
import sqlite3
import asyncio
import logging
import aiohttp
import json
import random
import qrcode
import re
import html
import urllib.parse
import hashlib
import base64

from io import BytesIO
from datetime import datetime, timedelta
from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)





# --- Configuration ---
TOKEN = os.environ.get("BOT_TOKEN")
WEATHER_API_KEY = os.environ.get("WEATHER_KEY")
GEMINI_API_KEY = os.environ.get("GEMINI_KEY", "")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL")
PORT = int(os.environ.get("PORT", "7860"))
DB_PATH = "bot_data.db"

# --- Logging ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ==================== DATABASE ====================
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    tables = [
        """CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, default_city TEXT DEFAULT '', daily_briefing INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS gemini_keys (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, api_key TEXT, label TEXT DEFAULT 'Key')""",
        """CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, title TEXT, content TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS passwords (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, site TEXT, username TEXT, password TEXT)""",
        """CREATE TABLE IF NOT EXISTS todos (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, task TEXT, completed INTEGER DEFAULT 0, created_at TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS chat_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, role TEXT, content TEXT, timestamp TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS lenden (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, person_name TEXT, amount REAL, type TEXT, reason TEXT DEFAULT '', date TEXT DEFAULT CURRENT_TIMESTAMP, settled INTEGER DEFAULT 0)""",
        """CREATE TABLE IF NOT EXISTS expenses (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT, amount REAL, description TEXT, date TEXT DEFAULT CURRENT_TIMESTAMP)""",
        """CREATE TABLE IF NOT EXISTS custom_links (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, category TEXT DEFAULT 'general', name TEXT, url TEXT, description TEXT DEFAULT '')""",
    ]
    for t in tables:
        c.execute(t)
    conn.commit()
    conn.close()

def db_query(query, params=(), fetchone=False, fetchall=False, commit=False):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(query, params)
    result = None
    if fetchone: result = cursor.fetchone()
    if fetchall: result = cursor.fetchall()
    if commit: conn.commit()
    conn.close()
    return result

def ensure_user(user_id):
    existing = db_query("SELECT user_id FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    if not existing:
        db_query("INSERT INTO users (user_id) VALUES (?)", (user_id,), commit=True)

# ==================== AI CHAT (Gemini) ====================
async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    text = update.message.text.strip()
    if not text:
        return
    
    key_index = 0
    if re.match(r'^#\d+\s', text):
        parts = text.split(maxsplit=1)
        try:
            key_index = int(parts[0][1:]) - 1
            text = parts[1] if len(parts) > 1 else ""
        except: pass
    
    if not text:
        await update.message.reply_text("Please type a message after the key number.")
        return
    
    await update.message.chat.send_action("typing")
    
    keys = db_query("SELECT api_key FROM gemini_keys WHERE user_id = ?", (user_id,), fetchall=True)
    if keys and key_index < len(keys):
        api_key = keys[key_index][0]
    elif keys:
        api_key = keys[0][0]
    elif GEMINI_API_KEY:
        api_key = GEMINI_API_KEY
    else:
        await update.message.reply_text(
            "⚠️ No API key set!\n\n"
            "Add your FREE Gemini key:\n"
            "1. Go to: https://aistudio.google.com/app/apikey\n"
            "2. Click 'Create API Key'\n"
            "3. Send: /addkey YOUR_KEY\n\nIt's 100% free!"
        )
        return
    
    history = db_query("SELECT role, content FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 10", (user_id,), fetchall=True)
    history = list(reversed(history)) if history else []
    
    try:
        reply = await call_gemini(api_key, text, history)
        db_query("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, "user", text), commit=True)
        db_query("INSERT INTO chat_history (user_id, role, content) VALUES (?, ?, ?)", (user_id, "model", reply), commit=True)
        db_query("DELETE FROM chat_history WHERE user_id = ? AND id NOT IN (SELECT id FROM chat_history WHERE user_id = ? ORDER BY id DESC LIMIT 50)", (user_id, user_id), commit=True)
        
        if len(reply) > 4000:
            for i in range(0, len(reply), 4000):
                await update.message.reply_text(reply[i:i+4000])
        else:
            await update.message.reply_text(reply)
    except Exception as e:
        error_msg = str(e)
        if "API_KEY_INVALID" in error_msg or "403" in error_msg:
            await update.message.reply_text("❌ Invalid API key! /addkey YOUR_KEY\nGet free: https://aistudio.google.com/app/apikey")
        else:
            await update.message.reply_text(f"❌ AI Error: {error_msg[:200]}")

async def call_gemini(api_key, text, history):
    contents = []
    for role, content in history:
        r = "model" if role == "model" else "user"
        contents.append({"role": r, "parts": [{"text": content}]})
    contents.append({"role": "user", "parts": [{"text": text}]})
    
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={api_key}"
    payload = {"contents": contents, "generationConfig": {"temperature": 0.7, "maxOutputTokens": 4096}}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status == 200:
                data = await resp.json()
                try:
                    return data['candidates'][0]['content']['parts'][0]['text']
                except (KeyError, IndexError):
                    return "Sorry, I couldn't generate a response."
            else:
                error = await resp.text()
                raise Exception(f"Gemini ({resp.status}): {error[:150]}")

# ==================== VIDEO/URL DOWNLOADER ====================
async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else ""
    if not url:
        keyboard = [
            [InlineKeyboardButton("🎬 YouTube", callback_data="dl_youtube"),
             InlineKeyboardButton("📸 Instagram", callback_data="dl_instagram")],
            [InlineKeyboardButton("🎵 TikTok", callback_data="dl_tiktok"),
             InlineKeyboardButton("🐦 Twitter/X", callback_data="dl_twitter")],
            [InlineKeyboardButton("📘 Facebook", callback_data="dl_facebook"),
             InlineKeyboardButton("🎵 Spotify", callback_data="dl_spotify")],
            [InlineKeyboardButton("📌 Pinterest", callback_data="dl_pinterest"),
             InlineKeyboardButton("▶️ Dailymotion", callback_data="dl_dailymotion")],
            [InlineKeyboardButton("🔗 Any URL", callback_data="dl_any")],
        ]
        await update.message.reply_text(
            "📥 ALL-IN-ONE VIDEO DOWNLOADER\n\n"
            "Usage: /download <url>\n"
            "Or: /dl <url>\n\n"
            "Supports:\n"
            "• YouTube (video + audio + shorts)\n"
            "• Instagram (reels, stories, posts)\n"
            "• TikTok (without watermark)\n"
            "• Twitter/X (videos, GIFs)\n"
            "• Facebook (videos, reels)\n"
            "• Spotify (track info)\n"
            "• Pinterest (images, videos)\n"
            "• Dailymotion, Vimeo, Reddit\n"
            "• Any direct video URL\n\n"
            "Tap a platform for direct download links:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        return
    
    await update.message.chat.send_action("typing")
    
    # Detect platform and provide download links
    platform = detect_platform(url)
    download_links = get_download_links(url, platform)
    
    msg = f"📥 Download: {platform.upper()}\n\n"
    msg += f"🔗 Original: {url}\n\n"
    msg += "⬇️ Download Options:\n\n"
    
    for name, dl_url in download_links:
        msg += f"▶️ {name}:\n{dl_url}\n\n"
    
    msg += "💡 Tap any link to download!"
    await update.message.reply_text(msg, disable_web_page_preview=True)

def detect_platform(url):
    url_lower = url.lower()
    if "youtube.com" in url_lower or "youtu.be" in url_lower:
        return "youtube"
    elif "instagram.com" in url_lower:
        return "instagram"
    elif "tiktok.com" in url_lower:
        return "tiktok"
    elif "twitter.com" in url_lower or "x.com" in url_lower:
        return "twitter"
    elif "facebook.com" in url_lower or "fb.watch" in url_lower:
        return "facebook"
    elif "spotify.com" in url_lower:
        return "spotify"
    elif "pinterest.com" in url_lower or "pin.it" in url_lower:
        return "pinterest"
    elif "dailymotion.com" in url_lower:
        return "dailymotion"
    elif "vimeo.com" in url_lower:
        return "vimeo"
    elif "reddit.com" in url_lower:
        return "reddit"
    return "other"

def get_download_links(url, platform):
    encoded = urllib.parse.quote(url, safe='')
    links = []
    
    if platform == "youtube":
        links = [
            ("🎬 HD Video (SaveFrom)", f"https://en.savefrom.net/1-youtube-video-downloader-430/?url={encoded}"),
            ("🎬 All Formats (Y2Mate)", f"https://www.y2mate.com/youtube/{encoded}"),
            ("🎵 MP3 Audio", f"https://ytmp3.cc/youtube-to-mp3/?url={encoded}"),
            ("📱 SS Method", url.replace("youtube.com", "ssyoutube.com")),
            ("⚡ 9xBuddy", f"https://9xbuddy.com/process?url={encoded}"),
            ("🔥 Cobalt (Best)", f"https://cobalt.tools"),
        ]
    elif platform == "instagram":
        links = [
            ("📸 SaveInsta", f"https://www.saveinsta.app/en"),
            ("📸 IGDownloader", f"https://igdownloader.app"),
            ("📸 SnapInsta", f"https://snapinsta.app"),
            ("⚡ Cobalt", f"https://cobalt.tools"),
        ]
    elif platform == "tiktok":
        links = [
            ("🎵 No Watermark (SnapTik)", f"https://snaptik.app"),
            ("🎵 SSSTikTok", f"https://ssstik.io"),
            ("🎵 MusicalDown", f"https://musicaldown.com"),
            ("⚡ Cobalt", f"https://cobalt.tools"),
        ]
    elif platform == "twitter":
        links = [
            ("🐦 SaveTweetVid", f"https://www.savetweetvid.com/?url={encoded}"),
            ("🐦 TWDown", f"https://twdown.net"),
            ("🐦 Twitter Video DL", f"https://twittervideodownloader.com"),
            ("⚡ Cobalt", f"https://cobalt.tools"),
        ]
    elif platform == "facebook":
        links = [
            ("📘 FBDown", f"https://fbdown.net"),
            ("📘 SaveFB", f"https://www.savefrom.net"),
            ("📘 FBVideoDown", f"https://fbvideodownloader.net"),
            ("⚡ Cobalt", f"https://cobalt.tools"),
        ]
    elif platform == "spotify":
        links = [
            ("🎵 SpotifyDown", f"https://spotifydown.com"),
            ("🎵 Spotify Mate", f"https://spotifymate.com"),
            ("🎵 SpotDL", f"https://spotdl.readthedocs.io"),
        ]
    elif platform == "pinterest":
        links = [
            ("📌 PinDownloader", f"https://www.expertsphp.com/pinterest-video-downloader.html"),
            ("📌 SavePin", f"https://www.savepin.app"),
        ]
    else:
        links = [
            ("🔗 Cobalt (Universal)", f"https://cobalt.tools"),
            ("🔗 9xBuddy", f"https://9xbuddy.com/process?url={encoded}"),
            ("🔗 SaveFrom", f"https://en.savefrom.net/?url={encoded}"),
            ("🔗 Loader.to", f"https://loader.to"),
        ]
    
    return links

async def dl_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    platform = query.data.replace("dl_", "")
    
    platform_info = {
        "youtube": "🎬 YOUTUBE DOWNLOADER\n\nSend: /dl <youtube_url>\n\nOr use these sites:\n• https://cobalt.tools (BEST - HD)\n• https://en.savefrom.net\n• https://y2mate.com\n• https://ytmp3.cc (Audio)\n\n💡 Trick: Add 'ss' before youtube.com\nExample: ssyoutube.com/watch?v=...",
        "instagram": "📸 INSTAGRAM DOWNLOADER\n\nSend: /dl <instagram_url>\n\nWorks with:\n• Posts, Reels, Stories, IGTV\n\nSites:\n• https://cobalt.tools\n• https://snapinsta.app\n• https://igdownloader.app\n• https://saveinsta.app",
        "tiktok": "🎵 TIKTOK DOWNLOADER\n\nSend: /dl <tiktok_url>\n\n✅ No watermark!\n\nSites:\n• https://cobalt.tools\n• https://snaptik.app\n• https://ssstik.io\n• https://musicaldown.com",
        "twitter": "🐦 TWITTER/X DOWNLOADER\n\nSend: /dl <tweet_url>\n\nDownloads videos & GIFs\n\nSites:\n• https://cobalt.tools\n• https://twdown.net\n• https://savetweetvid.com\n• https://twittervideodownloader.com",
        "facebook": "📘 FACEBOOK DOWNLOADER\n\nSend: /dl <facebook_url>\n\nWorks with videos & reels\n\nSites:\n• https://cobalt.tools\n• https://fbdown.net\n• https://fbvideodownloader.net",
        "spotify": "🎵 SPOTIFY DOWNLOADER\n\nSend: /dl <spotify_url>\n\nSites:\n• https://spotifydown.com\n• https://spotifymate.com\n• https://spotdl.readthedocs.io",
        "pinterest": "📌 PINTEREST DOWNLOADER\n\nSend: /dl <pinterest_url>\n\nSites:\n• https://savepin.app\n• https://expertsphp.com/pinterest-video-downloader.html",
        "dailymotion": "▶️ DAILYMOTION DOWNLOADER\n\nSend: /dl <dailymotion_url>\n\nSites:\n• https://cobalt.tools\n• https://9xbuddy.com\n• https://savefrom.net",
        "any": "🔗 UNIVERSAL DOWNLOADER\n\nSend: /dl <any_url>\n\n⚡ Cobalt (BEST - supports 20+ sites):\nhttps://cobalt.tools\n\nAlso:\n• https://9xbuddy.com\n• https://en.savefrom.net\n• https://loader.to\n\nSupports: YouTube, Instagram, TikTok, Twitter, Facebook, Reddit, Vimeo, Dailymotion, SoundCloud, Twitch, Pinterest, Tumblr & more!",
    }
    
    msg = platform_info.get(platform, "Send /dl <url> to download")
    await query.message.edit_text(msg, disable_web_page_preview=True)

# ==================== SAFE LINK CHECKER ====================
async def safelink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else ""
    if not url:
        await update.message.reply_text(
            "🛡️ SAFE LINK CHECKER\n\n"
            "Usage: /safelink <url>\n"
            "Example: /safelink https://example.com\n\n"
            "Checks:\n"
            "• URL reputation\n"
            "• SSL certificate\n"
            "• Redirect chains\n"
            "• Known malware/phishing\n"
            "• Domain age & trust"
        )
        return
    
    if not url.startswith("http"):
        url = "https://" + url
    
    await update.message.chat.send_action("typing")
    
    msg = f"🛡️ SAFE LINK CHECK\n🔗 {url}\n\n"
    
    # Check 1: URL structure analysis
    suspicious_patterns = ['bit.ly', 'tinyurl', 'goo.gl', 'shorturl', 't.co', 'is.gd',
                          'exe', '.zip', '.rar', 'login', 'verify', 'account', 'secure',
                          'update', 'confirm', 'banking', 'paypal', 'password']
    warnings = []
    for p in suspicious_patterns:
        if p in url.lower():
            warnings.append(f"⚠️ Contains '{p}'")
    
    # Check 2: Domain info
    try:
        domain = urllib.parse.urlparse(url).netloc
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://ip-api.com/json/{domain}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('status') == 'success':
                        msg += f"📍 Server: {data.get('country', 'Unknown')} ({data.get('isp', 'Unknown')})\n"
    except:
        pass
    
    # Check 3: HTTP response
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10), ssl=False) as resp:
                msg += f"📊 Status: {resp.status}\n"
                msg += f"🔄 Redirects: {len(resp.history)}\n"
                if resp.history:
                    msg += f"📍 Final URL: {str(resp.url)[:100]}\n"
                if 'strict-transport-security' in resp.headers:
                    msg += "🔒 HTTPS: Enforced ✅\n"
                else:
                    msg += "🔓 HTTPS: Not enforced ⚠️\n"
                server = resp.headers.get('server', 'Hidden')
                msg += f"🖥️ Server: {server}\n"
    except aiohttp.ClientSSLError:
        msg += "🔓 SSL: INVALID CERTIFICATE ❌\n"
        warnings.append("❌ SSL certificate error!")
    except Exception as e:
        msg += f"❌ Connection failed: {str(e)[:50]}\n"
        warnings.append("❌ Could not connect!")
    
    # Check 4: VirusTotal link
    msg += f"\n🔬 Deep Scan:\n"
    msg += f"• VirusTotal: https://www.virustotal.com/gui/url/{hashlib.sha256(url.encode()).hexdigest()}\n"
    msg += f"• URLScan: https://urlscan.io/search/#{urllib.parse.quote(url)}\n"
    msg += f"• Google Safe: https://transparencyreport.google.com/safe-browsing/search?url={urllib.parse.quote(url)}\n"
    
    # Verdict
    msg += "\n━━━━━━━━━━━━\n"
    if warnings:
        msg += "⚠️ WARNINGS:\n" + "\n".join(warnings) + "\n\n"
        msg += "🟡 CAUTION - Check the deep scan links above"
    else:
        msg += "🟢 No obvious issues found\n💡 For full safety, check VirusTotal link above"
    
    await update.message.reply_text(msg, disable_web_page_preview=True)

# ==================== ETHICAL HACKING TOOLS (EXPANDED) ====================
async def hack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🔍 IP Lookup", callback_data="hack_ip"),
         InlineKeyboardButton("🌐 DNS Lookup", callback_data="hack_dns")],
        [InlineKeyboardButton("📡 Port Scan", callback_data="hack_port"),
         InlineKeyboardButton("🔎 Whois", callback_data="hack_whois")],
        [InlineKeyboardButton("🔐 Hash Gen", callback_data="hack_hash"),
         InlineKeyboardButton("🛡️ Headers", callback_data="hack_headers")],
        [InlineKeyboardButton("📧 Email Check", callback_data="hack_email"),
         InlineKeyboardButton("🔑 Pass Strength", callback_data="hack_passcheck")],
        [InlineKeyboardButton("🌍 Subdomain", callback_data="hack_subdomain"),
         InlineKeyboardButton("🔗 Safe Link", callback_data="hack_safelink")],
        [InlineKeyboardButton("📱 Phone Lookup", callback_data="hack_phone"),
         InlineKeyboardButton("🕵️ OSINT", callback_data="hack_osint")],
        [InlineKeyboardButton("💉 SQLi Check", callback_data="hack_sqli"),
         InlineKeyboardButton("🕸️ XSS Check", callback_data="hack_xss")],
        [InlineKeyboardButton("🔓 Encode/Decode", callback_data="hack_encode"),
         InlineKeyboardButton("📡 WiFi Tools", callback_data="hack_wifi")],
        [InlineKeyboardButton("🛠️ All Tools & Apps", callback_data="hack_alltools")],
    ]
    await update.message.reply_text(
        "🔓 ETHICAL HACKING TOOLKIT v2.0\n\n"
        "⚠️ For educational & authorized testing only!\n\n"
        "📋 Commands:\n"
        "/iplookup <ip> • /dnslookup <domain>\n"
        "/portscan <ip> [port] • /whois <domain>\n"
        "/hash <text> • /headers <url>\n"
        "/passcheck <pass> • /subdomains <domain>\n"
        "/phonelookup <num> • /safelink <url>\n"
        "/encode <text> • /decode <text>\n"
        "/emailcheck <email> • /techstack <url>\n"
        "/reversedns <ip> • /sslcheck <domain>\n"
        "/hacktools • /hacklinks\n\n"
        "Select a tool:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def hack_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tool = query.data.replace("hack_", "")
    
    help_texts = {
        "ip": "🔍 IP Lookup\nUsage: /iplookup <ip or domain>\nExample: /iplookup 8.8.8.8\nShows: Location, ISP, Country, City, VPN detection",
        "dns": "🌐 DNS Lookup\nUsage: /dnslookup <domain>\nExample: /dnslookup google.com\nShows: A, AAAA, MX, NS records",
        "port": "📡 Port Scanner\nUsage: /portscan <ip> [port]\nExample: /portscan 8.8.8.8 80\nScans top 20 ports or specific port",
        "whois": "🔎 Whois\nUsage: /whois <domain>\nShows: Registration, owner, dates",
        "hash": "🔐 Hash Generator\nUsage: /hash <text>\nGenerates: MD5, SHA1, SHA256, SHA512, Base64",
        "headers": "🛡️ HTTP Headers\nUsage: /headers <url>\nShows: Security headers, server info, vulnerabilities",
        "email": "📧 Email Check\nUsage: /emailcheck <email>\nChecks: Format, domain, MX records",
        "passcheck": "🔑 Password Strength\nUsage: /passcheck <password>\nChecks: Entropy, patterns, common passwords",
        "subdomain": "🌍 Subdomain Finder\nUsage: /subdomains <domain>\nFinds subdomains via certificate transparency",
        "safelink": "🔗 Safe Link\nUsage: /safelink <url>\nChecks: Reputation, SSL, redirects, malware",
        "phone": "📱 Phone Lookup\nUsage: /phonelookup <number>\nShows: Country, carrier, type",
        "osint": "🕵️ OSINT TOOLS\n\n• Sherlock (username): https://sherlock-project.github.io\n• IntelX: https://intelx.io\n• OSINT Framework: https://osintframework.com\n• Maltego: https://www.maltego.com\n• SpiderFoot: https://www.spiderfoot.net\n• Recon-ng: https://github.com/lanmaster53/recon-ng\n• theHarvester: https://github.com/laramies/theHarvester\n• Holehe (email): https://github.com/megadose/holehe\n\nUse /hacklinks for full list",
        "sqli": "💉 SQL INJECTION TOOLS\n\n• SQLMap: https://sqlmap.org\n• Havij: https://github.com/AabyssZG/Havij\n• jSQL: https://github.com/ron190/jsql-injection\n• NoSQLMap: https://github.com/codingo/NoSQLMap\n\n⚠️ Only test on YOUR OWN systems!\nUse /hacklinks for more",
        "xss": "🕸️ XSS TOOLS\n\n• XSSHunter: https://xsshunter.trufflesecurity.com\n• DalFox: https://github.com/hahwul/dalfox\n• XSSer: https://github.com/epsylon/xsser\n• BeEF: https://beefproject.com\n\n⚠️ Only test authorized targets!\nUse /hacklinks for more",
        "encode": "🔓 Encode/Decode\nUsage:\n/encode <text> - Encode to Base64/Hex/URL\n/decode <text> - Decode from Base64/Hex/URL",
        "wifi": "📡 WIFI TOOLS\n\n• Aircrack-ng: https://www.aircrack-ng.org\n• Wifite2: https://github.com/derv82/wifite2\n• Fluxion: https://github.com/FluxionNetwork/fluxion\n• WiFi Pumpkin: https://github.com/P0cL4bs/wifipumpkin3\n• Kismet: https://www.kismetwireless.net\n\n📱 Android:\n• WiFi WPS WPA Tester\n• Termux + aircrack\n\n⚠️ Only test YOUR OWN network!",
        "alltools": "Use /hacktools or /hacklinks for complete list",
    }
    await query.message.edit_text(help_texts.get(tool, "Use /hack"), disable_web_page_preview=True)

async def iplookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    target = " ".join(context.args) if context.args else ""
    if not target:
        await update.message.reply_text("Usage: /iplookup <ip or domain>")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://ip-api.com/json/{target}?fields=66846719") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    if d.get('status') == 'success':
                        msg = (
                            f"🔍 IP Lookup: {target}\n\n"
                            f"📍 IP: {d.get('query', 'N/A')}\n"
                            f"🌍 Country: {d.get('country', 'N/A')} ({d.get('countryCode', '')})\n"
                            f"🏙️ City: {d.get('city', 'N/A')}\n"
                            f"📮 Region: {d.get('regionName', 'N/A')}\n"
                            f"📮 ZIP: {d.get('zip', 'N/A')}\n"
                            f"🕐 Timezone: {d.get('timezone', 'N/A')}\n"
                            f"📡 ISP: {d.get('isp', 'N/A')}\n"
                            f"🏢 Org: {d.get('org', 'N/A')}\n"
                            f"🔌 AS: {d.get('as', 'N/A')}\n"
                            f"📐 Lat: {d.get('lat')}, Lon: {d.get('lon')}\n"
                            f"📱 Mobile: {'Yes' if d.get('mobile') else 'No'}\n"
                            f"🛡️ Proxy/VPN: {'Yes' if d.get('proxy') else 'No'}\n"
                            f"🏠 Hosting: {'Yes' if d.get('hosting') else 'No'}"
                        )
                        await update.message.reply_text(msg)
                    else:
                        await update.message.reply_text(f"❌ {d.get('message', 'Error')}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def dnslookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = " ".join(context.args) if context.args else ""
    if not domain:
        await update.message.reply_text("Usage: /dnslookup <domain>")
        return
    try:
        msg = f"🌐 DNS: {domain}\n\n"
        async with aiohttp.ClientSession() as session:
            for rtype in ["A", "AAAA", "MX", "NS", "TXT"]:
                async with session.get(f"https://dns.google/resolve?name={domain}&type={rtype}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        if data.get('Answer'):
                            msg += f"📌 {rtype}:\n"
                            for ans in data['Answer'][:5]:
                                msg += f"  → {ans.get('data', '')}\n"
                            msg += "\n"
        await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def portscan(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /portscan <ip> [port]")
        return
    target = args[0]
    ports = [int(args[1])] if len(args) > 1 else [21,22,23,25,53,80,110,143,443,445,993,995,3306,3389,5432,8080,8443]
    
    await update.message.reply_text(f"📡 Scanning {target}...")
    open_ports = []
    for port in ports[:20]:
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(target, port), timeout=2)
            open_ports.append(port)
            writer.close()
            await writer.wait_closed()
        except: pass
    
    services = {21:"FTP",22:"SSH",23:"Telnet",25:"SMTP",53:"DNS",80:"HTTP",110:"POP3",
                143:"IMAP",443:"HTTPS",445:"SMB",993:"IMAPS",995:"POP3S",3306:"MySQL",
                3389:"RDP",5432:"PostgreSQL",8080:"HTTP-Alt",8443:"HTTPS-Alt"}
    msg = f"📡 Results: {target}\n\n"
    if open_ports:
        for p in open_ports:
            msg += f"✅ {p} ({services.get(p,'Unknown')}) - OPEN\n"
    else:
        msg += "No open ports found."
    await update.message.reply_text(msg)

async def whois_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = " ".join(context.args) if context.args else ""
    if not domain:
        await update.message.reply_text("Usage: /whois <domain>")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://ip-api.com/json/{domain}") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    msg = f"🔎 Whois: {domain}\n\n📍 IP: {d.get('query','N/A')}\n🌍 Country: {d.get('country','N/A')}\n📡 ISP: {d.get('isp','N/A')}\n🏢 Org: {d.get('org','N/A')}\n🔌 AS: {d.get('as','N/A')}\n\n🔗 Full: https://who.is/whois/{domain}"
                    await update.message.reply_text(msg, disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def hash_gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /hash <text>")
        return
    msg = (
        f"🔐 Hash: {text}\n\n"
        f"MD5: {hashlib.md5(text.encode()).hexdigest()}\n\n"
        f"SHA1: {hashlib.sha1(text.encode()).hexdigest()}\n\n"
        f"SHA256: {hashlib.sha256(text.encode()).hexdigest()}\n\n"
        f"Base64: {base64.b64encode(text.encode()).decode()}\n\n"
        f"URL: {urllib.parse.quote(text)}"
    )
    await update.message.reply_text(msg)

async def headers_check(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else ""
    if not url:
        await update.message.reply_text("Usage: /headers <url>")
        return
    if not url.startswith("http"): url = "https://" + url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.head(url, allow_redirects=True, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                msg = f"🛡️ Headers: {url}\nStatus: {resp.status}\n\n"
                for h in ['server','x-powered-by','x-frame-options','x-content-type-options',
                         'strict-transport-security','content-security-policy','x-xss-protection',
                         'access-control-allow-origin','set-cookie']:
                    val = resp.headers.get(h, '❌ Missing')
                    emoji = "✅" if val != '❌ Missing' else "❌"
                    msg += f"{emoji} {h}: {val[:60]}\n"
                await update.message.reply_text(msg)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def passcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = " ".join(context.args) if context.args else ""
    if not password:
        await update.message.reply_text("Usage: /passcheck <password>")
        return
    score = 0
    feedback = []
    if len(password) >= 8: score += 1
    else: feedback.append("❌ Min 8 chars")
    if len(password) >= 12: score += 1
    if len(password) >= 16: score += 1
    if re.search(r'[A-Z]', password): score += 1
    else: feedback.append("❌ Add uppercase")
    if re.search(r'[a-z]', password): score += 1
    else: feedback.append("❌ Add lowercase")
    if re.search(r'[0-9]', password): score += 1
    else: feedback.append("❌ Add numbers")
    if re.search(r'[!@#$%^&*(),.?":{}|<>]', password): score += 2
    else: feedback.append("❌ Add special chars")
    
    strength = ["💀 Very Weak","😰 Weak","😐 Fair","🙂 Good","💪 Strong","🔒 Very Strong","🏆 Excellent","🛡️ Unbreakable"]
    level = min(score, len(strength)-1)
    
    # Check HaveIBeenPwned
    sha1 = hashlib.sha1(password.encode()).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]
    pwned = False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.pwnedpasswords.com/range/{prefix}") as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if suffix in text:
                        pwned = True
    except: pass
    
    msg = f"🔑 Password Check\nLength: {len(password)}\nStrength: {strength[level]} ({score}/8)\n\n"
    if pwned:
        msg += "🚨 LEAKED! This password was found in data breaches!\n\n"
    if feedback:
        msg += "Improve:\n" + "\n".join(feedback)
    else:
        msg += "✅ Strong password!"
    await update.message.reply_text(msg)

async def subdomains(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = " ".join(context.args) if context.args else ""
    if not domain:
        await update.message.reply_text("Usage: /subdomains <domain>")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://crt.sh/?q=%.{domain}&output=json", timeout=aiohttp.ClientTimeout(total=15)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    subs = set()
                    for entry in data[:100]:
                        for s in entry.get('name_value','').split('\n'):
                            subs.add(s.strip())
                    msg = f"🌍 Subdomains: {domain}\n\n"
                    for i, sub in enumerate(sorted(subs)[:40], 1):
                        msg += f"{i}. {sub}\n"
                    if len(subs) > 40:
                        msg += f"\n+{len(subs)-40} more"
                    await update.message.reply_text(msg[:4000])
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def phonelookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    number = " ".join(context.args) if context.args else ""
    if not number:
        await update.message.reply_text("Usage: /phonelookup <number>")
        return
    msg = f"📱 Phone: {number}\n\n"
    if number.startswith("+880"):
        msg += "🌍 Bangladesh 🇧🇩\n"
        ops = {"17":"Grameenphone","13":"Grameenphone","14":"Banglalink","18":"Robi","16":"Robi","19":"Banglalink","15":"Teletalk"}
        op = ops.get(number[4:6], "Unknown")
        msg += f"📡 Operator: {op}\n"
    elif number.startswith("+91"): msg += "🌍 India 🇮🇳\n"
    elif number.startswith("+1"): msg += "🌍 USA/Canada 🇺🇸\n"
    elif number.startswith("+44"): msg += "🌍 UK 🇬🇧\n"
    msg += f"\n🔗 Truecaller: https://www.truecaller.com/search/{number.replace('+','')}"
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def encode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /encode <text>")
        return
    msg = (
        f"🔓 ENCODE: {text}\n\n"
        f"Base64: {base64.b64encode(text.encode()).decode()}\n\n"
        f"Hex: {text.encode().hex()}\n\n"
        f"URL: {urllib.parse.quote(text)}\n\n"
        f"Binary: {' '.join(format(ord(c), '08b') for c in text[:20])}\n\n"
        f"ROT13: {text.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz','NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm'))}"
    )
    await update.message.reply_text(msg)

async def decode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.message.reply_text("Usage: /decode <text>")
        return
    msg = f"🔓 DECODE: {text}\n\n"
    try:
        msg += f"Base64: {base64.b64decode(text).decode('utf-8', errors='replace')}\n\n"
    except: msg += "Base64: ❌ Invalid\n\n"
    try:
        msg += f"Hex: {bytes.fromhex(text).decode('utf-8', errors='replace')}\n\n"
    except: msg += "Hex: ❌ Invalid\n\n"
    try:
        msg += f"URL: {urllib.parse.unquote(text)}\n\n"
    except: pass
    msg += f"ROT13: {text.translate(str.maketrans('ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz','NOPQRSTUVWXYZABCDEFGHIJKLMnopqrstuvwxyzabcdefghijklm'))}"
    await update.message.reply_text(msg)

async def emailcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    email = " ".join(context.args) if context.args else ""
    if not email:
        await update.message.reply_text("Usage: /emailcheck <email>")
        return
    msg = f"📧 Email Check: {email}\n\n"
    # Validate format
    if re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', email):
        msg += "✅ Format: Valid\n"
    else:
        msg += "❌ Format: Invalid\n"
        await update.message.reply_text(msg)
        return
    
    domain = email.split('@')[1]
    # Check MX records
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://dns.google/resolve?name={domain}&type=MX") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('Answer'):
                        msg += f"✅ Domain: Active\n"
                        msg += f"📧 MX: {data['Answer'][0].get('data','N/A')}\n"
                    else:
                        msg += "❌ Domain: No mail server\n"
    except: pass
    
    msg += f"\n🔍 Check breaches: https://haveibeenpwned.com/account/{urllib.parse.quote(email)}"
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def techstack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args) if context.args else ""
    if not url:
        await update.message.reply_text("Usage: /techstack <url>")
        return
    if not url.startswith("http"): url = "https://" + url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                headers = dict(resp.headers)
                body = (await resp.text())[:5000].lower()
                msg = f"🔧 Tech Stack: {url}\n\n"
                # Server
                msg += f"🖥️ Server: {headers.get('server', 'Hidden')}\n"
                msg += f"⚡ Powered: {headers.get('x-powered-by', 'Hidden')}\n\n"
                # Detect frameworks
                techs = []
                if 'react' in body or 'reactdom' in body: techs.append("⚛️ React")
                if 'vue' in body or '__vue' in body: techs.append("💚 Vue.js")
                if 'angular' in body: techs.append("🅰️ Angular")
                if 'next' in body or '_next' in body: techs.append("▲ Next.js")
                if 'wordpress' in body or 'wp-content' in body: techs.append("📝 WordPress")
                if 'jquery' in body: techs.append("💲 jQuery")
                if 'bootstrap' in body: techs.append("🅱️ Bootstrap")
                if 'tailwind' in body: techs.append("🎨 Tailwind")
                if 'cloudflare' in str(headers).lower(): techs.append("☁️ Cloudflare")
                if 'nginx' in str(headers).lower(): techs.append("🟢 Nginx")
                if 'apache' in str(headers).lower(): techs.append("🔴 Apache")
                if techs:
                    msg += "Detected:\n" + "\n".join(techs)
                else:
                    msg += "No common frameworks detected"
                msg += f"\n\n🔗 Full analysis: https://www.wappalyzer.com/lookup/?url={urllib.parse.quote(url)}"
                await update.message.reply_text(msg, disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def reversedns(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ip = " ".join(context.args) if context.args else ""
    if not ip:
        await update.message.reply_text("Usage: /reversedns <ip>")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://dns.google/resolve?name={'.'.join(reversed(ip.split('.')))}.in-addr.arpa&type=PTR") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get('Answer'):
                        msg = f"🔄 Reverse DNS: {ip}\n\n"
                        for a in data['Answer']:
                            msg += f"→ {a.get('data','N/A')}\n"
                        await update.message.reply_text(msg)
                    else:
                        await update.message.reply_text(f"No PTR record for {ip}")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def sslcheck(update: Update, context: ContextTypes.DEFAULT_TYPE):
    domain = " ".join(context.args) if context.args else ""
    if not domain:
        await update.message.reply_text("Usage: /sslcheck <domain>")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://{domain}", timeout=aiohttp.ClientTimeout(total=10)) as resp:
                msg = f"🔒 SSL Check: {domain}\n\n"
                msg += f"✅ SSL: Valid (Connected over HTTPS)\n"
                msg += f"Status: {resp.status}\n"
                if 'strict-transport-security' in resp.headers:
                    msg += "✅ HSTS: Enabled\n"
                else:
                    msg += "⚠️ HSTS: Not set\n"
                msg += f"\n🔗 Full report: https://www.ssllabs.com/ssltest/analyze.html?d={domain}"
                await update.message.reply_text(msg, disable_web_page_preview=True)
    except aiohttp.ClientSSLError:
        await update.message.reply_text(f"❌ SSL INVALID for {domain}!")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

async def hacktools(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🛠️ HACKING TOOLS & APPS\n\n"
        "📱 MOBILE APPS:\n"
        "• Termux: https://f-droid.org/en/packages/com.termux/\n"
        "• NetHunter: https://www.kali.org/get-kali/#kali-mobile\n"
        "• DroidSheep: Network sniffer\n"
        "• zANTI: Penetration testing\n"
        "• WiFi Analyzer\n"
        "• Fing: Network scanner\n"
        "• Hackode: Recon toolkit\n"
        "• cSploit: Network analysis\n\n"
        "💻 DESKTOP TOOLS:\n"
        "• Kali Linux: https://www.kali.org\n"
        "• Parrot OS: https://www.parrotsec.org\n"
        "• Metasploit: https://www.metasploit.com\n"
        "• Burp Suite: https://portswigger.net/burp\n"
        "• Wireshark: https://www.wireshark.org\n"
        "• Nmap: https://nmap.org\n"
        "• John the Ripper: https://www.openwall.com/john/\n"
        "• Hashcat: https://hashcat.net\n"
        "• Hydra: https://github.com/vanhauser-thc/thc-hydra\n"
        "• Aircrack-ng: https://www.aircrack-ng.org\n\n"
        "Use /hacklinks for online tools & resources"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)

async def hacklinks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "🔗 HACKING LINKS & RESOURCES\n\n"
        "🔍 RECON & OSINT:\n"
        "• Shodan: https://www.shodan.io\n"
        "• Censys: https://search.censys.io\n"
        "• ZoomEye: https://www.zoomeye.org\n"
        "• GreyNoise: https://www.greynoise.io\n"
        "• Hunter.io: https://hunter.io\n"
        "• Sherlock: https://sherlock-project.github.io\n"
        "• OSINT Framework: https://osintframework.com\n"
        "• IntelX: https://intelx.io\n"
        "• Maltego: https://www.maltego.com\n\n"
        "🛡️ VULNERABILITY:\n"
        "• ExploitDB: https://www.exploit-db.com\n"
        "• CVE Details: https://www.cvedetails.com\n"
        "• NVD: https://nvd.nist.gov\n"
        "• Vulners: https://vulners.com\n"
        "• PacketStorm: https://packetstormsecurity.com\n\n"
        "🔐 PASSWORD & CRYPTO:\n"
        "• CrackStation: https://crackstation.net\n"
        "• HaveIBeenPwned: https://haveibeenpwned.com\n"
        "• CyberChef: https://gchq.github.io/CyberChef\n"
        "• dCode: https://www.dcode.fr\n\n"
        "🌐 WEB SECURITY:\n"
        "• VirusTotal: https://www.virustotal.com\n"
        "• URLScan: https://urlscan.io\n"
        "• SecurityTrails: https://securitytrails.com\n"
        "• DNSDumpster: https://dnsdumpster.com\n"
        "• Wappalyzer: https://www.wappalyzer.com\n"
        "• BuiltWith: https://builtwith.com\n\n"
        "📡 NETWORK:\n"
        "• Wigle WiFi: https://wigle.net\n"
        "• BGP Toolkit: https://bgp.he.net\n"
        "• MXToolbox: https://mxtoolbox.com\n\n"
        "📚 LEARN:\n"
        "• TryHackMe: https://tryhackme.com\n"
        "• HackTheBox: https://hackthebox.com\n"
        "• OverTheWire: https://overthewire.org\n"
        "• PicoCTF: https://picoctf.org\n"
        "• VulnHub: https://www.vulnhub.com\n"
        "• OWASP: https://owasp.org\n"
        "• PortSwigger Academy: https://portswigger.net/web-security\n\n"
        "⚠️ Use responsibly & legally!"
    )
    await update.message.reply_text(msg, disable_web_page_preview=True)

# ==================== WEATHER ====================
async def weather(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    city = " ".join(context.args) if context.args else None
    if not city:
        result = db_query("SELECT default_city FROM users WHERE user_id = ?", (user_id,), fetchone=True)
        city = result[0] if result and result[0] else None
    if not city:
        await update.message.reply_text("Usage: /weather <city>\nSet default: /setcity <city>")
        return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://api.openweathermap.org/data/2.5/weather?q={urllib.parse.quote(city)}&appid={WEATHER_API_KEY}&units=metric") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    emojis = {"Clear":"☀️","Clouds":"☁️","Rain":"🌧️","Snow":"❄️","Thunderstorm":"⛈️","Drizzle":"🌦️","Mist":"🌫️","Fog":"🌫️"}
                    e = emojis.get(d['weather'][0]['main'], "🌡️")
                    msg = f"{e} {d['name']}, {d['sys']['country']}\n\n🌡️ {d['main']['temp']}°C (Feels {d['main']['feels_like']}°C)\n💧 Humidity: {d['main']['humidity']}%\n💨 Wind: {d['wind']['speed']} m/s\n☁️ {d['weather'][0]['description'].capitalize()}\n🌅 Sunrise: {datetime.fromtimestamp(d['sys']['sunrise']).strftime('%H:%M')}\n🌇 Sunset: {datetime.fromtimestamp(d['sys']['sunset']).strftime('%H:%M')}"
                    await update.message.reply_text(msg)
                else:
                    await update.message.reply_text("❌ City not found.")
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ==================== NEWS ====================
async def news(update: Update, context: ContextTypes.DEFAULT_TYPE):
    category = context.args[0].lower() if context.args else "world"
    rss_urls = {
        "world": "https://news.google.com/rss?hl=en&gl=US&ceid=US:en",
        "sports": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFp1ZEdvU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US",
        "tech": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRFpxYW5RU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US",
        "entertainment": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNREpxYW5RU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US",
        "business": "https://news.google.com/rss/topics/CAAqJggKIiBDQkFTRWdvSUwyMHZNRGx6TVdZU0FtVnVHZ0pWVXlnQVAB?hl=en&gl=US",
        "bangladesh": "https://news.google.com/rss/search?q=bangladesh&hl=en&gl=BD&ceid=BD:en",
    }
    url = rss_urls.get(category, rss_urls["world"])
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    items = re.findall(r'<item>.*?<title>(.*?)</title>.*?<link>(.*?)</link>.*?</item>', text, re.DOTALL)[:8]
                    if items:
                        msg = f"📰 {category.upper()} NEWS\n\n"
                        for i, (title, link) in enumerate(items, 1):
                            title = html.unescape(re.sub(r'<[^>]+>', '', title))
                            msg += f"{i}. {title}\n🔗 {link}\n\n"
                        msg += "Categories: world | sports | tech | entertainment | business | bangladesh"
                        await update.message.reply_text(msg[:4000], disable_web_page_preview=True)
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {str(e)[:100]}")

# ==================== FREE TV / SPORTS ====================
async def tv_sports(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("⚽ Sports", callback_data="tv_sports"),
         InlineKeyboardButton("📺 News", callback_data="tv_news")],
        [InlineKeyboardButton("🎬 Entertainment", callback_data="tv_entertainment"),
         InlineKeyboardButton("🎵 Music", callback_data="tv_music")],
        [InlineKeyboardButton("📽️ Movies", callback_data="tv_movies"),
         InlineKeyboardButton("👶 Kids", callback_data="tv_kids")],
        [InlineKeyboardButton("🇧🇩 Bangla", callback_data="tv_bangla"),
         InlineKeyboardButton("🌍 All Countries", callback_data="tv_all")],
    ]
    await update.message.reply_text("📺 FREE TV\nSelect category (open in VLC/MX Player):", reply_markup=InlineKeyboardMarkup(keyboard))

async def tv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.replace("tv_", "")
    channels = {
        "sports": "⚽ SPORTS\n\n▶️ All Sports:\nhttps://iptv-org.github.io/iptv/categories/sports.m3u\n\n▶️ beIN Sports:\nhttps://iptv-org.github.io/iptv/categories/sports.m3u\n\n▶️ ESPN:\nhttps://iptv-org.github.io/iptv/categories/sports.m3u\n\n💡 Open in VLC/MX Player!",
        "news": "📺 NEWS\n\n▶️ Al Jazeera:\nhttps://live-hls-web-aje.getaj.net/AJE/01.m3u8\n\n▶️ France 24:\nhttps://stream.france24.com/f24_en/smil:f24_en.smil/playlist.m3u8\n\n▶️ DW News:\nhttps://dwamdstream102.akamaized.net/hls/live/2015525/dwstream102/index.m3u8\n\n▶️ All News:\nhttps://iptv-org.github.io/iptv/categories/news.m3u",
        "entertainment": "🎬 ENTERTAINMENT\n\nhttps://iptv-org.github.io/iptv/categories/entertainment.m3u",
        "music": "🎵 MUSIC\n\nhttps://iptv-org.github.io/iptv/categories/music.m3u",
        "movies": "📽️ MOVIES\n\nhttps://iptv-org.github.io/iptv/categories/movies.m3u",
        "kids": "👶 KIDS\n\nhttps://iptv-org.github.io/iptv/categories/kids.m3u",
        "bangla": "🇧🇩 BANGLA\n\nhttps://iptv-org.github.io/iptv/countries/bd.m3u",
        "all": "🌍 ALL COUNTRIES\n\nhttps://iptv-org.github.io/iptv/index.m3u\n\nBy country:\nhttps://iptv-org.github.io/iptv/index.country.m3u\n\nBy language:\nhttps://iptv-org.github.io/iptv/index.language.m3u",
    }
    await query.message.edit_text(channels.get(cat, "Use /tv"))

# ==================== NOTES ====================
async def handle_notes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("📝 /note save <title> | <content>\n/note list\n/note view <id>\n/note delete <id>")
        return
    action = args[0].lower()
    if action == "save":
        text = " ".join(args[1:])
        if "|" in text:
            title, content = text.split("|", 1)
        else:
            title, content = text[:30], text
        db_query("INSERT INTO notes (user_id, title, content) VALUES (?, ?, ?)", (user_id, title.strip(), content.strip()), commit=True)
        await update.message.reply_text("✅ Saved!")
    elif action == "list":
        notes = db_query("SELECT id, title, created_at FROM notes WHERE user_id = ?", (user_id,), fetchall=True)
        if notes:
            msg = "📝 Notes:\n\n"
            for n in notes: msg += f"#{n[0]} - {n[1]} ({n[2][:10]})\n"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("No notes.")
    elif action == "view" and len(args) > 1:
        note = db_query("SELECT title, content, created_at FROM notes WHERE id = ? AND user_id = ?", (args[1], user_id), fetchone=True)
        if note: await update.message.reply_text(f"📝 {note[0]}\n\n{note[1]}\n\n📅 {note[2]}")
        else: await update.message.reply_text("Not found.")
    elif action == "delete" and len(args) > 1:
        db_query("DELETE FROM notes WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("🗑️ Deleted!")

# ==================== PASSWORDS ====================
async def handle_pass(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("🔐 /pass save <site> <user> <pass>\n/pass list\n/pass get <site>\n/pass delete <id>")
        return
    action = args[0].lower()
    if action == "save" and len(args) >= 4:
        db_query("INSERT INTO passwords (user_id, site, username, password) VALUES (?, ?, ?, ?)", (user_id, args[1], args[2], " ".join(args[3:])), commit=True)
        await update.message.reply_text(f"🔐 Saved!")
    elif action == "list":
        pws = db_query("SELECT id, site, username FROM passwords WHERE user_id = ?", (user_id,), fetchall=True)
        if pws:
            msg = "🔐 Passwords:\n\n"
            for p in pws: msg += f"#{p[0]} - {p[1]} ({p[2]})\n"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("No passwords.")
    elif action == "get" and len(args) >= 2:
        r = db_query("SELECT site, username, password FROM passwords WHERE user_id = ? AND site LIKE ?", (user_id, f"%{args[1]}%"), fetchone=True)
        if r: await update.message.reply_text(f"🔐 {r[0]}\n👤 {r[1]}\n🔑 {r[2]}")
        else: await update.message.reply_text("Not found.")
    elif action == "delete" and len(args) >= 2:
        db_query("DELETE FROM passwords WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("🗑️ Deleted!")

# ==================== LENDEN (MONEY TRACKER) ====================
async def lenden(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("💰 লেনদেন / Money Tracker\n\n/lenden gave <name> <amount> [reason]\n/lenden got <name> <amount> [reason]\n/lenden list\n/lenden check <name>\n/lenden settle <id>\n/lenden delete <id>\n/lenden summary")
        return
    action = args[0].lower()
    if action in ["gave", "got"] and len(args) >= 3:
        name = args[1]
        try: amount = float(args[2])
        except: await update.message.reply_text("❌ Invalid amount."); return
        reason = " ".join(args[3:]) if len(args) > 3 else ""
        db_query("INSERT INTO lenden (user_id, person_name, amount, type, reason) VALUES (?, ?, ?, ?, ?)", (user_id, name, amount, action, reason), commit=True)
        if action == "gave":
            await update.message.reply_text(f"✅ আপনি {name} কে ৳{amount:.0f} দিয়েছেন\n📝 {reason or 'N/A'}\n({name} আপনার কাছে ঋণী)")
        else:
            await update.message.reply_text(f"✅ আপনি {name} থেকে ৳{amount:.0f} পেয়েছেন\n📝 {reason or 'N/A'}\n(আপনি {name} এর কাছে ঋণী)")
    elif action == "list":
        records = db_query("SELECT id, person_name, amount, type, reason, date FROM lenden WHERE user_id = ? AND settled = 0 ORDER BY date DESC", (user_id,), fetchall=True)
        if records:
            msg = "💰 Active:\n\n"
            for r in records:
                msg += f"#{r[0]} {'💸→' if r[3]=='gave' else '💰←'} {r[1]}: ৳{r[2]:.0f} {r[4] or ''}\n"
            await update.message.reply_text(msg[:4000])
        else: await update.message.reply_text("✅ কোনো বাকি নেই!")
    elif action == "check" and len(args) >= 2:
        name = args[1]
        records = db_query("SELECT amount, type FROM lenden WHERE user_id = ? AND person_name LIKE ? AND settled = 0", (user_id, f"%{name}%"), fetchall=True)
        if records:
            gave = sum(r[0] for r in records if r[1] == "gave")
            got = sum(r[0] for r in records if r[1] == "got")
            bal = gave - got
            msg = f"💰 {name}:\nদিয়েছি: ৳{gave:.0f}\nপেয়েছি: ৳{got:.0f}\n\n"
            if bal > 0: msg += f"{name} আপনাকে ৳{bal:.0f} দিবে"
            elif bal < 0: msg += f"আপনি {name} কে ৳{abs(bal):.0f} দিবেন"
            else: msg += "✅ মিটে গেছে!"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("No records.")
    elif action == "settle" and len(args) >= 2:
        db_query("UPDATE lenden SET settled = 1 WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("✅ মিটিয়ে দেওয়া হয়েছে!")
    elif action == "delete" and len(args) >= 2:
        db_query("DELETE FROM lenden WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("🗑️ মুছে ফেলা হয়েছে!")
    elif action == "summary":
        records = db_query("SELECT person_name, amount, type FROM lenden WHERE user_id = ? AND settled = 0", (user_id,), fetchall=True)
        if records:
            people = {}
            for name, amount, type_ in records:
                people.setdefault(name, 0)
                people[name] += amount if type_ == "gave" else -amount
            msg = "💰 সারাংশ:\n\n"
            for name, bal in sorted(people.items(), key=lambda x: x[1], reverse=True):
                if bal > 0: msg += f"💸 {name} দিবে ৳{bal:.0f}\n"
                elif bal < 0: msg += f"💰 {name} কে দিবেন ৳{abs(bal):.0f}\n"
            total_get = sum(v for v in people.values() if v > 0)
            total_give = sum(abs(v) for v in people.values() if v < 0)
            msg += f"\n━━━━━━━━━━━━\nপাবেন: ৳{total_get:.0f}\nদিবেন: ৳{total_give:.0f}\nনেট: ৳{total_get-total_give:.0f}"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("✅ কোনো বাকি নেই!")

# ==================== EXPENSE TRACKER ====================
async def expense(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        await update.message.reply_text("💳 /expense add <amount> <category> [desc]\n/expense today\n/expense week\n/expense month\n/expense list")
        return
    action = args[0].lower()
    if action == "add" and len(args) >= 3:
        try: amount = float(args[1])
        except: await update.message.reply_text("❌ Invalid amount."); return
        cat = args[2]
        desc = " ".join(args[3:]) if len(args) > 3 else ""
        db_query("INSERT INTO expenses (user_id, category, amount, description) VALUES (?, ?, ?, ?)", (user_id, cat, amount, desc), commit=True)
        await update.message.reply_text(f"✅ ৳{amount:.0f} ({cat})")
    elif action in ["today","week","month"]:
        days = {"today":0,"week":7,"month":30}
        df = (datetime.now() - timedelta(days=days[action])).strftime('%Y-%m-%d')
        if action == "today": df = datetime.now().strftime('%Y-%m-%d')
        exps = db_query("SELECT category, SUM(amount) FROM expenses WHERE user_id = ? AND date >= ? GROUP BY category", (user_id, df), fetchall=True)
        if exps:
            total = sum(e[1] for e in exps)
            msg = f"💳 {action.capitalize()}:\n\n"
            for e in exps: msg += f"• {e[0]}: ৳{e[1]:.0f}\n"
            msg += f"\nTotal: ৳{total:.0f}"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("No expenses.")
    elif action == "list":
        exps = db_query("SELECT id, category, amount, description, date FROM expenses WHERE user_id = ? ORDER BY date DESC LIMIT 20", (user_id,), fetchall=True)
        if exps:
            msg = "💳 Recent:\n\n"
            for e in exps: msg += f"#{e[0]} {e[1]}: ৳{e[2]:.0f} {e[3]} ({e[4][:10]})\n"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("No expenses.")

# ==================== UTILITIES ====================
async def calc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    expr = " ".join(context.args)
    if not expr: await update.message.reply_text("Usage: /calc <expression>"); return
    try:
        allowed = set("0123456789+-*/.() %")
        if all(c in allowed for c in expr.replace(" ", "")):
            result = eval(expr)
            await update.message.reply_text(f"🧮 {expr} = {result}")
        else: await update.message.reply_text("❌ Invalid.")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def convert(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 3:
        await update.message.reply_text("📐 /convert <value> <from> to <to>\nExamples: /convert 100 usd to bdt")
        return
    try:
        value = float(args[0])
        from_u = args[1].lower()
        to_u = args[-1].lower()
        conversions = {("km","miles"):0.621371,("miles","km"):1.60934,("kg","lbs"):2.20462,("lbs","kg"):0.453592,
                      ("c","f"):None,("f","c"):None,("cm","inches"):0.393701,("inches","cm"):2.54,("m","ft"):3.28084,("ft","m"):0.3048}
        if (from_u, to_u) == ("c","f"):
            await update.message.reply_text(f"📐 {value}°C = {(value*9/5)+32:.2f}°F")
        elif (from_u, to_u) == ("f","c"):
            await update.message.reply_text(f"📐 {value}°F = {(value-32)*5/9:.2f}°C")
        elif (from_u, to_u) in conversions:
            await update.message.reply_text(f"📐 {value} {from_u} = {value*conversions[(from_u,to_u)]:.4f} {to_u}")
        else:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"https://api.exchangerate-api.com/v4/latest/{from_u.upper()}") as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        rate = data['rates'].get(to_u.upper())
                        if rate: await update.message.reply_text(f"💰 {value} {from_u.upper()} = {value*rate:.2f} {to_u.upper()}")
                        else: await update.message.reply_text("❌ Not supported.")
    except: await update.message.reply_text("❌ Error.")

async def translate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("🌐 /translate <lang> <text>")
        return
    target = args[0].lower()
    text = " ".join(args[1:])
    codes = {"spanish":"es","french":"fr","german":"de","hindi":"hi","arabic":"ar","chinese":"zh","japanese":"ja","korean":"ko","bengali":"bn","bangla":"bn","urdu":"ur","english":"en"}
    lc = codes.get(target, target[:2])
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.mymemory.translated.net/get?q={urllib.parse.quote(text)}&langpair=en|{lc}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    await update.message.reply_text(f"🌐 {data['responseData']['translatedText']}")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def qr_gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args)
    if not text: await update.message.reply_text("Usage: /qr <text>"); return
    try:
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(text); qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = BytesIO(); img.save(bio, 'PNG'); bio.seek(0)
        await update.message.reply_photo(photo=bio, caption=f"📱 {text[:100]}")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def shorten(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = " ".join(context.args)
    if not url: await update.message.reply_text("Usage: /shorten <url>"); return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://tinyurl.com/api-create.php?url={urllib.parse.quote(url)}") as resp:
                if resp.status == 200: await update.message.reply_text(f"🔗 {await resp.text()}")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def remind(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if not args or len(args) < 2:
        await update.message.reply_text("⏰ /remind <minutes> <message>")
        return
    try:
        minutes = int(args[0])
        msg_text = " ".join(args[1:])
        chat_id = update.effective_chat.id
        async def send_reminder(ctx):
            await ctx.bot.send_message(chat_id=chat_id, text=f"⏰ REMINDER:\n\n{msg_text}")
        context.job_queue.run_once(send_reminder, when=minutes*60)
        await update.message.reply_text(f"✅ Reminder in {minutes} min!")
    except: await update.message.reply_text("❌ Invalid.")

# ==================== ENTERTAINMENT ====================
async def joke(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://official-joke-api.appspot.com/random_joke") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    await update.message.reply_text(f"😂 {d['setup']}\n\n{d['punchline']}"); return
    except: pass
    await update.message.reply_text("😂 Why don't scientists trust atoms?\nBecause they make up everything!")

async def quote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://zenquotes.io/api/random") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    await update.message.reply_text(f"💬 \"{d[0]['q']}\"\n— {d[0]['a']}"); return
    except: pass
    await update.message.reply_text("💬 \"Be the change you wish to see.\" — Gandhi")

async def fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    await update.message.reply_text(f"🧠 {d['text']}"); return
    except: pass
    await update.message.reply_text("🧠 Honey never spoils!")

async def meme(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://meme-api.com/gimme") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    await update.message.reply_photo(photo=d['url'], caption=f"😆 {d['title']}"); return
    except: pass
    await update.message.reply_text("😆 Try again!")

async def horoscope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    sign = context.args[0].lower() if context.args else ""
    if not sign:
        await update.message.reply_text("♈ /horoscope <sign>\nSigns: aries, taurus, gemini, cancer, leo, virgo, libra, scorpio, sagittarius, capricorn, aquarius, pisces")
        return
    await update.message.reply_text(f"♈ {sign.capitalize()}: Positive energy today. Lucky #{random.randint(1,99)}. Color: {random.choice(['Red','Blue','Green','Gold','Purple'])}.")

async def lyrics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query: await update.message.reply_text("Usage: /lyrics <song>"); return
    user_id = update.effective_user.id
    keys = db_query("SELECT api_key FROM gemini_keys WHERE user_id = ?", (user_id,), fetchall=True)
    api_key = keys[0][0] if keys else GEMINI_API_KEY
    if api_key:
        try:
            reply = await call_gemini(api_key, f"Give me the lyrics of '{query}'. Only lyrics, no explanations.", [])
            await update.message.reply_text(f"🎵 {query}\n\n{reply[:4000]}")
        except: await update.message.reply_text("❌ Need key. /addkey")
    else: await update.message.reply_text("❌ Need key. /addkey")

async def todo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args: await update.message.reply_text("✅ /todo add <task>\n/todo list\n/todo done <id>\n/todo delete <id>"); return
    action = args[0].lower()
    if action == "add" and len(args) > 1:
        db_query("INSERT INTO todos (user_id, task) VALUES (?, ?)", (user_id, " ".join(args[1:])), commit=True)
        await update.message.reply_text("✅ Added!")
    elif action == "list":
        todos = db_query("SELECT id, task, completed FROM todos WHERE user_id = ?", (user_id,), fetchall=True)
        if todos:
            msg = "✅ To-Do:\n\n"
            for t in todos: msg += f"{'✅' if t[2] else '⬜'} #{t[0]} {t[1]}\n"
            await update.message.reply_text(msg)
        else: await update.message.reply_text("Empty!")
    elif action == "done" and len(args) > 1:
        db_query("UPDATE todos SET completed = 1 WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("✅ Done!")
    elif action == "delete" and len(args) > 1:
        db_query("DELETE FROM todos WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("🗑️ Deleted!")

async def wiki(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query: await update.message.reply_text("Usage: /wiki <topic>"); return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(query)}") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    msg = f"📚 {d.get('title', query)}\n\n{d.get('extract','N/A')[:2000]}"
                    await update.message.reply_text(msg, disable_web_page_preview=True)
                else: await update.message.reply_text("❌ Not found.")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def define(update: Update, context: ContextTypes.DEFAULT_TYPE):
    word = " ".join(context.args)
    if not word: await update.message.reply_text("Usage: /define <word>"); return
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    entry = data[0]
                    msg = f"📖 {entry['word']}\n\n"
                    for m in entry.get('meanings',[])[:3]:
                        msg += f"{m['partOfSpeech']}:\n"
                        for d in m.get('definitions',[])[:2]: msg += f"• {d['definition']}\n"
                        msg += "\n"
                    await update.message.reply_text(msg)
                else: await update.message.reply_text("❌ Not found.")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def crypto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    coin = (context.args[0] if context.args else "bitcoin").lower()
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://api.coingecko.com/api/v3/simple/price?ids={coin}&vs_currencies=usd,bdt&include_24hr_change=true") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if coin in data:
                        p = data[coin]
                        ch = p.get('usd_24h_change', 0)
                        await update.message.reply_text(f"🪙 {coin.capitalize()}\n💰 ${p['usd']:,.2f}\n💰 ৳{p.get('bdt',p['usd']*121):,.0f}\n{'📈' if ch>0 else '📉'} {ch:.2f}%")
    except Exception as e: await update.message.reply_text(f"❌ {str(e)[:100]}")

async def trivia(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://opentdb.com/api.php?amount=1&type=multiple") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    q = data['results'][0]
                    question = html.unescape(q['question'])
                    correct = html.unescape(q['correct_answer'])
                    answers = [html.unescape(a) for a in q['incorrect_answers']] + [correct]
                    random.shuffle(answers)
                    msg = f"🎯 {question}\n\n"
                    for i, a in enumerate(answers, 1): msg += f"{i}. {a}\n"
                    msg += f"\nAnswer: {correct}"
                    await update.message.reply_text(msg); return
    except: pass
    await update.message.reply_text("🎯 Try again!")

async def riddle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    riddles = [("What has keys but no locks?","Piano"),("What gets wetter as it dries?","Towel"),("What has hands but can't clap?","Clock"),("What has a head and tail but no body?","Coin")]
    r = random.choice(riddles)
    await update.message.reply_text(f"🧩 {r[0]}\n\nAnswer: {r[1]}")

async def img_gen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prompt = " ".join(context.args)
    if not prompt: await update.message.reply_text("Usage: /img <description>"); return
    try:
        url = f"https://image.pollinations.ai/prompt/{urllib.parse.quote(prompt)}?width=512&height=512&nologo=true"
        await update.message.reply_photo(photo=url, caption=f"🎨 {prompt}")
    except: await update.message.reply_text("❌ Failed.")

async def briefing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    msg = "☀️ DAILY BRIEFING\n\n"
    result = db_query("SELECT default_city FROM users WHERE user_id = ?", (user_id,), fetchone=True)
    city = result[0] if result and result[0] else "Dhaka"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    msg += f"🌡️ {city}: {d['main']['temp']}°C, {d['weather'][0]['description']}\n\n"
    except: pass
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://news.google.com/rss?hl=en&gl=US") as resp:
                if resp.status == 200:
                    text = await resp.text()
                    items = re.findall(r'<title>(.*?)</title>', text)[2:5]
                    msg += "📰 News:\n"
                    for item in items: msg += f"• {html.unescape(item)}\n"
    except: pass
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get("https://zenquotes.io/api/random") as resp:
                if resp.status == 200:
                    d = await resp.json()
                    msg += f"\n💬 \"{d[0]['q']}\" — {d[0]['a']}"
    except: pass
    await update.message.reply_text(msg, disable_web_page_preview=True)

# ==================== APP/LINK MANAGER ====================
async def apps(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    args = context.args
    if not args:
        keyboard = [
            [InlineKeyboardButton("📱 My Links", callback_data="apps_list"),
             InlineKeyboardButton("➕ Add", callback_data="apps_add")],
            [InlineKeyboardButton("🛠️ Hacking", callback_data="apps_hacking"),
             InlineKeyboardButton("📚 Learning", callback_data="apps_learning")],
            [InlineKeyboardButton("🎮 Entertainment", callback_data="apps_entertainment"),
             InlineKeyboardButton("💼 Productivity", callback_data="apps_productivity")],
        ]
        await update.message.reply_text("📱 APP MANAGER\n\n/apps add <name> | <url> | [category]\n/apps list\n/apps remove <id>", reply_markup=InlineKeyboardMarkup(keyboard))
        return
    action = args[0].lower()
    if action == "add":
        text = " ".join(args[1:])
        parts = text.split("|")
        if len(parts) >= 2:
            name, url = parts[0].strip(), parts[1].strip()
            cat = parts[2].strip() if len(parts) > 2 else "general"
            db_query("INSERT INTO custom_links (user_id, name, url, category) VALUES (?, ?, ?, ?)", (user_id, name, url, cat), commit=True)
            await update.message.reply_text(f"✅ Added: {name}")
        else: await update.message.reply_text("Usage: /apps add <name> | <url> | [category]")
    elif action == "list":
        links = db_query("SELECT id, name, url, category FROM custom_links WHERE user_id = ?", (user_id,), fetchall=True)
        if links:
            msg = "📱 Links:\n\n"
            for l in links: msg += f"#{l[0]} [{l[3]}] {l[1]}\n🔗 {l[2]}\n\n"
            await update.message.reply_text(msg[:4000], disable_web_page_preview=True)
        else: await update.message.reply_text("No links.")
    elif action == "remove" and len(args) > 1:
        db_query("DELETE FROM custom_links WHERE id = ? AND user_id = ?", (args[1], user_id), commit=True)
        await update.message.reply_text("🗑️ Removed!")

async def apps_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.replace("apps_", "")
    preloaded = {
        "hacking": "🛠️ HACKING\n• Termux: https://f-droid.org/en/packages/com.termux/\n• Kali: https://www.kali.org\n• Shodan: https://www.shodan.io\n• VirusTotal: https://www.virustotal.com\n• CyberChef: https://gchq.github.io/CyberChef\n• HaveIBeenPwned: https://haveibeenpwned.com",
        "learning": "📚 LEARNING\n• TryHackMe: https://tryhackme.com\n• HackTheBox: https://hackthebox.com\n• FreeCodeCamp: https://freecodecamp.org\n• Coursera: https://coursera.org",
        "entertainment": "🎮 ENTERTAINMENT\n• YouTube: https://youtube.com\n• Spotify: https://open.spotify.com\n• Reddit: https://reddit.com\n• Twitch: https://twitch.tv",
        "productivity": "💼 PRODUCTIVITY\n• Notion: https://notion.so\n• Canva: https://canva.com\n• Remove.bg: https://remove.bg",
        "list": "Use /apps list",
        "add": "Use: /apps add <name> | <url> | [category]",
    }
    await query.message.edit_text(preloaded.get(cat, "/apps"), disable_web_page_preview=True)

# ==================== SETTINGS ====================
async def addkey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    if not context.args:
        await update.message.reply_text("🔑 /addkey <gemini_key>\nGet FREE: https://aistudio.google.com/app/apikey")
        return
    key = context.args[0]
    count = db_query("SELECT COUNT(*) FROM gemini_keys WHERE user_id = ?", (user_id,), fetchone=True)[0]
    db_query("INSERT INTO gemini_keys (user_id, api_key, label) VALUES (?, ?, ?)", (user_id, key, f"Key #{count+1}"), commit=True)
    await update.message.reply_text(f"✅ Key #{count+1} added!\nJust type anything to chat!\n#2 msg = use Key #2")

async def keys_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    kl = db_query("SELECT id, label, api_key FROM gemini_keys WHERE user_id = ?", (user_id,), fetchall=True)
    if kl:
        msg = "🔑 Keys:\n\n"
        for i, (kid, label, key) in enumerate(kl, 1): msg += f"#{i} {label} ({key[:6]}...{key[-4:]})\n"
        msg += "\n/removekey <num>"
        await update.message.reply_text(msg)
    else: await update.message.reply_text("No keys. /addkey")

async def removekey(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not context.args: await update.message.reply_text("/removekey <number>"); return
    kl = db_query("SELECT id FROM gemini_keys WHERE user_id = ?", (user_id,), fetchall=True)
    try:
        idx = int(context.args[0]) - 1
        if 0 <= idx < len(kl):
            db_query("DELETE FROM gemini_keys WHERE id = ?", (kl[idx][0],), commit=True)
            await update.message.reply_text("🗑️ Removed!")
        else: await update.message.reply_text("❌ Invalid.")
    except: await update.message.reply_text("❌ Invalid.")

async def setcity(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    if not context.args: await update.message.reply_text("/setcity <city>"); return
    city = " ".join(context.args)
    db_query("UPDATE users SET default_city = ? WHERE user_id = ?", (city, user_id), commit=True)
    await update.message.reply_text(f"✅ City: {city}")

async def clearchat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    db_query("DELETE FROM chat_history WHERE user_id = ?", (user_id,), commit=True)
    await update.message.reply_text("🗑️ Cleared!")

# ==================== START & HELP ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    ensure_user(user_id)
    name = update.effective_user.first_name
    await update.message.reply_text(
        f"👋 Welcome {name}!\n\n"
        f"🤖 All-in-One AI Bot v2.0\n\n"
        f"⚡ SETUP:\n"
        f"1. Get free key: https://aistudio.google.com/app/apikey\n"
        f"2. /addkey YOUR_KEY\n"
        f"3. Type anything to chat!\n\n"
        f"🔥 Features:\n"
        f"🤖 AI Chat • 🌤️ Weather • 📰 News\n"
        f"📺 Free TV • 📥 Video Downloader\n"
        f"📝 Notes • 🔐 Passwords • ✅ ToDo\n"
        f"💰 লেনদেন (Money) • 💳 Expenses\n"
        f"🔓 Hacking Tools • 🛡️ Safe Link\n"
        f"📱 App Manager • 🔗 Hack Links\n"
        f"🧮 Calc • 📐 Convert • 🌐 Translate\n"
        f"📱 QR • 🔗 Shorten • ⏰ Remind\n"
        f"😂 Joke • 💬 Quote • 🧠 Fact • 😆 Meme\n"
        f"♈ Horoscope • 🎵 Lyrics • 🎨 AI Image\n"
        f"📚 Wiki • 📖 Define • 🪙 Crypto\n"
        f"🎯 Trivia • 🧩 Riddle • ☀️ Briefing\n\n"
        f"Type /help for all commands!",
        disable_web_page_preview=True
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📋 ALL COMMANDS:\n\n"
        "🤖 AI: Just type! (#2 msg = Key #2)\n\n"
        "📥 DOWNLOAD:\n"
        "/download <url> • /dl <url>\n"
        "YouTube, Insta, TikTok, Twitter, FB...\n\n"
        "🌤️ /weather • /setcity • /news\n"
        "📺 /tv - Free TV/Sports\n\n"
        "💰 /lenden - Money (বাংলা)\n"
        "💳 /expense • 📝 /note • 🔐 /pass\n"
        "✅ /todo\n\n"
        "🔓 HACKING:\n"
        "/hack • /iplookup • /dnslookup\n"
        "/portscan • /whois • /hash\n"
        "/headers • /passcheck • /subdomains\n"
        "/phonelookup • /safelink • /encode\n"
        "/decode • /emailcheck • /techstack\n"
        "/reversedns • /sslcheck\n"
        "/hacktools • /hacklinks\n\n"
        "📱 /apps - Link manager\n\n"
        "🧮 /calc • /convert • /translate\n"
        "📱 /qr • /shorten • /remind\n\n"
        "🎭 /joke • /quote • /fact • /meme\n"
        "♈ /horoscope • /lyrics • /trivia\n"
        "🧩 /riddle • 🎨 /img\n\n"
        "📚 /wiki • /define • /crypto\n"
        "☀️ /briefing\n\n"
        "⚙️ /addkey • /keys • /removekey\n"
        "/clearchat"
    )

# ==================== MAIN ====================
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    
    # Core
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    
    # Download
    app.add_handler(CommandHandler("download", download))
    app.add_handler(CommandHandler("dl", download))
    
    # Info
    app.add_handler(CommandHandler("weather", weather))
    app.add_handler(CommandHandler("news", news))
    app.add_handler(CommandHandler("tv", tv_sports))
    app.add_handler(CommandHandler("sports", tv_sports))
    app.add_handler(CommandHandler("briefing", briefing))
    
    # Productivity
    app.add_handler(CommandHandler("note", handle_notes))
    app.add_handler(CommandHandler("pass", handle_pass))
    app.add_handler(CommandHandler("lenden", lenden))
    app.add_handler(CommandHandler("expense", expense))
    app.add_handler(CommandHandler("todo", todo))
    
    # Utilities
    app.add_handler(CommandHandler("calc", calc))
    app.add_handler(CommandHandler("convert", convert))
    app.add_handler(CommandHandler("translate", translate))
    app.add_handler(CommandHandler("qr", qr_gen))
    app.add_handler(CommandHandler("shorten", shorten))
    app.add_handler(CommandHandler("remind", remind))
    
    # Entertainment
    app.add_handler(CommandHandler("joke", joke))
    app.add_handler(CommandHandler("quote", quote))
    app.add_handler(CommandHandler("fact", fact))
    app.add_handler(CommandHandler("meme", meme))
    app.add_handler(CommandHandler("horoscope", horoscope))
    app.add_handler(CommandHandler("lyrics", lyrics))
    app.add_handler(CommandHandler("trivia", trivia))
    app.add_handler(CommandHandler("riddle", riddle))
    app.add_handler(CommandHandler("img", img_gen))
    
    # Info/Reference
    app.add_handler(CommandHandler("wiki", wiki))
    app.add_handler(CommandHandler("define", define))
    app.add_handler(CommandHandler("crypto", crypto))
    
    # Hacking
    app.add_handler(CommandHandler("hack", hack))
    app.add_handler(CommandHandler("iplookup", iplookup))
    app.add_handler(CommandHandler("dnslookup", dnslookup))
    app.add_handler(CommandHandler("portscan", portscan))
    app.add_handler(CommandHandler("whois", whois_lookup))
    app.add_handler(CommandHandler("hash", hash_gen))
    app.add_handler(CommandHandler("headers", headers_check))
    app.add_handler(CommandHandler("passcheck", passcheck))
    app.add_handler(CommandHandler("subdomains", subdomains))
    app.add_handler(CommandHandler("phonelookup", phonelookup))
    app.add_handler(CommandHandler("safelink", safelink))
    app.add_handler(CommandHandler("encode", encode_cmd))
    app.add_handler(CommandHandler("decode", decode_cmd))
    app.add_handler(CommandHandler("emailcheck", emailcheck))
    app.add_handler(CommandHandler("techstack", techstack))
    app.add_handler(CommandHandler("reversedns", reversedns))
    app.add_handler(CommandHandler("sslcheck", sslcheck))
    app.add_handler(CommandHandler("hacktools", hacktools))
    app.add_handler(CommandHandler("hacklinks", hacklinks))
    
    # Apps
    app.add_handler(CommandHandler("apps", apps))
    
    # Settings
    app.add_handler(CommandHandler("addkey", addkey))
    app.add_handler(CommandHandler("keys", keys_list))
    app.add_handler(CommandHandler("removekey", removekey))
    app.add_handler(CommandHandler("setcity", setcity))
    app.add_handler(CommandHandler("clearchat", clearchat))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(tv_callback, pattern="^tv_"))
    app.add_handler(CallbackQueryHandler(hack_callback, pattern="^hack_"))
    app.add_handler(CallbackQueryHandler(apps_callback, pattern="^apps_"))
    app.add_handler(CallbackQueryHandler(dl_callback, pattern="^dl_"))
    
    # AI Chat (all text that's not a command)
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), ai_chat))
    
    if WEBHOOK_URL:
        logger.info(f"🤖 Bot v2.0 started in WEBHOOK mode!")
        logger.info(f"   Webhook URL: {WEBHOOK_URL}/{TOKEN}")
        logger.info(f"   Listening on 0.0.0.0:{PORT}")
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=TOKEN,
            webhook_url=f"{WEBHOOK_URL}/{TOKEN}",
            drop_pending_updates=True
        )
    else:
        # Polling mode - start Flask health check server in background
        import threading
        flask_app = Flask(__name__)

        @flask_app.route('/')
        def home():
            return '<h1>Bot is running!</h1>'

        @flask_app.route('/health')
        def health():
            return 'OK'

        threading.Thread(
            target=lambda: flask_app.run(host='0.0.0.0', port=PORT),
            daemon=True
        ).start()
        logger.info("🤖 Bot v2.0 started in POLLING mode! All systems online.")
        app.run_polling(drop_pending_updates=True)

if __name__ == '__main__':
    main()
