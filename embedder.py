# -*- coding: utf-8 -*-
"""Embedding provider wrapper: Zhipu cloud or Ollama local."""
import time, logging, httpx, config

logger = logging.getLogger(__name__)

# NOTE: Do NOT clear global proxy env vars!
# Only use proxy=None + trust_env=False on this client to ensure direct connect to Zhipu API.
# Global os.environ.pop would pollute httpx clients in other modules like paper_discovery.
_client = httpx.Client(timeout=60, follow_redirects=True, proxy=None, trust_env=False)


_TRANSIENT_HTTPX_ERRORS = (
    httpx.ReadError,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    httpx.WriteError,
    httpx.PoolTimeout,
)


def _reset_client() -> None:
    """Recreate the direct embedding client after TLS/socket failures."""
    global _client
    try:
        _client.close()
    except Exception:
        pass
    _client = httpx.Client(timeout=60, follow_redirects=True, proxy=None, trust_env=False)

def _post(url, headers=None, json=None, retries=5, retry_base_seconds=3.0):
    for i in range(1, retries + 1):
        try:
            resp = _client.post(url, headers=headers, json=json)
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                if i == retries:
                    return resp
                retry_after = resp.headers.get("retry-after")
                if retry_after:
                    try:
                        delay = float(retry_after)
                    except ValueError:
                        delay = retry_base_seconds * i
                else:
                    delay = retry_base_seconds * i
                logger.warning(
                    "  Embedding API returned HTTP %s; retrying in %.1fs (%s/%s)",
                    resp.status_code, delay, i, retries,
                )
                time.sleep(delay)
                continue
            return resp
        except _TRANSIENT_HTTPX_ERRORS as e:
            _reset_client()
            if i == retries:
                raise
            delay = retry_base_seconds * i
            logger.warning(
                "  Embedding connection failed; retrying in %.1fs (%s/%s): %s",
                delay, i, retries, e,
            )
            time.sleep(delay)

def _post_zhipu(headers, json):
    return _post(
        config.ZHIPU_EMBED_URL,
        headers=headers,
        json=json,
        retry_base_seconds=config.ZHIPU_RETRY_BASE_SECONDS,
    )


def _embed_zhipu_one(text: str) -> list[float]:
    """单条向量化"""
    h = {"Authorization": f"Bearer {config.ZHIPU_API_KEY}", "Content-Type": "application/json"}
    body = {"model": config.ZHIPU_MODEL, "input": [text[:config.ZHIPU_MAX_CHARS]]}
    resp = _post_zhipu(h, body)
    resp.raise_for_status()
    data = resp.json()
    if "data" not in data or not data["data"]:
        raise RuntimeError(f"Embedding failed: {data}")
    return data["data"][0]["embedding"]

def _embed_zhipu_batch(texts: list[str]) -> list[list[float]]:
    """Zhipu batch embedding."""
    results = []
    for start in range(0, len(texts), config.ZHIPU_MAX_BATCH):
        batch = texts[start:start + config.ZHIPU_MAX_BATCH]
        h = {"Authorization": f"Bearer {config.ZHIPU_API_KEY}", "Content-Type": "application/json"}
        body = {"model": config.ZHIPU_MODEL, "input": [t[:config.ZHIPU_MAX_CHARS] for t in batch]}
        for attempt in range(1, 4):
            try:
                resp = _post_zhipu(h, body)
                resp.raise_for_status()
                data = resp.json()
                if "data" not in data:
                    raise RuntimeError(f"Embedding failed: {data}")
                # Sort by index to ensure correct order
                items = sorted(data["data"], key=lambda x: x.get("index", 0))
                results.extend(item["embedding"] for item in items)
                logger.info(f"  已向量化 {len(results)}/{len(texts)}")
                break
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 400:
                    # Some item triggered safety filter, retry one by one
                    logger.warning(f"  批次 400 错误，逐条重试...")
                    for t in batch:
                        try:
                            results.append(_embed_zhipu_one(t))
                        except Exception:
                            logger.warning(f"  跳过 1 条 (embedding 失败)")
                            results.append([0.0] * config.ZHIPU_DIM)
                        time.sleep(0.1)
                    break
                raise
            except _TRANSIENT_HTTPX_ERRORS as e:
                _reset_client()
                if attempt == 3:
                    raise
                delay = config.ZHIPU_RETRY_BASE_SECONDS * attempt
                logger.warning(
                    "  Embedding batch connection failed; retrying whole batch in %.1fs (%s/3): %s",
                    delay, attempt, e,
                )
                time.sleep(delay)
        time.sleep(config.ZHIPU_BATCH_SLEEP_SECONDS)  # 限流
    return results


def _embed_ollama_one(text: str) -> list[float]:
    """Single-text Ollama embedding, with old endpoint fallback."""
    embed_url = f"{config.OLLAMA_BASE_URL}/api/embed"
    body = {"model": config.OLLAMA_EMBED_MODEL, "input": text[:config.OLLAMA_MAX_CHARS]}
    resp = _post(embed_url, json=body, retries=3, retry_base_seconds=1.0)
    if resp.status_code == 404:
        old_url = f"{config.OLLAMA_BASE_URL}/api/embeddings"
        resp = _post(
            old_url,
            json={"model": config.OLLAMA_EMBED_MODEL, "prompt": text[:config.OLLAMA_MAX_CHARS]},
            retries=3,
            retry_base_seconds=1.0,
        )
    resp.raise_for_status()
    data = resp.json()
    if "embedding" in data:
        return data["embedding"]
    embeddings = data.get("embeddings")
    if embeddings:
        return embeddings[0]
    raise RuntimeError(f"Ollama embedding failed: {data}")


def _embed_ollama_batch(texts: list[str]) -> list[list[float]]:
    """Ollama local embeddings via /api/embed."""
    results = []
    embed_url = f"{config.OLLAMA_BASE_URL}/api/embed"
    for start in range(0, len(texts), config.OLLAMA_MAX_BATCH):
        batch = texts[start:start + config.OLLAMA_MAX_BATCH]
        body = {
            "model": config.OLLAMA_EMBED_MODEL,
            "input": [t[:config.OLLAMA_MAX_CHARS] for t in batch],
        }
        try:
            resp = _post(embed_url, json=body, retries=3, retry_base_seconds=1.0)
            resp.raise_for_status()
            data = resp.json()
            embeddings = data.get("embeddings")
            if not embeddings:
                raise RuntimeError(f"Ollama embedding failed: {data}")
            if len(embeddings) != len(batch):
                raise RuntimeError(f"Ollama returned {len(embeddings)} embeddings for {len(batch)} inputs")
            results.extend(embeddings)
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise
            for t in batch:
                results.append(_embed_ollama_one(t))
        logger.info(f"  已向量化 {len(results)}/{len(texts)} ({config.OLLAMA_EMBED_MODEL})")
        if config.OLLAMA_BATCH_SLEEP_SECONDS > 0:
            time.sleep(config.OLLAMA_BATCH_SLEEP_SECONDS)
    return results


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Batch embedding using the configured provider."""
    if config.EMBED_PROVIDER == "ollama":
        return _embed_ollama_batch(texts)
    return _embed_zhipu_batch(texts)
