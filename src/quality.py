import re
from uuid import uuid4

from .corpus import connect, normalize
from .dialogue import infer_unit


def prepare_quality(database):
    with connect(database) as db:
        db.execute("""CREATE TABLE IF NOT EXISTS quality_cases (
            id TEXT PRIMARY KEY, unit TEXT NOT NULL, question TEXT NOT NULL,
            question_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            approved_at TEXT, resolution TEXT, document TEXT, point TEXT,
            heading TEXT, revision TEXT, UNIQUE(unit, question_key))""")
        db.execute("""CREATE TABLE IF NOT EXISTS quality_aliases (
            unit TEXT NOT NULL, question_key TEXT NOT NULL, alternative TEXT NOT NULL,
            case_id TEXT NOT NULL UNIQUE REFERENCES quality_cases(id), PRIMARY KEY(unit, question_key))""")


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
            WHERE k.unit=:unit AND k.approved_at IS NOT NULL
            AND (k.question_key=:key OR EXISTS (SELECT 1 FROM quality_aliases a
                 WHERE a.case_id=k.id AND a.unit=k.unit AND a.question_key=:key))
            AND EXISTS (SELECT 1 FROM passages p WHERE p.unit=k.unit AND p.document=k.document
                AND p.point=k.point AND p.revision=k.revision)""",
                         {"unit": unit, "key": question_key(question)}).fetchone()
    if not row:
        return None
    return {"status": "approved", "message": "Ответ подтверждён службой качества",
            "answer": row["resolution"], "case_id": row["id"], "approved_at": row["approved_at"],
            "sources": [{key: row[key] for key in ("unit", "document", "point", "heading", "revision")}],
            "unit": unit}


def get_case(database, case_id):
    with connect(database) as db:
        row = db.execute("""SELECT k.id, k.unit, k.question, k.approved_at, k.resolution,
            k.document, k.point, a.alternative FROM quality_cases k
            LEFT JOIN quality_aliases a ON a.case_id=k.id WHERE k.id=?""", (case_id,)).fetchone()
        if not row:
            raise LookupError("Запрос не найден")
        options = db.execute("""SELECT DISTINCT document, point, heading FROM passages WHERE unit=?
            ORDER BY document, CAST(point AS INTEGER), point, heading""", (row["unit"],))
        return {"case": dict(row), "points": [dict(option) for option in options]}


def approve(database, case_id, resolution, document, point, alternative=None):
    resolution = normalize(resolution)
    alternative = normalize(alternative or "")
    alias_key = question_key(alternative) if alternative else None
    if len(resolution) < 5:
        raise ValueError("Введите подтверждённый ответ")
    if alternative and (not alias_key or len(alias_key) < 2):
        raise ValueError("Введите текст альтернативной формулировки")
    with connect(database) as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT unit, question_key, document, point, revision, approved_at
            FROM quality_cases WHERE id=?""", (case_id,)).fetchone()
        if not row:
            raise LookupError("Запрос не найден")
        if row["approved_at"]:
            current = db.execute("""SELECT 1 FROM passages WHERE unit=? AND document=? AND point=?
                AND revision=? LIMIT 1""", (row["unit"], row["document"], row["point"], row["revision"])).fetchone()
            if current:
                raise FileExistsError("Запрос уже подтверждён")
        if alias_key == row["question_key"]:
            raise ValueError("Альтернативная формулировка совпадает с исходной")
        if infer_unit(alternative) - {row["unit"]}:
            raise ValueError("Альтернативная формулировка должна относиться к тому же подразделению")
        collision = db.execute("""SELECT 1 FROM quality_cases k
            LEFT JOIN quality_aliases a ON a.case_id=k.id
            WHERE k.id<>? AND k.unit=? AND k.approved_at IS NOT NULL
            AND (k.question_key IN (?, ?) OR a.question_key IN (?, ?)) LIMIT 1""",
                               (case_id, row["unit"], row["question_key"], alias_key,
                                row["question_key"], alias_key)).fetchone()
        if collision:
            raise FileExistsError("Эта формулировка уже связана с другим подтверждённым ответом")
        sources = db.execute("""SELECT DISTINCT heading, revision FROM passages
            WHERE unit=? AND document=? AND point=?""", (row["unit"], document, point)).fetchall()
        if len(sources) != 1:
            raise ValueError("Выберите однозначный пункт регламента этого подразделения")
        db.execute("""UPDATE quality_cases SET resolution=?, document=?, point=?, heading=?,
            revision=?, approved_at=CURRENT_TIMESTAMP WHERE id=?""",
                   (resolution, document, point, sources[0]["heading"], sources[0]["revision"], case_id))
        db.execute("DELETE FROM quality_aliases WHERE case_id=?", (case_id,))
        if alias_key:
            db.execute("INSERT INTO quality_aliases VALUES (?, ?, ?, ?)",
                       (row["unit"], alias_key, alternative, case_id))
