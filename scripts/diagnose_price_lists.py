"""Read-only price export diagnostic. Prints known field types, never source values."""
import argparse
import asyncio
import json
import sys

from app.services import price_lists as service


FIELDS = ("id", "name", "unit", "isParent", "hierarchicalId", "images")


def describe(value):
    result = {"type": type(value).__name__}
    if isinstance(value, (list, dict)):
        result["length"] = len(value)
    if isinstance(value, str):
        result["empty"] = not value.strip()
    if type(value) is int:
        result["positive"] = value > 0
    return result


def row_shape(row):
    if not isinstance(row, dict):
        return describe(row)
    return {key: describe(row[key]) if key in row else {"type": "missing"}
            for key in FIELDS}


def response_shape(data, key):
    result = describe(data)
    if not isinstance(data, dict):
        return result
    for field in (key, "outcome", "success", "error", "errors", "Error", "Errors"):
        result[field] = describe(data[field]) if field in data else {"type": "missing"}
    outcome = data.get("outcome")
    if isinstance(outcome, dict):
        value = outcome.get("hasMore")
        result["hasMore"] = value if type(value) is bool else describe(value)
    elif type(outcome) is bool:
        result["outcome_boolean"] = outcome
    rows = data.get(key)
    if isinstance(rows, list):
        # One sample of each shape, bounded; no names, identifiers or image URLs.
        samples, seen = [], set()
        for index, row in enumerate(rows):
            shape = row_shape(row)
            signature = json.dumps(shape, sort_keys=True)
            if signature not in seen and len(samples) < 8:
                seen.add(signature)
                samples.append({"row_index": index, "fields": shape})
        result["row_shapes"] = samples
    return result


async def run(point_id, price_id):
    history = []
    original = service.saby_client._get

    async def observed(path, params, *args, **kwargs):
        entry = {
            "stage": "price_lists" if path.endswith("price-list") else "products",
            "page": params.get("page"),
            "cursor_present": "position" in params,
        }
        history.append(entry)
        if len(history) > 2:
            history.pop(0)
        data = await original(path, params, *args, **kwargs)
        key = "priceLists" if entry["stage"] == "price_lists" else "nomenclatures"
        entry["response"] = response_shape(data, key)
        return data

    service.saby_client._get = observed
    try:
        data = await service.get_price(point_id, price_id)
        print(json.dumps({"complete_snapshot": True, "product_count": data["product_count"],
                          "folders_skipped": data["folders_skipped"]}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({
            "complete_snapshot": False,
            "error": exc.code if isinstance(exc, service.ExportError) else type(exc).__name__,
            "diagnostics": history,
        }, ensure_ascii=False, indent=2))
        return 1
    finally:
        service.saby_client._get = original


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("point_id")
    parser.add_argument("price_id")
    args = parser.parse_args()
    try:
        status = asyncio.run(asyncio.wait_for(run(args.point_id, args.price_id), 600))
    except Exception as exc:
        print(json.dumps({"complete_snapshot": False, "error": type(exc).__name__}))
        status = 1
    sys.exit(status)


if __name__ == "__main__":
    main()
