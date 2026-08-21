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

WORKER_LANE = os.getenv("V27_WORKER_LANE", "normal").strip().lower()
if WORKER_LANE not in {"normal", "slow"}:
    raise ValueError("V27_WORKER_LANE must be 'normal' or 'slow'")

DEFAULT_CONCURRENCY = "2" if WORKER_LANE == "slow" else "8"
DEFAULT_CLASSIFY_TIMEOUT = "30" if WORKER_LANE == "slow" else "8"
DEFAULT_LEASE_SECONDS = "180" if WORKER_LANE == "slow" else "120"
DEFAULT_RETRY_SECONDS = "300" if WORKER_LANE == "slow" else "60"

CONCURRENCY = max(1, int(os.getenv("V27_WORKER_CONCURRENCY", DEFAULT_CONCURRENCY)))
CLASSIFY_TIMEOUT_SECONDS = float(
    os.getenv("V27_CLASSIFY_TIMEOUT_SECONDS", DEFAULT_CLASSIFY_TIMEOUT)
)
FINALIZE_TIMEOUT_SECONDS = float(os.getenv("V27_FINALIZE_TIMEOUT_SECONDS", "30"))
LEASE_SECONDS = max(
    30, int(os.getenv("V27_LEASE_SECONDS", DEFAULT_LEASE_SECONDS))
)
MAX_ATTEMPTS = max(1, int(os.getenv("V27_MAX_ATTEMPTS", "3")))
RETRY_SECONDS = max(
    1, int(os.getenv("V27_RETRY_SECONDS", DEFAULT_RETRY_SECONDS))
)
IDLE_SLEEP_SECONDS = float(os.getenv("V27_IDLE_SLEEP_SECONDS", "1"))

WORKER_INSTANCE = os.getenv(
    "V27_WORKER_NAME", f"{socket.gethostname()}-{os.getpid()}"
)

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
    claim_rpc = (
        "v27_claim_one_slow_classification_item"
        if WORKER_LANE == "slow"
        else "v27_claim_one_classification_item"
    )
    result = rpc(
        claim_rpc,
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


def classify_and_finalize(source_product_id: int, lease_token: str) -> str:
    result = rpc(
        "v27_classify_leased_source_product",
        {
            "p_source_product_id": source_product_id,
            "p_lease_token": lease_token,
        },
        CLASSIFY_TIMEOUT_SECONDS,
    )
    accepted_statuses = {
        "classified",
        "classified_fast_signature",
        "classified_fast_exact_alias",
    }
    if not isinstance(result, dict) or result.get("status") not in accepted_statuses:
        raise RuntimeError(f"classifier lease rejected: {result}")

    rpc(
        "v27_finalize_source_product_incremental",
        {"p_source_product_id": source_product_id},
        FINALIZE_TIMEOUT_SECONDS,
    )
    return str(result.get("status"))


def worker_loop(slot: int) -> None:
    worker_name = f"{WORKER_INSTANCE}-{WORKER_LANE}-slot{slot:02d}"
    logger.info("worker started name=%s lane=%s", worker_name, WORKER_LANE)

    done_note = (
        "external_slow_worker_classified_and_finalized"
        if WORKER_LANE == "slow"
        else "external_worker_classified_and_finalized_cancel_safe"
    )
    timeout_note = (
        "external_slow_worker_timeout_cancel_requested"
        if WORKER_LANE == "slow"
        else "external_worker_timeout_cancel_requested"
    )
    error_note = (
        "external_slow_worker_error"
        if WORKER_LANE == "slow"
        else "external_worker_error"
    )

    while True:
        try:
            claim = claim_one(worker_name)
        except Exception:
            logger.exception("claim failed lane=%s", WORKER_LANE)
            time.sleep(3)
            continue

        if not claim:
            time.sleep(IDLE_SLEEP_SECONDS)
            continue

        source_product_id = int(claim["source_product_id"])
        lease_token = str(claim["lease_token"])
        started = time.monotonic()

        try:
            classifier_status = classify_and_finalize(source_product_id, lease_token)
            complete(
                source_product_id,
                lease_token,
                "done",
                note=done_note,
            )
            logger.info(
                "done lane=%s source_product_id=%s classifier_status=%s elapsed=%.2fs",
                WORKER_LANE,
                source_product_id,
                classifier_status,
                time.monotonic() - started,
            )

        except httpx.TimeoutException as exc:
            logger.warning(
                "timeout lane=%s source_product_id=%s elapsed=%.2fs; requesting database cancel",
                WORKER_LANE,
                source_product_id,
                time.monotonic() - started,
            )
            try:
                cancel_result = cancel_classifier(lease_token)
                logger.info(
                    "cancel lane=%s source_product_id=%s result=%s",
                    WORKER_LANE,
                    source_product_id,
                    cancel_result,
                )
            except Exception:
                logger.exception(
                    "database cancel request failed lane=%s source_product_id=%s",
                    WORKER_LANE,
                    source_product_id,
                )

            try:
                complete(
                    source_product_id,
                    lease_token,
                    "retry",
                    error=f"external_timeout:{type(exc).__name__}",
                    note=timeout_note,
                )
            except Exception:
                logger.exception(
                    "failed to record timeout lane=%s source_product_id=%s; lease will expire",
                    WORKER_LANE,
                    source_product_id,
                )

        except Exception as exc:
            logger.exception(
                "classification failed lane=%s source_product_id=%s",
                WORKER_LANE,
                source_product_id,
            )
            try:
                complete(
                    source_product_id,
                    lease_token,
                    "retry",
                    error=f"external_error:{type(exc).__name__}:{str(exc)[:700]}",
                    note=error_note,
                )
            except Exception:
                logger.exception(
                    "failed to record error lane=%s source_product_id=%s; lease will expire",
                    WORKER_LANE,
                    source_product_id,
                )


def main() -> None:
    logger.info(
        "starting v2.7 worker instance=%s lane=%s concurrency=%s classify_timeout=%ss lease=%ss retry=%ss",
        WORKER_INSTANCE,
        WORKER_LANE,
        CONCURRENCY,
        CLASSIFY_TIMEOUT_SECONDS,
        LEASE_SECONDS,
        RETRY_SECONDS,
    )

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=CONCURRENCY,
        thread_name_prefix=f"v27-{WORKER_LANE}",
    ) as pool:
        futures = [pool.submit(worker_loop, i + 1) for i in range(CONCURRENCY)]
        for future in futures:
            future.result()


if __name__ == "__main__":
    main()
