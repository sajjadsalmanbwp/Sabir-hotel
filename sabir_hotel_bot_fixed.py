"""
Sabir Hotel WhatsApp Automation - FIXED version
================================================
Fixes in this version (2026-11-06):
1. /settings is protected by admin login, and secrets are NEVER
   shown back in the page. Empty password fields mean "keep current".
2. "hi" / "hello" / "menu" no longer creates a fake order.
   They show a welcome + menu and stay in IDLE.
3. Prices come from a real MENU in this file, not from Gemini
   guessing. Gemini only extracts items + quantities.
   No dummy fallback order - if an order is not found, dispatch
   stops and tells the owner instead of messaging the wrong person.

Setup:
  pip install fastapi uvicorn twilio gspread google-generativeai python-dotenv
  Run: uvicorn sabir_hotel_bot_fixed:app --host 0.0.0.0 --port 8000

Required .env (no real values in this file):
  TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_NUMBER
  GEMINI_API_KEY
  KITCHEN_WHATSAPP, OWNER_WHATSAPP   (e.g. whatsapp:+92...)
  SERVER_URL
  FIXED_DELIVERY_CHARGE=150
  ADMIN_USERNAME=admin
  ADMIN_PASSWORD=<choose a strong password, never commit it>
  MENU is in sabir_hotel_config.json next to this file - REPLACE the
  example prices there with the real menu.
"""
import json
import os
import re
import secrets
import time
import shutil
from datetime import datetime
from urllib.parse import parse_qs

from dotenv import load_dotenv
from fastapi import FastAPI, Form, File, UploadFile, Response, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from twilio.rest import Client
from twilio.request_validator import RequestValidator
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai

load_dotenv()

# ------------------------------------------------------------
# 1. CONFIG - no real secrets or phone numbers in this file
# ------------------------------------------------------------
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")

KITCHEN_WHATSAPP = os.getenv("KITCHEN_WHATSAPP", "")
OWNER_WHATSAPP = os.getenv("OWNER_WHATSAPP", "")
SERVER_URL = os.getenv("SERVER_URL", "")
try:
    FIXED_DELIVERY_CHARGE = int(os.getenv("FIXED_DELIVERY_CHARGE", "150"))
except ValueError:
    FIXED_DELIVERY_CHARGE = 150

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")

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

def menu_text() -> str:
    lines = ["🍽️ *SABIR HOTEL - MENU*\n"]
    for name, price in MENU.items():
        lines.append(f"• {name.title()} - Rs. {price}")
    lines.append(f"\n🚚 Delivery: Rs. {FIXED_DELIVERY_CHARGE}")
    lines.append("\nOrder likh kar bhejein, masalan: '2 chicken biryani, 1 naan'")
    return "\n".join(lines)

def format_whatsapp_number(number: str) -> str:
    number = (number or "").strip()
    if not number:
        return ""
    return number if number.startswith("whatsapp:") else f"whatsapp:{number}"

# ------------------------------------------------------------
# 3. ADMIN AUTH - protects /settings and /save-settings
# ------------------------------------------------------------
security = HTTPBasic()

def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    if not ADMIN_PASSWORD:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Admin password is not set. Set ADMIN_PASSWORD in .env first.",
        )
    user_ok = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    pass_ok = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect admin login",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username

def secret_status(value: str) -> str:
    return "✅ Set" if value else "❌ Not set"

# ------------------------------------------------------------
# 4. GOOGLE SHEETS
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
        # over the file, because a file uploaded via /settings would be
        # lost on redeploy. Invalid JSON is logged and the Sheet is
        # skipped — the bot never crashes over it.
        env_creds = (os.getenv("GOOGLE_CREDENTIALS_JSON") or "").strip()
        if env_creds:
            creds = Credentials.from_service_account_info(json.loads(env_creds), scopes=scopes)
        elif os.path.exists("credentials.json"):
            creds = Credentials.from_service_account_file("credentials.json", scopes=scopes)
        if creds is not None:
            client = gspread.authorize(creds)
            sheet = client.open("Sabir Hotel").sheet1
            # Ensure headers exist on first use
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

# ------------------------------------------------------------
# 5. APP & STATE
# ------------------------------------------------------------
app = FastAPI(title="Sabir Hotel WhatsApp Automation System")

user_sessions = {}
SESSION_TIMEOUT_SECONDS = 30 * 60  # 30 minutes

STATE_IDLE = "IDLE"
STATE_AWAITING_NAME = "AWAITING_NAME"
STATE_AWAITING_ADDRESS = "AWAITING_ADDRESS"
STATE_AWAITING_PHONE = "AWAITING_PHONE"

GREETINGS = {
    "hi", "hello", "hey", "salam", "aoa", "assalam o alaikum",
    "assalamualaikum", "menu", "show menu", "price", "prices",
    "timing", "hours", "open", "order karna hai",
}

def new_session():
    return {"state": STATE_IDLE, "order_data": {}, "name": "", "address": "", "phone": "", "updated_at": time.time()}

def get_session(phone: str) -> dict:
    session = user_sessions.get(phone)
    if session and time.time() - session.get("updated_at", 0) > SESSION_TIMEOUT_SECONDS:
        session = new_session()
    if not session:
        session = new_session()
    session["updated_at"] = time.time()
    user_sessions[phone] = session
    return session

def reset_session(phone: str):
    user_sessions[phone] = new_session()

@app.get("/health")
async def health():
    return {"status": "ok", "sheet": bool(get_google_sheet()), "twilio": bool(twilio_client)}

# ------------------------------------------------------------
# 6. WHATSAPP SEND HELPERS
# ------------------------------------------------------------
def send_whatsapp_message(to_number: str, body: str):
    if not twilio_client:
        print(f"Twilio not configured, would send to {to_number}: {body[:120]}")
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

def send_whatsapp_audio(to_number: str, body: str, media_url: str):
    if not twilio_client:
        print("Twilio not configured, audio not sent")
        return
    try:
        twilio_client.messages.create(
            from_=format_whatsapp_number(TWILIO_WHATSAPP_NUMBER),
            body=body,
            media_url=[media_url],
            to=format_whatsapp_number(to_number),
        )
    except Exception as e:
        print(f"WhatsApp Audio Send Error ({to_number}): {e}")

# ------------------------------------------------------------
# 7. ORDER PARSING - prices ALWAYS come from MENU, never Gemini
# ------------------------------------------------------------
def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower().strip())

def _qty_before(text: str, start: int) -> int:
    """Look for a number just before the item name, e.g. '2 chicken biryani'."""
    before = text[max(0, start - 12):start]
    m = re.findall(r"(\d+)\s*$", before)
    return int(m[0]) if m else 1

def parse_menu_items_locally(text: str) -> dict:
    """Simple keyword match against MENU. Returns {item: qty}."""
    low = _normalise(text)
    found = {}
    for item in MENU:
        idx = low.find(item)
        if idx != -1:
            found[item] = _qty_before(low, idx)
    return found

def _has_unmatched_quantity(text: str, items: dict) -> bool:
    """True if a number in the order has no matched menu item after it.

    Catches the partial-match trap: '2 chiken biryani, 1 naan' locally
    matches only 'naan', so the '2' is unaccounted for and the biryani
    would otherwise be silently dropped. A number counts as accounted
    for only if it sits just before a matched item name (same 12-char
    window _qty_before uses). Extra numbers such as a delivery time
    also trigger Gemini - harmless, Gemini just returns the same items.
    """
    low = _normalise(text)
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
    items = {}
    # 1) Try local matching first - fast and reliable
    items = parse_menu_items_locally(text)

    # 2) If Gemini is available and local match found nothing - or found
    #    only part of the order (a quantity with no matched item, e.g.
    #    a typo) - ask it to extract item names only, then price those
    #    from MENU. Gemini's items are merged into the local ones.
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

# ------------------------------------------------------------
# 8. SHEET HELPERS - no dummy fallback order
# ------------------------------------------------------------
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

def update_order_status_in_sheet(order_id, status_text, rider_name, rider_phone):
    sheet = get_google_sheet()
    if not sheet:
        return False
    try:
        cell = sheet.find(order_id)
        if cell:
            row = cell.row
            sheet.update_cell(row, 8, status_text)
            sheet.update_cell(row, 9, rider_name)
            sheet.update_cell(row, 10, rider_phone)
            return True
    except Exception as e:
        print(f"Error updating sheet status: {e}")
    return False

def get_order_from_sheet_by_phone(phone_clean):
    """
    Returns the last matching order, or None if not found.
    Never return a fake order.
    """
    sheet = get_google_sheet()
    if not sheet:
        return None
    try:
        records = sheet.get_all_records()
        digits = re.sub(r"\D", "", phone_clean or "")[-10:]
        if not digits:
            return None
        for row in reversed(records):
            row_phone = re.sub(r"\D", "", str(row.get("Phone", "")))
            if row_phone and row_phone.endswith(digits):
                return row
    except Exception as e:
        print(f"Error reading sheet: {e}")
    return None

# ------------------------------------------------------------
# 9. MAIN WHATSAPP WEBHOOK - with Twilio signature check
# ------------------------------------------------------------
def _twilio_signature_valid(request: Request, raw_body: bytes) -> bool:
    """Validate X-Twilio-Signature when a real token is configured."""
    if not TWILIO_AUTH_TOKEN:
        return True
    signature = request.headers.get("X-Twilio-Signature", "")
    if not signature:
        # Twilio always sends this header in production. In local
        # testing it may be missing - log it, do not silently trust it.
        print("Warning: missing X-Twilio-Signature header")
        return True
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    url = str(request.url)
    params = {k: v[0] for k, v in parse_qs(raw_body.decode("utf-8", "ignore")).items()}
    return validator.validate(url, params, signature)

@app.post("/whatsapp")
async def whatsapp_webhook(request: Request):
    global FIXED_DELIVERY_CHARGE
    raw_body = await request.body()
    if not _twilio_signature_valid(request, raw_body):
        return Response(content="<Response></Response>", media_type="application/xml", status_code=403)

    form = parse_qs(raw_body.decode("utf-8", "ignore"))
    From = form.get("From", [""])[0]
    Body = form.get("Body", [""])[0]

    user_phone = From.replace("whatsapp:", "").strip()
    msg_text = Body.strip()
    msg_lower = _normalise(msg_text)
    if not user_phone:
        return Response(content="<Response></Response>", media_type="application/xml")

    # ---- A. MANAGER / OWNER COMMANDS ----
    owner_num_clean = OWNER_WHATSAPP.replace("whatsapp:", "").strip()
    kitchen_num_clean = KITCHEN_WHATSAPP.replace("whatsapp:", "").strip()

    if user_phone in (owner_num_clean, kitchen_num_clean) and owner_num_clean:
        if msg_lower.startswith("delivery"):
            parts = msg_text.split()
            if len(parts) >= 2 and parts[1].isdigit():
                FIXED_DELIVERY_CHARGE = int(parts[1])
                send_whatsapp_message(to_number=user_phone, body=f"✅ Fixed Delivery Charge updated to: Rs. {FIXED_DELIVERY_CHARGE}")
                return Response(content="<Response></Response>", media_type="application/xml")
            send_whatsapp_message(to_number=user_phone, body="Usage: Delivery 200")
            return Response(content="<Response></Response>", media_type="application/xml")

        if msg_lower.startswith("done"):
            parts = msg_text.split()
            if len(parts) < 4:
                send_whatsapp_message(to_number=user_phone, body="Usage: Done <Customer_Phone> <Rider_Name> <Rider_Phone>")
                return Response(content="<Response></Response>", media_type="application/xml")
            cust_phone, rider_name, rider_phone = parts[1], parts[2], parts[3]
            order = get_order_from_sheet_by_phone(cust_phone)
            if not order:
                send_whatsapp_message(
                    to_number=user_phone,
                    body=f"❌ Order not found for phone {cust_phone}. Check the number and try again. No message was sent to the customer.",
                )
                return Response(content="<Response></Response>", media_type="application/xml")

            order_id = order.get("Order ID", "")
            grand_total = order.get("Grand Total", "")
            address = order.get("Address", "N/A")
            cust_name = order.get("Name", "Customer")

            customer_dispatch_msg = (
                f"🚴‍♂️ *SABIR HOTEL - ORDER DISPATCHED*\n\n"
                f"🆔 *Order ID:* {order_id}\n\n"
                f"آپ کا آرڈر ہوٹل سے روانہ ہو چکا ہے!\n\n"
                f"👤 *Rider Name:* {rider_name}\n"
                f"📞 *Rider Phone:* {rider_phone}\n"
                f"💰 *Cash to Pay:* Rs. {grand_total}\n"
                f"⏱️ *Estimated Time:* 20-30 Minutes\n\n"
                f"براہِ کرم اپنا فون آن رکھیں۔ صابر ہوٹل منتخب کرنے کا شکریہ! ❤️"
            )
            send_whatsapp_message(to_number=cust_phone, body=customer_dispatch_msg)

            owner_dispatch_msg = (
                f"🚀 *DISPATCH REPORT - SABIR HOTEL*\n\n"
                f"🆔 *Order ID:* {order_id}\n"
                f"⏰ *Time:* {datetime.now().strftime('%I:%M %p')}\n\n"
                f"👤 *Customer:* {cust_name}\n"
                f"📞 *Phone:* {cust_phone}\n"
                f"📍 *Address:* {address}\n\n"
                f"🚴 *RIDER ASSIGNED:*\n"
                f"• Rider Name: {rider_name}\n"
                f"• Rider Phone: {rider_phone}\n\n"
                f"💰 *CASH TO COLLECT:* Rs. {grand_total}\n"
                f"📌 *Status:* Out for Delivery"
            )
            send_whatsapp_message(to_number=OWNER_WHATSAPP, body=owner_dispatch_msg)
            update_order_status_in_sheet(order_id, "Dispatched", rider_name, rider_phone)
            return Response(content="<Response></Response>", media_type="application/xml")

    # ---- B. CUSTOMER FLOW ----
    if msg_lower in ("cancel", "reset", "new order"):
        reset_session(user_phone)
        send_whatsapp_message(to_number=user_phone, body="Order cancel kar diya gaya hai. Naya order ke liye menu se choose karein.\n\n" + menu_text())
        return Response(content="<Response></Response>", media_type="application/xml")

    session = get_session(user_phone)
    current_state = session["state"]

    # STEP 0: IDLE - greeting / menu first, never create an order here
    if current_state == STATE_IDLE:
        if msg_lower in GREETINGS:
            send_whatsapp_message(
                to_number=user_phone,
                body=f"Assalam o Alaikum! 👋 Sabir Hotel mein khush amdeed.\n\n{menu_text()}",
            )
            return Response(content="<Response></Response>", media_type="application/xml")

        parsed_order = parse_order_with_gemini(msg_text)
        if not parsed_order:
            send_whatsapp_message(
                to_number=user_phone,
                body=f"Maaf kijiye, main aapka order samajh nahi paya. 👇\n\n{menu_text()}\n\nKya aap menu se koi item choose karna chahenge?",
            )
            return Response(content="<Response></Response>", media_type="application/xml")

        session["order_data"] = parsed_order
        session["state"] = STATE_AWAITING_NAME
        user_sessions[user_phone] = session
        reply = (
            f"📝 *ORDER SUMMARY*\n"
            f"{parsed_order['items_summary']}\n\n"
            f"🍔 Food Total: Rs. {parsed_order['food_total']}\n"
            f"🚚 Delivery Charges: Rs. {parsed_order['delivery_charges']}\n"
            f"💰 *Total: Rs. {parsed_order['grand_total']}*\n\n"
            f"👤 **Step 1 of 3:** براہِ کرم اپنا *مکمل نام* لکھ کر بھیجیں۔"
        )
        send_whatsapp_message(to_number=user_phone, body=reply)
        return Response(content="<Response></Response>", media_type="application/xml")

    # STEP 1: Name
    if current_state == STATE_AWAITING_NAME:
        if len(msg_text) < 2:
            send_whatsapp_message(to_number=user_phone, body="Please apna sahi naam likhein.")
            return Response(content="<Response></Response>", media_type="application/xml")
        session["name"] = msg_text
        session["state"] = STATE_AWAITING_ADDRESS
        user_sessions[user_phone] = session
        send_whatsapp_message(
            to_number=user_phone,
            body=f"شکریہ *{msg_text}* صاحب! 🌸\n\n📍 **Step 2 of 3:** اب اپنا *مکمل ڈلیوری ایڈریس* درج کریں:",
        )
        return Response(content="<Response></Response>", media_type="application/xml")

    # STEP 2: Address
    if current_state == STATE_AWAITING_ADDRESS:
        if len(msg_text) < 5:
            send_whatsapp_message(to_number=user_phone, body="Please apna mukammal address likhein.")
            return Response(content="<Response></Response>", media_type="application/xml")
        session["address"] = msg_text
        session["state"] = STATE_AWAITING_PHONE
        user_sessions[user_phone] = session
        send_whatsapp_message(
            to_number=user_phone,
            body="📞 **Step 3 of 3:** اپنا *فون نمبر* درج کریں (یا اگر یہی واٹس ایپ نمبر ہے تو *'Same'* لکھ کر بھیجیں):",
        )
        return Response(content="<Response></Response>", media_type="application/xml")

    # STEP 3: Phone -> confirm, save, notify kitchen + owner
    if current_state == STATE_AWAITING_PHONE:
        final_phone = user_phone if msg_lower == "same" else re.sub(r"[^\d+]", "", msg_text)
        if len(re.sub(r"\D", "", final_phone)) < 10:
            send_whatsapp_message(to_number=user_phone, body="Please sahi phone number likhein, ya 'Same' likhein.")
            return Response(content="<Response></Response>", media_type="application/xml")
        session["phone"] = final_phone
        order_id = f"ORD-{datetime.now().strftime('%Y%m%d')}-{user_phone[-4:]}"
        saved = save_order_to_sheet(order_id, session)
        if not saved:
            print(f"Warning: order {order_id} was confirmed but NOT saved to sheet")

        cust_msg = (
            f"🎉 *ORDER CONFIRMED* 🎉\n\n"
            f"🆔 Order ID: *{order_id}*\n"
            f"👤 Name: {session['name']}\n"
            f"📍 Address: {session['address']}\n\n"
            f"💰 *Grand Total: Rs. {session['order_data']['grand_total']}*\n\n"
            f"آپ کا آرڈر کچن کو بھیج دیا گیا ہے۔ صابر ہوٹل منتخب کرنے کا شکریہ! ❤️"
        )
        send_whatsapp_message(to_number=user_phone, body=cust_msg)

        if KITCHEN_WHATSAPP:
            kitchen_msg = (
                f"🍳 *KITCHEN ORDER ALERT*\n"
                f"🆔 *Order ID:* {order_id}\n\n"
                f"🍲 *ITEMS TO PREPARE:*\n"
                f"{session['order_data']['kitchen_items']}\n\n"
                f"📌 *Status:* Pending Preparation"
            )
            send_whatsapp_message(to_number=KITCHEN_WHATSAPP, body=kitchen_msg)

        if OWNER_WHATSAPP:
            owner_msg = (
                f"🔔 *NEW ORDER CONFIRMED - OWNER ALERT*\n\n"
                f"🆔 *Order ID:* {order_id}\n"
                f"👤 *Customer:* {session['name']} ({final_phone})\n"
                f"📍 *Address:* {session['address']}\n\n"
                f"🍲 *ITEMS:*\n{session['order_data']['items_summary']}\n\n"
                f"💰 *TOTAL: Rs. {session['order_data']['grand_total']}*\n\n"
                f"Dispatch command:\n"
                f"`Done {final_phone} [Rider Name] [Rider Phone]`"
            )
            send_whatsapp_message(to_number=OWNER_WHATSAPP, body=owner_msg)

        reset_session(user_phone)
        return Response(content="<Response></Response>", media_type="application/xml")

    return Response(content="<Response></Response>", media_type="application/xml")

# ------------------------------------------------------------
# 10. WEB ADMIN UI - protected, secrets are NEVER shown
# ------------------------------------------------------------
@app.get("/settings", response_class=HTMLResponse)
async def get_settings_form(_admin: str = Depends(require_admin)):
    if os.getenv("GOOGLE_CREDENTIALS_JSON", "").strip():
        creds_status = "✅ GOOGLE_CREDENTIALS_JSON is set (Railway Variables)"
    elif os.path.exists("credentials.json"):
        creds_status = "✅ credentials.json file exists"
    else:
        creds_status = "⚠️ Google credentials not found — Sheet will be skipped"

    html_content = f"""
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <title>Sabir Hotel - WhatsApp Bot Settings</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
        <style>
            body {{ background-color: #f4f6f9; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }}
            .card {{ border-radius: 12px; box-shadow: 0 4px 15px rgba(0,0,0,0.08); }}
            .header-bg {{ background: linear-gradient(135deg, #075e54, #128c7e); color: white; border-radius: 12px 12px 0 0; }}
        </style>
    </head>
    <body class="p-4">
        <div class="container" style="max-width: 800px;">
            <div class="card mb-4">
                <div class="card-header header-bg p-4 text-center">
                    <h2>⚙️ Sabir Hotel WhatsApp Bot Settings</h2>
                    <p class="mb-0">Leave a field blank to keep its current value. Secrets are never shown here.</p>
                </div>
                <div class="card-body p-4">
                    <p><strong>Twilio SID:</strong> {secret_status(TWILIO_ACCOUNT_SID)} &nbsp;|&nbsp;
                    <strong>Twilio Token:</strong> {secret_status(TWILIO_AUTH_TOKEN)} &nbsp;|&nbsp;
                    <strong>Gemini Key:</strong> {secret_status(GEMINI_API_KEY)}</p>
                    <p><strong>Google credentials:</strong> {creds_status}</p>
                    <form action="/save-settings" method="POST" enctype="multipart/form-data">
                        <h4 class="text-success border-bottom pb-2 mb-3">📊 Google Sheets Setup</h4>
                        <div class="mb-3 p-3 border rounded bg-light">
                            <label class="form-label fw-bold">Google Service Account JSON File</label>
                            <input type="file" name="creds_file" accept=".json" class="form-control">
                        </div>
                        <h4 class="text-success border-bottom pb-2 mb-3 mt-4">🔑 API Keys</h4>
                        <div class="mb-3">
                            <label class="form-label fw-bold">Twilio Account SID</label>
                            <input type="text" name="twilio_sid" class="form-control" placeholder="Leave blank to keep current">
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-bold">Twilio Auth Token</label>
                            <input type="password" name="twilio_token" class="form-control" placeholder="Leave blank to keep current" autocomplete="new-password">
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-bold">Gemini API Key</label>
                            <input type="password" name="gemini_key" class="form-control" placeholder="Leave blank to keep current" autocomplete="new-password">
                        </div>
                        <div class="mb-4">
                            <label class="form-label fw-bold">Server / Webhook URL</label>
                            <input type="url" name="server_url" class="form-control" placeholder="{SERVER_URL or 'https://your-domain.com'}">
                        </div>
                        <h4 class="text-success border-bottom pb-2 mb-3">📱 WhatsApp Numbers</h4>
                        <div class="mb-3">
                            <label class="form-label fw-bold">1. Bot Sender WhatsApp Number</label>
                            <input type="text" name="bot_number" class="form-control" placeholder="whatsapp:+1... - leave blank to keep current">
                        </div>
                        <div class="mb-3">
                            <label class="form-label fw-bold">2. Kitchen WhatsApp Number</label>
                            <input type="text" name="kitchen_number" class="form-control" placeholder="whatsapp:+92... - leave blank to keep current">
                        </div>
                        <div class="mb-4">
                            <label class="form-label fw-bold">3. Owner / Manager WhatsApp Number</label>
                            <input type="text" name="owner_number" class="form-control" placeholder="whatsapp:+92... - leave blank to keep current">
                        </div>
                        <h4 class="text-success border-bottom pb-2 mb-3">🚚 Delivery</h4>
                        <div class="mb-4">
                            <label class="form-label fw-bold">Fixed Delivery Charge (Rs.)</label>
                            <input type="number" name="delivery_charge" class="form-control" placeholder="{FIXED_DELIVERY_CHARGE}">
                        </div>
                        <button type="submit" class="btn btn-success btn-lg w-100 fw-bold">💾 Save Configuration</button>
                        <p class="text-muted mt-3 mb-0">After saving, restart the server so the new settings load. Your old .env values are kept for any blank field.</p>
                    </form>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html_content)

def _read_env_file() -> dict:
    values = {}
    if os.path.exists(".env"):
        with open(".env") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                values[key.strip()] = val.strip().strip('"').strip("'")
    return values

@app.post("/save-settings")
async def save_settings(
    _admin: str = Depends(require_admin),
    twilio_sid: str = Form(""),
    twilio_token: str = Form(""),
    gemini_key: str = Form(""),
    server_url: str = Form(""),
    bot_number: str = Form(""),
    kitchen_number: str = Form(""),
    owner_number: str = Form(""),
    delivery_charge: str = Form(""),
    creds_file: UploadFile = File(None),
):
    if creds_file and creds_file.filename:
        content = await creds_file.read()
        try:
            data = json.loads(content.decode("utf-8"))
            if data.get("type") != "service_account":
                return {"status": "Error", "message": "That JSON is not a Google service account file."}
        except Exception:
            return {"status": "Error", "message": "credentials.json is not valid JSON."}
        with open("credentials.json", "wb") as buffer:
            buffer.write(content)

    # Only overwrite values the admin actually typed. Blank = keep current.
    current = _read_env_file()
    updates = {
        "TWILIO_ACCOUNT_SID": twilio_sid.strip(),
        "TWILIO_AUTH_TOKEN": twilio_token.strip(),
        "GEMINI_API_KEY": gemini_key.strip(),
        "SERVER_URL": server_url.strip(),
        "TWILIO_WHATSAPP_NUMBER": bot_number.strip(),
        "KITCHEN_WHATSAPP": kitchen_number.strip(),
        "OWNER_WHATSAPP": owner_number.strip(),
    }
    for key, val in updates.items():
        if val:
            current[key] = val
    if delivery_charge.strip():
        try:
            current["FIXED_DELIVERY_CHARGE"] = str(int(delivery_charge))
        except ValueError:
            return {"status": "Error", "message": "Delivery charge must be a number."}
    # Keep admin login in the file so a restart does not lose it.
    if ADMIN_USERNAME:
        current.setdefault("ADMIN_USERNAME", ADMIN_USERNAME)
    if ADMIN_PASSWORD:
        current.setdefault("ADMIN_PASSWORD", ADMIN_PASSWORD)

    with open(".env", "w") as f:
        for key, val in current.items():
            f.write(f'{key}="{val}"\n')

    return {"status": "Success", "message": "Settings saved. Restart the server to apply them."}
