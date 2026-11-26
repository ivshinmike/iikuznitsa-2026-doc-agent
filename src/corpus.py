import argparse
import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from xml.etree import ElementTree
from zipfile import ZipFile


UNITS = {
    "dbrct": "ДБРЦТ",
    "security": "Дирекция ИБ",
    "technical": "Техническая дирекция",
}
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NUMBER = re.compile(r"^((?:\d+\.)+\d*)\s+([А-Яа-яЁёA-Za-z].*)")
WORDS = re.compile(r"[а-яa-z0-9]+")
STOP = set("что как кто когда какой какая какие какое куда можно нужно надо или если для это этот при по на где сколько ли так из за от до про его мне не".split())


@contextmanager
def connect(path):
    database = sqlite3.connect(path, timeout=10)
    database.row_factory = sqlite3.Row
    try:
        with database:
            yield database
    finally:
        database.close()


def normalize(text):
    return " ".join(text.split())


def terms(text):
    return list(dict.fromkeys(
        word[:5] if len(word) >= 6 and not word[0].isascii() else word
        for word in WORDS.findall(text.casefold().replace("ё", "е"))
        if len(word) >= 2 and word not in STOP
    ))


def prepare(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS passages USING fts5(
            search, body UNINDEXED, unit UNINDEXED, document UNINDEXED,
            point UNINDEXED, heading UNINDEXED, revision UNINDEXED)""")


def paragraphs(path):
    def text(node):
        return normalize("".join(item.text or "" for item in node.iter(WORD + "t")))

    with ZipFile(path) as archive:
        info = archive.getinfo("word/document.xml")
        if info.file_size > 20_000_000:
            raise ValueError(f"DOCX XML is too large: {path.name}")
        root = ElementTree.fromstring(archive.read(info))
    for node in root.find(WORD + "body"):
        if node.tag == WORD + "p":
            yield text(node)
        elif node.tag == WORD + "tbl":
            for row in node.findall(WORD + "tr"):
                yield " | ".join(text(cell) for cell in row.findall(WORD + "tc"))


def numbered(paragraph_list):
    point, heading, parts = None, "", []
    for paragraph in filter(None, paragraph_list):
        match = NUMBER.fullmatch(paragraph)
        if not match:
            if point:
                parts.append(paragraph)
            continue
        if point:
            yield point, heading, " ".join(parts)
        point, body = match.groups()
        point = point.rstrip(".")
        if "." not in point:
            heading = body
        parts = [body]
    if point:
        yield point, heading, " ".join(parts)


def windows(body, size=720, step=560):
    start = 0
    while start < len(body):
        end = min(start + size, len(body))
        if end < len(body):
            end = body.rfind(" ", start + step, end)
            if end < 0:
                end = min(start + size, len(body))
        yield body[start:end].strip()
        if end == len(body):
            return
        next_start = body.rfind(" ", start + 1, min(start + step, end))
        start = next_start + 1 if next_start > start else end


def ingest(database, manifest):
    manifest = Path(manifest).resolve()
    entries = json.loads(manifest.read_text(encoding="utf-8-sig"))
    rows, seen = [], set()
    for item in entries:
        path = (manifest.parent / item["file"]).resolve()
        unit, title = item["unit"], item["title"]
        if unit not in UNITS or path.suffix.casefold() != ".docx" or (unit, title) in seen:
            raise ValueError("Each source needs a unique title, a supported unit and a DOCX")
        seen.add((unit, title))
        revision = hashlib.sha256(path.read_bytes()).hexdigest()
        clauses = list(numbered(paragraphs(path)))
        if not clauses:
            raise ValueError(f"No numbered clauses in {path.name}")
        rows.extend((" ".join(terms(fragment) + terms(heading)), fragment, unit, title,
                     point, heading, revision)
                    for point, heading, body in clauses for fragment in windows(body))
    if not rows:
        raise ValueError("No corpus sources")
    prepare(database)
    with connect(database) as db:
        db.execute("DELETE FROM passages")
        db.executemany("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    return len(seen), len(rows)


def search(database, question, unit, limit=14):
    keywords = terms(question)
    if not keywords:
        return []
    expression = " OR ".join(f'"{keyword}"' for keyword in keywords[:42])
    with connect(database) as db:
        options = " AND unit=?" if unit else ""
        parameters = (expression, unit, limit * 3) if unit else (expression, limit * 3)
        rows = db.execute(f"""SELECT rowid AS id, *, bm25(passages) AS rank FROM passages
            WHERE passages MATCH ?{options} ORDER BY rank, rowid LIMIT ?""", parameters)
        selected, counts = [], {}
        for row in rows:
            if not set(keywords).intersection(terms(row["body"])):
                continue
            key = (row["document"], row["point"], row["heading"])
            if counts.get(key, 0) >= 2:
                continue
            counts[key] = counts.get(key, 0) + 1
            selected.append(dict(row))
            if len(selected) == limit:
                break
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index locally available corporate DOCX files")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--db", type=Path, default=Path("data/knowledge.sqlite"))
    args = parser.parse_args()
    documents, fragments = ingest(args.db, args.manifest)
    print(f"documents={documents} fragments={fragments}")
