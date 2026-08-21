from __future__ import annotations

import importlib
import inspect
import re
from collections.abc import Mapping
from typing import Any

from services.store_service.capabilities.store_info_normalization.store_info_normalization_service import (
    StoreInfoNormalizationService,
)
from services.store_service.capabilities.store_location_acquisition.protocals import (
    StoreLocationAcquisitionStrategy,
)


STRATEGY_MODULES: Mapping[str, str] = {
    "albertsons": "albertsons_acquisition_strategy",
    "aldi": "aldi_acquisition_strategy",
    "bestbuy": "bestbuy_acquisition_strategy",
    "bjs": "bjs_acquisition_strategy",
    "costco": "costco_acquisition_strategy",
    "cvs_pharmacy": "cvs_pharmacy_acquisition_strategy",
    "dollar_tree": "dollar_tree_acquisition_strategy",
    "family_dollar": "family_dollar_acquisition_strategy",
    "fareway": "fareway_acquisition_strategy",
    "food_lion": "food_lion_acquisition_strategy",
    "gelsons": "gelsons_acquisition_strategy_commented",
    "giant_company": "giant_company_acquisition_strategy",
    "giant_eagle": "giant_eagle_acquisition_strategy",
    "giant_food": "giant_food_acquisition_strategy",
    "hannaford": "hannaford_acquisition_strategy",
    "heb": "heb_acquisition_strategy",
    "hyvee": "hyvee_acquisition_strategy",
    "kroger": "kroger_acquisition_strategy",
    "meijer": "meijer_acquisition_strategy",
    "petco": "petco_acquisition_strategy",
    "petsmart": "petsmart_acquisition_strategy",
    "piggly_wiggly": "piggly_wiggly_acquisition_strategy",
    "safeway": "safeway_acquisition_strategy",
    "sams_club": "samsclub_acquisition_strategy",
    "shoprite": "shoprite_acquisition_strategy",
    "smart_final": "smart_final_acquisition_strategy",
    "sprouts": "sprouts_acquisition_strategy",
    "target": "target_acquisition_strategy",
    "trader_joes": "trader_joes_acquisition_strategy",
    "ulta": "ulta_acquisition_strategy",
    "walgreens": "walgreens_acquisitoin_strategy",
    "wegmans": "wegmans_acquisition_strategy",
    "whole_foods": "whole_foods_acquisition_strategy",
}


class StoreLocationAcquisitionStrategyRegistry:
    """
    Resolve a retailer name to its retailer-specific acquisition strategy.

    The registry owns strategy selection, while the acquisition service owns
    the common acquisition workflow. Strategy modules remain independent from
    StoreService orchestration.
    """

    def __init__(
        self,
        normalizer: StoreInfoNormalizationService | None = None,
    ) -> None:
        """
        Initialize the strategy registry.

        :param normalizer: Optional shared retailer normalization service.
        """
        self.normalizer = (
            normalizer
            or StoreInfoNormalizationService()
        )

    def get_strategy(
        self,
        retailer: str,
        *,
        strategy_kwargs: Mapping[str, Any] | None = None,
    ) -> StoreLocationAcquisitionStrategy:
        """
        Resolve and instantiate the strategy for a retailer name.

        :param retailer: Raw retailer name supplied by the caller.
        :param strategy_kwargs: Optional constructor arguments for the strategy.
        :return: Configured retailer-specific acquisition strategy.
        :raises ValueError: If the retailer is unsupported or cannot be resolved.
        """
        strategy_key = self._resolve_strategy_key(retailer)
        module_name = STRATEGY_MODULES.get(strategy_key)

        if module_name is None:
            raise ValueError(
                f"Unsupported retailer acquisition strategy: {retailer}"
            )

        module = importlib.import_module(
            f"services.store_service.capabilities.store_location_acquisition.strategies.{module_name}"
        )
        strategy_class = self._find_strategy_class(module)

        kwargs = dict(strategy_kwargs or {})
        strategy = strategy_class(**kwargs)

        if not isinstance(strategy, StoreLocationAcquisitionStrategy):
            raise TypeError(
                f"{strategy_class.__name__} does not satisfy "
                "StoreLocationAcquisitionStrategy"
            )

        return strategy

    def supported_retailers(self) -> tuple[str, ...]:
        """
        Return normalized retailer keys with acquisition strategies.

        :return: Sorted supported retailer keys.
        """
        return tuple(
            sorted(STRATEGY_MODULES)
        )

    def _resolve_strategy_key(
        self,
        retailer: str,
    ) -> str:
        """
        Normalize a retailer name into the registry key.

        :param retailer: Raw retailer name.
        :return: Canonical strategy key.
        """
        value = (retailer or "").strip()
        if not value:
            raise ValueError(
                "retailer must be a non-empty string"
            )

        key = (
            self.normalizer.normalize_retailer_key(value)
            or self.normalizer.make_retailer_key(value)
        )

        candidates = [
            self._slugify(value),
            self._slugify(key),
        ]

        # Compatibility aliases for source names that differ from the strategy key.
        aliases = {
            "cvs": "cvs_pharmacy",
            "cvs_pharmacy": "cvs_pharmacy",
            "samsclub": "sams_club",
            "sam_s_club": "sams_club",
            "sams_club": "sams_club",
            "traderjoes": "trader_joes",
            "trader_joes": "trader_joes",
            "smart_final": "smart_final",
            "smartandfinal": "smart_final",
            "whole_foods_market": "whole_foods",
            "wholefoods": "whole_foods",
            "giant_company": "giant_company",
            "the_giant_company": "giant_company",
            "giantfood": "giant_food",
            "giant_eagle": "giant_eagle",
            "pigglywiggly": "piggly_wiggly",
        }

        for candidate in candidates:
            if candidate in STRATEGY_MODULES:
                return candidate
            if candidate in aliases:
                return aliases[candidate]

        raise ValueError(
            f"Unsupported retailer acquisition strategy: {retailer}"
        )

    @staticmethod
    def _find_strategy_class(module: Any) -> type:
        """
        Find the retailer strategy class defined by an acquisition module.

        :param module: Imported retailer strategy module.
        :return: Concrete strategy class defined by the module.
        :raises TypeError: If no concrete strategy implementation is found.
        """
        candidates = [
            value
            for value in vars(module).values()
            if inspect.isclass(value)
            and value.__module__ == module.__name__
            and all(
                callable(getattr(value, method_name, None))
                for method_name in (
                    "discover_source",
                    "fetch_raw_artifacts",
                    "extract_store_payloads",
                    "validate_store_payloads",
                    "build_run_notes",
                )
            )
        ]

        if len(candidates) == 1:
            return candidates[0]

        if not candidates:
            raise TypeError(
                f"No acquisition strategy implementation found in {module.__name__}"
            )

        raise TypeError(
            f"Multiple acquisition strategy implementations found in "
            f"{module.__name__}: "
            + ", ".join(candidate.__name__ for candidate in candidates)
        )

    @staticmethod
    def _slugify(value: Any) -> str:
        """Convert a retailer name or key into a normalized lookup slug."""
        text = str(value or "").strip().lower()
        text = re.sub(r"[^a-z0-9]+", "_", text)
        text = re.sub(r"_+", "_", text)
        return text.strip("_")


__all__ = [
    "STRATEGY_MODULES",
    "StoreLocationAcquisitionStrategyRegistry",
]
