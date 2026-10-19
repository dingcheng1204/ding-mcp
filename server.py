"""ding-mcp: a remote MCP server (Streamable HTTP) for finding and editing images.

Layout of this file:
    1. Config & helpers
    2. Safe image download     -> SSRF-checked fetch used by /img
    3. Background removal      -> U^2-Net "silueta" model run with onnxruntime
    4. DuckDuckGo search       -> search_duckduckgo()
    5. Server object
    6. Simple tools            -> add, current_time
    7. Image tools             -> search_images, resize_image, remove_background_as_png,
                                  build_download_command
    8. HTTP routes             -> GET /img (download, remove background, resize), GET /
    9. ASGI app                -> `app` (Vercel imports this) + local entry point

Design note: this server may run on Vercel, so it never writes results to "its own"
disk. Image tools return URLs that point at GET /img on this server; the image is
processed on the fly when the URL is downloaded. build_download_command turns the
URLs into a curl command that Claude Code runs on the user's machine, so files end
up in the user's local folder even though the work happens in the cloud.
"""

import asyncio
import hashlib
import io
import ipaddress
import os
import random
import re
import shlex
import socket
import tempfile
import warnings
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import numpy as np
import onnxruntime as ort
from ddgs import DDGS
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

# ---------------------------------------------------------------------------
# 1. Config & helpers
# ---------------------------------------------------------------------------

MIN_SIZE, MAX_SIZE = 16, 2000
MAX_COUNT = 10
SEARCH_RESULTS = 30  # how many results to ask DuckDuckGo for
MAX_DOWNLOAD_BYTES = 15 * 1024 * 1024  # refuse source images larger than 15 MB
MAX_RESPONSE_BYTES = 4_400_000  # Vercel Functions can't return more than 4.5 MB
Image.MAX_IMAGE_PIXELS = 40_000_000  # refuse "decompression bomb" images (> 40 MP)

FORMATS = {"png": ("PNG", "image/png"), "jpg": ("JPEG", "image/jpeg")}
# kind -> DuckDuckGo's type_image filter (None = no filter)
KINDS = {"any": None, "photo": "photo", "illustration": "clipart"}
# crop    = fill w x h exactly, cutting off the edges
# contain = keep aspect ratio, fit inside w x h (one side may be smaller)
# stretch = force w x h exactly, distorting if needed
RESIZE_MODES = ("crop", "contain", "stretch")

# Send an identifiable User-Agent rather than pretending to be a browser.
USER_AGENT = "ding-mcp/0.6 (https://github.com/dingcheng1204/ding-mcp)"

# Every image tool only computes a URL: no side effects, same input -> same output,
# and it talks to the outside world (the web). These hints follow the MCP spec.
READ_ONLY_WEB = ToolAnnotations(
    readOnlyHint=True, idempotentHint=True, openWorldHint=True
)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(value, high))


def _normalize_format(fmt: str) -> str:
    """Accept png / jpg / jpeg (any case, optional dot) and return 'png' or 'jpg'."""
    fmt = fmt.lower().strip().lstrip(".")
    fmt = "jpg" if fmt == "jpeg" else fmt
    if fmt not in FORMATS:
        raise ValueError("format must be png or jpg")
    return fmt


def _plain(text: str | None) -> str:
    """'[label](url)' -> 'label'; DuckDuckGo sometimes returns markdown links."""
    return re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text or "").strip()


def http_client(**kwargs) -> httpx.AsyncClient:
    """One place to configure outgoing HTTP: timeout, redirects and User-Agent."""
    kwargs.setdefault("follow_redirects", True)
    kwargs.setdefault("timeout", 15)
    return httpx.AsyncClient(headers={"User-Agent": USER_AGENT}, **kwargs)


# ---------------------------------------------------------------------------
# 2. Safe image download
#
# GET /img downloads images from arbitrary websites (DuckDuckGo results can be
# anywhere), so it must not let callers make the server fetch internal
# addresses such as 127.0.0.1 or the cloud metadata service 169.254.169.254
# (SSRF). Every URL, including every redirect hop, must use http(s) and resolve
# only to public IP addresses.
# ---------------------------------------------------------------------------


async def resolve_ips(host: str) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(
        host, None, type=socket.SOCK_STREAM
    )
    return [info[4][0] for info in infos]


async def assert_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("only http(s) URLs are allowed")
    try:
        ips = await resolve_ips(parsed.hostname)
    except OSError as exc:
        raise ValueError(f"cannot resolve host {parsed.hostname!r}") from exc
    for ip in ips:
        if not ipaddress.ip_address(ip).is_global:
            raise ValueError(
                f"host {parsed.hostname!r} resolves to non-public address {ip}"
            )


async def fetch_image_bytes(url: str, max_redirects: int = 3) -> bytes:
    """Download url with SSRF checks on every hop and a size limit."""
    async with http_client(follow_redirects=False) as client:
        for _ in range(max_redirects + 1):
            await assert_public_url(url)
            async with client.stream("GET", url) as resp:
                if resp.is_redirect:
                    url = urljoin(url, resp.headers["location"])
                    continue
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_DOWNLOAD_BYTES:
                        raise ValueError("source image is larger than 15 MB")
                return bytes(data)
    raise ValueError("too many redirects")


def resize(img: Image.Image, width: int, height: int, mode: str) -> Image.Image:
    if mode == "crop":
        return ImageOps.fit(img, (width, height), Image.LANCZOS)
    if mode == "contain":
        return ImageOps.contain(img, (width, height), Image.LANCZOS)
    return img.resize((width, height), Image.LANCZOS)  # stretch


def encode_image(img: Image.Image, fmt: str) -> tuple[bytes, str]:
    """Encode in memory (nothing touches the disk) and return (bytes, media type)."""
    pil_format, media_type = FORMATS[fmt]
    buf = io.BytesIO()
    if pil_format == "JPEG":
        img.convert("RGB").save(buf, format=pil_format, quality=90)
    else:
        img.save(buf, format=pil_format, optimize=True)
    return buf.getvalue(), media_type


# ---------------------------------------------------------------------------
# 3. Background removal
#
# Uses "silueta" (44 MB), a compact U^2-Net salient-object model published with
# the open-source rembg project, run directly with onnxruntime (rembg itself
# pulls in much heavier dependencies). The model predicts, for every pixel, how
# likely it belongs to the main object; that becomes the PNG's alpha channel.
#
# The model file is not stored in git. On first use it is downloaded once,
# checked against a pinned SHA-256 (so a tampered download is rejected), and
# cached: in /tmp on Vercel (the only writable folder there), otherwise in
# ~/.cache/ding-mcp. Set BG_MODEL_PATH to use a file you already have.
# ---------------------------------------------------------------------------

MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/silueta.onnx"
MODEL_SHA256 = "75da6c8d2f8096ec743d071951be73b4a8bc7b3e51d9a6625d63644f90ffeedb"
MODEL_INPUT = 320  # the network's fixed input size (320 x 320)
MAX_BG_INPUT = 1500  # shrink bigger inputs first: the mask is only 320 px anyway
IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)

_session: ort.InferenceSession | None = None
_session_lock = asyncio.Lock()


def _model_path() -> Path:
    if os.environ.get("BG_MODEL_PATH"):
        return Path(os.environ["BG_MODEL_PATH"])
    cache = (
        Path(tempfile.gettempdir())
        if os.environ.get("VERCEL")
        else Path.home() / ".cache"
    )
    return cache / "ding-mcp" / "silueta.onnx"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _download_model(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".part")
    async with http_client(timeout=120) as client:
        async with client.stream("GET", MODEL_URL) as resp:
            resp.raise_for_status()
            with partial.open("wb") as f:
                async for chunk in resp.aiter_bytes():
                    f.write(chunk)
    if _sha256(partial) != MODEL_SHA256:
        partial.unlink(missing_ok=True)
        raise RuntimeError(
            "downloaded background-removal model failed its SHA-256 check"
        )
    partial.replace(path)  # atomic: concurrent readers never see a half-written file


async def get_session() -> ort.InferenceSession:
    """Load the model once per process (lazily, so startup and other tools stay fast)."""
    global _session
    async with _session_lock:  # two requests at once must not both download the model
        if _session is None:
            path = _model_path()
            if not path.exists():
                await _download_model(path)
            _session = await asyncio.to_thread(
                ort.InferenceSession, str(path), providers=["CPUExecutionProvider"]
            )
    return _session


def _cut_out(session: ort.InferenceSession, img: Image.Image) -> Image.Image:
    """Return an RGBA copy of img whose alpha channel is the predicted foreground mask."""
    rgb = img.convert("RGB")
    rgb.thumbnail((MAX_BG_INPUT, MAX_BG_INPUT), Image.LANCZOS)

    # Pre-processing expected by U^2-Net: 320x320, scaled to 0..1, ImageNet-normalised, NCHW.
    x = np.asarray(
        rgb.resize((MODEL_INPUT, MODEL_INPUT), Image.LANCZOS), dtype=np.float32
    )
    x = x / max(float(x.max()), 1e-6)
    x = (x - IMAGENET_MEAN) / IMAGENET_STD
    x = x.transpose(2, 0, 1)[np.newaxis]

    pred = session.run(None, {session.get_inputs()[0].name: x})[0][0, 0]
    pred = (pred - pred.min()) / max(float(pred.max() - pred.min()), 1e-6)
    mask = Image.fromarray((pred * 255).astype(np.uint8)).resize(
        rgb.size, Image.LANCZOS
    )

    out = rgb.convert("RGBA")
    out.putalpha(mask)
    return out


async def remove_background(img: Image.Image) -> Image.Image:
    session = await get_session()
    # Inference is CPU-bound: run it in a worker thread so the event loop stays responsive.
    return await asyncio.to_thread(_cut_out, session, img)


# ---------------------------------------------------------------------------
# 4. DuckDuckGo search
# ---------------------------------------------------------------------------


@dataclass
class Hit:
    title: str
    image_url: str  # the image itself (processed on the fly by /img)
    page_url: str  # web page the image was found on
    site: str  # website name, for the credit line


async def search_duckduckgo(keyword: str, kind: str) -> list[Hit]:
    """Search the whole web with DuckDuckGo; results come back in relevance order.

    The `ddgs` library scrapes DuckDuckGo (no API key), so it can be rate-limited,
    and the images come from any website with unknown copyright.
    """

    def run() -> list[dict]:
        # ddgs is synchronous, so this runs in a worker thread (asyncio.to_thread below)
        # instead of blocking the server's event loop.
        return DDGS().images(
            keyword,
            region="wt-wt",
            safesearch="moderate",
            type_image=KINDS[kind],
            max_results=SEARCH_RESULTS,
        )

    try:
        results = await asyncio.to_thread(run)
    except (
        Exception
    ) as exc:  # ddgs raises its own exception types for rate limits, timeouts, ...
        raise ToolError(
            f"DuckDuckGo search failed (maybe rate-limited, retry later): {exc!r}"
        ) from exc

    hits = []
    for item in results:
        url = item.get("image", "")
        if url.startswith(("http://", "https://")):  # skip data: URLs and the like
            hits.append(
                Hit(
                    title=item.get("title") or "untitled",
                    image_url=url,
                    page_url=item.get("url", ""),
                    site=_plain(item.get("source")) or urlparse(url).hostname or "web",
                )
            )
    return hits


# ---------------------------------------------------------------------------
# 5. Server object
# ---------------------------------------------------------------------------

mcp = MCPServer(
    name="ding-mcp",
    instructions=(
        "Ding's image MCP server. Every image tool returns URLs on this server; nothing is "
        "saved until you download them. To save pictures for the user: call search_images "
        "(set remove_background=true for transparent PNGs), then build_download_command, "
        "then run the returned command with your Bash tool. remove_background_as_png and "
        "resize_image work on any online image URL, including URLs returned by search_images. "
        "Always tell the user each saved image's credit and page_url."
    ),
)


# ---------------------------------------------------------------------------
# 6. Simple tools
# ---------------------------------------------------------------------------


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False
    )
)
def add(a: float, b: float) -> float:
    """Add two numbers and return the sum."""
    return a + b


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def current_time(timezone: str = "Asia/Taipei") -> str:
    """Return the current time in an IANA timezone, e.g. 'Asia/Taipei' or 'Asia/Tokyo'."""
    try:
        tz = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ToolError(
            f"Unknown timezone {timezone!r}; use an IANA name like 'Asia/Tokyo'"
        ) from exc
    return datetime.now(tz).isoformat(timespec="seconds")


@mcp.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
def random_number(low: int = 1, high: int = 100) -> int:
    """Return a random integer between low and high (both inclusive)."""
    if low > high:
        raise ToolError(f"low ({low}) must not be greater than high ({high})")
    return random.randint(low, high)


# ---------------------------------------------------------------------------
# 7. Image tools
# ---------------------------------------------------------------------------


def _base_url(ctx: Context) -> str:
    """Work out this server's public URL from the request headers (local or Vercel)."""
    headers = ctx.headers or {}
    host = headers.get("x-forwarded-host") or headers.get("host") or "127.0.0.1:8000"
    is_local = host.startswith(("127.0.0.1", "localhost"))
    proto = headers.get("x-forwarded-proto") or ("http" if is_local else "https")
    return f"{proto}://{host}"


def _img_url(
    base: str,
    src: str,
    width: int,
    height: int,
    mode: str,
    fmt: str,
    nobg: bool = False,
) -> str:
    """Build a GET /img URL. width/height 0 = keep the original size."""
    params = {"src": src, "w": width, "h": height, "mode": mode, "format": fmt}
    if nobg:
        params["nobg"] = 1
    return f"{base}/img?{urlencode(params)}"


def _unwrap_own_url(url: str) -> tuple[str, dict[str, str]]:
    """If url is one of our own /img URLs, return (original src, its params)."""
    parsed = urlparse(url)
    if parsed.path.endswith("/img"):
        params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if params.get("src"):
            return params["src"], params
    return url, {}


def _validate_size_and_format(
    width: int, height: int, format: str, resize_mode: str
) -> tuple:
    """0 x 0 means "keep the original size"; otherwise clamp to the allowed range."""
    try:
        fmt = _normalize_format(format)
    except ValueError as exc:
        raise ToolError(str(exc)) from exc
    mode = resize_mode.lower().strip()
    if mode not in RESIZE_MODES:
        raise ToolError(f"resize_mode must be one of {', '.join(RESIZE_MODES)}")
    if (width <= 0) != (height <= 0):
        raise ToolError(
            "give both width and height, or neither (0) to keep the original size"
        )
    if width > 0:
        width, height = (
            _clamp(width, MIN_SIZE, MAX_SIZE),
            _clamp(height, MIN_SIZE, MAX_SIZE),
        )
    return width, height, fmt, mode


@mcp.tool(annotations=READ_ONLY_WEB)
async def search_images(
    ctx: Context,
    keyword: str,
    count: int = 2,
    width: int = 200,
    height: int = 200,
    format: str = "png",
    resize_mode: str = "crop",
    kind: str = "any",
    top: int = 8,
    remove_background: bool = False,
) -> dict:
    """Step 1 of saving pictures: search the web (DuckDuckGo) and pick random results.

    keyword: English works best, e.g. "penguin", "cat wearing a hat".
    kind: "any", "photo" or "illustration". Use "illustration" for cartoons,
        drawings, clipart or anime-style requests. The filter is best-effort, so
        check the downloaded images.
    top: pick randomly only among the `top` most relevant results.
        Use a small value (e.g. 3) for specific requests, larger (up to 30) for variety.
    width, height, format (png/jpg), resize_mode: the size and format of the files
        the returned URLs produce. resize_mode "crop" (default) fills width x height
        exactly by trimming edges; "contain" keeps the whole image inside width x
        height; "stretch" forces width x height.
    remove_background: true = cut out the main object and make the rest
        transparent (output is always PNG; resize_mode "contain" is usually best,
        so the object isn't cropped).

    Images come from arbitrary websites and their copyright is unknown: fine for
    personal reference, but check page_url before publishing anything.

    Returns {"images": [...]}. To save them on the user's computer, call
    build_download_command next with the "url" values, and tell the user each
    image's "credit" and "page_url".
    """
    keyword = keyword.strip()
    if not keyword:
        raise ToolError("keyword must not be empty")
    if remove_background:
        format = "png"  # transparency needs PNG
    width, height, fmt, mode = _validate_size_and_format(
        width, height, format, resize_mode
    )
    kind = kind.lower().strip()
    if kind not in KINDS:
        raise ToolError(f"kind must be one of {', '.join(KINDS)}")
    count = _clamp(count, 1, MAX_COUNT)
    top = _clamp(top, 1, SEARCH_RESULTS)

    # Results arrive sorted by relevance; pick randomly among the best `top`.
    hits = (await search_duckduckgo(keyword, kind))[:top]
    if not hits:
        raise ToolError(f"No images found for {keyword!r}; try another English keyword")

    base = _base_url(ctx)
    images = [
        {
            "title": hit.title,
            "credit": f"{hit.site} (copyright unknown - check the page)",
            "page_url": hit.page_url,
            "url": _img_url(
                base, hit.image_url, width, height, mode, fmt, nobg=remove_background
            ),
        }
        for hit in random.sample(hits, min(count, len(hits)))
    ]
    return {"images": images, "format": fmt}


@mcp.tool(annotations=READ_ONLY_WEB)
def resize_image(
    ctx: Context,
    image_url: str,
    width: int,
    height: int,
    format: str = "png",
    resize_mode: str = "crop",
) -> str:
    """Resize an image that is already online (any public http/https image URL).

    Returns a URL that serves the resized image; pass it to build_download_command
    to save it. resize_mode: "crop" (fill exactly, trim edges), "contain" (keep the
    whole image, aspect ratio kept) or "stretch" (force exact size).

    This server cannot read files on the user's computer; for a local file, resize
    it locally instead (e.g. with Python Pillow or ImageMagick).
    """
    if not image_url.startswith(("http://", "https://")):
        raise ToolError(
            "image_url must be an http(s) URL; this server cannot read local files"
        )
    src, previous = _unwrap_own_url(image_url)
    width, height, fmt, mode = _validate_size_and_format(
        width, height, format, resize_mode
    )
    nobg = previous.get("nobg") == "1"
    return _img_url(
        _base_url(ctx), src, width, height, mode, "png" if nobg else fmt, nobg=nobg
    )


@mcp.tool(annotations=READ_ONLY_WEB)
def remove_background_as_png(
    ctx: Context,
    image_url: str,
    width: int = 0,
    height: int = 0,
    resize_mode: str = "contain",
) -> dict:
    """Remove the background of an online image, keeping the main object, as a PNG.

    image_url: any public http/https image URL, including a "url" returned by
        search_images or resize_image (its size settings are reused unless you
        give width/height).
    width, height: output size; 0 and 0 (default) keep the original size (or the
        size the given URL already asked for).
    resize_mode: "contain" (default, keeps the whole object), "crop" or "stretch".

    Works best when there is one clear subject (a person, an animal, a product, a
    cartoon character). Returns {"url", "format": "png"}; the transparent PNG is
    made when the URL is downloaded, so call build_download_command next with
    ext "png" to save it on the user's computer.

    This server cannot read files on the user's computer; for a local file, the
    user must first put it online, or run a background-removal tool locally.
    """
    if not image_url.startswith(("http://", "https://")):
        raise ToolError(
            "image_url must be an http(s) URL; this server cannot read local files"
        )
    src, previous = _unwrap_own_url(image_url)
    if width == 0 and height == 0 and previous:
        width, height = int(previous.get("w", 0)), int(previous.get("h", 0))
    width, height, _, mode = _validate_size_and_format(
        width, height, "png", resize_mode
    )
    return {
        "url": _img_url(_base_url(ctx), src, width, height, mode, "png", nobg=True),
        "format": "png",
    }


@mcp.tool(
    annotations=ToolAnnotations(
        readOnlyHint=True, idempotentHint=True, openWorldHint=False
    )
)
def build_download_command(
    urls: list[str],
    folder: str = "images",
    name: str = "image",
    ext: str = "png",
) -> dict:
    """Final step of saving pictures: build one shell command that downloads the URLs.

    This server cannot write to the user's computer. Run the returned "command"
    with your Bash tool in the user's current project directory, then tell the
    user which "files" were saved. Use an English, filename-friendly "name"
    (e.g. "penguin") and the same "ext" as the format you asked for (always "png"
    for background-removed images). Downloads that remove a background can take
    several seconds each.
    """
    if not urls:
        raise ToolError("urls must not be empty")

    # The command runs on the user's machine, so every input is validated and quoted.
    folder = folder.strip().rstrip("/") or "images"
    if folder.startswith(("/", "~")) or ".." in folder.split("/"):
        raise ToolError(
            "folder must be a relative path inside the project, e.g. 'images'"
        )
    if not re.fullmatch(r"[A-Za-z0-9_\-/]+", folder):
        raise ToolError("folder may only contain letters, digits, '_', '-' and '/'")
    name = re.sub(r"[^A-Za-z0-9_-]", "_", name) or "image"
    try:
        ext = _normalize_format(ext)
    except ValueError as exc:
        raise ToolError("ext must be png or jpg") from exc

    files: list[str] = []
    parts = [f"mkdir -p {shlex.quote(folder)}"]
    for i, url in enumerate(urls, start=1):
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise ToolError(f"Only https (or local) URLs are allowed: {url!r}")
        path = f"{folder}/{name}_{i}.{ext}"
        files.append(path)
        # --max-time: background removal on a cold server (model loading) can take a while
        parts.append(
            f"curl -fsSL --max-time 120 -A {shlex.quote(USER_AGENT)} "
            f"-o {shlex.quote(path)} {shlex.quote(url)}"
        )

    return {"files": files, "command": " && ".join(parts)}


# ---------------------------------------------------------------------------
# 8. HTTP routes (plain HTTP, not MCP tools)
# ---------------------------------------------------------------------------


@mcp.custom_route("/img", methods=["GET"])
async def img_route(request: Request) -> Response:
    """GET /img?src=<image url>&w=200&h=200&mode=crop&format=png[&nobg=1]

    Downloads the source image (SSRF-checked), optionally removes its background,
    resizes it (w=0&h=0 keeps the original size) and returns it.
    """
    q = request.query_params
    src = q.get("src", "")
    nobg = q.get("nobg") == "1"
    try:
        width = int(q.get("w", 200))
        height = int(q.get("h", 200))
        fmt = "png" if nobg else _normalize_format(q.get("format", "png"))
        mode = q.get("mode", "crop")
        if mode not in RESIZE_MODES:
            raise ValueError(f"mode must be one of {', '.join(RESIZE_MODES)}")
    except ValueError as exc:
        return Response(f"Bad request: {exc}", status_code=400)

    try:
        raw = await fetch_image_bytes(src)
    except (httpx.HTTPError, ValueError) as exc:
        return Response(f"Could not download source image: {exc}", status_code=502)

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            img = Image.open(io.BytesIO(raw))
            img = ImageOps.exif_transpose(img)  # honour phone rotation
            img = img.convert("RGBA" if fmt == "png" else "RGB")
    except (
        UnidentifiedImageError,
        OSError,
        Image.DecompressionBombWarning,
        Image.DecompressionBombError,
    ):
        return Response(
            "Source is not a readable (or safely sized) image", status_code=502
        )

    if nobg:
        try:
            img = await remove_background(
                img
            )  # cut out first, at full detail, then resize
        except (
            httpx.HTTPError,
            RuntimeError,
            OSError,
            ort.capi.onnxruntime_pybind11_state.Fail,
        ) as exc:
            return Response(f"Background removal failed: {exc}", status_code=500)

    if width > 0 and height > 0:
        img = resize(
            img,
            _clamp(width, MIN_SIZE, MAX_SIZE),
            _clamp(height, MIN_SIZE, MAX_SIZE),
            mode,
        )
    else:  # keep the original size, but never larger than MAX_SIZE
        img = (
            ImageOps.contain(img, (MAX_SIZE, MAX_SIZE), Image.LANCZOS)
            if max(img.size) > MAX_SIZE
            else img
        )

    body, media_type = encode_image(img, fmt)
    if len(body) > MAX_RESPONSE_BYTES:
        return Response(
            "Result is larger than 4.4 MB; ask for a smaller width/height",
            status_code=413,
        )
    return Response(
        body, media_type=media_type, headers={"Cache-Control": "public, max-age=86400"}
    )


@mcp.custom_route("/", methods=["GET"])
async def health_route(request: Request) -> Response:
    """Opening the bare domain in a browser shows this instead of a 404."""
    return JSONResponse(
        {
            "name": "ding-mcp",
            "mcp_endpoint": "/mcp",
            "image_source": "duckduckgo",
            "background_model_loaded": _session is not None,
        }
    )


# ---------------------------------------------------------------------------
# 9. ASGI app
# ---------------------------------------------------------------------------

# - stateless_http=True: every request stands alone (required on serverless).
# - json_response=True: plain JSON replies instead of an SSE stream.
# - DNS-rebinding protection only allows localhost by default; turn it off so
#   Vercel's *.vercel.app Host header is accepted.
_starlette_app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class EnsureStarted:
    """ASGI wrapper that makes the MCP session manager work with or without lifespan.

    The session manager must be started (`session_manager.run()`) before /mcp can
    answer; normally the ASGI "lifespan" startup event does that. Not every host
    sends lifespan events (some serverless runtimes skip them), and then every
    /mcp request fails with "Task group is not initialized". This wrapper notices
    when startup has not happened and starts the session manager itself on the
    first HTTP request.
    """

    def __init__(self, inner):
        self.inner = inner
        self._started = asyncio.Event()
        self._lock = asyncio.Lock()
        self._runner: asyncio.Task | None = None

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":

            async def watch_send(message):
                if message["type"] == "lifespan.startup.complete":
                    self._started.set()
                await send(message)

            await self.inner(scope, receive, watch_send)
            return
        if scope["type"] == "http" and not self._started.is_set():
            await self._start_without_lifespan()
        await self.inner(scope, receive, send)

    async def _start_without_lifespan(self) -> None:
        async with self._lock:
            if self._started.is_set():
                return

            async def keep_running():
                async with mcp.session_manager.run():
                    self._started.set()
                    await (
                        asyncio.Event().wait()
                    )  # keep the manager open for the process lifetime

            self._runner = asyncio.create_task(keep_running())
            ready = asyncio.create_task(self._started.wait())
            await asyncio.wait(
                {self._runner, ready}, return_when=asyncio.FIRST_COMPLETED
            )
            if not self._started.is_set():  # the runner died before it was ready
                ready.cancel()
                self._runner.result()  # re-raise its exception


# `app` is what uvicorn (locally) and Vercel (api/index.py) serve.
app = EnsureStarted(_starlette_app)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
