# Progress

## Release 0.1.0

- AWAITING-HUMAN: PyPI pending publisher for `acdp-agent-hooks`. The `v0.1.0`
  release run (40416968747) failed at publish with `invalid-publisher`. On
  pypi.org, add a pending trusted publisher: project `acdp-agent-hooks`, owner
  `agentcontextdistributionprotocol`, repo `acdp-agent-hooks`, workflow
  `release.yml`, environment `pypi` (the environment already exists in the repo).
  Then re-run the failed jobs of run 40416968747; no new tag is needed.
- AWAITING-HUMAN: branch protection on `main`. The org `auto-merge` workflow
  refuses to arm because `main` has no required status checks
  (`protected=false`). Adopt it with acdp-ci `scripts/standardize.sh`.
