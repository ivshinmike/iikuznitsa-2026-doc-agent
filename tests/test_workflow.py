import tempfile
import unittest
from pathlib import Path

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
