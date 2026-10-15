#!/usr/bin/env bash
# GPU VM 에 k3s 설치 (한 번만). Docker 엔진을 그대로 컨테이너 런타임으로 쓴다 - vLLM 이미지(32GB)를 두 벌 두지 않고,
# VM 에서 docker build 한 이미지를 바로 파드로 띄울 수 있게.
#   sudo bash deploy/k8s/install-k3s.sh
set -euo pipefail
cd "$(dirname "$0")"

# GPU: Docker 기본 런타임을 nvidia 로 (장치 플러그인이 할당한 파드에만 GPU 가 붙는다)
nvidia-ctk runtime configure --runtime=docker --set-as-default
systemctl restart docker

if ! command -v k3s >/dev/null; then
  curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL=stable \
    INSTALL_K3S_EXEC="--docker --disable traefik --disable servicelb --write-kubeconfig-mode 644" sh -
fi
until kubectl get nodes >/dev/null 2>&1; do sleep 3; done
kubectl apply -f base.yaml
echo "GPU 자원이 보일 때까지 기다림"
until [ "$(kubectl get node -o jsonpath='{.items[0].status.allocatable.nvidia\.com/gpu}')" = "4" ]; do sleep 5; done
kubectl get node -o custom-columns=NAME:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu,CPU:.status.allocatable.cpu,MEM:.status.allocatable.memory
