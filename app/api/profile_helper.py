"""Profile helper API: chat, session, profile, download, scales, publish."""

import json
import re

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response, StreamingResponse
from pydantic import BaseModel

from app.api.auth_bridge import get_current_auth_context
from app.integrations.account_sync import sync_twin_record
from app.services.profile_helper import agent as profile_agent
from app.services.profile_helper import block_agent as profile_block_agent
from app.services.profile_helper import sessions as profile_sessions

router = APIRouter()
_INVALID_SESSION_IDS = frozenset({"", "undefined", "null", "none", "nan"})


class ChatRequest(BaseModel):
    session_id: str | None = None
    message: str
    model: str | None = None


class ScaleSubmitRequest(BaseModel):
    session_id: str
    scale_name: str
    answers: dict
    scores: dict
    result_summary: dict | None = None


class PublishRequest(BaseModel):
    session_id: str
    visibility: str = "private"
    exposure: str = "brief"
    display_name: str | None = None


def _get_uid(auth_ctx: dict) -> str:
    auth_context = auth_ctx.get("auth_context")
    if auth_context is not None:
        return str(auth_context.subject)
    user = auth_ctx.get("user") or {}
    return str(user.get("id", "anonymous"))


def _normalize_session_id(session_id: str | None) -> str | None:
    if session_id is None:
        return None
    sid = session_id.strip()
    if sid.lower() in _INVALID_SESSION_IDS:
        return None
    return sid


def _get_session_for_user(session_id: str, uid: str) -> dict:
    session = profile_sessions.get(session_id)
    if not session:
        # 会话不在内存中（常见于 uvicorn --reload 后热重载导致内存清空）
        # 尝试用 get_or_create 从磁盘恢复画像文件
        _, session = profile_sessions.get_or_create(session_id, user_id=uid)

    existing_uid = session.get("user_id")
    if existing_uid and str(existing_uid) != str(uid):
        raise HTTPException(status_code=404, detail="会话不存在或已过期")
    if not existing_uid:
        session["user_id"] = uid
    return session


_PLACEHOLDER_FIRST_LINE = frozenset({
    "unnamed", "未命名", "forum_profile", "论坛画像", "identity", "姓名/标识", "[姓名/标识]",
})


def _is_placeholder_title(title: str) -> bool:
    """Check if the first-line title is a placeholder that should be replaced."""
    t = (title or "").strip().lower()
    if not t:
        return True
    if t in _PLACEHOLDER_FIRST_LINE:
        return True
    if re.match(r"^unnamed-\d{4}-\d{2}-\d{2}$", t):
        return True
    for p in _PLACEHOLDER_FIRST_LINE:
        if t.startswith(p):
            return True
    return False


def _ensure_display_name_in_profile(content: str, display_name: str) -> str:
    """Replace placeholder first # line with display_name so shared twin shows correct name."""
    lines = content.strip().split("\n")
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            if _is_placeholder_title(title):
                lines[i] = f"# {display_name}"
                return "\n".join(lines)
            return content
    if not content.strip().startswith("# "):
        return f"# {display_name}\n\n{content}"
    return content


def _generate_brief_from_profile(parsed: dict, name: str) -> str:
    """Generate a compact forum profile when no explicit forum profile exists."""
    identity = parsed.get("identity", {})
    capability = parsed.get("capability", {})
    cognitive = parsed.get("cognitive_style", {})

    stage = identity.get("research_stage", "")
    primary = identity.get("primary_field", "")
    secondary = identity.get("secondary_field", "")
    cross = identity.get("cross_field", "")
    method = identity.get("method", "")
    institution = identity.get("institution", "")

    fields = " / ".join(f for f in [primary, secondary] if f)
    identity_line = f"A {stage} researcher" if stage else "A researcher"
    if fields:
        identity_line += f" focused on {fields}"
    if cross:
        identity_line += f", with cross-disciplinary interests in {cross}"
    if institution:
        identity_line += f", based at {institution}"
    identity_line += "."

    expertise_items = []
    if primary:
        expertise_items.append(f"- {primary}")
    if secondary and secondary != primary:
        expertise_items.append(f"- {secondary}")
    if cross:
        expertise_items.append(f"- {cross}")
    if method:
        expertise_items.append(f"- Method: {method}")
    tech_stack = capability.get("tech_stack", [])
    tech_names = [t.get("tech", "") for t in tech_stack[:5] if t.get("tech")]
    if tech_names:
        expertise_items.append(f"- Tools: {', '.join(tech_names)}")

    thinking_items = []
    cog_type = cognitive.get("type", "")
    if cog_type:
        thinking_items.append(f"- {cog_type}")
    if method:
        thinking_items.append(f"- Methodological approach: {method}")
    process = capability.get("process", {})
    top_skills = sorted(
        (
            (k, v)
            for k, v in process.items()
            if isinstance(v, dict) and v.get("score") is not None
        ),
        key=lambda x: x[1]["score"],
        reverse=True,
    )[:3]
    for _, val in top_skills:
        desc = val.get("description", "")
        if desc:
            thinking_items.append(f"- {desc}")

    lines = [f"# {name}", "", "## Identity", "", identity_line]
    if expertise_items:
        lines += ["", "## Expertise", ""] + expertise_items
    if thinking_items:
        lines += ["", "## Thinking Style", ""] + thinking_items
    return "\n".join(lines) + "\n"


async def _record_twin_to_account_service(token: str | None, payload: dict) -> dict:
    return await sync_twin_record(token, payload)


@router.post("/chat", response_class=StreamingResponse)
async def chat_stream(
    req: ChatRequest,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Streaming chat: SSE response."""
    uid = _get_uid(auth_ctx)
    normalized_session_id = _normalize_session_id(req.session_id)
    session_id, session = profile_sessions.get_or_create(
        normalized_session_id,
        user_id=uid,
    )
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    def generate():
        try:
            for chunk in profile_agent.run_agent(
                req.message, session, stream=True, model=req.model
            ):
                yield f"data: {json.dumps({'content': chunk}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-Id": session_id,
        },
    )


@router.post("/chat/blocks", response_class=StreamingResponse)
async def chat_blocks_stream(
    req: ChatRequest,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Block 协议 SSE：每个 Block 作为一条 SSE 事件发送。"""
    uid = _get_uid(auth_ctx)
    normalized_session_id = _normalize_session_id(req.session_id)
    session_id, session = profile_sessions.get_or_create(
        normalized_session_id,
        user_id=uid,
    )
    if not req.message.strip():
        raise HTTPException(status_code=400, detail="消息不能为空")

    def generate():
        try:
            blocks = profile_block_agent.run_block_agent(
                req.message, session, model=req.model
            )
            # 每轮对话结束后立即将消息历史持久化到磁盘
            # 确保 session 过期后仍可从磁盘恢复 AI 上下文
            profile_sessions.save_messages(session)
            for block in blocks:
                yield f"data: {json.dumps(block, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'text', 'content': f'服务器错误: {e}'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Session-Id": session_id,
        },
    )


@router.get("/profile/{session_id}")
async def get_profile(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Get development and forum profile content for session."""
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    return {
        "profile": session["profile"],
        "forum_profile": session.get("forum_profile", ""),
    }


@router.get("/chat-history/{session_id}")
async def get_chat_history(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """返回 session 的用户可见对话历史（过滤 tool 角色，保留 user/assistant 可读消息）。"""
    uid = _get_uid(auth_ctx)
    normalized = _normalize_session_id(session_id)
    session = _get_session_for_user(normalized, uid)

    # 优先从内存 session 读取；内存中没有则从磁盘恢复
    raw_messages = session.get("messages") or profile_sessions.load_messages(normalized, uid)

    # 只保留用户可读的消息（user 角色 + 有文字内容的 assistant 角色）
    visible: list[dict] = []
    for m in raw_messages:
        role = m.get("role")
        if role == "user":
            content = m.get("content", "")
            if content:
                visible.append({"role": "user", "content": content})
        elif role == "assistant":
            content = m.get("content", "")
            # 跳过纯工具调用中转消息（content 为空或仅是系统桥接短文本）
            if content and len(content) > 5 and not content.startswith("（"):
                visible.append({"role": "assistant", "content": content})

    return {"messages": visible, "count": len(visible)}


@router.get("/profile/{session_id}/scientists/famous")
async def get_famous_scientists(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """知名科学家相似度匹配（纯计算，瞬间返回）。"""
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    from app.services.profile_helper.profile_parser import parse_profile
    from app.services.profile_helper.scientist_match import match_famous_scientists
    parsed = parse_profile(session["profile"])
    return match_famous_scientists(parsed)


@router.get("/profile/{session_id}/scientists/field")
async def get_field_scientist_recommendations(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """领域相关科学家推荐（LLM 调用，可能较慢）。"""
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    from app.services.profile_helper.profile_parser import parse_profile
    from app.services.profile_helper.scientist_match import recommend_field_scientists
    parsed = parse_profile(session["profile"])
    return {"recommendations": recommend_field_scientists(parsed)}


@router.get("/profile/{session_id}/structured")
async def get_structured_profile(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    from app.services.profile_helper.profile_parser import parse_profile

    return parse_profile(session["profile"])


@router.get("/download/{session_id}")
async def download_profile(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Download development profile as .md."""
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    return Response(
        content=session["profile"].encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="profile.md"'},
    )


@router.get("/download/{session_id}/forum")
async def download_forum_profile(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Download forum profile as .md."""
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    content = session.get("forum_profile", "")
    if not content:
        raise HTTPException(status_code=404, detail="尚未生成论坛画像")
    return Response(
        content=content.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="forum-profile.md"'},
    )


@router.post("/scales/submit")
async def submit_scale(
    req: ScaleSubmitRequest,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(req.session_id, uid)
    profile_sessions.save_scales(
        session,
        req.scale_name,
        {
            "answers": req.answers,
            "scores": req.scores,
            "result_summary": req.result_summary,
        },
    )
    return {"ok": True, "scale_name": req.scale_name}


@router.get("/scales/{session_id}")
async def get_scales(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(session_id, uid)
    return {"scales": session.get("scales", {})}


@router.post("/publish-to-library")
async def publish_to_library(
    req: PublishRequest,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    if req.visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="visibility 必须是 private 或 public")
    if req.exposure not in ("brief", "full"):
        raise HTTPException(status_code=400, detail="exposure 必须是 brief 或 full")

    token = auth_ctx.get("token")
    uid = _get_uid(auth_ctx)
    session = _get_session_for_user(req.session_id, uid)
    full_profile = session.get("profile", "")
    forum_profile = session.get("forum_profile", "")

    try:
        from app.services.profile_helper.profile_parser import parse_profile

        parsed = parse_profile(full_profile)
        default_name = parsed.get("name", "") or "我的数字分身"
    except Exception:
        parsed = {}
        default_name = "我的数字分身"

    display_name = (req.display_name or "").strip() or default_name
    if req.exposure == "brief":
        if forum_profile:
            role_content = forum_profile
            if not role_content.strip().startswith("# "):
                role_content = f"# {display_name}\n\n{role_content}"
        else:
            role_content = _generate_brief_from_profile(parsed, display_name)
    else:
        full_incomplete = not full_profile or full_profile.startswith("# 科研人员画像 — [姓名")
        if full_incomplete and forum_profile:
            role_content = forum_profile
            if not role_content.strip().startswith("# "):
                role_content = f"# {display_name}\n\n{role_content}"
        elif full_incomplete:
            raise HTTPException(status_code=400, detail="画像尚未完成，请先完成采集")
        else:
            role_content = full_profile

    from app.api.experts import ImportProfileRequest as ExpertImportReq
    from app.api.experts import import_profile_to_experts
    from app.core.config import get_user_agents_dir

    role_content = _ensure_display_name_in_profile(role_content, display_name)

    try:
        result = import_profile_to_experts(
            ExpertImportReq(forum_profile=role_content, session_id=req.session_id)
        )
        expert_name = result.get("expert_name", display_name)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"导入角色库失败: {exc}") from exc

    from datetime import date, datetime

    agent_dir = get_user_agents_dir(uid) / "my_twin"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "role.md").write_text(role_content, encoding="utf-8")
    meta = {
        "owner_user_id": uid,
        "visibility": req.visibility,
        "source": "profile_twin",
        "description": "基于科研画像生成的数字分身",
        "display_name": display_name,
        "exposure": req.exposure,
        "expert_name": expert_name,
        "created_at": date.today().strftime("%Y-%m-%d"),
        "updated_at": date.today().strftime("%Y-%m-%d"),
    }
    (agent_dir / "meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    twin_record_name = f"my_twin_{datetime.now().strftime('%Y%m%d%H%M%S%f')}"

    sync_result = await _record_twin_to_account_service(
        token,
        {
            "agent_name": twin_record_name,
            "display_name": display_name,
            "expert_name": expert_name,
            "visibility": req.visibility,
            "exposure": req.exposure,
            "session_id": req.session_id,
            "source": "profile_twin",
            "role_content": role_content,
        },
    )
    if not isinstance(sync_result, dict):
        sync_result = {"status": "unknown"}

    return {
        "ok": True,
        "agent_name": expert_name,
        "display_name": display_name,
        "visibility": req.visibility,
        "exposure": req.exposure,
        "sync_status": sync_result.get("status", "unknown"),
    }


@router.post("/session/reset/{session_id}")
async def session_reset(
    session_id: str,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Reset session: clear messages, restore blank profile."""
    uid = _get_uid(auth_ctx)
    _ = _get_session_for_user(session_id, uid)
    profile_sessions.reset(session_id)
    new_session = profile_sessions.get(session_id)
    if new_session:
        new_session["user_id"] = uid
    return {"ok": True, "session_id": session_id}


@router.get("/session")
async def session_get(
    session_id: str | None = None,
    auth_ctx: dict = Depends(get_current_auth_context),
):
    """Get or create session, return session_id."""
    uid = _get_uid(auth_ctx)
    sid, _ = profile_sessions.get_or_create(
        _normalize_session_id(session_id),
        user_id=uid,
    )
    return {"session_id": sid}
