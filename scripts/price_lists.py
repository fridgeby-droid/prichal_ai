"""Read-only live check using server environment. No secrets in command arguments."""
import argparse
import asyncio
import json
import sys

from app.services.price_lists import ExportError, download_image, get_price, list_prices


async def run(args):
    if args.price_id:
        data = await get_price(args.point_id, args.price_id)
        if args.check_image:
            image = next((i for p in data["products"] for i in p["images"]), None)
            if image:
                raw, mime, checksum = await download_image(image["download_path"].rsplit("/", 1)[1])
                data["image_check"] = {"status": "ok", "bytes": len(raw), "mime": mime, "sha256": checksum}
            else:
                data["image_check"] = {"status": "no_images"}
        if not args.full:
            data.pop("products")
    else:
        data = await list_prices(args.point_id)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("point_id")
    parser.add_argument("price_id", nargs="?")
    parser.add_argument("--check-image", action="store_true")
    parser.add_argument("--full", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(asyncio.wait_for(run(args), timeout=600))
    except Exception as exc:
        code = exc.code if isinstance(exc, ExportError) else type(exc).__name__
        print(json.dumps({"complete_snapshot": False, "error": code}), file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
