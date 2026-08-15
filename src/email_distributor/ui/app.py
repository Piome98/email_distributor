"""Tkinter desktop UI.

Tkinter is not the prettiest toolkit available, but it ships inside CPython.
On a managed corporate laptop that matters more than looks: the entire install
is `pip install --user pywin32`, with no second GUI framework to get past IT.

Threading model: Outlook COM objects are apartment-threaded and cannot be
shared, so every mailbox operation runs on a worker thread that builds its own
`OutlookClient` and `IdentityStore`. Workers never touch widgets; they post
messages onto a queue that the UI thread drains on a timer.
"""

from __future__ import annotations

import json
import queue
import threading
import traceback
from typing import Any, Callable, Optional

import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from ..actions.filing import Distributor, RunSummary
from ..actions.folders import FolderBuilder
from ..config import Settings, data_dir, rules_path
from ..identity.learner import Learner
from ..identity.models import SOURCE_MANUAL
from ..identity.store import IdentityStore
from ..outlook.client import COM_AVAILABLE, OutlookClient
from ..rules.engine import RuleSet, default_ruleset
from ..service.watcher import Watcher

UI_FONT = ("Malgun Gothic", 9)   # renders Korean and Latin correctly
MONO_FONT = ("Consolas", 9)


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Email Distributor - 메일 자동 분류")
        self.geometry("1060x720")
        self.minsize(900, 600)

        self.settings = Settings.load()
        self.store = IdentityStore()          # this thread's connection only
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.watcher: Optional[Watcher] = None
        self._busy = False

        self._init_style()
        self._build()
        self._refresh_companies()
        self._refresh_people()
        self._load_rules_text()

        self.after(120, self._drain_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        if not COM_AVAILABLE:
            self._log("error", "pywin32 is not installed - run: pip install --user pywin32")
        else:
            self._log("info", f"Ready. Data folder: {data_dir()}")
            if self.settings.dry_run:
                self._log("info", "DRY RUN is on - nothing in your mailbox will change.")

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------
    def _init_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure(".", font=UI_FONT)
        style.configure("Treeview", rowheight=22, font=UI_FONT)
        style.configure("Header.TLabel", font=("Malgun Gothic", 11, "bold"))

    def _build(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.tab_run = ttk.Frame(notebook)
        self.tab_companies = ttk.Frame(notebook)
        self.tab_people = ttk.Frame(notebook)
        self.tab_rules = ttk.Frame(notebook)
        self.tab_settings = ttk.Frame(notebook)

        notebook.add(self.tab_run, text="  실행 / Run  ")
        notebook.add(self.tab_companies, text="  회사 / Companies  ")
        notebook.add(self.tab_people, text="  담당자 / People  ")
        notebook.add(self.tab_rules, text="  규칙 / Rules  ")
        notebook.add(self.tab_settings, text="  설정 / Settings  ")

        self._build_run_tab()
        self._build_companies_tab()
        self._build_people_tab()
        self._build_rules_tab()
        self._build_settings_tab()

        self.status = tk.StringVar(value="Ready")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill="x", side="bottom"
        )

    # -- Run tab -------------------------------------------------------
    def _build_run_tab(self) -> None:
        frame = self.tab_run

        top = ttk.LabelFrame(frame, text="1단계 · 사서함 학습 (Learn from your mailbox)")
        top.pack(fill="x", padx=6, pady=6)
        ttk.Label(
            top,
            text="받은 편지함과 보낸 편지함을 읽어 회사·담당자 정보를 만듭니다. "
            "메일을 변경하지 않고 읽기만 합니다.",
            wraplength=980,
        ).pack(anchor="w", padx=8, pady=(6, 2))
        ttk.Button(top, text="사서함 학습 시작  (Learn)", command=self._on_learn).pack(
            anchor="w", padx=8, pady=(0, 8)
        )

        folders = ttk.LabelFrame(frame, text="2단계 · 폴더 만들기 (Create folders)")
        folders.pack(fill="x", padx=6, pady=6)
        ttk.Label(
            folders,
            text="업체/담당자 구조로 Outlook 폴더를 미리 만듭니다. "
            "Exchange·IMAP 계정이면 서버와 동기화되어 웹·휴대폰에서도 보입니다.",
            wraplength=980,
        ).pack(anchor="w", padx=8, pady=(6, 2))

        frow = ttk.Frame(folders)
        frow.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(frow, text="최소 메일 수:").pack(side="left")
        self.var_min_msgs = tk.StringVar(value="1")
        ttk.Spinbox(frow, from_=1, to=99, width=5, textvariable=self.var_min_msgs).pack(
            side="left", padx=(4, 10)
        )
        self.var_people_folders = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frow, text="담당자 하위 폴더까지", variable=self.var_people_folders
        ).pack(side="left")

        ttk.Button(
            frow, text="폴더 만들기 (Create)", command=lambda: self._on_folders(False)
        ).pack(side="right", padx=4)
        ttk.Button(
            frow, text="미리보기 (Preview)", command=lambda: self._on_folders(True)
        ).pack(side="right", padx=4)

        mid = ttk.LabelFrame(frame, text="3단계 · 분류 실행 (Distribute)")
        mid.pack(fill="x", padx=6, pady=6)

        row = ttk.Frame(mid)
        row.pack(fill="x", padx=8, pady=6)

        self.var_dry = tk.BooleanVar(value=self.settings.dry_run)
        ttk.Checkbutton(
            row,
            text="미리보기 모드 (dry run - 실제로 옮기지 않음)",
            variable=self.var_dry,
            command=self._on_toggle_dry,
        ).pack(side="left")

        ttk.Button(row, text="한 번 실행  (Run once)", command=self._on_run_once).pack(
            side="right", padx=4
        )
        self.btn_watch = ttk.Button(
            row, text="자동 감시 시작  (Start watching)", command=self._on_toggle_watch
        )
        self.btn_watch.pack(side="right", padx=4)

        log_frame = ttk.LabelFrame(frame, text="활동 기록 (Activity)")
        log_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.log = ScrolledText(log_frame, height=18, font=MONO_FONT, wrap="word")
        self.log.pack(fill="both", expand=True, padx=6, pady=6)
        self.log.tag_config("error", foreground="#b00020")
        self.log.tag_config("action", foreground="#0b6b2f")
        self.log.tag_config("info", foreground="#333333")
        self.log.configure(state="disabled")

        btns = ttk.Frame(log_frame)
        btns.pack(fill="x", padx=6, pady=(0, 6))
        ttk.Button(btns, text="지우기 (Clear)", command=self._clear_log).pack(side="right")

    # -- Companies tab -------------------------------------------------
    def _build_companies_tab(self) -> None:
        frame = self.tab_companies
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="새로고침", command=self._refresh_companies).pack(side="left")
        ttk.Button(bar, text="수정 (Edit)", command=self._edit_company).pack(side="left", padx=4)
        ttk.Button(bar, text="삭제 (Delete)", command=self._delete_company).pack(side="left")
        ttk.Label(bar, text="  회사명을 실제 상호로 바꾸면 폴더 이름도 함께 바뀝니다.").pack(side="left", padx=8)

        columns = ("name", "group", "internal", "domains", "address", "people")
        self.tree_companies = ttk.Treeview(frame, columns=columns, show="headings")
        for col, text, width in (
            ("name", "회사 (Company)", 200),
            ("group", "그룹 (Group)", 110),
            ("internal", "사내", 50),
            ("domains", "도메인 (Domains)", 220),
            ("address", "주소 (Address)", 280),
            ("people", "담당자 수", 70),
        ):
            self.tree_companies.heading(col, text=text)
            self.tree_companies.column(col, width=width, anchor="w")
        self.tree_companies.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tree_companies.bind("<Double-1>", lambda _e: self._edit_company())

    # -- People tab ----------------------------------------------------
    def _build_people_tab(self) -> None:
        frame = self.tab_people
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="새로고침", command=self._refresh_people).pack(side="left")
        ttk.Label(bar, text="  검색:").pack(side="left", padx=(12, 2))
        self.var_search = tk.StringVar()
        entry = ttk.Entry(bar, textvariable=self.var_search, width=30)
        entry.pack(side="left")
        entry.bind("<KeyRelease>", lambda _e: self._refresh_people())

        columns = ("email", "name", "title", "dept", "company", "contact", "address", "count")
        self.tree_people = ttk.Treeview(frame, columns=columns, show="headings")
        for col, text, width in (
            ("email", "이메일", 210),
            ("name", "이름", 90),
            ("title", "직급", 70),
            ("dept", "부서", 120),
            ("company", "회사", 150),
            ("contact", "연락처", 120),
            ("address", "주소", 230),
            ("count", "메일 수", 60),
        ):
            self.tree_people.heading(col, text=text)
            self.tree_people.column(col, width=width, anchor="w")
        self.tree_people.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # -- Rules tab -----------------------------------------------------
    def _build_rules_tab(self) -> None:
        frame = self.tab_rules
        bar = ttk.Frame(frame)
        bar.pack(fill="x", padx=6, pady=6)
        ttk.Button(bar, text="저장 (Save)", command=self._save_rules_text).pack(side="left")
        ttk.Button(bar, text="다시 불러오기", command=self._load_rules_text).pack(side="left", padx=4)
        ttk.Button(bar, text="기본값으로 (Reset)", command=self._reset_rules).pack(side="left")
        ttk.Label(bar, text=f"  {rules_path()}").pack(side="left", padx=8)

        help_text = (
            "사용 가능한 치환자 placeholders:  {group} {company} {person} {department} "
            "{domain} {email} {title} {year} {month} {yyyymm}\n"
            "조건 match:  group, company, sender_domain, sender_email, department, "
            "subject_contains, body_contains, is_internal, is_unknown, "
            "is_public_domain, has_attachments, importance_min\n"
            "동작 actions:  move_to (폴더 경로), categories (분류 항목 목록), mark_read"
        )
        ttk.Label(frame, text=help_text, foreground="#555555", justify="left").pack(
            anchor="w", padx=8, pady=(0, 4)
        )

        self.rules_text = ScrolledText(frame, font=MONO_FONT, wrap="none")
        self.rules_text.pack(fill="both", expand=True, padx=6, pady=(0, 6))

    # -- Settings tab --------------------------------------------------
    def _build_settings_tab(self) -> None:
        frame = self.tab_settings
        box = ttk.LabelFrame(frame, text="설정 (Settings)")
        box.pack(fill="x", padx=6, pady=6)

        self.var_watch = tk.StringVar(value=self.settings.watch_folder)
        self.var_interval = tk.StringVar(value=str(self.settings.poll_interval))
        self.var_internal = tk.StringVar(value=", ".join(self.settings.internal_domains))
        self.var_limit = tk.StringVar(value=str(self.settings.learn_limit))
        self.var_cats = tk.BooleanVar(value=self.settings.apply_categories)
        self.var_move = tk.BooleanVar(value=self.settings.move_to_folders)

        rows = [
            ("감시할 폴더 (Watch folder)", self.var_watch, "예: Inbox"),
            ("확인 주기 (초) (Poll interval)", self.var_interval, "예: 60"),
            ("사내 도메인 (Internal domains)", self.var_internal, "쉼표로 구분"),
            ("학습 메일 수 (Learn limit)", self.var_limit, "폴더당 최대"),
        ]
        for i, (label, var, hint) in enumerate(rows):
            ttk.Label(box, text=label).grid(row=i, column=0, sticky="w", padx=8, pady=5)
            ttk.Entry(box, textvariable=var, width=46).grid(row=i, column=1, sticky="w", pady=5)
            ttk.Label(box, text=hint, foreground="#777777").grid(
                row=i, column=2, sticky="w", padx=8
            )

        ttk.Checkbutton(box, text="분류 항목(카테고리) 적용", variable=self.var_cats).grid(
            row=len(rows), column=1, sticky="w", pady=3
        )
        ttk.Checkbutton(box, text="폴더로 이동", variable=self.var_move).grid(
            row=len(rows) + 1, column=1, sticky="w", pady=3
        )

        actions = ttk.Frame(box)
        actions.grid(row=len(rows) + 2, column=1, sticky="w", pady=10)
        ttk.Button(actions, text="저장 (Save)", command=self._save_settings).pack(side="left")
        ttk.Button(
            actions, text="내 주소에서 사내 도메인 채우기", command=self._detect_internal
        ).pack(side="left", padx=6)

        info = ttk.LabelFrame(frame, text="데이터 위치 (Data location)")
        info.pack(fill="x", padx=6, pady=6)
        ttk.Label(
            info,
            text=f"{data_dir()}\n\n"
            "모든 데이터는 이 PC에만 저장되며 외부로 전송되지 않습니다.\n"
            "All data stays on this PC. The app makes no network calls.",
            justify="left",
        ).pack(anchor="w", padx=8, pady=8)

    # ------------------------------------------------------------------
    # Event plumbing
    # ------------------------------------------------------------------
    def _log(self, level: str, text: str) -> None:
        """Thread-safe: workers call this, the queue hands it to the UI."""
        self.events.put((level, text))

    def _drain_events(self) -> None:
        try:
            while True:
                level, text = self.events.get_nowait()
                self.log.configure(state="normal")
                self.log.insert("end", text + "\n", level)
                self.log.see("end")
                self.log.configure(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._drain_events)

    def _clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status.set(text)

    def _run_worker(self, job: Callable[[OutlookClient, IdentityStore], None], label: str) -> None:
        """Run a mailbox job on its own thread, with its own COM + DB handles."""
        if self._busy:
            messagebox.showinfo("실행 중", "다른 작업이 진행 중입니다. 잠시 기다려 주세요.")
            return
        self._busy = True
        self._set_status(f"{label} ...")

        def run() -> None:
            client = OutlookClient()
            store: Optional[IdentityStore] = None
            try:
                client.connect()
                store = IdentityStore()
                job(client, store)
            except Exception as exc:  # noqa: BLE001
                self._log("error", f"{label} failed: {exc}")
                self._log("error", traceback.format_exc(limit=3))
            finally:
                if store is not None:
                    store.close()
                client.close()
                self._busy = False
                self.after(0, lambda: self._set_status("Ready"))
                self.after(0, self._refresh_companies)
                self.after(0, self._refresh_people)

        threading.Thread(target=run, name=f"job-{label}", daemon=True).start()

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def _on_learn(self) -> None:
        def job(client: OutlookClient, store: IdentityStore) -> None:
            self._log("info", "Learning from mailbox - this may take a few minutes...")
            learner = Learner(client, store, self.settings)

            def progress(folder: str, done: int, total: int) -> None:
                self.after(0, lambda: self._set_status(f"Learning {folder}: {done} read"))

            stats = learner.learn_all(progress=progress)
            self._log("info", f"Learned: {stats.describe()}")
            if stats.errors:
                self._log("error", f"{stats.errors} item(s) could not be read.")

        self._run_worker(job, "Learn")

    def _on_folders(self, preview: bool) -> None:
        try:
            min_messages = max(1, int(self.var_min_msgs.get().strip()))
        except ValueError:
            min_messages = 1
        include_people = self.var_people_folders.get()

        def job(client: OutlookClient, store: IdentityStore) -> None:
            builder = FolderBuilder(client, store, RuleSet.load(), self.settings)
            report = builder.build(
                min_messages=min_messages,
                include_people=include_people,
                dry_run=preview,
            )

            self._log(
                "info",
                f"--- 폴더 {'미리보기' if preview else '만들기'} --- "
                f"store: {report.store_name} ({report.store_kind})",
            )
            if not report.store_syncs:
                self._log(
                    "error",
                    "이 저장소는 로컬 .pst 입니다. 여기에 만든 폴더는 이 PC에만 "
                    "존재하며 서버·웹·휴대폰과 동기화되지 않습니다.",
                )

            for plan in report.plans:
                if plan.error:
                    self._log("error", plan.describe())
                elif not plan.exists:
                    self._log("action", plan.describe())
            self._log("info", report.describe())
            if preview and report.missing:
                self._log("info", "'폴더 만들기' 를 누르면 실제로 생성됩니다.")

        self._run_worker(job, "폴더 미리보기" if preview else "폴더 만들기")

    def _on_run_once(self) -> None:
        self.settings.dry_run = self.var_dry.get()

        def job(client: OutlookClient, store: IdentityStore) -> None:
            ruleset = RuleSet.load()
            distributor = Distributor(client, store, ruleset, self.settings)
            mode = "DRY RUN" if self.settings.dry_run else "LIVE"
            self._log("info", f"--- Run once on '{self.settings.watch_folder}' ({mode}) ---")

            summary: RunSummary = distributor.process_watch_folder(
                limit=200,
                on_result=lambda r: self._log(
                    "error" if r.error else "action",
                    r.describe(),
                )
                if (r.decision.has_effect and not r.skipped_reason) or r.error
                else None,
            )
            self._log("info", summary.describe())
            if self.settings.dry_run and summary.planned:
                self._log(
                    "info",
                    "미리보기였습니다. 실제로 적용하려면 '미리보기 모드'를 끄고 다시 실행하세요.",
                )

        self._run_worker(job, "Run once")

    def _on_toggle_dry(self) -> None:
        if not self.var_dry.get():
            confirmed = messagebox.askyesno(
                "실제 적용 (Live mode)",
                "미리보기 모드를 끄면 메일이 실제로 폴더로 이동되고 분류 항목이 지정됩니다.\n\n"
                "먼저 미리보기로 결과를 확인하셨나요?\n\n"
                "Turn off dry run and let the app really move your mail?",
                icon="warning",
            )
            if not confirmed:
                self.var_dry.set(True)
                return
        self.settings.dry_run = self.var_dry.get()
        self.settings.save()
        self._log("info", f"Dry run: {'ON' if self.settings.dry_run else 'OFF (LIVE)'}")

    def _on_toggle_watch(self) -> None:
        if self.watcher and self.watcher.running:
            self.watcher.stop()
            self.watcher = None
            self.btn_watch.configure(text="자동 감시 시작  (Start watching)")
            self._set_status("Ready")
            return

        self.settings.dry_run = self.var_dry.get()
        self.watcher = Watcher(self.settings, on_event=self._log)
        self.watcher.start()
        self.btn_watch.configure(text="자동 감시 중지  (Stop watching)")
        self._set_status(f"Watching every {self.settings.poll_interval}s")

    # ------------------------------------------------------------------
    # Companies / people views
    # ------------------------------------------------------------------
    def _refresh_companies(self) -> None:
        for row in self.tree_companies.get_children():
            self.tree_companies.delete(row)
        for company in self.store.list_companies():
            people = len(self.store.list_people(company.id))
            self.tree_companies.insert(
                "",
                "end",
                iid=str(company.id),
                values=(
                    company.name,
                    company.group_name,
                    "예" if company.is_internal else "",
                    ", ".join(company.domains),
                    company.address,
                    people,
                ),
            )

    def _refresh_people(self) -> None:
        for row in self.tree_people.get_children():
            self.tree_people.delete(row)
        needle = (self.var_search.get() if hasattr(self, "var_search") else "").lower()
        companies = {c.id: c.name for c in self.store.list_companies()}
        for person in self.store.list_people():
            company = companies.get(person.company_id, "")
            haystack = " ".join(
                [person.email, person.display_name, person.department, company]
            ).lower()
            if needle and needle not in haystack:
                continue
            self.tree_people.insert(
                "",
                "end",
                values=(
                    person.email,
                    person.display_name,
                    person.title,
                    person.department,
                    company,
                    person.mobile or person.phone,
                    person.address,
                    person.message_count,
                ),
            )

    def _selected_company_id(self) -> Optional[int]:
        selection = self.tree_companies.selection()
        return int(selection[0]) if selection else None

    def _edit_company(self) -> None:
        company_id = self._selected_company_id()
        if company_id is None:
            messagebox.showinfo("선택 필요", "먼저 회사를 선택하세요.")
            return
        company = self.store.company_by_id(company_id)
        if company is None:
            return
        CompanyDialog(self, company, self._apply_company_edit)

    def _apply_company_edit(
        self, company_id: int, name: str, group: str, is_internal: bool
    ) -> None:
        old = self.store.company_by_id(company_id)
        if old is None or not name.strip():
            return
        # Renaming is done by writing the new name onto the same row, so the
        # domain links and person links survive the change.
        self.store.conn.execute(
            "UPDATE companies SET name = ? WHERE id = ?", (name.strip(), company_id)
        )
        self.store.conn.commit()
        self.store.upsert_company(
            name.strip(), group=group, is_internal=is_internal, source=SOURCE_MANUAL
        )
        self._refresh_companies()
        self._log("info", f"Company updated: {name} ({group or 'no group'})")

    def _delete_company(self) -> None:
        company_id = self._selected_company_id()
        if company_id is None:
            return
        company = self.store.company_by_id(company_id)
        if company is None:
            return
        if messagebox.askyesno(
            "삭제 확인",
            f"'{company.name}' 을(를) 삭제할까요?\n\n"
            "메일은 삭제되지 않으며, 이 앱의 분류 정보만 지워집니다.",
        ):
            self.store.delete_company(company_id)
            self._refresh_companies()
            self._refresh_people()

    # ------------------------------------------------------------------
    # Rules / settings
    # ------------------------------------------------------------------
    def _load_rules_text(self) -> None:
        ruleset = RuleSet.load()
        self.rules_text.delete("1.0", "end")
        self.rules_text.insert(
            "1.0", json.dumps(ruleset.to_dict(), indent=2, ensure_ascii=False)
        )

    def _save_rules_text(self) -> None:
        raw = self.rules_text.get("1.0", "end")
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            messagebox.showerror("JSON 오류", f"규칙을 저장할 수 없습니다.\n\n{exc}")
            return
        try:
            ruleset = RuleSet.from_dict(parsed)
        except (TypeError, ValueError) as exc:
            messagebox.showerror("규칙 오류", f"규칙 형식이 올바르지 않습니다.\n\n{exc}")
            return
        ruleset.save()
        self._log("info", f"Rules saved: {len(ruleset.rules)} rule(s)")
        messagebox.showinfo("저장됨", f"{len(ruleset.rules)}개의 규칙을 저장했습니다.")

    def _reset_rules(self) -> None:
        if messagebox.askyesno("기본값 복원", "규칙을 기본값으로 되돌릴까요?"):
            default_ruleset().save()
            self._load_rules_text()

    def _save_settings(self) -> None:
        try:
            interval = max(10, int(self.var_interval.get().strip()))
            limit = max(50, int(self.var_limit.get().strip()))
        except ValueError:
            messagebox.showerror("입력 오류", "주기와 학습 메일 수는 숫자여야 합니다.")
            return

        self.settings.watch_folder = self.var_watch.get().strip() or "Inbox"
        self.settings.poll_interval = interval
        self.settings.learn_limit = limit
        self.settings.apply_categories = self.var_cats.get()
        self.settings.move_to_folders = self.var_move.get()
        self.settings.internal_domains = [
            d.strip().lower().lstrip("@")
            for d in self.var_internal.get().split(",")
            if d.strip()
        ]
        self.settings.save()
        self._log("info", "Settings saved.")
        messagebox.showinfo("저장됨", "설정을 저장했습니다.")

    def _detect_internal(self) -> None:
        def job(client: OutlookClient, store: IdentityStore) -> None:
            address = client.current_user_address()
            if not address or "@" not in address:
                self._log("error", "Could not read your own address from Outlook.")
                return
            domain = address.rsplit("@", 1)[1].lower()
            self.after(0, lambda: self.var_internal.set(domain))
            self._log("info", f"Your address: {address} -> internal domain '{domain}'")

        self._run_worker(job, "Detect address")

    # ------------------------------------------------------------------
    def _on_close(self) -> None:
        if self.watcher and self.watcher.running:
            self.watcher.stop(timeout=3)
        try:
            self.store.close()
        except Exception:  # noqa: BLE001
            pass
        self.destroy()


class CompanyDialog(tk.Toplevel):
    """Small modal for renaming a company and assigning it to a group."""

    def __init__(self, parent: App, company: Any, on_save: Callable[..., None]) -> None:
        super().__init__(parent)
        self.title("회사 정보 수정")
        self.transient(parent)
        self.grab_set()
        self.resizable(False, False)

        self.company = company
        self.on_save = on_save

        self.var_name = tk.StringVar(value=company.name)
        self.var_group = tk.StringVar(value=company.group_name)
        self.var_internal = tk.BooleanVar(value=company.is_internal)

        body = ttk.Frame(self, padding=12)
        body.pack(fill="both", expand=True)

        ttk.Label(body, text="회사명 (Company)").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(body, textvariable=self.var_name, width=40).grid(row=0, column=1, pady=4)

        ttk.Label(body, text="그룹 (Group)").grid(row=1, column=0, sticky="w", pady=4)
        groups = [g.name for g in parent.store.list_groups()]
        combo = ttk.Combobox(body, textvariable=self.var_group, values=groups, width=37)
        combo.grid(row=1, column=1, pady=4)

        ttk.Checkbutton(body, text="사내 조직 (internal)", variable=self.var_internal).grid(
            row=2, column=1, sticky="w", pady=4
        )

        details = "\n".join(
            filter(
                None,
                (
                    f"도메인: {', '.join(company.domains) or '(없음)'}",
                    f"주소: {company.address}" if company.address else "",
                    f"홈페이지: {company.website}" if company.website else "",
                ),
            )
        )
        ttk.Label(body, text=details, foreground="#666666", justify="left").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(8, 4)
        )

        buttons = ttk.Frame(body)
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(buttons, text="저장", command=self._save).pack(side="right")

    def _save(self) -> None:
        self.on_save(
            self.company.id,
            self.var_name.get(),
            self.var_group.get().strip(),
            self.var_internal.get(),
        )
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
