import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from .answers import answer
from .corpus import UNITS, prepare


DATABASE = Path(os.environ.get("IIKUZNITSA_DB", "data/knowledge.sqlite"))
prepare(DATABASE)
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=1400)
    unit: str | None = None


@app.get("/", response_class=HTMLResponse)
def home():
    return Path(__file__).with_name("index.html").read_text(encoding="utf-8")


@app.post("/ask")
def ask(request: Question):
    if request.unit not in UNITS:
        raise HTTPException(400, "Выберите подразделение")
    if len(request.question.strip()) < 2:
        raise HTTPException(400, "Введите вопрос")
    return answer(DATABASE, request.question, request.unit)
