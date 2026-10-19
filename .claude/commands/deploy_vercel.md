---
description: Deploy this local project to Vercel (production) and print the live URL
argument-hint: "[preview]  (default: production)"
allowed-tools: Bash(vercel:*), Bash(npx vercel:*), Bash(npm:*), Bash(node:*), Bash(git status:*), Bash(curl:*), Bash(.venv/bin/python test_client.py:*), Read
---

Deploy the project in the current directory to Vercel and give me the URL where I can see it on the internet.

Arguments: `$ARGUMENTS` — if it is `preview`, make a preview deployment; otherwise deploy to production.

## Steps

1. **Check the Vercel CLI.**
   - Run `vercel --version`. If it is missing, check `node --version`.
   - If Node.js is missing too, stop and tell me to install it (e.g. `sudo apt install nodejs npm`, or nvm), then run `npm i -g vercel`. Do not try to install Node yourself.
   - If Node exists but Vercel does not, install it with `npm i -g vercel` (fall back to `npx vercel` for the commands below if the global install fails).

2. **Check login.** Run `vercel whoami`. If not logged in, stop and tell me to run `! vercel login` in the prompt (it is interactive), then re-run `/deploy_vercel`.

3. **Pre-flight.**
   - Run `git status --short` and mention any uncommitted changes (they *will* be deployed, since this uploads the local folder, not GitHub).
   - Make sure `.env` files and `.venv/` are not uploaded: they are ignored via `.gitignore` / `vercel.json` `excludeFiles`. If a `.vercelignore` is needed, suggest it rather than creating secrets-related files silently.

4. **Deploy.**
   - Production (default): `vercel deploy --prod --yes`
   - Preview: `vercel deploy --yes`
   - The first run links the folder to a Vercel project (creates `.vercel/`). If it asks which project, prefer an existing project named after this folder.
   - Capture the deployment URL printed on stdout. If the deploy fails, show the relevant error lines and run `vercel inspect <url> --logs` if a URL was produced.

5. **Verify it is live.**
   - `curl -s -o /dev/null -w "%{http_code}" --max-time 30 <url>` and report the status code.
   - For production, also find the stable alias (e.g. `https://<project>.vercel.app`) from the deploy output or `vercel inspect <url>`.
   - If `test_client.py` exists and `.venv` is present, run `.venv/bin/python test_client.py <alias>/mcp --offline` and summarize pass/fail.

6. **Report** in a short summary:
   - 🌐 **Live URL** (the stable `*.vercel.app` alias for production, or the preview URL)
   - The unique deployment URL
   - HTTP status / test result
   - If this is an MCP server, remind me that `.mcp.json` should point at `<alias>/mcp`, and say whether it already does.
