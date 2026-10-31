# Progress

## Release 0.1.0

- AWAITING-HUMAN: PyPI pending publisher for `acdp-agent-hooks`. The `v0.1.0`
  release run (40416968747) failed at publish with `invalid-publisher`. On
  pypi.org, add a pending trusted publisher: project `acdp-agent-hooks`, owner
  `agentcontextdistributionprotocol`, repo `acdp-agent-hooks`, workflow
  `release.yml`, environment `pypi` (the environment already exists in the repo).
  Then re-run the failed jobs of run 40416968747; no new tag is needed.
- DONE: branch protection on `main` matches the acdp-ci `standardize.sh`
  baseline (`--check` in sync, six required checks), so auto-merge can arm.
