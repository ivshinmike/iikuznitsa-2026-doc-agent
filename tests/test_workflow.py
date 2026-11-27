import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from fastapi.testclient import TestClient

from src import app as application
from src.answers import answer
from src.corpus import connect, numbered, prepare, terms
from src.dialogue import prepare_dialogue, resolve
from src.quality import approve, find_knowledge, open_case, prepare_quality


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
        self.assertIsNone(find_knowledge(self.database, question, "technical"))
        with self.assertRaises(ValueError):
            approve(self.database, case, "Тестовый ответ", "Другой регламент", "2.1")
        approve(self.database, case, "Тестовый ответ", "Синтетический регламент", "2.1")
        self.assertEqual(find_knowledge(self.database, question.upper(), "technical")["resolution"], "Тестовый ответ")
        self.assertIsNone(find_knowledge(self.database, question, "security"))
        with connect(self.database) as db:
            db.execute("UPDATE passages SET revision='v2'")
        self.assertIsNone(find_knowledge(self.database, question, "technical"))

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

    def test_conflict_creates_a_pending_case_and_preserves_the_current_rule(self):
        question = "Кто назначает ответственного исполнителя заявки?"
        request = dict(question=question, unit="technical")
        headers = {"X-Quality-Key": "test-reviewer-key"}
        answer = {"status": "answer", "sources": [{"point": "2.1", "quote": self.quote}]}
        with patch.object(application, "DATABASE", self.database), patch.dict("os.environ", {
            "IIKUZNITSA_QUALITY_KEY": "test-reviewer-key", "IIKUZNITSA_QUALITY_REVIEWER": "Эксперт теста",
            "IIKUZNITSA_MAILPIT_SMTP_PORT": "",
        }), patch.object(application, "answer", return_value=answer), TestClient(application.app) as client:
            self.assertEqual(client.post("/ask", json=request).json()["status"], "answer")
            self.assertEqual(client.post("/quality/review", json=request).status_code, 403)
            cross_scope = dict(request, question="Кто назначает исполнителя в Дирекции ИБ?")
            self.assertEqual(client.post("/quality/review", json=cross_scope, headers=headers).status_code, 400)
            case = client.post("/quality/review", json=request, headers=headers).json()["case_id"]
            review = dict(case_id=case, resolution="Исполнителя назначает руководитель группы.",
                          document="Синтетический регламент", point="2.1",
                          reviewer="Попытка подмены", conflict_note="Предложение требует согласования.")
            self.assertEqual(client.post("/quality/approve", json=review).status_code, 403)
            sent = client.post("/quality/approve", json=review, headers=headers)
            self.assertEqual(sent.status_code, 200)
            self.assertEqual(sent.json(), {"status": "escalated", "case_id": case})
            result = client.post("/ask", json=request).json()
            self.assertEqual(result["status"], "answer")
            self.assertNotIn("answer", result)
            self.assertEqual(result["sources"], answer["sources"])
            self.assertEqual(result["case_id"], case)
            self.assertIs(result["conflict"], True)
            details = client.get(f"/quality/case/{case}", headers=headers).json()["case"]
            self.assertEqual(details["reviewer"], "Эксперт теста")
            self.assertIsNone(details["approved_at"])
            self.assertIsNotNone(details["escalated_at"])
            self.assertEqual(details["resolution"], review["resolution"])
            self.assertEqual(client.post("/quality/approve", json=review, headers=headers).status_code, 409)
            self.assertIsNone(find_knowledge(self.database, question, "security"))
            with connect(self.database) as db:
                db.execute("UPDATE passages SET revision='v2'")
            self.assertNotIn("conflict", client.post("/ask", json=request).json())

    def test_unmarked_and_legacy_conflicts_do_not_replace_the_three_day_rule(self):
        quote = "Заявку следует инициировать не менее чем за 3 рабочих дня."
        question = "За сколько рабочих дней нужно инициировать заявку?"
        proposal = "Заявку следует инициировать не менее чем за 5 рабочих дней."
        alternate = "Какой срок подачи?"
        with connect(self.database) as db:
            db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?)", (
                " ".join(terms(quote)), quote, "dbrct", "Синтетический порядок", "1.5", "Срок", "v1",
            ))
        official = {"status": "answer", "sources": [{"point": "1.5", "quote": quote}]}

        def evidence(_, text, __):
            return official if text == question else {"status": "missing"}

        case = open_case(self.database, question, "dbrct")
        review = dict(case_id=case, resolution=proposal, document="Синтетический порядок", point="1.5")
        headers = {"X-Quality-Key": "test-reviewer-key"}
        with patch.object(application, "DATABASE", self.database), patch.object(
            application, "answer", side_effect=evidence,
        ), patch.dict("os.environ", {"IIKUZNITSA_QUALITY_KEY": "test-reviewer-key"}), TestClient(application.app) as client:
            self.assertEqual(client.post("/quality/approve", json=review, headers=headers).json()["status"], "escalated")
            with connect(self.database) as db:
                db.execute("""UPDATE quality_cases SET conflict_note=NULL, escalated_at=NULL,
                    approved_at=CURRENT_TIMESTAMP WHERE id=?""", (case,))
                db.execute("INSERT INTO quality_aliases VALUES (?, ?, ?, ?)",
                           ("dbrct", "какой срок подачи", alternate, case))
            for wording in (alternate, question):
                result = client.post("/ask", json={"question": wording, "unit": "dbrct"}).json()
                self.assertIs(result["conflict"], True)
                self.assertNotIn("5 рабочих", str(result))
                self.assertIn("3 рабочих", str(result))
            self.assertIsNone(find_knowledge(self.database, question, "dbrct")["approved_at"])

    def test_legacy_marked_override_and_unavailable_model_keep_the_linked_rule(self):
        question = "Кто назначает ответственного исполнителя заявки?"
        case = open_case(self.database, question, "technical")
        approve(self.database, case, "Проверяем старое предложение", "Синтетический регламент", "2.1")
        with connect(self.database) as db:
            db.execute("UPDATE quality_cases SET conflict_note='Старое расхождение' WHERE id=?", (case,))
        prepare_quality(self.database)
        with patch.object(application, "answer", return_value={"status": "unavailable"}):
            result = application.with_quality(self.database, question, "technical")
        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["sources"][0]["quote"], self.quote)
        self.assertEqual(result["case_id"], case)
        self.assertNotIn("answer", result)

    def test_two_ambiguous_inputs_do_not_query_the_model(self):
        def unexpected(*_):
            self.fail("No single applicable subdivision was established")
        pending = resolve(self.database, "Когда нужно согласовать заявку?", None, None, unexpected)
        pending = resolve(self.database, "Не знаю подразделение", None, pending["turn"], unexpected)
        result = resolve(self.database, "Не могу уточнить", None, pending["turn"], unexpected)
        self.assertEqual(result["status"], "missing")
        self.assertNotIn("sources", result)

    def test_mail_is_claimed_once_and_failure_preserves_case(self):
        request = dict(question="Как попасть на экскурсию?", unit="technical")
        with patch.object(application, "DATABASE", self.database), patch.dict(
            "os.environ", {"IIKUZNITSA_MAILPIT_SMTP_PORT": "1025"}
        ), patch("src.quality_mail.smtplib.SMTP", side_effect=ConnectionRefusedError) as smtp, TestClient(application.app) as client:
            result = client.post("/ask", json=request).json()
            duplicate = client.post("/ask", json=request).json()
            self.assertEqual(result["status"], "missing")
            self.assertEqual(duplicate["case_id"], result["case_id"])
            self.assertEqual(result["mail_state"], "failed")
            self.assertEqual(duplicate["mail_state"], "failed")
            smtp.assert_called_once_with("127.0.0.1", 1025, timeout=2)

    def test_ingest_accepts_an_unseen_docx_and_ignores_another_unit(self):
        from json import dumps
        from zipfile import ZipFile
        from src.corpus import ingest, search
        path = Path(self.folder.name)
        with ZipFile(path / "new.docx", "w") as archive:
            archive.writestr("word/document.xml", """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body><w:p><w:r><w:t>1. Калибровка маяка</w:t></w:r></w:p>
            <w:p><w:r><w:t>1.3. Старший техник проверяет маяк до запуска.</w:t></w:r></w:p></w:body></w:document>""")
        manifest = path / "corpus.json"
        manifest.write_text(dumps([{"file": "new.docx", "unit": "technical", "title": "Новый синтетический порядок"}]),
                            encoding="utf-8")
        self.assertEqual(ingest(self.database, manifest), (1, 2))
        found = search(self.database, "Кто проверяет маяк?", "technical")
        self.assertEqual(found[0]["point"], "1.3")
        self.assertFalse(search(self.database, "Кто проверяет маяк?", "security"))

    def test_legacy_quality_schema_preserves_preexisting_data(self):
        legacy = Path(self.folder.name) / "legacy.sqlite"
        prepare(legacy)
        with connect(legacy) as db:
            db.execute("""CREATE TABLE quality_cases (id TEXT PRIMARY KEY, unit TEXT NOT NULL, question TEXT NOT NULL,
                question_key TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                approved_at TEXT, resolution TEXT, document TEXT, point TEXT, heading TEXT, revision TEXT,
                UNIQUE(unit, question_key))""")
            db.execute("INSERT INTO quality_cases (id, unit, question, question_key) VALUES ('legacy','technical','test','test')")
        prepare_quality(legacy)
        prepare_quality(legacy)
        self.assertEqual(open_case(legacy, "test", "technical"), "legacy")
        with connect(legacy) as db:
            self.assertEqual(db.execute("SELECT reviewer, conflict_note, mail_state FROM quality_cases").fetchone()[:],
                             (None, None, None))



    def test_lookalike_clauses_keep_their_own_numbers_and_department(self):
        other = "Координатор назначает ответственного исполнителя до проверки заявки."
        with connect(self.database) as db:
            db.execute("INSERT INTO passages VALUES (?, ?, ?, ?, ?, ?, ?)", (
                " ".join(terms(other)), other, "security", "Синтетический порядок ИБ",
                "2.1", "Назначение", "v1",
            ))
        question = "Кто назначает ответственного исполнителя заявки?"

        def select(_, candidates):
            self.assertEqual(len(candidates), 1)
            return {"status": "answer", "selections": [{"id": candidates[0]["id"], "quote": candidates[0]["body"]}]}

        for unit, expected in (("technical", self.quote), ("security", other)):
            result = answer(self.database, question, unit, select)
            quote, = result["sources"]
            self.assertEqual((quote["unit"], quote["point"], quote["quote"]), (unit, "2.1", expected))
