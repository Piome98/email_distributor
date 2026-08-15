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


class TestLabelledFields(unittest.TestCase):
    """Korean corporate footers label their fields; reading the label beats
    guessing from position. These blocks are modelled on real mail."""

    def test_address_label(self):
        info = signature.parse(
            "(주)사람인\n대표 : 황현순\n"
            "주소 : 서울특별시 강서구 공항대로 000, 원그로브 C동 00층\n"
            "문의처: 02-1111-2222, help@saramin.co.kr"
        )
        self.assertEqual(info.company, "(주)사람인")
        self.assertEqual(
            info.address, "서울특별시 강서구 공항대로 000, 원그로브 C동 00층"
        )

    def test_munuicheo_is_a_landline_not_a_mobile(self):
        """"문의처" is an office contact number, not somebody's mobile."""
        info = signature.parse("(주)사람인\n문의처: 02-1111-2222")
        self.assertEqual(info.phone, "02-1111-2222")
        self.assertEqual(info.mobile, "")

    def test_labelled_phone_email_and_website(self):
        info = signature.parse(
            "㈜ 위시켓\n서울특별시 강남구 테헤란로 000 0층\n"
            "전화 : 02-3333-4444\n이메일 : help@wishket.com\n"
            "홈페이지 : https://wishket.com"
        )
        self.assertEqual(info.company, "㈜ 위시켓")
        self.assertEqual(info.phone, "02-3333-4444")
        self.assertEqual(info.email, "help@wishket.com")
        self.assertEqual(info.website, "https://wishket.com")

    def test_unlabelled_korean_address_is_still_found(self):
        info = signature.parse(
            "DMK Global\n서울특별시 종로구 새문안로 00, 광화문오피시아빌딩 000호"
        )
        self.assertTrue(info.address.startswith("서울특별시 종로구"))

    def test_labels_override_positional_guesses(self):
        info = signature.parse(
            "홍길동 부장 / 영업1팀\n(주)한국전자\n부서 : 해외영업팀\n직급 : 이사\nM. 010-1-1"
        )
        self.assertEqual(info.department, "해외영업팀")
        self.assertEqual(info.title, "이사")

    def test_company_found_on_a_very_long_footer_line(self):
        """A packed one-line footer used to be dropped by the length filter."""
        info = signature.parse(
            "쿠팡페이(주) | 대표이사: 홍길동,김철수 ㅣ 사업자등록번호: 000-00-00000 "
            "ㅣ (00000) 서울특별시 광진구 아차산로 000 (자양동)"
        )
        self.assertEqual(info.company, "쿠팡페이(주)")

    def test_organisation_is_not_read_as_a_persons_name(self):
        """"DMK Global" has the shape of a name but an all-caps token."""
        info = signature.parse("DMK Global\ninfo@dmkglobal.co.kr | 02-111-2222")
        self.assertNotEqual(info.name, "DMK Global")

    def test_building_letter_is_not_a_mobile_label(self):
        """"원그로브 C동" - a bare C used to be read as a mobile marker."""
        info = signature.parse(
            "(주)사람인\n주소 : 서울특별시 강서구 공항대로 000, 원그로브 C동 00층\n"
            "문의처: 02-1111-2222"
        )
        self.assertEqual(info.mobile, "")
        self.assertEqual(info.phone, "02-1111-2222")

    def test_company_lifted_out_of_a_sentence(self):
        info = signature.parse("안녕하십니까? 가온전선(주)입니다.\n채용 안내드립니다.")
        self.assertEqual(info.company, "가온전선(주)")

    def test_company_not_swallowed_by_surrounding_words(self):
        info = signature.parse("고용형태: (주)서플러스글로벌 소속 정규직(수습 3개월)")
        self.assertEqual(info.company, "(주)서플러스글로벌")

    def test_english_company_stops_at_its_marker(self):
        info = signature.parse(
            "Microsoft Corporation, One Microsoft Way, Redmond, WA 98052"
        )
        self.assertEqual(info.company, "Microsoft Corporation")

    def test_bare_marker_word_is_not_a_company(self):
        info = signature.parse("company\nsomething else\nM. 010-1-1")
        self.assertEqual(info.company, "")

    def test_building_name_in_an_address_is_not_a_department(self):
        info = signature.parse(
            "김철수 대리\n서울특별시 강남구 테헤란로 000, 00층 (역삼동, 한국지식재산센터)\n"
            "M. 010-1111-2222"
        )
        self.assertNotEqual(info.department, "한국지식재산센터")

    def test_copyright_prefix_stripped_from_company(self):
        info = signature.parse("Some newsletter text\n© 2026 Google LLC\nView online")
        self.assertEqual(info.company, "Google LLC")

    def test_recruitment_word_is_not_a_persons_name(self):
        """"인턴사원" is a job grade, not the person 인턴 ranked 사원."""
        info = signature.parse("가온전선(주) 인턴사원 모집 안내")
        self.assertNotEqual(info.name, "인턴")

    def test_qualification_prefix_is_not_a_name(self):
        """"공인회계사" is one qualification, not 공인 ranked 회계사."""
        info = signature.parse("공인회계사 사무소 안내\n02-111-2222")
        self.assertNotEqual(info.name, "공인")

    def test_korean_conjunction_is_not_a_department(self):
        """과 is the everyday word "and"; "행동과 심리" is not a department."""
        info = signature.parse("이규호 팀장\n행동과 심리 뉴스레터\nM. 010-1-1")
        self.assertNotEqual(info.department, "행동과")

    def test_a_real_latin_name_still_parses(self):
        info = signature.parse("John Smith\nAcme Co., Ltd.\nM. 010-1234-5678")
        self.assertEqual(info.name, "John Smith")


class TestPersonalFlag(unittest.TestCase):
    """`personal` gates whether a signature may name the sender's company.

    Newsletters quote other organisations constantly, so trusting them made a
    recruitment mailshot's advertised employer become the sender's own company.
    """

    def test_real_signature_is_personal(self):
        info = signature.parse("홍길동 부장 / 영업1팀\n(주)한국전자\nM. 010-1-1")
        self.assertTrue(info.personal)

    def test_english_signature_is_personal(self):
        info = signature.parse("John Smith\nAcme Co., Ltd.\nM. 010-1234-5678")
        self.assertTrue(info.personal)

    def test_labelled_name_is_personal(self):
        info = signature.parse("성명 : 김철수\n(주)대한")
        self.assertTrue(info.personal)

    def test_newsletter_footer_is_not_personal(self):
        info = signature.parse(
            "(주)사람인\n주소 : 서울특별시 강서구 공항대로 000\n문의처: 02-1111-2222\n"
            "이번주 추천 공고: ㈜카카오페이 신입 채용"
        )
        self.assertFalse(info.personal)

    def test_company_still_extracted_from_a_non_personal_footer(self):
        """Not personal, but the footer's own details are still readable."""
        info = signature.parse("(주)사람인\n주소 : 서울특별시 강서구 공항대로 000")
        self.assertEqual(info.company, "(주)사람인")
        self.assertTrue(info.address)


class TestTrackingTokensAreNotCompanies(unittest.TestCase):
    def test_marker_glued_inside_a_token_is_ignored(self):
        info = signature.parse("unsubscribe\neNg-xIMAkWZ6XpLcInc\nfooter")
        self.assertEqual(info.company, "")

    def test_marketing_copy_is_not_scanned_for_a_company(self):
        long_ad = (
            "이번 주 추천 채용 공고를 확인해 보세요. 지원자 여러분께 딱 맞는 "
            "포지션을 골라 담았습니다. 아래에서 ㈜카카오페이 채용 소식을 만나보세요."
        )
        info = signature.parse(f"{long_ad}\n수신거부")
        self.assertEqual(info.company, "")

    def test_statutory_footer_line_is_still_scanned(self):
        info = signature.parse(
            "쿠팡페이(주) | 대표이사: 홍길동 ㅣ 사업자등록번호: 000-00-00000 "
            "ㅣ (00000) 서울특별시 광진구 아차산로 000 (자양동)"
        )
        self.assertEqual(info.company, "쿠팡페이(주)")


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
