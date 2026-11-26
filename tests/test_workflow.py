import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from src import app as application
from src.answers import answer
from src.corpus import connect, numbered, prepare, terms
from src.dialogue import prepare_dialogue, resolve
from src.quality import approve, find_approval, open_case, prepare_quality


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.database = Path(self.folder.name) / "test.sqlite"
        self.quote = "Координатор назначает ответственного исполнителя после проверки заявки."
        prepare(self.database)
        prepare_quality(self.database)
        prepare_dialogue(self.database)
        with connect(self.database) as db:
            row = (" ".join(terms(self.quote)), self.quote, "technical", "Синтетический регламент", "2.1", "Назначение", "v1")
            db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?)", row)

    def test_preserves_numeric_statements_under_their_clause(self):
        source = ["2. Анкетирование", "2.1. Срок зависит от условий:", "2 рабочих дня после оплаты."]
        self.assertEqual(list(numbered(source))[-1], (
            "2.1", "Анкетирование", "Срок зависит от условий: 2 рабочих дня после оплаты.",
        ))

    def test_only_verbatim_quote_from_the_selected_department(self):
        question = "Кто назначает ответственного исполнителя заявки?"
        def selector(_, candidates):
            return {"status": "answer", "selections": [{"id": candidates[0]["id"], "quote": self.quote}]}
        result = answer(self.database, question, "technical", selector)
        self.assertEqual(result["sources"][0]["point"], "2.1")
        self.assertEqual(answer(self.database, question, "security", selector)["status"], "missing")
        forged = lambda _, candidates: {"status": "answer", "selections": [
            {"id": candidates[0]["id"], "quote": self.quote.replace("после", "до")},
        ]}
        self.assertEqual(answer(self.database, question, "technical", forged)["status"], "missing")
        contact = "Какой номер телефона у ответственного исполнителя заявки?"
        self.assertEqual(answer(self.database, contact, "technical", selector)["status"], "missing")

    def test_fabricated_source_id_fails_closed(self):
        question = "Кто назначает ответственного исполнителя заявки?"
        def invented(_, candidates):
            self.assertEqual(len(candidates), 1)
            return {"status": "answer", "selections": [{"id": 9001, "quote": self.quote}]}
        result = answer(self.database, question, "technical", invented)
        self.assertEqual(result["status"], "missing")
        self.assertNotIn("sources", result)

    def test_scope_clarification_retains_question_and_consumes_token(self):
        question = "Кто назначает ответственного исполнителя заявки?"
        def stub(_, context, unit):
            self.assertEqual(context, question)
            self.assertEqual(unit, "technical")
            return {"status": "answer"}
        pending = resolve(self.database, question, None, None, stub)
        self.assertEqual(pending["status"], "clarify")
        self.assertEqual(resolve(self.database, "", "technical", pending["turn"], stub)["status"], "answer")
        self.assertEqual(resolve(self.database, "", "security", pending["turn"], stub)["status"], "expired")

    def test_mixed_scope_asks_instead_of_searching(self):
        def unexpected(*args):
            self.fail("Search must wait for a single subdivision")
        pending = resolve(self.database, "Кто отвечает в ДБРЦТ и Дирекции ИБ?", None, None, unexpected)
        self.assertEqual(pending["mode"], "scope")
        self.assertEqual(set(pending["options"]), {"dbrct", "security", "technical"})

    def test_review_requires_a_same_department_source_and_its_revision(self):
        question = "Какой номер у справочной службы?"
        case = open_case(self.database, question, "technical")
        self.assertEqual(open_case(self.database, question.upper(), "technical"), case)
        self.assertIsNone(find_approval(self.database, question, "technical"))
        with self.assertRaises(ValueError):
            approve(self.database, case, "Тестовый ответ", "Другой регламент", "2.1")
        approve(self.database, case, "Тестовый ответ", "Синтетический регламент", "2.1")
        self.assertEqual(find_approval(self.database, question.upper(), "technical")["answer"], "Тестовый ответ")
        self.assertIsNone(find_approval(self.database, question, "security"))
        with connect(self.database) as db:
            db.execute("UPDATE passages SET revision='v2'")
        self.assertIsNone(find_approval(self.database, question, "technical"))

    def test_http_question_user_cannot_open_or_approve_quality_case(self):
        question = "Где взять билет на экскурсию?"
        with patch.object(application, "DATABASE", self.database), patch.dict(
            "os.environ", {"IIKUZNITSA_QUALITY_KEY": "test-reviewer-key"}
        ), TestClient(application.app) as client:
            request = {"question": question, "unit": "technical"}
            result = client.post("/ask", json=request).json()
            self.assertEqual(result["status"], "missing")
            self.assertNotIn("sources", result)
            case = result["case_id"]
            self.assertEqual(client.get(f"/quality/case/{case}").status_code, 403)
            review = dict(case_id=case, resolution="Синтетическое подтверждение",
                          document="Синтетический регламент", point="2.1")
            self.assertEqual(client.post("/quality/approve", json=review).status_code, 403)
            self.assertEqual(client.post("/quality/approve", json=review,
                                         headers={"X-Quality-Key": "wrong"}).status_code, 403)
            self.assertEqual(client.post("/ask", json=request).json()["status"], "missing")
            self.assertEqual(client.post("/quality/approve", json=review,
                                         headers={"X-Quality-Key": "test-reviewer-key"}).status_code, 200)
            self.assertEqual(client.post("/ask", json=request).json()["status"], "approved")

    def test_approved_alternative_reuses_case_but_respects_rights_scope_and_revision(self):
        original = "Где взять билет на экскурсию?"
        alternative = "Как получить пропуск на экскурсию?"
        different = "Как вернуть билет на экскурсию?"
        def ask(client, question, unit="technical"):
            return client.post("/ask", json={"question": question, "unit": unit}).json()
        with patch.object(application, "DATABASE", self.database), patch.dict(
            "os.environ", {"IIKUZNITSA_QUALITY_KEY": "test-reviewer-key"}
        ), TestClient(application.app) as client:
            initial = ask(client, original)
            self.assertEqual(initial["status"], "missing")
            alt_case = ask(client, alternative)
            self.assertEqual(alt_case["status"], "missing")
            self.assertNotEqual(initial["case_id"], alt_case["case_id"])
            review = dict(case_id=initial["case_id"], resolution="Синтетическое подтверждение",
                          document="Синтетический регламент", point="2.1", alternative=alternative)
            headers = {"X-Quality-Key": "test-reviewer-key"}
            self.assertEqual(client.post("/quality/approve", json=review).status_code, 403)
            self.assertEqual(client.post("/quality/approve", json=dict(review, alternative="Где взять билет в ДБРЦТ?"),
                                         headers=headers).status_code, 400)
            self.assertEqual(client.post("/quality/approve", json=review, headers=headers).status_code, 200)
            result = ask(client, alternative.upper().rstrip("?"))
            self.assertEqual(result, ask(client, original))
            self.assertEqual(result["case_id"], initial["case_id"])
            self.assertEqual(result["message"], "Ответ подтверждён службой качества")
            self.assertEqual(result["sources"][0]["point"], "2.1")
            self.assertEqual(ask(client, different)["status"], "missing")
            self.assertEqual(ask(client, alternative, "security")["status"], "missing")
            conflicting = dict(review, case_id=alt_case["case_id"], alternative=None)
            self.assertEqual(client.post("/quality/approve", json=conflicting, headers=headers).status_code, 409)
            with connect(self.database) as db:
                db.execute("UPDATE passages SET revision='v2'")
            self.assertEqual(ask(client, original)["status"], "missing")
            self.assertEqual(ask(client, alternative)["status"], "missing")
