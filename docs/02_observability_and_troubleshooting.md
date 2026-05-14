# 관측 가능성 및 장애 대응 가이드

## 1. 관측 가능성 설계 (Observability)

### 1.1 Three Pillars 구현

| 계층 | 도구 | 수집 방식 | 보관 기간 |
|------|------|-----------|-----------|
| **Metrics** | Prometheus | Pull (/metrics, 15초) | 15일 |
| **Logs** | Loki + Promtail | Push (JSON stdout) | 30일 |
| **Traces** | 향후 Jaeger 도입 | - | - |

### 1.2 핵심 메트릭 (RED Method)

**Rate (요청률)**
- `http_requests_total{handler="/predict"}` — 초당 예측 요청 수
- `http_requests_total{status="5xx"}` — 5xx 에러 수

**Error (에러율)**
- `rate(http_requests_total{status="5xx"}[5m]) / rate(http_requests_total[5m])` — 5분 에러율

**Duration (지연시간)**
- `http_request_duration_seconds{handler="/predict"}` — 예측 API 지연 히스토그램
- `histogram_quantile(0.99, ...)` — P99 지연시간

### 1.3 커스텀 비즈니스 메트릭

| 메트릭 | 타입 | 설명 |
|--------|------|------|
| `model_inference_seconds` | Histogram | 순수 모델 추론 시간 |
| `model_loaded` | Gauge | 모델 로드 상태 (1/0) |
| `http_requests_inprogress` | Gauge | 현재 처리 중인 요청 수 |

### 1.4 로그 설계

**JSON 구조화 로그 형식:**
```json
{
  "timestamp": "2024-01-15T10:30:00.123Z",
  "level": "INFO",
  "logger": "kr_finbert",
  "message": "request",
  "method": "POST",
  "path": "/predict",
  "status_code": 200,
  "elapsed_ms": 65.42,
  "client_ip": "10.0.0.1"
}
```

**로그 레벨 정책:**
- `INFO`: 모든 HTTP 요청, 모델 로드/언로드
- `WARNING`: 느린 요청 (>200ms), 재시도 발생
- `ERROR`: 추론 실패, 모델 로드 실패
- `CRITICAL`: 프로세스 비정상 종료

---

## 2. Grafana 대시보드 설계

### 2.1 서비스 대시보드 패널 구성

| 패널 | 쿼리 | 용도 |
|------|------|------|
| RPS | `rate(http_requests_total[1m])` | 실시간 트래픽 |
| Error Rate | `5xx / total * 100` | SLA 모니터링 |
| P50/P95/P99 Latency | `histogram_quantile(0.99, ...)` | 성능 추세 |
| Active Requests | `http_requests_inprogress` | 부하 상태 |
| Pod CPU/Memory | `container_cpu_usage_seconds_total` | 리소스 |

### 2.2 Loki 로그 쿼리 예시

```logql
# 5xx 에러 로그 조회
{service="kr-finbert"} |= "status_code" | json | status_code >= 500

# 느린 요청 (200ms 이상) 필터링
{service="kr-finbert"} | json | elapsed_ms > 200

# 최근 1시간 에러 통계
sum(count_over_time({service="kr-finbert"} | json | level="ERROR" [1h]))
```

---

## 3. 알림 규칙 (Alerting)

### 3.1 Prometheus Alert Rules

```yaml
groups:
  - name: kr-finbert-alerts
    rules:
      # 1. 가용성 SLA 위반: 5분간 에러율 > 0.5%
      - alert: HighErrorRate
        expr: |
          sum(rate(http_requests_total{status=~"5.."}[5m]))
          / sum(rate(http_requests_total[5m])) > 0.005
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "에러율 SLA 위반 (>0.5%)"

      # 2. 높은 지연: P99 > 500ms
      - alert: HighLatency
        expr: |
          histogram_quantile(0.99, 
            rate(http_request_duration_seconds_bucket[5m])) > 0.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "P99 응답시간 500ms 초과"

      # 3. Pod 부족: Ready Pod < 2
      - alert: InsufficientPods
        expr: |
          kube_deployment_status_replicas_available{
            deployment="kr-finbert-api"} < 2
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "가용 Pod 2개 미만 — 가용성 위험"

      # 4. 메모리 사용률 90% 초과
      - alert: HighMemoryUsage
        expr: |
          container_memory_usage_bytes / 
          container_spec_memory_limit_bytes > 0.9
        for: 5m
        labels:
          severity: warning
```

### 3.2 알림 채널

| 심각도 | 채널 | 대응 |
|--------|------|------|
| Critical | Slack #ml-alerts + PagerDuty | 즉시 확인 |
| Warning | Slack #ml-alerts | 업무 시간 내 확인 |
| Info | Grafana 대시보드 | 주간 리뷰 |

---

## 4. 장애 시나리오 및 자동 복구

### 4.1 시나리오별 대응

| # | 장애 | 감지 | 자동 복구 | RTO |
|---|------|------|-----------|-----|
| 1 | Pod 크래시 | Liveness Probe 실패 | K8s 자동 재시작 | ~2분 |
| 2 | 모델 로드 실패 | Readiness Probe 503 | 트래픽 차단 + 재시작 | ~3분 |
| 3 | OOM Kill | K8s OOMKilled 이벤트 | 자동 재시작 + HPA 스케일 | ~2분 |
| 4 | 노드 장애 | Pod Anti-Affinity | 다른 노드에 재스케줄 | ~5분 |
| 5 | 배포 실패 | Rolling Update 실패 | 자동 롤백 (revisionHistoryLimit) | ~3분 |
| 6 | 트래픽 급증 | HPA CPU >60% | 자동 스케일 아웃 | ~1분 |

### 4.2 수동 롤백 절차 (비상 시)

```bash
# 1. 현재 배포 상태 확인
kubectl rollout status deployment/kr-finbert-api -n ml-serving

# 2. 이전 리비전으로 롤백
kubectl rollout undo deployment/kr-finbert-api -n ml-serving

# 3. 롤백 확인
kubectl rollout history deployment/kr-finbert-api -n ml-serving
```

---

## 5. 트러블슈팅 체크리스트

### 5.1 API 응답 없음

1. `kubectl get pods -n ml-serving` → Pod 상태 확인
2. `kubectl logs <pod> -n ml-serving --tail=50` → 에러 로그
3. `kubectl describe pod <pod> -n ml-serving` → 이벤트 확인
4. Grafana → `http_requests_inprogress` 확인 (요청 적체 여부)

### 5.2 느린 응답

1. Grafana P99 latency 패널 확인
2. `kubectl top pod -n ml-serving` → CPU/메모리 확인
3. Loki: `{service="kr-finbert"} | json | elapsed_ms > 200`
4. `TORCH_THREADS` 값 조정 검토

### 5.3 모델 로드 실패

1. Pod 로그에서 `모델 로딩` 관련 에러 확인
2. 디스크 공간 확인 (모델 ~440MB)
3. HuggingFace 캐시 경로 권한 확인
4. Docker 이미지 내 모델 포함 여부 확인
