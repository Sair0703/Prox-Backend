# services/store_service/capabilities/store_location_acquisition/constants.py

"""Shared paths for store location acquisition."""

from pathlib import Path


ACQUISITION_ROOT = Path(__file__).resolve().parent

INPUT_ROOT = ACQUISITION_ROOT / "input"
OUTPUT_ROOT = ACQUISITION_ROOT / "_output"