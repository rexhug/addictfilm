"""Transport boundary for the versioned recommendation engine.

The domain implementation remains in ``main`` for this incremental split; these
handlers keep the public routes grouped without changing its request contract.
"""
from deps import current_user, throttled_quiz
from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/recommendations", tags=["recommendations"])


class RecommendationStartBody(BaseModel):
    language: str = Field(default="ru", max_length=8)


class RecommendationAnswerBody(RecommendationStartBody):
    question_id: str = Field(min_length=1, max_length=64)
    answer_id: str = Field(min_length=1, max_length=64)


class RandomRecommendationBody(RecommendationStartBody):
    context: str = Field(default="solo", max_length=16)


class RecommendationReplaceBody(RecommendationStartBody):
    film_id: int
    role: str | None = Field(default=None, max_length=24)


class RecommendationFeedbackBody(BaseModel):
    action: str = Field(min_length=1, max_length=24)
    mode: str = Field(default="random", max_length=16)
    session_id: str | None = Field(default=None, max_length=96)
    role: str | None = Field(default=None, max_length=24)
    score: float | None = None


async def _main_handler(name: str, *args, **kwargs):
    # Resolve lazily to avoid a module import cycle.  The existing implementation
    # remains the single source of truth while the transport surface is split.
    import main
    return await getattr(main, name)(*args, **kwargs)


@router.post("/random")
async def recommendation_random(body: RandomRecommendationBody,
                                user: dict = Depends(throttled_quiz)):
    return await _main_handler("recommendation_random", body, user)


@router.post("/quiz/start")
async def recommendation_quiz_start(body: RecommendationStartBody,
                                    user: dict = Depends(throttled_quiz)):
    return await _main_handler("recommendation_quiz_start", body, user)


@router.get("/quiz/{session_id}")
async def recommendation_quiz_get(session_id: str, language: str = "ru",
                                  user: dict = Depends(current_user)):
    return await _main_handler("recommendation_quiz_get", session_id, language, user)


@router.post("/quiz/{session_id}/answer")
async def recommendation_quiz_answer(session_id: str, body: RecommendationAnswerBody,
                                     user: dict = Depends(throttled_quiz)):
    return await _main_handler("recommendation_quiz_answer", session_id, body, user)


@router.post("/quiz/{session_id}/back")
async def recommendation_quiz_back(session_id: str, body: RecommendationStartBody,
                                    user: dict = Depends(throttled_quiz)):
    return await _main_handler("recommendation_quiz_back", session_id, body, user)


@router.post("/quiz/{session_id}/restart")
async def recommendation_quiz_restart(session_id: str, body: RecommendationStartBody,
                                       user: dict = Depends(throttled_quiz)):
    return await _main_handler("recommendation_quiz_restart", session_id, body, user)


@router.get("/quiz/{session_id}/results")
async def recommendation_quiz_results(session_id: str, language: str = "ru",
                                      user: dict = Depends(current_user)):
    return await _main_handler("recommendation_quiz_results", session_id, language, user)


@router.post("/quiz/{session_id}/replace")
async def recommendation_quiz_replace(session_id: str, body: RecommendationReplaceBody,
                                      user: dict = Depends(throttled_quiz)):
    return await _main_handler("recommendation_quiz_replace", session_id, body, user)


@router.post("/{film_id}/feedback")
async def recommendation_feedback(film_id: int, body: RecommendationFeedbackBody,
                                  user: dict = Depends(current_user)):
    return await _main_handler("recommendation_feedback", film_id, body, user)
