# AGENTS.md — how agents work on afl-cam

Several agent sessions (cloud and local) work on this repo at once. These
rules keep them out of each other's way. Project knowledge and hardware
invariants live in `CLAUDE.md`; read it before touching `afl_cam/native/`
or the daemon.

## The board is a test rig, not a dev box

Code is written and built off the board. The Orange Pi only runs tests:
`scripts/rig_test.sh` checks out nothing itself — it runs a given tree's
daemon on a side port, queued on a lock, then restores the installed
`afl-camd`. Never edit the checkout at `/home/hamish/Documents/afl-cam`
on the board, never run tests there outside `rig_test.sh`, and never write
to `calibration/` (the frozen alignment is shared by every test run).

## Picking up work

Work is GitHub Issues. Labels: `ready` (free to take), `claimed`,
`blocked`, `needs-rig`, `area:*`.

```bash
gh issue list -l ready                     # what's free
gh issue edit N --add-label claimed --remove-label ready
gh issue comment N -b "Claimed: branch issue-N-<slug>"
```

Claim before starting; if the issue already says `claimed`, pick another.
If you find more work, open a new issue with the Task template rather than
widening yours. If you stop, say where you got to in a comment and swap
`claimed` back to `ready` (or `blocked`, with the reason).

## Branches, worktrees, PRs

- One issue → one branch `issue-N-<slug>` off `origin/main` → one PR with
  `Closes #N`. Never push to `main`.
- Local sessions use a worktree per issue so parallel sessions never share
  a working tree: `claude -w` or
  `git worktree add ../afl-cam-wt/issue-N -b issue-N-<slug> origin/main`.
  Remove it after merge (`git worktree remove`).
- Keep PRs small and focused; the rig tests one PR at a time.

## Testing

- **Cloud sessions** (no board access): push the branch and open the PR.
  `rig-test` runs on the board automatically and reports on the PR; fix
  what it reports. Label the PR `rig:slow` to include the record cycle.
- **Local sessions**: `scripts/remote_rig_test.sh` ships your worktree
  (uncommitted edits included) to the board over Tailscale ssh and runs
  the same suite. `RIG_SLOW=1` for the record cycle; pytest args pass
  through (`scripts/remote_rig_test.sh -k preview`).
- Web UI: drive `http://<board>/` with Playwright from your machine;
  screenshots are evidence in the PR.
- Performance claims (fps, encode ms, frame capture %) need a rig number in
  the PR, measured with nobody on VNC.
