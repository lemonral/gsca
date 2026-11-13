#!/bin/sh
set -eu

PROJECT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PROJECT_DIR"

compose() {
  docker compose --project-directory "$PROJECT_DIR" \
    --env-file "$PROJECT_DIR/.env" -f "$PROJECT_DIR/compose.yaml" "$@"
}

case "${1:-help}" in
  up)
    compose config --quiet
    compose up -d --build
    ;;
  down)
    compose down
    ;;
  logs)
    compose logs --tail=150 -f agent
    ;;
  status)
    compose ps
    ;;
  test)
    if [ -x "$PROJECT_DIR/.venv/bin/python" ]; then
      TEST_DIR=$(mktemp -d "${TMPDIR:-/tmp}/gsca-tests.XXXXXX")
      trap 'rm -rf "$TEST_DIR"' EXIT HUP INT TERM
      env AGENT_DATA_DIR="$TEST_DIR/data" APP_ACCESS_TOKEN_FILE="$TEST_DIR/access-token" \
        MODEL_TOKEN_FILE= MODEL_BASE_URL=http://127.0.0.1:9 \
        "$PROJECT_DIR/.venv/bin/python" -m unittest discover -s tests -v
    else
      compose run --rm --no-deps \
        -e AGENT_DATA_DIR=/tmp/gsca-tests \
        -e APP_ACCESS_TOKEN_FILE=/tmp/gsca-test-access \
        -e MODEL_TOKEN_FILE= -e MODEL_BASE_URL=http://127.0.0.1:9 \
        agent python -m unittest discover -s tests -v
    fi
    ;;
  *)
    printf '%s\n' 'Usage: ./scripts/manage.sh {up|down|logs|status|test}'
    ;;
esac
