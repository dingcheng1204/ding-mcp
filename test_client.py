"""End-to-end test for ding-mcp, using the official MCP Python client.

Usage:
    python test_client.py                                   # local server
    python test_client.py https://<project>.vercel.app/mcp  # deployed server
    python test_client.py <url> --offline                   # skip checks that need the web

It talks to the server exactly like Claude Code does (initialize -> tools/list ->
tools/call), then downloads the image URLs the tools return and checks the files.
Exits with status 1 if any check fails, so it can also run in CI.
"""

import argparse
import asyncio
import io
import json
import sys
import time
from urllib.parse import urlparse

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from PIL import Image

EXPECTED_TOOLS = {
    "add",
    "current_time",
    "random_number",
    "search_images",
    "resize_image",
    "remove_background_as_png",
    "build_download_command",
}
# A stable, openly licensed test photo (Wikimedia Commons).
# The original file (not a /thumb/ URL): Wikimedia only serves a few fixed thumbnail widths.
SAMPLE_IMAGE = "https://upload.wikimedia.org/wikipedia/commons/3/3a/Cat03.jpg"
USER_AGENT = "ding-mcp-test/0.6 (https://github.com/dingcheng1204/ding-mcp)"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return ok


def payload(result) -> object:
    """structuredContent when the tool declares an output schema, else parse the text."""
    if result.structured_content is not None:
        data = result.structured_content
        return (
            data.get("result", data)
            if isinstance(data, dict) and set(data) == {"result"}
            else data
        )
    return json.loads(result.content[0].text)


async def fetch_image(url: str, timeout: float = 120) -> tuple[Image.Image | None, str]:
    async with httpx.AsyncClient(
        timeout=timeout, headers={"User-Agent": USER_AGENT}
    ) as client:
        resp = await client.get(url)
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:120]}"
    try:
        img = Image.open(io.BytesIO(resp.content))
        img.load()
        return img, f"{img.format} {img.size[0]}x{img.size[1]} {img.mode}"
    except Exception as exc:  # noqa: BLE001 - report anything as a failed check
        return None, f"not an image: {exc!r}"


async def run(url: str, offline: bool) -> None:
    base = f"{urlparse(url).scheme}://{urlparse(url).netloc}"
    print(f"Server: {url}\n")

    print("[HTTP]")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{base}/")
    check(
        "GET / health page",
        resp.status_code == 200 and resp.json().get("mcp_endpoint") == "/mcp",
        resp.text[:80],
    )

    async with streamable_http_client(url) as (read, write, *_):
        async with ClientSession(read, write) as session:
            print("\n[MCP protocol]")
            init = await session.initialize()
            check(
                "initialize",
                init.server_info.name == "ding-mcp",
                f"protocol {init.protocol_version}",
            )
            tools = {t.name: t for t in (await session.list_tools()).tools}
            missing = EXPECTED_TOOLS - set(tools)
            check(
                "tools/list has all tools",
                not missing,
                f"missing {missing}" if missing else f"{len(tools)} tools",
            )
            ann = (
                tools.get("remove_background_as_png")
                and tools["remove_background_as_png"].annotations
            )
            check("tool annotations present", bool(ann and ann.read_only_hint))

            print("\n[simple tools]")
            r = await session.call_tool("add", {"a": 2, "b": 3})
            check("add(2, 3) == 5", not r.is_error and payload(r) == 5)
            r = await session.call_tool("current_time", {"timezone": "Asia/Tokyo"})
            check(
                "current_time(Asia/Tokyo)",
                not r.is_error and "+09:00" in str(payload(r)),
                str(payload(r)),
            )
            r = await session.call_tool("current_time", {"timezone": "Mars/Base"})
            check("bad timezone -> isError", r.is_error)
            r = await session.call_tool("random_number", {"low": 5, "high": 10})
            check(
                "random_number(5, 10) in range",
                not r.is_error and 5 <= payload(r) <= 10,
                str(payload(r)),
            )
            r = await session.call_tool("random_number", {"low": 10, "high": 5})
            check("low > high -> isError", r.is_error)

            print("\n[validation]")
            r = await session.call_tool(
                "remove_background_as_png", {"image_url": "/home/me/cat.png"}
            )
            check("local path rejected", r.is_error)
            r = await session.call_tool(
                "build_download_command",
                {"urls": ["https://x/a.png"], "folder": "../etc"},
            )
            check("folder '../etc' rejected", r.is_error)
            r = await session.call_tool(
                "resize_image",
                {"image_url": "https://x/a.png", "width": 1, "height": 1},
            )
            check(
                "resize_image returns /img URL",
                not r.is_error and "/img?" in str(payload(r)),
            )

            if offline:
                print("\n(--offline: skipping checks that download from the web)")
                return

            print("\n[images: resize]")
            r = await session.call_tool(
                "resize_image",
                {
                    "image_url": SAMPLE_IMAGE,
                    "width": 200,
                    "height": 200,
                    "format": "jpg",
                },
            )
            img, info = await fetch_image(str(payload(r)))
            check(
                "resize_image -> 200x200 JPEG",
                bool(img) and img.size == (200, 200) and img.format == "JPEG",
                info,
            )

            print(
                "\n[images: remove background]  (first call may download the 44 MB model)"
            )
            r = await session.call_tool(
                "remove_background_as_png",
                {"image_url": SAMPLE_IMAGE, "width": 300, "height": 300},
            )
            started = time.monotonic()
            img, info = await fetch_image(payload(r)["url"])
            took = f"{time.monotonic() - started:.1f}s"
            ok = (
                bool(img)
                and img.format == "PNG"
                and img.mode == "RGBA"
                and max(img.size) == 300
            )
            if ok:
                alpha = img.getchannel("A")
                ok = (
                    alpha.getextrema()[0] < 50 and alpha.getextrema()[1] > 200
                )  # some transparent + some solid
                info += f", alpha range {alpha.getextrema()}"
            check("remove_background_as_png -> transparent PNG", ok, f"{info}, {took}")

            print("\n[images: search (DuckDuckGo)]")
            r = await session.call_tool(
                "search_images",
                {"keyword": "penguin", "count": 2, "kind": "photo", "top": 5},
            )
            if r.is_error:
                check("search_images", False, r.content[0].text[:150])
                return
            images = payload(r)["images"]
            check(
                "search_images returns 2 images",
                len(images) == 2,
                images[0]["credit"] if images else "",
            )
            r = await session.call_tool(
                "build_download_command",
                {"urls": [i["url"] for i in images], "name": "penguin"},
            )
            plan = payload(r)
            check(
                "build_download_command",
                not r.is_error and plan["command"].startswith("mkdir -p images"),
            )
            downloaded = 0
            for item in images:
                img, info = await fetch_image(item["url"])
                downloaded += bool(img and img.size == (200, 200))
                print(f"        {info}  <- {item['title'][:50]}")
            # Some websites block downloads, so one failure out of two is tolerated.
            check(
                "search result images download as 200x200",
                downloaded >= 1,
                f"{downloaded}/2 ok",
            )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("url", nargs="?", default="http://127.0.0.1:8000/mcp")
    parser.add_argument(
        "--offline", action="store_true", help="skip checks that need the internet"
    )
    args = parser.parse_args()

    try:
        asyncio.run(run(args.url, args.offline))
    except Exception as exc:  # noqa: BLE001 - connection problems etc.
        check("connect / run", False, repr(exc))

    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
