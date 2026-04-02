"""In-memory session management with cleanup and profile auto-save."""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from datetime import date
from pathlib import Path

from app.core.config import (
    get_profile_helper_profiles_dir,
    get_user_agents_dir,
    get_user_profile_dir,
)
from app.services.profile_helper.tools import load_template

_sessions: dict[str, dict] = {}
SESSION_TTL_SECONDS = max(60, int(os.getenv("PROFILE_HELPER_SESSION_TTL_SECONDS", "3600")))
SESSION_MAX_COUNT = max(10, int(os.getenv("PROFILE_HELPER_SESSION_MAX_COUNT", "1000")))
PLACEHOLDER_IDENTIFIERS = {"[姓名/标识]", "姓名/标识"}
PROFILE_TITLE_PREFIXES = (
    "# 科研人员画像 — ",
    "# 科研数字分身 — ",
)


def _now() -> float:
    return time.time()


def _load_template_with_date() -> str:
    today_str = date.today().strftime("%Y-%m-%d")
    return load_template().replace("YYYY-MM-DD", today_str)


def _today_unnamed() -> str:
    return f"unnamed-{date.today().strftime('%Y-%m-%d')}"


def _sanitize_identifier(identifier: str) -> str:
    cleaned = identifier.strip()
    if cleaned in PLACEHOLDER_IDENTIFIERS or not cleaned:
        return _today_unnamed()
    cleaned = re.sub(r'[\\/:*?"<>|]+', "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or _today_unnamed()


def _extract_profile_identifier(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for prefix in PROFILE_TITLE_PREFIXES:
            if stripped.startswith(prefix):
                return _sanitize_identifier(stripped[len(prefix) :])
        if stripped.startswith("# "):
            return _sanitize_identifier(stripped[2:])
        break
    return _today_unnamed()


def _normalize_existing_path(path_value: str | None) -> Path | None:
    if not path_value:
        return None
    return Path(path_value)


def _profiles_dir(user_id: int | str | None = None) -> Path:
    if user_id:
        return get_user_profile_dir(user_id)
    return get_profile_helper_profiles_dir()


def _session_suffix(session: dict) -> str:
    sid = session.get("session_id") or ""
    if sid:
        return sid.replace("-", "")[:8]
    return uuid.uuid4().hex[:8]


def _target_profile_path(content: str, session: dict) -> Path:
    identifier = _extract_profile_identifier(content)
    user_id = session.get("user_id")
    profiles_dir = _profiles_dir(user_id)
    if user_id:
        return profiles_dir / "profile.md"
    suffix = _session_suffix(session)
    return profiles_dir / f"{identifier}-{suffix}.md"


def _target_forum_profile_path(session: dict) -> Path:
    user_id = session.get("user_id")
    if user_id:
        return _profiles_dir(user_id) / "forum_profile.md"
    profile_path = _normalize_existing_path(session.get("profile_path"))
    if not profile_path:
        profile_path = _target_profile_path(session.get("profile", ""), session)
    return profile_path.with_name(f"{profile_path.stem}-论坛画像.md")


def _relocate_file_if_needed(current_path: Path | None, target_path: Path) -> None:
    if not current_path or current_path == target_path or not current_path.exists():
        return
    if target_path.exists():
        current_path.unlink()
        return
    target_path.parent.mkdir(parents=True, exist_ok=True)
    current_path.rename(target_path)


def _new_session(session_id: str, user_id: int | str | None = None) -> dict:
    now = _now()
    profile = _load_template_with_date()
    forum_profile = ""
    profile_path = None
    forum_profile_path = None
    scales = {}

    if user_id:
        pdir = _profiles_dir(user_id)
        pf = pdir / "profile.md"
        ff = pdir / "forum_profile.md"
        sf = pdir / "scales.json"
        if pf.exists():
            profile = pf.read_text(encoding="utf-8")
            profile_path = str(pf)
        if ff.exists():
            forum_profile = ff.read_text(encoding="utf-8")
            forum_profile_path = str(ff)
        if sf.exists():
            try:
                scales = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                scales = {}
    else:
        # 匿名用户：按 session_id 前 8 位查找已有 profile 文件
        # 文件命名规则：{identifier}-{sid[:8]}.md（见 _target_profile_path）
        sid_suffix = session_id.replace("-", "")[:8]
        anon_dir = _profiles_dir(None)
        if anon_dir.exists():
            profile_matches = sorted(anon_dir.glob(f"*-{sid_suffix}.md"))
            if profile_matches:
                profile = profile_matches[0].read_text(encoding="utf-8")
                profile_path = str(profile_matches[0])
            forum_matches = sorted(anon_dir.glob(f"*-{sid_suffix}-论坛画像.md"))
            if forum_matches:
                forum_profile = forum_matches[0].read_text(encoding="utf-8")
                forum_profile_path = str(forum_matches[0])

    # 从磁盘恢复消息历史（AI 续接对话的关键）
    messages = load_messages(session_id, user_id=user_id)

    return {
        "session_id": session_id,
        "user_id": user_id,
        "messages": messages,
        "profile": profile,
        "forum_profile": forum_profile,
        "profile_path": profile_path,
        "forum_profile_path": forum_profile_path,
        "scales": scales,
        "created_at": now,
        "updated_at": now,
    }


def _touch(session: dict) -> None:
    session["updated_at"] = _now()


def _is_expired(session: dict, now: float) -> bool:
    updated = float(session.get("updated_at") or 0)
    return (now - updated) > SESSION_TTL_SECONDS


def _cleanup() -> None:
    """Drop expired sessions and cap total count."""
    now = _now()
    expired = [sid for sid, s in _sessions.items() if _is_expired(s, now)]
    for sid in expired:
        _sessions.pop(sid, None)

    overflow = len(_sessions) - SESSION_MAX_COUNT
    if overflow > 0:
        oldest = sorted(
            _sessions.items(),
            key=lambda kv: float(kv[1].get("updated_at") or 0),
        )
        for sid, _ in oldest[:overflow]:
            _sessions.pop(sid, None)


def save_profile(session: dict, content: str) -> Path:
    """Persist the development profile to disk and session memory."""
    profiles_dir = _profiles_dir(session.get("user_id"))
    profiles_dir.mkdir(parents=True, exist_ok=True)

    target_path = _target_profile_path(content, session)
    current_path = _normalize_existing_path(session.get("profile_path"))
    _relocate_file_if_needed(current_path, target_path)
    target_path.write_text(content, encoding="utf-8")

    session["profile"] = content
    session["profile_path"] = str(target_path)

    forum_content = session.get("forum_profile", "")
    if forum_content:
        forum_target_path = _target_forum_profile_path(session)
        forum_current_path = _normalize_existing_path(session.get("forum_profile_path"))
        _relocate_file_if_needed(forum_current_path, forum_target_path)
        forum_target_path.write_text(forum_content, encoding="utf-8")
        session["forum_profile_path"] = str(forum_target_path)

    _sync_twin_agent(session)
    _touch(session)
    return target_path


def save_forum_profile(session: dict, content: str) -> Path:
    """Persist the forum profile to disk and session memory."""
    profiles_dir = _profiles_dir(session.get("user_id"))
    profiles_dir.mkdir(parents=True, exist_ok=True)

    profile_content = session.get("profile", "")
    if profile_content:
        save_profile(session, profile_content)

    target_path = _target_forum_profile_path(session)
    current_path = _normalize_existing_path(session.get("forum_profile_path"))
    _relocate_file_if_needed(current_path, target_path)
    target_path.write_text(content, encoding="utf-8")

    session["forum_profile"] = content
    session["forum_profile_path"] = str(target_path)
    _sync_twin_agent(session)
    _touch(session)
    return target_path


def _messages_path(session: dict) -> Path:
    """返回该 session 的消息历史文件路径。"""
    user_id = session.get("user_id")
    if user_id:
        return _profiles_dir(user_id) / "messages.json"
    sid = _session_suffix(session)
    anon_dir = _profiles_dir(None)
    anon_dir.mkdir(parents=True, exist_ok=True)
    return anon_dir / f"messages-{sid}.json"


def save_messages(session: dict) -> None:
    """将 session.messages 持久化到磁盘（每次 run_block_agent 后调用）。"""
    path = _messages_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    # 保留 user/assistant/tool 三种角色，确保 AI 续接时有完整上下文
    storable = [
        m for m in session.get("messages", [])
        if m.get("role") in ("user", "assistant", "tool")
    ]
    path.write_text(
        json.dumps(storable, ensure_ascii=False),
        encoding="utf-8",
    )


def load_messages(session_id: str, user_id=None) -> list:
    """在 session 重建时尝试从磁盘恢复消息历史。"""
    if user_id:
        path = _profiles_dir(user_id) / "messages.json"
    else:
        sid = session_id.replace("-", "")[:8]
        path = _profiles_dir(None) / f"messages-{sid}.json"
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _sync_twin_agent(session: dict) -> None:
    """When forum profile exists, auto-generate user's my_twin agent files."""
    user_id = session.get("user_id")
    forum_content = session.get("forum_profile", "")
    if not user_id or not forum_content:
        return

    twin_dir = get_user_agents_dir(user_id) / "my_twin"
    twin_dir.mkdir(parents=True, exist_ok=True)
    (twin_dir / "role.md").write_text(forum_content, encoding="utf-8")

    meta_path = twin_dir / "meta.json"
    existing_meta = {}
    if meta_path.exists():
        try:
            existing_meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            existing_meta = {}

    owner_user_id: int | str
    try:
        owner_user_id = int(user_id)
    except (TypeError, ValueError):
        owner_user_id = str(user_id)

    meta = {
        "owner_user_id": owner_user_id,
        "visibility": existing_meta.get("visibility", "private"),
        "source": "profile_twin",
        "description": "基于科研画像自动生成的数字分身",
        "created_at": existing_meta.get("created_at", date.today().strftime("%Y-%m-%d")),
        "updated_at": date.today().strftime("%Y-%m-%d"),
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _migrate_anon_to_user(session: dict, user_id: int | str) -> None:
    """将匿名 session 的画像和消息迁移到用户专属目录，并更新 session.user_id。"""
    pdir = _profiles_dir(user_id)
    pdir.mkdir(parents=True, exist_ok=True)

    # 迁移画像文件
    profile_content = session.get("profile", "")
    if profile_content and profile_content != _load_template_with_date():
        target = pdir / "profile.md"
        if not target.exists():
            target.write_text(profile_content, encoding="utf-8")
            session["profile_path"] = str(target)

    # 迁移消息历史
    msgs = session.get("messages", [])
    if msgs:
        messages_path = pdir / "messages.json"
        if not messages_path.exists():
            messages_path.write_text(json.dumps(msgs, ensure_ascii=False), encoding="utf-8")

    # 更新 session 的 user_id
    session["user_id"] = user_id
    _touch(session)


def get_or_create(
    session_id: str | None = None,
    user_id: int | str | None = None,
) -> tuple[str, dict]:
    """Get or create session. Returns (session_id, session_data)."""
    _cleanup()
    if session_id and session_id in _sessions:
        s = _sessions[session_id]
        s["session_id"] = session_id
        if user_id and not s.get("user_id"):
            # 用户刚登录，之前是匿名 session → 触发迁移
            _migrate_anon_to_user(s, user_id)
        if "forum_profile" not in s:
            s["forum_profile"] = ""
        if "profile_path" not in s:
            s["profile_path"] = None
        if "forum_profile_path" not in s:
            s["forum_profile_path"] = None
        if "scales" not in s:
            s["scales"] = {}
        _touch(s)
        return session_id, s
    sid = session_id or str(uuid.uuid4())
    _sessions[sid] = _new_session(sid, user_id=user_id)
    _cleanup()
    return sid, _sessions[sid]


def save_scales(session: dict, scale_name: str, data: dict) -> None:
    """Save scale results to session and disk."""
    if "scales" not in session:
        session["scales"] = {}
    data["completed_at"] = date.today().strftime("%Y-%m-%d")
    session["scales"][scale_name] = data

    user_id = session.get("user_id")
    if user_id:
        pdir = _profiles_dir(user_id)
        pdir.mkdir(parents=True, exist_ok=True)
        scales_path = pdir / "scales.json"
        scales_path.write_text(
            json.dumps(session["scales"], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    _touch(session)


def get(session_id: str) -> dict | None:
    """Get session by id, or None if not found."""
    s = _sessions.get(session_id)
    if not s:
        return None
    if _is_expired(s, _now()):
        _sessions.pop(session_id, None)
        return None
    _touch(s)
    return s


def list_ids() -> list[str]:
    """List active session IDs (used by agent-links runtime/tests)."""
    _cleanup()
    return list(_sessions.keys())


def reset(session_id: str) -> dict:
    """Reset session: clear messages and restore template profile."""
    user_id = _sessions.get(session_id, {}).get("user_id")
    _sessions[session_id] = _new_session(session_id, user_id=user_id)
    return _sessions[session_id]
