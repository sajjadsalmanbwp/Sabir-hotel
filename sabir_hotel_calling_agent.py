"""
Sabir Hotel Inbound Calling Agent - FIRST VERSION (2026-11-07)
===============================================================
Inbound only: a customer calls, the agent takes a delivery order.
No outbound / follow-up calls. No table booking.

How a call flows:
  POST /voice          -> greeting + spoken menu, Gather order (speech)
  POST /voice/order    -> parse items, read back totals, Gather name
  POST /voice/name     -> Gather address
  POST /voice/address  -> read back full order + total, Gather yes/no
  POST /voice/confirm  -> yes: save to Google Sheet, WhatsApp alerts to
                          kitchen + owner, give order ID. No: cancel.
  If speech is empty or an item is not on the menu, the prompt repeats
  (max 2 repeats) and then the agent says a human will call back.

Single source of truth (same as the WhatsApp bot):
  * Prices + delivery charge come ONLY from sabir_hotel_config.json
    (menu + delivery_charge_pkr). NEVER edit prices in the MENU dict
    below - the JSON overwrites it at startup.
  * sabir_hotel_config.json's whatsapp_numbers / server_url are NOT
    read by this code. Numbers and server URL go in .env only.
  * Delivery charge: this file's delivery_charge_pkr IS used, but only
    if FIXED_DELIVERY_CHARGE is not set in .env. The .env.template
    leaves that line commented out for exactly this reason.

Setup:
  pip install -r requirements.txt
  Run: uvicorn sabir_hotel_calling_agent:app --host 0.0.0.0 --port 8001
  (a different port from the WhatsApp bot's 8000)
  In Twilio, set the voice number's webhook to: <SERVER_URL>/voice
  (if your domain routes /voice to port 8001; otherwise use
  https://your-domain.com:8001/voice)

Required .env (no real values in this file - same .env as the WhatsApp bot):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN
  TWILIO_VOICE_NUMBER   (Twilio voice number for inbound calls, e.g. +1...)
  TWILIO_WHATSAPP_NUMBER (sender for the kitchen/owner WhatsApp alerts;
                          already in .env for the WhatsApp bot)
  GEMINI_API_KEY         (items + quantities only; prices come from MENU)
  KITCHEN_WHATSAPP, OWNER_WHATSAPP   (e.g. whatsapp:+92...)
  SERVER_URL
"""
import json
import os
import re
import time
from datetime import datetime

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Response
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather, Hangup
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

load_dotenv()

# ------------------------------------------------------------
# 1. CONFIG - no real secrets or phone numbers in this file
# ------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_VOICE_NUMBER = os.getenv("TWILIO_VOICE_NUMBER", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

KITCHEN_WHATSAPP = os.getenv("KITCHEN_WHATSAPP", "")
OWNER_WHATSAPP = os.getenv("OWNER_WHATSAPP", "")
SERVER_URL = os.getenv("SERVER_URL", "")
try:
    FIXED_DELIVERY_CHARGE = int(os.getenv("FIXED_DELIVERY_CHARGE", "150"))
except ValueError:
    FIXED_DELIVERY_CHARGE = 150

# Twilio client is only created when real credentials exist.
twilio_client = None
if TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
    twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

if GEMINI_API_KEY:
    genai.configure(api_key=GEMINI_API_KEY)

# ------------------------------------------------------------
# 2. MENU - loaded from sabir_hotel_config.json when present.
# Edit prices in that JSON file, not here. This dict is only
# the fallback if the JSON file is missing.
# ------------------------------------------------------------
MENU = {
    "chicken biryani": 350,
    "beef biryani": 400,
    "chicken karahi": 850,
    "chicken tikka": 380,
    "seekh kabab": 450,
    "naan": 40,
    "raita": 50,
    "cold drink": 80,
}

def _load_config_menu():
    """Load menu + delivery charge from sabir_hotel_config.json if it exists."""
    global FIXED_DELIVERY_CHARGE
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "sabir_hotel_config.json"),
        "sabir_hotel_config.json",
    ]
    for path in candidates:
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    cfg = json.load(f)
                items = cfg.get("menu") or []
                loaded = {}
                for entry in items:
                    name = str(entry.get("name", "")).strip().lower()
                    price = int(entry.get("price_pkr", 0) or 0)
                    if name and price > 0:
                        loaded[name] = price
                if loaded:
                    MENU.clear()
                    MENU.update(loaded)
                if os.getenv("FIXED_DELIVERY_CHARGE") is None and cfg.get("delivery_charge_pkr"):
                    FIXED_DELIVERY_CHARGE = int(cfg["delivery_charge_pkr"])
                print(f"Loaded menu from {path}: {len(MENU)} items")
                return
        except Exception as e:
            print(f"Could not load config JSON ({path}): {e}")
    print("No sabir_hotel_config.json found, using built-in example menu")

_load_config_menu()

def menu_spoken() -> str:
    """Short spoken menu, e.g. 'Chicken biryani, 350 rupees.'"""
    parts = [f"{name}, {price} rupees" for name, price in MENU.items()]
    return ". ".join(parts) + f". Delivery is {FIXED_DELIVERY_CHARGE} rupees."

def menu_hints() -> str:
    """Speech-recognition hints for the order Gather."""
    return ", ".join(MENU.keys())

def format_whatsapp_number(number: str) -> str:
    number = (number or "").strip()
    if not number:
        return ""
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"

# ------------------------------------------------------------
# 3. GOOGLE SHEETS - same sheet and columns as the WhatsApp bot
# ------------------------------------------------------------
SHEET_HEADERS = [
    "Order ID", "Date", "Name", "Phone", "Address",
    "Items", "Grand Total", "Status", "Rider Name", "Rider Phone",
]

def get_google_sheet():
    try:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = None
        # Railway fix (2026-11-07): Railway has no way to upload a
        # credentials.json file, so the whole service-account JSON can be
        # pasted into GOOGLE_CREDENTIALS_JSON (Railway Variables). It wins
        # over the file, because on Railway the file cannot be uploaded.
        # Invalid JSON is logged and the Sheet is skipped — the agent
        # never crashes over it.
        env_creds = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
        if env_creds:
            creds = Credentials.from_service_account_info(json.loads(env_creds), scopes=scopes)
        elif os.path.exists("credentials.json"):
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        if creds is not None:
            client = gspread.authorize(creds)
            sheet = client.open("Sabir Hotel").sheet1
            try:
                first_row = sheet.row_values(1)
                if first_row != SHEET_HEADERS:
                    if not first_row:
                        sheet.append_row(SHEET_HEADERS)
            except Exception:
                pass
            return sheet
    except Exception as e:
        print(f"Google Sheet Connection Error: {e}")
    return None

def save_order_to_sheet(order_id, session):
    sheet = get_google_sheet()
    if not sheet:
        print("Sheet not available, order not saved to sheet")
        return False
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet.append_row([
            order_id, now,
            session["name"], session["phone"], session["address"],
            session["order_data"]["items_summary"],
            session["order_data"]["grand_total"],
            "Pending", "", "",
        ])
        return True
    except Exception as e:
        print(f"Error saving row to sheet: {e}")
        return False

# ------------------------------------------------------------
# 4. WHATSAPP ALERTS - same messages as the WhatsApp bot
# ------------------------------------------------------------
def send_whatsapp_message(to_number: str, body: str):
    if not twilio_client:
        print(f"Twilio not configured, would send to {to_number}: {body[:120]}")
        return
    if not TWILIO_WHATSAPP_NUMBER:
        print("No WhatsApp sender number in .env, message not sent")
        return
    to_number = format_whatsapp_number(to_number)
    if not to_number:
        print("No recipient number, message not sent")
        return
    try:
        twilio_client.messages.create(
            from_=format_whatsapp_number(TWILIO_WHATSAPP_NUMBER),
            body=body,
            to=to_number,
        )
    except Exception as e:
        print(f"WhatsApp Send Error ({to_number}): {e}")

# ------------------------------------------------------------
# 5. ORDER PARSING - prices ALWAYS come from MENU, never Gemini.
# Same local + Gemini logic as the WhatsApp bot, plus number words
# ("two chicken biryani") because callers speak quantities as words.
# ------------------------------------------------------------
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}

def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())

def _words_to_digits(text: str) -> str:
    low = _normalise(text)
    for word, num in _NUMBER_WORDS.items():
        low = re.sub(rf"\b{word}\b", str(num), low)
    return low

def _qty_before(text: str, start: int) -> int:
    """Look for a number just before the item name, e.g. '2 chicken biryani'."""
    before = text[max(0, start - 12):start]
    m = re.findall(r"(\d+)\s*$", before)
    return int(m[0]) if m else 1

def parse_menu_items_locally(text: str) -> dict:
    """Simple keyword match against MENU. Returns {item: qty}."""
    low = _words_to_digits(text)
    found = {}
    for item in MENU:
        idx = low.find(item)
        if idx != -1:
            found[item] = _qty_before(low, idx)
    return found

def _has_unmatched_quantity(text: str, items: dict) -> bool:
    """True if a number in the order has no matched menu item after it."""
    low = _words_to_digits(text)
    for m in re.finditer(r"\d+", low):
        accounted = False
        for item in items:
            idx = low.find(item)
            while idx != -1:
                before = low[max(0, idx - 12):idx]
                if re.search(rf"{re.escape(m.group())}\s*$", before):
                    accounted = True
                    break
                idx = low.find(item, idx + 1)
            if accounted:
                break
        if not accounted:
            return True
    return False

def parse_order_with_gemini(text: str):
    """
    Returns a dict with items_summary, kitchen_items, food_total,
    delivery_charges, grand_total - or None if no menu item was found.
    Gemini is only used to extract items + quantities. Pricing is
    always calculated from MENU.
    """
    items = parse_menu_items_locally(text)

    if GEMINI_API_KEY and (not items or _has_unmatched_quantity(text, items)):
        try:
            model = genai.GenerativeModel("gemini-3.5-flash-lite")
            prompt = (
                "You are a restaurant order parser for 'Sabir Hotel'. "
                "Extract only items and quantities from this order as JSON, "
                "e.g. {\"items\": [{\"name\": \"chicken biryani\", \"qty\": 2}]}. "
                "Only use item names from this menu: "
                + ", ".join(MENU.keys())
                + f". Order: \"{text}\". Return JSON only, no other text."
            )
            response = model.generate_content(prompt)
            data = json.loads(response.text.strip().strip("`").replace("json", "", 1).strip())
            for entry in data.get("items", []):
                name = _normalise(entry.get("name", ""))
                qty = int(entry.get("qty", 1) or 1)
                if name in MENU and qty > 0:
                    items[name] = qty
        except Exception as e:
            print(f"Gemini Parsing Error: {e}")
            items = {}

    if not items:
        return None

    lines = []
    kitchen_lines = []
    food_total = 0
    for name, qty in items.items():
        price = MENU[name]
        total = price * qty
        food_total += total
        lines.append(f"• {name.title()} x{qty} - Rs. {total}")
        kitchen_lines.append(f"• {name.title()} x{qty}")

    return {
        "items_summary": "\n".join(lines),
        "kitchen_items": "\n".join(kitchen_lines),
        "food_total": food_total,
        "delivery_charges": FIXED_DELIVERY_CHARGE,
        "grand_total": food_total + FIXED_DELIVERY_CHARGE,
    }

def order_spoken(order_data: dict) -> str:
    """Spoken order summary, e.g. '2 chicken biryani, 1 naan'."""
    parts = []
    for line in order_data["items_summary"].splitlines():
        # '• Chicken Biryani x2 - Rs. 700' -> '2 chicken biryani'
        m = re.match(r"•\s*(.+?)\s*x(\d+)\s*-", line)
        if m:
            parts.append(f"{m.group(2)} {m.group(1).lower()}")
    return ", ".join(parts)

# ------------------------------------------------------------
# 6. APP & CALL STATE - sessions keyed by CallSid
# ------------------------------------------------------------
app = FastAPI(title="Sabir Hotel Inbound Calling Agent")

call_sessions = {}
SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 minutes

STATE_AWAITING_ORDER = "AWAITING_ORDER"
STATE_AWAITING_NAME = "AWAITING_NAME"
STATE_AWAITING_ADDRESS = "AWAITING_ADDRESS"
STATE_AWAITING_CONFIRM = "AWAITING_CONFIRM"

YES_WORDS = {"yes", "yeah", "yep", "han", "haan", "ji", "confirm", "correct", "theek", "ok", "okay", "sahi", "hao"}
NO_WORDS = {"no", "nahi", "nain", "cancel", "galat", "wrong", "naheen"}

def new_session(phone: str = ""):
    return {
        "state": STATE_AWAITING_ORDER, "order_data": {}, "name": "",
        "address": "", "phone": phone, "repeats": 0, "updated_at": time.time(),
    }

def get_session(call_sid: str, phone: str = "") -> dict:
    session = call_sessions.get(call_sid)
    if session and time.time() - session.get("updated_at", 0) > SESSION_TIMEOUT_SECONDS:
        session = new_session(phone)
    if not session:
        session = new_session(phone)
    if phone and not session.get("phone"):
        session["phone"] = phone
    session["updated_at"] = time.time()
    call_sessions[call_sid] = session
    return session

def reset_session(call_sid: str):
    call_sessions.pop(call_sid, None)

def _respond(vr: VoiceResponse):
    return Response(content=str(vr), media_type="application/xml")

def _gather(vr: VoiceResponse, text: str, action: str, hints: str = ""):
    gather = Gather(input="speech", action=action, method="POST",
                    speech_timeout="auto", language="en-US")
    if hints:
        gather.hints = hints
    gather.say(text)
    vr.append(gather)

def _repeat_or_end(call_sid: str, prompt: str, action: str, hints: str = ""):
    """Repeat the prompt (max 2 repeats), then say a human will call back."""
    session = call_sessions.get(call_sid) or new_session()
    call_sessions[call_sid] = session
    session["repeats"] = session.get("repeats", 0) + 1
    vr = VoiceResponse()
    if session["repeats"] >= 3:
        vr.say("Sorry, I could not understand you. Someone from Sabir Hotel "
               "will call you back. Goodbye.")
        vr.append(Hangup())
        reset_session(call_sid)
    else:
        _gather(vr, prompt, action, hints)
    return _respond(vr)

def _cancel(call_sid: str):
    vr = VoiceResponse()
    vr.say("Your order has been cancelled. Thank you for calling Sabir Hotel. Goodbye.")
    vr.append(Hangup())
    reset_session(call_sid)
    return _respond(vr)

def _is_cancel(text: str) -> bool:
    return _normalise(text) in ("cancel", "cancel order", "stop")

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "sheet": bool(get_google_sheet()),
        "twilio": bool(twilio_client),
        "voice_number_set": bool(TWILIO_VOICE_NUMBER),
    }

# ------------------------------------------------------------
# 7. VOICE FLOW
# ------------------------------------------------------------
@app.post("/voice")
async def voice_webhook(CallSid: str = Form(""), From: str = Form("")):
    """Call starts: greeting + spoken menu, then listen for the order."""
    call_sid = CallSid or "unknown"
    call_sessions[call_sid] = new_session(From)
    vr = VoiceResponse()
    greeting = (
        "Assalam o Alaikum! Welcome to Sabir Hotel. We take delivery orders on call. "
        f"Our menu is: {menu_spoken()} "
        "What would you like to order? Please say it after the beep."
    )
    _gather(vr, greeting, "/voice/order", hints=menu_hints())
    return _respond(vr)

@app.post("/voice/order")
async def voice_order(CallSid: str = Form(""), SpeechResult: str = Form("")):
    """Parse the spoken order, read back totals, then ask for the name."""
    call_sid = CallSid or "unknown"
    session = get_session(call_sid)
    speech = (SpeechResult or "").strip()

    if _is_cancel(speech):
        return _cancel(call_sid)
    if not speech:
        return _repeat_or_end(
            call_sid,
            "Sorry, I did not hear you. Please say your order, for example, "
            "2 chicken biryani, 1 naan.",
            "/voice/order", hints=menu_hints())

    parsed = parse_order_with_gemini(speech)
    if not parsed:
        return _repeat_or_end(
            call_sid,
            "Sorry, I could not find that item on our menu. "
            f"Our menu is: {menu_spoken()} "
            "Please say your order again.",
            "/voice/order", hints=menu_hints())

    session["order_data"] = parsed
    session["state"] = STATE_AWAITING_NAME
    session["repeats"] = 0
    call_sessions[call_sid] = session
    vr = VoiceResponse()
    summary = (
        f"Your order is: {order_spoken(parsed)}. "
        f"Food total {parsed['food_total']} rupees. "
        f"Delivery {parsed['delivery_charges']} rupees. "
        f"Total {parsed['grand_total']} rupees. "
        "Please say your full name."
    )
    _gather(vr, summary, "/voice/name")
    return _respond(vr)

@app.post("/voice/name")
async def voice_name(CallSid: str = Form(""), SpeechResult: str = Form("")):
    """Take the customer's name, then ask for the address."""
    call_sid = CallSid or "unknown"
    session = get_session(call_sid)
    speech = (SpeechResult or "").strip()

    if _is_cancel(speech):
        return _cancel(call_sid)
    if len(speech) < 2:
        return _repeat_or_end(
            call_sid,
            "Sorry, I did not catch your name. Please say your full name.",
            "/voice/name")

    session["name"] = speech
    session["state"] = STATE_AWAITING_ADDRESS
    session["repeats"] = 0
    call_sessions[call_sid] = session
    vr = VoiceResponse()
    _gather(vr, f"Thank you, {speech}. Please say your full delivery address.",
            "/voice/address")
    return _respond(vr)

@app.post("/voice/address")
async def voice_address(CallSid: str = Form(""), SpeechResult: str = Form("")):
    """Take the delivery address, read back the full order, ask to confirm."""
    call_sid = CallSid or "unknown"
    session = get_session(call_sid)
    speech = (SpeechResult or "").strip()

    if _is_cancel(speech):
        return _cancel(call_sid)
    if len(speech) < 5:
        return _repeat_or_end(
            call_sid,
            "Sorry, please say your full delivery address.",
            "/voice/address")

    session["address"] = speech
    session["state"] = STATE_AWAITING_CONFIRM
    session["repeats"] = 0
    call_sessions[call_sid] = session
    order_data = session["order_data"]
    vr = VoiceResponse()
    summary = (
        f"To confirm: {order_spoken(order_data)}, for {session['name']}, "
        f"address {speech}. "
        f"Food total {order_data['food_total']} rupees. "
        f"Delivery {order_data['delivery_charges']} rupees. "
        f"Total {order_data['grand_total']} rupees. "
        "Say yes to confirm your order, or no to cancel."
    )
    _gather(vr, summary, "/voice/confirm")
    return _respond(vr)

@app.post("/voice/confirm")
async def voice_confirm(CallSid: str = Form(""), From: str = Form(""), SpeechResult: str = Form("")):
    """Yes: save order, alert kitchen + owner, give order ID. No: cancel."""
    call_sid = CallSid or "unknown"
    session = get_session(call_sid, From)
    speech = (SpeechResult or "").strip()

    if _is_cancel(speech):
        return _cancel(call_sid)
    tokens = set(re.findall(r"[a-z]+", _normalise(speech)))
    if tokens & NO_WORDS:
        return _cancel(call_sid)
    if not (tokens & YES_WORDS):
        return _repeat_or_end(
            call_sid,
            "Please say yes to confirm your order, or no to cancel.",
            "/voice/confirm")

    order_data = session.get("order_data") or {}
    if not order_data:
        vr = VoiceResponse()
        vr.say("Sorry, I could not find your order. Please call again. Goodbye.")
        vr.append(Hangup())
        reset_session(call_sid)
        return _respond(vr)

    phone_digits = re.sub(r"\D", "", session.get("phone") or From or "")
    suffix = phone_digits[-4:] if len(phone_digits) >= 4 else (call_sid[-4:] or "0000")
    order_id = f"ORD-{datetime.now().strftime('%Y%m%d')}-{suffix}"
    saved = save_order_to_sheet(order_id, session)
    if not saved:
        print(f"Warning: order {order_id} was confirmed but NOT saved to sheet")

    if KITCHEN_WHATSAPP:
        kitchen_msg = (
            f"🍳 *KITCHEN ORDER ALERT (CALL)*\n"
            f"🆔 *Order ID:* {order_id}\n\n"
            f"🍲 *ITEMS TO PREPARE:*\n"
            f"{order_data['kitchen_items']}\n\n"
            f"📌 *Status:* Pending Preparation"
        )
        send_whatsapp_message(to_number=KITCHEN_WHATSAPP, body=kitchen_msg)

    if OWNER_WHATSAPP:
        owner_msg = (
            f"🔔 *NEW ORDER CONFIRMED - OWNER ALERT (CALL)*\n\n"
            f"🆔 *Order ID:* {order_id}\n"
            f"👤 *Customer:* {session['name']} ({session.get('phone') or From})\n"
            f"📍 *Address:* {session['address']}\n\n"
            f"🍲 *ITEMS:*\n{order_data['items_summary']}\n\n"
            f"💰 *TOTAL: Rs. {order_data['grand_total']}*\n\n"
            f"Dispatch command:\n"
            f"`Done {session.get('phone') or From} [Rider Name] [Rider Phone]`"
        )
        send_whatsapp_message(to_number=OWNER_WHATSAPP, body=owner_msg)

    vr = VoiceResponse()
    vr.say(
        f"Thank you, {session['name']}! Your order is confirmed. "
        f"Your order ID is {order_id}. "
        f"Total {order_data['grand_total']} rupees. "
        "Your food will arrive in 20 to 30 minutes. Goodbye."
    )
    vr.append(Hangup())
    reset_session(call_sid)
    return _respond(vr)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
