"""Resumable one-question-at-a-time chat questionnaire; no TUI dependencies."""

import json

from fitagent.domain import profile_number, validate_birth_date, validate_profile
from fitagent.memory import project

QUESTIONS = (
    (
        "birth_date",
        "Укажите дату рождения: ГГГГ-ММ-ДД. FitAgent предназначен для взрослых 18–120 лет.",
    ),
    ("height", "Какой у вас рост в сантиметрах? Например: 180."),
    ("weight", "Какой у вас текущий вес в килограммах? Например: 80.5."),
    ("sex", "Укажите пол для оценки параметров питания и тренировок."),
    ("goal", "Какова ваша цель? Опишите желаемый результат и срок, если он есть."),
    ("target", "Какой целевой вес в килограммах? Если он не задан, напишите «нет»."),
    (
        "allergies",
        "Есть ли пищевые аллергии или непереносимости? Перечислите их или напишите «нет».",
    ),
    (
        "preferences",
        "Какие продукты вы предпочитаете или исключаете по личному выбору? Опишите или напишите «нет».",
    ),
    (
        "routine",
        "Есть ли привычный режим питания? Опишите частоту, время и особенности или напишите «нет».",
    ),
    (
        "supplements",
        "Какие добавки вы уже принимаете? Укажите названия, известные дозы и частоту или напишите «нет».",
    ),
    (
        "medications",
        "Какие лекарства вы уже принимаете? Укажите названия, известные дозы и частоту или напишите «нет».",
    ),
    (
        "training",
        "Есть ли программа тренировок? Опишите упражнения, частоту и нагрузку или напишите «нет».",
    ),
)
FACT_FIELDS = {
    "allergies": ("food_restrictions", "reported_allergies", "Аллергии и непереносимости"),
    "preferences": ("food_preferences", "reported_preferences", "Пищевые предпочтения"),
    "routine": ("meal_routine", "reported_routine", "Привычный режим питания"),
    "supplements": ("supplements", "reported_regimen", "Принимаемые добавки"),
    "medications": ("medications", "reported_regimen", "Принимаемые лекарства"),
    "training": ("training_routine", "reported_program", "Текущая программа тренировок"),
}
FIELD_NAMES = {
    "дата рождения": "birth_date",
    "рост": "height",
    "вес": "weight",
    "пол": "sex",
    "цель": "goal",
    "целевой вес": "target",
    "аллергии": "allergies",
    "предпочтения": "preferences",
    "режим питания": "routine",
    "добавки": "supplements",
    "лекарства": "medications",
    "тренировки": "training",
}


class ChatOnboarding:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def state(self):
        with self.store.connect() as db:
            row = db.execute("SELECT value FROM chat_state WHERE key='onboarding'").fetchone()
        return json.loads(row[0]) if row else None

    def write_state(self, state):
        with self.store.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO chat_state VALUES ('onboarding',?)",
                (json.dumps(state, ensure_ascii=False),),
            )

    def prompt(self, state):
        for name, question in QUESTIONS:
            if name not in state["answers"]:
                return question
        return "Ответы собраны. Напишите «сохранить», чтобы повторить проверку. Для исправления: «цель: ...», «аллергии: ...», «лекарства: ...» или название другого поля и новый ответ."

    def start(self):
        if self.store.latest("profiles"):
            return None
        state = self.state()
        if state is None:
            state = {"answers": {}, "sources": {}}
            self.write_state(state)
            return (
                "Давайте заполним профиль и короткий опрос в чате — по одному вопросу. "
                "Сведения хранятся локально; для ответов и проверки безопасности передаются API Go. "
                "Неизвестные дозировки не додумываем. Ответы не заменяют врача.\n\n"
                + self.prompt(state)
            )
        return self.prompt(state)

    def validate_answer(self, name, text):
        if not text or len(text) > (4000 if name == "goal" else 500):
            raise ValueError(
                "Введите непустой ответ: до 4000 символов для цели, до 500 для остальных полей."
            )
        if name == "birth_date":
            return validate_birth_date(text, self.service.clock().date())
        if name in {"height", "weight", "target"}:
            if name == "target" and text.casefold() == "нет":
                return None
            label = {"height": "Рост", "weight": "Вес", "target": "Целевой вес"}[name]
            value = profile_number(text, label)
            low, high = (100, 250) if name == "height" else (25, 400)
            if not low <= value <= high:
                raise ValueError(f"{label}: допустимый диапазон от {low} до {high}.")
            return value
        if name == "sex" and len(text) > 100:
            raise ValueError("Пол: введите до 100 символов.")
        if name in FACT_FIELDS and text.casefold().strip(" .!?") in {"да", "не знаю", "пропустить"}:
            raise ValueError(
                "Сразу опишите подробности или напишите «нет». Для препаратов достаточно названия; неизвестную дозу не придумывайте."
            )
        return text

    def candidate(self, state):
        memory = self.service.memory.repository.load(self.service.clock())
        combined = {"journal": [], "sources": [], "retired": [], "notifications": []}
        for field, (category, key, label) in FACT_FIELDS.items():
            source = state["sources"][field]
            text = state["answers"][field]
            operation = {
                "action": "upsert",
                "update_mode": "replace",
                "fact_id": None,
                "category": category,
                "key": key,
                "scope": "usual",
                "applicability": "global",
                "topic_id": None,
                "value": {
                    "text": label + ": " + text,
                    "quantity": None,
                    "unit": None,
                    "qualifier": "Со слов пользователя; неуказанные подробности неизвестны",
                },
                "valid_from": None,
                "valid_until": None,
                "valid_until_action": "keep",
                "source_quote": text,
                "clarification": None,
            }
            memory = project(memory, [operation], source, self.service.clock())
            for name in combined:
                combined[name].extend(memory[name])
        memory.update(combined)
        memory["weight_source_message_id"] = state["sources"]["weight"]
        return memory

    def respond(self, text):
        state = self.state()
        if state is None:
            self.start()
            state = self.state()
        answers = state["answers"]
        original = text
        field = text.split(":", 1)[0].strip().casefold()
        if ":" in text and field in FIELD_NAMES:
            name, text = FIELD_NAMES[field], text.split(":", 1)[1].strip()
            state.pop("warning", None)
            state.pop("warning_shown", None)
        else:
            name = next((key for key, _ in QUESTIONS if key not in answers), None)
        risk_confirmed = (
            name is None and text.casefold() == "подтверждаю" and state.get("warning_shown", False)
        )
        if name is not None:
            value = self.validate_answer(name, text)
            source = self.store.message("user", original, self.service.clock(), "onboarding")
            answers[name] = value
            state["sources"][name] = source
            self.write_state(state)
        elif text.casefold() not in {"сохранить", "подтверждаю"}:
            return self.prompt(state)
        if any(key not in answers for key, _ in QUESTIONS):
            return self.prompt(state)
        profile = validate_profile(
            **{
                key: answers[key]
                for key in ("height", "weight", "sex", "birth_date", "goal", "target")
            },
            now=self.service.clock(),
        )
        memory = self.candidate(state)
        result = self.service.safety.check("onboarding", answers["goal"], memory, profile)
        warning = state.get("warning")
        if warning is None:
            warning = self.service.assess_goal(profile, safety_result=result, memory=memory)
            state["warning"] = warning
            self.write_state(state)
        else:
            checked_warning = self.service.checked_goal_warning(profile, warning, memory=memory)
            if checked_warning != warning:
                warning = checked_warning
                state["warning"] = warning
                self.write_state(state)
        if warning and not risk_confirmed:
            state["warning_shown"] = True
            self.write_state(state)
            return (
                warning
                + "\nПодтверждение не делает цель безопасной. Напишите «подтверждаю» или «цель: новая цель»."
            )
        self.store.save_profile(
            profile,
            self.service.clock(),
            self.service.next_check(),
            warning,
            bool(warning),
            memory=memory,
            setup_done=True,
        )
        notice = (
            "\nПредупреждение безопасности: " + result["description"]
            if result["status"] == "warning"
            else ""
        )
        if warning:
            notice += "\nПроверка цели: " + warning
        return (
            "Профиль и ответы сохранены. Память обновлена: ограничения, предпочтения, режим, добавки, лекарства, тренировки и измерение веса. /profile — просмотр. Теперь можно задать вопрос."
            + notice
        )
