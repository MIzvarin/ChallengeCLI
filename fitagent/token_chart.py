"""A modal cumulative token chart with the same pinned summary as /logs."""

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from fitagent.chart import cumulative_chart
from fitagent.token_summary import TokenSummary


class TokenChart(Static):
    DEFAULT_CSS = """
    TokenChart { height: 1fr; min-height: 7; border: round #484848;
                 border-title-color: #b8b8b8; }
    """

    def __init__(self, **kwargs):
        super().__init__("", markup=False, **kwargs)
        self.points = []
        self.message = ""
        self.border_title = "Накопленный расход · известная сумма"

    def set_history(self, points, message=""):
        self.points, self.message = points, message
        self.refresh()

    def render(self):
        if self.message:
            return Text(self.message)
        return cumulative_chart(self.points, self.size.width, self.size.height)


class TokensScreen(ModalScreen):
    BINDINGS = [("escape", "close", "Назад")]
    DEFAULT_CSS = """
    #token-dialog { width: 96%; max-width: 130; height: 94%; padding: 0 1;
                    background: #191919; }
    #tokens-title { height: 1; text-style: bold; color: #e0e0e0; }
    #chart-note { height: auto; max-height: 3; overflow-y: auto; color: #929292; }
    #close-tokens { height: 3; }
    """

    def __init__(self, store):
        super().__init__()
        self.store = store

    def compose(self) -> ComposeResult:
        with Vertical(id="token-dialog"):
            yield Static("Токены за переписку", id="tokens-title", markup=False)
            yield TokenSummary(id="chart-token-counts")
            yield TokenChart(id="token-chart")
            yield Static(
                "X — номер обращения к API; Y — накоплено токенов.\n"
                "? — неполные данные; сумма только по известным значениям.\n"
                "Длинная история сгруппирована по ширине окна.",
                id="chart-note",
                markup=False,
            )
            yield Button("Закрыть · Esc", id="close-tokens")

    def on_mount(self):
        self.refresh_usage()
        self.set_interval(2, self.refresh_usage)

    def refresh_usage(self):
        counts = self.query_one(TokenSummary)
        chart = self.query_one(TokenChart)
        try:
            report = self.store.token_usage(include_history=True)
        except Exception:
            counts.update(
                "Не удалось загрузить расход токенов. "
                "Откройте /tokens снова; проверьте доступ к папке данных."
            )
            chart.set_history([], "График недоступен. Попробуйте открыть /tokens снова.")
            return
        counts.show_report(report)
        chart.set_history(report["history"])

    def action_close(self):
        self.dismiss()

    def on_button_pressed(self, event):
        if event.button.id == "close-tokens":
            self.action_close()
