from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urljoin, urlparse
import re
import time

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from tqdm import tqdm

from services.store_service.capabilities.store_location_acquisition.protocals import (
    AcquisitionArtifact,
    AcquisitionSourceInfo,
    AcquisitionValidationResult,
    StoreLocationAcquisitionStrategy,
)

BASE_URL = "https://www.samsclub.com"
ROOT_URL = f"{BASE_URL}/club-directory"

STATE_NAME_TO_CODE = {
    "ALABAMA": "AL",
    "ALASKA": "AK",
    "ARIZONA": "AZ",
    "ARKANSAS": "AR",
    "CALIFORNIA": "CA",
    "COLORADO": "CO",
    "CONNECTICUT": "CT",
    "DELAWARE": "DE",
    "DISTRICT OF COLUMBIA": "DC",
    "FLORIDA": "FL",
    "GEORGIA": "GA",
    "HAWAII": "HI",
    "IDAHO": "ID",
    "ILLINOIS": "IL",
    "INDIANA": "IN",
    "IOWA": "IA",
    "KANSAS": "KS",
    "KENTUCKY": "KY",
    "LOUISIANA": "LA",
    "MAINE": "ME",
    "MARYLAND": "MD",
    "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI",
    "MINNESOTA": "MN",
    "MISSISSIPPI": "MS",
    "MISSOURI": "MO",
    "MONTANA": "MT",
    "NEBRASKA": "NE",
    "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH",
    "NEW JERSEY": "NJ",
    "NEW MEXICO": "NM",
    "NEW YORK": "NY",
    "NORTH CAROLINA": "NC",
    "NORTH DAKOTA": "ND",
    "OHIO": "OH",
    "OKLAHOMA": "OK",
    "OREGON": "OR",
    "PENNSYLVANIA": "PA",
    "PUERTO RICO": "PR",
    "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD",
    "TENNESSEE": "TN",
    "TEXAS": "TX",
    "UTAH": "UT",
    "VERMONT": "VT",
    "VIRGINIA": "VA",
    "WASHINGTON": "WA",
    "WEST VIRGINIA": "WV",
    "WISCONSIN": "WI",
    "WYOMING": "WY",
}


@dataclass(slots=True)
class _StateJob:
    state_code: str
    href: str


@dataclass(slots=True)
class _LeafJob:
    href: str
    page_type: str  # city | detail
    state_code: str | None = None
    city_slug: str | None = None
    city_name: str | None = None


class SamsClubAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    retailer_key = "sams_club"
    retailer_name = "Sam's Club"

    def __init__(
        self,
        *,
        state_workers=2,
        city_workers = 4,
        store_workers = 4,
        request_timeout: int = 20,
        max_retries: int = 3,
    ) -> None:
        """Initialize acquisition configuration and run state.

        :param state_workers: State workers.
        :param city_workers: City workers.
        :param store_workers: Store workers.
        :param request_timeout: Request timeout.
        :param max_retries: Max retries.
        :return: Result produced by init  .
        """
        self.state_workers = state_workers
        self.city_workers = city_workers
        self.store_workers = store_workers
        self.request_timeout = request_timeout
        self.max_retries = max_retries

        self._failed_state_pages: list[dict[str, Any]] = []
        self._failed_city_pages: list[dict[str, Any]] = []
        self._failed_detail_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source.

        :return: Metadata describing the acquisition source.
        """
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.samsclub.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="BeautifulSoup",
            notes=(
                "Sam's Club directory hierarchy: root -> state pages -> "
                "single-club detail pages or multi-club city pages."
            ),
        )

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition.

        :return: Raw acquisition artifacts.
        """
        self._failed_state_pages = []
        self._failed_city_pages = []
        self._failed_detail_pages = []

        session = self._create_session()
        artifacts: list[AcquisitionArtifact] = []

        try:
            root_html, root_anchors = self._render_root_html_with_playwright(ROOT_URL)

            artifacts.append(
                AcquisitionArtifact(
                    artifact_type="html",
                    source_url=ROOT_URL,
                    content=root_html,
                    metadata={
                        "retrieved_at_utc": self._utc_now(),
                        "page_type": "root",
                        "http_status": 200,
                    },
                )
            )

            state_jobs = self._parse_state_jobs_from_anchors(root_anchors)
            print(f"[Sam's Club] discovered states: {len(state_jobs)}")

            if not state_jobs:
                raise RuntimeError(
                    "Sam's Club root page was rendered successfully, "
                    "but no state links were parsed."
                )

            state_artifacts: list[AcquisitionArtifact] = []

            with tqdm(
                total=len(state_jobs),
                desc="Sam's Club states",
                unit="state",
            ) as pbar:
                with ThreadPoolExecutor(max_workers=self.state_workers) as pool:
                    futures = {
                        pool.submit(
                            self._fetch_state_page,
                            session,
                            job,
                        ): job
                        for job in state_jobs
                    }

                    for future in as_completed(futures):
                        artifact = future.result()
                        artifacts.append(artifact)

                        if artifact.metadata.get("scrape_status") == "success":
                            state_artifacts.append(artifact)

                        pbar.update(1)

            leaf_jobs: list[_LeafJob] = []
            for artifact in state_artifacts:
                state_code = self._clean_text(artifact.metadata.get("state_code"))
                leaf_jobs.extend(
                    self._parse_state_leaf_jobs(
                        artifact.content or "",
                        state_code=state_code,
                    )
                )

            leaf_jobs = self._dedupe_leaf_jobs(leaf_jobs)

            detail_jobs = [job for job in leaf_jobs if job.page_type == "detail"]
            city_jobs = [job for job in leaf_jobs if job.page_type == "city"]

            print(
                "[Sam's Club] discovered "
                f"{len(detail_jobs)} direct club pages + "
                f"{len(city_jobs)} multi-club city pages"
            )

            if not leaf_jobs:
                raise RuntimeError(
                    "Sam's Club state pages were fetched, "
                    "but no club or city links were discovered."
                )

            with tqdm(
                total=len(leaf_jobs),
                desc="Sam's Club club pages",
                unit="page",
            ) as pbar:
                with ThreadPoolExecutor(max_workers=self.city_workers) as pool:
                    futures = {
                        pool.submit(
                            self._fetch_leaf_page,
                            session,
                            job,
                        ): job
                        for job in leaf_jobs
                    }

                    for future in as_completed(futures):
                        artifact = future.result()
                        artifacts.append(artifact)
                        pbar.update(1)

            return artifacts

        finally:
            session.close()

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts.

        :param artifacts: Acquisition artifacts to process.
        :return: Normalized and deduplicated store payloads.
        """
        parse_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") in {"city", "detail"}
            and artifact.metadata.get("scrape_status") == "success"
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}

        with tqdm(
            total=len(parse_artifacts),
            desc="Parsing Sam's Club stores",
            unit="page",
        ) as pbar:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._parse_artifact, artifact): artifact
                    for artifact in parse_artifacts
                }

                for future in as_completed(futures):
                    rows = future.result()
                    for row in rows:
                        store_id = self._clean_text(row.get("retailer_store_id"))
                        if not store_id:
                            continue
                        rows_by_store_id[store_id] = row
                    pbar.update(1)

        return list(rows_by_store_id.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness.

        :param payloads: Normalized store payloads to validate.
        :return: Validation result for the acquired payloads.
        """
        total_records = len(payloads)

        store_ids = [
            self._clean_text(row.get("retailer_store_id"))
            for row in payloads
        ]

        unique_store_ids = len({sid for sid in store_ids if sid})
        missing_store_ids = sum(1 for sid in store_ids if not sid)

        missing_addresses = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("street_address"))
            or not self._clean_text(row.get("city"))
            or not self._clean_text(row.get("state"))
            or not self._clean_text(row.get("zip_code"))
        )

        missing_phones = sum(
            1
            for row in payloads
            if not self._clean_text(row.get("phone"))
        )

        duplicate_store_ids: list[str] = []
        seen: set[str] = set()
        for sid in store_ids:
            if not sid:
                continue
            if sid in seen and sid not in duplicate_store_ids:
                duplicate_store_ids.append(sid)
            seen.add(sid)

        issue_counts: dict[str, int] = {}
        if missing_store_ids:
            issue_counts["missing_store_ids"] = missing_store_ids
        if missing_addresses:
            issue_counts["missing_addresses"] = missing_addresses
        if missing_phones:
            issue_counts["missing_phones"] = missing_phones
        if self._failed_state_pages:
            issue_counts["failed_state_pages"] = len(self._failed_state_pages)
        if self._failed_city_pages:
            issue_counts["failed_city_pages"] = len(self._failed_city_pages)
        if self._failed_detail_pages:
            issue_counts["failed_detail_pages"] = len(self._failed_detail_pages)

        notes = [
            "Root directory discovers /club-directory/<state> pages.",
            "State pages contain either direct /club/<id>-... links or /club-directory/<state>/<city> links.",
            "Multi-club city pages are parsed directly from their club cards.",
            "Store id is extracted from /club/<id>-... and cross-checked against the #<id> heading.",
            f"Workers: state={self.state_workers}, city={self.city_workers}, store={self.store_workers}",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
            and len(self._failed_state_pages) == 0
            and len(self._failed_city_pages) == 0
            and len(self._failed_detail_pages) == 0
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=0,
            non_us_records=0,
            duplicate_store_ids=duplicate_store_ids,
            issue_counts=issue_counts,
            notes=notes,
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary.

        :return: Notes describing the acquisition run.
        """
        return [
            f"Source: {ROOT_URL}",
            "Method: Playwright directory rendering + requests / BeautifulSoup leaf pages",
            "Hierarchy: root -> state pages -> city pages/detail pages",
            "Single-club entries use /club/<id>-...; multi-club cities use /club-directory/<state>/<city>.",
            "Dedup key: retailer_store_id from /club/<id>-... URL suffix.",
        ]

    def _create_session(self) -> requests.Session:
        """Create session.

        :return: Result produced by create session.
        """
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return session

    def _render_root_html_with_playwright(
            self,
            url: str,
    ) -> tuple[str, list[dict[str, str]]]:
        """Render root html with playwright.

        :param url: URL to fetch or process.
        :return: Result produced by render root html with playwright.
        """
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
            )

            context = browser.new_context(
                viewport={
                    "width": 1440,
                    "height": 1600,
                },
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )

            page = context.new_page()

            try:
                response = page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )

                print()
                print("=" * 100)
                print("SAM'S CLUB PLAYWRIGHT DEBUG")
                print("=" * 100)

                print(
                    "[Sam's Club] initial response status:",
                    response.status if response else "unknown",
                )

                print(
                    "[Sam's Club] initial URL:",
                    page.url,
                )

                print(
                    "[Sam's Club] initial title:",
                    page.title(),
                )

                # Give React / client-side rendering time.
                for second in range(1, 11):
                    page.wait_for_timeout(1000)

                    anchor_count = page.locator("a[href]").count()

                    club_directory_count = page.locator(
                        'a[href*="club-directory"]'
                    ).count()

                    state_link_count = page.locator(
                        'a[href^="/club-directory/"]'
                    ).count()

                    body_length = len(
                        page.locator("body").inner_text()
                    )

                    print(
                        f"[Sam's Club] t={second:02d}s | "
                        f"anchors={anchor_count} | "
                        f"club-directory-links={club_directory_count} | "
                        f"state-links={state_link_count} | "
                        f"body-length={body_length}"
                    )

                print()
                print("-" * 100)
                print("FINAL PAGE INFO")
                print("-" * 100)

                print(
                    "[Sam's Club] final URL:",
                    page.url,
                )

                print(
                    "[Sam's Club] final title:",
                    page.title(),
                )

                body_text = page.locator("body").inner_text()

                print()
                print("-" * 100)
                print("BODY TEXT - FIRST 5000 CHARS")
                print("-" * 100)

                print(
                    body_text[:5000]
                )

                print()
                print("-" * 100)
                print("ALL club-directory LINKS")
                print("-" * 100)

                directory_links = page.locator(
                    'a[href*="club-directory"]'
                ).evaluate_all(
                    """
                    els => els.map(a => ({
                        href: a.getAttribute('href') || '',
                        text: (
                            a.textContent ||
                            a.innerText ||
                            ''
                        ).trim()
                    }))
                    """
                )

                print(
                    f"[Sam's Club] directory link count: "
                    f"{len(directory_links)}"
                )

                for index, item in enumerate(
                        directory_links[:100],
                        start=1,
                ):
                    print(
                        f"[{index:03d}] "
                        f"href={item.get('href')!r} | "
                        f"text={item.get('text')!r}"
                    )

                print()
                print("-" * 100)
                print("FIRST 100 LINKS ON PAGE")
                print("-" * 100)

                anchor_data = page.locator(
                    "a[href]"
                ).evaluate_all(
                    """
                    els => els.map(a => ({
                        href: a.getAttribute('href') || '',
                        text: (
                            a.textContent ||
                            a.innerText ||
                            ''
                        ).trim()
                    }))
                    """
                )

                print(
                    f"[Sam's Club] total anchors: "
                    f"{len(anchor_data)}"
                )

                for index, item in enumerate(
                        anchor_data[:100],
                        start=1,
                ):
                    print(
                        f"[{index:03d}] "
                        f"href={item.get('href')!r} | "
                        f"text={item.get('text')!r}"
                    )

                html = page.content()

                print()
                print("-" * 100)
                print("HTML INFO")
                print("-" * 100)

                print(
                    "[Sam's Club] HTML length:",
                    len(html),
                )

                print(
                    "[Sam's Club] href occurrences:",
                    html.count("href="),
                )

                print(
                    "[Sam's Club] '/club-directory/' occurrences:",
                    html.count("/club-directory/"),
                )

                print(
                    "[Sam's Club] '/club/' occurrences:",
                    html.count("/club/"),
                )

                print()
                print("=" * 100)
                print("END SAM'S CLUB PLAYWRIGHT DEBUG")
                print("=" * 100)
                print()

                if not html or "<html" not in html.lower():
                    raise RuntimeError(
                        "Sam's Club root page returned empty HTML."
                    )

                return html, anchor_data

            finally:
                context.close()
                browser.close()

    def _fetch_text(self, session: requests.Session, url: str) -> str:
        """Fetch text.

        :param session: HTTP session used for requests.
        :param url: URL to fetch or process.
        :return: Result produced by fetch text.
        """
        last_error: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = session.get(url, timeout=self.request_timeout)
                response.raise_for_status()
                return response.text
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries - 1:
                    time.sleep(random.uniform(2.0, 5.0))
        raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error

    def _fetch_state_page(
        self,
        session: requests.Session,
        job: _StateJob,
    ) -> AcquisitionArtifact:
        """Fetch state page.

        :param session: HTTP session used for requests.
        :param job: Acquisition job to process.
        :return: Result produced by fetch state page.
        """
        url = urljoin(BASE_URL + "/", job.href.lstrip("/"))

        try:
            html = self._fetch_text(session, url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content=html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "state",
                    "state_code": job.state_code,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            self._failed_state_pages.append(
                {
                    "url": url,
                    "state_code": job.state_code,
                    "error": str(exc),
                }
            )
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content="",
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": "state",
                    "state_code": job.state_code,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _fetch_leaf_page(
        self,
        session: requests.Session,
        job: _LeafJob,
    ) -> AcquisitionArtifact:
        """Fetch leaf page.

        :param session: HTTP session used for requests.
        :param job: Acquisition job to process.
        :return: Result produced by fetch leaf page.
        """
        url = urljoin(BASE_URL + "/", job.href.lstrip("/"))

        try:
            html = self._fetch_text(session, url)
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content=html,
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": job.page_type,
                    "state_code": job.state_code,
                    "city_slug": job.city_slug,
                    "city_name": job.city_name,
                    "http_status": 200,
                    "scrape_status": "success",
                },
            )
        except Exception as exc:
            failed_bucket = (
                self._failed_city_pages
                if job.page_type == "city"
                else self._failed_detail_pages
            )
            failed_bucket.append(
                {
                    "url": url,
                    "state_code": job.state_code,
                    "city_slug": job.city_slug,
                    "error": str(exc),
                }
            )
            return AcquisitionArtifact(
                artifact_type="html",
                source_url=url,
                content="",
                metadata={
                    "retrieved_at_utc": self._utc_now(),
                    "page_type": job.page_type,
                    "state_code": job.state_code,
                    "city_slug": job.city_slug,
                    "city_name": job.city_name,
                    "http_status": 500,
                    "scrape_status": "failed",
                    "error": str(exc),
                },
            )

    def _parse_state_jobs_from_anchors(
        self,
        anchors: Sequence[Mapping[str, Any]],
    ) -> list[_StateJob]:
        """Parse state jobs from anchors.

        :param anchors: Anchors.
        :return: Result produced by parse state jobs from anchors.
        """
        jobs: list[_StateJob] = []

        for item in anchors:
            href = self._clean_text(item.get("href"))
            text = self._clean_text(item.get("text"))

            if not href:
                continue

            abs_url = urljoin(BASE_URL + "/", href.lstrip("/"))
            parsed = urlparse(abs_url)
            path = parsed.path.strip("/")
            parts = [part for part in path.split("/") if part]

            state_code: str | None = None

            # /club-directory/al
            if len(parts) == 2 and parts[0] == "club-directory":
                state_code = self._normalize_state_token(parts[1]) or self._normalize_state_token(text)

            # /club-directory#AL or /club-directory#California
            elif path == "club-directory" and parsed.fragment:
                state_code = self._normalize_state_token(parsed.fragment) or self._normalize_state_token(text)

            if not state_code:
                continue

            jobs.append(
                _StateJob(
                    state_code=state_code,
                    href=f"/club-directory/{state_code.lower()}",
                )
            )

        return self._dedupe_state_jobs(jobs)

    def _parse_state_leaf_jobs(
        self,
        html: str,
        *,
        state_code: str | None,
    ) -> list[_LeafJob]:
        """Parse state leaf jobs.

        :param html: HTML content to parse.
        :param state_code: State code associated with the page.
        :return: Result produced by parse state leaf jobs.
        """
        soup = BeautifulSoup(html, "html.parser")
        jobs: list[_LeafJob] = []

        for anchor in soup.select("a[href]"):
            href = self._clean_text(anchor.get("href"))
            if not href:
                continue

            abs_url = urljoin(BASE_URL + "/", href.lstrip("/"))
            path = urlparse(abs_url).path.strip("/")
            parts = [part for part in path.split("/") if part]

            # Direct detail page:
            # /club/4989-auburn-al
            if len(parts) >= 2 and parts[0] == "club":
                if self._extract_store_id_from_url(href):
                    jobs.append(
                        _LeafJob(
                            href=href,
                            page_type="detail",
                            state_code=state_code,
                            city_name=self._clean_text(anchor.get_text(" ", strip=True)),
                        )
                    )
                continue

            # Multi-club city page:
            # /club-directory/al/huntsville
            if len(parts) >= 3 and parts[0] == "club-directory":
                if parts[1].lower() != (state_code or "").lower():
                    continue

                city_slug = parts[2]
                jobs.append(
                    _LeafJob(
                        href=href,
                        page_type="city",
                        state_code=state_code,
                        city_slug=city_slug,
                        city_name=self._clean_text(anchor.get_text(" ", strip=True)),
                    )
                )

        return self._dedupe_leaf_jobs(jobs)

    def _parse_artifact(
        self,
        artifact: AcquisitionArtifact,
    ) -> list[dict[str, Any]]:
        """Parse artifact.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse artifact.
        """
        page_type = artifact.metadata.get("page_type")

        if page_type == "detail":
            row = self._parse_detail_page(artifact)
            return [row] if row is not None else []

        if page_type == "city":
            return self._parse_city_page(artifact)

        return []

    def _parse_detail_page(self, artifact: AcquisitionArtifact) -> dict[str, Any] | None:
        """Parse detail page.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse detail page.
        """
        soup = BeautifulSoup(artifact.content or "", "html.parser")
        source_url = self._clean_text(artifact.source_url)

        store_id = self._extract_store_id_from_url(source_url)
        if not store_id:
            store_id = self._extract_store_id_from_heading(soup)

        if not store_id:
            return None

        heading = self._clean_text(soup.select_one("h1"))
        address_text = self._clean_text(soup.select_one('[data-testid="store-address"]'))

        phone = self._clean_text(soup.select_one('[data-testid="store-phone"] a[href^="tel:"]'))
        if not phone:
            phone = self._clean_text(soup.select_one('a[href^="tel:"]'))

        street_address, city, state, zip_code = self._parse_full_address(address_text)

        return self._build_row(
            store_id=store_id,
            store_name=heading,
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
            phone=phone,
            store_url=source_url,
            source_url=source_url,
            state_code=self._clean_text(artifact.metadata.get("state_code")),
            city_slug=self._clean_text(artifact.metadata.get("city_slug")),
            city_name=self._clean_text(artifact.metadata.get("city_name")),
            scraped_at_utc=self._clean_text(artifact.metadata.get("retrieved_at_utc")),
        )

    def _parse_city_page(self, artifact: AcquisitionArtifact) -> list[dict[str, Any]]:
        """Parse city page.

        :param artifact: Acquisition artifact to parse.
        :return: Result produced by parse city page.
        """
        soup = BeautifulSoup(artifact.content or "", "html.parser")

        source_url = self._clean_text(artifact.source_url)
        state_code = self._clean_text(artifact.metadata.get("state_code"))
        city_slug = self._clean_text(artifact.metadata.get("city_slug"))
        city_name = self._clean_text(artifact.metadata.get("city_name"))

        rows: list[dict[str, Any]] = []

        info_links = soup.find_all("a", href=re.compile(r"^/club/\d+-"))

        for info_link in info_links:
            href = self._clean_text(info_link.get("href"))
            if not href:
                continue

            store_id = self._extract_store_id_from_url(href)
            if not store_id:
                continue

            card = self._find_club_card(info_link)
            if card is None:
                continue

            heading = self._clean_text(card.select_one("h3"))
            phone = self._clean_text(card.select_one('a[href^="tel:"]'))

            street_address, city, state, zip_code = self._extract_card_address(card)

            store_url = urljoin(BASE_URL + "/", href.lstrip("/"))

            rows.append(
                self._build_row(
                    store_id=store_id,
                    store_name=heading,
                    street_address=street_address,
                    city=city,
                    state=state,
                    zip_code=zip_code,
                    phone=phone,
                    store_url=store_url,
                    source_url=source_url,
                    state_code=state_code,
                    city_slug=city_slug,
                    city_name=city_name,
                    scraped_at_utc=self._clean_text(artifact.metadata.get("retrieved_at_utc")),
                )
            )

        return self._dedupe_rows(rows)

    def _find_club_card(self, info_link: Any) -> Any | None:
        """Find club card.

        :param info_link: Info link.
        :return: Result produced by find club card.
        """
        current = info_link

        for _ in range(8):
            current = current.parent
            if current is None:
                return None

            heading = current.find("h3")
            phone = current.find("a", href=re.compile(r"^tel:"))
            if heading and phone:
                return current

        return None

    def _extract_card_address(
        self,
        card: Any,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract card address.

        :param card: Store card HTML element.
        :return: Result produced by extract card address.
        """
        mb4 = card.select_one("div.mb4")
        if mb4:
            address_container = mb4.select_one("div.flex.flex-column.mb2")
            if address_container:
                direct_lines = [
                    self._clean_text(node)
                    for node in address_container.find_all("div", recursive=False)
                ]
                direct_lines = [line for line in direct_lines if line]
                if len(direct_lines) >= 2:
                    street_address = direct_lines[0]
                    city, state, zip_code = self._parse_city_state_zip(direct_lines[1])
                    return street_address, city, state, zip_code

        texts = [self._clean_text(text) for text in card.stripped_strings]
        texts = [text for text in texts if text]

        locality_pattern = re.compile(r"^(.+?),\s*([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$")

        locality_index: int | None = None
        city = None
        state = None
        zip_code = None

        for index, text in enumerate(texts):
            match = locality_pattern.match(text)
            if not match:
                continue

            locality_index = index
            city = match.group(1).strip()
            state = match.group(2).strip()
            zip_code = match.group(3).strip()
            break

        if locality_index is None:
            return None, None, None, None

        street_address = None
        for index in range(locality_index - 1, -1, -1):
            candidate = texts[index]
            if not candidate:
                continue
            if candidate == city:
                continue
            if len(candidate) > 120:
                continue
            if re.search(r"\d", candidate):
                street_address = candidate
                break

        return street_address, city, state, zip_code

    def _build_row(
        self,
        *,
        store_id: str,
        store_name: str | None,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
        phone: str | None,
        store_url: str | None,
        source_url: str | None,
        state_code: str | None,
        city_slug: str | None,
        city_name: str | None,
        scraped_at_utc: str | None,
    ) -> dict[str, Any]:
        """Build row.

        :param store_id: Store id.
        :param store_name: Store name.
        :param street_address: Street address component.
        :param city: City entry to process.
        :param state: State name or abbreviation.
        :param zip_code: Postal code component.
        :param phone: Phone.
        :param store_url: Store url.
        :param source_url: Source URL associated with the record.
        :param state_code: State code associated with the page.
        :param city_slug: City slug associated with the page.
        :param city_name: City name associated with the page.
        :param scraped_at_utc: Scraped at utc.
        :return: Result produced by build row.
        """
        full_address = self._compose_full_address(
            street_address=street_address,
            city=city,
            state=state,
            zip_code=zip_code,
        )

        return {
            "retailer": self.retailer_name,
            "retailer_store_id": store_id,
            "store_number": store_id,
            "store_type": "Club",
            "store_name": store_name,
            "address": street_address,
            "street_address": street_address,
            "city": city,
            "state": state,
            "address_city": city,
            "address_state": state,
            "zip_code": zip_code,
            "full_address": full_address,
            "phone": phone,
            "store_url": store_url,
            "source_url": source_url,
            "source_sitemap": None,
            "state_code": state_code,
            "city_slug": city_slug,
            "city_name": city_name,
            "extraction_source": "HTML / BeautifulSoup",
            "scrape_status": "success",
            "http_status": 200,
            "error_message": None,
            "scraped_at_utc": scraped_at_utc or self._utc_now(),
        }

    @staticmethod
    def _extract_store_id_from_url(url: str | None) -> str | None:
        """Extract store id from url.

        :param url: URL to fetch or process.
        :return: Result produced by extract store id from url.
        """
        if not url:
            return None

        path = urlparse(url).path
        match = re.search(r"/club/(\d+)-", path)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _extract_store_id_from_heading(soup: BeautifulSoup) -> str | None:
        """Extract store id from heading.

        :param soup: Parsed HTML document.
        :return: Result produced by extract store id from heading.
        """
        heading = soup.select_one("h1")
        if not heading:
            return None

        text = heading.get_text(" ", strip=True)
        match = re.search(r"#(\d+)", text)
        if match:
            return match.group(1)

        return None

    @staticmethod
    def _parse_full_address(
        address: str | None,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Parse full address.

        :param address: Address.
        :return: Result produced by parse full address.
        """
        if not address:
            return None, None, None, None

        text = re.sub(r"\s+", " ", address).strip()

        match = re.match(
            r"^(?P<street>.+?),\s*(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            text,
        )
        if match:
            return (
                match.group("street").strip(),
                match.group("city").strip(),
                match.group("state").strip(),
                match.group("zip").strip(),
            )

        match = re.match(
            r"^(?P<street>.+?),\s*(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            text,
        )
        if match:
            return (
                match.group("street").strip(),
                match.group("city").strip(),
                match.group("state").strip(),
                match.group("zip").strip(),
            )

        return text, None, None, None

    @staticmethod
    def _parse_city_state_zip(
        text: str | None,
    ) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip.

        :param text: Text.
        :return: Result produced by parse city state zip.
        """
        if not text:
            return None, None, None

        cleaned = re.sub(r"\s+", " ", text).strip()
        cleaned = cleaned.replace(" ,", ",")

        patterns = [
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})\s+(?P<zip>\d{5}(?:-\d{4})?)$",
            r"^(?P<city>.+?),\s*(?P<state>[A-Z]{2})$",
            r"^(?P<city>.+?)\s+(?P<state>[A-Z]{2})$",
        ]

        for pattern in patterns:
            match = re.match(pattern, cleaned)
            if not match:
                continue

            city = match.groupdict().get("city")
            state = match.groupdict().get("state")
            zip_code = match.groupdict().get("zip")

            return (
                city.strip() if city else None,
                state.strip().upper() if state else None,
                zip_code.strip() if zip_code else None,
            )

        zip_match = re.search(r"(\d{5}(?:-\d{4})?)", cleaned)
        zip_code = zip_match.group(1) if zip_match else None

        state_match = re.search(r"\b([A-Z]{2})\b", cleaned)
        state = state_match.group(1) if state_match else None

        city = cleaned
        if state:
            city = re.sub(rf"\b{re.escape(state)}\b", "", city).strip(" ,")
        if zip_code:
            city = city.replace(zip_code, "").strip(" ,")

        city = re.sub(r"\s+", " ", city).strip() or None
        return city, state, zip_code

    @staticmethod
    def _compose_full_address(
        *,
        street_address: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> str | None:
        """Handle compose full address.

        :param street_address: Street address component.
        :param city: City entry to process.
        :param state: State name or abbreviation.
        :param zip_code: Postal code component.
        :return: Result produced by compose full address.
        """
        if not any([street_address, city, state, zip_code]):
            return None

        locality = ""
        if city:
            locality = city
        if state:
            locality = f"{locality}, {state}" if locality else state
        if zip_code:
            locality = f"{locality} {zip_code}" if locality else zip_code

        parts = [part for part in [street_address, locality] if part]
        return ", ".join(parts) or None

    def _dedupe_state_jobs(self, jobs: Sequence[_StateJob]) -> list[_StateJob]:
        """Deduplicate state jobs.

        :param jobs: Acquisition jobs to deduplicate.
        :return: Result produced by dedupe state jobs.
        """
        output: list[_StateJob] = []
        seen: set[str] = set()
        for job in jobs:
            if job.state_code in seen:
                continue
            seen.add(job.state_code)
            output.append(job)
        return output

    def _dedupe_leaf_jobs(self, jobs: Sequence[_LeafJob]) -> list[_LeafJob]:
        """Deduplicate leaf jobs.

        :param jobs: Acquisition jobs to deduplicate.
        :return: Result produced by dedupe leaf jobs.
        """
        output: list[_LeafJob] = []
        seen: set[str] = set()
        for job in jobs:
            absolute_url = urljoin(BASE_URL + "/", job.href.lstrip("/"))
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            output.append(job)
        return output

    def _dedupe_rows(self, rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Deduplicate rows.

        :param rows: Store rows to deduplicate.
        :return: Result produced by dedupe rows.
        """
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            store_id = self._clean_text(row.get("retailer_store_id"))
            if not store_id:
                continue
            if store_id in seen:
                continue
            seen.add(store_id)
            output.append(row)
        return output

    @staticmethod
    def _normalize_state_token(token: str | None) -> str | None:
        """Normalize state token.

        :param token: State token to normalize.
        :return: Result produced by normalize state token.
        """
        if not token:
            return None
        cleaned = re.sub(r"\s+", " ", token).strip().upper()
        if not cleaned:
            return None
        if re.fullmatch(r"[A-Z]{2}", cleaned):
            return cleaned
        return STATE_NAME_TO_CODE.get(cleaned)

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text.

        :param value: Value to normalize or convert.
        :return: Result produced by clean text.
        """
        if value is None:
            return None
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = str(value).strip()
        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Handle utc now.

        :return: Result produced by utc now.
        """
        return datetime.now(timezone.utc).isoformat()