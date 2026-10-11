"""Executor-facing APIs for TopicLab integration mode."""

from __future__ import annotations

import asyncio
from uuid import uuid4
from app.api import executor_runs
import logging
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
import re

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.agent.discussion import run_discussion_for_topic
from app.agent.experts import EXPERT_SPECS
from app.agent.expert_reply import run_expert_reply
from app.agent.moderator_modes import PRESET_MODES, save_moderator_mode_config
from app.agent.workspace import (
    _get_expert_label,
    add_expert_metadata,
    copy_skills_to_workspace,
    ensure_topic_workspace,
    read_discussion_history,
    read_discussion_summary,
)
from app.core.config import get_workspace_base
from app.core.topic_defaults import DEFAULT_TOPIC_EXPERT_NAMES, DEFAULT_TOPIC_SKILL_IDS

router = APIRouter()


class ExecutorTopicBootstrapRequest(BaseModel):
    topic_id: str
    topic_title: str
    topic_body: str = ""
    num_rounds: int = 5
    use_ai_generated_roles: bool = False


class ExecutorGeneratedExpert(BaseModel):
    name: str
    label: str
    description: str
    role_content: str


class ExecutorSetGeneratedExpertsRequest(BaseModel):
    experts: list[ExecutorGeneratedExpert] = Field(..., min_length=1, max_length=10)


class ExecutorDiscussionRequest(BaseModel):
    run_id: str | None = Field(default=None, pattern=r"^run_[a-f0-9]{32}$")
    topic_id: str
    topic_title: str
    topic_body: str = ""
    num_rounds: int = Field(default=5, ge=1, le=20)
    expert_names: list[str] = Field(default_factory=list)
    max_turns: int = Field(default=50000, ge=10, le=50000)
    max_budget_usd: float = Field(default=500.0, ge=0.1)
    model: str | None = None
    allowed_tools: list[str] | None = None
    skill_list: list[str] | None = None
    mcp_server_ids: list[str] | None = None
    posts_context: str | None = None
    topiclab_sync_url: str | None = None  # When set, push snapshot per-round to TopicLab DB


class ExecutorExpertReplyRequest(BaseModel):
    topic_id: str
    topic_title: str
    topic_body: str = ""
    expert_name: str
    expert_label: str
    user_post_id: str
    user_author: str
    user_question: str
    reply_post_id: str
    reply_created_at: str
    max_turns: int = 100
    max_budget_usd: float = 10.0
    posts_context: str | None = None


def _topic_workspace(topic_id: str) -> Path:
    return get_workspace_base() / "topics" / topic_id


def _write_posts_context(ws_path: Path, posts_context: str | None) -> None:
    if posts_context is None:
        return
    shared_dir = ws_path / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    (shared_dir / "posts_context.md").write_text(posts_context, encoding="utf-8")


def _read_turns(ws_path: Path) -> list[dict]:
    turns_dir = ws_path / "shared" / "turns"
    if not turns_dir.exists():
        return []
    turns: list[dict] = []
    for turn_file in sorted(turns_dir.glob("*.md")):
        match = re.fullmatch(r"round(\d+)_(.+)", turn_file.stem)
        round_num = int(match.group(1)) if match else None
        expert_name = match.group(2) if match else turn_file.stem
        turns.append(
            {
                "turn_key": turn_file.stem,
                "round_num": round_num,
                "expert_name": expert_name,
                "expert_label": _get_expert_label(expert_name, ws_path),
                "body": turn_file.read_text(encoding="utf-8").strip(),
                "updated_at": datetime.fromtimestamp(turn_file.stat().st_mtime, tz=timezone.utc).isoformat(),
            }
        )
    return turns


def _read_generated_images(ws_path: Path) -> list[str]:
    images_dir = ws_path / "shared" / "generated_images"
    if not images_dir.exists():
        return []
    return sorted(
        str(path.relative_to(images_dir)).replace("\\", "/")
        for path in images_dir.rglob("*")
        if path.is_file()
    )


def _read_discussion_snapshot(topic_id: str) -> dict:
    ws_path = _topic_workspace(topic_id)
    if not ws_path.exists():
        return {
            "topic_id": topic_id,
            "turns": [],
            "turns_count": 0,
            "discussion_history": "",
            "discussion_summary": "",
            "generated_images": [],
        }
    turns = _read_turns(ws_path)
    return {
        "topic_id": topic_id,
        "turns": turns,
        "turns_count": len(turns),
        "discussion_history": read_discussion_history(ws_path),
        "discussion_summary": read_discussion_summary(ws_path),
        "generated_images": _read_generated_images(ws_path),
    }


def _get_sync_interval_seconds() -> float:
    raw = os.getenv("DISCUSSION_SYNC_INTERVAL_SECONDS", "10.0").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 10.0


logger = logging.getLogger(__name__)


async def _push_snapshot_to_topiclab(topic_id: str, sync_url: str, snapshot: dict) -> bool:
    """POST snapshot to TopicLab internal endpoint. Returns True on success."""
    url = urljoin(f"{sync_url.rstrip('/')}/", f"internal/discussion-snapshot/{topic_id}")
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            await client.post(
                url,
                json={
                    "turns": snapshot.get("turns", []),
                    "turns_count": snapshot.get("turns_count", 0),
                    "discussion_history": snapshot.get("discussion_history", ""),
                    "discussion_summary": snapshot.get("discussion_summary", ""),
                    "generated_images": snapshot.get("generated_images", []),
                },
            )
        return True
    except Exception as e:
        logger.warning("Failed to push snapshot to TopicLab: %s", e)
        return False


async def _run_sync_loop_until_done(topic_id: str, sync_url: str, discussion_task: asyncio.Task) -> None:
    """Push snapshot to TopicLab only when turns increase, to reduce server load."""
    interval = _get_sync_interval_seconds()
    last_turns_count = -1
    while not discussion_task.done():
        await asyncio.sleep(interval)
        snapshot = _read_discussion_snapshot(topic_id)
        turns_count = snapshot.get("turns_count", 0)
        if turns_count > last_turns_count:
            await _push_snapshot_to_topiclab(topic_id, sync_url, snapshot)
            last_turns_count = turns_count


@router.post("/topics/bootstrap")
async def bootstrap_topic_workspace(req: ExecutorTopicBootstrapRequest):
    expert_names: list[str] | None = None if not req.use_ai_generated_roles else []
    ws_path = ensure_topic_workspace(
        get_workspace_base(), req.topic_id, expert_names=expert_names
    )
    copied_skills = copy_skills_to_workspace(ws_path, DEFAULT_TOPIC_SKILL_IDS)
    save_moderator_mode_config(
        ws_path,
        {
            "mode_id": "standard",
            "num_rounds": req.num_rounds,
            "custom_prompt": None,
            "skill_list": copied_skills,
            "mcp_server_ids": [],
            "model": None,
        },
    )
    if not req.use_ai_generated_roles:
        for expert_name in DEFAULT_TOPIC_EXPERT_NAMES:
            spec = EXPERT_SPECS.get(expert_name)
            if not spec:
                continue
            add_expert_metadata(
                ws_path,
                expert_name=expert_name,
                label=spec.get("label", expert_name),
                description=spec.get("description", ""),
                source="preset",
                is_from_topic_creation=True,
            )
    return {"ok": True, "topic_id": req.topic_id}


def _write_generated_experts(ws_path: Path, experts: list) -> None:
    """Write expert definitions to agents/ and metadata. Used by both add and replace."""
    agents_dir = ws_path / "agents"
    agents_dir.mkdir(exist_ok=True)
    for expert in experts:
        expert_dir = agents_dir / expert.name
        expert_dir.mkdir(exist_ok=True)
        role_file = expert_dir / "role.md"
        role_file.write_text(expert.role_content, encoding="utf-8")
        add_expert_metadata(
            ws_path,
            expert_name=expert.name,
            label=expert.label,
            description=expert.description,
            source="ai_generated",
            is_from_topic_creation=True,
        )


@router.post("/topics/{topic_id}/experts/generated")
async def set_generated_experts(topic_id: str, req: ExecutorSetGeneratedExpertsRequest):
    """Add AI-generated experts to topic workspace. Creates agents/<name>/role.md and metadata."""
    ws_path = ensure_topic_workspace(
        get_workspace_base(), topic_id, expert_names=[]
    )
    _write_generated_experts(ws_path, req.experts)
    return {"ok": True, "topic_id": topic_id, "expert_names": [e.name for e in req.experts]}


@router.post("/topics/{topic_id}/experts/replace")
async def replace_generated_experts(topic_id: str, req: ExecutorSetGeneratedExpertsRequest):
    """Replace all topic experts with AI-generated set. Removes existing agents first."""
    ws_path = ensure_topic_workspace(get_workspace_base(), topic_id)
    agents_dir = ws_path / "agents"
    if agents_dir.exists():
        shutil.rmtree(agents_dir)
    _write_generated_experts(ws_path, req.experts)
    return {"ok": True, "topic_id": topic_id, "expert_names": [e.name for e in req.experts]}


@router.post("/discussions")
async def run_discussion_executor(req: ExecutorDiscussionRequest):
    run_id = req.run_id or 'run_' + uuid4().hex
    record, created = executor_runs.reserve_run(run_id, req.model_dump())
    if not created:
        return record['result']
    try:
        result = await _execute_discussion(req, run_id, record)
        record.update(status='completed', result=result)
        executor_runs.write_record(record)
        return result
    except asyncio.CancelledError:
        record.update(status='cancelled')
        executor_runs.write_record(record)
        raise HTTPException(409, detail={'code': 'run_cancelled', 'run_id': run_id})
    except Exception:
        record.update(status='failed', error_code='executor_failed')
        executor_runs.write_record(record)
        raise
    finally:
        executor_runs.tasks.pop(run_id, None)


async def _execute_discussion(req, run_id, record):
    ws_path = ensure_topic_workspace(get_workspace_base(), req.topic_id)
    _write_posts_context(ws_path, req.posts_context)

    discussion_task = asyncio.create_task(
        run_discussion_for_topic(
            topic_id=req.topic_id,
            topic_title=req.topic_title,
            topic_body=req.topic_body,
            num_rounds=req.num_rounds,
            expert_names=req.expert_names,
            max_turns=req.max_turns,
            max_budget_usd=req.max_budget_usd,
            model=req.model,
            allowed_tools=req.allowed_tools,
            skill_list=req.skill_list,
            mcp_server_ids=req.mcp_server_ids,
        )
    )

    executor_runs.tasks[run_id] = discussion_task
    heartbeat = asyncio.create_task(executor_runs.heartbeat(record))
    try:
        if req.topiclab_sync_url:
            sync_task = asyncio.create_task(
                _run_sync_loop_until_done(req.topic_id, req.topiclab_sync_url, discussion_task)
            )
            await asyncio.gather(discussion_task, sync_task)
            # Final push so TopicLab has latest state before we return
            snapshot = _read_discussion_snapshot(req.topic_id)
            await _push_snapshot_to_topiclab(req.topic_id, req.topiclab_sync_url, snapshot)
        else:
            await discussion_task
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)

    result = discussion_task.result()
    result["turns"] = _read_turns(ws_path)
    result["generated_images"] = _read_generated_images(ws_path)
    return result


@router.post("/expert-replies")
async def run_expert_reply_executor(req: ExecutorExpertReplyRequest):
    ws_path = ensure_topic_workspace(get_workspace_base(), req.topic_id)
    _write_posts_context(ws_path, req.posts_context)
    result = await run_expert_reply(
        ws_path=ws_path,
        topic_id=req.topic_id,
        topic_title=req.topic_title,
        expert_name=req.expert_name,
        expert_label=req.expert_label,
        user_post_id=req.user_post_id,
        user_author=req.user_author,
        user_question=req.user_question,
        reply_post_id=req.reply_post_id,
        reply_created_at=req.reply_created_at,
        max_turns=req.max_turns,
        max_budget_usd=req.max_budget_usd,
        persist_reply=False,
        posts_context_text=req.posts_context,
    )
    return result


@router.get("/discussions/{topic_id}/snapshot")
async def get_discussion_snapshot(topic_id: str):
    return _read_discussion_snapshot(topic_id)


@router.get('/runs/{run_id}')
async def get_executor_run(run_id: str):
    return executor_runs.read_record(run_id)


@router.post('/runs/{run_id}/cancel')
async def cancel_executor_run(run_id: str):
    record = executor_runs.read_record(run_id)
    if record['status'] in {'completed', 'cancelled', 'failed', 'interrupted'}:
        return record
    task = executor_runs.tasks.get(run_id)
    if task is None:
        raise HTTPException(409, detail={'code': 'run_owner_unavailable', 'run_id': run_id})
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    record = executor_runs.read_record(run_id)
    record['status'] = 'cancelled'
    executor_runs.write_record(record)
    return record
