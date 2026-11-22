# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp>=1.9,<2", "pillow>=10"]
# ///
"""Offline end-to-end tests for server.py.

A real MCP client talks to the server over stdio. A fake `codex` binary stands in for
ChatGPT and a local mock stands in for the OpenAI API, so no account or quota is used.

    uv run --script tests/test_server.py
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import stat
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
SERVER = HERE.parent / "server.py"
FAKE_CODEX = HERE / "fake_codex.py"

passed = 0


def check(condition: bool, label: str) -> None:
    global passed
    if not condition:
        raise AssertionError(label)
    passed += 1
    print(f"  ok  {label}")


def text_of(result) -> str:
    return "\n".join(c.text for c in result.content if c.type == "text")


def previews(result) -> list[str]:
    return [c.mimeType for c in result.content if c.type == "image"]


def saved_paths(result) -> list[Path]:
    return [Path(line[2:].rsplit(" (", 1)[0]) for line in text_of(result).splitlines() if line.startswith("- /")]


async def call(env: dict, name: str, args: dict):
    params = StdioServerParameters(command="uv", args=["run", "--script", str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            return await s.call_tool(name, args)


async def list_tools(env: dict):
    params = StdioServerParameters(command="uv", args=["run", "--script", str(SERVER)], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as s:
            await s.initialize()
            return (await s.list_tools()).tools


class MockOpenAI(BaseHTTPRequestHandler):
    requests: list[str] = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        MockOpenAI.requests.append(self.path)
        if self.headers.get("Authorization") != "Bearer sk-test":
            body, code = {"error": {"message": "Incorrect API key provided"}}, 401
        else:
            buf = io.BytesIO()
            Image.new("RGB", (1024, 1024), (10, 10, 200)).save(buf, "PNG")
            body, code = {"data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]}, 200
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def make_fixtures(folder: Path) -> None:
    Image.new("RGB", (1536, 1024), (240, 240, 240)).save(folder / "plain.png")
    for name, bg in (("green", (0, 255, 0)), ("magenta", (255, 0, 255))):
        im = Image.new("RGB", (1024, 1024), bg)
        draw = ImageDraw.Draw(im)
        # A thin ring of outline/screen blend, like the anti-aliasing a real image has.
        blend = tuple((c + o) // 2 for c, o in zip(bg, (0, 0, 0)))
        draw.ellipse((296, 296, 728, 728), outline=blend, width=4)
        draw.ellipse((300, 300, 724, 724), fill=(85, 107, 47), outline=(0, 0, 0), width=12)
        im.save(folder / f"{name}.png")


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="image-mcp-test-"))
    fixtures = tmp / "fixtures"
    fixtures.mkdir()
    make_fixtures(fixtures)
    FAKE_CODEX.chmod(FAKE_CODEX.stat().st_mode | stat.S_IEXEC)
    log = tmp / "codex-calls.jsonl"
    env = {
        "PATH": os.environ["PATH"],
        "HOME": os.environ["HOME"],
        "CODEX_PATH": str(FAKE_CODEX),
        "CODEX_HOME": str(tmp / "codex-home"),
        "IMAGE_OUTPUT_DIR": str(tmp / "out"),
        "FAKE_LOG": str(log),
        "FAKE_IMG_DIR": str(fixtures),
    }

    def last_codex_call() -> dict:
        return json.loads(log.read_text().splitlines()[-1])

    print("Tool annotations")
    tools = {t.name: t for t in await list_tools(env)}
    check(set(tools) == {"generate_image", "edit_image"}, "both tools listed")
    for name, tool in tools.items():
        a = tool.annotations
        check(
            a is not None and a.title
            and (a.readOnlyHint, a.destructiveHint, a.idempotentHint, a.openWorldHint) == (False, False, False, True),
            f"{name} declares all four hints",
        )

    print("ChatGPT backend (fake codex)")
    r = await call(env, "generate_image", {"prompt": "A shark logo", "size": "1536x1024", "quality": "high"})
    check(not r.isError, "generate_image succeeds")
    check(len(saved_paths(r)) == 1 and saved_paths(r)[0].is_file(), "image saved to disk")
    check(previews(r) == ["image/jpeg"], "one JPEG preview returned")
    c = last_codex_call()
    check(["--sandbox", "read-only"] == c["args"][c["args"].index("--sandbox"):][:2], "codex runs read-only")
    check("--ephemeral" in c["args"], "codex run is ephemeral")
    check("1536x1024 pixels (landscape)" in c["prompt"] and "high detail" in c["prompt"], "size/quality hints sent")

    r = await call(env, "generate_image", {"prompt": "red sticker", "background": "transparent", "n": 2, "filename": "sticker"})
    paths = saved_paths(r)
    check(len(paths) == 2 and all(p.name.startswith("sticker-") for p in paths), "n=2 saves two named files")
    check("#00FF00" in last_codex_call()["prompt"], "green screen for ordinary subjects")
    with Image.open(paths[0]) as im:
        check(im.mode == "RGBA" and im.getpixel((5, 5))[3] == 0 and im.getpixel((512, 512))[3] == 255,
              "green screen removed, subject kept")
    check(previews(r) == ["image/webp", "image/webp"], "transparent previews are WebP")

    r = await call(env, "generate_image", {"prompt": "army style camo logo", "background": "transparent"})
    check("#FF00FF" in last_codex_call()["prompt"], "magenta screen for green subjects")
    with Image.open(saved_paths(r)[0]) as im:
        olive = im.getpixel((512, 512))
        check(im.getpixel((5, 5))[3] == 0 and olive[3] == 255 and olive[1] > olive[0], "magenta removed, olive kept")
        edge = [im.getpixel((x, 512)) for x in range(280, 312)]
        check(not any(p[3] > 0 and min(p[0], p[2]) > p[1] + 30 for p in edge), "no magenta fringe on the outline")

    r = await call(env, "edit_image", {"prompt": "combine them", "images": [str(fixtures / "plain.png"), str(fixtures / "green.png")],
                                       "output_format": "webp", "output_dir": str(tmp / "assets")})
    check(not r.isError and saved_paths(r)[0].suffix == ".webp" and saved_paths(r)[0].parent.name == "assets",
          "edit_image saves webp into output_dir")
    check(last_codex_call()["args"].count("-i") == 2, "both input images attached")

    r = await call(env, "edit_image", {"prompt": "x", "images": [str(fixtures / "plain.png")], "mask": str(fixtures / "plain.png")})
    check(r.isError and "Masks need the API backend" in text_of(r), "mask is rejected with a clear message")
    r = await call(env, "edit_image", {"prompt": "x", "images": [str(tmp / "missing.png")]})
    check(r.isError and "not found" in text_of(r), "missing input file is reported")
    r = await call({**env, "FAKE_FAIL": "1"}, "generate_image", {"prompt": "x"})
    check(r.isError and "signed in" in text_of(r), "login failure explains how to sign in")
    r = await call({**env, "CODEX_PATH": str(tmp / "nope")}, "generate_image", {"prompt": "x"})
    check(r.isError and "CODEX_PATH" in text_of(r), "missing codex binary is reported")

    print("API backend (mock OpenAI)")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), MockOpenAI)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    api_env = {**env, "IMAGE_BACKEND": "api", "OPENAI_API_KEY": "sk-test",
               "OPENAI_BASE_URL": f"http://127.0.0.1:{httpd.server_address[1]}/v1"}
    r = await call(api_env, "generate_image", {"prompt": "api"})
    check(not r.isError and MockOpenAI.requests[-1] == "/v1/images/generations", "API generate works")
    r = await call(api_env, "edit_image", {"prompt": "api", "images": [str(fixtures / "plain.png")], "mask": str(fixtures / "plain.png")})
    check(not r.isError and MockOpenAI.requests[-1] == "/v1/images/edits", "API edit with mask works")
    r = await call({**api_env, "OPENAI_API_KEY": "sk-wrong"}, "generate_image", {"prompt": "x"})
    check(r.isError and "401" in text_of(r), "bad API key is reported")
    r = await call({**api_env, "OPENAI_API_KEY": ""}, "generate_image", {"prompt": "x"})
    check(r.isError and "needs an OpenAI API key" in text_of(r), "missing API key is reported")
    httpd.shutdown()

    print(f"\nAll {passed} checks passed.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as failure:
        print(f"  FAIL  {failure}")
        sys.exit(1)
