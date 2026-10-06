"""5단계: 청크 임베딩 (로컬 CPU, multilingual-e5-small, 384차원, L2 정규화).

  python -m moarag.embed [--batch 32] [--block 20000] [--threads 16]

- 추론은 onnxruntime(모델 저장소의 onnx/model.onnx, fp32). 같은 CPU에서 PyTorch 대비 약 1.4~1.5배 빠르고
  벡터는 동일하다(코사인 1.0). int8(VNNI) 모델은 품질이 조금 바뀌는데(평균 코사인 0.995) 속도 이득이 없어 쓰지 않는다.
- e5 규약: 문서 "passage: ", 질의 "query: " 접두어. 청크 앞에 문서 제목을 붙여 짧은 청크도 맥락을 갖게 한다
  (저장 텍스트에는 붙이지 않음). 풀링은 attention mask 평균.
- 블록 안에서 길이순으로 정렬해 배치 패딩을 줄인다.
- 결과는 chunks.parquet 행 순서와 같은 float32 행렬(.npy memmap). 블록마다 진행 상황을 기록해 이어서 실행할 수 있다.
- --skip-periodic: 정기보고서 청크를 건너뛰고 먼저 끝낸다(나중에 옵션 없이 다시 돌리면 나머지만 계산).
- 증분: 입력 텍스트의 SHA-1을 키로 이전 결과(emb_keys.npy + embeddings)를 재사용한다. process를 다시 돌려
  청크 순서가 바뀌거나 공시가 추가돼도 새 청크만 계산한다.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re

import numpy as np
import pyarrow.parquet as pq

from .config import CHUNKS_PATH, DATA_DIR, EMB_PATH, EMBED_DIM, EMBED_MODEL
from .metrics import Progress, Stage

STATE = DATA_DIR / "embed_state.json"
KEYS_PATH = DATA_DIR / "emb_keys.npy"       # 각 행 입력 텍스트의 SHA-1 (20바이트)
DONE_PATH = DATA_DIR / "embed_done.npy"     # 행별 임베딩 완료 여부 (bool)


class OrtEncoder:
    def __init__(self, model: str = EMBED_MODEL, threads: int = 16, max_len: int = 512):
        import onnxruntime as ort
        from huggingface_hub import hf_hub_download
        from transformers import AutoTokenizer
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        self.sess = ort.InferenceSession(hf_hub_download(model, "onnx/model.onnx"), so,
                                         providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.sess.get_inputs()}
        self.tok = AutoTokenizer.from_pretrained(model)
        self.max_len = max_len

    def _batch(self, texts):
        x = self.tok(texts, padding=True, truncation=True, max_length=self.max_len, return_tensors="np")
        feed = {k: v.astype(np.int64) for k, v in x.items() if k in self.inputs}
        if "token_type_ids" in self.inputs and "token_type_ids" not in feed:
            feed["token_type_ids"] = np.zeros_like(feed["input_ids"])
        h = self.sess.run(None, feed)[0]
        m = x["attention_mask"][..., None].astype(np.float32)
        e = (h * m).sum(1) / np.maximum(m.sum(1), 1e-9)
        return e / np.linalg.norm(e, axis=1, keepdims=True)

    def encode(self, texts, batch: int = 32):
        out = np.zeros((len(texts), EMBED_DIM), np.float32)
        order = np.argsort([len(t) for t in texts], kind="stable")
        for i in range(0, len(texts), batch):
            idx = order[i:i + batch]
            out[idx] = self._batch([texts[j] for j in idx])
        return out


def passage_texts(tbl):
    titles = tbl.column("title").to_pylist()
    texts = tbl.column("text").to_pylist()
    return [f"passage: {t}\n{x}" if t else f"passage: {x}" for t, x in zip(titles, texts)]


PERIODIC = re.compile(r"사업보고서|반기보고서|분기보고서")


def iter_rows(batch_rows: int = 50_000):
    """chunks.parquet을 순서대로 읽으며 (시작행, 입력텍스트, 정기보고서 여부)를 낸다 - 전체를 메모리에 올리지 않는다."""
    pf = pq.ParquetFile(CHUNKS_PATH)
    off = 0
    for rb in pf.iter_batches(batch_size=batch_rows, columns=["title", "text", "report_nm"]):
        texts = passage_texts(rb)
        periodic = np.array([bool(r and PERIODIC.search(r)) for r in rb.column("report_nm").to_pylist()])
        yield off, texts, periodic
        off += len(texts)


def load_done_mask(n: int) -> np.ndarray:
    """임베딩이 끝난 행 표시. load/bench는 이 행만 쓴다."""
    if DONE_PATH.exists():
        m = np.load(DONE_PATH)
        if len(m) == n:
            return m
    return np.zeros(n, dtype=bool)


def main(batch: int, block: int, threads: int, skip_periodic: bool):
    n = pq.ParquetFile(CHUNKS_PATH).metadata.num_rows
    keys = np.empty(n, dtype="S20")
    for off, texts, _ in iter_rows():
        keys[off:off + len(texts)] = [hashlib.sha1(t.encode("utf-8")).digest() for t in texts]
    keys_sig = hashlib.sha1(keys.tobytes()).hexdigest()

    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    if state.get("keys_sig") == keys_sig and EMB_PATH.exists() and DONE_PATH.exists():
        done = np.load(DONE_PATH)                                   # 같은 청크 집합 - 이어서
        reused = 0
    else:
        # 청크 집합이 바뀌었다 - 이전 결과에서 같은 텍스트의 벡터를 가져오고 나머지만 계산
        hit = np.full(n, -1, dtype=np.int64)
        if EMB_PATH.exists() and KEYS_PATH.exists():
            old_keys = np.load(KEYS_PATH)
            old_done = np.load(DONE_PATH) if DONE_PATH.exists() and len(np.load(DONE_PATH)) == len(old_keys)                 else np.ones(len(old_keys), dtype=bool)
            pos = {k: i for i, (k, d) in enumerate(zip(old_keys.tolist(), old_done.tolist())) if d}
            hit = np.array([pos.get(k, -1) for k in keys.tolist()], dtype=np.int64)
            del pos
        tmp = EMB_PATH.with_suffix(".tmp.npy")
        new = np.lib.format.open_memmap(tmp, mode="w+", dtype=np.float32, shape=(n, EMBED_DIM))
        src = np.nonzero(hit >= 0)[0]
        if len(src):
            old_emb = np.load(EMB_PATH, mmap_mode="r")
            for i in range(0, len(src), 100_000):
                sl = src[i:i + 100_000]
                new[sl] = old_emb[hit[sl]]
            del old_emb
        new.flush()
        del new
        os.replace(tmp, EMB_PATH)
        reused = len(src)
        done = hit >= 0
        np.save(KEYS_PATH, keys)
        np.save(DONE_PATH, done)
        state = {"keys_sig": keys_sig, "n": n}
        STATE.write_text(json.dumps(state))

    emb = np.lib.format.open_memmap(EMB_PATH, mode="r+")
    enc = OrtEncoder(threads=threads)
    with Stage("embed", model=EMBED_MODEL, runtime="onnxruntime-fp32", chunks=n, reused=reused,
               already_done=int(done.sum()), skip_periodic=skip_periodic, batch=batch, threads=threads) as st:
        prog = Progress("임베딩", total=None, every=60)
        for off, texts, periodic in iter_rows(block):
            end = off + len(texts)
            need = ~done[off:end]
            if skip_periodic:
                need &= ~periodic
            idx = np.nonzero(need)[0]
            if not len(idx):
                continue
            sub = [texts[i] for i in idx]
            emb[idx + off] = enc.encode(sub, batch)
            emb.flush()
            done[idx + off] = True
            np.save(DONE_PATH, done)
            st.add(chunks=len(idx), chars=sum(map(len, sub)))
            prog.tick(len(idx), done=int(done.sum()))
        st.set(done_total=int(done.sum()), matrix_bytes=os.path.getsize(EMB_PATH))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=32)
    ap.add_argument("--block", type=int, default=20000)
    ap.add_argument("--threads", type=int, default=8)
    ap.add_argument("--skip-periodic", action="store_true", help="정기보고서(사업/반기/분기) 청크는 나중에")
    a = ap.parse_args()
    main(a.batch, a.block, a.threads, a.skip_periodic)
