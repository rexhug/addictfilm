"""Canonical, versioned graph for the deterministic recommendation quiz.

The browser never receives scoring tags.  It only renders the current localized
question and returns stable question/answer IDs; this module remains the one
source of truth for routing and scoring semantics.
"""
from __future__ import annotations

QUESTION_VERSION = "quiz-v2"


def _text(ru: str, en: str) -> dict[str, str]:
    return {"ru": ru, "en": en}


def _option(id_: str, ru: str, en: str, *, weights=None, filters=None, context=None, reason=None):
    return {
        "id": id_, "label": _text(ru, en), "weights": weights or {},
        "filters": filters or {}, "context": context or {}, "reason": reason,
    }


# Question payloads are deliberately data-only.  The optional ``route`` value
# is consumed by next_question_id below and never trusted from a client.
QUESTIONS: dict[str, dict] = {
    "q1": {
        "id": "q1", "branch": "start", "answer_type": "single", "required": True,
        "text": _text("Что тебе сейчас больше всего хочется?", "What do you most want right now?"),
        "options": [
            _option("relax", "Отключить голову и расслабиться", "Switch off and relax", weights={"comfort": 4, "complexity_low": 3}, reason="comfort"),
            _option("tension", "Почувствовать напряжение", "Feel some tension", weights={"tension": 4}, reason="tension"),
            _option("humor", "Посмеяться", "Have a laugh", weights={"humor": 4}, reason="humor"),
            _option("emotion", "Сильно прочувствовать историю", "Feel a story deeply", weights={"emotion": 4}, reason="emotion"),
            _option("unusual", "Увидеть что-то необычное", "See something unusual", weights={"originality": 4, "risk": 3}, reason="unusual"),
        ],
    },
    "r1": {"id": "r1", "branch": "relax", "answer_type": "single", "text": _text("Как именно ты хочешь отдохнуть?", "How would you like to unwind?"), "options": [
        _option("warm", "Уютная и добрая история", "A warm, kind story", weights={"warm": 4, "low_tension": 3}, reason="warm"),
        _option("adventure", "Лёгкое приключение", "A light adventure", weights={"adventure": 3, "medium_pace": 2}, reason="adventure"),
        _option("visual", "Красивый фильм с атмосферой", "A beautiful, atmospheric movie", weights={"visuals": 4, "slow_pace": 2}, reason="atmosphere"),
        _option("simple", "Что-то весёлое и простое", "Something fun and easy", weights={"light_humor": 4, "complexity_low": 3}, reason="light humor"),
    ]},
    "r2": {"id": "r2", "branch": "relax", "answer_type": "single", "text": _text("В каком темпе смотрим?", "What pace are you after?"), "options": [
        _option("fast", "Пусть события начнутся сразу", "Let it start right away", weights={"fast_pace": 4}, reason="fast pace"),
        _option("medium", "Нормальный темп, без спешки", "A steady pace, no rush", weights={"medium_pace": 4}, reason="steady pace"),
        _option("slow", "Хочу медленно погрузиться в атмосферу", "Let me slowly sink into the atmosphere", weights={"slow_pace": 4, "visuals": 2}, reason="atmosphere"),
    ]},
    "r3": {"id": "r3", "branch": "relax", "answer_type": "single", "text": _text("Сколько конфликта ты сегодня выдержишь?", "How much conflict are you up for today?"), "options": [
        _option("comfort", "Почти никакой — мне нужен комфорт", "Almost none — I need comfort", weights={"low_conflict": 5}, reason="comfort"),
        _option("positive", "Небольшие проблемы, но всё будет хорошо", "Small problems, but it should turn out well", weights={"positive": 3}, reason="a hopeful tone"),
        _option("drama", "Можно немного драмы", "A little drama is fine", weights={"drama": 3}, reason="some drama"),
    ]},
    "t1": {"id": "t1", "branch": "tension", "answer_type": "single", "text": _text("Что должно держать тебя в напряжении?", "What should keep you on edge?"), "options": [
        _option("survival", "Опасность, погони и выживание", "Danger, chases and survival", weights={"action": 4, "survival": 4, "fast_pace": 3}, reason="danger and survival"),
        _option("psychological", "Психологическая игра", "A psychological game", weights={"psychological": 5, "complexity": 3}, reason="psychological tension"),
        _option("mystery", "Тайна, которую хочется разгадать", "A mystery to solve", weights={"mystery": 5, "detective": 3}, reason="a mystery"),
        _option("horror", "Страх и неизвестность", "Fear and the unknown", weights={"horror": 5, "supernatural": 2}, reason="fear of the unknown"),
    ]},
    "t2_psychological": {"id": "t2_psychological", "branch": "tension", "answer_type": "single", "text": _text("Кому меньше всего хочется доверять?", "Who should you trust the least?"), "options": [
        _option("hero", "Главному герою", "The main character", weights={"unreliable_narrator": 5}, reason="an unreliable hero"),
        _option("close", "Его близким", "The people closest to them", weights={"betrayal": 4, "relationship_tension": 3}, reason="relationship tension"),
        _option("everyone", "Всем персонажам", "Everyone", weights={"paranoia": 5}, reason="paranoia"),
        _option("eyes", "Даже собственным глазам", "Even your own eyes", weights={"surreal": 4, "mind_bending": 5}, reason="a mind-bending angle"),
    ]},
    "t2_mystery": {"id": "t2_mystery", "branch": "tension", "answer_type": "single", "text": _text("Какую загадку интереснее разгадывать?", "Which mystery sounds most compelling?"), "options": [
        _option("crime", "Преступление", "A crime", weights={"crime": 5, "detective": 4}, reason="a crime mystery"),
        _option("missing", "Исчезновение человека", "A missing person", weights={"missing_person": 5}, reason="a disappearance"),
        _option("past", "Секрет прошлого", "A secret from the past", weights={"past_secret": 5, "drama": 2}, reason="a secret from the past"),
        _option("unexplained", "Нечто необъяснимое", "Something unexplained", weights={"supernatural": 4, "mystery": 4}, reason="the unexplained"),
    ]},
    "t2_survival": {"id": "t2_survival", "branch": "tension", "answer_type": "single", "text": _text("Против чего должен бороться герой?", "What should the hero fight against?"), "options": [
        _option("people", "Других людей", "Other people", weights={"crime": 3, "thriller": 4}, reason="human danger"),
        _option("nature", "Природы или катастрофы", "Nature or disaster", weights={"survival": 5, "disaster": 4}, reason="survival"),
        _option("system", "Системы", "The system", weights={"dystopia": 5, "conspiracy": 3}, reason="a system to fight"),
        _option("self", "Самого себя", "Themselves", weights={"psychological": 5, "drama": 3}, reason="an internal struggle"),
    ]},
    "t2_horror": {"id": "t2_horror", "branch": "tension", "answer_type": "single", "text": _text("Какой страх тебе интереснее?", "What kind of fear works best for you?"), "options": [
        _option("unknown", "Неизвестность", "The unknown", weights={"supernatural": 4, "mystery": 4}, reason="the unknown"),
        _option("mind", "Собственный разум", "The mind", weights={"psychological": 5, "mind_bending": 3}, reason="psychological fear"),
        _option("threat", "Реальная опасность", "A real threat", weights={"survival": 4, "fast_pace": 2}, reason="real danger"),
    ]},
    "t4": {"id": "t4", "branch": "tension", "answer_type": "single", "text": _text("Финал должен всё объяснить?", "Should the ending explain everything?"), "options": [
        _option("clear", "Да, хочу получить ответы", "Yes, I want answers", weights={"clear_ending": 5}, reason="a clear resolution"),
        _option("semi", "Можно оставить несколько вопросов", "A few open questions are okay", weights={"semi_open": 4}, reason="some ambiguity"),
        _option("open", "Пусть останется загадкой", "Let it stay a puzzle", weights={"open_ending": 5, "complexity": 3}, reason="an open ending"),
    ]},
    "h1": {"id": "h1", "branch": "humor", "answer_type": "single", "text": _text("Над чем тебе хочется смеяться?", "What do you feel like laughing at?"), "options": [
        _option("situational", "Неловкие жизненные ситуации", "Awkward life situations", weights={"situational_humor": 5, "comedy": 3}, reason="situational humor"),
        _option("sarcastic", "Острые диалоги и сарказм", "Sharp dialogue and sarcasm", weights={"sarcastic_humor": 5}, reason="sharp humor"),
        _option("absurd", "Абсурд и полный хаос", "Absurdity and complete chaos", weights={"absurd": 5}, reason="absurd humor"),
        _option("dark", "Чёрный юмор", "Dark humor", weights={"dark_humor": 5}, reason="dark humor"),
        _option("warm", "Добрые и смешные персонажи", "Kind, funny characters", weights={"warm_humor": 5}, reason="warm humor"),
    ]},
    "h2": {"id": "h2", "branch": "humor", "answer_type": "single", "text": _text("Насколько серьёзным может быть фильм между шутками?", "How serious can it get between the jokes?"), "options": [
        _option("none", "Вообще не нужен серьёзный смысл", "No serious meaning needed", weights={"complexity_low": 4, "emotion_low": 3}, reason="an easy watch"),
        _option("soul", "Немного душевности не помешает", "A little heart is welcome", weights={"emotion": 4}, reason="some heart"),
        _option("satire", "Хочу смеяться над серьёзными вещами", "I want to laugh at serious things", weights={"satire": 5, "dark_comedy": 4}, reason="satire"),
    ]},
    # Прежний h3 спрашивал про архетип героя (неудачник / самоуверенный идиот /
    # компания друзей / циник). Ни один из этих признаков не выводится из
    # доступных метаданных, поэтому все четыре варианта не влияли на выдачу:
    # человек выбирал разное и получал одно и то же. Заменено на темп — его
    # каталог измеряет надёжно.
    "h3": {"id": "h3", "branch": "humor", "answer_type": "single", "text": _text("В каком темпе смотрим?", "What pace are you after?"), "options": [
        _option("calm", "Спокойно, без суеты", "Calm, unhurried", weights={"slow_pace": 4}, reason="a calm pace"),
        _option("steady", "Ровно, но не скучно", "Steady, but not slow", weights={"medium_pace": 4}, reason="a steady pace"),
        _option("wild", "Бешеный ритм и хаос", "Frantic rhythm and chaos", weights={"fast_pace": 5, "absurd": 3}, reason="a frantic pace"),
    ]},
    "e1": {"id": "e1", "branch": "emotion", "answer_type": "single", "text": _text("Что должно задеть тебя сильнее?", "What should move you most?"), "options": [
        _option("romance", "Любовь и отношения", "Love and relationships", weights={"romance": 4, "relationships": 5}, reason="relationships"),
        _option("family", "Семья", "Family", weights={"family": 5}, reason="family"),
        _option("friendship", "Дружба", "Friendship", weights={"friendship": 5}, reason="friendship"),
        _option("dream", "Мечта и борьба за неё", "A dream worth fighting for", weights={"inspiration": 5}, reason="an inspiring story"),
        _option("loss", "Потеря и сложный выбор", "Loss and a difficult choice", weights={"grief": 4, "moral_choice": 5}, reason="a difficult choice"),
    ]},
    "e2": {"id": "e2", "branch": "emotion", "answer_type": "single", "text": _text("Насколько больно может быть в финале?", "How painful can the ending be?"), "options": [
        _option("positive", "Сегодня нужен хороший конец", "I need a good ending today", weights={"positive": 5}, reason="a hopeful tone"),
        _option("bittersweet", "Можно немного погрустить", "A little sadness is okay", weights={"bittersweet": 5}, reason="bittersweet emotion"),
        _option("tragic", "Можно тяжёлый финал", "I'm ready to feel it deeply", weights={"tragic": 5, "emotion": 3}, reason="emotional intensity"),
    ]},
    "e3": {"id": "e3", "branch": "emotion", "answer_type": "single", "text": _text("Что важнее: события или люди?", "What matters more: events or people?"), "options": [
        _option("characters", "Персонажи и их отношения", "Characters and their relationships", weights={"character_driven": 5}, reason="character relationships"),
        _option("plot", "История и большие события", "Story and big events", weights={"plot_driven": 5}, reason="a strong plot"),
        _option("balance", "Баланс", "A balance", weights={"character_driven": 3, "plot_driven": 3}, reason="a balanced story"),
    ]},
    "u1": {"id": "u1", "branch": "unusual", "answer_type": "single", "text": _text("Что именно должно быть необычным?", "What should be unusual about it?"), "options": [
        _option("visual", "Визуальный мир", "The visual world", weights={"visuals": 5, "fantasy": 2}, reason="a distinctive visual world"),
        _option("idea", "Идея фильма", "The idea", weights={"high_concept": 5}, reason="a high-concept idea"),
        _option("telling", "Способ рассказа", "The way it is told", weights={"nonlinear": 5, "experimental": 3}, reason="an unusual narrative"),
        _option("characters", "Странные персонажи", "Strange characters", weights={"eccentric": 5}, reason="unusual characters"),
        _option("reality", "Ощущение, что реальность сломалась", "Reality feels broken", weights={"surreal": 5, "mind_bending": 5}, reason="a reality-bending premise"),
    ]},
    "u2": {"id": "u2", "branch": "unusual", "answer_type": "single", "text": _text("Насколько далеко можно зайти в странность?", "How far can the weirdness go?"), "options": [
        _option("low", "Немного необычно, но понятно", "A little unusual, but clear", weights={"experimental_low": 4}, reason="accessible originality"),
        _option("medium", "Хочу удивляться, но понимать сюжет", "Surprise me, but keep it understandable", weights={"experimental_medium": 4}, reason="understandable originality"),
        _option("high", "Чем страннее, тем лучше", "The stranger, the better", weights={"experimental": 5}, reason="bold originality"),
    ]},
    "u3": {"id": "u3", "branch": "unusual", "answer_type": "single", "text": _text("Что тебе ближе?", "Which feels closer to you?"), "options": [
        _option("dream", "Красивый сон", "A beautiful dream", weights={"dreamlike": 5, "visuals": 3}, reason="dreamlike visuals"),
        _option("nightmare", "Тревожный кошмар", "An anxious nightmare", weights={"nightmare": 5, "tension": 3}, reason="an anxious mood"),
        _option("puzzle", "Интеллектуальная головоломка", "An intellectual puzzle", weights={"complexity": 5, "puzzle": 4}, reason="a puzzle"),
        _option("adventure", "Безумное приключение", "A wild adventure", weights={"fast_pace": 3, "absurd": 4}, reason="a wild adventure"),
    ]},
    "c1": {"id": "c1", "branch": "common", "answer_type": "single", "text": _text("Сколько времени у тебя есть?", "How much time do you have?"), "options": [
        _option("short", "До 90 минут", "Up to 90 minutes", filters={"runtime_max": 100}),
        _option("two_hours", "До двух часов", "Up to two hours", filters={"runtime_max": 125}),
        _option("long", "Можно длинный фильм", "A longer movie is fine", filters={"runtime_max": 180}),
        _option("any", "Время не важно", "Time does not matter"),
    ]},
    "c2": {"id": "c2", "branch": "common", "answer_type": "single", "text": _text("Насколько новый фильм ты хочешь?", "How new should the movie be?"), "options": [
        _option("modern", "Только что-то современное", "Something modern", filters={"year_min": 2018}),
        _option("any", "Неважно, главное — хороший", "Any year, as long as it is good"),
        _option("classic", "Хочу проверенную классику", "A proven classic", filters={"year_max": 2005}),
    ]},
    "c3": {"id": "c3", "branch": "common", "answer_type": "single", "text": _text("Как сегодня выбираем?", "How should we choose today?"), "options": [
        _option("safe", "Надёжный вариант с высоким рейтингом", "A reliable, highly rated choice", weights={"risk_low": 5, "rating_quality": 3}, context={"risk": "low"}),
        _option("less_known", "Что-то не слишком известное", "Something not too well known", weights={"less_known": 4}, context={"risk": "medium"}),
        _option("surprise", "Удиви меня", "Surprise me", weights={"risk": 5, "diversity": 3}, context={"risk": "high"}),
    ]},
    "c4": {"id": "c4", "branch": "common", "answer_type": "single", "text": _text("С кем смотришь?", "Who are you watching with?"), "options": [
        _option("solo", "В одиночку", "On my own", context={"watch_context": "solo"}),
        _option("pair", "С партнёром", "With a partner", context={"watch_context": "pair"}),
        _option("group", "С друзьями", "With friends", weights={"group": 4, "fast_pace": 1}, context={"watch_context": "group"}),
    ]},
}

BRANCH_STEPS = {
    "relax": ["r1", "r2", "r3"], "tension": ["t1", "__t2__", "t4"],
    "humor": ["h1", "h2", "h3"], "emotion": ["e1", "e2", "e3"],
    "unusual": ["u1", "u2", "u3"],
}
COMMON_STEPS = ["c1", "c2", "c3", "c4"]


def _branch_from_answers(answers: dict[str, str]) -> str | None:
    return answers.get("q1")


def next_question_id(answers: dict[str, str]) -> str | None:
    """Return the next question from server-side answers, capped at eight.

    q1 + exactly three branch questions + C1-C4 = eight screens.  The tension
    follow-up is selected only after the answer to T1 is known.
    """
    if "q1" not in answers:
        return "q1"
    branch = _branch_from_answers(answers)
    if branch not in BRANCH_STEPS:
        return None
    for step in BRANCH_STEPS[branch]:
        qid = f"t2_{answers.get('t1')}" if step == "__t2__" else step
        if qid not in QUESTIONS:
            # Раньше здесь стояла молчаливая подмена на t2_horror: человек
            # выбирал «психологическая игра», а получал вопрос про страх, и
            # заметить это было нечем. Отсутствие продолжения — ошибка
            # конфигурации, её ловит validate_question_graph() в тестах.
            raise KeyError(f"нет специализированного продолжения ветки: {qid}")
        if qid not in answers:
            return qid
    for qid in COMMON_STEPS:
        if qid not in answers:
            return qid
    return None


def public_question(question_id: str, language: str) -> dict:
    q = QUESTIONS[question_id]
    locale = "en" if language == "en" else "ru"
    return {
        "id": q["id"], "version": QUESTION_VERSION, "branch": q["branch"],
        "answer_type": q["answer_type"], "text": q["text"][locale],
        "options": [{"id": o["id"], "label": o["label"][locale]} for o in q["options"]],
    }


def option_for(question_id: str, option_id: str) -> dict | None:
    question = QUESTIONS.get(question_id)
    if not question:
        return None
    return next((o for o in question["options"] if o["id"] == option_id), None)


def answer_state(answers: dict[str, str]) -> tuple[dict[str, float], dict, dict]:
    """Build deterministic score weights, hard filters and context from answers."""
    weights: dict[str, float] = {}
    filters: dict = {}
    context: dict = {}
    for question_id, option_id in answers.items():
        option = option_for(question_id, option_id)
        if not option:
            continue
        for key, value in option.get("weights", {}).items():
            weights[key] = weights.get(key, 0) + float(value)
        filters.update(option.get("filters", {}))
        context.update(option.get("context", {}))
    return weights, filters, context


def answer_reasons(answers: dict[str, str], language: str, limit: int = 4) -> list[str]:
    reasons = []
    for question_id, option_id in answers.items():
        option = option_for(question_id, option_id)
        if option and option.get("reason") and option["reason"] not in reasons:
            reasons.append(option["reason"])
    if language == "ru":
        # Keep these factual labels compact; they are derived only from chosen answers.
        translate = {"comfort": "комфорт", "tension": "напряжение", "humor": "юмор", "emotion": "эмоции", "unusual": "необычный опыт", "warm": "тёплая история", "adventure": "приключение", "atmosphere": "атмосфера", "light humor": "лёгкий юмор", "fast pace": "быстрый темп", "steady pace": "ровный темп", "a hopeful tone": "светлый тон", "some drama": "драма", "danger and survival": "опасность и выживание", "psychological tension": "психологическое напряжение", "a mystery": "загадка", "fear of the unknown": "страх неизвестности", "an unreliable hero": "ненадёжный герой", "relationship tension": "напряжение в отношениях", "paranoia": "паранойя", "a mind-bending angle": "игра с восприятием", "a crime mystery": "криминальная загадка", "a disappearance": "исчезновение", "the unexplained": "необъяснимое", "a clear resolution": "понятный финал", "some ambiguity": "немного недосказанности", "an open ending": "открытый финал", "situational humor": "ситуативный юмор", "sharp humor": "сарказм", "absurd humor": "абсурд", "dark humor": "чёрный юмор", "warm humor": "добрый юмор", "an easy watch": "лёгкий просмотр", "some heart": "душевность", "satire": "сатира", "relationships": "отношения", "family": "семья", "friendship": "дружба", "an inspiring story": "вдохновляющая история", "a difficult choice": "сложный выбор", "a calm pace": "спокойный темп", "a frantic pace": "бешеный ритм", "character relationships": "герои и отношения", "a strong plot": "сильный сюжет", "a balanced story": "баланс героев и сюжета", "a distinctive visual world": "визуальный мир", "a high-concept idea": "необычная идея", "an unusual narrative": "нестандартный рассказ", "unusual characters": "необычные герои", "a reality-bending premise": "сломанная реальность", "accessible originality": "понятная необычность", "understandable originality": "необычность с логикой", "bold originality": "смелая необычность", "dreamlike visuals": "сновидческая атмосфера", "an anxious mood": "тревожная атмосфера", "a puzzle": "головоломка", "a wild adventure": "безумное приключение"}
        reasons = [translate.get(reason, reason) for reason in reasons]
    return reasons[:limit]
