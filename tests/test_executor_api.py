from __future__ import annotations

import importlib
from fastapi.testclient import TestClient


def test_executor_endpoints_work_without_database(monkeypatch, tmp_path):
    monkeypatch.setenv("RESONNET_MODE", "executor")
    monkeypatch.setenv("TOPICLAB_EXECUTOR_TOKEN", "test-service-token")
    monkeypatch.delenv("TOPICDATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WORKSPACE_BASE", str(tmp_path / "workspace"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("AI_GENERATION_BASE_URL", "https://example.com")
    monkeypatch.setenv("AI_GENERATION_API_KEY", "test")
    monkeypatch.setenv("AI_GENERATION_MODEL", "test")

    import app.api.executor as executor_module
    import main as main_module

    async def fake_run_discussion_for_topic(**kwargs):
        ws_path = tmp_path / "workspace" / "topics" / kwargs["topic_id"] / "shared" / "turns"
        ws_path.mkdir(parents=True, exist_ok=True)
        (ws_path / "round1_physicist.md").write_text("第一轮观点", encoding="utf-8")
        summary_path = ws_path.parent / "discussion_summary.md"
        summary_path.write_text("总结", encoding="utf-8")
        return {
            "discussion_history": "## Round 1 - Physicist\n\n第一轮观点",
            "discussion_summary": "总结",
            "turns_count": 1,
            "cost_usd": 0.01,
            "completed_at": "2026-03-14T00:00:00+00:00",
        }

    async def fake_run_expert_reply(**kwargs):
        return {"reply_body": "专家回复", "num_turns": 1, "total_cost_usd": 0.01}

    executor_module = importlib.reload(executor_module)
    monkeypatch.setattr(executor_module, "run_discussion_for_topic", fake_run_discussion_for_topic)
    monkeypatch.setattr(executor_module, "run_expert_reply", fake_run_expert_reply)
    main_module = importlib.reload(main_module)

    with TestClient(main_module.app, headers={"X-TopicLab-Executor-Token":"test-service-token"}) as client:
        bootstrap = client.post(
            "/executor/topics/bootstrap",
            json={"topic_id": "topic-1", "topic_title": "Title", "topic_body": "Body", "num_rounds": 1},
        )
        assert bootstrap.status_code == 200, bootstrap.text

        discussion = client.post(
            "/executor/discussions",
            json={
                "topic_id": "topic-1",
                "topic_title": "Title",
                "topic_body": "Body",
                "num_rounds": 1,
                "expert_names": ["physicist"],
                "posts_context": "# Posts Context\n\n_No posts yet._\n",
            },
        )
        assert discussion.status_code == 200, discussion.text
        assert discussion.json()["turns_count"] == 1

        generated_dir = tmp_path / "workspace" / "topics" / "topic-1" / "shared" / "generated_images"
        generated_dir.mkdir(parents=True, exist_ok=True)
        (generated_dir / "round1.png").write_bytes(b"fake-image")

        snapshot = client.get("/executor/discussions/topic-1/snapshot")
        assert snapshot.status_code == 200, snapshot.text
        assert snapshot.json()["turns_count"] == 1
        assert snapshot.json()["discussion_summary"] == "总结"
        assert snapshot.json()["generated_images"] == ["round1.png"]

        reply = client.post(
            "/executor/expert-replies",
            json={
                "topic_id": "topic-1",
                "topic_title": "Title",
                "topic_body": "Body",
                "expert_name": "physicist",
                "expert_label": "Physicist",
                "user_post_id": "user-post-1",
                "user_author": "alice",
                "user_question": "请回答",
                "reply_post_id": "reply-post-1",
                "reply_created_at": "2026-03-14T00:00:00+00:00",
            },
        )
        assert reply.status_code == 200, reply.text
        assert reply.json()["reply_body"] == "专家回复"


def test_bootstrap_with_ai_generated_roles(monkeypatch, tmp_path):
    """Bootstrap with use_ai_generated_roles creates workspace without default experts."""
    monkeypatch.setenv("RESONNET_MODE", "executor")
    monkeypatch.setenv("TOPICLAB_EXECUTOR_TOKEN", "test-service-token")
    monkeypatch.delenv("TOPICDATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WORKSPACE_BASE", str(tmp_path / "workspace"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("AI_GENERATION_BASE_URL", "https://example.com")
    monkeypatch.setenv("AI_GENERATION_API_KEY", "test")
    monkeypatch.setenv("AI_GENERATION_MODEL", "test")

    import importlib
    import main as main_module

    with TestClient(main_module.app, headers={"X-TopicLab-Executor-Token":"test-service-token"}) as client:
        bootstrap = client.post(
            "/executor/topics/bootstrap",
            json={
                "topic_id": "topic-ai-1",
                "topic_title": "AI Topic",
                "topic_body": "Body",
                "num_rounds": 5,
                "use_ai_generated_roles": True,
            },
        )
        assert bootstrap.status_code == 200, bootstrap.text

        ws_path = tmp_path / "workspace" / "topics" / "topic-ai-1"
        agents_dir = ws_path / "agents"
        assert not agents_dir.exists() or len(list(agents_dir.iterdir())) == 0


def test_set_generated_experts(monkeypatch, tmp_path):
    """POST /executor/topics/{id}/experts/generated adds AI-generated experts."""
    monkeypatch.setenv("RESONNET_MODE", "executor")
    monkeypatch.setenv("TOPICLAB_EXECUTOR_TOKEN", "test-service-token")
    monkeypatch.delenv("TOPICDATABASE_URL", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("WORKSPACE_BASE", str(tmp_path / "workspace"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("AI_GENERATION_BASE_URL", "https://example.com")
    monkeypatch.setenv("AI_GENERATION_API_KEY", "test")
    monkeypatch.setenv("AI_GENERATION_MODEL", "test")

    import main as main_module

    with TestClient(main_module.app, headers={"X-TopicLab-Executor-Token":"test-service-token"}) as client:
        resp = client.post(
            f"/executor/topics/topic-gen-1/experts/generated",
            json={
                "experts": [
                    {
                        "name": "industry_analyst",
                        "label": "产业分析师",
                        "description": "产业视角分析",
                        "role_content": "# 产业分析师\n\n## Identity\n\n产业分析师。\n\n## Expertise\n\n- 产业趋势\n\n## Thinking Style\n\n- 数据驱动",
                    },
                    {
                        "name": "tech_researcher",
                        "label": "技术研究员",
                        "description": "技术研究视角",
                        "role_content": "# 技术研究员\n\n## Identity\n\n技术研究员。",
                    },
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["expert_names"] == ["industry_analyst", "tech_researcher"]

        ws_path = tmp_path / "workspace" / "topics" / "topic-gen-1"
        role1 = ws_path / "agents" / "industry_analyst" / "role.md"
        role2 = ws_path / "agents" / "tech_researcher" / "role.md"
        assert role1.exists()
        assert role2.exists()
        assert "产业分析师" in role1.read_text()
