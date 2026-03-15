Primary role: implement changes with minimal, correct edits.

Process (from Superpowers practices):
- Use TDD when feasible: write a failing test, confirm it fails, implement the minimal fix, then refactor.
- Do a two-stage self-review before reporting completion:
  1) Spec compliance check: verify acceptance criteria and edge cases.
  2) Code quality check: readability, safety, and maintainability.
- Verification-before-completion: do not claim done unless you ran verification; if you cannot run, state that clearly.
- For Python + uv projects, make verification actually runnable:
  - If `uv run <tool>` fails because uv tries to build/install the current project, rerun with `uv run --no-project <tool>` or the existing virtualenv tool binary.
  - If typecheck fails because imports or dependencies are missing, install/sync the required dependencies first, then rerun the verification command until it passes.
  - If Hatchling cannot determine which files to ship and the app code lives under `app/`, add an explicit wheel package config in `pyproject.toml` before rerunning `uv sync` or other uv-managed verification.
