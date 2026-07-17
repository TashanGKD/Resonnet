"""
Profile Helper - Block 协议交互流 + API 端点测试
覆盖沙盘：SB-B01~B10, SB-A01~A15
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.api.auth_bridge import get_current_auth_context
from app.services.profile_helper import sessions as profile_sessions
from app.services.profile_helper.block_agent import (
    _is_ai_memory_request,
    _is_ai_memory_enhanced_request,
    _WELCOME_BLOCKS,
    run_block_agent,
)


# ──────────────────────────────────────────────
# auth fixture
# ──────────────────────────────────────────────

@pytest.fixture
def auth_override(client):
    async def _fake_auth_ctx():
        return {
            "user": {"id": 1, "phone": "13800138000", "username": "tester"},
            "token": "test-token",
        }

    app = client.app
    app.dependency_overrides[get_current_auth_context] = _fake_auth_ctx
    yield
    app.dependency_overrides.pop(get_current_auth_context, None)


@pytest.fixture
def anon_auth_override(client):
    async def _fake_anon():
        return {
            "auth_context": type(
                "Ctx", (), {"subject": "anonymous", "is_anonymous": True}
            )(),
            "user": {"id": "anonymous"},
            "token": None,
        }

    app = client.app
    app.dependency_overrides[get_current_auth_context] = _fake_anon
    yield
    app.dependency_overrides.pop(get_current_auth_context, None)


# ──────────────────────────────────────────────
# SB-B01 ~ SB-B05  Block 快速路径验证
# ──────────────────────────────────────────────

def test_sb_b01_welcome_blocks_on_first_message(isolated_workspace):
    """SB-B01：首次消息触发欢迎 Block（不调用 LLM）"""
    session_id, session = profile_sessions.get_or_create(None)
    blocks = run_block_agent("建立我的分身", session)

    types = [b["type"] for b in blocks]
    assert "text" in types
    assert "choice" in types


def test_sb_b02_welcome_blocks_only_a_and_b(isolated_workspace):
    """SB-B02：欢迎页选项只有 A 和 B（C 已关闭）"""
    choice_blocks = [b for b in _WELCOME_BLOCKS if b["type"] == "choice"]
    assert len(choice_blocks) == 1
    options = choice_blocks[0]["options"]
    option_ids = [o["id"] for o in options]
    assert "ai_memory" in option_ids
    assert "direct" in option_ids
    assert "ai_memory_enhanced" not in option_ids


def test_sb_b03_option_a_triggers_ai_memory_fast_path(isolated_workspace):
    """SB-B03：选 A 后用户粘贴消息会触发 fast path"""
    session_id, session = profile_sessions.get_or_create(None)
    # 先触发欢迎
    run_block_agent("建立我的分身", session)
    # 选 A
    blocks = run_block_agent("A. 有，先从 AI 记忆中提取信息（标准版）", session)

    types = [b["type"] for b in blocks]
    assert "copyable" in types


def test_sb_b04_ai_memory_trigger_detection():
    """SB-B04：_is_ai_memory_request 正确识别各种触发词"""
    session = {"messages": [{"role": "user", "content": "选了 A"}]}
    assert _is_ai_memory_request("A. 有，先从 AI 记忆中提取信息（标准版）", session)
    assert _is_ai_memory_request("ai记忆", session)
    assert _is_ai_memory_request("从ai", session)
    assert not _is_ai_memory_request("直接开始", session)


def test_sb_b05_enhanced_trigger_not_active():
    """SB-B05：C 路径触发词识别（C 当前关闭，但函数存在）"""
    session = {"messages": []}
    assert _is_ai_memory_enhanced_request("C. 有，先从 AI 记忆中提取信息（推断优化版）", session)
    assert not _is_ai_memory_enhanced_request("A. 标准版", session)


# ──────────────────────────────────────────────
# SB-B06 ~ SB-B10  Block 结构与消息历史
# ──────────────────────────────────────────────

def test_sb_b06_choice_block_structure(isolated_workspace):
    """SB-B06：欢迎页 choice Block 结构正确"""
    choice = next(b for b in _WELCOME_BLOCKS if b["type"] == "choice")
    assert "question" in choice
    assert "options" in choice
    for opt in choice["options"]:
        assert "id" in opt
        assert "label" in opt


def test_sb_b07_multiple_interactive_blocks_intercepted(isolated_workspace, monkeypatch):
    """SB-B07：同一轮内多个 ask_choice 只返回第一个"""
    import app.services.profile_helper.block_agent as ba

    def _fake_create(*args, **kwargs):
        resp = MagicMock()
        resp.choices[0].message.content = None
        tc1 = MagicMock()
        tc1.id = "tc1"
        tc1.function.name = "ask_choice"
        tc1.function.arguments = json.dumps({
            "question": "第一个问题",
            "options": [{"id": "a", "label": "A"}],
        })
        tc2 = MagicMock()
        tc2.id = "tc2"
        tc2.function.name = "ask_choice"
        tc2.function.arguments = json.dumps({
            "question": "第二个问题",
            "options": [{"id": "b", "label": "B"}],
        })
        resp.choices[0].message.tool_calls = [tc1, tc2]
        return resp

    from unittest.mock import MagicMock, patch
    session_id, session = profile_sessions.get_or_create(None)
    # 先走欢迎，确保非 fresh session
    session["messages"].append({"role": "user", "content": "hello"})
    session["messages"].append({"role": "assistant", "content": "欢迎"})

    with patch.object(ba, "get_client_with_rotation") as mock_client_fn:
        mock_client = MagicMock()
        mock_client_fn.return_value = (mock_client, "test-key")
        mock_client.chat.completions.create.return_value = _fake_create()

        blocks = ba.run_block_agent("直接开始填写", session)

    choice_blocks = [b for b in blocks if b["type"] == "choice"]
    assert len(choice_blocks) == 1
    assert choice_blocks[0]["question"] == "第一个问题"


def test_sb_b08_write_profile_creates_md_file(isolated_workspace):
    """SB-B08：write_profile 调用后 .md 文件落盘"""
    session_id, session = profile_sessions.get_or_create(None)
    profile_sessions.save_profile(session, "# 科研人员画像 — 测试用户\n\n## 元信息\n\n- **研究阶段**：博士生\n")

    profiles_dir = isolated_workspace / "profile_helper" / "profiles"
    md_files = list(profiles_dir.glob("*.md"))
    assert len(md_files) >= 1


def test_sb_b09_messages_restored_from_disk(isolated_workspace):
    """SB-B09：messages 写磁盘后重建 session 可恢复"""
    session_id, session = profile_sessions.get_or_create(None)
    session["messages"].append({"role": "user", "content": "测试消息"})
    profile_sessions.save_messages(session)

    # 清除内存中的 session
    profile_sessions._sessions.clear()

    # 重建
    _, restored = profile_sessions.get_or_create(session_id)
    messages = profile_sessions.load_messages(session_id, None)
    assert any(m.get("content") == "测试消息" for m in messages)


# ──────────────────────────────────────────────
# SB-A01 ~ SB-A15  API 端点测试
# ──────────────────────────────────────────────

def test_sb_a01_session_creates_new(client, auth_override):
    """SB-A01：GET /session 无 session_id 时创建新 session"""
    resp = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    assert "session_id" in resp.json()


def test_sb_a02_session_reuses_existing(client, auth_override):
    """SB-A02：GET /session 传已有 session_id 时复用"""
    r1 = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r1.json()["session_id"]
    r2 = client.get(f"/profile-helper/session?session_id={sid}",
                    headers={"Authorization": "Bearer test-token"})
    assert r2.json()["session_id"] == sid


@pytest.mark.parametrize("bad_id", ["undefined", "null", "none", ""])
def test_sb_a03_session_ignores_invalid_ids(client, auth_override, bad_id):
    """SB-A03：无效 session_id 被忽略，创建新 session"""
    url = f"/profile-helper/session?session_id={bad_id}" if bad_id else "/profile-helper/session"
    resp = client.get(url, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    new_sid = resp.json()["session_id"]
    assert new_sid and new_sid.lower() not in {"undefined", "null", "none", ""}


def test_sb_a04_auth_required_returns_401(client, monkeypatch):
    """SB-A04：AUTH_MODE=jwt 时未认证返回 401"""
    monkeypatch.setenv("AUTH_MODE", "jwt")
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    resp = client.get("/profile-helper/session")
    assert resp.status_code == 401


def test_sb_a05_get_profile_returns_content(client, auth_override):
    """SB-A05：GET /profile/{session_id} 返回 profile 和 forum_profile"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    resp = client.get(f"/profile-helper/profile/{sid}",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert "profile" in body
    assert "forum_profile" in body


def test_sb_a06_structured_endpoint_returns_completion(client, auth_override):
    """SB-A06：GET /profile/{session_id}/structured 含 completion 和 identity"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    resp = client.get(f"/profile-helper/profile/{sid}/structured",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    assert "completion" in data
    assert "identity" in data


def test_sb_a07_famous_scientists_returns_top3(client, auth_override):
    """SB-A07：GET /profile/{session_id}/scientists/famous 返回 top3 + scatter_data"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    resp = client.get(f"/profile-helper/profile/{sid}/scientists/famous",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert "top3" in body
    assert len(body["top3"]) == 3
    assert "scatter_data" in body
    assert "user_point" in body


def test_sb_a08_famous_scientists_blank_profile_uses_defaults(client, auth_override):
    """SB-A08：画像无量表数据时使用默认值正常返回"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    resp = client.get(f"/profile-helper/profile/{sid}/scientists/famous",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    # 空画像使用默认 csi=0, rai=25
    assert resp.json()["user_point"]["csi"] == 0
    assert resp.json()["user_point"]["rai"] == 25


def test_sb_a09_scales_submit_and_retrieve(client, auth_override):
    """SB-A09：POST /scales/submit 写入量表，GET /scales/{id} 可读回"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]

    submit = client.post(
        "/profile-helper/scales/submit",
        headers={"Authorization": "Bearer test-token"},
        json={
            "session_id": sid,
            "scale_name": "rcss",
            "answers": {"A1": 6, "A2": 6, "A3": 6, "A4": 5, "B1": 4, "B2": 4, "B3": 4, "B4": 4},
            "scores": {"integration": 23, "depth": 16, "csi": 7},
            "result_summary": {"CSI": 7, "type": "倾向整合型"},
        },
    )
    assert submit.status_code == 200
    assert submit.json()["ok"] is True

    get_resp = client.get(
        f"/profile-helper/scales/{sid}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert get_resp.status_code == 200
    scales = get_resp.json()["scales"]
    assert "rcss" in scales
    assert scales["rcss"]["scores"]["csi"] == 7


def test_sb_a10_scales_get_multiple(client, auth_override):
    """SB-A10：提交多个量表后 GET /scales 全部返回"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]

    for scale in ["rcss", "ams", "mini_ipip"]:
        client.post(
            "/profile-helper/scales/submit",
            headers={"Authorization": "Bearer test-token"},
            json={
                "session_id": sid,
                "scale_name": scale,
                "answers": {},
                "scores": {"test": 1.0},
            },
        )

    resp = client.get(f"/profile-helper/scales/{sid}",
                      headers={"Authorization": "Bearer test-token"})
    scales = resp.json()["scales"]
    assert all(s in scales for s in ["rcss", "ams", "mini_ipip"])


def test_sb_a11_scales_submit_unknown_session_creates_new(client, auth_override):
    """SB-A11：提交量表到不存在的 session 时 API 会 get_or_create 新 session，返回 200"""
    # 设计决策：_get_session_for_user 使用 get_or_create，不存在就创建，不返回 404
    resp = client.post(
        "/profile-helper/scales/submit",
        headers={"Authorization": "Bearer test-token"},
        json={
            "session_id": "00000000-0000-0000-0000-000000000000",
            "scale_name": "rcss",
            "answers": {},
            "scores": {"test": 1.0},
        },
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_sb_a12_download_profile_returns_markdown(client, auth_override):
    """SB-A12：GET /download/{session_id} 返回 Markdown 文本"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    resp = client.get(f"/profile-helper/download/{sid}",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    assert "text" in resp.headers.get("content-type", "")


def test_sb_a13_download_forum_profile(client, auth_override):
    """SB-A13：GET /download/{session_id}/forum 返回论坛分身"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    session = profile_sessions.get(sid)
    session["forum_profile"] = "# 我的分身\n\n## Identity\n\n测试分身"

    resp = client.get(f"/profile-helper/download/{sid}/forum",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200


def test_sb_a14_publish_anonymous_returns_401(client, anon_auth_override, isolated_workspace):
    """SB-A14：匿名用户发布数字分身返回 401"""
    r = client.get("/profile-helper/session")
    sid = r.json()["session_id"]
    session = profile_sessions.get(sid)
    if session:
        session["forum_profile"] = "# 匿名\n\n## Identity\n\n测试"

    resp = client.post(
        "/profile-helper/publish-to-library",
        json={"session_id": sid, "visibility": "private", "exposure": "brief", "display_name": "匿名"},
    )
    assert resp.status_code == 401


def test_sb_a15_session_reset_clears_data(client, auth_override):
    """SB-A15：POST /session/reset/{session_id} 清空 session 数据"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    session = profile_sessions.get(sid)
    if session:
        session["messages"].append({"role": "user", "content": "测试消息"})

    resp = client.post(
        f"/profile-helper/session/reset/{sid}",
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200

    reset_session = profile_sessions.get(sid)
    if reset_session:
        # 重置后消息历史应为空
        assert len(reset_session.get("messages", [])) == 0


# ──────────────────────────────────────────────
# 边界与异常  SB-X 系列
# ──────────────────────────────────────────────

def test_sb_x01_profile_with_special_chars():
    """SB-X01：画像含特殊字符不抛出异常"""
    from app.services.profile_helper.profile_parser import parse_profile
    md = "# 科研人员画像 — 测试🧪\n\n## 一、基础身份\n\n- **研究阶段**：博士生（∑ ≥ 2）\n"
    result = parse_profile(md)
    assert result["identity"]["research_stage"] == "博士生（∑ ≥ 2）"


def test_sb_x04_chat_blocks_empty_message(client, auth_override):
    """SB-X04：POST /chat/blocks 消息为空时返回 400"""
    r = client.get("/profile-helper/session", headers={"Authorization": "Bearer test-token"})
    sid = r.json()["session_id"]
    resp = client.post(
        "/profile-helper/chat/blocks",
        headers={"Authorization": "Bearer test-token"},
        json={"session_id": sid, "message": ""},
    )
    assert resp.status_code == 400
