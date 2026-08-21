from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse
import re

from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from tqdm import tqdm

try:
    from services.store_service.capabilities.store_location_acquisition.protocals import (
        AcquisitionArtifact,
        AcquisitionSourceInfo,
        AcquisitionValidationResult,
        StoreLocationAcquisitionStrategy,
    )
except ImportError:  # pragma: no cover - compatibility fallback
    from services.store_service.capabilities.store_location_acquisition.protocols import (
        AcquisitionArtifact,
        AcquisitionSourceInfo,
        AcquisitionValidationResult,
        StoreLocationAcquisitionStrategy,
    )


ROOT_URL = "https://www.bjs.com/clubLocatorDetail?srsltid=AfmBOooabl-9yycoANU1h59a1HAtYcPk4gn5WDi1OdauLEfTGs5X35gm"


@dataclass(slots=True)
class _StateTownJob:
    """Represent StateTownJob data used by the acquisition strategy."""
    state_name: str
    town_name: str


class BjsAcquisitionStrategy(StoreLocationAcquisitionStrategy):
    """Represent BjsAcquisitionStrategy data used by the acquisition strategy."""
    retailer_key = "bjs"
    retailer_name = "BJ's Wholesale Club"

    def __init__(
        self,
        *,
        headless: bool = False,
        state_workers: int = 1,
        town_workers: int = 1,
        store_workers: int = 4,
        page_timeout_ms: int = 30_000,
        render_wait_ms: int = 1_500,
        max_retries: int = 2,
    ) -> None:
        """Initialize acquisition configuration and run state."""
        self.headless = headless
        self.state_workers = state_workers
        self.town_workers = town_workers
        self.store_workers = store_workers
        self.page_timeout_ms = page_timeout_ms
        self.render_wait_ms = render_wait_ms
        self.max_retries = max_retries

        self._failed_states: list[dict[str, Any]] = []
        self._failed_towns: list[dict[str, Any]] = []
        self._failed_detail_pages: list[dict[str, Any]] = []

    def discover_source(self) -> AcquisitionSourceInfo:
        """Return metadata describing the retailer's official acquisition source."""
        return AcquisitionSourceInfo(
            retailer_key=self.retailer_key,
            retailer_name=self.retailer_name,
            official_website_url="https://www.bjs.com/",
            store_locator_url=ROOT_URL,
            endpoint_url=ROOT_URL,
            source_type="html",
            provider="Playwright + BeautifulSoup",
            notes=(
                "BJ's Club Locator Detail uses State -> Town / City -> detail page. "
                "Each town resolves to a single club detail page, and the trailing "
                "four-digit suffix in the detail URL is used as retailer_store_id."
            ),
        )

    def build_run_notes(self) -> list[str]:
        """Return acquisition source and execution details for the run summary."""
        return [
            f"Source: {ROOT_URL}",
            "Method: Playwright locator discovery + Playwright detail capture + BeautifulSoup parsing",
            "Hierarchy: root -> state dropdown -> town dropdown -> detail page",
            "Dedup key: retailer_store_id from trailing four-digit suffix in detail URL",
            f"Workers: state={self.state_workers}, town={self.town_workers}, store={self.store_workers}",
        ]

    def fetch_raw_artifacts(self) -> list[AcquisitionArtifact]:
        """Fetch raw artifacts required for store location acquisition."""
        self._failed_states = []
        self._failed_towns = []
        self._failed_detail_pages = []

        artifacts: list[AcquisitionArtifact] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=self.headless)
            context = browser.new_context(
                viewport={"width": 1440, "height": 1600},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                locale="en-US",
            )

            page = context.new_page()
            try:
                self._open_locator_page(page)
                root_html = page.content()
                artifacts.append(
                    AcquisitionArtifact(
                        artifact_type="html",
                        source_url=ROOT_URL,
                        content=root_html,
                        metadata={
                            "retrieved_at_utc": self._utc_now(),
                            "page_type": "root",
                            "http_status": 200,
                            "scrape_status": "success",
                        },
                    )
                )

                state_names = self._discover_state_names(root_html)
                print(f"[BJ's] discovered states: {len(state_names)}")

                if not state_names:
                    raise RuntimeError(
                        "BJ's Club Locator Detail page was rendered successfully, "
                        "but no states were discovered."
                    )

                # Discover town names for each state first.
                state_to_towns: list[_StateTownJob] = []
                with tqdm(total=len(state_names), desc="BJ's states", unit="state") as pbar:
                    for state_name in state_names:
                        try:
                            towns = self._discover_town_names_for_state(page, state_name)
                            if not towns:
                                self._failed_states.append(
                                    {
                                        "state_name": state_name,
                                        "error": "No towns discovered for state",
                                    }
                                )
                            else:
                                for town_name in towns:
                                    state_to_towns.append(
                                        _StateTownJob(
                                            state_name=state_name,
                                            town_name=town_name,
                                        )
                                    )
                        except Exception as exc:
                            self._failed_states.append(
                                {
                                    "state_name": state_name,
                                    "error": str(exc),
                                }
                            )
                        finally:
                            pbar.update(1)

                state_to_towns = self._dedupe_state_town_jobs(state_to_towns)
                print(f"[BJ's] discovered towns: {len(state_to_towns)}")

                if not state_to_towns:
                    raise RuntimeError(
                        "BJ's Club Locator Detail page was rendered successfully, "
                        "but no towns were discovered."
                    )

                detail_pbar = tqdm(total=len(state_to_towns), desc="BJ's clubs", unit="club")
                try:
                    for job in state_to_towns:
                        try:
                            detail_url, detail_html = self._navigate_to_detail(
                                page,
                                state_name=job.state_name,
                                town_name=job.town_name,
                            )

                            artifacts.append(
                                AcquisitionArtifact(
                                    artifact_type="html",
                                    source_url=detail_url,
                                    content=detail_html,
                                    metadata={
                                        "retrieved_at_utc": self._utc_now(),
                                        "page_type": "detail",
                                        "state_name": job.state_name,
                                        "town_name": job.town_name,
                                        "detail_url": detail_url,
                                        "http_status": 200,
                                        "scrape_status": "success",
                                    },
                                )
                            )

                        except Exception as exc:
                            self._failed_towns.append(
                                {
                                    "state_name": job.state_name,
                                    "town_name": job.town_name,
                                    "error": str(exc),
                                }
                            )
                            artifacts.append(
                                AcquisitionArtifact(
                                    artifact_type="html",
                                    source_url=ROOT_URL,
                                    content="",
                                    metadata={
                                        "retrieved_at_utc": self._utc_now(),
                                        "page_type": "detail",
                                        "state_name": job.state_name,
                                        "town_name": job.town_name,
                                        "http_status": 500,
                                        "scrape_status": "failed",
                                        "error": str(exc),
                                    },
                                )
                            )
                        finally:
                            detail_pbar.update(1)
                finally:
                    detail_pbar.close()

                return artifacts
            finally:
                context.close()
                browser.close()

    def extract_store_payloads(
        self,
        artifacts: Sequence[AcquisitionArtifact],
    ) -> list[dict[str, Any]]:
        """Extract normalized store payloads from acquired artifacts."""
        parse_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.metadata.get("page_type") == "detail"
            and artifact.metadata.get("scrape_status") == "success"
            and artifact.content
        ]

        rows_by_store_id: dict[str, dict[str, Any]] = {}

        with tqdm(total=len(parse_artifacts), desc="Parsing BJ's clubs", unit="page") as pbar:
            with ThreadPoolExecutor(max_workers=self.store_workers) as pool:
                futures = {
                    pool.submit(self._parse_detail_artifact, artifact): artifact
                    for artifact in parse_artifacts
                }
                for future in as_completed(futures):
                    row = future.result()
                    if row is not None:
                        store_id = self._clean_text(row.get("retailer_store_id"))
                        if store_id:
                            rows_by_store_id[store_id] = row
                    pbar.update(1)

        return list(rows_by_store_id.values())

    def validate_store_payloads(
        self,
        payloads: Sequence[Mapping[str, Any]],
    ) -> AcquisitionValidationResult:
        """Validate acquired store payloads for completeness and uniqueness."""
        total_records = len(payloads)
        store_ids = [self._clean_text(row.get("retailer_store_id")) for row in payloads]
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

        missing_phones = sum(1 for row in payloads if not self._clean_text(row.get("phone")))
        missing_coordinates = sum(
            1
            for row in payloads
            if row.get("latitude") is None or row.get("longitude") is None
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
        if missing_coordinates:
            issue_counts["missing_coordinates"] = missing_coordinates
        if self._failed_states:
            issue_counts["failed_states"] = len(self._failed_states)
        if self._failed_towns:
            issue_counts["failed_towns"] = len(self._failed_towns)
        if self._failed_detail_pages:
            issue_counts["failed_detail_pages"] = len(self._failed_detail_pages)

        notes = [
            "State -> Town / City -> Detail page flow is discovered from the locator page.",
            "Each town is expected to resolve to a single BJ's club.",
            "retailer_store_id is extracted from the trailing four-digit URL suffix.",
        ]

        is_valid = (
            total_records > 0
            and missing_store_ids == 0
            and missing_addresses == 0
            and missing_phones == 0
            and missing_coordinates == 0
            and len(self._failed_states) == 0
            and len(self._failed_towns) == 0
            and len(self._failed_detail_pages) == 0
        )

        return AcquisitionValidationResult(
            is_valid=is_valid,
            total_records=total_records,
            unique_store_ids=unique_store_ids,
            missing_store_ids=missing_store_ids,
            missing_coordinates=missing_coordinates,
            non_us_records=0,
            duplicate_store_ids=duplicate_store_ids,
            issue_counts=issue_counts,
            notes=notes,
        )

    def _discover_state_names(self, root_html: str) -> list[str]:
        """Discover state names."""
        soup = BeautifulSoup(root_html or "", "html.parser")
        state_names: list[str] = []

        for node in soup.select("li.dropdownItem"):
            text = self._clean_text(node)
            if not text:
                continue
            if text in {"State", "Town / City", "Select State", "Select Town"}:
                continue
            state_names.append(text)

        return self._dedupe_text_list(state_names)

    def _discover_town_names_for_state(self, page: Any, state_name: str) -> list[str]:
        """Discover town names for state."""
        self._open_locator_page(page)
        self._select_state(page, state_name)
        self._wait_for_town_options(page)
        town_names = self._read_town_names(page)
        return self._dedupe_text_list(town_names)

    def _navigate_to_detail(
        self,
        page: Any,
        *,
        state_name: str,
        town_name: str,
    ) -> tuple[str, str]:
        """Handle navigate to detail."""
        self._open_locator_page(page)
        self._select_state(page, state_name)
        self._wait_for_town_options(page)
        self._select_town(page, town_name)
        detail_url = self._click_find_club(page)
        detail_html = page.content()
        if not self._is_detail_url(detail_url):
            raise RuntimeError(f"Unexpected BJ's detail URL: {detail_url}")
        return detail_url, detail_html

    def _open_locator_page(self, page: Any) -> None:
        """Open locator page."""
        response = page.goto(
            ROOT_URL,
            wait_until="domcontentloaded",
            timeout=self.page_timeout_ms,
        )

        print(
            f"[BJ's] root initial response status: "
            f"{response.status if response else 'unknown'}"
        )

        page.wait_for_timeout(self.render_wait_ms)
        self._wait_for_locator_page(page)

    def _wait_for_locator_page(self, page: Any) -> None:
        """Wait for for locator page."""
        try:
            page.wait_for_selector("div.custom-state-dropdown", timeout=self.page_timeout_ms)
        except PlaywrightTimeoutError as exc:
            body_text = page.locator("body").inner_text()[:3000]
            raise RuntimeError(
                "Timed out waiting for BJ's locator form.\n"
                f"URL: {page.url}\n"
                f"Title: {page.title()}\n"
                f"Body preview:\n{body_text}"
            ) from exc

    def _state_dropdown(self, page: Any) -> Any:
        """Handle state dropdown."""
        dropdowns = page.locator("div.custom-state-dropdown")
        if dropdowns.count() < 3:
            body_text = page.locator("body").inner_text()[:3000]
            raise RuntimeError(
                "No dropdown elements were found on BJ's Club Locator page.\n"
                f"URL: {page.url}\n"
                f"Title: {page.title()}\n"
                f"Body preview:\n{body_text}"
            )
        return dropdowns.nth(1)

    def _town_dropdown(self, page: Any) -> Any:
        """Handle town dropdown."""
        dropdowns = page.locator("div.custom-state-dropdown")
        if dropdowns.count() < 3:
            body_text = page.locator("body").inner_text()[:3000]
            raise RuntimeError(
                "No dropdown elements were found on BJ's Club Locator page.\n"
                f"URL: {page.url}\n"
                f"Title: {page.title()}\n"
                f"Body preview:\n{body_text}"
            )
        return dropdowns.nth(2)

    def _select_state(self, page: Any, state_name: str) -> None:
        """Select state."""
        dropdown = self._state_dropdown(page)
        self._open_dropdown(dropdown)
        option = dropdown.locator("li.dropdownItem").filter(has_text=re.compile(rf"^\s*{re.escape(state_name)}\s*$"))
        if option.count() == 0:
            option = page.locator("li.dropdownItem").filter(has_text=re.compile(rf"^\s*{re.escape(state_name)}\s*$"))
        if option.count() == 0:
            raise RuntimeError(f"Could not find BJ's state option: {state_name}")
        option.first.click()
        page.wait_for_timeout(self.render_wait_ms)

    def _select_town(self, page: Any, town_name: str) -> None:
        """Select town."""
        dropdown = self._town_dropdown(page)
        self._open_dropdown(dropdown)
        option = dropdown.locator("li.dropdownItem").filter(has_text=re.compile(rf"^\s*{re.escape(town_name)}\s*$"))
        if option.count() == 0:
            option = page.locator("li.dropdownItem").filter(has_text=re.compile(rf"^\s*{re.escape(town_name)}\s*$"))
        if option.count() == 0:
            raise RuntimeError(f"Could not find BJ's town option: {town_name}")
        option.first.click()
        page.wait_for_timeout(self.render_wait_ms)

    def _wait_for_town_options(self, page: Any) -> None:
        """Wait for for town options."""
        for _ in range(20):
            town_names = self._read_town_names(page)
            if town_names:
                return
            page.wait_for_timeout(500)

        body_text = page.locator("body").inner_text()[:3000]
        raise RuntimeError(
            "Timed out waiting for BJ's town options to populate.\n"
            f"URL: {page.url}\n"
            f"Title: {page.title()}\n"
            f"Body preview:\n{body_text}"
        )

    def _read_town_names(self, page: Any) -> list[str]:
        """Read town names."""
        dropdown = self._town_dropdown(page)
        labels = dropdown.locator("li.dropdownItem").evaluate_all(
            """els => els.map(el => (el.textContent || '').trim()).filter(Boolean)"""
        )
        return [self._clean_text(label) for label in labels if self._clean_text(label)]

    def _open_dropdown(self, dropdown: Any) -> None:
        """Open dropdown."""
        trigger = dropdown.locator("p.best-match")
        if trigger.count() > 0:
            trigger.first.click(force=True)
        else:
            dropdown.click(force=True)
        dropdown.page.wait_for_timeout(300)

    def _click_find_club(self, page: Any) -> str:
        """Click find club."""
        button = page.get_by_role("button", name=re.compile(r"FIND CLUB", re.I))
        if button.count() == 0:
            button = page.locator("button.continue")
        if button.count() == 0:
            raise RuntimeError("Could not locate BJ's FIND CLUB button.")

        try:
            with page.expect_navigation(wait_until="domcontentloaded", timeout=self.page_timeout_ms):
                button.first.click(force=True)
        except PlaywrightTimeoutError:
            button.first.click(force=True)
            page.wait_for_timeout(self.render_wait_ms)

        page.wait_for_timeout(self.render_wait_ms)
        return page.url

    def _parse_detail_artifact(self, artifact: AcquisitionArtifact) -> dict[str, Any] | None:
        """Parse detail artifact."""
        soup = BeautifulSoup(artifact.content or "", "html.parser")
        detail_url = self._clean_text(artifact.source_url)

        store_id = self._extract_store_id_from_url(detail_url)
        if not store_id:
            store_id = self._extract_store_id_from_heading(soup)

        if not store_id:
            return None

        store_name = self._extract_store_name(soup)
        street_address, city, state, zip_code = self._extract_address(soup)
        latitude, longitude = self._extract_coordinates(soup)
        phone = self._extract_primary_phone(soup)

        if city is None:
            city = self._extract_city_from_heading(soup)

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
            "store_url": detail_url,
            "source_url": detail_url,
            "street_address": street_address,
            "address": street_address,
            "city": city,
            "state": state,
            "address_city": city,
            "address_state": state,
            "zip_code": zip_code,
            "full_address": full_address,
            "phone": phone,
            "latitude": latitude,
            "longitude": longitude,
            "source_sitemap": None,
            "state_name": self._clean_text(artifact.metadata.get("state_name")),
            "town_name": self._clean_text(artifact.metadata.get("town_name")),
            "detail_url": detail_url,
            "extraction_source": "Playwright + BeautifulSoup",
            "scrape_status": "success",
            "http_status": 200,
            "error_message": None,
            "scraped_at_utc": self._clean_text(artifact.metadata.get("retrieved_at_utc"))
            or self._utc_now(),
        }

    def _extract_store_name(self, soup: BeautifulSoup) -> str | None:
        """Extract store name."""
        heading = soup.select_one("div.clubName h1.name")
        if not heading:
            heading = soup.select_one("h1.name")
        return self._clean_text(heading)

    def _extract_city_from_heading(self, soup: BeautifulSoup) -> str | None:
        """Extract city from heading."""
        headings = soup.select("div.clubName h1.name")
        if len(headings) < 2:
            return None

        text = self._clean_text(headings[1])
        if not text:
            return None

        cleaned = re.sub(r"\s+", "", text)
        match = re.match(r"^(?P<city>[^,]+),(?P<state>[A-Z]{2})$", cleaned)
        if match:
            return match.group("city").strip()

        if "," in text:
            return text.split(",", 1)[0].strip() or None
        return text or None

    def _extract_primary_phone(self, soup: BeautifulSoup) -> str | None:
        """Extract primary phone."""
        for selector in (
            "div.clubDetailDiv .mob-top p.normal-text.tel span.timing-detail",
            "div.clubDetailDiv p.normal-text.tel span.timing-detail",
            "p.normal-text.tel span.timing-detail",
            'a[href^="tel:"]',
        ):
            node = soup.select_one(selector)
            text = self._clean_text(node)
            if text and re.search(r"\(?\d{3}\)?[-\s]\d{3}[-\s]\d{4}", text):
                return text

        for text in soup.stripped_strings:
            cleaned = self._clean_text(text)
            if cleaned and re.search(r"\(?\d{3}\)?[-\s]\d{3}[-\s]\d{4}", cleaned):
                return cleaned
        return None

    def _extract_address(
        self,
        soup: BeautifulSoup,
    ) -> tuple[str | None, str | None, str | None, str | None]:
        """Extract address."""
        address_block = self._find_detail_address_block(soup)
        if not address_block:
            return None, None, None, None

        lines = [self._clean_text(node) for node in address_block.select("p.normal-text")]
        lines = [line for line in lines if line]

        street_address = None
        city = None
        state = None
        zip_code = None

        if lines:
            street_address = lines[0]
        if len(lines) >= 2:
            city, state, zip_code = self._parse_city_state_zip(lines[1])

        if state is None:
            state_tag = soup.select_one("abbr[title]")
            if state_tag:
                state = self._state_name_to_code(state_tag.get("title"))

        if city is None:
            city = self._extract_city_from_heading(soup)

        return street_address, city, state, zip_code

    def _find_detail_address_block(self, soup: BeautifulSoup) -> Any | None:
        """Find detail address block."""
        blocks = soup.select("div.clubDetailDiv div.address")
        for block in blocks:
            lines = [self._clean_text(node) for node in block.select("p.normal-text")]
            lines = [line for line in lines if line]
            if len(lines) >= 2:
                return block
        return None

    def _extract_coordinates(self, soup: BeautifulSoup) -> tuple[float | None, float | None]:
        """Extract coordinates."""
        for anchor in soup.select('a[href*="maps.google.com"], a[href*="google.com/maps"]'):
            href = anchor.get("href") or ""
            match = re.search(r"[?&]ll=(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", href)
            if match:
                try:
                    return float(match.group(1)), float(match.group(2))
                except ValueError:
                    continue
            match = re.search(r"@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)", href)
            if match:
                try:
                    return float(match.group(1)), float(match.group(2))
                except ValueError:
                    continue

        for meta_lat, meta_lon in (
            (soup.select_one('meta[itemprop="latitude"]'), soup.select_one('meta[itemprop="longitude"]')),
            (soup.select_one('meta[name="latitude"]'), soup.select_one('meta[name="longitude"]')),
        ):
            try:
                if meta_lat and meta_lat.get("content") and meta_lon and meta_lon.get("content"):
                    return float(meta_lat["content"]), float(meta_lon["content"])
            except ValueError:
                continue

        return None, None

    def _extract_store_id_from_url(self, url: str | None) -> str | None:
        """Extract store id from url."""
        if not url:
            return None

        path = urlparse(url).path.strip("/")
        if not path:
            return None

        match = re.search(r"/(\d{4})(?:$|[/?#])", urlparse(url).path)
        if match:
            return match.group(1)

        last_segment = path.split("/")[-1]
        match = re.search(r"(\d{4})$", last_segment)
        if match:
            return match.group(1)

        return None

    def _extract_store_id_from_heading(self, soup: BeautifulSoup) -> str | None:
        """Extract store id from heading."""
        heading = soup.select_one("h1.name")
        if not heading:
            return None
        text = heading.get_text(" ", strip=True)
        match = re.search(r"#(\d{4})\b", text)
        if match:
            return match.group(1)
        return None

    @staticmethod
    def _parse_city_state_zip(text: str | None) -> tuple[str | None, str | None, str | None]:
        """Parse city state zip."""
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
        state = state_match.group(1) if state else None

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
        """Compose full address."""
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

    @staticmethod
    def _state_name_to_code(state_name: str | None) -> str | None:
        """Handle state name to code."""
        if not state_name:
            return None

        mapping = {
            "ALABAMA": "AL",
            "CONNECTICUT": "CT",
            "DELAWARE": "DE",
            "FLORIDA": "FL",
            "GEORGIA": "GA",
            "INDIANA": "IN",
            "KENTUCKY": "KY",
            "MAINE": "ME",
            "MARYLAND": "MD",
            "MASSACHUSETTS": "MA",
            "MICHIGAN": "MI",
            "NEW HAMPSHIRE": "NH",
            "NEW JERSEY": "NJ",
            "NEW YORK": "NY",
            "NORTH CAROLINA": "NC",
            "OHIO": "OH",
            "PENNSYLVANIA": "PA",
            "RHODE ISLAND": "RI",
            "SOUTH CAROLINA": "SC",
            "TENNESSEE": "TN",
            "TEXAS": "TX",
            "VIRGINIA": "VA",
        }

        cleaned = re.sub(r"\s+", " ", state_name).strip().upper()
        if re.fullmatch(r"[A-Z]{2}", cleaned):
            return cleaned
        return mapping.get(cleaned)

    @staticmethod
    def _dedupe_state_town_jobs(jobs: Sequence[_StateTownJob]) -> list[_StateTownJob]:
        """Deduplicate state town jobs."""
        output: list[_StateTownJob] = []
        seen: set[tuple[str, str]] = set()
        for job in jobs:
            key = (job.state_name, job.town_name)
            if key in seen:
                continue
            seen.add(key)
            output.append(job)
        return output

    @staticmethod
    def _dedupe_text_list(values: Sequence[str]) -> list[str]:
        """Deduplicate text list."""
        output: list[str] = []
        seen: set[str] = set()
        for value in values:
            cleaned = re.sub(r"\s+", " ", value).strip()
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            output.append(cleaned)
        return output

    @staticmethod
    def _is_detail_url(url: str | None) -> bool:
        """Return whether detail url."""
        if not url:
            return False
        return bool(re.search(r"/cl/[^/]+/\d{4}(?:[/?#].*)?$", urlparse(url).path))

    @staticmethod
    def _clean_text(value: Any) -> str | None:
        """Normalize text."""
        if value is None:
            return None
        if hasattr(value, "get_text"):
            value = value.get_text(" ", strip=True)
        text = str(value).strip()
        return text or None

    @staticmethod
    def _utc_now() -> str:
        """Return the current UTC timestamp in ISO 8601 format."""
        return datetime.now(timezone.utc).isoformat()