import concurrent.futures
import logging
import os
import socket
import threading
import time
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise EnvironmentError("SUPABASE_URL and SUPABASE_KEY must be set")

CONCURRENCY = max(1, int(os.getenv("V27_WORKER_CONCURRENCY", "8")))
CLASSIFY_TIMEOUT_SECONDS = float(os.getenv("V27_CLASSIFY_TIMEOUT_SECONDS", "8"))
FINALIZE_TIMEOUT_SECONDS = float(os.getenv("V27_FINALIZE_TIMEOUT_SECONDS", "30"))
LEASE_SECONDS = max(30, int(os.getenv("V27_LEASE_SECONDS", "120")))
MAX_ATTEMPTS = max(1, int(os.getenv("V27_MAX_ATTEMPTS", "3")))
RETRY_SECONDS = max(1, int(os.getenv("V27_RETRY_SECONDS", "60")))
IDLE_SLEEP_SECONDS = float(os.getenv("V27_IDLE_SLEEP_SECONDS", "1"))

WORKER_INSTANCE = os.getenv("V27_WORKER_NAME", f"{socket.gethostname()}-{os.getpid()}")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(threadName)s %(message)s",
)
logger = logging.getLogger("v27-classification-worker")

HEADERS = {
    "apikey": SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type": "application/json",
}

_thread_local = threading.local()


def _client() -> httpx.Client:
    client = getattr(_thread_local, "client", None)
    if client is None:
        client = httpx.Client(
            base_url=f"{SUPABASE_URL.rstrip('/')}/rest/v1",
            headers=HEADERS,
            timeout=httpx.Timeout(15.0, connect=5.0),
        )
        _thread_local.client = client
    return client


def rpc(name: str, payload: dict[str, Any], timeout_seconds: float) -> Any:
    response = _client().post(
        f"/rpc/{name}",
        json=payload,
        timeout=httpx.Timeout(timeout_seconds, connect=5.0),
    )
    response.raise_for_status()
    return response.json()


def claim_one(worker_name: str) -> dict[str, Any] | None:
    result = rpc(
        "v27_claim_one_classification_item",
        {"p_worker": worker_name, "p_lease_seconds": LEASE_SECONDS},
        10.0,
    )
    if not isinstance(result, dict) or result.get("status") != "claimed":
        return None
    return result


def complete(
    source_product_id: int,
    lease_token: str,
    status: str,
    error: str | None = None,
    note: str | None = None,
) -> None:
    rpc(
        "v27_complete_classification_lease",
        {
            "p_source_product_id": source_product_id,
            "p_lease_token": lease_token,
            "p_status": status,
            "p_error": error,
            "p_worker_note": note,
            "p_retry_seconds": RETRY_SECONDS,
            "p_max_attempts": MAX_ATTEMPTS,
        },
        FINALIZE_TIMEOUT_SECONDS,
    )


def cancel_classifier(lease_token: str) -> Any:
    return rpc(
        "v27_cancel_classification_lease",
        {"p_lease_token": lease_token},
        5.0,
    )


def classify_and_finalize(source_product_id: int, lease_token: str) -> None:
    result = rpc(
        "v27_classify_leased_source_product",
        {
            "p_source_product_id": source_product_id,
            "p_lease_token": lease_token,
        },
        CLASSIFY_TIMEOUT_SECONDS,
    )
    if not isinstance(result, dict) or result.get("status") != "classified":
        raise RuntimeError(f"classifier lease rejected: {result}")

    rpc(
        "v27_finalize_source_product_incremental",
        {"p_source_product_id": source_product_id},
        FINALIZE_TIMEOUT_SECONDS,
    )


def worker_loop(slot: int) -> None:
    worker_name = f"{WORKER_INSTANCE}-slot{slot:02d}"
    logger.info("worker started name=%s", worker_name)

    while True:
        try:
            claim = claim_one(worker_name)
        except Exception:
            logger.exception("claim failed")
            time.sleep(3)
            continue

        if not claim:
            time.sleep(IDLE_SLEEP_SECONDS)
            continue

        source_product_id = int(claim["source_product_id"])
        lease_token = str(claim["lease_token"])
        started = time.monotonic()

        try:
            classify_and_finalize(source_product_id, lease_token)
            complete(
                source_product_id,
                lease_token,
                "done",
                note="external_worker_classified_and_finalized_cancel_safe",
            )
            logger.info(
                "done source_product_id=%s elapsed=%.2fs",
                source_product_id,
                time.monotonic() - started,
            )

        except httpx.TimeoutException as exc:
            logger.warning(
                "timeout source_product_id=%s elapsed=%.2fs; requesting database cancel",
                source_product_id,
                time.monotonic() - started,
            )
            try:
                cancel_result = cancel_classifier(lease_token)
                logger.info(
                    "cancel source_product_id=%s result=%s",
                    source_product_id,
                    cancel_result,
                )
            except Exception:
                logger.exception(
                    "database cancel request failed source_product_id=%s",
                    source_product_id,
                )

            try:
                complete(
                    source_product_id,
                    lease_token,
                    "retry",
                    error=f"external_timeout:{type(exc).__name__}",
                    note="external_worker_timeout_cancel_requested",
                )
            except Exception:
                logger.exception(
                    "failed to record timeout source_product_id=%s; lease will expire",
                    source_product_id,
                )

        except Exception as exc:
            logger.exception("classification failed source_product_id=%s", source_product_id)
            try:
                complete(
                    source_product_id,
                    lease_token,
                    "retry",
                    error=f"external_error:{type(exc).__name__}:{str(exc)[:700]}",
                    note="external_worker_error",
                )
            except Exception:
                logger.exception(
                    "failed to record error source_product_id=%s; lease will expire",
                    source_product_id,
                )


def main() -> None:
    logger.info(
        "starting v2.7 worker instance=%s concurrency=%s classify_timeout=%ss lease=%ss",
        WORKER_INSTANCE,
        CONCURRENCY,
        CLASSIFY_TIMEOUT_SECONDS,
        LEASE_SECONDS,
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=CONCURRENCY,
        thread_name_prefix="v27",
    ) as pool:
        futures = [pool.submit(worker_loop, i + 1) for i in range(CONCURRENCY)]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
