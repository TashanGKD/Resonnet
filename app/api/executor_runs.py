"""Durable executor receipts prevent replaying paid runs after a lost response."""
import asyncio
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException
from app.core.config import get_workspace_base

tasks: dict[str, asyncio.Task] = {}


def path_for(run_id: str) -> Path:
    if not re.fullmatch(r'run_[a-f0-9]{32}', run_id):
        raise HTTPException(400, 'Invalid run_id')
    directory = get_workspace_base() / 'executor-runs'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory / (run_id + '.json')


def write_record(record):
    path = path_for(record['run_id'])
    record['updated_at'] = datetime.now(timezone.utc).isoformat()
    temporary = path.with_suffix('.' + uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(record, ensure_ascii=False))
    temporary.chmod(0o600)
    os.replace(temporary, path)


def read_record(run_id):
    path = path_for(run_id)
    if not path.exists():
        raise HTTPException(404, 'Run not found')
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError):
        raise HTTPException(503, 'Run receipt unavailable; do not resubmit automatically')
    if record['status'] == 'running' and run_id not in tasks:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record['updated_at'])).total_seconds()
        if age > 90:
            record['status'] = 'interrupted'
            record['error_code'] = 'executor_interrupted'
            write_record(record)
    return record


def reserve_run(run_id, payload):
    path = path_for(run_id)
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    record = {'run_id': run_id, 'topic_id': payload['topic_id'], 'input_hash': fingerprint,
              'status': 'running', 'updated_at': datetime.now(timezone.utc).isoformat()}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        existing = read_record(run_id)
        if existing['input_hash'] != fingerprint:
            raise HTTPException(409, 'run_id already used with different input')
        if existing['status'] == 'completed':
            return existing, False
        raise HTTPException(409, {'code': 'run_already_accepted', 'run_id': run_id,
                                 'status': existing['status'], 'retryable': False})
    with os.fdopen(fd, 'w') as output:
        json.dump(record, output)
        output.flush()
        os.fsync(output.fileno())
    return record, True


async def heartbeat(record):
    while True:
        await asyncio.sleep(15)
        write_record(record)
