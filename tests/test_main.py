"""
tests/test_main.py — API 엔드포인트 자동화 테스트

설계 근거:
1. httpx.AsyncClient + ASGITransport로 실제 HTTP 계층 테스트 (네트워크 불필요).
2. 모델 로딩은 CI 환경에서도 실행 가능하도록 monkeypatch로 모킹 가능.
3. 각 엔드포인트에 대해 정상/오류 케이스를 모두 검증.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.model import ModelManager, get_model_manager


# ── Fixtures ──────────────────────────────────────────
@pytest.fixture()
def mock_model_manager():
    """모델 매니저 모킹 — CI 환경에서 모델 다운로드 없이 테스트."""
    manager = MagicMock(spec=ModelManager)
    manager.is_loaded = True
    manager.model_name = "snunlp/KR-FinBert-SC"
    manager.predict = AsyncMock(
        return_value={
            "label": "positive",
            "score": 0.9876,
            "elapsed_ms": 42.5,
        }
    )
    manager.predict_batch = AsyncMock(
        return_value=[
            {"label": "positive", "score": 0.9876, "elapsed_ms": 42.5},
            {"label": "negative", "score": 0.8543, "elapsed_ms": 38.2},
        ]
    )
    return manager


@pytest.fixture()
def patched_app(mock_model_manager):
    """get_model_manager를 모킹된 매니저로 교체."""
    with patch("app.main.get_model_manager", return_value=mock_model_manager):
        yield app


@pytest.fixture()
async def client(patched_app):
    """비동기 테스트 클라이언트."""
    transport = ASGITransport(app=patched_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Health Check 테스트 ───────────────────────────────
@pytest.mark.anyio
async def test_health_check(client):
    """정상 상태에서 헬스 체크가 200을 반환하는지 검증."""
    response = await client.get("/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["model_loaded"] is True
    assert data["model_name"] == "snunlp/KR-FinBert-SC"


@pytest.mark.anyio
async def test_health_check_model_not_loaded():
    """모델 미로드 시 503 반환 검증."""
    manager = MagicMock(spec=ModelManager)
    manager.is_loaded = False

    with patch("app.main.get_model_manager", return_value=manager):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as ac:
            response = await ac.get("/health")
            assert response.status_code == 503


# ── Predict 엔드포인트 테스트 ─────────────────────────
@pytest.mark.anyio
async def test_predict_success(client):
    """정상 예측 요청이 올바른 응답을 반환하는지 검증."""
    payload = {"text": "삼성전자가 2분기 영업이익 컨센서스를 상회했다."}
    response = await client.post("/predict", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert data["text"] == payload["text"]
    assert data["result"]["label"] == "positive"
    assert 0.0 <= data["result"]["score"] <= 1.0
    assert data["elapsed_ms"] > 0


@pytest.mark.anyio
async def test_predict_empty_text(client):
    """빈 텍스트 요청 시 422 Validation Error 반환 검증."""
    payload = {"text": ""}
    response = await client.post("/predict", json=payload)
    assert response.status_code == 422


@pytest.mark.anyio
async def test_predict_missing_field(client):
    """text 필드 누락 시 422 반환 검증."""
    response = await client.post("/predict", json={})
    assert response.status_code == 422


# ── Batch Predict 테스트 ──────────────────────────────
@pytest.mark.anyio
async def test_batch_predict_success(client):
    """배치 예측이 올바른 결과를 반환하는지 검증."""
    payload = {
        "texts": [
            "삼성전자 실적이 좋다.",
            "코스피가 폭락했다.",
        ]
    }
    response = await client.post("/predict/batch", json=payload)

    assert response.status_code == 200
    data = response.json()
    assert len(data["results"]) == 2
    assert data["total_elapsed_ms"] > 0


@pytest.mark.anyio
async def test_batch_predict_empty_list(client):
    """빈 배치 요청 시 422 반환 검증."""
    payload = {"texts": []}
    response = await client.post("/predict/batch", json=payload)
    assert response.status_code == 422


# ── Metrics 엔드포인트 테스트 ─────────────────────────
@pytest.mark.anyio
async def test_metrics_endpoint(client):
    """/metrics 엔드포인트가 Prometheus 형식 데이터를 반환하는지 검증."""
    response = await client.get("/metrics")
    assert response.status_code == 200
    # Prometheus 텍스트 형식에는 HELP 또는 TYPE 라인이 포함됨
    assert "http_request" in response.text or "HELP" in response.text


# ── OpenAPI 스키마 테스트 ─────────────────────────────
@pytest.mark.anyio
async def test_openapi_schema(client):
    """OpenAPI 스키마가 정상적으로 생성되는지 검증."""
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "paths" in schema
    assert "/predict" in schema["paths"]
    assert "/health" in schema["paths"]
