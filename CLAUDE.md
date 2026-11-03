# OpenRV Kitsu integration - Project Guidelines

OpenRV package that adds a **Kitsu Review** panel to RV: browse revisions, comment, and sync annotations both ways. It talks to Zou through gazu. See `README.md` for features and limitations.

## Quick Reference

```bash
# Sort imports, then format (Black lives in the pyenv 3.11 version on this machine; isort must be installed)
isort --profile black -l 79 src test
PYENV_VERSION=3.11.14 black -l 79 src test

# Run the converter without OpenRV
python src/annotation_converter.py annotations.json --width 1920 --height 1080 --fps 24 -o shapes.json

# Package locally (install steps are in README.md)
zip -r kitsu-1.0.rvpkg ./src/PACKAGE ./src/*.py
```

## Architecture

Dependencies go one way: `kitsu.py` → `kitsu_review_model.py` → the other modules.

| Module | Role | May import |
|--------|------|------------|
| `src/kitsu.py` | Panel widgets, event handlers, the RV `MinorMode` | Qt, rv, the model |
| `src/kitsu_review_model.py` | gazu calls, status rules, revision state. Creates no widgets | gazu, requests, rv, the modules below |
| `src/kitsu_session_store.py` | Saved identity and tokens | QSettings, keyring |
| `src/kitsu_rv_session.py` | RV sources and paint nodes | rv |
| `src/kitsu_review_log.py` | Log messages | Nothing |
| `src/annotation_converter.py` | Pure Kitsu ↔ RV conversion and its CLI | Standard library only |

- Keep the layers: no gazu call in `kitsu.py`, no Qt, rv or gazu in the converter.
- Keep the guarded imports (`try/except ImportError`, `_INSIDE_OPENRV`): modules must stay importable outside OpenRV.
- `keyring` is optional: without it, **Remember me** is disabled and everything else still works.
- There are no worker threads: Kitsu calls run on the Qt main thread and block the UI.

## Code Conventions

Shared with zou and gazu:

- **Black** with line length 79, and **isort** with the Black profile. There is no config file and no pre-commit hook: pass the flags shown in Quick Reference.
- **f-strings only** in new or edited code. Multi-line: implicit concatenation, not `+`.
- **Imports sorted alphabetically** inside a parenthesized `from x import (...)` block. Adding a name to an unsorted block means sorting the whole block.
- **No parentheses around tuples** where they add nothing: `return a, b`.
- **Docstrings**: opening and closing `"""` each on their own line, even for one-liners.
- **Comments and docstrings are written in [ASD-STE100](https://www.asd-ste100.org/)** Simplified Technical English: short sentences (20 words for an instruction, 25 for a description), active voice, simple tenses, one topic per sentence, no contractions. Technical names stay as they are. Log messages and UI text are not covered.
- **Comments** only for the non-obvious why. Eraser and coordinate conversions usually qualify.
- **Naming**: `snake_case` functions, verb-first. `convertToKitsu` and `convertToORV` are public names: don't copy the camelCase and don't rename them.
- No em-dashes in prose, comments, commit or PR text.

Python runs inside OpenRV's bundled interpreter, and no version is pinned here: check the build you target before using recent syntax. Dependencies are installed by hand into that interpreter (see `README.md`), so a new one has a real cost for users.

## Talking to Kitsu

- **Use gazu wrappers.** Follow gazu's `all_*` / `get_*` / `new_*` / `update_*` / `remove_*` naming. Use `gazu.client` or `requests` directly only when no wrapper exists, and give direct `requests` calls a timeout.
- **A missing wrapper belongs in gazu first** (see `../gazu/CLAUDE.md`) when it is generally useful. Check that the route exists in `../zou/zou/app/blueprints/*/__init__.py`.
- **Zou owns permissions.** UI filtering, such as the status dropdown, is a convenience: still handle the error when Zou refuses.
- **UUIDs identify everything.** Never match on a display name when an id is available.
- **Secrets**: the session token goes only in the OS credential store. Never save the password, and never log or print tokens or passwords.
- **Annotations must not lose data.** Unchanged annotations keep their Kitsu id, author and timestamps. Changes go through `split_annotation_changes`, so one export does not overwrite other reviewers' work.

## Annotation conversion

- Conversion must hold in both directions. When you change one side, check the other with a round trip.
- Erasers differ between the sides: read the module docstring of `annotation_converter.py` first.
- Kitsu stores Fabric.js objects. If Kitsu changes that format (`../kitsu/`), the converter must follow.
- A feature that cannot cross over goes in the README's **Limitations** section.

## Testing

There is no automated suite and no linter in CI.

- `test/annotation_image.py` and `test/annotation_video.py` are manual scripts that dump annotations from a live Kitsu into JSON fixtures. Don't add credentials to them.
- Check converter changes with the CLI on a real dump.
- Panel and paint-node changes can only be checked inside OpenRV: say so when you could not run them.

## Packaging & Release

- `src/PACKAGE` holds the version and the mode list. Bump the version there.
- Pushing a `v*` tag runs `.github/workflows/package.yml`, which attaches the `.rvpkg` to the GitHub release.
- The package holds `src/PACKAGE` and every `src/*.py`, so a new module needs no packaging change.

## Git & PR

- **Commits**: `[scope] Imperative description`, with the domain as scope: `annotations`, `review`, `auth`, `packaging`.
- **PR body**: bold two-paragraph format, no `##` headers, no AI attribution.

  ```markdown
  **Problem**
  - Concise bullet per issue

  **Solution**
  - Concise bullet per fix
  ```

- Contributions follow the [C4 contract](https://rfc.zeromq.org/spec:42/C4).
- Update `README.md` when behavior, limitations or install steps change.
