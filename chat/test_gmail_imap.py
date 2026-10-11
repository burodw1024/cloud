import imaplib
import getpass

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
EMAIL_USER = "burodw1024@gmail.com"

password = getpass.getpass("xkyy ovsx zony qvcb")

print("HOST:", IMAP_HOST)
print("PORT:", IMAP_PORT)
print("USER:", EMAIL_USER)
print("PASSWORD SET:", bool(password))

if not password:
    raise RuntimeError("Password was not entered")

mail = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
print("Connected")

mail.login(EMAIL_USER, password)
print("Login OK")

mail.select("INBOX")
status, data = mail.search(None, "ALL")

print("Search status:", status)
print("Total emails:", len(data[0].split()) if data and data[0] else 0)

mail.logout()
