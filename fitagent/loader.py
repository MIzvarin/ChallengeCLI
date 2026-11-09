"""Quiet monochrome animation while a model request is in flight."""

from time import monotonic

from rich.text import Text
from textual.widgets import Static


class ResponseLoader(Static):
    """Braille spinner, soft text highlight and actual elapsed waiting time."""

    DEFAULT_CSS = """
    ResponseLoader { display: none; height: 1; margin-bottom: 1; background: transparent; }
    """
    FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.frame_index = 0
        self.started_at = None
        self.animation_timer = None

    def on_mount(self):
        self.animation_timer = self.set_interval(0.08, self.advance, pause=True)

    def start(self):
        self.frame_index = 0
        self.started_at = monotonic()
        self.display = True
        self.animation_timer.resume()
        self.refresh()

    def stop(self):
        self.animation_timer.pause()
        self.started_at = None
        self.display = False

    def advance(self):
        self.frame_index += 1
        self.refresh()

    def render(self):
        if self.started_at is None:
            return Text()
        elapsed = max(0, int(monotonic() - self.started_at))
        label = Text("Готовлю ответ", style="#888888")
        highlight = (self.frame_index // 2) % (len(label) + 4) - 3
        if highlight + 3 > 0 and highlight < len(label):
            label.stylize("#d4d4d4", max(0, highlight), min(len(label), highlight + 3))
        return Text.assemble(
            (self.FRAMES[self.frame_index % len(self.FRAMES)], "#bdbdbd"),
            "  ",
            label,
            (f"  ·  {elapsed} с", "#808080"),
        )
