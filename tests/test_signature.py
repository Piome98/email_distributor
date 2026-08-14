"""Signature-block parsing, the least deterministic part of the system."""

import unittest

from email_distributor.identity import signature


class TestStripQuoted(unittest.TestCase):
    def test_cuts_at_original_message_marker(self):
        body = "네 알겠습니다.\n\n김영수 대리\n\n-----Original Message-----\nFrom: someone\n박민수 사장"
        self.assertNotIn("박민수", signature.strip_quoted(body))

    def test_cuts_at_korean_header(self):
        body = "확인했습니다.\n\n이영희 과장\n\n보낸 사람: 홍길동\n홍길동 부장"
        kept = signature.strip_quoted(body)
        self.assertIn("이영희", kept)
        self.assertNotIn("홍길동", kept)

    def test_keeps_body_without_quotes(self):
        body = "안녕하세요.\n\n김철수 대리"
        self.assertEqual(signature.strip_quoted(body), body)


class TestParseKorean(unittest.TestCase):
    def test_full_korean_signature(self):
        info = signature.parse(
            "안녕하세요, 부장님.\n첨부 파일 확인 부탁드립니다.\n\n감사합니다.\n\n"
            "홍길동 부장 / 영업1팀\n(주)한국전자\n"
            "Tel. 02-1234-5678  |  M. 010-9876-5432\ngildong.hong@hanguk.co.kr"
        )
        self.assertEqual(info.name, "홍길동")
        self.assertEqual(info.title, "부장")
        self.assertEqual(info.department, "영업1팀")
        self.assertEqual(info.company, "(주)한국전자")
        self.assertEqual(info.phone, "02-1234-5678")
        self.assertEqual(info.mobile, "010-9876-5432")
        self.assertEqual(info.email, "gildong.hong@hanguk.co.kr")

    def test_prose_does_not_become_a_department(self):
        """"첨부 파일" ends in a unit suffix but is ordinary prose."""
        info = signature.parse("첨부 파일 확인 부탁드립니다.\n\n김철수 과장\n구매팀\n(주)대한")
        self.assertEqual(info.department, "구매팀")

    def test_title_attached_without_space(self):
        info = signature.parse("김철수과장\n글로벌사업본부\n주식회사 대한산업\n휴대폰: 010-1111-2222")
        self.assertEqual(info.name, "김철수")
        self.assertEqual(info.title, "과장")
        self.assertEqual(info.department, "글로벌사업본부")

    def test_company_wins_over_department_on_shared_line(self):
        info = signature.parse(
            "최지우 차장\n재무팀 / (주)서울파이낸스\nTel 02-9999-0000  M 010-8888-7777"
        )
        self.assertEqual(info.company, "(주)서울파이낸스")
        self.assertEqual(info.department, "재무팀")

    def test_fax_is_not_mistaken_for_a_phone(self):
        info = signature.parse(
            "최지우 차장\n(주)서울파이낸스\nTel 02-9999-0000  Fax 02-9999-0001  M 010-8888-7777"
        )
        self.assertEqual(info.phone, "02-9999-0000")
        self.assertEqual(info.mobile, "010-8888-7777")
        self.assertNotIn("9999-0001", (info.phone, info.mobile))

    def test_mobile_detected_by_010_prefix_without_label(self):
        info = signature.parse("박서준 대리\n(주)테스트\n010-5555-6666")
        self.assertEqual(info.mobile, "010-5555-6666")

    def test_legal_disclaimer_is_ignored(self):
        info = signature.parse(
            "정한별 팀장\nR&D센터\n(주)넥스트테크\nM. 010-2222-3333\n\n"
            "본 메일은 수신자만 열람할 수 있는 기밀 정보를 포함하고 있습니다."
        )
        self.assertEqual(info.company, "(주)넥스트테크")
        self.assertEqual(info.name, "정한별")


class TestParseEnglish(unittest.TestCase):
    def test_english_signature(self):
        info = signature.parse(
            "Please find attached.\n\nBest regards,\nJohn Smith\n"
            "Senior Manager, Overseas Sales Team\nHanguk Electronics Co., Ltd.\n"
            "Tel: +82-2-555-1234 / Mobile: +82-10-5555-6789"
        )
        self.assertEqual(info.name, "John Smith")
        self.assertEqual(info.title, "Senior Manager")
        self.assertEqual(info.department, "Overseas Sales Team")
        self.assertEqual(info.company, "Hanguk Electronics Co., Ltd.")

    def test_comma_inside_company_name_is_preserved(self):
        info = signature.parse("Jane Doe\nAcme Trading Co., Ltd.\nM. 010-1234-5678")
        self.assertEqual(info.company, "Acme Trading Co., Ltd.")


class TestBulkMailIsNotAPerson(unittest.TestCase):
    """Regressions found by running against a real mailbox.

    Newsletters and notification mail contain capitalised prose that the
    title/department patterns will happily misread as somebody's job.
    """

    def test_marketing_mail_yields_no_title_or_department(self):
        info = signature.parse(
            "New Course! Learn how to create things.\n"
            "The Google Team\nCoursera Inc.\nBrowse Catalog\nUnsubscribe here",
            sender_name="Coursera",
        )
        self.assertEqual(info.title, "")
        self.assertEqual(info.department, "")

    def test_department_never_splices_two_lines(self):
        """`\\s` in the English pattern used to match across a newline."""
        info = signature.parse("Sent from SGT\nThe Manus Team\nmanus.im", sender_name="Manus")
        self.assertNotIn("\n", info.department)

    def test_a_real_signature_still_keeps_its_fields(self):
        info = signature.parse("홍길동 부장\n영업1팀\n(주)한국전자")
        self.assertEqual(info.title, "부장")
        self.assertEqual(info.department, "영업1팀")

    def test_an_organisation_name_is_not_a_department(self):
        """"한국고등교육재단" is a foundation, not somebody's team."""
        info = signature.parse(
            "홍길동 대리\n한국고등교육재단 소식\n(주)위시켓\nM. 010-1111-2222"
        )
        self.assertNotEqual(info.department, "한국고등교육재단")

    def test_common_words_ending_in_dropped_suffixes(self):
        for text in ("한국 시장 동향", "거래처 안내", "판단 기준"):
            info = signature.parse(f"김철수 과장\n{text}\n(주)테스트\nM. 010-1-1")
            self.assertNotIn(info.department, ("한국", "거래처", "판단"))

    def test_contact_number_is_enough_evidence_of_a_person(self):
        info = signature.parse(
            "Jane Doe\nOverseas Sales Team\nAcme Co., Ltd.\nM. 010-1234-5678"
        )
        self.assertEqual(info.department, "Overseas Sales Team")


class TestFieldsAreClean(unittest.TestCase):
    def test_no_field_contains_a_newline_or_double_space(self):
        info = signature.parse(
            "김철수 과장\n영업팀\n(주)대한산업\nTel. 02-111-2222\nM. 010-3333-4444"
        )
        for value in (info.name, info.title, info.department, info.company,
                      info.phone, info.mobile):
            self.assertNotIn("\n", value)
            self.assertNotIn("  ", value)
            self.assertEqual(value, value.strip())


class TestFallbacks(unittest.TestCase):
    def test_empty_body_falls_back_to_display_name(self):
        info = signature.parse("", sender_name="홍길동")
        self.assertEqual(info.name, "홍길동")

    def test_display_name_carrying_a_rank_is_split(self):
        info = signature.parse("내용 없음", sender_name="홍길동 부장")
        self.assertEqual(info.name, "홍길동")
        self.assertEqual(info.title, "부장")

    def test_is_empty_reports_no_findings(self):
        self.assertTrue(signature.SignatureInfo().is_empty())
        self.assertFalse(signature.SignatureInfo(company="(주)test").is_empty())

    def test_confidence_rises_with_more_fields(self):
        sparse = signature.SignatureInfo(name="홍길동")
        rich = signature.SignatureInfo(
            name="홍길동", company="(주)한국", title="부장", department="영업팀"
        )
        self.assertLess(sparse.confidence, rich.confidence)


if __name__ == "__main__":
    unittest.main()
