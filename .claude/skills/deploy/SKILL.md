---
name: deploy
description: Push and redeploy a homelab project (options-alerter, pia-speedtest, or UNRAIDnotUnHealthy) to Unraid. Auto-detects which project to deploy by checking unpushed commits; pass an explicit project name to override.
argument-hint: [<project>] [--dry-run]
allowed-tools: [Bash, Read]
---

# Deploy a homelab project to Unraid

Three projects live under `~/` and each has its own `scripts/deploy-unraid.sh`:

- **options-alerter** — port 8000, container `options-alerter`, health via `/api/version`
- **pia-speedtest** — port 8080, container `pia-speedtest`, health via `/api/version`
- **UNRAIDnotUnHealthy** — port 3000, container `UNRAIDnotUnHealthy`, health via Grafana `/api/health`

All three share the same SSH key (`~/.ssh/unraid_deploy`) and Unraid box (`root@192.168.1.133:2222`), and all three follow the same flow: push to main → GitHub Actions builds + publishes to ghcr.io → SSH to Unraid → `php update_container <name>` → verify health endpoint.

This skill picks the right project to deploy, since the session cwd doesn't always match what the user has been working on.

## How to invoke

When the user types `/deploy`:

### 1. Pick the target project

**If `$ARGUMENTS` contains an explicit project name** (`options-alerter`, `pia-speedtest`, `UNRAIDnotUnHealthy`, or unambiguous prefix like `pia` / `options` / `unraid`), use that. Skip detection.

**Otherwise auto-detect** by counting unpushed commits per project:

```bash
for p in ~/options-alerter ~/pia-speedtest ~/UNRAIDnotUnHealthy; do
  ahead=$(git -C "$p" rev-list --count origin/main..HEAD 2>/dev/null || echo "0")
  printf "%-30s %s unpushed\n" "$(basename "$p")" "$ahead"
done
```

Decide:
- **Exactly one project has unpushed commits → deploy that one.** Common case.
- **Multiple have unpushed commits → ask the user which.** Don't guess.
- **None has unpushed commits → ask the user which.** Unusual; they may want a no-op redeploy to refresh build args, but be explicit.

State your choice in one sentence before running so the user can interrupt if it's wrong.

### 2. Dry-run path

If `$ARGUMENTS` contains `--dry-run`, `cat ~/<project>/scripts/deploy-unraid.sh` and summarize what it would do. Do not run.

### 3. Run the deploy

```bash
cd ~/<project> && bash scripts/deploy-unraid.sh
```

Use a 600000ms (10 min) timeout — UNRAIDnotUnHealthy's image is heavy and a cache-cold CI build can take ~5 min; pia-speedtest and options-alerter are usually under 90s.

### 4. Report concisely

Pull the version line out of output and surface it as the bottom line. The script already logs each phase (`[deploy] ✓ pushed to origin`, etc.) — don't restate. If exit is non-zero, surface error + exit code so the user can act.

## Failure modes worth flagging

- **Exit 1** — local working tree dirty or push refused. Show `git status --short` from the active project's dir.
- **Exit 2 ssh failed** — `~/.ssh/unraid_deploy` missing or Unraid box unreachable.
- **Exit 3** — `update_container` failed on Unraid. Could be: image pull denied (token expired), template XML missing for that container, Docker daemon issue. SSH and check `docker logs <container>`.
- **Exit 4** — container restarted but health endpoint didn't respond. The script doesn't dump remote logs; suggest the user SSH and `docker logs <container> --tail 50`.
- **Exit 5** — CI run for the pushed SHA didn't appear in 30s. Workflow file may be missing or `gh` isn't authenticated.
- **Exit 6** — CI build failed. Surface the `https://github.com/jztiger/<project>/actions/runs/<id>` URL.

## When NOT to run

- The target project has uncommitted local changes — let the user commit first. (Detection still works, but deploy will exit 1.)
- A pia-speedtest sweep is currently running (check `http://192.168.1.227:8080/api/status` when dispatching to pia-speedtest). Rebuild kills the active WG tunnel and orphans the run.
- The user just deployed seconds ago — confirm before re-running.
