"""Bounded, chat-scoped reaction summaries returned by the storage API."""

from typing import Annotated, Literal

from pydantic import Field, field_validator

from msu_hub_bot.storage.models import BigInt, DatabaseModel

Count = Annotated[int, Field(strict=True, ge=0)]


class ReactionRank(DatabaseModel):
    user_id: BigInt
    first_name: str
    last_name: str | None = None
    username: str | None = None
    score: Count
    people: Count
    messages: Count


class ReactionEmoji(DatabaseModel):
    key: str = Field(min_length=1, max_length=256)
    count: Count


class ReactionPost(DatabaseModel):
    message_id: BigInt
    thread_id: BigInt | None = None
    author_id: BigInt | None = None
    score: Count
    people: Count


class ReactionSummary(DatabaseModel):
    points: Count
    reactions: Count
    givers: Count
    getters: Count
    messages: Count
    anonymous: Count
    paid: Count
    unattributed: Count
    channel_reactions: Count


class ReactionScoreboard(DatabaseModel):
    days: Literal[1, 7, 30]
    getters: list[ReactionRank] = Field(max_length=10)
    givers: list[ReactionRank] = Field(max_length=10)
    emoji: list[ReactionEmoji] = Field(max_length=10)
    posts: list[ReactionPost] = Field(max_length=10)
    summary: ReactionSummary

    @field_validator("days", mode="before")
    @classmethod
    def integer_window(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Reaction statistics require an integer window")
        return value
