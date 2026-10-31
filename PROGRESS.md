# Progress

## Release 0.1.0

- DONE: 0.1.0 is on PyPI (wheel + sdist, with attestations) and on the
  `v0.1.0` GitHub release. The PyPI trusted publisher was created and release
  run 40416968747 re-ran green (attempt 2). Install from PyPI was smoke-tested
  in clean venvs, base and `[claude,openai,otel]`.
- DONE: branch protection on `main` matches the acdp-ci `standardize.sh`
  baseline (`--check` in sync, six required checks), so auto-merge can arm.
