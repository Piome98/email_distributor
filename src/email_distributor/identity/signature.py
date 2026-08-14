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
# 국, 처 and 단 were tried and removed: as single characters they match far
# more ordinary Korean words than departments - 한국/외국/결국, 거래처/출처/근처,
# and 재단/집단/판단. "한국고등교육재단" (a foundation) was being filed as
# somebody's department because of the trailing 단.
DEPT_SUFFIXES = [
    "사업본부", "사업부문", "연구소", "사업부", "본부", "센터", "그룹",
    "팀", "실", "부", "과", "파트",
]

# Suffixes that mark a whole organisation rather than a unit inside one, so a
# match ending in one of these is never a department.
ORG_NOT_DEPT_SUFFIXES = ("재단", "법인", "공단", "공사", "협회", "조합", "학회")

EN_DEPT_SUFFIXES = [
    "Business Unit", "Headquarters", "Laboratory", "Department", "Institute",
    "Division", "Team", "Group", "Center", "Centre", "Office", "Unit",
    "Dept.", "Dept", "Lab", "HQ",
]

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

# "Overseas Sales Team", "R&D Division". The inner separators are explicitly
# spaces and tabs rather than \s, which would match a newline and let the
# pattern splice two unrelated lines into one bogus department.
RE_EN_DEPT = re.compile(
    r"([A-Z][A-Za-z0-9&\.\-]*(?:[ \t]+[A-Z&][A-Za-z0-9&\.\-]*){0,3}[ \t]+"
    rf"(?:{'|'.join(re.escape(s) for s in EN_DEPT_SUFFIXES)}))\b"
)

# A line that is nothing but a person's name: "John Smith", "Jane A. Doe".
RE_EN_NAME_LINE = re.compile(
    r"^[A-Z][a-zA-Z\-']+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-zA-Z\-']+){1,2}$"
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
RE_MOBILE_LABEL = re.compile(
    r"(?:^|[\s\|,/·\(\[])(?:M|Mobile|Cell|C|HP|H\.P|H/P|핸드폰|휴대폰|휴대전화|모바일)"
    r"\s*[\.:）\)]?\s*",
    re.IGNORECASE,
)
RE_PHONE_LABEL = re.compile(
    r"(?:^|[\s\|,/·\(\[])(?:T|Tel|TEL|Phone|Off|Office|직통|유선|전화|사무실)"
    r"\s*[\.:）\)]?\s*",
    re.IGNORECASE,
)
RE_FAX_LABEL = re.compile(r"(?:^|[\s\|,/·])(?:F|Fax|팩스)\s*[\.:）\)]?\s*", re.IGNORECASE)

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
    department: str = ""
    company: str = ""
    phone: str = ""
    mobile: str = ""
    email: str = ""

    def is_empty(self) -> bool:
        return not any(
            (self.name, self.title, self.department, self.company, self.phone,
             self.mobile)
        )

    @property
    def confidence(self) -> int:
        """Rough 0-100 score, used to prefer one parse over another."""
        score = 0
        for field_value, weight in (
            (self.name, 25), (self.company, 25), (self.title, 20),
            (self.department, 15), (self.mobile, 10), (self.phone, 5),
        ):
            if field_value:
                score += weight
        return score


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


def _segments(line: str) -> list[str]:
    """Split a signature line on its visual separators.

    Commas are deliberately *not* separators: they occur inside legitimate
    company names such as "Hanguk Electronics Co., Ltd.".
    """
    parts = re.split(r"\s*[|/·]\s*|\s{2,}", line)
    return [p.strip() for p in parts if p.strip()]


def _extract_company(lines: list[str]) -> str:
    """Find the organisation name in a signature block.

    A line often carries several fields at once ("재무팀 / (주)서울파이낸스"),
    so we return the *segment* holding the corporate-form marker rather than
    the first segment of the line.
    """
    for line in lines:
        for marker in KO_COMPANY_MARKERS + EN_COMPANY_MARKERS:
            if marker.lower() not in line.lower():
                continue
            for segment in _segments(line):
                if marker.lower() in segment.lower():
                    candidate = RE_EMAIL.sub("", segment).strip(" ,|/-·")
                    if 2 <= len(candidate) <= 60:
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
    lines = _clean_lines(text)
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

    # Name + rank together is the strongest signal, so try it first.
    match = RE_KO_NAME_TITLE.search(blob)
    if match:
        info.name, info.title = match.group(1), match.group(2)
    else:
        ko_title = RE_KO_TITLE.search(blob)
        if ko_title:
            info.title = ko_title.group(1)
        else:
            en_title = RE_EN_TITLE.search(blob)
            if en_title:
                info.title = en_title.group(1)

    dept = RE_DEPT.search(blob)
    if dept:
        candidate = dept.group(1).strip()
        # "영업팀" is a department; a bare suffix like "팀" is not, and neither
        # is an everyday word that merely ends in one.
        if (
            len(candidate) >= 2
            and candidate not in DEPT_SUFFIXES
            and candidate not in DEPT_STOPWORDS
            and not candidate.endswith(ORG_NOT_DEPT_SUFFIXES)
        ):
            info.department = candidate
    if not info.department:
        en_dept = RE_EN_DEPT.search(blob)
        if en_dept:
            info.department = en_dept.group(1).strip()

    info.company = _extract_company(sig_lines)
    info.phone, info.mobile = _extract_phones(blob)

    email = RE_EMAIL.search(blob)
    if email:
        info.email = email.group(0).lower()

    # Bulk and marketing mail has no personal signature, but it does have
    # plenty of capitalised prose that these patterns will happily mistake for
    # a job title or a department. Require some positive evidence that a real
    # individual signed off - a name-with-rank, or a contact number - before
    # attributing person-level fields to anybody.
    if not (RE_KO_NAME_TITLE.search(blob) or info.mobile or info.phone):
        info.title = ""
        info.department = ""

    if not info.name:
        # A Latin-script name sits on a line of its own, above the job title.
        for line in sig_lines:
            if line == info.company or RE_EMAIL.search(line):
                continue
            if RE_EN_NAME_LINE.match(line) and not RE_EN_TITLE.search(line):
                info.name = line
                break

    if not info.name:
        # Outlook's display name is often "홍길동" or "홍길동 부장".
        fallback = sender_name.strip()
        named = RE_KO_NAME_TITLE.search(fallback)
        if named:
            info.name = named.group(1)
            info.title = info.title or named.group(2)
        else:
            info.name = fallback

    for field_name in ("name", "title", "department", "company", "phone", "mobile"):
        setattr(info, field_name, _clean_field(getattr(info, field_name)))
    return info
