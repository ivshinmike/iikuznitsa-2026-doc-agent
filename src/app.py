import os
from hmac import compare_digest
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .answers import answer
from .corpus import UNITS, normalize, prepare
from .dialogue import infer_unit, prepare_dialogue, resolve
from .quality import (
    approve, find_knowledge, get_case, hold_approval, open_case, prepare_quality, published_answer, question_key,
)
from .quality_mail import notify


DATABASE = Path(os.environ.get("IIKUZNITSA_DB", "data/knowledge.sqlite"))
WEB = Path(__file__).parent
prepare(DATABASE)
prepare_dialogue(DATABASE)
prepare_quality(DATABASE)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")


class Question(BaseModel):
    question: str = Field(default="", max_length=1400)
    unit: str | None = None
    turn: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class Review(BaseModel):
    case_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    resolution: str = Field(min_length=5, max_length=3000)
    document: str = Field(min_length=2, max_length=240)
    point: str = Field(pattern=r"^\d+(?:\.\d+)*$", max_length=30)
    alternative: str | None = Field(default=None, max_length=1400)
    conflict_note: str | None = Field(default=None, max_length=1000)


class ReviewQuestion(BaseModel):
    question: str = Field(min_length=2, max_length=1400)
    unit: str


def with_quality(database, question, unit):
    result = answer(database, question, unit)
    existing = find_knowledge(database, question, unit)
    if existing:
        if result["status"] == "answer" and existing["approved_at"]:
            hold_approval(database, existing)
        elif existing["approved_at"] and result["status"] == "missing":
            canonical = result
            if question_key(question) != existing["question_key"]:
                canonical = answer(database, existing["question"], unit)
            if canonical["status"] == "missing":
                return published_answer(existing)
            if canonical["status"] != "answer":
                return canonical
            hold_approval(database, existing)
            result = canonical
        elif existing["approved_at"]:
            return result
        if result["status"] != "answer":
            result = {"status": "conflict", "message": "Связанный действующий пункт регламента",
                      "sources": existing["sources"]}
        return {**result, "case_id": existing["id"], "conflict": True}
    if result["status"] == "missing":
        result["case_id"] = open_case(database, question, unit)
        result["message"] = "В регламентах нет подтверждённого ответа. Запрос в службу качества создан."
        result["mail_state"] = notify(database, result["case_id"])
    return result


def check_reviewer(supplied):
    expected = os.environ.get("IIKUZNITSA_QUALITY_KEY", "")
    if not expected:
        raise HTTPException(404, "Раздел службы качества отключён")
    if not supplied or not compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(403, "Требуется ключ службы качества")


@app.get("/", response_class=HTMLResponse)
def home():
    return (WEB / "index.html").read_text(encoding="utf-8")


@app.post("/ask")
def ask(request: Question):
    if request.unit is not None and request.unit not in UNITS:
        raise HTTPException(400, "Выберите подразделение")
    if len(request.question.strip()) < 2 and not (request.turn and request.unit and not request.question.strip()):
        raise HTTPException(400, "Введите вопрос")
    return resolve(DATABASE, request.question, request.unit, request.turn, with_quality)


@app.get("/quality", response_class=HTMLResponse)
def quality_page():
    if not os.environ.get("IIKUZNITSA_QUALITY_KEY"):
        raise HTTPException(404, "Раздел службы качества отключён")
    return (WEB / "quality.html").read_text(encoding="utf-8")


@app.get("/quality/case/{case_id}")
def review_case(case_id: str, x_quality_key: str | None = Header(default=None)):
    check_reviewer(x_quality_key)
    try:
        return get_case(DATABASE, case_id)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/quality/review")
def open_review(review: ReviewQuestion, x_quality_key: str | None = Header(default=None)):
    check_reviewer(x_quality_key)
    question = normalize(review.question)
    if review.unit not in UNITS or len(question) < 2:
        raise HTTPException(400, "Укажите вопрос и подразделение")
    if infer_unit(question) - {review.unit}:
        raise HTTPException(400, "Вопрос относится к другому подразделению")
    return {"case_id": open_case(DATABASE, question, review.unit)}


@app.post("/quality/approve")
def confirm_review(review: Review, x_quality_key: str | None = Header(default=None)):
    check_reviewer(x_quality_key)
    try:
        reviewer = normalize(os.environ.get("IIKUZNITSA_QUALITY_REVIEWER") or "") or "Служба качества"
        reason = normalize(review.conflict_note or "")
        if not reason:
            case = get_case(DATABASE, review.case_id)["case"]
            questions = [case["question"]]
            if review.alternative and question_key(review.alternative) != question_key(case["question"]):
                if infer_unit(review.alternative) - {case["unit"]}:
                    raise ValueError("Альтернативная формулировка должна относиться к тому же подразделению")
                questions.append(review.alternative)
            for question in questions:
                status = answer(DATABASE, question, case["unit"])["status"]
                if status == "answer":
                    reason = "В действующем регламенте найден ответ. Предложение требует дополнительного согласования."
                    break
                if status != "missing":
                    raise HTTPException(409, "Сначала проверьте, можно ли однозначно ответить по действующему регламенту")
        status = approve(DATABASE, review.case_id, review.resolution, review.document, review.point, review.alternative,
                         reviewer=reviewer, conflict_note=reason)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error
    except FileExistsError as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    return {"status": status, "case_id": review.case_id}
