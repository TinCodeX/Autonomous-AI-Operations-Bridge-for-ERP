"""Diagnostic check for Odoo JSON-RPC authentication and Groq LLM connectivity."""

import os

import requests
from dotenv import load_dotenv
from groq import Groq, GroqError

load_dotenv()

# 1. Test Odoo Auth
url = os.getenv("ODOO_URL")
db = os.getenv("ODOO_DB")
user = os.getenv("ODOO_USER")
pwd = os.getenv("ODOO_PASSWORD")

print("--- DIAGNOSTIC CHECK ---")
try:
    payload = {
        "jsonrpc": "2.0",
        "method": "call",
        "params": {
            "service": "common",
            "method": "authenticate",
            "args": [db, user, pwd, {}],
        },
        "id": 1,
    }
    res = requests.post(f"{url}/jsonrpc", json=payload, timeout=5).json()
    uid = res.get("result")
    if uid:
        print(f"✅ Odoo Auth: OK (UID: {uid}, DB: {db})")
    else:
        print(f"❌ Odoo Auth Failed: {res}")
except (requests.RequestException, ValueError, OSError) as exc:
    print(f"❌ Odoo Connection Error: {exc}")

# 2. Test Groq
try:
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    model = os.getenv("LLM_MODEL", "openai/gpt-oss-20b")
    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "respond with pong"}],
        max_tokens=10,
    )
    reply = completion.choices[0].message.content.strip()
    print(f"✅ Groq LLM: OK ({model} -> {reply})")
except GroqError as exc:
    print(f"❌ Groq Error: {exc}")
