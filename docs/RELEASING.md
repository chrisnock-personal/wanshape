# Releasing to PyPI

> **Not published yet (2026-11-26).** The package metadata (`pyproject.toml`) and the tag-triggered Release workflow (`.github/workflows/release.yml`) exist, but there are no tags or GitHub releases and `https://pypi.org/pypi/wanshape/json` returns 404. The README still installs from GitHub. Nothing below has been done yet; this is a checklist, not a record of a release.

This covers the publishing part of issue #14. The rest of the release process is ordinary Git tagging.

## One-time PyPI setup

PyPI trusted publishing uses OpenID Connect (OIDC): the workflow identifies itself to PyPI and receives a short-lived publishing credential. No PyPI API token is stored in the repository or in GitHub secrets.

On pypi.org, as the account that will own the project, add a pending publisher (Publishing settings → [Add a pending publisher](https://pypi.org/manage/account/publishing/)) with these values:

| Field | Value |
|---|---|
| PyPI project name | `wanshape` |
| Owner | `chrisnock-personal` |
| Repository name | `wanshape` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

The workflow already declares `environment: pypi` and `permissions: id-token: write`. If the `publish` job cannot use the `pypi` environment, PyPI will not recognise the publisher and the upload will fail.

## Before tagging

- [ ] The version in `pyproject.toml` matches the intended tag (`0.4.0` → `v0.4.0`). The workflow checks this and fails if they differ.
- [ ] The README is accurate: it still uses the git URL, which stays until a PyPI install is verified. There is no `CHANGELOG` file yet; if one is added, update it.
- [ ] If `build` and `twine` are available, build locally and check the distribution: `python -m build`, then `twine check dist/*`.
- [ ] Unit tests: `PYTHONPATH=src python -m unittest discover -s tests` (155 passing on 2026-11-26).
- [ ] Optional: install the built wheel in a throwaway environment and run `wanshape doctor`:

  ```sh
  pipx install --suffix=-test ./dist/wanshape-0.4.0-py3-none-any.whl
  wanshape-test doctor
  pipx uninstall wanshape-test
  ```

## Release

- [ ] Commit any version bump (and README changes for that version) and push to `main`.
- [ ] Tag and push the tag:

  ```sh
  git tag v0.4.0
  git push origin v0.4.0
  ```

- [ ] Watch the Actions **Release** workflow: `build` (version/tag check, build, unit tests, upload `dist`), then `publish` (`pypa/gh-action-pypi-publish`).
- [ ] Check that `publish` ran in the `pypi` environment. If it did not, fix the pending publisher or environment before tagging again.

## After publishing

- [ ] Check that `https://pypi.org/pypi/wanshape/json` returns 200 (it returns 404 now):

  ```sh
  curl -fsS https://pypi.org/pypi/wanshape/json > /dev/null
  ```

- [ ] Install from PyPI, not from the git URL. Replace an existing git-based install so the test proves the published package works:

  ```sh
  pipx uninstall wanshape   # if already installed from git
  pipx install wanshape
  wanshape doctor
  ```

- [ ] Only once that works, change the README Install section to `pipx install wanshape`, remove the "not published yet" note there, and keep the full-path sudo guidance.
- [ ] Optionally create a GitHub Release for the tag; the workflow does not do this.

## Not included

- The optional RPM spec (Fedora, system-wide install) suggested in issue #14 is not written.
- A `--sudo` mode is not implemented. Host and VM modes still need root; the documented approach is `sudo "$(command -v wanshape)" ...`, because `sudo` resets `PATH`.
