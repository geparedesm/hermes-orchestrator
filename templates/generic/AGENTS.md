# {{name}}: development rules

{{description}}

These rules apply to every change, by people and by agents. When a rule and a request conflict, stop and ask: do not
guess.

## The first task: pick the stack

This project starts empty. The first task:
1. picks the stack that fits the description (language, framework, test runner) and says why in its result;
2. creates the minimal skeleton with at least one passing test;
3. proposes the real `.hermes/project.yaml`: the toolchain, the `build` and `test` commands, and a Quality Gate that
   requires tests. It is a configuration change: a person approves it;
4. updates this file with the stack's own rules (structure, naming, how to run the tests) and the README.

## Always

- Code, comments, and documentation in English.
- Every change comes with its tests, and the tests pass before you finish.
- Small, focused commits: `[ACTION] Description`, where ACTION is one of ADD, FIX, IMP, REF, REM, TEST, DOC.
- No secrets in the repository: no tokens, passwords, keys, or real personal data. Use environment variables and
  document them in the README.
- Update the README when behavior visible to users changes.
- Keep each change to the task; propose anything else as a follow-up.
