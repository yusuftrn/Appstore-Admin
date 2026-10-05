# Release checklist

1. Run the offline tests, Bandit and public-tree scanner from the README.
2. Review `git ls-files` and the staged diff. No profile, key, plan, export,
   screenshot, backup or private run should be tracked. New repos must start
   with fresh history, without copying the personal checkout's Git history.
3. Build a fresh wheel and source archive with `python tools/build_release.py`.
   This removes machine owner names, UID/GID and timestamps from the source archive. Run `tools/check_distribution.py`
   on both. Install the wheel in an empty environment and test CLI/skill/schema
   loading without credentials or network access.
4. Confirm README/version/license/third-party notices match the final artifacts.
   Record the actual checks and limitations in `docs/security-review.md`.
5. Choose the GitHub owner, repository name and visibility. Add a remote and
   push only after authorization to publish. Local preparation grants no remote
   write or PyPI publication permission.
6. On GitHub, enable private vulnerability reporting, secret scanning/push
   protection where available, and branch rules requiring CI before merging.
   Those settings are not configured by merely committing workflow files.
7. Attach only freshly checked distribution files to a release. Never upload a
   ZIP of the personal working directory. Review artifact contents again after
   any packaging change. No release, upload or PyPI workflow runs automatically.

The package name is not reserved on PyPI. Verify ownership and choose a
nonconflicting registry name separately before any future PyPI publication.
