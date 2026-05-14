# ============================================================
# Dockerfile — KR-FinBert 감성 분석 서비스
#
# 설계 근거:
# 1. python:3.10-slim 기반: 전체 python 이미지 대비 ~70% 용량 절감.
#    Alpine은 musl libc 호환성 문제로 PyTorch에서 제외.
# 2. 비루트 사용자(appuser): 컨테이너 탈출 공격 시 권한 최소화.
# 3. 단일 스테이지 구성: PyTorch CPU 휠이 ~800MB로,
#    multi-stage 시 COPY --from 오버헤드가 크므로
#    레이어 캐싱 최적화로 대체.
# 4. 모델 다운로드를 빌드 시 수행하여 런타임 cold start 제거.
# ============================================================

FROM python:3.10-slim AS base

# ── 시스템 의존성 (최소) ──
# curl: 헬스 체크, tini: PID 1 좀비 프로세스 방지
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        curl \
        tini \
    && rm -rf /var/lib/apt/lists/*

# ── 비루트 사용자 생성 ──
# 보안: 컨테이너 내 프로세스를 root가 아닌 전용 사용자로 실행
RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --shell /bin/bash --create-home appuser

WORKDIR /app

# ── 의존성 설치 (레이어 캐싱 최적화) ──
# requirements.txt가 변경되지 않으면 이 레이어를 재사용하여 빌드 시간 단축
COPY requirements.txt .

# PyTorch CPU 전용 인덱스를 사용하여 CUDA 바이너리 다운로드 방지
RUN pip install --no-cache-dir \
    --extra-index-url https://download.pytorch.org/whl/cpu \
    -r requirements.txt

# ── 모델 사전 다운로드 ──
# 이유: 런타임에 HuggingFace 다운로드 시 네트워크 장애 → 서비스 불가.
# 빌드 시점에 모델을 컨테이너에 포함하여 에어갭 환경에서도 동작.
RUN python -c "\
from transformers import AutoTokenizer, AutoModelForSequenceClassification; \
AutoTokenizer.from_pretrained('snunlp/KR-FinBert-SC'); \
AutoModelForSequenceClassification.from_pretrained('snunlp/KR-FinBert-SC')"

# ── 애플리케이션 코드 복사 ──
COPY app/ ./app/

# ── 소유권 변경 후 비루트 전환 ──
RUN chown -R appuser:appuser /app
USER appuser

# ── HuggingFace 캐시 디렉토리 설정 ──
# 비루트 사용자가 접근 가능한 경로로 캐시 지정
ENV HF_HOME=/home/appuser/.cache/huggingface
ENV TRANSFORMERS_CACHE=/home/appuser/.cache/huggingface

# ── 실행 설정 ──
# tini: PID 1 문제(좀비 프로세스) 해결
# uvicorn: ASGI 서버, gunicorn 없이 단독 실행
#   --workers 1: CPU 추론 시 worker 간 모델 복제는 메모리 낭비
#   --timeout-keep-alive 30: 로드밸런서 keep-alive 호환
EXPOSE 8000

ENTRYPOINT ["tini", "--"]

CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "1", \
     "--timeout-keep-alive", "30", \
     "--access-log"]

# ── 헬스 체크 ──
# Docker 자체 헬스 체크 + K8s probe 이중 안전장치
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ── 메타데이터 ──
LABEL maintainer="MLOps Team" \
      description="KR-FinBert-SC Sentiment Analysis Service" \
      version="1.0.0"
