"""Installing the plugin must not install instructions for coding agents.

`omarchy plugin add` clones this repository into
`~/.config/omarchy/plugins/<id>/`, so every tracked file lands in a directory
that coding agents routinely operate in or below. A file named `AGENTS.md`
(or any of its siblings) is discovered and applied automatically there, which
hands whoever wrote it an instruction channel into the user's agent that the
user never opted into -- independent of whether the text happens to be benign.

Found in the marketplace security review of 2026-09-14 at commit `36b4fa4`,
which blocked listing. tonearm's file was renamed to `CONTRIBUTING.md`; this
test is what stops it coming back under any of the names that carry the same
meaning.

`git ls-files` is the right question to ask because it is exactly what a
clone produces. An untracked `AGENTS.md` in a local worktree is fine -- it is
not published.
"""

import os
import subprocess
import unittest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# Filenames that a coding agent reads as instructions addressed to itself.
AGENT_INSTRUCTION_FILES = {
    "agents.md",
    "claude.md",
    "gemini.md",
    "conventions.md",
    "copilot-instructions.md",
    ".cursorrules",
    ".windsurfrules",
    ".clinerules",
    ".aider.conf.yml",
}


def tracked_files():
    out = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return [p for p in out.stdout.split("\0") if p]


class TestNoAgentInstructionsAreShipped(unittest.TestCase):

    def test_no_tracked_file_instructs_a_coding_agent(self):
        offenders = [p for p in tracked_files()
                     if os.path.basename(p).lower() in AGENT_INSTRUCTION_FILES]
        self.assertEqual(offenders, [], "\n".join([
            "These files ship to ~/.config/omarchy/plugins/ and would be read",
            "as instructions by any coding agent working there:",
            *(f"  {p}" for p in offenders),
            "Rename to a filename that carries no agent meaning "
            "(CONTRIBUTING.md, docs/…).",
        ]))

    def test_the_contributor_guide_survived_the_rename(self):
        # The rename is only correct if the content is still there to read;
        # deleting it would also pass the test above.
        self.assertIn("CONTRIBUTING.md", tracked_files())


if __name__ == "__main__":
    unittest.main()


class TestTheSingletonSurvivesInstallation(unittest.TestCase):
    """`qmldir` is what makes Service a singleton, and it fails silently.

    Without it, `import "."` resolves nothing, `Service` is an unknown type,
    and every binding that reads it raises a ReferenceError inside a property
    binding -- which qmllint does not catch and which presents as a widget
    that renders nothing, with nothing in the journal. The file is three lines
    of declaration guarding a behaviour nothing else asserts, so what gets
    pinned here is that it ARRIVES: tracked for a clone, and not excluded from
    the dev deploy.
    """

    def qmldir_lines(self):
        with open(os.path.join(REPO, "qmldir")) as handle:
            return [line.strip() for line in handle
                    if line.strip() and not line.startswith("#")]

    def test_the_qmldir_is_tracked_so_a_clone_gets_it(self):
        # `omarchy plugin add` clones; an untracked qmldir reaches nobody.
        self.assertIn("qmldir", tracked_files())

    def test_every_type_the_qmldir_declares_exists_and_is_tracked(self):
        # Renaming Service.qml without touching qmldir is the same silent
        # failure as shipping no qmldir at all.
        declarations = [l for l in self.qmldir_lines() if l.startswith("singleton ")]
        self.assertTrue(declarations, "qmldir declares no singleton")
        tracked = set(tracked_files())
        for line in declarations:
            target = line.split()[-1]
            self.assertTrue(os.path.isfile(os.path.join(REPO, target)),
                            "qmldir names %s, which does not exist" % target)
            self.assertIn(target, tracked)

    def test_the_dev_deploy_does_not_exclude_the_qmldir(self):
        # bin/dev rsyncs with a blocklist. A future --exclude that catches
        # this file would leave the deployed plugin importing a type that is
        # not there, while the repo copy kept working.
        with open(os.path.join(REPO, "bin", "dev")) as handle:
            deploy = handle.read()
        self.assertNotIn("--exclude 'qmldir'", deploy)
        self.assertNotIn('--exclude "qmldir"', deploy)
