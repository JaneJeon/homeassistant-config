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
2. GitHub runs Home Assistant's own config check (`.github/workflows/check.yaml`), at the version in `.HA_VERSION`.
3. Merge to `master`.
4. Deploy: run `script.deploy_config` (in `packages/config_as_code.yaml`). It starts the Git pull app, which pulls `master`, then fires `config_as_code_pulled`. The automation `Config repo - reload after pull` catches that and runs `homeassistant.reload_all`. No restart.
   The reload lives in an automation on purpose: a script that reloads scripts can replace itself mid-run and orphan its own entity.

Deploys are not automatic yet. Run the script after merging.

## Drift

`sensor.config_repo_drift` counts files on the Green that differ from this repo. If it stays above zero for 30 minutes, a Telegram alert fires: something was edited outside git.
