"""The same pinned token counters for the audit panel and chart screen."""

from rich.text import Text
from textual.widgets import Static

from fitagent.tokens import usage_text


class TokenSummary(Static):
    DEFAULT_CSS = """
    TokenSummary { height: auto; min-height: 5; max-height: 6; overflow-y: auto;
                   border: round #686868; border-title-color: #e0e0e0;
                   border-title-style: bold; background: #202020; padding: 0 1;
                   color: #b8b8b8; }
    """

    def __init__(self, **kwargs):
        super().__init__("", markup=False, **kwargs)
        self.border_title = "Расход токенов"

    def show_report(self, report):
        summary = Text(usage_text(report), style="#b8b8b8")
        summary.highlight_words(("Токены", "За переписку"), style="bold #e8e8e8")
        summary.highlight_regex(r"\b\d[\d ]*\b", style="bold #f0f0f0")
        summary.highlight_regex(r"всего: [^\n]+", style="bold #f0f0f0")
        summary.highlight_words(("нет данных",), style="italic #a0a0a0")
        self.update(summary)
