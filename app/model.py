"""
app/model.py — 모델 로딩 및 추론 로직

설계 근거:
1. 싱글턴 패턴으로 모델을 1회만 로드하여 메모리 효율 극대화.
2. torch.no_grad() + eval() 모드로 추론 전용 최적화.
3. CPU 환경에서 BERT-base 기준 단일 추론 ~50-80ms 예상.
   (batch padding 시 throughput 향상 가능)
4. ThreadPoolExecutor를 통해 동기 PyTorch 추론을
   FastAPI의 비동기 이벤트 루프에서 블로킹 없이 실행.
"""

from __future__ import annotations

import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from functools import lru_cache
from typing import TYPE_CHECKING

import torch
from transformers import AutoModelForSequenceClassification, AutoTokenizer

if TYPE_CHECKING:
    pass

logger = logging.getLogger("kr_finbert")

# ── 설정 ──────────────────────────────────────────────
# 환경 변수로 모델명 오버라이드 가능 → 블루/그린 배포 시 유용
MODEL_NAME: str = os.getenv("MODEL_NAME", "snunlp/KR-FinBert-SC")

# CPU 추론 시 스레드 수를 제한하여 과도한 컨텍스트 스위칭 방지
# 보통 물리 코어 수의 절반이 최적 (기본값: 4)
TORCH_THREADS: int = int(os.getenv("TORCH_THREADS", "4"))

# 추론 전용 스레드 풀 — FastAPI 이벤트 루프 블로킹 방지
# max_workers = 2: CPU-bound 작업이므로 과다 스레드는 역효과
INFERENCE_WORKERS: int = int(os.getenv("INFERENCE_WORKERS", "2"))

# 레이블 매핑: KR-FinBert-SC 모델의 출력 ID → 사람이 읽을 수 있는 레이블
LABEL_MAP: dict[int, str] = {
    0: "negative",
    1: "neutral",
    2: "positive",
}


# ── 모델 매니저 ───────────────────────────────────────
class ModelManager:
    """모델 로딩·추론을 관리하는 싱글턴 클래스.

    이유:
    - 글로벌 변수 대신 클래스로 캡슐화하여 테스트 모킹 용이.
    - startup 이벤트에서 load()를 호출, shutdown에서 자원 해제.
    """

    def __init__(self) -> None:
        self.model = None
        self.tokenizer = None
        self.model_name: str = MODEL_NAME
        self._is_loaded: bool = False
        self._executor = ThreadPoolExecutor(
            max_workers=INFERENCE_WORKERS,
            thread_name_prefix="inference",
        )

    # ── 로드 ──
    def load(self) -> None:
        """모델 및 토크나이저 로드.

        - torch.set_num_threads: 추론 시 intra-op 병렬성 제어
        - model.eval(): Dropout/BatchNorm을 추론 모드로 전환
        """
        logger.info("모델 로딩 시작: %s", self.model_name)
        start = time.monotonic()

        torch.set_num_threads(TORCH_THREADS)

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)
        self.model = AutoModelForSequenceClassification.from_pretrained(
            self.model_name
        )
        self.model.eval()

        elapsed = (time.monotonic() - start) * 1000
        self._is_loaded = True
        logger.info(
            "모델 로딩 완료 (%.1fms), threads=%d", elapsed, TORCH_THREADS
        )

    # ── 상태 조회 ──
    @property
    def is_loaded(self) -> bool:
        return self._is_loaded

    # ── 단일 추론 (동기) ──
    def _predict_sync(self, text: str) -> dict:
        """동기 추론 — ThreadPoolExecutor 내부에서 호출됨.

        반환: {"label": str, "score": float, "elapsed_ms": float}
        """
        start = time.monotonic()

        inputs = self.tokenizer(
            text,
            return_tensors="pt",
            truncation=True,
            max_length=512,
            padding=True,
        )

        with torch.no_grad():
            outputs = self.model(**inputs)

        probs = torch.nn.functional.softmax(outputs.logits, dim=-1)
        pred_id = int(torch.argmax(probs, dim=-1).item())
        pred_score = float(probs[0][pred_id].item())

        elapsed_ms = (time.monotonic() - start) * 1000

        return {
            "label": LABEL_MAP.get(pred_id, f"unknown_{pred_id}"),
            "score": round(pred_score, 4),
            "elapsed_ms": round(elapsed_ms, 2),
        }

    # ── 비동기 래퍼 ──
    async def predict(self, text: str) -> dict:
        """비동기 추론 — 이벤트 루프를 블로킹하지 않음.

        설계 근거:
        - PyTorch 추론은 CPU-bound → asyncio.run_in_executor 사용
        - GIL 존재하나, torch 내부 C++ 연산은 GIL 해제 구간이 많아
          ThreadPool 사용이 ProcessPool보다 오버헤드 적음.
        """
        import asyncio

        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self._predict_sync, text
        )

    # ── 배치 추론 ──
    async def predict_batch(self, texts: list[str]) -> list[dict]:
        """배치 추론 — 개별 추론을 병렬 실행.

        참고: 진정한 배치 추론(padding + 단일 forward)은
        GPU 환경에서 효과적이나, CPU에서는 개별 추론의
        병렬 실행이 메모리 효율 면에서 우수.
        """
        import asyncio

        tasks = [self.predict(text) for text in texts]
        return await asyncio.gather(*tasks)

    # ── 자원 해제 ──
    def unload(self) -> None:
        """모델 자원 해제."""
        self._executor.shutdown(wait=False)
        self.model = None
        self.tokenizer = None
        self._is_loaded = False
        logger.info("모델 자원 해제 완료")


@lru_cache(maxsize=1)
def get_model_manager() -> ModelManager:
    """ModelManager 싱글턴 인스턴스 반환.

    lru_cache(maxsize=1)로 애플리케이션 전체에서 단일 인스턴스 보장.
    """
    return ModelManager()
