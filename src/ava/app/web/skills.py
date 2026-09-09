"""Authenticated skill inventory and configuration for an execution project."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import Field

from ava.agent import skills

from .models import RequestBody, parse_body
from .registry import Registry
from .routes import error_response


class NewSkill(RequestBody):
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(min_length=1, max_length=1024)
    body: str = Field(min_length=1, max_length=65536)
    scope: Literal["project", "global"] = "project"


class SkillState(RequestBody):
    state: Literal["enabled", "disabled", "removed"]


def register_skill_routes(app: FastAPI, registry: Registry) -> None:
    @app.api_route("/api/projects/{project_id}/skills", methods=["GET", "POST"])
    @app.api_route("/api/projects/{project_id}/skills/{identity}", methods=["GET", "POST"])
    async def endpoint(project_id: str, request: Request, identity: str = "", cwd: str = "") -> Response:
        project = registry.find_project(project_id)
        if project is None or project.hidden:
            return error_response(404, "No such project.")
        directory = Path(cwd) if cwd else project.path
        if directory != project.path and not any(chat.agent.cwd == directory for chat in project.chats):
            return error_response(404, "No such project workspace.")
        try:
            if request.method == "POST":
                if identity:
                    state = await parse_body(request, SkillState)
                    if state is None:
                        return error_response(400, "Choose enabled, disabled or removed.")
                    await asyncio.to_thread(skills.set_state, directory, identity, state.state)
                else:
                    draft = await parse_body(request, NewSkill)
                    if draft is None:
                        return error_response(400, "Provide a valid name, description, instructions and location.")
                    created = await asyncio.to_thread(skills.create, directory, **draft.model_dump())
                    return JSONResponse(created, status_code=201)
            if identity:
                return JSONResponse(await asyncio.to_thread(skills.detail, directory, identity))
            rows = await asyncio.to_thread(skills.inventory, directory)
            return JSONResponse({"skills": rows, "revision": skills.revision()})
        except (ValueError, OSError, sqlite3.Error) as error:
            return error_response(400, str(error))
