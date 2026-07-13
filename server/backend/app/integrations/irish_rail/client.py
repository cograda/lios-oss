"""Irish Rail real-time API client. Public API, no auth required.

API docs: http://api.irishrail.ie/realtime/
Note: all webservice names and parameters are case sensitive.
"""

import logging
import xml.etree.ElementTree as ET
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "http://api.irishrail.ie/realtime/realtime.asmx"
TIMEOUT = 15.0


def fetch_station_data(station_code: str, num_mins: int = 90) -> list[dict[str, Any]]:
    """Fetch real-time departure data for a station.

    Returns a list of train dicts sorted by due_in_mins ascending.
    """
    url = f"{BASE_URL}/getStationDataByCodeXML_WithNumMins"
    params = {"StationCode": station_code, "NumMins": str(num_mins)}

    try:
        response = httpx.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception(f"Failed to fetch station data for {station_code}")
        return []

    trains = _parse_station_data(response.text)
    trains.sort(key=lambda t: t["due_in_mins"])
    return trains


def fetch_train_movements(train_code: str, train_date: str) -> list[dict[str, Any]]:
    """Fetch all stops for a specific train service.

    Args:
        train_code: Irish Rail train code (e.g. "E109")
        train_date: Date string (e.g. "26 Mar 2026")
    """
    url = f"{BASE_URL}/getTrainMovementsXML"
    params = {"TrainId": train_code, "TrainDate": train_date}

    try:
        response = httpx.get(url, params=params, timeout=TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError:
        logger.exception(f"Failed to fetch movements for {train_code}")
        return []

    return _parse_train_movements(response.text)


def _parse_station_data(xml_text: str) -> list[dict[str, Any]]:
    """Parse Irish Rail station data XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.exception("Failed to parse Irish Rail XML response")
        return []

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    trains = []
    for obj in root.findall(f"{ns}objStationData"):
        train = {
            "train_code": _text(obj, f"{ns}Traincode"),
            "destination": _text(obj, f"{ns}Destination"),
            "origin": _text(obj, f"{ns}Origin"),
            "direction": _text(obj, f"{ns}Direction"),
            "due_in_mins": _int(obj, f"{ns}Duein"),
            "late_mins": _int(obj, f"{ns}Late"),
            "scheduled_arrival": _text(obj, f"{ns}Scharrival"),
            "scheduled_departure": _text(obj, f"{ns}Schdepart"),
            "expected_arrival": _text(obj, f"{ns}Exparrival"),
            "expected_departure": _text(obj, f"{ns}Expdepart"),
            "origin_time": _text(obj, f"{ns}Origintime"),
            "destination_time": _text(obj, f"{ns}Destinationtime"),
            "status": _text(obj, f"{ns}Status"),
            "train_type": _text(obj, f"{ns}Traintype"),
            "last_location": _text(obj, f"{ns}Lastlocation"),
            "location_type": _text(obj, f"{ns}Locationtype"),
            "train_date": _text(obj, f"{ns}Traindate"),
        }
        trains.append(train)

    return trains


def _parse_train_movements(xml_text: str) -> list[dict[str, Any]]:
    """Parse Irish Rail train movements XML response."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        logger.exception("Failed to parse Irish Rail movements XML")
        return []

    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"

    stops = []
    for obj in root.findall(f"{ns}objTrainMovements"):
        stop = {
            "location_code": _text(obj, f"{ns}LocationCode"),
            "location_name": _text(obj, f"{ns}LocationFullName"),
            "location_order": _int(obj, f"{ns}LocationOrder"),
            "location_type": _text(obj, f"{ns}LocationType"),
            "scheduled_arrival": _text(obj, f"{ns}ScheduledArrival"),
            "scheduled_departure": _text(obj, f"{ns}ScheduledDeparture"),
            "actual_arrival": _text(obj, f"{ns}Arrival"),
            "actual_departure": _text(obj, f"{ns}Departure"),
            "stop_type": _text(obj, f"{ns}StopType"),
        }
        stops.append(stop)

    stops.sort(key=lambda s: s["location_order"])
    return stops


def _text(element: ET.Element, tag: str) -> str | None:
    """Extract text from a child element, or None if missing/empty."""
    child = element.find(tag)
    if child is not None and child.text:
        return child.text.strip()
    return None


def _int(element: ET.Element, tag: str) -> int:
    """Extract integer from a child element, defaulting to 0."""
    val = _text(element, tag)
    if val is not None:
        try:
            return int(val)
        except ValueError:
            return 0
    return 0
