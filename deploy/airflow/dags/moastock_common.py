"""DAG 공통: 작업 파드 만들기 (KubernetesPodOperator). DAG 파일이 아니라 .airflowignore 로 파싱에서 뺀다."""
from datetime import timedelta

from airflow.providers.cncf.kubernetes.operators.pod import KubernetesPodOperator
from airflow.providers.cncf.kubernetes.secret import Secret
from kubernetes.client import models as k8s

NS = "moastock"
DSN = "postgresql://moarag:moarag@db:5432/moarag"

DEFAULT_ARGS = {
    "owner": "moastock",
    "retries": 2,                         # 스팟 회수로 파드가 죽으면 VM 이 다시 켜진 뒤 이어서
    "retry_delay": timedelta(minutes=5),
}

RAG_VOLUME = k8s.V1Volume(name="rag", host_path=k8s.V1HostPathVolumeSource(path="/data/rag", type="Directory"))
RAG_MOUNT = k8s.V1VolumeMount(name="rag", mount_path="/data/rag")


def pod(task_id: str, image: str, cmd: list[str], *, env: dict | None = None, secrets: list[Secret] | None = None,
        mem: str = "2Gi", cpu: str = "100m", rag: bool = False, timeout_min: int = 90, **kw):
    return KubernetesPodOperator(
        task_id=task_id,
        name=task_id.replace("_", "-"),
        namespace=NS,
        image=f"moastock/{image}:current",
        image_pull_policy="Never",        # VM 에서 빌드한 이미지 (k3s 가 같은 Docker 엔진을 쓴다)
        cmds=cmd[:1],
        arguments=cmd[1:],
        env_vars=env or {},
        secrets=secrets or [],
        volumes=[RAG_VOLUME] if rag else [],
        volume_mounts=[RAG_MOUNT] if rag else [],
        container_resources=k8s.V1ResourceRequirements(requests={"cpu": cpu, "memory": "256Mi"},
                                                       limits={"memory": mem}),
        in_cluster=True,
        get_logs=True,
        on_finish_action="delete_succeeded_pod",   # 실패한 파드는 남겨 두고 로그를 본다
        startup_timeout_seconds=300,
        execution_timeout=timedelta(minutes=timeout_min),
        **kw,
    )
