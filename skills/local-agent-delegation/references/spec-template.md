# delegate_task spec template

Copy, fill every section, delete the hints. Short is fine; vague is not.

```
GOAL
<One sentence, stated as a change: "X currently does A; change it so it does B." If the issue is a
question, you decide the answer here.>

ROOT CAUSE / WHERE
<File(s) and function(s) to change, and your hypothesis of why it's wrong. e.g.
"src/pluggy/_manager.py::PluginManager.register mutates _name2plugin before validating all
hookimpls (see the XXX comment)." If unknown, say what to inspect first.>

REQUIRED BEHAVIOR (all of these)
- <case 1, with a concrete input -> expected output>
- <case 2 ... include the non-obvious variants: other number bases, empty input, None, async path,
  both class- and instance-style specs, etc.>
- <exact error type + message text / exact new public name, if anything will match on it>

MUST NOT CHANGE
- <existing behaviors/tests that must keep passing, especially edge cases near the change, e.g.
  "historic hook replay during register() must still verify specs added by the replay">

VERIFY (run these; all must pass)
  <exact command covering every test module that touches the changed code, e.g.
   python -m pytest -q testing/test_pluginmanager.py testing/test_hookcaller.py>
If an EXISTING test fails, the change is wrong: fix the code, do not edit or delete that test.

CONSTRAINTS
- Minimal change; add new tests for the new behavior next to similar ones.
- Do not modify existing tests. No new dependencies (no network). Do not commit.
- Finish with: what you changed, the verify command output summary, anything unresolved.
```
