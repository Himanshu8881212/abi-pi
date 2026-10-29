"""Saved Main Agent chats (CHAT-PERSIST, D-90; MA, SC-2, SC-4, SC-9): each person's own conversations, never shared.

A chat is the UI's transcript (`items`), saved by the UI through `save_chat` (never by an agent: it would write history
it didn't have). Only its owner reads it, and a project chat only while the owner still has a role in that project.
A confirm item keeps action, input, message and a status word; its `confirm_token` is dropped here (SC-4).
"""

import json
from typing import Literal

from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field, model_validator

from abi import storage
from abi.actions import ActionFailed, action
from abi.agent import CTX
from abi.security import Role, User, role_in

TEXT = 100_000  # characters per item text
SIZE = 2_000_000  # characters of a whole chat's items as JSON
TITLE = 80
QUOTABLE = ["chat", "created", "updated"]  # D-22: server-made IDs and times an agent may quote (GATE-9-fix-8.4)


class Used(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    effort: str | None = Field(None, max_length=20)
    model: str | None = Field(None, max_length=200)  # UI-DS.5.4: the model that answered, as the server named it


class Confirmed(BaseModel):  # extra keys (confirm_token, a shown result) are ignored: never stored
    action: str = Field(max_length=64)
    input: dict = {}
    message: str = Field("", max_length=TEXT)
    status: Literal["done", "failed", "cancelled"] | None = None  # UI-DS.5.4: Cancel drops the token, no call


class Flag(BaseModel):  # GROUNDING-UX: a refused draft's counts; never its values (AG-2)
    numbers: int = Field(ge=0)
    names: int = Field(ge=0)


class Item(BaseModel):
    kind: Literal["user", "assistant", "error", "tool", "confirm"]
    text: str | None = Field(None, max_length=TEXT)
    template: bool | None = None
    flags: list[Flag] | None = Field(None, max_length=10)
    used: Used | None = None
    c: Confirmed | None = None
    ctx: str | None = Field(None, max_length=CTX)  # GATE-9-fix-5: an answer's sealed tool context, sent back with it

    @model_validator(mode="after")
    def _shape(self):
        if (self.kind == "confirm") != (self.c is not None) or (self.kind != "confirm") != (self.text is not None):
            raise ValueError("a confirm item has `c` only, any other item has `text`")
        return self


class Save(BaseModel):
    id: int | None = Field(None, description="the chat to replace; omitted starts a new chat")
    project: str | None = Field(None, pattern=storage.PROJECT, description="the project the chat was started in")
    items: list[Item] = Field(max_length=1000)


class ChatRef(BaseModel):
    id: int


class Rename(ChatRef):
    title: str = Field(min_length=1, max_length=TITLE)


class ListChats(BaseModel):
    project: str | None = Field(None, pattern=storage.PROJECT, description="omitted: every chat of yours")


def _title(items: list[Item]) -> str:
    return next((" ".join(i.text.split())[:TITLE] for i in items if i.kind == "user" and i.text.strip()), "")


def _own(conn, user: User, chat: int) -> dict:
    """The caller's chat `chat`, else 404 (another person's, or a project the caller no longer has a role in)."""
    row = conn.execute("SELECT * FROM chats WHERE id = %s AND user_id = %s", [chat, user.id]).fetchone()
    if row is None or row["project"] and role_in(conn, user, row["project"]) is None:
        raise ActionFailed(404, f"No chat {chat}.")
    return row


@action(Role.viewer)  # a project chat needs a role there (run() checks `project`)
def save_chat(conn, user: User, args: Save) -> dict:
    """Saves the conversation shown in your chat panel (new, or replacing chat `id`)."""
    items = [i.model_dump(exclude_none=True) for i in args.items]
    if len(json.dumps(items)) > SIZE:
        raise ActionFailed(413, f"A chat holds at most {SIZE:,} characters; start a new chat.")
    title = _title(args.items)
    if args.id is None:
        row = conn.execute("INSERT INTO chats (user_id, project, title, items) VALUES (%s, %s, %s, %s) RETURNING id, title",
                           [user.id, args.project, title, Jsonb(items)]).fetchone()
    else:  # the project never changes: its role check was the one run() made
        row = conn.execute("UPDATE chats SET items = %s, updated = now(), title = CASE WHEN title = '' THEN %s ELSE title END"
                           " WHERE id = %s AND user_id = %s AND project IS NOT DISTINCT FROM %s RETURNING id, title",
                           [Jsonb(items), title, args.id, user.id, args.project]).fetchone()
        if row is None:
            raise ActionFailed(404, f"No chat {args.id}{f' in project {args.project!r}' if args.project else ''}.")
    return row


@action(Role.viewer)
def list_chats(conn, user: User, args: ListChats) -> dict:
    """Your saved chats, newest first: one project's, or (no project) your project-less chats and those of projects
    you still have a role in."""
    where = "project = %(p)s" if args.project else (
        "(project IS NULL OR %(owner)s OR project IN (SELECT project FROM project_roles WHERE user_id = %(user)s))")
    chats = conn.execute(
        f"SELECT id, project, title, created, updated FROM chats WHERE user_id = %(user)s AND {where}"
        " ORDER BY updated DESC, id DESC", {"p": args.project, "owner": user.workspace_owner, "user": user.id}).fetchall()
    return {"chats": chats, "columns": QUOTABLE, "rows": [[c[k] for k in ("id", "created", "updated")] for c in chats]}


@action(Role.viewer)
def chat_history(conn, user: User, args: ChatRef) -> dict:
    """One of your saved chats with its messages."""
    row = _own(conn, user, args.id)
    return row | {"columns": QUOTABLE, "rows": [[row[k] for k in ("id", "created", "updated")]]}


@action(Role.viewer)
def rename_chat(conn, user: User, args: Rename) -> dict:
    """Renames one of your saved chats."""
    _own(conn, user, args.id)
    conn.execute("UPDATE chats SET title = %s WHERE id = %s", [args.title, args.id])
    return {"id": args.id, "title": args.title}


@action(Role.viewer, destructive=True)
def delete_chat(conn, user: User, args: ChatRef) -> dict:
    """Deletes one of your saved chats."""
    _own(conn, user, args.id)
    conn.execute("DELETE FROM chats WHERE id = %s", [args.id])
    return {"deleted": args.id}
