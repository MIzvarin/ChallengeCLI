"""Shared presentation of live and restored chat messages."""

from rich.console import Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.text import Text


def write_topic_notice(chat, action, title):
    label = {
        "switch": "Новая тема",
        "resume": "Возвращаемся к теме",
        "detour": "Короткое отступление",
        "mixed": "Связанные темы",
    }.get(action)
    if label and title:
        chat.write(Text(f"↳ {label}: {title}", style="#929292"))


def basket_display(content):
    """Remove the advisory appendix from previously saved shopping outcomes."""
    if not (
        "Корзина ВкусВилла: <https://vkusvill.ru/" in content
        or content.startswith(("Не удалось подобрать рацион,", "Для подбора рациона под вашу цель"))
    ):
        return content
    result, marker, appendix = content.partition("\n\nПредупреждение безопасности: ")
    if marker:
        _, memory, updates = appendix.partition("\n\nПамять обновлена:")
        return result + memory + updates
    return content


def write_message(chat, role, content, created_at="", status="complete"):
    assistant = role == "assistant"
    title = "FitAgent" if assistant else "Вы"
    if created_at:
        title += f" · {created_at[:10]}"
    body = Markdown(basket_display(content)) if assistant else Text(content)
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
