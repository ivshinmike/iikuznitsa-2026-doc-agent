import re
import time
from uuid import uuid4

from .answers import MISSING
from .corpus import UNITS, connect, normalize


ALIASES = {
    "dbrct": re.compile(r"\b(?:дбрцт|dbrct)\b", re.I),
    "security": re.compile(r"\b(?:иб|security|информационн\w* безопасност\w*)\b", re.I),
    "technical": re.compile(r"\b(?:тд|technical|тех(?:ническ\w*)?\s*дирекц\w*)\b", re.I),
}
SCOPE_MESSAGE = "Уточните подразделение. Правила подразделений различаются."


def prepare_dialogue(database):
    with connect(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS pending_dialogue (
            id TEXT PRIMARY KEY, question TEXT NOT NULL, unit TEXT, mode TEXT NOT NULL,
            attempts INTEGER NOT NULL, expires INTEGER NOT NULL)""")


def remember(database, question, unit, mode, attempts, message):
    token = uuid4().hex
    now = int(time.time())
    with connect(database) as db:
        db.execute("DELETE FROM pending_dialogue WHERE expires <= ?", (now,))
        db.execute("INSERT INTO pending_dialogue VALUES (?, ?, ?, ?, ?, ?)",
                   (token, question, unit, mode, attempts, now + 3600))
    result = {"status": "clarify", "message": message, "turn": token, "mode": mode}
    if mode == "scope":
        result["options"] = UNITS
    return result


def resume(database, token):
    with connect(database) as db:
        return db.execute("DELETE FROM pending_dialogue WHERE id=? AND expires>? RETURNING *",
                          (token, int(time.time()))).fetchone()


def infer_unit(text):
    return {unit for unit, pattern in ALIASES.items() if pattern.search(text)}


def resolve(database, question, unit, token, answer_question):
    previous = resume(database, token) if token else None
    if token and previous is None:
        return {"status": "expired", "message": "Уточнение устарело. Задайте исходный вопрос ещё раз."}
    question = normalize(question)
    named = infer_unit(question)
    if unit:
        named.add(unit)
    attempts = previous["attempts"] if previous else 0
    if previous and previous["mode"] == "detail":
        if named - {previous["unit"]}:
            return remember(database, previous["question"], previous["unit"], "detail", attempts,
                            "Уточните условия для прежнего подразделения или начните новый вопрос.")
        named.add(previous["unit"])
        question = f"{previous['question']} Уточнение: {question}"
    elif previous:
        question = previous["question"]
    if len(named) != 1:
        if attempts >= 2:
            return {"status": "missing", "message": MISSING}
        return remember(database, question, None, "scope", attempts + 1, SCOPE_MESSAGE)
    unit, = named
    result = answer_question(database, question, unit)
    if result["status"] == "clarify":
        if attempts >= 2:
            return {"status": "missing", "message": MISSING, "unit": unit}
        return remember(database, question, unit, "detail", attempts + 1, result["message"])
    if result["status"] == "unavailable" and previous:
        retry = remember(database, previous["question"], previous["unit"], previous["mode"],
                         previous["attempts"], result["message"])
        retry["status"] = "unavailable"
        return retry
    result["unit"] = unit
    return result
