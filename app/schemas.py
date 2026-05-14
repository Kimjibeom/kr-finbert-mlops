"""
app/schemas.py — 요청·응답 스키마 정의

설계 근거:
- Pydantic v2 BaseModel을 사용하여 런타임 타입 검증 + OpenAPI 스키마 자동 생성.
- 감성 분석의 입력은 단일 텍스트 또는 배치이며,
  응답에는 예측 레이블, 확률, 처리 시간을 포함하여
  클라이언트 측 SLA 모니터링이 가능하도록 설계합니다.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ── 요청 ──────────────────────────────────────────────
class PredictRequest(BaseModel):
    """감성 분석 요청 스키마.

    - text: 분석할 한국어 금융 텍스트 (최대 512 토큰 기준)
    - 배치 요청은 List[PredictRequest]로 처리하되,
      단일 요청 우선 지원하여 API 진입 장벽을 낮춤.
    """

    text: str = Field(
        ...,
        min_length=1,
        max_length=2000,
        description="분석할 한국어 금융 텍스트",
        examples=["삼성전자가 2분기 영업이익 컨센서스를 상회했다."],
    )


class BatchPredictRequest(BaseModel):
    """배치 감성 분석 요청 스키마.

    최대 32건까지 허용 — CPU 추론 환경에서 메모리 OOM 방지.
    """

    texts: list[str] = Field(
        ...,
        min_length=1,
        max_length=32,
        description="분석할 한국어 금융 텍스트 목록 (최대 32건)",
    )


# ── 응답 ──────────────────────────────────────────────
class SentimentResult(BaseModel):
    """개별 감성 분석 결과."""

    label: str = Field(
        ...,
        description="예측 감성 레이블 (positive / negative / neutral)",
        examples=["positive"],
    )
    score: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description="예측 확률 (0.0 ~ 1.0)",
    )


class PredictResponse(BaseModel):
    """단일 텍스트 감성 분석 응답."""

    text: str
    result: SentimentResult
    elapsed_ms: float = Field(
        ...,
        description="추론에 소요된 시간 (밀리초)",
    )


class BatchPredictResponse(BaseModel):
    """배치 감성 분석 응답."""

    results: list[PredictResponse]
    total_elapsed_ms: float


class HealthResponse(BaseModel):
    """헬스 체크 응답 스키마."""

    status: str = Field(default="ok", description="서비스 상태")
    model_loaded: bool = Field(
        ..., description="모델 로드 여부"
    )
    model_name: str = Field(
        ..., description="로드된 모델 이름"
    )
