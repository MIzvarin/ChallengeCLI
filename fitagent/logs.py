"""An inline audit panel that leaves the chat and its input available."""

import re

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import RichLog

from fitagent.token_summary import TokenSummary


class LogsPanel(Vertical):
    DEFAULT_CSS = """
    LogsPanel { display: none; height: 45%; min-height: 10; max-height: 18;
                border-top: solid #505050; border-title-color: #c0c0c0; margin-top: 1; }
    #events { height: 1fr; min-height: 3; border-top: solid #505050;
              border-title-color: #929292; scrollbar-gutter: stable; }
    """

    def __init__(self, store, **kwargs):
        super().__init__(**kwargs)
        self.store = store
        self.last_logs = None
        self.border_title = "Журнал · /logs или Ctrl+L — скрыть"

    def compose(self) -> ComposeResult:
        yield TokenSummary(id="token-counts")
        events = RichLog(id="events", min_width=1, wrap=True, markup=False)
        events.border_title = "Лента событий"
        yield events

    def on_mount(self):
        self.set_interval(2, self.refresh_logs)

    def refresh_logs(self):
        if not self.display:
            return
        counts = self.query_one("#token-counts", TokenSummary)
        try:
            text = self.store.logs() or "Событий пока нет."
            report = self.store.token_usage()
        except Exception:
            counts.update(
                "Не удалось загрузить журнал и расход токенов. "
                "Откройте панель снова; проверьте доступ к папке данных."
            )
            return
        counts.show_report(report)
        if text != self.last_logs:
            log = self.query_one("#events", RichLog)
            events = Text(text)
            for match in re.finditer(
                r"^\S+  (summary_started|summary_finished|summary_cache_hit)  ",
                text,
                re.MULTILINE,
            ):
                events.stylize("blue", match.start(1), match.end(1))
            log.clear()
            log.write(events)
            self.last_logs = text
