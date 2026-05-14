# 시스템 아키텍처 및 의사결정 기록

## 1. 개요

본 문서는 한국어 금융 감성 분석 모델(`snunlp/KR-FinBert-SC`)의 프로덕션 서빙 시스템 아키텍처와 기술 선택 근거를 기록합니다.

### 1.1 프로젝트 목표

| 항목 | 목표 |
|------|------|
| **가용성** | 99.5% 이상 (연간 다운타임 ≤ 43.8시간) |
| **운영 방식** | 전담 운영자 없음, 완전 자동화 |
| **모델 업데이트** | 월 1~2회, CI/CD 자동 배포 |
| **추론 환경** | CPU 전용 (GPU 불필요) |
| **팀 규모** | 개발자 5명 (MLOps 전담 1명) |

### 1.2 아키텍처 다이어그램

```
Client → [K8s Ingress / LB]
              │
     ┌────────┼────────┐
     ▼        ▼        ▼
  Pod#1    Pod#2    Pod#3     ← HPA (2~6 pods)
  FastAPI  FastAPI  FastAPI
  +BERT    +BERT    +BERT
     │        │        │
     └────────┼────────┘
              │ /metrics
              ▼
        Prometheus ──→ Grafana (대시보드+알림)
        Loki ─────────→ Grafana (로그 검색)

GitHub Actions CI/CD:
  Push → Lint → Test → Build → Push → K8s Manifest Update
```

---

## 2. 기술 스택 선정 근거

### 2.1 FastAPI 선택

- 비동기 I/O 네이티브, OpenAPI 자동 문서화, 타입 검증 내장
- TorchServe/Triton 대비 운영 복잡도가 낮아 5명 소규모 팀에 적합
- Prometheus 통합이 라이브러리 1줄로 가능

### 2.2 PyTorch CPU 추론

- 원본 모델 직접 사용으로 ONNX 변환 파이프라인 유지보수 불필요
- 단일 추론 ~50-80ms로 금융 감성 분석 SLA(200ms) 충족
- 월 1~2회 모델 업데이트 시 즉시 반영 가능 (재변환 불필요)
- **향후**: 트래픽 증가 시 ONNX Runtime 전환 권장 (~2x 성능 향상)

### 2.3 Loki vs ELK

| 구분 | ELK | Loki |
|------|-----|------|
| 메모리 | ~4-8GB 최소 | ~100-200MB |
| 운영 복잡도 | JVM 튜닝, 샤드 관리 | 단일 바이너리 |
| Grafana 통합 | 별도 Kibana | 네이티브 |

→ 5명 팀에서 ELK 운영은 인력 대비 과도한 부담

---

## 3. 성능 분석

### 3.1 CPU 추론 성능

- 모델: BERT-base (110M params, 12층, 768 hidden)
- 단일 추론: 50-80ms (4 vCPU 기준)
- P99 지연: ~120ms (512 토큰 입력 시)
- 단일 Pod TPS: 12-20 req/s
- 3 Pod TPS: 36-60 req/s → 피크 트래픽(~10 req/s) 대비 **3.6배 여유**

### 3.2 가용성 99.5% 달성

1. **Multi-Pod (≥2)**: 단일 Pod 장애 시에도 서비스 지속
2. **Rolling Update** (maxUnavailable: 0): 배포 중 Zero Downtime
3. **3중 Probe**: startup/readiness/liveness
4. **HPA**: 트래픽 급증 시 자동 스케일 아웃 (2→6 pods)
5. **자동 복구**: K8s CrashLoopBackOff 자동 재시작
6. **CI/CD**: 수동 배포 실수 제거

---

## 4. 보안 및 거버넌스

| 항목 | 적용 |
|------|------|
| 비루트 실행 | UID 1000, `runAsNonRoot: true` |
| 모델 보안 | Docker 이미지에 포함, 런타임 다운로드 없음 |
| 로그 보안 | 원문 텍스트 미기록 (PII 보호) |
| API 인증 | 프로덕션에서 API Gateway + JWT 권장 |
| 감사 추적 | JSON 로그 + Loki 30일 보관 |
| 이미지 스캔 | Trivy CI 단계 추가 권장 |

---

## 5. 확장 로드맵

| Phase | 내용 | 트리거 |
|-------|------|--------|
| 1 | 현재 구성 (CPU, 단일 모델) | 즉시 |
| 2 | ONNX Runtime 전환 | 트래픽 증가 시 |
| 3 | A/B 테스트 (Istio) | 모델 비교 필요 시 |
| 4 | GPU 추론 | 일 10만+ 요청 시 |
| 5 | Feature Store + Model Registry | MLOps 성숙도 3 |
