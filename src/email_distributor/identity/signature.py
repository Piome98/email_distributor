"""Heuristic parser for the signature block at the bottom of an email.

Domain-to-company mapping tells us *which* organisation someone belongs to, but
not their name, department or rank. Those live in the signature block, so this
module digs them out. It is deliberately tuned for the mixed Korean/English
footers common in Korean office mail:

    홍길동 부장 / 영업1팀
    (주)한국전자
    Tel. 02-1234-5678  |  M. 010-9876-5432
    gildong.hong@hanguk.co.kr

Every field is optional and every extraction is best-effort: a signature is
free-form text, so the parser aims to be right often and wrong quietly, never
to be authoritative. Anything it returns is stored with source='signature',
which a manual correction always outranks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

# Korean ranks (직급) and roles (직책), longest first so that "대표이사" is
# matched before the "이사" hiding inside it.
KO_TITLES = [
    "대표이사", "부사장", "본부장", "사업부장", "연구소장", "그룹장", "센터장",
    "공장장", "부문장", "파트장", "팀장", "실장", "소장", "회장", "사장",
    "전무", "상무", "부장", "차장", "과장", "대리", "주임", "사원",
    "수석연구원", "책임연구원", "선임연구원", "전임연구원", "연구원",
    "수석", "책임", "선임", "전임", "매니저", "컨설턴트", "기사", "주치의",
    "변호사", "회계사", "세무사", "노무사", "변리사", "교수", "박사",
]

EN_TITLES = [
    "Chief Executive Officer", "Chief Technology Officer",
    "Chief Financial Officer", "Chief Operating Officer",
    "Managing Director", "Vice President", "General Manager",
    "Senior Manager", "Project Manager", "Product Manager",
    "Account Manager", "Sales Manager", "Team Leader", "Team Lead",
    "Principal", "Director", "Manager", "Engineer", "Researcher",
    "Consultant", "Analyst", "Designer", "Developer", "Architect",
    "Specialist", "Associate", "Assistant", "President", "Partner",
    "CEO", "CTO", "CFO", "COO", "CIO", "CMO", "VP", "PM", "PL",
]

# Organisational-unit suffixes. Used to spot a department name.
#
# 국, 처, 단 and 과 were tried and removed: as single characters they match far
# more ordinary Korean words than departments - 한국/외국/결국, 거래처/출처/근처,
# and 재단/집단/판단. "한국고등교육재단" (a foundation) was being filed as
# somebody's department because of the trailing 단, and 과 is the everyday
# conjunction "and", which turned "행동과 심리" into the department "행동과".
# 과 as a unit (총무과, 인사과) is mostly government and school usage.
DEPT_SUFFIXES = [
    "사업본부", "사업부문", "연구소", "사업부", "본부", "센터", "그룹",
    "팀", "실", "부", "파트",
]

# Korean organisations are layered: a division (본부/실/사업부) contains teams
# (팀/파트). Splitting the suffixes lets a signature reading
# "글로벌사업본부 해외영업팀" fill both levels instead of collapsing to one.
DIVISION_SUFFIXES = [
    "사업본부", "사업부문", "사업부", "본부", "부문", "연구소", "센터", "실", "부",
]
TEAM_SUFFIXES = ["팀", "파트", "그룹"]

# Suffixes that mark a whole organisation rather than a unit inside one, so a
# match ending in one of these is never a department.
ORG_NOT_DEPT_SUFFIXES = ("재단", "법인", "공단", "공사", "협회", "조합", "학회")

# Recruitment words that pair with a rank and imitate a name+rank pair -
# "인턴사원" parses as the person 인턴 holding the rank 사원.
NAME_STOPWORDS = frozenset(
    {
        "인턴", "신입", "경력", "담당", "채용", "모집", "지원", "우대", "대상", "정규",
        # Qualification prefixes that sit directly in front of a professional
        # rank: "공인회계사" is one word, not the person 공인 ranked 회계사.
        "공인", "세무", "노무", "변리", "법무", "관세",
    }
)

# Copyright furniture that precedes a company name in a footer:
# "© 2026 Google LLC" should yield "Google LLC".
RE_COMPANY_NOISE_PREFIX = re.compile(
    r"^(?:©|\(c\)|copyright)?\s*(?:\d{4})?\s*(?:©|\(c\))?\s*", re.IGNORECASE
)

# Statutory details that only ever appear in a genuine corporate footer. A long
# line carrying one of these is worth scanning for a company name; an equally
# long line of marketing copy is not.
RE_CORPORATE_FOOTER = re.compile(
    r"사업자\s*등록\s*번호|사업자번호|법인등록번호|통신판매업|대표이사|대표자"
    r"|Business Registration|All rights reserved",
    re.IGNORECASE,
)

EN_DEPT_SUFFIXES = [
    "Business Unit", "Headquarters", "Laboratory", "Department", "Institute",
    "Division", "Team", "Group", "Center", "Centre", "Office", "Unit",
    "Services", "Dept.", "Dept", "Lab", "HQ", "BU",
]

# Split the same way as the Korean suffixes. In a global company footer
# "Automotive BU" is the division and "Korea HR Services" the team.
EN_DIVISION_SUFFIXES = [
    "Business Unit", "Headquarters", "Laboratory", "Department", "Institute",
    "Division", "Center", "Centre", "Office", "Dept.", "Dept", "Lab", "HQ", "BU",
]
EN_TEAM_SUFFIXES = ["Team", "Group", "Services", "Unit"]

# Everyday words that happen to end in a single-character unit suffix such as
# 부 or 과. Without this, prose like "첨부 파일" would be read as a department.
DEPT_STOPWORDS = frozenset(
    {
        "첨부", "일부", "전부", "내부", "외부", "세부", "대부", "간부", "학부",
        "환부", "해당부", "결과", "경과", "통과", "사과", "초과", "부탁", "공부",
        "업무", "확인", "관련", "회신", "참고", "이하", "이상", "아래", "위실",
    }
)

# Corporate-form markers that identify a line as a company name.
KO_COMPANY_MARKERS = ["(주)", "㈜", "주식회사", "유한회사", "(유)", "재단법인", "사단법인"]
EN_COMPANY_MARKERS = [
    "Co., Ltd.", "Co.,Ltd.", "Co. Ltd", "Corporation", "Company",
    "Inc.", "Corp.", "Ltd.", "LLC", "L.L.C.", "GmbH", "S.A.", "B.V.", "PLC",
]

# --------------------------------------------------------------------------
# Compiled patterns
# --------------------------------------------------------------------------

_TITLE_ALT = "|".join(re.escape(t) for t in KO_TITLES)
_EN_TITLE_ALT = "|".join(re.escape(t) for t in EN_TITLES)
_DEPT_ALT = "|".join(re.escape(s) for s in DEPT_SUFFIXES)

# "홍길동 부장", "홍길동부장", "홍길동 / 부장"
RE_KO_NAME_TITLE = re.compile(
    rf"(?<![가-힣])([가-힣]{{2,4}})\s*(?:/|\||,|·)?\s*({_TITLE_ALT})(?![가-힣])"
)

# A bare Korean rank anywhere on the line.
RE_KO_TITLE = re.compile(rf"(?<![가-힣])({_TITLE_ALT})(?![가-힣])")

RE_EN_TITLE = re.compile(rf"\b({_EN_TITLE_ALT})\b", re.IGNORECASE)

# "영업1팀", "글로벌사업본부", "R&D센터"
RE_DEPT = re.compile(rf"([A-Za-z0-9가-힣&\.\-]{{1,20}}?(?:{_DEPT_ALT}))(?![가-힣])")

_DIVISION_ALT = "|".join(re.escape(s) for s in DIVISION_SUFFIXES)
_TEAM_ALT = "|".join(re.escape(s) for s in TEAM_SUFFIXES)

RE_DIVISION = re.compile(
    rf"([A-Za-z0-9가-힣&\.\-]{{1,20}}?(?:{_DIVISION_ALT}))(?![가-힣])"
)
RE_TEAM = re.compile(rf"([A-Za-z0-9가-힣&\.\-]{{1,20}}?(?:{_TEAM_ALT}))(?![가-힣])")


def _en_unit_pattern(suffixes: list[str]) -> re.Pattern[str]:
    return re.compile(
        r"([A-Z][A-Za-z0-9&\.\-]*(?:[ \t]+[A-Z&][A-Za-z0-9&\.\-]*){0,3}[ \t]+"
        rf"(?:{'|'.join(re.escape(s) for s in suffixes)}))\b"
    )


RE_EN_DIVISION = _en_unit_pattern(EN_DIVISION_SUFFIXES)
RE_EN_TEAM = _en_unit_pattern(EN_TEAM_SUFFIXES)

# Korean honorific verb endings that happen to close with 실 - "많으실",
# "있으실", "하실". Without this they are read as a 실 organisational unit.
RE_VERB_ENDING = re.compile(r"(?:으|하|되|주|시|가|오|보|받|드|계|리|르)실$")

# "Overseas Sales Team", "R&D Division". The inner separators are explicitly
# spaces and tabs rather than \s, which would match a newline and let the
# pattern splice two unrelated lines into one bogus department.
RE_EN_DEPT = re.compile(
    r"([A-Z][A-Za-z0-9&\.\-]*(?:[ \t]+[A-Z&][A-Za-z0-9&\.\-]*){0,3}[ \t]+"
    rf"(?:{'|'.join(re.escape(s) for s in EN_DEPT_SUFFIXES)}))\b"
)

# A line that is nothing but a person's name: "John Smith", "Jane A. Doe".
#
# The optional comma matters more than it looks: Korean offices of global
# companies sign as "Kim, Gyuree" or "Ji, Dong Jin". Without it those lines are
# not recognised as names, the block is not treated as a personal signature,
# and every organisational field is discarded.
RE_EN_NAME_LINE = re.compile(
    r"^[A-Z][a-zA-Z\-']+,?(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z\-']+){1,2}$"
)

# Korean sentence endings and courtesy phrases. A line containing one of these
# is prose from the message body, not part of the signature block.
RE_PROSE = re.compile(
    r"습니다|합니다|입니다|드립니다|바랍니다|하세요|세요|십시오|주세요|감사|안녕"
    r"|please|regards|thank|sincerely|hello|dear",
    re.IGNORECASE,
)

RE_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

# Korean landline / mobile / international formats.
RE_PHONE = re.compile(
    r"(?:\+?82[\-\s\.]?)?0?\d{1,4}[\-\s\.]\d{3,4}[\-\s\.]\d{4}"
    r"|\+\d{1,3}[\-\s\.]?\d{1,4}[\-\s\.]?\d{3,4}[\-\s\.]?\d{3,4}"
)

# Labels that mark the number that follows as mobile vs. landline.
#
# Single-letter labels (M, C, T, F) must be followed by "." or ":" to count.
# A bare letter is far too common inside ordinary Korean text - the real
# address fragment "원그로브 C동 12층" was being read as a mobile marker.
RE_MOBILE_LABEL = re.compile(
    r"(?:^|[\s\|,/·\(\[])(?:(?:Mobile|Cell|HP|H\.P|H/P|핸드폰|휴대폰|휴대전화|모바일)"
    r"\s*[\.:：）\)]?|(?:M|C)\s*[\.:：])\s*",
    re.IGNORECASE,
)
RE_PHONE_LABEL = re.compile(
    r"(?:^|[\s\|,/·\(\[])(?:(?:Tel|TEL|Phone|Off|Office|직통|유선|전화|사무실)"
    r"\s*[\.:：）\)]?|T\s*[\.:：])\s*",
    re.IGNORECASE,
)
RE_FAX_LABEL = re.compile(
    r"(?:^|[\s\|,/·])(?:(?:Fax|팩스)\s*[\.:：）\)]?|F\s*[\.:：])\s*", re.IGNORECASE
)

# --------------------------------------------------------------------------
# Labelled fields
#
# Korean corporate footers label their fields explicitly - "주소 : ...",
# "문의처: ...", "전화 : ...". Reading the label is far more reliable than
# inferring a field from its position, so labels are consulted first and the
# positional heuristics are only a fallback.
#
# Order matters: mobile is tried before phone so "휴대폰"/"M." is not swallowed
# by the more general telephone labels.
# --------------------------------------------------------------------------
LABEL_PATTERNS: dict[str, list[str]] = {
    "mobile": ["휴대폰", "휴대전화", "핸드폰", "모바일", "Mobile", "Cell",
               "H.P", "H/P", "HP", "M"],
    "fax": ["팩스", "Fax", "F"],
    "phone": ["대표전화", "사무실", "문의처", "연락처", "직통", "유선", "전화",
              "Tel", "Phone", "Office", "T"],
    "address": ["본사주소", "회사주소", "소재지", "주소", "본사", "Address", "Addr"],
    "email": ["이메일", "메일주소", "E-mail", "Email", "Mail"],
    "website": ["홈페이지", "웹사이트", "Homepage", "Website", "Web", "URL"],
    "department": ["부서", "소속", "Department", "Dept"],
    "title": ["직급", "직위", "직책", "Position", "Title"],
    "name": ["성명", "이름", "Name"],
    "company": ["회사명", "상호", "회사", "Company"],
}


def _compile_label(labels: list[str]) -> re.Pattern[str]:
    alt = "|".join(re.escape(l) for l in sorted(labels, key=len, reverse=True))
    return re.compile(rf"^\s*(?:{alt})\s*[:：]\s*(.+)$", re.IGNORECASE)


LABEL_RE: dict[str, re.Pattern[str]] = {
    field: _compile_label(labels) for field, labels in LABEL_PATTERNS.items()
}

RE_URL = re.compile(r"(?:https?://|www\.)[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+")

# An unlabelled Korean address still opens with a province or metropolitan
# city, which makes it recognisable without a label.
RE_KR_ADDRESS = re.compile(
    r"((?:서울|부산|대구|인천|광주|대전|울산|세종|경기|강원|충북|충남|전북|전남|경북|경남|제주)"
    r"(?:특별자치시|특별자치도|특별시|광역시|도)?\s+[^\n|ㅣ｜]{5,70})"
)

# Marks a line as part of a postal address, where a "센터" or "실" names a
# building rather than somebody's department.
#
# Note the deliberate omission of 동, 로 and 길 as standalone endings: Korean
# personal names routinely end in them ("홍길동"), and treating those as
# district markers made the parser discard genuine signature lines.
RE_ADDRESS_HINT = re.compile(
    r"특별시|광역시|특별자치|\d+\s*층|\d+\s*호|빌딩|타워|B/D|우편번호|\(우\)"
)

# An administrative unit only counts as an address when the line also carries
# a number - "용인시 처인구 남사읍 서촌로 12".
RE_ADMIN_UNIT = re.compile(r"[가-힣]{2,}(?:시|군|구|읍|면)(?=\s|,|$)")

# The corporate-form markers as a pattern, so a company name can be lifted out
# of a longer sentence instead of swallowing the whole line.
RE_KO_COMPANY_TOKEN = re.compile(
    r"(?:\(주\)|㈜|\(유\)|\(재\)|주식회사|유한회사)\s?[A-Za-z0-9가-힣&\.\-]{1,25}"
    r"|[A-Za-z0-9가-힣&\.\-]{1,25}\s?(?:\(주\)|㈜|\(유\)|\(재\))"
)

# Where a reply/forward begins - everything below this is somebody else's mail.
RE_QUOTE_BOUNDARY = re.compile(
    r"^\s*(?:"
    r"-{2,}\s*Original Message\s*-{2,}"
    r"|_{5,}"
    r"|-{5,}"
    r"|={5,}"
    r"|From:\s"
    r"|Sent:\s"
    r"|보낸\s*사람\s*:"
    r"|보낸사람\s*:"
    r"|받는\s*사람\s*:"
    r"|작성자\s*:"
    r"|On .{5,80} wrote:"
    r"|.{0,40}(?:님이|님께서) .{0,20}작성"
    r")",
    re.IGNORECASE,
)

# Boilerplate that is never part of an identity.
RE_DISCLAIMER = re.compile(
    r"confidential|disclaimer|본 (?:메일|이메일)|수신을 원하지|무단 (?:전재|복제)"
    r"|intended recipient|unsubscribe|개인정보|법적 고지",
    re.IGNORECASE,
)


@dataclass
class SignatureInfo:
    """Whatever we managed to read off a signature block."""

    name: str = ""
    title: str = ""
    # The most specific organisational unit found - kept for display and for
    # rules written against {department}.
    department: str = ""
    # The two levels separately, so mail can be filed 부서 > 파트 > 담당자.
    division: str = ""   # 본부 / 실 / 사업부
    team: str = ""       # 팀 / 파트
    company: str = ""
    phone: str = ""
    mobile: str = ""
    fax: str = ""
    email: str = ""
    address: str = ""
    website: str = ""

    # True only when an actual individual signed off - a name paired with a
    # rank, a labelled 성명, or a Latin name on its own line. Bulk mail sets
    # this False even when other fields were found, because a newsletter's
    # footer describes a mailing list, not a correspondent.
    personal: bool = False

    def is_empty(self) -> bool:
        return not any(
            (self.name, self.title, self.department, self.company, self.phone,
             self.mobile, self.address)
        )

    @property
    def confidence(self) -> int:
        """Rough 0-100 score, used to prefer one parse over another."""
        score = 0
        for field_value, weight in (
            (self.name, 25), (self.company, 25), (self.title, 20),
            (self.department, 15), (self.address, 10), (self.mobile, 10),
            (self.phone, 5),
        ):
            if field_value:
                score += weight
        return min(score, 100)


def _clean_field(value: str) -> str:
    """Collapse any internal whitespace and trim punctuation.

    A regex that spans a line break would otherwise store a value containing a
    newline, which then breaks folder names and category names downstream.
    """
    return re.sub(r"\s+", " ", value or "").strip(" ,|/-·\t\r\n")


def strip_quoted(body: str) -> str:
    """Drop everything from the first reply/forward marker onwards.

    Without this, a long thread would have us parse the signature of whoever
    wrote three replies ago and attribute it to the current sender.
    """
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if RE_QUOTE_BOUNDARY.match(line):
            return "\n".join(lines[:i])
    return body


def _clean_lines(text: str, tail: int = 18) -> list[str]:
    """Last few meaningful lines - where a signature almost always sits."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [
        ln for ln in lines
        if ln and not RE_DISCLAIMER.search(ln) and not set(ln) <= set("-=_*~ ")
    ]
    return lines[-tail:]


def _looks_like_address(line: str) -> bool:
    """True when a line reads as a postal address rather than an identity."""
    if RE_ADDRESS_HINT.search(line):
        return True
    return bool(RE_ADMIN_UNIT.search(line) and re.search(r"\d", line))


def _segments(line: str) -> list[str]:
    """Split a signature line on its visual separators.

    Commas are deliberately *not* separators: they occur inside legitimate
    company names such as "Hanguk Electronics Co., Ltd.".

    Korean footers frequently use the hangul filler ㅣ (U+3163) or the
    fullwidth ｜ (U+FF5C) in place of a pipe, so both count as separators.
    """
    parts = re.split(r"\s*[|/·ㅣ｜･]\s*|\s{2,}", line)
    return [p.strip() for p in parts if p.strip()]


def _looks_like_person_name(line: str) -> bool:
    """True when a Latin-script line is plausibly a person rather than a firm.

    "DMK Global" and "IBM Korea" have the shape of a two-word personal name,
    so an all-capitals token is taken as evidence of an organisation.
    """
    if not RE_EN_NAME_LINE.match(line):
        return False
    if any(marker.lower() in line.lower() for marker in EN_COMPANY_MARKERS):
        return False
    # A line that is itself an organisational unit is not a person, however
    # much it resembles a three-part name: "The Google Team", "Korea HR
    # Services".
    if RE_EN_DIVISION.search(line) or RE_EN_TEAM.search(line):
        return False
    return not any(
        len(token.strip(".")) >= 2 and token.strip(".").isupper()
        for token in line.split()
    )


def is_organisational_unit(candidate: str) -> bool:
    """Public check that a stored 부서/파트 value is plausible.

    Used by the store so a bad value from an older parser cannot survive
    indefinitely: blanks never overwrite, so anything written once stays.
    """
    return _is_org_unit(candidate, DIVISION_SUFFIXES + TEAM_SUFFIXES)


def _is_org_unit(candidate: str, suffixes: list[str]) -> bool:
    """True when a match is a real organisational unit rather than a word.

    "영업팀" is a team; a bare suffix like "팀" is not, and neither is an
    everyday word that merely ends in one, nor an organisation in its own right
    such as a 재단.
    """
    candidate = candidate.strip()
    return (
        len(candidate) >= 2
        and candidate not in suffixes
        and candidate not in DEPT_STOPWORDS
        and not candidate.endswith(ORG_NOT_DEPT_SUFFIXES)
        and not RE_VERB_ENDING.search(candidate)
    )


def _extract_labelled(lines: list[str]) -> dict[str, str]:
    """Read every "라벨 : 값" pair the block declares. First one per field wins."""
    found: dict[str, str] = {}
    for line in lines:
        for field, pattern in LABEL_RE.items():
            if field in found:
                continue
            match = pattern.match(line)
            if match and match.group(1).strip():
                found[field] = match.group(1).strip()
    return found


def _extract_company(lines: list[str]) -> str:
    """Find the organisation name in a signature block.

    A line often carries several fields at once ("재무팀 / (주)서울파이낸스"),
    so we return the *segment* holding the corporate-form marker rather than
    the first segment of the line.
    """
    for line in lines:
        # Korean forms are bounded by the marker itself, so the name can be
        # lifted out of anywhere in the line - including mid-sentence, as in
        # "안녕하십니까? 가온전선(주)입니다".
        korean = RE_KO_COMPANY_TOKEN.search(line)
        if korean:
            candidate = _clean_field(korean.group(0))
            if 2 <= len(candidate) <= 60:
                return candidate

        # English forms end at their marker, so keep everything up to it and
        # drop whatever follows - usually the postal address.
        for segment in _segments(line):
            for marker in sorted(EN_COMPANY_MARKERS, key=len, reverse=True):
                position = segment.lower().find(marker.lower())
                if position < 0:
                    continue
                # The marker has to be its own word. Without this, a tracking
                # token such as "eNg-xIMAkWZ6XpLcInc" would be harvested as a
                # company simply because "Inc" appears inside it.
                if position > 0 and not segment[position - 1].isspace():
                    continue
                candidate = _clean_field(
                    RE_COMPANY_NOISE_PREFIX.sub(
                        "", RE_EMAIL.sub("", segment[: position + len(marker)]).strip()
                    )
                )
                # A bare marker is not a name: the word "Company" alone tells
                # us nothing about who sent the mail.
                if len(candidate) > len(marker) and 2 <= len(candidate) <= 60:
                    return candidate
    return ""


def _extract_phones(text: str) -> tuple[str, str]:
    """Return (landline, mobile), using nearby labels to tell them apart."""
    phone = mobile = ""

    for match in RE_PHONE.finditer(text):
        number = match.group(0).strip()
        # Look at the ~14 characters in front of the number for its label.
        prefix = text[max(0, match.start() - 14): match.start()]

        if RE_FAX_LABEL.search(prefix):
            continue  # A fax number identifies an office, not a person.
        digits = re.sub(r"\D", "", number)
        if RE_MOBILE_LABEL.search(prefix) or digits.startswith(("010", "8210")):
            mobile = mobile or number
        elif RE_PHONE_LABEL.search(prefix):
            phone = phone or number
        else:
            phone = phone or number

    return phone, mobile


def parse(body: str, sender_name: str = "") -> SignatureInfo:
    """Best-effort read of the signature block in `body`.

    `sender_name` (the display name Outlook reports) is used only as a
    fallback when no name can be found in the text itself.
    """
    if not body:
        return SignatureInfo(name=sender_name.strip())

    text = strip_quoted(body)
    lines = _clean_lines(text, tail=26)
    if not lines:
        return SignatureInfo(name=sender_name.strip())

    # Identity fields are only read from lines that look like signature lines.
    # Sentences from the message body ("첨부 파일 확인 부탁드립니다") contain
    # words that superficially resemble departments and ranks, and would
    # otherwise poison every field.
    sig_lines = [ln for ln in lines if len(ln) <= 70 and not RE_PROSE.search(ln)]
    if not sig_lines:
        sig_lines = lines

    blob = "\n".join(sig_lines)
    info = SignatureInfo()

    # ---- Declared labels win over anything we could infer ----------------
    labelled = _extract_labelled(lines)
    for field in ("name", "title", "department", "company", "address"):
        if labelled.get(field):
            setattr(info, field, labelled[field])
    if labelled.get("name"):
        info.personal = True
    for field in ("phone", "mobile", "fax"):
        if labelled.get(field):
            number = RE_PHONE.search(labelled[field])
            if number:
                setattr(info, field, number.group(0).strip())
    if labelled.get("email"):
        found = RE_EMAIL.search(labelled["email"])
        if found:
            info.email = found.group(0).lower()
    if labelled.get("website"):
        url = RE_URL.search(labelled["website"])
        info.website = url.group(0) if url else labelled["website"]

    # ---- Positional fallbacks, only for what the labels left unset -------
    # Name + rank together is the strongest signal, so try it first.
    match = RE_KO_NAME_TITLE.search(blob)
    if match and match.group(1) not in NAME_STOPWORDS:
        info.name = info.name or match.group(1)
        info.title = info.title or match.group(2)
        info.personal = True
    elif not info.title:
        ko_title = RE_KO_TITLE.search(blob)
        if ko_title:
            info.title = ko_title.group(1)
        else:
            en_title = RE_EN_TITLE.search(blob)
            if en_title:
                info.title = en_title.group(1)

    # Organisational units. Division and team are collected separately so mail
    # can be filed 부서 > 파트 > 담당자 rather than into one flat level.
    #
    # Address lines are skipped: a building named "한국지식재산센터" or a room
    # named "수면실" ends in a unit suffix but is a place, not a team.
    for line in sig_lines:
        if _looks_like_address(line):
            continue
        if not info.division:
            found = RE_DIVISION.search(line)
            if found and _is_org_unit(found.group(1), DIVISION_SUFFIXES):
                info.division = found.group(1).strip()
        if not info.team:
            found = RE_TEAM.search(line)
            if found and _is_org_unit(found.group(1), TEAM_SUFFIXES):
                info.team = found.group(1).strip()
        if info.division and info.team:
            break

    # Latin-script footers, which is what a global company's Korean office
    # actually sends: "Automotive BU", "Korea HR Services".
    for line in sig_lines:
        if info.division and info.team:
            break
        if _looks_like_address(line) or RE_EMAIL.search(line):
            continue
        if not info.division:
            found = RE_EN_DIVISION.search(line)
            if found:
                info.division = found.group(1).strip()
        if not info.team:
            found = RE_EN_TEAM.search(line)
            if found:
                info.team = found.group(1).strip()

    # A label such as "부서 : 해외영업팀" is authoritative but unclassified, so
    # sort it onto the level its own suffix implies.
    if info.department and not (info.division or info.team):
        if info.department.endswith(tuple(TEAM_SUFFIXES)):
            info.team = info.department
        else:
            info.division = info.department

    # The most specific unit is what people mean by "부서" in a contact list.
    info.department = info.department or info.team or info.division

    # Company detection sees the short signature lines plus any long line that
    # carries statutory footer details - a real footer often packs the company,
    # the CEO, the registration number and the address onto one long line.
    # Arbitrary long marketing copy stays excluded: scanning it turned a
    # newsletter's advertised employer into the sender's own company.
    if not info.company:
        info.company = _extract_company(
            [l for l in lines if len(l) <= 70 or RE_CORPORATE_FOOTER.search(l)]
        )

    # Phone numbers are read from every line, not just the short ones. A
    # contact line packing "TEL ... MOBILE ... EMAIL ..." onto one row is
    # normal and easily exceeds the length filter; the number patterns are
    # distinctive enough not to need the prose guard.
    phone, mobile = _extract_phones("\n".join(lines))
    info.phone = info.phone or phone
    info.mobile = info.mobile or mobile

    if not info.address:
        for line in lines:
            found = RE_KR_ADDRESS.search(line)
            if found:
                info.address = found.group(1)
                break

    if not info.email:
        email = RE_EMAIL.search(blob)
        if email:
            info.email = email.group(0).lower()

    if not info.name:
        # A Latin-script name sits on a line of its own, above the job title.
        # This runs before the evidence check below, because finding a name is
        # itself the strongest evidence that a person signed off - and a global
        # company's footer ("Kim, Gyuree" / "Automotive BU") often carries no
        # Korean rank at all.
        for line in sig_lines:
            if line == info.company or RE_EMAIL.search(line):
                continue
            if _looks_like_person_name(line) and not RE_EN_TITLE.search(line):
                info.name = line
                info.personal = True
                break

    # Bulk and marketing mail has no personal signature, but it does have
    # plenty of capitalised prose that these patterns will happily mistake for
    # a job title or a department. Require positive evidence that a real
    # individual signed off - a name-with-rank, or a contact number - before
    # attributing person-level fields to anybody.
    #
    # A bare Latin name line is deliberately NOT accepted as that evidence: it
    # is a weak signal that also fires on "Browse Catalog" in a newsletter
    # footer. A real sign-off almost always carries a number as well.
    if not (RE_KO_NAME_TITLE.search(blob) or info.mobile or info.phone):
        info.title = ""
        info.department = ""
        info.division = ""
        info.team = ""

    if not info.name:
        # Outlook's display name is often "홍길동" or "홍길동 부장".
        fallback = sender_name.strip()
        named = RE_KO_NAME_TITLE.search(fallback)
        if named:
            info.name = named.group(1)
            info.title = info.title or named.group(2)
        else:
            info.name = fallback

    for field_name in ("name", "title", "department", "division", "team",
                       "company", "phone", "mobile", "fax", "address", "website"):
        setattr(info, field_name, _clean_field(getattr(info, field_name)))
    info.address = info.address[:120]
    return info
