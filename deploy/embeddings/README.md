# HBIM-030 — isolated Qwen3-Embedding-8B service (TEI)

Runs `Qwen/Qwen3-Embedding-8B` in its own GPU process behind a loopback HTTP API,
so the API and indexer processes never load a model. Consumed through
`backend/models/embeddings_qwen3.py`.

## Pinned contract

| Item | Pinned value |
|---|---|
| Backend | Hugging Face Text Embeddings Inference (TEI) |
| Image | `ghcr.io/huggingface/text-embeddings-inference:120-1.9` (Blackwell 12.0 / `sm_120`) |
| Digest | `sha256:aedf3b34836dc57289583142adcf2b93836cda0736ac8e6ce43691b9c2c67170` |
| Model | `Qwen/Qwen3-Embedding-8B` |
| Revision | `1d8ad4ca9b3dd8059ad90a75d4983776a23d44af` (40-hex; floating refs forbidden) |
| dtype | `float16` |
| Bind | `127.0.0.1:8081` → container `:80` (**loopback only**) |
| Cache | `${HBIM_HF_CACHE:-${HOME}/.cache/huggingface/hub}` → `/data` (**outside the repository**) |

No `latest` tag, no public bind, no privileged mode, no host networking, no
embedded credentials. `/health` returns 200 only once the model is loaded.

## Commands

```bash
# start
docker compose -f deploy/embeddings/docker-compose.yml up -d

# readiness — must report healthy before use
docker compose -f deploy/embeddings/docker-compose.yml ps
curl --fail --silent http://127.0.0.1:8081/health && echo READY

# served model identity (model_id + model_sha must match the pins above)
curl --fail --silent http://127.0.0.1:8081/info

# logs
docker compose -f deploy/embeddings/docker-compose.yml logs --tail=100

# stop (containers only; the model cache is preserved)
docker compose -f deploy/embeddings/docker-compose.yml down
```

## Safe cleanup

`docker compose ... down` removes **only** this project's container and network.

Never use `docker system prune`, `docker volume prune` or `docker image prune`
here: the model cache is a shared host directory and such commands would delete
unrelated models, images and containers. To reclaim the ~15 GiB model cache,
delete only the specific model directory under the cache path above,
deliberately and by hand.

## Notes

- The Blackwell `120-*` image line is marked experimental upstream; it is pinned
  by tag **and** digest, and the live test suite must pass before acceptance.
- First start downloads the weights into the external cache; later starts reuse
  it and reach `healthy` in well under a minute.
- The service has no OpenSearch dependency and must never be exposed publicly.
