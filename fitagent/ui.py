"""Minimal Textual screens; business rules live in FitnessService."""

import asyncio
import sqlite3
from datetime import date

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Footer, Input, RichLog, Select, Static

from fitagent.chat import basket_display, write_message, write_topic_notice
from fitagent.daily_plan import ERROR as DAILY_PLAN_ERROR
from fitagent.daily_plan import PENDING as DAILY_PLAN_PENDING
from fitagent.domain import PROFILE_SEX_OPTIONS
from fitagent.goal_state import LABELS
from fitagent.loader import ResponseLoader
from fitagent.logs import LogsPanel
from fitagent.onboarding import validate_wake_time
from fitagent.plan_mcp import request_plan
from fitagent.token_chart import TokensScreen
from fitagent.training import ACCEPT as ACCEPT_TRAINING

COMMAND_HINT = (
    "/plan  /training  /profile  /progress  /memory  /logs  /tokens  /clear  /help  /exit"
)


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

    def __init__(self, service, *, daily_plan_request=None):
        super().__init__()
        self.service = service
        self.busy = False
        self.last_basket_id = 0
        self.daily_plan_request = daily_plan_request

    def compose(self) -> ComposeResult:
        with Vertical(id="workspace"):
            yield Static("fitagent", id="brand")
            yield Static("", id="profile-bar", markup=False)
            yield RichLog(id="chat", min_width=1, wrap=True, markup=False)
            yield LogsPanel(self.service.store, id="logs-panel")
            yield ResponseLoader(id="response-loader")
            yield Static(
                COMMAND_HINT,
                id="status",
                markup=False,
            )
            selector = Select(
                [(value.capitalize(), value) for value in PROFILE_SEX_OPTIONS],
                prompt="Выберите пол",
                id="profile-sex",
            )
            selector.display = False
            yield selector
            yield Input(placeholder="Напишите сообщение…", id="question")
        yield Footer()

    def on_mount(self):
        try:
            self.service.start_chat()
            self.refresh_profile()
            self.refresh_sex_selection()
        except Exception:
            self.query_one("#chat", RichLog).write(
                "Не удалось подготовить профиль и опрос. Перезапустите приложение и проверьте доступ к папке данных."
            )
        self.restore_chat()
        self.set_interval(2, self.sync_baskets)
        self.set_interval(60, self.check_reminder)
        self.query_one("#question", Input).focus()
        self.show_daily_plan()

    @work(group="daily_plan", exclusive=True)
    async def show_daily_plan(self):
        if self.daily_plan_request is None:
            return
        chat = self.screen_stack[0].query_one("#chat", RichLog)
        error = None
        try:
            if not self.service.current_profile():
                return
            day = self.service.clock().date().isoformat()
            while (now := self.service.clock()).date().isoformat() == day:
                state = self.service.store.daily_plan_status(now)
                if state == "saved":
                    break
                if state == "missing":
                    try:
                        await self.daily_plan_request(self.service.store.directory)
                    except ValueError as exc:
                        if str(exc) != DAILY_PLAN_PENDING:
                            error = str(exc)
                            break
                    else:
                        break
                await asyncio.sleep(2)
        except Exception:
            error = DAILY_PLAN_ERROR
        if error:
            write_message(chat, "assistant", error)
        self.sync_baskets()

    def restore_chat(self):
        chat = self.query_one("#chat", RichLog)
        try:
            messages = self.service.store.chat_messages(now=self.service.clock())
        except Exception:
            chat.write(
                "Не удалось загрузить переписку. Перезапустите приложение; "
                "если ошибка повторяется, проверьте доступ к папке данных."
            )
            return
        if not messages:
            chat.write("Питание, тренировки и ваш прогресс. Начните с вопроса.")
        if messages:
            self.last_basket_id = messages[-1]["id"]
        for message in messages:
            write_message(
                chat,
                message["role"],
                message["content"],
                message["created_at"] if message["role"] == "user" else "",
                message["status"],
            )
            write_topic_notice(chat, message.get("topic_action"), message.get("topic_title"))

    def sync_baskets(self, local_answer=None):
        if (self.busy and local_answer is None) or len(self.screen_stack) != 1:
            return
        try:
            baskets = self.service.store.saved_baskets(
                self.last_basket_id, include_daily=True, now=self.service.clock()
            )
        except (OSError, RuntimeError, ValueError, sqlite3.Error):
            return
        for basket in baskets:
            if (
                basket["content"] != local_answer
                and basket_display(basket["content"], basket["status"]) != local_answer
            ):
                write_message(
                    self.query_one("#chat", RichLog),
                    "assistant",
                    basket["content"],
                    status=basket["status"],
                )
            self.last_basket_id = basket["id"]

    def refresh_profile(self, saved=None):
        profile = self.service.current_profile()
        goal = self.service.store.latest("goals")
        text = "Ответьте на вопросы в чате. Данные хранятся локально."
        if profile:
            weight = (
                f"{profile['weight']:g} кг" if profile["weight"] is not None else "вес не указан"
            )
            text = (
                f"{profile['height']:g} см · {weight} · {profile['age']} лет\n"
                f"Цель: {goal['text'] if goal else 'не задана'} · целевой вес: {goal['target'] or 'не задан' if goal else 'не задан'}"
            )
            state = self.service.workflow.state(goal) if goal else None
            if state:
                text += "\n" + LABELS[state["stage"]]
        self.query_one("#profile-bar", Static).update(text)

    def refresh_sex_selection(self):
        selector = self.query_one("#profile-sex", Select)
        state = self.service.onboarding.state()
        selector.display = bool(state and self.service.onboarding.next_field(state) == "sex")
        selector.value = Select.BLANK

    def on_select_changed(self, event: Select.Changed):
        if event.select.id == "profile-sex" and event.select.display and not self.busy:
            if event.value in PROFILE_SEX_OPTIONS:
                self.ask(event.value)

    def check_reminder(self):
        if not self.busy and self.service.current_profile() and self.service.due():
            self.query_one("#chat", RichLog).write(
                "Если режим или цель изменились, сообщите об этом в чате. /profile — просмотр."
            )
            self.service.defer()

    def action_profile(self):
        if not self.busy and len(self.screen_stack) == 1:
            log = self.query_one("#chat", RichLog)
            try:
                write_message(log, "assistant", self.service.profile_text())
            except Exception:
                log.write(
                    "Не удалось открыть профиль. Повторите попытку или проверьте базу данных."
                )

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
        self.workers.cancel_group(self, "daily_plan")
        self.last_basket_id = 0
        log.clear()
        log.write("Данные очищены. Начата новая сессия.")
        self.query_one("#question", Input).value = ""
        self.query_one("#response-loader", ResponseLoader).stop()
        self.query_one("#status", Static).update(COMMAND_HINT)
        self.refresh_profile()
        self.query_one("#logs-panel", LogsPanel).refresh_logs()
        prompt = self.service.start_chat()
        self.refresh_sex_selection()
        if prompt:
            write_message(log, "assistant", prompt)

    def action_tokens(self):
        if len(self.screen_stack) == 1:
            self.push_screen(TokensScreen(self.service.store))

    def memory_command(self, text):
        log = self.query_one("#chat", RichLog)
        try:
            self.service.memory.clock = self.service.clock
            log.write(self.service.memory.command(text))
        except ValueError as exc:
            log.write(str(exc))
        except Exception:
            log.write(
                "Не удалось открыть или изменить память. Повторите команду; проверьте доступ к папке данных."
            )
        log.scroll_end(animate=False, force=True)

    def wake_command(self, text):
        try:
            if not self.service.current_profile():
                raise ValueError("Укажите время подъёма в ходе опроса в чате.")
            wake_time = validate_wake_time(text)
            self.service.store.set_wake_time(wake_time)
            response = (
                f"Время подъёма — {wake_time}. Учту его в следующем плане дня."
                if wake_time
                else "Сохранено, что время подъёма меняется. Учту это в следующем плане дня."
            )
        except ValueError as exc:
            response = str(exc)
        except Exception:
            response = "Не удалось сохранить время подъёма. Проверьте доступ к папке данных."
        write_message(self.query_one("#chat", RichLog), "assistant", response)

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
        elif text == "/wake" or text.startswith("/wake "):
            self.wake_command(text[5:].strip())
        elif text == "/memory" or text.startswith("/memory "):
            self.memory_command(text)
        elif text == "/progress":
            log = self.query_one("#chat", RichLog)
            try:
                rows = self.service.store.weights()
            except Exception:
                log.write(
                    "Не удалось прочитать историю веса. Повторите попытку или проверьте базу данных."
                )
                return
            log.write("Динамика веса (последние 30 измерений):")
            for row in rows:
                log.write(f"{row['confirmed_at'][:10]}  {row['weight']:g} кг")
            if rows:
                log.write(f"Изменение: {rows[-1]['weight'] - rows[0]['weight']:+.1f} кг")
        elif text == "/help":
            self.query_one("#chat", RichLog).write(
                "/plan [условия] — составить программу питания через MCP FitAgent.\n"
                "/plan последняя корзина — показать сохранённую корзину.\n"
                "/training [условия] — составить программу тренировок; /training выход — закрыть согласование.\n"
                "«Подтверждаю программу тренировок» — сохранить показанный черновик в памяти.\n"
                "/wake ЧЧ:ММ или /wake нет — изменить время подъёма для будущих планов дня.\n"
                "/profile — профиль только для чтения; изменения сообщайте в чате. /progress — вес.\n"
                "/memory — факты; /memory edit НОМЕР текст — исправить; "
                "/memory delete НОМЕР — удалить обычный факт. Эти команды не обращаются к API.\n"
                "Ограничения, лекарства, добавки, измерения и принятые планы исправляйте в чате: замена требует подтверждения.\n"
                "Последнее сообщение имеет приоритет; новые редакции применяются сразу, даже с будущей датой начала.\n"
                "/memory history НОМЕР — журнал; /memory archive — архив целей.\n"
                "/confirm НОМЕР — подтвердить предложение; /cancel НОМЕР — отменить.\n"
                "Цель: планирование → валидация → выполнение → готово; назад только с выполнения на валидацию.\n"
                "«Подтверждаю план» — принять проверенный план и предупреждение. Опасность требует корректировки.\n"
                "При каждом открытии на этапе выполнения спрашиваем о прогрессе.\n"
                "/tokens — график накопленного расхода токенов; Escape — вернуться в чат.\n"
                "/exit или Ctrl+Q — закрыть приложение без удаления сохранённых данных.\n"
                "/logs или Ctrl+L — показать/скрыть нижнюю панель логов и токенов. "
                "Checkpoint в контексте, смена темы, чтение фактов, компакт истории, "
                "предупреждения и блокировки безопасности, ошибки проверки безопасности и памяти. "
                "Успешные проверки скрыты; тексты диалога и личные данные в ленту не попадают. "
                "Контекст и ответ — по статистике API после получения ответа; "
                "общий расход включает оценку цели, сводки и извлечение фактов.\n"
                "Номер факта постоянный. /memory history НОМЕР — его журнал. В /logs — «Факт №НОМЕР обновлён» или «Актуальность факта подтверждена». Без личных значений.\n"
                "Темы переключаются автоматически; при возврате используется checkpoint. "
                "Ручных команд переключения нет.\n"
                "/clear — без подтверждения удаляет профиль, цели, переписку, память, сводки и логи, "
                "начинает новую сессию. API-ключ остаётся.\n"
                "После отказа обновлять профиль напомним через сутки. "
                "Профиль и переписка передаются API Go. Для памяти и отдельной проверки безопасности "
                "используется DeepSeek Flash. Условия обработки данных: opencode.ai/docs/go/#privacy. "
                "Ответы не заменяют врача."
            )
        elif text in {"/plan", "/training"} or text.startswith(("/plan ", "/training ")):
            self.ask(text)
        elif text.startswith(("/confirm", "/cancel")):
            self.ask(text)
        elif text.startswith("/"):
            self.query_one("#chat", RichLog).write("Неизвестная команда. Доступные: /help")
        else:
            self.ask(text)

    @work(exclusive=True)
    async def ask(self, text):
        self.busy = True
        field = self.query_one("#question", Input)
        selector = self.query_one("#profile-sex", Select)
        field.disabled = True
        selector.disabled = True
        log = self.query_one("#chat", RichLog)
        write_message(log, "user", text, date.today().isoformat())
        status = self.query_one("#status", Static)
        status.update("Ctrl+L · журнал")
        loader = self.query_one("#response-loader", ResponseLoader)
        loader.start()
        was_training_paused = None
        try:
            had_profile = bool(self.service.current_profile())
            if had_profile:
                was_training_paused = self.service.store.training_paused()
            normal_turn = had_profile and not text.startswith("/")
            if text == "/plan" or text.startswith("/plan "):
                answer = await request_plan(self.service.store.directory, text[5:].strip())
            else:
                answer = await asyncio.to_thread(self.service.chat, text)
            try:
                notice = self.service.topics.repository.notice() if normal_turn else None
            except Exception:
                notice = None
                log.write(
                    "Ответ сохранён, но отметку темы показать не удалось. Перезапустите приложение."
                )
            if notice:
                write_topic_notice(log, notice["action"], notice["title"])
            self.sync_baskets(local_answer=answer)
            recall = text[5:].strip().casefold() == "последняя корзина" and text.startswith(
                "/plan "
            )
            write_message(
                log, "assistant", answer, status="basket_recall" if recall else "complete"
            )
            self.refresh_profile()
            self.refresh_sex_selection()
            if (not had_profile and self.service.current_profile()) or (
                had_profile and text.casefold() == ACCEPT_TRAINING
            ):
                self.show_daily_plan()
        except Exception as exc:
            # No traceback/request payload in UI or logs; audit only exception type.
            self.service.audit("ui_error", {"type": type(exc).__name__})
            log.write(
                str(exc)
                if isinstance(exc, ValueError)
                else "Не удалось обработать запрос. Повторите попытку или перезапустите приложение."
            )
            field.value = text
        finally:
            loader.stop()
            self.query_one("#logs-panel", LogsPanel).refresh_logs()
            status.update(COMMAND_HINT)
            self.busy = False
            field.disabled = False
            selector.value = Select.BLANK
            selector.disabled = False
            field.focus()
            if was_training_paused is False:
                try:
                    needs_refresh = self.service.store.training_paused()
                except (OSError, RuntimeError, ValueError, sqlite3.Error):
                    needs_refresh = False
                if needs_refresh:
                    self.show_daily_plan()
