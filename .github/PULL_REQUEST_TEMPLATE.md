## Summary

<!-- What changed and why. Link the issue if one exists. -->

## Type

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor
- [ ] Documentation
- [ ] CI / tooling
- [ ] Security

## Verification

<!-- Paste the commands you ran and their result. -->

- [ ] `python -m pytest tests -q`
- [ ] `python -m compileall -q lilbot`
- [ ] `ruff check lilbot tests` (if installed)
- [ ] Manual check on Windows, Linux, or macOS where relevant

## Checklist

- [ ] One logical change per pull request.
- [ ] New behavior has a test that fails without the change.
- [ ] User-visible behavior is documented in `README.md` or `docs/`.
- [ ] `CHANGELOG.md` has an entry under `Unreleased`.
- [ ] No secrets, tokens, or personal paths are included.
- [ ] Permission, sandbox, and session-persistence changes include a failure-path test.
