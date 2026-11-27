import os
import smtplib
from email.message import EmailMessage

from .corpus import UNITS, connect


def notify(database, case_id):
    port = os.environ.get("IIKUZNITSA_MAILPIT_SMTP_PORT")
    if not port:
        return None
    with connect(database) as db:
        case = db.execute("""UPDATE quality_cases SET mail_state='sending'
            WHERE id=? AND mail_state IS NULL AND approved_at IS NULL
            RETURNING unit, question""", (case_id,)).fetchone()
        if case is None:
            return db.execute("SELECT mail_state FROM quality_cases WHERE id=?", (case_id,)).fetchone()["mail_state"]

    message = EmailMessage()
    message["From"] = "agent@iikuznitsa.test"
    message["To"] = "quality@iikuznitsa.test"
    message["Subject"] = "Запрос в службу качества № " + case_id
    message["Message-ID"] = f"<{case_id}@iikuznitsa.test>"
    message.set_content(f"Запрос № {case_id}\nПодразделение: {UNITS[case['unit']]}\n\n"
                        f"{case['question']}\n\nАгент не нашёл подтверждённого ответа.")
    state = "sent"
    try:
        with smtplib.SMTP("127.0.0.1", int(port), timeout=2) as client:
            client.send_message(message)
    except (OSError, ValueError, OverflowError):
        state = "failed"
    with connect(database) as db:
        db.execute("UPDATE quality_cases SET mail_state=? WHERE id=?", (state, case_id))
    return state
