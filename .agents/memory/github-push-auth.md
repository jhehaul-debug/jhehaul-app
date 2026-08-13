---
name: GitHub push auth
description: How to push to GitHub from this Replit workspace when the token rotates
---

# GitHub Push Authentication

## The rule
The git remote URL stores the PAT inline:
`https://<TOKEN>@github.com/jhehaul-debug/jhehaul-app.git`

When `GITHUB_TOKEN` is updated in Replit Secrets, the remote URL is NOT automatically updated. You must run:

```bash
git remote set-url origin "https://$(printenv GITHUB_TOKEN)@github.com/jhehaul-debug/jhehaul-app.git"
git push origin main
```

**Why:** The Replit git askpass (`replit-git-askpass`) does not work for shell-initiated pushes — it only works for Replit-platform-initiated operations (task agent merges). Shell pushes must use the token directly in the URL.

**How to apply:** Any time a push returns 401 / "invalid token" / "unable to read askpass response", check `GITHUB_TOKEN` first (`gh auth status`), then re-run the set-url + push pair above.

## DigitalOcean auto-deploy
After a successful push to `main`, DigitalOcean detects the new commit and auto-deploys within 1–2 minutes. No manual trigger needed unless auto-deploy is disabled in the app settings.

## Repo
`github.com/jhehaul-debug/jhehaul-app` — branch `main` — connected to jhehaul.com via DigitalOcean App Platform.
