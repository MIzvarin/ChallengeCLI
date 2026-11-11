"""Shared presentation of live and restored chat messages."""

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


def write_message(chat, role, content, created_at="", status="complete"):
    assistant = role == "assistant"
    title = "FitAgent" if assistant else "Вы"
    if created_at:
        title += f" · {created_at[:10]}"
    body = Markdown(content) if assistant else Text(content)
    notice = {
        "failed": "Ответ не получен. При необходимости отправьте вопрос ещё раз.",
        "pending": "Запрос не завершён. При необходимости отправьте вопрос ещё раз.",
    }.get(status)
    if notice:
        body = Group(body, Text(""), Text(notice, style="#929292"))
    chat.write(
        Panel(
            body,
            title=Text(title, style="bold #d4d4d4"),
            title_align="left",
            border_style="#686868" if assistant else "#484848",
            style="#d4d4d4",
            padding=(1, 2),
        ),
        expand=True,
    )
    chat.write("")
