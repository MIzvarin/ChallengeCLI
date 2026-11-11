"""Minimal Textual screens; business rules live in FitnessService."""

import asyncio
from datetime import date

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Input, Label, RichLog, Static

from fitagent.chat import write_message
from fitagent.domain import validate_profile
from fitagent.loader import ResponseLoader
from fitagent.logs import LogsPanel
from fitagent.token_chart import TokensScreen


class RiskScreen(ModalScreen):
    def __init__(self, warning):
        super().__init__()
        self.warning = warning

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="risk-dialog"):
            yield Label("Проверьте цель", classes="title")
            with VerticalScroll(id="warning-body"):
                yield Static(self.warning, markup=False)
                yield Static("Подтверждение не означает, что цель безопасна.", classes="muted")
            with Horizontal(classes="buttons"):
                yield Button("Исправить", id="edit", variant="primary")
                yield Button("Подтверждаю цель", id="confirm", variant="warning")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "confirm")


class ProfileScreen(ModalScreen):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.profile = service.store.latest("profiles") or {}
        self.goal = service.store.latest("goals") or {}

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes="profile-dialog"):
            yield Label("Профиль", classes="title")
            yield Static("Раз в неделю · если изменений нет, сохраните как есть", classes="muted")
            with VerticalScroll(id="profile-fields"):
                for name, label, value in (
                    ("height", "Рост, см", self.profile.get("height", "")),
                    ("weight", "Вес, кг", self.profile.get("weight", "")),
                    ("sex", "Пол", self.profile.get("sex", "")),
                    ("age", "Возраст · 18+", self.profile.get("age", "")),
                    ("goal", "Цель и срок", self.goal.get("text", "")),
                    ("target", "Целевой вес, кг · необязательно", self.goal.get("target") or ""),
                ):
                    yield Label(label)
                    yield Input(value=str(value), id=name)
            yield Static("", id="form-error", markup=False)
            with Horizontal(classes="buttons"):
                yield Button("Сохранить", id="save", variant="primary")
                yield Button("Позже · на сутки", id="later", disabled=not bool(self.profile))

    def on_button_pressed(self, event):
        if event.button.id == "later":
            self.service.defer()
            self.dismiss(False)
        elif event.button.id == "save":
            self.submit()

    @work(exclusive=True)
    async def submit(self):
        buttons = list(self.query(Button))
        for button in buttons:
            button.disabled = True
        error = self.query_one("#form-error", Static)
        try:
            values = {
                name: self.query_one(f"#{name}", Input).value
                for name in ("height", "weight", "sex", "age", "goal", "target")
            }
            profile = validate_profile(**values)
            error.update("Проверяем цель…")
            warning = await asyncio.to_thread(self.service.assess_goal, profile)
            confirmed = False
            if warning:
                confirmed = await self.app.push_screen_wait(RiskScreen(warning))
                if not confirmed:
                    error.update("Измените цель и сохраните снова.")
                    return
            self.service.save(profile, warning, confirmed)
            self.dismiss(True)
        except ValueError as exc:
            error.update(str(exc))
        except Exception as exc:
            self.service.audit("profile_save_error", {"type": type(exc).__name__})
            error.update(
                "Не удалось сохранить профиль. Данные формы оставлены без изменений. "
                "Попробуйте ещё раз. Если ошибка повторяется, откройте журнал командой fitagent logs."
            )
        finally:
            for button in buttons:
                button.disabled = button.id == "later" and not bool(self.profile)


class FitAgentApp(App):
    TITLE = "FitAgent"
    SUB_TITLE = ""
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+q", "quit", "Выход", priority=True),
        ("ctrl+p", "profile", "Профиль"),
        ("ctrl+l", "logs", "Логи"),
    ]
    CSS = """
    Screen { background: #121212; color: #d4d4d4; align-horizontal: center; }
    #workspace { width: 100%; max-width: 104; height: 1fr; padding: 1 3;
                 align-horizontal: center; }
    #brand { height: 2; color: #e0e0e0; text-style: bold; }
    #profile-bar { height: auto; max-height: 4; margin-bottom: 1; color: #929292; }
    RichLog { background: transparent; color: #d4d4d4; padding: 0; }
    #chat { height: 1fr; margin: 1 0; border: none; scrollbar-gutter: stable; }
    #workspace.logs-open { padding: 0 2; }
    #workspace.logs-open #brand { height: 1; }
    #workspace.logs-open #profile-bar { max-height: 2; margin-bottom: 0; }
    #workspace.logs-open #chat { min-height: 3; margin: 0; }
    #workspace.logs-open #question { margin-top: 0; }
    #status { height: 1; color: #8a8a8a; }
    Input { height: 3; border: none; border-left: wide #242424; padding: 1;
            background: #1c1c1c; color: #dedede; }
    Input:focus { border: none; border-left: wide #a0a0a0; background: #222222; }
    Input > .input--placeholder { color: #858585; }
    Input > .input--cursor { background: #cccccc; color: #181818; }
    Input > .input--selection { background: #4b4b4b; color: #ffffff; }
    Input:disabled { opacity: 60%; }
    #question { margin-top: 1; }
    Footer { background: #121212; color: #858585; }
    Footer > .footer--key { background: #121212; color: #b3b3b3; }
    Footer > .footer--description { color: #858585; }
    Footer > .footer--highlight { background: #292929; color: #e0e0e0; }
    Footer > .footer--highlight-key { background: #292929; color: #ffffff; }
    ModalScreen { align: center middle; background: #000000 55%; }
    #dialog { width: 86%; max-width: 76; height: 90%; padding: 1 3;
              background: #191919; border: none; }
    #dialog.risk-dialog { height: 70%; max-height: 26; }
    #profile-fields { height: 1fr; margin-top: 1; }
    #warning-body { height: 1fr; }
    .title { height: auto; text-style: bold; color: #e0e0e0; margin-bottom: 1; }
    .muted { height: auto; color: #969696; margin-bottom: 1; }
    #profile-fields Label { margin-top: 1; color: #a8a8a8; }
    .buttons { height: auto; margin-top: 1; }
    Button { height: 3; min-width: 12; margin-right: 1; padding: 1 2; border: none;
             background: #282828; color: #c8c8c8; text-style: none; }
    Button.-primary { background: #d0d0d0; color: #171717; }
    Button.-warning { background: #303030; color: #eeeeee; }
    Button:hover { background: #424242; color: #ffffff; border: none; }
    Button:focus { text-style: bold underline; border: none; }
    Button.-active { background: #a0a0a0; color: #121212; }
    Button:disabled { background: #202020; color: #777777; text-opacity: 100%; }
    #form-error { height: auto; max-height: 5; color: #d0d0d0; margin-top: 1; }
    * { scrollbar-size: 1 1; scrollbar-color: #424242; scrollbar-color-hover: #626262;
        scrollbar-color-active: #858585; scrollbar-background: #191919;
        scrollbar-background-hover: #191919; scrollbar-background-active: #191919; }
    """

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.busy = False

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace"):
            yield Static("fitagent", id="brand")
            yield Static("", id="profile-bar", markup=False)
            yield RichLog(id="chat", min_width=1, wrap=True, markup=False)
            yield LogsPanel(self.service.store, id="logs-panel")
            yield ResponseLoader(id="response-loader")
            yield Static(
                "/profile   /progress   /logs   /tokens   /clear   /help   /exit",
                id="status",
                markup=False,
            )
            yield Input(placeholder="Напишите сообщение…", id="question")
        yield Footer()

    def on_mount(self):
        self.refresh_profile()
        self.restore_chat()
        self.set_interval(60, self.check_reminder)
        self.query_one("#question", Input).focus()
        self.check_reminder()

    def restore_chat(self):
        chat = self.query_one("#chat", RichLog)
        try:
            messages = self.service.store.chat_messages()
        except Exception:
            chat.write(
                "Не удалось загрузить переписку. Перезапустите приложение; "
                "если ошибка повторяется, проверьте доступ к папке данных."
            )
            return
        if not messages:
            chat.write("Питание, тренировки и ваш прогресс. Начните с вопроса.")
        for message in messages:
            write_message(
                chat,
                message["role"],
                message["content"],
                message["created_at"] if message["role"] == "user" else "",
                message["status"],
            )

    def refresh_profile(self, saved=None):
        profile = self.service.store.latest("profiles")
        goal = self.service.store.latest("goals")
        text = "Заполните профиль, чтобы начать. Данные хранятся локально."
        if profile and goal:
            text = (
                f"{profile['height']:g} см · {profile['weight']:g} кг · {profile['age']} лет\n"
                f"Цель: {goal['text']} · целевой вес: {goal['target'] or 'не задан'}"
            )
        self.query_one("#profile-bar", Static).update(text)

    def check_reminder(self):
        if not self.busy and len(self.screen_stack) == 1 and self.service.due():
            self.action_profile()

    def action_profile(self):
        if not self.busy and len(self.screen_stack) == 1:
            self.push_screen(ProfileScreen(self.service), self.refresh_profile)

    def action_logs(self):
        if len(self.screen_stack) == 1:
            panel = self.query_one("#logs-panel", LogsPanel)
            panel.display = not panel.display
            self.query_one("#workspace").set_class(panel.display, "logs-open")
            panel.refresh_logs()

    def action_clear(self):
        if self.busy or len(self.screen_stack) != 1:
            return
        log = self.query_one("#chat", RichLog)
        try:
            self.service.clear()
        except Exception:
            log.write(
                "Не удалось очистить данные. Повторите попытку; проверьте доступ к папке данных."
            )
            return
        log.clear()
        log.write("Данные очищены. Начата новая сессия.")
        self.query_one("#question", Input).value = ""
        self.query_one("#response-loader", ResponseLoader).stop()
        self.query_one("#status", Static).update(
            "/profile   /progress   /logs   /tokens   /clear   /help   /exit"
        )
        self.refresh_profile()
        self.query_one("#logs-panel", LogsPanel).refresh_logs()
        self.action_profile()

    def action_tokens(self):
        if len(self.screen_stack) == 1:
            self.push_screen(TokensScreen(self.service.store))

    def on_input_submitted(self, event):
        if event.input.id != "question" or self.busy:
            return
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if text == "/logs":
            self.action_logs()
        elif text == "/tokens":
            self.action_tokens()
        elif text == "/exit":
            self.exit()
        elif text == "/clear":
            self.action_clear()
        elif text == "/profile":
            self.action_profile()
        elif text == "/progress":
            rows = self.service.store.weights()
            log = self.query_one("#chat", RichLog)
            log.write("Динамика веса (последние 30 подтверждений):")
            for row in rows:
                log.write(f"{row['confirmed_at'][:10]}  {row['weight']:g} кг")
            if rows:
                log.write(f"Изменение: {rows[-1]['weight'] - rows[0]['weight']:+.1f} кг")
        elif text == "/help":
            self.query_one("#chat", RichLog).write(
                "/profile — профиль и новая цель; /progress — вес; /logs — аудит.\n"
                "/tokens — график накопленного расхода токенов; Escape — вернуться в чат.\n"
                "/exit или Ctrl+Q — закрыть приложение без удаления сохранённых данных.\n"
                "/logs или Ctrl+L — показать/скрыть нижнюю панель логов и токенов. "
                "Контекст и ответ — по статистике API после получения ответа; "
                "общий расход включает оценку цели и сводки.\n"
                "/clear — без подтверждения удаляет профиль, цели, переписку, сводки и логи, "
                "начинает новую сессию. API-ключ остаётся.\n"
                "После отказа обновлять профиль напомним через сутки. "
                "Профиль и переписка передаются API. Ответы не заменяют врача."
            )
        elif text.startswith("/"):
            self.query_one("#chat", RichLog).write("Неизвестная команда. Доступные: /help")
        else:
            self.ask(text)

    @work(exclusive=True)
    async def ask(self, text):
        self.busy = True
        field = self.query_one("#question", Input)
        field.disabled = True
        log = self.query_one("#chat", RichLog)
        write_message(log, "user", text, date.today().isoformat())
        status = self.query_one("#status", Static)
        status.update("Ctrl+L · журнал")
        loader = self.query_one("#response-loader", ResponseLoader)
        loader.start()
        try:
            answer = await asyncio.to_thread(self.service.answer, text)
            write_message(log, "assistant", answer)
        except Exception as exc:
            # No traceback/request payload in UI or logs; audit only exception type.
            self.service.audit("ui_error", {"type": type(exc).__name__})
            log.write(str(exc) if isinstance(exc, ValueError) else "Ошибка приложения. См. /logs.")
            field.value = text
        finally:
            loader.stop()
            self.query_one("#logs-panel", LogsPanel).refresh_logs()
            status.update("/profile   /progress   /logs   /tokens   /clear   /help   /exit")
            self.busy = False
            field.disabled = False
            field.focus()
