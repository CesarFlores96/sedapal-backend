from fastapi import APIRouter, Body
from pydantic import BaseModel, Field

from app.database import get_pool
from app.services.chat import answer_chat, get_chat_schema, run_query


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=500)


class QueryRequest(BaseModel):
    sql: str = Field(min_length=1)


router = APIRouter(prefix="/chat", tags=["chat"])


@router.post("")
async def post_chat(payload: ChatRequest = Body(...)) -> dict[str, str]:
    return await answer_chat(get_pool(), payload.query.strip())


@router.get("/schema")
async def get_schema() -> dict[str, dict[str, list[dict[str, str]]]]:
    return await get_chat_schema(get_pool())


@router.post("/query")
async def post_query(payload: QueryRequest = Body(...)) -> dict[str, list]:
    return await run_query(get_pool(), payload.sql.strip())
