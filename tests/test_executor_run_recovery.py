import asyncio
import importlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient


def setup_executor(monkeypatch, tmp_path):
    monkeypatch.setenv('WORKSPACE_BASE', str(tmp_path))
    monkeypatch.setenv('TOPICLAB_EXECUTOR_TOKEN', 'test-service-token')
    from app.api import executor, executor_runs
    importlib.reload(executor_runs)
    executor = importlib.reload(executor)
    app = FastAPI()
    app.include_router(executor.router, prefix='/executor')
    return executor, executor_runs, app


def test_completed_executor_run_is_replayed_without_computation(monkeypatch, tmp_path):
    executor, records, app = setup_executor(monkeypatch, tmp_path)
    calls = []
    async def compute(**kwargs):
        calls.append(kwargs)
        return {'completed_at': datetime.now(timezone.utc).isoformat(), 'turns_count': 0}
    monkeypatch.setattr(executor, 'run_discussion_for_topic', compute)
    body = {'run_id': 'run_' + 'a' * 32, 'topic_id': 'topic-1', 'topic_title': 'Title'}
    with TestClient(app, headers={"X-TopicLab-Executor-Token":"test-service-token"}) as client:
        first = client.post('/executor/discussions', json=body)
        replay = client.post('/executor/discussions', json=body)
        assert first.status_code == replay.status_code == 200
        assert first.json() == replay.json()
        assert len(calls) == 1
        assert client.get('/executor/runs/' + body['run_id']).json()['status'] == 'completed'
        assert client.post('/executor/discussions', json={**body, 'topic_title': 'Changed'}).status_code == 409


def test_cancellation_stops_the_actual_task(monkeypatch, tmp_path):
    executor, records, app = setup_executor(monkeypatch, tmp_path)
    started, stopped, sync_stopped = threading.Event(), threading.Event(), threading.Event()
    async def sync(*args):
        try:
            await asyncio.sleep(30)
        finally:
            sync_stopped.set()
    monkeypatch.setattr(executor, "_run_sync_loop_until_done", sync)
    async def compute(**kwargs):
        started.set()
        try:
            await asyncio.sleep(30)
        finally:
            stopped.set()
    monkeypatch.setattr(executor, 'run_discussion_for_topic', compute)
    run_id = 'run_' + 'b' * 32
    with TestClient(app, headers={"X-TopicLab-Executor-Token":"test-service-token"}) as client, ThreadPoolExecutor() as pool:
        future = pool.submit(client.post, '/executor/discussions', json={
            'run_id': run_id, 'topic_id': 'topic-2', 'topic_title': 'Title', 'topiclab_sync_url':'http://account.test'})
        assert started.wait(3)
        cancelled = client.post(f'/executor/runs/{run_id}/cancel')
        assert cancelled.status_code == 200
        assert stopped.wait(3)
        assert sync_stopped.wait(3)
        assert future.result(timeout=3).status_code == 409
        assert client.get(f'/executor/runs/{run_id}').json()['status'] == 'cancelled'


def test_interrupted_receipt_never_restarts_the_run(monkeypatch, tmp_path):
    executor, records, app = setup_executor(monkeypatch, tmp_path)
    run_id = 'run_' + 'c' * 32
    body = {'run_id': run_id, 'topic_id': 'topic-3', 'topic_title': 'Title'}
    request = executor.ExecutorDiscussionRequest(**body)
    record, _ = records.reserve_run(run_id, request.model_dump())
    record['updated_at'] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    records.path_for(run_id).write_text(json.dumps(record))
    with TestClient(app, headers={"X-TopicLab-Executor-Token":"test-service-token"}) as client:
        assert client.get(f'/executor/runs/{run_id}').json()['status'] == 'interrupted'
        assert client.post('/executor/discussions', json=body).status_code == 409


def test_executor_rejects_calls_without_service_authentication(monkeypatch, tmp_path):
    executor, records, app = setup_executor(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.post('/executor/discussions',json={'topic_id':'one','topic_title':'Title'}).status_code == 401
        assert not (tmp_path/'executor-runs').exists()
        monkeypatch.delenv('TOPICLAB_EXECUTOR_TOKEN')
        assert client.get('/executor/runs/'+'run_'+'a'*32).status_code == 503
