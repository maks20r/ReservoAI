from flask import Flask, request, render_template, jsonify
from openai import OpenAI
import spacy
from google.oauth2 import service_account
from googleapiclient.discovery import build
import os
import datetime
import pytz
from dateutil import parser
from dateutil.relativedelta import relativedelta
import re

app = Flask(__name__)
nlp = spacy.load("en_core_web_sm")
ID = ""
client = OpenAI(api_key="")
conversation_state = {}

SCOPES = ['https://www.googleapis.com/auth/calendar']
UAE_TZ = pytz.timezone('Asia/Dubai')
SERVICE_ACCOUNT_FILE = 'service_account_file.json'
CALENDAR_ID = 'd378bb6715e5eae240ea302dab2c8e2ef92f7f9a6d7e8555d5b885d13b095721@group.calendar.google.com'

def get_calendar_service():
    credentials = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    return build('calendar', 'v3', credentials=credentials)

def is_time_slot_available(start_time, end_time):
    if start_time.hour < 9 or end_time.hour > 18 or (end_time.hour == 18 and end_time.minute > 0):
        return False

    service = get_calendar_service()
    events_result = service.events().list(
        calendarId=CALENDAR_ID,
        timeMin=start_time.isoformat(),
        timeMax=end_time.isoformat(),
        singleEvents=True,
        orderBy='startTime'
    ).execute()
    events = events_result.get('items', [])
    
    return len(events) == 0

def find_next_available_slot(start_time, end_time, appointment_duration):
    service = get_calendar_service()
    max_attempts = 10
    
    for _ in range(max_attempts):
        events_result = service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=start_time.isoformat(),
            timeMax=(start_time + datetime.timedelta(days=7)).isoformat(),
            singleEvents=True,
            orderBy='startTime'
        ).execute()
        events = events_result.get('items', [])
        
        while start_time.hour < 9:
            start_time = start_time.replace(hour=9, minute=0, second=0, microsecond=0)
        
        if start_time.hour >= 18:
            start_time = (start_time + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        
        if not events:
            end_time = start_time + appointment_duration
            if end_time.hour <= 18 or (end_time.hour == 18 and end_time.minute == 0):
                return start_time, end_time
            else:
                start_time = start_time.replace(hour=9, minute=0, second=0, microsecond=0) + datetime.timedelta(days=1)
                continue
        
        for event in events:
            event_start = datetime.datetime.fromisoformat(event['start'].get('dateTime', event['start'].get('date')))
            event_end = datetime.datetime.fromisoformat(event['end'].get('dateTime', event['end'].get('date')))
            
            if start_time + appointment_duration <= event_start:
                end_time = start_time + appointment_duration
                if end_time.hour <= 18 or (end_time.hour == 18 and end_time.minute == 0):
                    return start_time, end_time
            
            start_time = max(start_time, event_end)
            if start_time.hour >= 18:
                start_time = (start_time + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
    
    return None, None

def create_event(summary, start_time, end_time, description):
    if not is_time_slot_available(start_time, end_time):
        return None
    
    service = get_calendar_service()
    event = {
        'summary': summary,
        'description': description,
        'start': {
            'dateTime': start_time.isoformat(),
            'timeZone': 'Asia/Dubai',
        },
        'end': {
            'dateTime': end_time.isoformat(),
            'timeZone': 'Asia/Dubai',
        },
    }
    event = service.events().insert(calendarId=CALENDAR_ID, body=event).execute()
    return event.get('htmlLink')

def format_response(response):
    if "*" in response:
        lines = response.split("\n")
        formatted_lines = []
        in_list = False
        for line in lines:
            if line.startswith("*"):
                if not in_list:
                    formatted_lines.append("<ul>")
                    in_list = True
                formatted_lines.append(f"<li>{line[1:].strip()}</li>")
            else:
                if in_list:
                    formatted_lines.append("</ul>")
                    in_list = False
                formatted_lines.append(line)
        if in_list:
            formatted_lines.append("</ul>")
        response = "<br>".join(formatted_lines)
    else:
        response = response.replace('\n', '<br>')
    return response

def parse_date_time(date_time_str):
    try:
        now = datetime.datetime.now(UAE_TZ)
        dt = parser.parse(date_time_str, fuzzy=True)
        if dt.date() == datetime.date(1, 1, 1):
            if dt.time() < now.time():
                dt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0) + datetime.timedelta(days=1)
            else:
                dt = now.replace(hour=dt.hour, minute=dt.minute, second=0, microsecond=0)
        if 'tomorrow' in date_time_str.lower():
            dt += datetime.timedelta(days=1)
        if dt.tzinfo is None:
            dt = UAE_TZ.localize(dt)
        else:
            dt = dt.astimezone(UAE_TZ)
        
        if dt.hour < 9:
            dt = dt.replace(hour=9, minute=0, second=0, microsecond=0)
        elif dt.hour >= 18:
            dt = (dt + datetime.timedelta(days=1)).replace(hour=9, minute=0, second=0, microsecond=0)
        
        end_time = dt + datetime.timedelta(hours=1)
        if end_time.hour > 18 or (end_time.hour == 18 and end_time.minute > 0):
            end_time = dt.replace(hour=18, minute=0, second=0, microsecond=0)
        
        return dt, end_time
    except ValueError:
        return None, None

def extract_date_time_and_type(doc):
    date_time_str = " ".join([ent.text for ent in doc.ents if ent.label_ in ["DATE", "TIME"]])
    if not any(ent.label_ == "DATE" for ent in doc.ents) and "tomorrow" in [token.text.lower() for token in doc]:
        date_time_str = "tomorrow " + date_time_str
    
    appointment_types = [
        "classic manicure", "deluxe pedicure", "facial treatment", "haircut and styling",
        "hair coloring", "waxing", "eyelash extensions", "microdermabrasion",
        "chemical peel", "massage therapy", "bridal makeup", "hair spa treatment"
    ]
    
    appointment_type = None
    for type in appointment_types:
        if type.lower() in doc.text.lower():
            appointment_type = type
            break
    return date_time_str, appointment_type

def extract_name_and_phone(message):
    doc = nlp(message)
    name = None
    phone = None
    for ent in doc.ents:
        if ent.label_ == "PERSON":
            name = ent.text
        elif ent.label_ == "CARDINAL" and len(ent.text) >= 7:
            phone = ent.text
    return name, phone

def validate_name(name):
    if name and len(name.split()) >= 2:
        return True
    return False

def validate_phone(phone):
    phone_regex = re.compile(r'^\+?1?\d{9,15}$')
    return phone_regex.match(phone) is not None 

def process_message(message, user_state):
    user_state["last_message"] = message
    user_state["messages"].append({"role": "user", "content": message})

    if "booking_state" in user_state and user_state["booking_state"] in ["awaiting_new_time", "prompt_new_time"]:
        date_time_str, new_appointment_type = extract_date_time_and_type(nlp(message.lower()))
        if date_time_str:
            start_time, end_time = parse_date_time(date_time_str)
            if start_time and end_time:
                if is_time_slot_available(start_time, end_time):
                    appointment_type = user_state.get("pending_appointment", {}).get("type", "appointment")
                    if "pending_appointment" not in user_state:
                        user_state["pending_appointment"] = {}
                    user_state["pending_appointment"].update({
                        "type": appointment_type,
                        "start_time": start_time.isoformat(),
                        "end_time": end_time.isoformat(),
                    })
                    del user_state["booking_state"]
                    reply = f"Great! I've found an available slot for your appointment on {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time. "
                    if "service" not in user_state["pending_appointment"]:
                        user_state["gathering_info"] = "service"
                        reply += "What service would you like to book? (e.g., Classic Manicure, Deluxe Pedicure, Facial Treatment, etc.)"
                    elif "name" not in user_state["pending_appointment"]:
                        user_state["gathering_info"] = "name"
                        reply += "Could you please provide your full name?"
                    elif "phone" not in user_state["pending_appointment"]:
                        user_state["gathering_info"] = "phone"
                        reply += "Could you please provide your phone number?"
                    else:
                        reply += "Is this information correct? Please respond with 'Yes' to confirm or 'No' to cancel."
                else:
                    reply = f"I'm sorry, but the time slot you requested ({start_time.strftime('%Y-%m-%d %I:%M %p')}) is either outside our business hours (9 AM to 5 PM) or already taken. Would you like to try another time?"
                    user_state["booking_state"] = "awaiting_new_time"
            else:
                reply = "I'm sorry, I couldn't understand the date and time. Could you please specify it more clearly? (e.g., 'next Monday at 2 PM')"
        else:
            reply = "I didn't catch a date and time in your message. Could you please specify when you'd like to schedule the appointment? Remember, our business hours are from 9 AM to 5 PM."
        user_state["messages"].append({"role": "assistant", "content": reply})
        return reply, user_state

    chat = client.beta.threads.create(messages=user_state["messages"])
    run = client.beta.threads.runs.create(thread_id=chat.id, assistant_id=ID)
    while run.status != "completed":
        run = client.beta.threads.runs.retrieve(thread_id=chat.id, run_id=run.id)
    message_response = client.beta.threads.messages.list(thread_id=chat.id)
    messages = message_response.data
    latest_message = messages[0]
    reply = latest_message.content[0].text.value

    if "gathering_info" in user_state:
        if user_state["gathering_info"] == "service":
            if "pending_appointment" not in user_state:
                user_state["pending_appointment"] = {}
            user_state["pending_appointment"]["service"] = message
            user_state["gathering_info"] = "date_time"
            reply = f"Thank you. You've selected {message} as your service. Now, could you please provide your preferred date and time for the appointment? (e.g., 'next Monday at 2 PM')"
        elif user_state["gathering_info"] == "date_time":
            date_time_str, _ = extract_date_time_and_type(nlp(message.lower()))
            if date_time_str:
                start_time, end_time = parse_date_time(date_time_str)
                if start_time and end_time:
                    if is_time_slot_available(start_time, end_time):
                        user_state["pending_appointment"].update({
                            "start_time": start_time.isoformat(),
                            "end_time": end_time.isoformat()
                        })
                        user_state["gathering_info"] = "name"
                        reply = f"Great! I've found an available slot for your {user_state['pending_appointment']['service']} appointment on {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time. Could you please provide your full name?"
                    else:
                        next_start, next_end = find_next_available_slot(start_time, end_time, datetime.timedelta(hours=1))
                        if next_start and next_end:
                            user_state["booking_state"] = "suggest_new_time"
                            user_state["suggested_time"] = {
                                "start": next_start.isoformat(),
                                "end": next_end.isoformat()
                            }
                            reply = f"I'm sorry, but the time slot you requested ({start_time.strftime('%Y-%m-%d %I:%M %p')}) is not available. The next available slot is on {next_start.strftime('%Y-%m-%d %I:%M %p')}. Would you like to book this slot instead? Please respond with 'Yes' to confirm or 'No' to choose a different time."
                        else:
                            user_state["booking_state"] = "awaiting_new_time"
                            reply = f"I'm sorry, but the time slot you requested ({start_time.strftime('%Y-%m-%d %I:%M %p')}) is not available, and I couldn't find an available slot in the near future. Would you like to choose a different time?"
                else:
                    reply = "I'm sorry, I couldn't understand the date and time. Could you please specify it more clearly? (e.g., 'next Monday at 2 PM')"
            else:
                reply = "I didn't catch a date and time in your message. Could you please specify when you'd like to schedule the appointment? Remember, our business hours are from 9 AM to 5 PM."
        elif user_state["gathering_info"] == "name":
            if "pending_appointment" not in user_state:
                user_state["pending_appointment"] = {}
            user_state["pending_appointment"]["name"] = message
            user_state["gathering_info"] = "phone"
            reply = "Thank you. Now, could you please provide your phone number?"
        elif user_state["gathering_info"] == "phone":
            if "pending_appointment" not in user_state:
                user_state["pending_appointment"] = {}
            user_state["pending_appointment"]["phone"] = message
            del user_state["gathering_info"]
            appointment = user_state["pending_appointment"]
            
            start_time_str = appointment.get('start_time')
            if start_time_str:
                try:
                    start_time = datetime.datetime.fromisoformat(start_time_str)
                    start_time_display = start_time.strftime('%Y-%m-%d %I:%M %p')
                except ValueError:
                    start_time_display = "Invalid date/time"
            else:
                start_time_display = "Not specified"

            reply = f"Great! I have the following details for your appointment:\n\n"
            reply += f"Service: {appointment.get('service', 'Not specified')}\n"
            reply += f"Date and Time: {start_time_display} UAE time\n"
            reply += f"Name: {appointment.get('name', 'Not specified')}\n"
            reply += f"Phone: {appointment.get('phone', 'Not specified')}\n\n"
            reply += "Is this information correct? Please respond with 'Yes' to confirm or 'No' to cancel."
    elif any(token.text.lower() in ["book", "schedule", "appointment"] for token in nlp(message.lower())) or \
         ("booking_state" in user_state and user_state["booking_state"] == "prompt_new_time"):
        date_time_str, new_appointment_type = extract_date_time_and_type(nlp(message.lower()))
        


        services = [
            "Classic Manicure",
            "Deluxe Pedicure",
            "Facial Treatment",
            "Haircut and Styling",
            "Hair Coloring",
            "Waxing (Full Body)",
            "Eyelash Extensions",
            "Microdermabrasion",
            "Chemical Peel",
            "Massage Therapy (1 hour)",
            "Bridal Makeup",
            "Hair Spa Treatment"
        ]

        numbered_services = [f"{i+1}. {service}" for i, service in enumerate(services)]
        services_list = "\n".join(numbered_services)

        if not date_time_str and not new_appointment_type:
            user_state["gathering_info"] = "service"
            reply = f"I understand you want to book an appointment. What service would you like to book? We offer:\n\n{services_list}"

        else:
            if "pending_appointment" in user_state and "service" in user_state["pending_appointment"]:
                appointment_type = user_state["pending_appointment"]["service"]
            else:
                appointment_type = new_appointment_type

            if date_time_str:
                start_time, end_time = parse_date_time(date_time_str)
                if start_time and end_time:
                    if appointment_type:
                        appointment_duration = end_time - start_time
                        if is_time_slot_available(start_time, end_time):
                            name = user_state.get("pending_appointment", {}).get("name")
                            phone = user_state.get("pending_appointment", {}).get("phone")
                            user_state["pending_appointment"] = {
                                "service": appointment_type,
                                "start_time": start_time.isoformat(),
                                "end_time": end_time.isoformat(),
                                "name": name,
                                "phone": phone
                            }
                            if "booking_state" in user_state:
                                del user_state["booking_state"]
                            if not appointment_type:
                                user_state["gathering_info"] = "service"
                                reply = f"la I understand you want to book an appointment for {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time. What service would you like to book? (e.g., Classic Manicure, Deluxe Pedicure, Facial Treatment, etc.)"
                            elif not name:
                                user_state["gathering_info"] = "name"
                                reply = f"I understand you want to book a {appointment_type} appointment for {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time. Could you please provide your full name?"
                            elif not phone:
                                user_state["gathering_info"] = "phone"
                                reply = f"Thank you, {name}. Could you please provide your phone number?"
                            else:
                                reply = f"I understand you want to book a {appointment_type} appointment for {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time.\n\n"
                                reply += f"Name: {name}\nPhone: {phone}\n\n"
                                reply += "Is this information correct? Please respond with 'Yes' to confirm or 'No' to cancel."
                        else:
                            next_start, next_end = find_next_available_slot(start_time, end_time, appointment_duration)
                            if next_start and next_end:
                                user_state["booking_state"] = "suggest_new_time"
                                user_state["suggested_time"] = {
                                    "start": next_start.isoformat(),
                                    "end": next_end.isoformat()
                                }
                                reply = f"Unfortunately, the time slot you requested ({start_time.strftime('%Y-%m-%d %I:%M %p')}) is either outside our business hours (9 AM to 5 PM) or already taken. "
                                reply += f"\n\nThe next available slot is on {next_start.strftime('%Y-%m-%d %I:%M %p')}. "
                                reply += "\n\nWould you like to book this slot instead? Please respond with 'Yes' to confirm or 'No' to choose a different time."
                            else:
                                user_state["booking_state"] = "awaiting_new_time"
                                reply = f"Unfortunately, the time slot you requested ({start_time.strftime('%Y-%m-%d %I:%M %p')}) is either outside our business hours (9 AM to 5 PM) or already taken. "
                                reply += "Additionally, I couldn't find an available slot in the near future. Would you like to choose a different time?"
                    else:
                        user_state["gathering_info"] = "service"
                        user_state["pending_appointment"] = {
                            "start_time": start_time.isoformat(),
                            "end_time": end_time.isoformat()
                        }
                        reply = f"I understand you want to book an appointment, what service would you like to book? (e.g., Classic Manicure, Deluxe Pedicure, Facial Treatment, etc.)"
                else:
                    reply += "\n\nI'm sorry, I couldn't understand the date and time for the appointment. Could you please specify it more clearly? Please note that our business hours are from 9 AM to 5 PM."
            elif appointment_type:
                user_state["gathering_info"] = "date_time"
                user_state["pending_appointment"] = {"service": appointment_type}
                reply = f"I understand you want to book a {appointment_type} appointment. What date and time would you prefer? Please note that our business hours are from 9 AM to 5 PM."
            else:
                reply += "\n\nI understand you want to book an appointment, but could you please specify what type of service you'd like? (e.g., Classic Manicure, Deluxe Pedicure, Facial Treatment, etc.)"
    elif "booking_state" in user_state and user_state["booking_state"] == "suggest_new_time":
        if message.lower() == "yes":
            start_time = datetime.datetime.fromisoformat(user_state["suggested_time"]["start"])
            end_time = datetime.datetime.fromisoformat(user_state["suggested_time"]["end"])
            appointment_type = user_state.get("pending_appointment", {}).get("service", "appointment")
            user_state["pending_appointment"] = {
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat()
            }
            del user_state["booking_state"]
            del user_state["suggested_time"]
            reply = f"Great! I've found an available slot for your appointment on {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time. "
            user_state["gathering_info"] = "service"
            reply += "\n\nWhat service would you like to book? (e.g., Classic Manicure, Deluxe Pedicure, Facial Treatment, etc.)"
        elif message.lower() == "no":
            user_state["booking_state"] = "awaiting_new_time"
            reply = "I understand. Would you like to choose a different time for your appointment?"
        else:
            reply = "I'm sorry, I didn't understand your response. Please respond with 'Yes' to confirm the suggested time slot, or 'No' to choose a different time."
    elif "pending_appointment" in user_state and "gathering_info" not in user_state:
        if message.lower() == "yes":
            appointment = user_state["pending_appointment"]
            event_summary = f"{appointment['service'].capitalize()} Appointment"
            description = f"Service: {appointment['service']}\nName: {appointment['name']}\nPhone: {appointment['phone']}"
            start_time = datetime.datetime.fromisoformat(appointment['start_time'])
            end_time = datetime.datetime.fromisoformat(appointment['end_time'])
            event_link = create_event(event_summary, start_time, end_time, description)
            if event_link:
                reply = f"Great! I've booked your {appointment['service']} appointment for {start_time.strftime('%Y-%m-%d %I:%M %p')} UAE time. You can view it here: {event_link}"
                reply += "\n\nIs there anything else I can help you with?"
                del user_state["pending_appointment"]
            else:
                reply = "I apologize, but it seems the time slot is no longer available. Would you like to choose a different time?"
                user_state["booking_state"] = "awaiting_new_time"
        elif message.lower() == "no":
            reply = "I understand. The appointment has not been booked. Is there anything else I can help you with?"
            del user_state["pending_appointment"]
        else:
            reply = "I'm waiting for your confirmation about the pending appointment. Please respond with 'Yes' to confirm or 'No' to cancel."
    else:
        reply += "\n\nIs there anything specific you'd like to know about our services or booking an appointment?"

    reply = format_response(reply)
    user_state["messages"].append({"role": "assistant", "content": reply})
    return reply, user_state


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/chat", methods=["POST"])
def chat():
    incoming_msg = request.json.get("message", "").strip()
    user_state = request.json.get("state", {})
    if not incoming_msg:
        return jsonify({"error": "Invalid request"}), 400
    
    if not user_state:
        user_state = {"messages": []}
    
    reply, updated_state = process_message(incoming_msg, user_state)
    return jsonify({"reply": reply, "state": updated_state})

if __name__ == "__main__":
    app.run(port=8080, debug=True)
