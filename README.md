# homeassistant-config

The Home Assistant config for the Green, as code. This repo is the source of truth for what it tracks.

## Rules

- Edit tracked files here, never on the Green or in the HA UI.
- `master` is what should be live. Every change to `master` must pass the config check first.
- Secrets never go in this repo. Real values live only in `secrets.yaml` on the Green. `secrets.fake.yaml` has the same keys with dummy values, for the check.

## Local setup

```
nvm use && npm install   # installs prettier, lint-staged and the husky hooks
brew install gitleaks    # the pre-commit hook refuses to run without it
npm run lint             # prettier check + inline-secret check
npm run format           # prettier write
```

Prettier skips `automations.yaml`, `scripts.yaml` and `scenes.yaml` because HA's UI rewrites them in its own style.

## Secret guard

Three layers, because no single one covers every way a commit reaches GitHub:

1. Local hook (`.husky/pre-commit`): prettier on staged files, gitleaks on the staged diff, then `tools/check-inline-secrets.py`. Installed by `npm install` (needs `brew install gitleaks`). Agents committing from their own sandboxes may not have it, so it is a convenience, not the guarantee.
2. CI (`.github/workflows/secret-scan.yaml`): the same two checks on every push and pull request, gitleaks over the full history.
3. GitHub push protection (on by default for public repos): blocks known token formats server-side for every pusher, including agents.

`tools/check-inline-secrets.py` is the HA-specific rule: any credential-shaped key (`password`, `token`, `api_key`, `Authorization`, ...) must be `!secret name`, never an inline value, and `secrets.yaml` is never committed. New secrets go into `secrets.yaml` on the Green by hand, plus a fake value in `secrets.fake.yaml`.

## What is tracked, and what is not

Tracked: automations, scripts, scenes, templates, packages, custom_templates, blueprints.

Not tracked, because Home Assistant keeps them in its internal `.storage` folder: integrations, devices, entity names, areas, labels, UI-made helpers and dashboards. HA's automatic backups cover those.

## How a change goes live

1. Change files on a branch, open a pull request.
2. GitHub runs Home Assistant's own config check (`.github/workflows/check.yaml`), at the version in `.ha-version` (Renovate bumps it after each HA release).
3. Merge to `master`.
4. Deploy happens by itself. The Git pull app polls GitHub every 2 minutes and fast-forwards the Green. `sensor.config_repo_head` notices the new commit within a minute, and the automation `Config repo - reload on new commit` runs `homeassistant.reload_all`, which checks the config first and refuses a broken one. No restart. To skip the wait, run `script.deploy_config`.

Nothing in GitHub can reach the Green: no deploy secrets, no inbound webhook. The Green only reaches out.

## Alerts

- Drift: `sensor.config_repo_drift` counts files on the Green that differ from this repo. Above zero for 30 minutes sends a Telegram alert: something was edited outside git.
- Behind: `sensor.config_repo_head` differing from `sensor.config_repo_github_master` for 15 minutes sends a Telegram alert: pulls are failing.
