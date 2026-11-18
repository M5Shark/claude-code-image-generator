#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "mcp>=1.9,<2",
#   "httpx>=0.27",
#   "pillow>=10",
#   "numpy>=1.26",
# ]
# ///
"""ChatGPT Images MCP server.

Gives Claude two tools, generate_image and edit_image, backed by ChatGPT's image model.

Backends (IMAGE_BACKEND):
  chatgpt  (default) Runs the Codex CLI that ships inside the ChatGPT desktop app, signed in
           with your ChatGPT account. No API key; images count against your ChatGPT plan.
  api      Calls the OpenAI Images API with OPENAI_API_KEY. Pay per image, exact control
           over size, quality and transparency.

Run:   uv run --script server.py        (stdio transport)
Settings: env vars, or a .env file next to this script (see .env.example).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import mimetypes
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Annotated, Literal

import httpx
from mcp.server.fastmcp import FastMCP, Image
from PIL import Image as PILImage
from pydantic import Field

HERE = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader; real environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(HERE / ".env")


def _env_path(name: str, default: str) -> Path:
    return Path(os.path.expanduser(os.environ.get(name, default)))


BACKEND = os.environ.get("IMAGE_BACKEND", "chatgpt").strip().lower()
OUTPUT_DIR = _env_path("IMAGE_OUTPUT_DIR", "~/Pictures/ChatGPT Images")
PREVIEW_MAX = int(os.environ.get("IMAGE_PREVIEW_MAX_PX", "768"))

# chatgpt backend (Codex CLI)
CODEX_HOME = _env_path("CODEX_HOME", "~/.codex")
CODEX_MODEL = os.environ.get("CODEX_MODEL", "").strip()
CODEX_EFFORT = os.environ.get("CODEX_REASONING_EFFORT", "low").strip()
CODEX_TIMEOUT = float(os.environ.get("CODEX_TIMEOUT", "600"))
CODEX_CANDIDATES = [
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
    "/Applications/Codex.app/Contents/Resources/codex",
    "~/.local/bin/codex",
    "/opt/homebrew/bin/codex",
    "/usr/local/bin/codex",
]

# api backend (OpenAI Images API)
API_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
API_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
API_TIMEOUT = float(os.environ.get("OPENAI_IMAGE_TIMEOUT", "300"))

MAX_INPUT_BYTES = 50 * 1024 * 1024
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}

mcp = FastMCP(
    "chatgpt-images",
    instructions=(
        "Generate and edit images with ChatGPT's image model (GPT Image). "
        "Use generate_image for new images and edit_image to change or combine existing image files. "
        "Each call takes roughly 20-60 seconds. Images are saved to disk; the result lists the saved "
        "paths and shows a small preview. Write detailed prompts: subject, style, composition, "
        "lighting, colors, and any exact text to render. When the user is working in a project and "
        "wants the image there (e.g. a website asset), pass an absolute output_dir inside the project."
    ),
)

# ---------------------------------------------------------------- shared helpers


def _slug(text: str, limit: int = 48) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].rstrip("-") or "image"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 10_000):
        candidate = path.with_name(f"{path.stem}-{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find a free file name for {path}")


def _check_input(path_str: str, label: str) -> Path:
    path = Path(os.path.expanduser(path_str)).resolve()
    if not path.is_file():
        raise RuntimeError(f"{label} not found: {path}")
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise RuntimeError(f"{label} is larger than 50 MB: {path}")
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime not in ("image/png", "image/jpeg", "image/webp"):
        raise RuntimeError(f"{label} must be PNG, JPEG or WebP: {path}")
    return path


def _encode(im: PILImage.Image, output_format: str) -> tuple[bytes, str]:
    buf = io.BytesIO()
    if output_format == "jpeg":
        im.convert("RGB").save(buf, format="JPEG", quality=92)
        return buf.getvalue(), "jpg"
    if output_format == "webp":
        im.save(buf, format="WEBP", quality=92)
        return buf.getvalue(), "webp"
    im.save(buf, format="PNG")
    return buf.getvalue(), "png"


def _preview(im: PILImage.Image) -> Image | None:
    if PREVIEW_MAX <= 0:
        return None
    has_alpha = im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info)
    small = im.convert("RGBA" if has_alpha else "RGB")
    small.thumbnail((PREVIEW_MAX, PREVIEW_MAX))
    buf = io.BytesIO()
    if has_alpha:  # WebP keeps transparency at a fraction of PNG's size
        small.save(buf, format="WEBP", quality=85)
        return Image(data=buf.getvalue(), format="webp")
    small.save(buf, format="JPEG", quality=85)
    return Image(data=buf.getvalue(), format="jpeg")


# Chroma-key colors for transparent backgrounds on the ChatGPT backend. Green is the default;
# magenta is used when the subject itself is likely to be green (army, camo, forest...).
KEYS = {
    "green": ((0, 255, 0), "pure green (#00FF00)", "green"),
    "magenta": ((255, 0, 255), "pure magenta (#FF00FF)", "magenta, pink or purple"),
}
GREEN_SUBJECT = re.compile(
    r"\b(green\w*|olive|army|military|milsim|camo\w*|forest|jungle|leaf|leaves|grass\w*|"
    r"emerald|lime|mint|sage|moss\w*|tactical|soldier\w*|frog\w*|plant\w*|tree\w*)\b",
    re.IGNORECASE,
)


def _key_for(prompt: str) -> str:
    return "magenta" if GREEN_SUBJECT.search(prompt) else "green"


def _remove_key(im: PILImage.Image, key_name: str) -> tuple[PILImage.Image, bool]:
    """Turn a flat chroma-key background into transparency. Returns (image, removed?)."""
    import numpy as np

    target = np.array(KEYS[key_name][0], dtype=np.float32)
    rgb = np.asarray(im.convert("RGB")).astype(np.float32)
    border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
    key = np.median(border, axis=0)
    if np.linalg.norm(key - target) > 110:
        return im, False  # background isn't the key color; leave the image alone
    dist = np.sqrt(((rgb - key) ** 2).sum(axis=2))
    alpha = np.clip((dist - 60.0) / 90.0, 0.0, 1.0)
    # De-spill: anti-aliasing blends the key color into outlines, leaving opaque tinted pixels
    # next to the cut. Neutralise the key tint on and within 6px of the background.
    edge = alpha < 0.999
    for _ in range(6):
        grown = edge.copy()
        grown[1:] |= edge[:-1]
        grown[:-1] |= edge[1:]
        grown[:, 1:] |= edge[:, :-1]
        grown[:, :-1] |= edge[:, 1:]
        edge = grown
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if key_name == "green":
        rgb[..., 1] = np.where(edge, np.minimum(g, np.maximum(r, b)), g)
    else:  # magenta: red and blue rise together above green
        excess = np.where(edge, np.maximum(np.minimum(r, b) - g, 0.0), 0.0)
        rgb[..., 0] = r - excess
        rgb[..., 2] = b - excess
    out = np.dstack([rgb, alpha * 255.0]).round().clip(0, 255).astype(np.uint8)
    return PILImage.fromarray(out), True  # HxWx4 uint8 -> RGBA


def _finish(
    images: list[bytes],
    *,
    prompt: str,
    source: str,
    output_dir: str | None,
    filename: str | None,
    output_format: str,
    transparent_key: str | None,
    notes: list[str],
) -> list:
    directory = Path(os.path.expanduser(output_dir)).resolve() if output_dir else OUTPUT_DIR
    directory.mkdir(parents=True, exist_ok=True)
    stem = _slug(Path(filename).stem) if filename else f"{time.strftime('%Y%m%d-%H%M%S')}-{_slug(prompt)}"

    lines: list[str] = []
    previews: list[Image] = []
    key_failed = False
    for i, data in enumerate(images, start=1):
        with PILImage.open(io.BytesIO(data)) as opened:
            opened.load()
            im = opened.copy()
        if transparent_key:
            im, removed = _remove_key(im, transparent_key)
            key_failed |= not removed
            if removed and output_format == "jpeg":
                output_format = "png"  # JPEG can't hold transparency
        encoded, ext = _encode(im, output_format)
        suffix = f"-{i}" if len(images) > 1 else ""
        path = _unique(directory / f"{stem}{suffix}.{ext}")
        path.write_bytes(encoded)
        lines.append(f"- {path} ({im.width}x{im.height})")
        preview = _preview(im)
        if preview:
            previews.append(preview)

    head = f"Created {len(images)} image(s) with {source}. Saved to:"
    tail = list(notes)
    if key_failed:
        tail.append(
            f"Note: the background did not come back as a flat {transparent_key} screen, so it was left as is."
        )
    if previews:
        tail.append(f"(Previews are downscaled to {PREVIEW_MAX}px; the saved files are full size.)")
    return ["\n".join([head, *lines, *tail]), *previews]


def _hints(size: str, quality: str, background: str, key: str | None = None) -> list[str]:
    hints = []
    if size and size != "auto":
        m = re.fullmatch(r"\s*(\d+)\s*[xX×]\s*(\d+)\s*", size)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            shape = "square" if w == h else ("landscape" if w > h else "portrait")
            hints.append(f"Size: {w}x{h} pixels ({shape}).")
        else:
            hints.append(f"Size / aspect ratio: {size}.")
    if quality in ("high", "medium"):
        hints.append(f"Quality: {quality} detail.")
    elif quality == "low":
        hints.append("Quality: a quick draft is fine.")
    if background == "transparent":
        _, color, avoid = KEYS[key or "green"]
        hints.append(
            f"Background: a perfectly flat, uniform, {color} chroma-key background, with no "
            f"shadows, gradients, floor or scenery, and no {avoid} in the subject itself. "
            "(It will be removed afterwards to make the background transparent.)"
        )
    elif background == "opaque":
        hints.append("Background: a solid, filled background (not transparent).")
    return hints


# ---------------------------------------------------------------- chatgpt backend (Codex CLI)


def _codex_path() -> str:
    configured = os.environ.get("CODEX_PATH", "").strip()
    if configured:
        path = Path(os.path.expanduser(configured))
        if path.is_file():
            return str(path)
        raise RuntimeError(f"CODEX_PATH points to a missing file: {path}")
    found = shutil.which("codex")
    if found:
        return found
    for candidate in CODEX_CANDIDATES:
        path = Path(os.path.expanduser(candidate))
        if path.is_file():
            return str(path)
    raise RuntimeError(
        "Could not find the Codex CLI. Install the ChatGPT desktop app (it includes Codex) "
        "or set CODEX_PATH in .env."
    )


def _codex_images(thread_id: str | None, started: float) -> list[Path]:
    root = CODEX_HOME / "generated_images"
    if not root.is_dir():
        return []
    if thread_id:  # normal case: Codex files images under its thread id
        folder = root / thread_id
        files = [p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS] if folder.is_dir() else []
    else:  # fallback if Codex ever stops reporting the thread id: anything written during this run
        files = [
            p for p in root.rglob("*") if p.suffix.lower() in IMAGE_EXTS and p.stat().st_mtime >= started
        ]
    return sorted(files, key=lambda p: p.stat().st_mtime)


async def _run_codex(instructions: str, attachments: list[Path]) -> tuple[list[bytes], str]:
    codex = _codex_path()
    work = Path(tempfile.mkdtemp(prefix="chatgpt-images-"))
    args = [
        codex, "exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
        "--sandbox", "read-only", "-C", str(work),
        "-c", f'model_reasoning_effort="{CODEX_EFFORT}"',
    ]  # fmt: skip
    if CODEX_MODEL:
        args += ["-m", CODEX_MODEL]
    for attachment in attachments:
        args += ["-i", str(attachment)]
    # The prompt goes in on stdin: `-i` takes several values and would swallow a positional prompt.

    started = time.time()
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=work,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(instructions.encode()), timeout=CODEX_TIMEOUT
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"ChatGPT (Codex) did not finish within {CODEX_TIMEOUT:.0f} seconds.")
    finally:
        shutil.rmtree(work, ignore_errors=True)

    thread_id: str | None = None
    messages: list[str] = []
    errors: list[str] = []
    for line in out.decode(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        kind = event.get("type")
        if kind == "thread.started":
            thread_id = event.get("thread_id")
        elif kind == "item.completed":
            item = event.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                messages.append(item["text"].strip())
            elif item.get("type") == "error" and item.get("message"):
                errors.append(item["message"])
        elif kind in ("error", "turn.failed"):
            detail = event.get("error", event)
            errors.append(detail.get("message", str(detail)) if isinstance(detail, dict) else str(detail))

    files = _codex_images(thread_id, started)
    if not files:
        stderr_tail = err.decode(errors="replace").replace("Reading prompt from stdin...", "").strip()
        detail = errors or messages[-1:] or [stderr_tail[-600:]] or ["no output"]
        text = " | ".join(d for d in detail if d)
        hint = ""
        if proc.returncode or re.search(r"log ?in|auth|401|unauthori", text, re.I):
            hint = (
                f" Make sure Codex is signed in with your ChatGPT account: open the ChatGPT app "
                f"and sign in to Codex, or run `{codex} login`."
            )
        raise RuntimeError(f"ChatGPT did not return an image. {text.rstrip('.')}.{hint}")

    last = messages[-1] if messages else ""
    return [p.read_bytes() for p in files], ("" if last.upper().strip(". ") == "DONE" else last)


RELAY_RULES = (
    "You are an image-generation relay for another assistant. Use your built-in image generation "
    "tool to create the image(s) described below. Do not run shell commands, do not read or write "
    "files, and do not ask questions. Keep the request's meaning; you may add visual detail only "
    "where it is missing. When finished, reply with only the word DONE. If you cannot make the "
    "image, reply with one sentence explaining why."
)


async def _chatgpt_generate(prompt, size, quality, n, background, key) -> tuple[list[bytes], list[str]]:
    count = "exactly one image" if n == 1 else f"exactly {n} separate images (one tool call each)"
    parts = [RELAY_RULES, f"Generate {count}.", "", "Image request:", prompt.strip()]
    parts += _hints(size, quality, background, key)
    images, message = await _run_codex("\n".join(parts), [])
    return images, [f"ChatGPT said: {message}"] if message else []


async def _chatgpt_edit(prompt, inputs, size, quality, n, background, key) -> tuple[list[bytes], list[str]]:
    count = "exactly one image" if n == 1 else f"exactly {n} separate images (one tool call each)"
    which = "the attached image" if len(inputs) == 1 else f"the {len(inputs)} attached images"
    parts = [
        RELAY_RULES,
        f"Use {which} as the input/reference and generate {count}.",
        "Keep everything from the input that the request does not ask to change.",
        "",
        "Edit request:",
        prompt.strip(),
    ]
    parts += _hints(size, quality, background, key)
    images, message = await _run_codex("\n".join(parts), inputs)
    return images, [f"ChatGPT said: {message}"] if message else []


# ---------------------------------------------------------------- api backend (OpenAI Images API)


def _api_headers() -> dict[str, str]:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "IMAGE_BACKEND=api needs an OpenAI API key. Add OPENAI_API_KEY=sk-... to "
            f"{HERE / '.env'}, or remove IMAGE_BACKEND=api to use your ChatGPT login instead."
        )
    headers = {"Authorization": f"Bearer {key}"}
    org = os.environ.get("OPENAI_ORG_ID", "").strip()
    if org:
        headers["OpenAI-Organization"] = org
    return headers


def _api_check(resp: httpx.Response) -> dict:
    if resp.is_success:
        return resp.json()
    try:
        message = resp.json().get("error", {}).get("message") or resp.text
    except ValueError:
        message = resp.text
    hint = ""
    if resp.status_code == 401:
        hint = " (check that OPENAI_API_KEY is correct)"
    elif resp.status_code == 403 and "verif" in message.lower():
        hint = " (verify your organization at platform.openai.com/settings/organization/general)"
    elif resp.status_code == 429:
        hint = " (rate limit, or no credit left on the OpenAI account)"
    raise RuntimeError(f"OpenAI API error {resp.status_code}: {message}{hint}")


def _api_options(**values) -> dict[str, str]:
    """Only send options that were set, so models that lack one still work."""
    return {k: str(v) for k, v in values.items() if v is not None and v != "auto"}


async def _api_collect(client: httpx.AsyncClient, payload: dict) -> tuple[list[bytes], list[str]]:
    images: list[bytes] = []
    for item in payload.get("data") or []:
        if item.get("b64_json"):
            images.append(base64.b64decode(item["b64_json"]))
        elif item.get("url"):
            resp = await client.get(item["url"])
            resp.raise_for_status()
            images.append(resp.content)
    if not images:
        raise RuntimeError("OpenAI returned no images.")
    notes = []
    revised = next((i["revised_prompt"] for i in payload["data"] if i.get("revised_prompt")), None)
    if revised:
        notes.append(f"Revised prompt: {revised}")
    usage = payload.get("usage")
    if isinstance(usage, dict) and usage.get("total_tokens"):
        notes.append(
            f"Usage: {usage.get('input_tokens', '?')} input / {usage.get('output_tokens', '?')} output tokens"
        )
    return images, notes


async def _api_generate(prompt, size, quality, n, background, output_format, model):
    body: dict = {"model": model or API_MODEL, "prompt": prompt}
    if n > 1:
        body["n"] = n
    body |= _api_options(size=size, quality=quality, background=background)
    if output_format != "png":
        body["output_format"] = output_format
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        resp = await client.post(f"{API_BASE_URL}/images/generations", headers=_api_headers(), json=body)
        return await _api_collect(client, _api_check(resp))


async def _api_edit(prompt, inputs, mask, size, quality, n, background, output_format, input_fidelity, model):
    field = "image[]" if len(inputs) > 1 else "image"
    files = []
    for path in inputs:
        files.append((field, (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0])))
    if mask:
        files.append(("mask", (mask.name, mask.read_bytes(), mimetypes.guess_type(mask.name)[0])))
    form: dict[str, str] = {"model": model or API_MODEL, "prompt": prompt}
    if n > 1:
        form["n"] = str(n)
    form |= _api_options(size=size, quality=quality, background=background, input_fidelity=input_fidelity)
    if output_format != "png":
        form["output_format"] = output_format
    async with httpx.AsyncClient(timeout=API_TIMEOUT) as client:
        resp = await client.post(f"{API_BASE_URL}/images/edits", headers=_api_headers(), data=form, files=files)
        return await _api_collect(client, _api_check(resp))


# ---------------------------------------------------------------- tool parameters

Size = Annotated[
    str,
    Field(
        description=(
            "Image size WIDTHxHEIGHT, or 'auto'. Common: 1024x1024 (square), 1536x1024 (landscape), "
            "1024x1536 (portrait). With the ChatGPT backend this sets the shape; exact pixels may differ."
        )
    ),
]
Quality = Annotated[
    Literal["auto", "low", "medium", "high"],
    Field(description="Render quality. 'low' is fastest, 'high' is most detailed."),
]
Count = Annotated[int, Field(ge=1, le=4, description="How many images to make (1-4).")]
Background = Annotated[
    Literal["auto", "transparent", "opaque"],
    Field(description="'transparent' removes the background (logos, stickers, icons, product cut-outs)."),
]
OutputFormat = Annotated[Literal["png", "jpeg", "webp"], Field(description="File format of the saved image.")]
OutputDir = Annotated[
    str | None,
    Field(description="Absolute folder to save into. Defaults to the server's IMAGE_OUTPUT_DIR."),
]
FileName = Annotated[
    str | None,
    Field(description="Base file name without extension. Defaults to a timestamp plus the prompt."),
]
Model = Annotated[
    str | None,
    Field(description="OpenAI image model id (API backend only). Leave empty for the default."),
]


def _source(model: str | None) -> str:
    return "ChatGPT (your ChatGPT plan, via Codex)" if BACKEND != "api" else f"the OpenAI API ({model or API_MODEL})"


# ---------------------------------------------------------------- tools


@mcp.tool()
async def generate_image(
    prompt: Annotated[str, Field(description="Detailed description of the image to create.")],
    size: Size = "auto",
    quality: Quality = "auto",
    n: Count = 1,
    background: Background = "auto",
    output_format: OutputFormat = "png",
    output_dir: OutputDir = None,
    filename: FileName = None,
    model: Model = None,
) -> list:
    """Create new images from a text prompt with ChatGPT's image generator.

    Saves the images to disk and returns their paths plus a preview.
    """
    if BACKEND == "api":
        images, notes = await _api_generate(prompt, size, quality, n, background, output_format, model)
        key = None
    else:
        key = _key_for(prompt) if background == "transparent" else None
        images, notes = await _chatgpt_generate(prompt, size, quality, n, background, key)
    return _finish(
        images, prompt=prompt, source=_source(model), output_dir=output_dir, filename=filename,
        output_format=output_format, transparent_key=key, notes=notes,
    )  # fmt: skip


@mcp.tool()
async def edit_image(
    prompt: Annotated[
        str, Field(description="What to change, or how to combine the input images into a new one.")
    ],
    images: Annotated[
        list[str],
        Field(min_length=1, max_length=16, description="Absolute paths of 1-16 input images (PNG, JPEG or WebP)."),
    ],
    mask: Annotated[
        str | None,
        Field(
            description=(
                "API backend only: absolute path to a PNG mask, same size as the first image; "
                "transparent pixels mark the area to repaint. With the ChatGPT backend, describe "
                "the area in the prompt instead."
            )
        ),
    ] = None,
    size: Size = "auto",
    quality: Quality = "auto",
    n: Count = 1,
    background: Background = "auto",
    output_format: OutputFormat = "png",
    input_fidelity: Annotated[
        Literal["auto", "low", "high"],
        Field(description="API backend only: 'high' keeps faces, logos and fine details closer to the input."),
    ] = "auto",
    output_dir: OutputDir = None,
    filename: FileName = None,
    model: Model = None,
) -> list:
    """Edit existing images, or combine several reference images, with ChatGPT's image generator.

    Examples: change the background of a product photo, restyle a picture, put a logo on a mockup,
    combine items from several photos into one scene. Saves results to disk and returns their paths
    plus a preview.
    """
    inputs = [_check_input(p, f"Input image {i}") for i, p in enumerate(images, start=1)]
    if BACKEND == "api":
        mask_path = _check_input(mask, "Mask") if mask else None
        result, notes = await _api_edit(
            prompt, inputs, mask_path, size, quality, n, background, output_format, input_fidelity, model
        )
        key = None
    else:
        if mask:
            raise RuntimeError(
                "Masks need the API backend. With your ChatGPT login, describe the area to change in "
                "the prompt instead (e.g. 'replace only the sky')."
            )
        key = _key_for(prompt) if background == "transparent" else None
        result, notes = await _chatgpt_edit(prompt, inputs, size, quality, n, background, key)
    return _finish(
        result, prompt=prompt, source=_source(model), output_dir=output_dir, filename=filename,
        output_format=output_format, transparent_key=key, notes=notes,
    )  # fmt: skip


if __name__ == "__main__":
    mcp.run()
