import os
import sys
import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import base64
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP

load_dotenv()

mcp = FastMCP("Gmail MCP Server")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "").replace(" ", "")


def _decode_str(header_val: str | None) -> str:
    if not header_val:
        return ""
    decoded_parts = decode_header(header_val)
    result = []
    for part, encoding in decoded_parts:
        if isinstance(part, bytes):
            result.append(part.decode(encoding or "utf-8", errors="replace"))
        else:
            result.append(part)
    return "".join(result)


@mcp.tool()
def send_email(
    to_email: str,
    subject: str,
    body: str,
    attachment_path: str = "",
) -> str:
    """
    Send an email via Gmail SMTP. To attach a generated PDF report, pass its local
    file path in attachment_path (for example: '/tmp/INV_1.pdf').
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured in .env."

    try:
        msg = MIMEMultipart()
        msg["From"] = GMAIL_ADDRESS
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        if attachment_path:
            if not os.path.exists(attachment_path):
                return f"ERROR: Attachment file not found at '{attachment_path}'."
            filename = os.path.basename(attachment_path)
            with open(attachment_path, "rb") as f:
                part = MIMEApplication(f.read(), Name=filename)
            part["Content-Disposition"] = f'attachment; filename="{filename}"'
            msg.attach(part)

        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as server:
            server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
            server.send_message(msg)

        return f"SUCCESS: Email sent to {to_email} with subject '{subject}'."
    except Exception as e:
        return f"ERROR sending email: {str(e)}"


@mcp.tool()
def search_emails(query: str = "ALL", max_results: int = 5) -> str:
    """
    Search Gmail inbox using IMAP criteria (e.g., 'ALL', 'UNSEEN', 'FROM \"client@example.com\"', 'SUBJECT \"Invoice\"').
    Returns a summary list of matching email IDs, senders, subjects, and dates.
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured in .env."

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, query)
        if status != "OK" or not messages[0]:
            mail.logout()
            return "No emails found matching that query."

        email_ids = messages[0].split()[-max_results:]
        results = []

        for eid in reversed(email_ids):
            _, msg_data = mail.fetch(eid, "(RFC822.HEADER)")
            raw_header = msg_data[0][1]
            msg = email.message_from_bytes(raw_header)
            subject = _decode_str(msg.get("Subject"))
            sender = _decode_str(msg.get("From"))
            date = msg.get("Date", "")
            results.append(f"ID: {eid.decode()} | Date: {date} | From: {sender} | Subject: {subject}")

        mail.logout()
        return "\n".join(results)
    except Exception as e:
        return f"ERROR searching emails: {str(e)}"


@mcp.tool()
def read_email(email_id: str) -> str:
    """
    Read the full plain-text body of a specific email by its IMAP ID (obtained from search_emails).
    """
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "ERROR: GMAIL_ADDRESS or GMAIL_APP_PASSWORD not configured in .env."

    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        status, msg_data = mail.fetch(email_id.encode(), "(RFC822)")
        if status != "OK":
            mail.logout()
            return f"ERROR: Could not fetch email ID {email_id}."

        msg = email.message_from_bytes(msg_data[0][1])
        subject = _decode_str(msg.get("Subject"))
        sender = _decode_str(msg.get("From"))
        body = ""

        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain" and "attachment" not in str(part.get("Content-Disposition")):
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = payload.decode(msg.get_content_charset() or "utf-8", errors="replace")

        mail.logout()
        return f"From: {sender}\nSubject: {subject}\n\n{body[:3000]}"
    except Exception as e:
        return f"ERROR reading email: {str(e)}"


if __name__ == "__main__":
    print("[+] Starting Gmail MCP server over stdio...", file=sys.stderr)
    mcp.run(transport="stdio")