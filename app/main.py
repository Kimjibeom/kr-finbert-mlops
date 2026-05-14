"""
app/main.py — FastAPI 애플리케이션 진입점

설계 근거:
1. lifespan 이벤트로 모델 사전 로딩 → 첫 요청 지연(cold start) 제거.
2. Prometheus Instrumentator로 RED 메트릭(Rate, Error, Duration) 자동 수집.
3. 구조화된 JSON 로깅 → Loki/ELK에서 필드 기반 검색 가능.
4. /health 엔드포인트로 K8s readiness/liveness probe 지원.
5. CORS 미들웨어 → 프론트엔드 직접 호출 시 대비.
"""

from __future__ import annotations

import logging
import sys
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pythonjsonlogger import json as json_logger

from app.model import get_model_manager
from app.schemas import (
    BatchPredictRequest,
    BatchPredictResponse,
    HealthResponse,
    PredictRequest,
    PredictResponse,
    SentimentResult,
)

# ── 구조화 JSON 로깅 설정 ─────────────────────────────
# 이유: 일반 텍스트 로그 대비 JSON 로그는
# Loki/ELK에서 필드 기반 검색·집계가 가능하여 MTTR 단축에 유리.
def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    formatter = json_logger.JsonFormatter(
        fmt="%(asctime)s %(name)s %(levelname)s %(message)s",
        rename_fields={
            "asctime": "timestamp",
            "levelname": "level",
            "name": "logger",
        },
    )
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.INFO)


_setup_logging()
logger = logging.getLogger("kr_finbert")


# ── Lifespan 이벤트 ───────────────────────────────────
# 이유: on_event("startup")은 deprecated, lifespan이 공식 권장 패턴.
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator:  # noqa: ARG001
    """애플리케이션 시작 시 모델 로드, 종료 시 자원 해제."""
    manager = get_model_manager()
    manager.load()
    logger.info("FastAPI 서버 시작 — 모델 준비 완료")
    yield
    manager.unload()
    logger.info("FastAPI 서버 종료 — 자원 해제 완료")


# ── FastAPI 인스턴스 ──────────────────────────────────
app = FastAPI(
    title="KR-FinBert Sentiment Analysis API",
    description=(
        "한국어 금융 감성 분석 서비스 — snunlp/KR-FinBert-SC 모델 기반. "
        "CPU 추론 최적화, Prometheus 메트릭 내장."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

# ── CORS ──────────────────────────────────────────────
# 이유: 내부 대시보드 또는 프론트엔드에서 직접 API 호출 시
# CORS 에러 방지. 프로덕션에서는 allow_origins를 특정 도메인으로 제한.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Prometheus 계측 ───────────────────────────────────
# 이유: prometheus-fastapi-instrumentator는 요청별
# duration, status code, in-progress 등을 자동 계측하여
# 별도 미들웨어 작성 불필요. /metrics 엔드포인트 자동 노출.
instrumentator = Instrumentator(
    should_group_status_codes=True,
    should_instrument_requests_inprogress=True,
    excluded_handlers=["/health", "/docs", "/openapi.json"],
    inprogress_name="http_requests_inprogress",
    inprogress_labels=True,
)
instrumentator.instrument(app).expose(app, endpoint="/metrics")


# ── 요청 로깅 미들웨어 ────────────────────────────────
@app.middleware("http")
async def log_requests(request: Request, call_next):
    """모든 HTTP 요청에 대해 method, path, status, latency를 로깅.

    이유: 액세스 로그를 구조화하여 Loki에서 경로별 트래픽 분석 가능.
    """
    start = time.monotonic()
    response = await call_next(request)
    elapsed_ms = (time.monotonic() - start) * 1000

    logger.info(
        "request",
        extra={
            "method": request.method,
            "path": str(request.url.path),
            "status_code": response.status_code,
            "elapsed_ms": round(elapsed_ms, 2),
            "client_ip": request.client.host if request.client else "unknown",
        },
    )
    return response


# ── 글로벌 예외 핸들러 ─────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """처리되지 않은 예외를 JSON 응답으로 반환.

    이유: 스택 트레이스가 클라이언트에 노출되지 않도록 보호하면서
    로그에는 상세 정보를 기록하여 디버깅 지원.
    """
    logger.exception(
        "unhandled_exception",
        extra={
            "path": str(request.url.path),
            "error": str(exc),
        },
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ── 엔드포인트: 헬스 체크 ─────────────────────────────
@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["System"],
    summary="서비스 헬스 체크",
)
async def health_check():
    """K8s liveness/readiness probe 및 로드밸런서 헬스 체크용.

    모델이 로드되지 않았으면 503을 반환하여
    트래픽 유입을 차단합니다.
    """
    manager = get_model_manager()
    if not manager.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="모델이 아직 로드되지 않았습니다",
        )
    return HealthResponse(
        status="ok",
        model_loaded=True,
        model_name=manager.model_name,
    )


# ── 엔드포인트: 단일 예측 ─────────────────────────────
@app.post(
    "/predict",
    response_model=PredictResponse,
    tags=["Prediction"],
    summary="단일 텍스트 감성 분석",
)
async def predict(request: PredictRequest):
    """단일 한국어 금융 텍스트의 감성을 분석합니다.

    - 입력: 한국어 금융 관련 텍스트 (최대 2000자)
    - 출력: 감성 레이블 (positive/negative/neutral) + 확률 + 소요시간
    """
    manager = get_model_manager()
    if not manager.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="모델이 아직 로드되지 않았습니다",
        )

    result = await manager.predict(request.text)

    return PredictResponse(
        text=request.text,
        result=SentimentResult(
            label=result["label"],
            score=result["score"],
        ),
        elapsed_ms=result["elapsed_ms"],
    )


# ── 엔드포인트: 배치 예측 ─────────────────────────────
@app.post(
    "/predict/batch",
    response_model=BatchPredictResponse,
    tags=["Prediction"],
    summary="배치 텍스트 감성 분석",
)
async def predict_batch(request: BatchPredictRequest):
    """최대 32건의 텍스트를 한 번에 분석합니다.

    CPU 환경에서 메모리 OOM을 방지하기 위해 32건으로 제한.
    """
    manager = get_model_manager()
    if not manager.is_loaded:
        raise HTTPException(
            status_code=503,
            detail="모델이 아직 로드되지 않았습니다",
        )

    start = time.monotonic()
    results = await manager.predict_batch(request.texts)
    total_elapsed = (time.monotonic() - start) * 1000

    responses = [
        PredictResponse(
            text=text,
            result=SentimentResult(
                label=r["label"],
                score=r["score"],
            ),
            elapsed_ms=r["elapsed_ms"],
        )
        for text, r in zip(request.texts, results)
    ]

    return BatchPredictResponse(
        results=responses,
        total_elapsed_ms=round(total_elapsed, 2),
    )
