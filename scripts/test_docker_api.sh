#!/usr/bin/env bash
# Test API calls against Docker deployment.
# Usage: ./scripts/test_docker_api.sh
# Prerequisite: Docker daemon running, .env with required vars (or script creates minimal .env for startup)

set -e

cd "$(dirname "$0")/.."
BASE_URL="${BASE_URL:-http://localhost:8000}"

BASE_HOST="$(python3 - "$BASE_URL" <<'PY'
import sys
from urllib.parse import urlparse

print((urlparse(sys.argv[1]).hostname or "").lower())
PY
)"
case "$BASE_HOST" in
  ""|localhost|127.0.0.1|::1|*.local)
    ;;
  *)
    if [[ "${BACKEND_DOCKER_API_ALLOW_REMOTE_WRITES:-0}" != "1" ]]; then
      echo "Refusing to run write API tests against non-local BASE_URL=$BASE_URL" >&2
      echo "Set BACKEND_DOCKER_API_ALLOW_REMOTE_WRITES=1 only for an intentional remote test." >&2
      exit 1
    fi
    ;;
esac

# Ensure .env exists for docker-compose (app requires ANTHROPIC_* and AI_GENERATION_* at startup)
if [[ ! -f .env ]]; then
  echo "Creating minimal .env for Docker (app won't call real APIs with placeholder keys)..."
  cat > .env << 'ENVEOF'
ANTHROPIC_API_KEY=test
ANTHROPIC_BASE_URL=
ANTHROPIC_MODEL=
AI_GENERATION_BASE_URL=https://api.example.com
AI_GENERATION_API_KEY=test
AI_GENERATION_MODEL=test-model
WORKSPACE_BASE=./workspace
ENVEOF
fi

echo "Building and starting Docker..."
docker compose up --build -d

cleanup() {
  echo "Stopping Docker..."
  docker compose down
}
trap cleanup EXIT

echo "Waiting for service..."
for i in {1..30}; do
  if curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health" 2>/dev/null | grep -q 200; then
    echo "Service ready."
    break
  fi
  if [[ $i -eq 30 ]]; then
    echo "Timeout waiting for service."
    docker compose logs --tail 50
    exit 1
  fi
  sleep 1
done

echo ""
echo "=== API Tests ==="

# Health
echo -n "GET /health ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/health")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

# Topics
echo -n "GET /topics ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/topics")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

echo -n "POST /topics ... "
resp=$(curl -s -w "\n%{http_code}" -X POST "$BASE_URL/topics" -H "Content-Type: application/json" -d '{"title":"Docker test","body":"Test topic"}')
code=$(echo "$resp" | tail -n1)
body=$(echo "$resp" | sed '$d')
[[ "$code" == "201" ]] && echo "OK ($code)" || { echo "FAIL ($code) $body"; exit 1; }
TOPIC_ID=$(echo "$body" | python3 -c "import sys,json; print(json.load(sys.stdin).get('id',''))")
[[ -z "$TOPIC_ID" ]] && { echo "No topic id"; exit 1; }

echo -n "GET /topics/$TOPIC_ID ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/topics/$TOPIC_ID")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

# Moderator modes
echo -n "GET /moderator-modes ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/moderator-modes")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

# Experts
echo -n "GET /experts ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/experts")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

# Topic experts
echo -n "GET /topics/$TOPIC_ID/experts ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/topics/$TOPIC_ID/experts")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

# Topic experts - add preset
echo -n "POST /topics/$TOPIC_ID/experts (preset) ... "
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/topics/$TOPIC_ID/experts" \
  -H "Content-Type: application/json" -d '{"source":"preset","preset_name":"physicist"}')
[[ "$code" == "201" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

# Posts
echo -n "POST /topics/$TOPIC_ID/posts ... "
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE_URL/topics/$TOPIC_ID/posts" \
  -H "Content-Type: application/json" -d '{"author":"tester","body":"Hello"}')
[[ "$code" == "201" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

echo -n "GET /topics/$TOPIC_ID/posts ... "
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/topics/$TOPIC_ID/posts")
[[ "$code" == "200" ]] && echo "OK ($code)" || { echo "FAIL ($code)"; exit 1; }

echo ""
echo "All API tests passed."
