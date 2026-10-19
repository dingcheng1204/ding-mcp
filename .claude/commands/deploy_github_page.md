---
description: Push this local project to a GitHub repository (creating it if needed) and print the repo URL
argument-hint: "[repo-name] [public|private]  (default: current folder name, private)"
allowed-tools: Bash(gh:*), Bash(git:*), Bash(basename:*), Bash(pwd:*), Bash(grep:*), Bash(curl:*), Read
---

Deploy the project in the current directory to my GitHub account and give me the URL where I can see the repo, i.e. `https://github.com/<MY_USERNAME>/<MY_REPO>`.

Arguments: `$ARGUMENTS` — optional repo name (default: the current folder name) and optional visibility `public` or `private` (default: `private`).

Walk me through each prerequisite. If a step needs me to do something interactive, stop, tell me exactly what to run, and ask me to re-run `/deploy_github_page` afterwards.

## Steps

1. **Check the tools.**
   - Run `git --version`. If missing, stop and tell me to install it (e.g. `sudo apt install git`).
   - Run `gh --version`. If missing, stop and tell me to install the GitHub CLI (`sudo apt install gh`, or see https://cli.github.com). Do not install it yourself.

2. **Check GitHub login.**
   - Run `gh auth status`. If not logged in, stop and tell me to run `! gh auth login` in the prompt (it is interactive; choose GitHub.com → HTTPS → login with a web browser), then re-run `/deploy_github_page`.
   - Get my username with `gh api user --jq .login` and tell me which account is active.
   - Check git identity: `git config user.name` and `git config user.email`. If either is empty, stop and tell me to set them with `! git config --global user.name "..."` / `user.email "..."`.

3. **Check the local repository.**
   - If the folder is not a git repo (`git rev-parse --is-inside-work-tree` fails), run `git init -b main`.
   - **Secrets check before anything is committed:** make sure `.gitignore` exists and covers `.env`, `.env.*`, `.venv/`, `__pycache__/`, `.claude/settings.local.json`. If something is missing, show me what you would add and add it. Then run `git status --short` and `git ls-files` and warn me loudly if any tracked or to-be-added file looks like a secret (`.env`, `*.pem`, `*.key`, `id_rsa*`, files containing `API_KEY=` / `TOKEN=`). Do not push until I confirm in that case.
   - If there are uncommitted changes:
     - List them, and read `git diff`, `git diff --stat`, and any untracked files to understand *what* changed and *why*.
     - Look at `git log --oneline -5` to match my existing commit message style.
     - Draft a detailed but concise commit message, always in English:
       - Subject line: ≤ 72 chars, imperative mood, summarizing the overall change (e.g. "Add random_number tool and deploy commands").
       - Blank line, then a body grouped by file (or area), one bullet per concrete change, naming the function/setting/value that changed — say *what* changed, not just "update X".
       - Keep each bullet short (one line, ≤ 72 chars, no filler words). Mention the *why* in a few words only when it isn't obvious.
       - Example:
         ```
         Add random_number tool and Claude deploy commands

         server.py:
         - Add random_number(low, high) tool; inclusive range
         - Raise ToolError when low > high

         test_client.py:
         - Expect random_number in tool list
         - Check result stays within 5..10 and low > high errors

         .mcp.json:
         - Point ding-mcp at https://ding-mcp.vercel.app/mcp
         ```
     - Show me the files to be committed and the drafted message, then ask me to choose: **commit with this message**, **edit the message**, or **skip committing** (push only what is already committed). Do not commit until I answer.
   - If there are no commits at all yet, draft an initial commit message the same way, show me what will be included, and ask before committing.

4. **Find or create the GitHub repository.**
   - Check `git remote get-url origin`.
     - **Remote exists:** extract `<owner>/<repo>` from it (works for both `git@github.com:owner/repo.git` and `https://github.com/owner/repo.git`) and confirm it exists with `gh repo view <owner>/<repo> --json url,visibility`. If it does not exist on GitHub, offer to create it (next bullet).
     - **No remote:** use the repo name from `$ARGUMENTS` (or `basename "$(pwd)"`). Check whether `<username>/<name>` already exists with `gh repo view`. If it exists, ask me whether to use it as `origin` or pick a different name. If it does not exist, tell me the name and visibility you are about to use, then create it:
       `gh repo create <name> --<private|public> --source=. --remote=origin`
   - Never delete, rename, or force-overwrite an existing GitHub repo.

5. **Push.**
   - Run `git push -u origin <current-branch>` (get it from `git branch --show-current`).
   - If the push is rejected because the remote has commits I do not have, stop and explain the options (`git pull --rebase origin <branch>` then push). **Never use `--force`** unless I explicitly ask.
   - If an SSH remote fails with a permission/key error, suggest `gh auth setup-git` and switching to HTTPS with `git remote set-url origin https://github.com/<owner>/<repo>.git`, and ask before changing it.

6. **Verify.**
   - `gh repo view <owner>/<repo> --json url,visibility,defaultBranchRef --jq '"\(.url) \(.visibility) \(.defaultBranchRef.name)"'`
   - Confirm the latest local commit (`git rev-parse --short HEAD`) matches the remote branch (`git ls-remote origin <branch>`).

7. **Report** in a short summary:
   - 🔗 **Repo URL:** `https://github.com/<owner>/<repo>`
   - Clone URL: `https://github.com/<owner>/<repo>.git`
   - Account, visibility (public/private), branch, and the commit that was pushed
   - If the repo is private, remind me only I (and collaborators) can see it; if I want a public website from it, mention that GitHub Pages can be enabled under Settings → Pages (this project is a Python server, so Pages would only serve static files).
