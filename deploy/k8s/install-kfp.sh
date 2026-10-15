#!/usr/bin/env bash
# Kubeflow Pipelines 단독 설치 (전체 Kubeflow 없이 파이프라인만, 네임스페이스 kubeflow). 한 번만.
#   bash deploy/k8s/install-kfp.sh
# 화면: kubectl -n kubeflow port-forward svc/ml-pipeline-ui 8081:80 → ssh -L 8081:localhost:8081
set -euo pipefail
V=${KFP_VERSION:-2.16.1}
kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/cluster-scoped-resources?ref=$V"
kubectl wait --for condition=established --timeout=120s crd/applications.app.k8s.io
kubectl apply -k "github.com/kubeflow/pipelines/manifests/kustomize/env/platform-agnostic?ref=$V"
# 노드 한 대·사용자 거의 없음: 캐시 서버는 끄고(웹훅 인증서 관리가 필요 없게) 나머지는 한 벌씩
kubectl -n kubeflow scale deploy cache-server cache-deployer-deployment --replicas=0 2>/dev/null || true
kubectl -n kubeflow wait --for=condition=available --timeout=900s deploy/ml-pipeline deploy/ml-pipeline-ui
kubectl -n kubeflow get pods
# 노드 한 대: 구성 요소마다 잡아 둔 CPU 예약(requests)이 커서 수집 작업 파드가 자리를 못 잡는다 - 예약만 낮춘다
kubectl -n kubeflow set resources deploy --all --requests=cpu=20m
