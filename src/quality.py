import re
from uuid import uuid4

from .corpus import connect, normalize


def prepare_quality(database):
    with connect(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS quality_cases (
            id TEXT PRIMARY KEY, unit TEXT NOT NULL, question TEXT NOT NULL,
            question_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT, resolution TEXT, document TEXT, point TEXT,
            heading TEXT, revision TEXT, UNIQUE(unit, question_key))""")


def question_key(question):
    # Ignore punctuation and case while preserving words, order, and negations.
    return " ".join(re.findall(r"\w+", question.casefold().replace("ё", "е")))


def open_case(database, question, unit):
    key = question_key(question)
    with connect(database) as db:
        db.execute("""INSERT INTO quality_cases (id, unit, question, question_key)
            VALUES (?, ?, ?, ?) ON CONFLICT(unit, question_key) DO NOTHING""",
                   (uuid4().hex, unit, question, key))
        row = db.execute("SELECT id FROM quality_cases WHERE unit=? AND question_key=?",
                         (unit, key)).fetchone()
    return row["id"]


def find_approval(database, question, unit):
    with connect(database) as db:
        row = db.execute("""SELECT k.* FROM quality_cases k
            WHERE k.unit=? AND k.question_key=? AND k.approved_at IS NOT NULL
            AND EXISTS (SELECT 1 FROM passages p WHERE p.unit=k.unit AND p.document=k.document
                AND p.point=k.point AND p.revision=k.revision)""",
                         (unit, question_key(question))).fetchone()
    if not row:
        return None
    return {"status": "approved", "message": "Ответ подтверждён службой качества",
            "answer": row["resolution"], "case_id": row["id"], "approved_at": row["approved_at"],
            "sources": [{key: row[key] for key in ("unit", "document", "point", "heading", "revision")}],
            "unit": unit}


def get_case(database, case_id):
    with connect(database) as db:
        row = db.execute("SELECT id, unit, question, approved_at, resolution, document, point FROM quality_cases WHERE id=?",
                         (case_id,)).fetchone()
        if not row:
            raise LookupError("Запрос не найден")
        options = db.execute("""SELECT DISTINCT document, point, heading FROM passages WHERE unit=?
            ORDER BY document, CAST(point AS INTEGER), point, heading""", (row["unit"],))
        return {"case": dict(row), "points": [dict(option) for option in options]}


def approve(database, case_id, resolution, document, point):
    resolution = normalize(resolution)
    if len(resolution) < 5:
        raise ValueError("Введите подтверждённый ответ")
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT unit, revision, approved_at FROM quality_cases WHERE id=?",
                         (case_id,)).fetchone()
        if not row:
            raise LookupError("Запрос не найден")
        if row["approved_at"]:
            current = db.execute("SELECT 1 FROM passages WHERE revision=? LIMIT 1", (row["revision"],)).fetchone()
            if current:
                raise FileExistsError("Запрос уже подтверждён")
        sources = db.execute("""SELECT DISTINCT heading, revision FROM passages
            WHERE unit=? AND document=? AND point=?""", (row["unit"], document, point)).fetchall()
        if len(sources) != 1:
            raise ValueError("Выберите однозначный пункт регламента этого подразделения")
        db.execute("""UPDATE quality_cases SET resolution=?, document=?, point=?, heading=?,
            revision=?, approved_at=CURRENT_TIMESTAMP WHERE id=?""",
                   (resolution, document, point, sources[0]["heading"], sources[0]["revision"], case_id))
