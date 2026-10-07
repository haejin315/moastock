#!/usr/bin/env bash
# GPU VM 처음 한 번 준비: Docker + NVIDIA 컨테이너 도구 설치, 데이터 폴더, 버킷에서 DB 덤프·청크 파일 받기.
#   sudo bash setup-vm.sh <버킷 이름>
set -euo pipefail
BUCKET=${1:?버킷 이름}

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
if ! dpkg -s nvidia-container-toolkit >/dev/null 2>&1; then
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list
  apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit
  nvidia-ctk runtime configure --runtime=docker
  systemctl restart docker
fi

mkdir -p /data/{hf,hf-tei,pg,dump,rag}
[ -f /data/dump/moarag.dump ] || gcloud storage cp "gs://$BUCKET/moarag.dump" /data/dump/
for f in chunks.parquet docs.parquet emb_keys.npy embed_done.npy embed_state.json; do
  [ -f "/data/rag/$f" ] || gcloud storage cp "gs://$BUCKET/$f" /data/rag/
done
docker run --rm --gpus all ubuntu:22.04 nvidia-smi -L
echo "준비 끝"
