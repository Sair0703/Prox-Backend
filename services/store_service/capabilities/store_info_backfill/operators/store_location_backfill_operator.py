# services/store_service/capabilities/store_info_backfill/operators/store_location_backfill_operator.py

from __future__ import annotations

from datetime import datetime, timezone

from config.supabase import get_supabase_client
from services.store_service.models.base import (
    StoreLocationRecord,
)


class StoreLocationBackfillOperator:
    """
    Backfill normalized or repaired store information into a store-location row.

    The current implementation targets ``store_locations``. This persistence
    path is retained as a demo implementation because writeback to that table
    is currently unavailable in the active environment.
    """

    def __init__(
        self,
        client=None,
    ) -> None:
        """
        Initialize the store-location backfill operator.

        :param client: Optional Supabase client override.
        """
        self.client = (
            client
            or get_supabase_client()
        )

    def backfill(
        self,
        store_location: StoreLocationRecord,
    ) -> None:
        """
        Backfill available store fields into the canonical store-location row.

        Fields with ``None`` values are omitted so existing persisted values are
        not overwritten by missing input data.

        :param store_location: Store-location record containing values to write.
        :raises RuntimeError: If the persistence operation reports an error.
        """
        patch = {
            "retailer": store_location.retailer,
            "store_id": store_location.store_id,
            "latitude": store_location.latitude,
            "longitude": store_location.longitude,
            "address": store_location.address,
            "zip_code": store_location.zip_code,
            "full_address": store_location.full_address,
            "retailer_key": store_location.retailer_key,
            "geocode_source": store_location.geocode_source,
            "geocode_confidence": store_location.geocode_confidence,
            "geocoded_at": store_location.geocoded_at,
            "osm_id": store_location.osm_id,
            "source": store_location.source,
            "store_name": store_location.store_name,
            "city": store_location.city,
            "state": store_location.state,
            "show_on_map": store_location.show_on_map,
            "updated_at": datetime.now(
                timezone.utc
            ).isoformat(),
        }

        patch = {
            key: value
            for key, value in patch.items()
            if value is not None
        }

        response = (
            self.client.table("store_locations")
            .update(patch)
            .eq(
                "id",
                store_location.id,
            )
            .execute()
        )

        if response.error:
            raise RuntimeError(
                response.error
            )


__all__ = ["StoreLocationBackfillOperator"]
