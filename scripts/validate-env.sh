#!/usr/bin/env bash
#
# validate-env.sh — Docker 배포 전 .env 검증
#
# 사용: bash scripts/validate-env.sh [.env 경로]
#
# BUG-017 재발 방지:
#   - Docker 서비스 URL에 localhost 사용 검출
#   - 필수 환경변수 존재 확인
#

set -euo pipefail

ENV_FILE="${1:-.env}"
ERRORS=0

if [[ ! -f "$ENV_FILE" ]]; then
    echo "❌ .env 파일이 없습니다: $ENV_FILE"
    exit 1
fi

echo "🔍 Docker .env 검증: $ENV_FILE"
echo "────────────────────────────────────"

# 1. Docker 서비스 URL에 localhost 사용 검출
DOCKER_URL_KEYS="RACHEL_WEBHOOK_URL|BFF_URL|DASHBOARD_URL"
while IFS= read -r line; do
    # 빈 줄, 주석 건너뛰기
    [[ -z "$line" || "$line" =~ ^# ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    if echo "$key" | grep -qiE "$DOCKER_URL_KEYS"; then
        if echo "$value" | grep -qi "localhost"; then
            echo "⚠️  $key=$value"
            echo "   → Docker 내부에서 localhost는 호스트에 도달 불가"
            echo "   → host.docker.internal 사용 권장"
            ERRORS=$((ERRORS + 1))
        fi
    fi
done < "$ENV_FILE"

# 2. 필수 환경변수 존재 확인
REQUIRED_KEYS=("DATABASE_URL" "EXCHANGE")
for key in "${REQUIRED_KEYS[@]}"; do
    if ! grep -q "^${key}=" "$ENV_FILE"; then
        echo "❌ 필수 환경변수 누락: $key"
        ERRORS=$((ERRORS + 1))
    fi
done

# 3. DATABASE_URL에 localhost 주의 (Docker 컨테이너 간 통신)
DB_URL=$(grep "^DATABASE_URL=" "$ENV_FILE" 2>/dev/null | head -1 | cut -d= -f2-)
if [[ -n "$DB_URL" ]] && echo "$DB_URL" | grep -q "localhost"; then
    echo "⚠️  DATABASE_URL에 localhost 사용 중"
    echo "   → Docker 컨테이너 간 통신 시 서비스명(예: trader-postgres) 사용 권장"
    ERRORS=$((ERRORS + 1))
fi

echo "────────────────────────────────────"
if [[ $ERRORS -eq 0 ]]; then
    echo "✅ 검증 완료: 문제 없음"
else
    echo "⚠️  $ERRORS건의 문제 발견 — 배포 전 확인 필요"
    echo ""
    echo "💡 .env 변경 후 반드시 'docker compose up -d' (docker restart 불가)"
    echo "💡 확인: docker exec <컨테이너> python3 -c \"import os; print(os.getenv('KEY'))\""
fi
