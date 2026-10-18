# ding-mcp

A remote [MCP](https://modelcontextprotocol.io) server in Python that lets Claude find,
resize and cut out images — running on Vercel, saving files on your own machine.

## Tools

| Tool | What it does |
|---|---|
| `search_images` | Search the web (DuckDuckGo) by keyword; filter photo / illustration; optional background removal |
| `resize_image` | Resize any online image (`crop` / `contain` / `stretch`) |
| `remove_background_as_png` | Cut out the main object into a transparent PNG (U²-Net *silueta* model via onnxruntime) |
| `build_download_command` | Turn result URLs into a `curl` command that Claude Code runs locally |
| `add`, `current_time` | Simple demo tools |

## How it works

```
Claude Code ──MCP (Streamable HTTP)──► server (local or Vercel)
            ◄── URLs: https://<server>/img?src=...&w=200&h=200[&nobg=1]

Claude Code runs curl on your machine ──► GET /img
            server: download (SSRF-checked) → remove background → resize → PNG/JPG
            ◄── image bytes, saved into ./images/
```

The server never writes results to its own disk, so it works unchanged on serverless.

## Run locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py                  # http://127.0.0.1:8000/mcp
python test_client.py             # end-to-end checks
```

## Deploy to Vercel

Import the GitHub repo at <https://vercel.com/new> (Framework Preset: **Other**), then:

```bash
python test_client.py https://<project>.vercel.app/mcp
```

## Use from Claude Code

`.mcp.json` in your project folder:

```json
{
  "mcpServers": {
    "ding-mcp": { "type": "http", "url": "https://<project>.vercel.app/mcp" }
  }
}
```

Then ask: *"Give me 2 cartoon cats wearing clothes, background removed, saved in the images folder."*

## Notes

- DuckDuckGo search is unofficial (via `ddgs`) and may be rate-limited; images have unknown copyright.
- The background model (44 MB) is downloaded on first use and verified by SHA-256.
- The server has no authentication: anyone with the URL can use it.
