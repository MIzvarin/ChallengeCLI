"""Width-bounded, dependency-free terminal chart of cumulative API usage."""

from rich.text import Text


def cumulative_chart(points, width, height):
    if not points:
        return Text("Обращений к API пока нет. График появится после первого обращения.")
    known = [point["total_tokens"] for point in points if point["total_tokens"] is not None]
    if not known:
        return Text(
            "API не сообщил расход токенов. Для графика нужна статистика хотя бы одного обращения."
        )
    peak = max(1, max(known))
    label_width = len(f"{peak:,}".replace(",", " "))
    plot_width, plot_height = width - label_width - 2, height - 2
    if plot_width < 8 or plot_height < 3:
        return Text("Увеличьте окно терминала для отображения графика.")

    count = points[-1]["request"]
    # Keep the last cumulative value per column, plus any missing-usage marker.
    # Column zero is reserved for the origin, even in very long conversations.
    buckets = {0: {"total_tokens": 0, "incomplete": False}}
    for point in points:
        x = max(1, point["request"] * (plot_width - 1) // count)
        incomplete = point["incomplete"] or buckets.get(x, {}).get("incomplete", False)
        buckets[x] = {"total_tokens": point["total_tokens"], "incomplete": incomplete}

    grid = [[" " for _ in range(plot_width)] for _ in range(plot_height)]
    markers = []
    previous_x, previous_y = 0, plot_height - 1
    for x, point in buckets.items():
        value = point["total_tokens"] or 0  # Unknown-only prefixes are marked ?, not real zeroes.
        y = plot_height - 1 - value * (plot_height - 1) // peak
        for column in range(previous_x, x + 1):
            grid[previous_y][column] = "─"
        for row in range(min(previous_y, y), max(previous_y, y) + 1):
            grid[row][x] = "│"
        markers.append((x, y, "?" if point["incomplete"] else "●"))
        previous_x, previous_y = x, y
    for x, y, marker in markers:
        grid[y][x] = marker

    ticks = {0, plot_height // 2, plot_height - 1}
    lines = []
    for row, cells in enumerate(grid):
        value = peak * (plot_height - 1 - row) // (plot_height - 1)
        label = f"{value:,}".replace(",", " ") if row in ticks else ""
        lines.append(f"{label:>{label_width}} │" + "".join(cells))
    lines.append(" " * (label_width + 1) + "└" + "─" * plot_width)
    last = str(count)
    lines.append(" " * (label_width + 2) + "0" + " " * (plot_width - len(last) - 1) + last)
    return Text("\n".join(lines), style="#d0d0d0", no_wrap=True, overflow="crop")
