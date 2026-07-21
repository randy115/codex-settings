from __future__ import annotations

import argparse
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "herdr_jobs.py"
SPEC = importlib.util.spec_from_file_location("herdr_jobs", SCRIPT)
assert SPEC and SPEC.loader
herdr_jobs = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = herdr_jobs
SPEC.loader.exec_module(herdr_jobs)


def start_args(**overrides):
    values = {
        "workspace": None,
        "new_workspace": False,
        "workspace_label": None,
        "cwd": None,
        "task": [["review", "Review the configuration."]],
        "confirm": False,
        "timeout": 30_000,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def cleanup_args(**overrides):
    values = {"group": None, "force": False}
    values.update(overrides)
    return argparse.Namespace(**values)


class StartTests(unittest.TestCase):
    def test_existing_workspace_launch_path_is_unchanged(self):
        workspace = herdr_jobs.Workspace("w3", "tmux-setting", "/tmp")

        def fake_run(arguments):
            if arguments[:2] == ["agent", "start"]:
                return {
                    "workspace_id": "w3",
                    "tab_id": "w3:t2",
                    "pane_id": "w3:p2",
                    "terminal_id": "term_job",
                }
            return {}

        with (
            mock.patch.object(
                herdr_jobs, "list_workspaces", return_value=[workspace]
            ),
            mock.patch.object(herdr_jobs, "discover_jobs", return_value=[]),
            mock.patch.object(herdr_jobs, "choose_group_token", return_value="a1b2"),
            mock.patch.object(
                herdr_jobs, "resolve_workspace", return_value=workspace
            ),
            mock.patch.object(
                herdr_jobs, "create_owned_workspace"
            ) as create_workspace,
            mock.patch.object(herdr_jobs, "run_herdr", side_effect=fake_run) as run,
        ):
            payload, code = herdr_jobs.start_command(
                start_args(workspace="tmux-setting")
            )

        self.assertEqual(code, 0)
        self.assertFalse(payload["workspace"]["created_by_skill"])
        create_workspace.assert_not_called()
        start_call = run.call_args_list[0].args[0]
        self.assertEqual(start_call[start_call.index("--workspace") + 1], "w3")

    def test_new_workspace_launches_without_focus_and_closes_reused_bootstrap_pane(self):
        with tempfile.TemporaryDirectory() as cwd:
            workspace = herdr_jobs.Workspace(
                "w9", "hj-a1b2 · review", cwd, "a1b2"
            )
            bootstrap = {
                "workspace_id": "w9",
                "tab_id": "w9:t1",
                "pane_id": "w9:p1",
                "terminal_id": "term_boot",
            }

            def fake_run(arguments):
                if arguments[:3] == ["agent", "start", "hj-a1b2-review"]:
                    return {
                        "workspace_id": "w9",
                        "tab_id": "w9:t1",
                        "pane_id": "w9:p2",
                        "terminal_id": "term_job",
                    }
                return {}

            with (
                mock.patch.object(herdr_jobs, "list_workspaces", return_value=[]),
                mock.patch.object(herdr_jobs, "discover_jobs", return_value=[]),
                mock.patch.object(
                    herdr_jobs, "choose_group_token", return_value="a1b2"
                ),
                mock.patch.object(
                    herdr_jobs,
                    "create_owned_workspace",
                    return_value=(workspace, bootstrap, []),
                ),
                mock.patch.object(
                    herdr_jobs, "run_herdr", side_effect=fake_run
                ) as run,
            ):
                payload, code = herdr_jobs.start_command(
                    start_args(new_workspace=True, cwd=cwd)
                )

        self.assertEqual(code, 0)
        self.assertTrue(payload["workspace"]["created_by_skill"])
        start_call = run.call_args_list[0].args[0]
        self.assertIn("--no-focus", start_call)
        self.assertEqual(start_call[start_call.index("--cwd") + 1], cwd)
        self.assertIn(mock.call(["pane", "close", "w9:p1"]), run.call_args_list)

    def test_total_launch_failure_rolls_back_created_workspace(self):
        workspace = herdr_jobs.Workspace(
            "w9", "hj-a1b2 · review", "/tmp", "a1b2"
        )
        bootstrap = {
            "workspace_id": "w9",
            "tab_id": "w9:t1",
            "pane_id": "w9:p1",
            "terminal_id": "term_boot",
        }

        def fake_run(arguments):
            if arguments[:2] == ["agent", "start"]:
                raise herdr_jobs.HerdrCommandError(arguments, 1, "", "failed")
            return {}

        with (
            mock.patch.object(herdr_jobs, "list_workspaces", return_value=[]),
            mock.patch.object(herdr_jobs, "discover_jobs", return_value=[]),
            mock.patch.object(herdr_jobs, "choose_group_token", return_value="a1b2"),
            mock.patch.object(
                herdr_jobs,
                "create_owned_workspace",
                return_value=(workspace, bootstrap, []),
            ),
            mock.patch.object(herdr_jobs, "run_herdr", side_effect=fake_run) as run,
            mock.patch.object(herdr_jobs.os.path, "isdir", return_value=True),
        ):
            payload, code = herdr_jobs.start_command(
                start_args(new_workspace=True, cwd="/tmp")
            )

        self.assertEqual(code, 2)
        self.assertFalse(payload["launched"])
        self.assertIn(mock.call(["workspace", "close", "w9"]), run.call_args_list)

    def test_fanout_uses_one_created_workspace_for_every_agent(self):
        workspace = herdr_jobs.Workspace(
            "w9", "hj-a1b2 · 2-jobs", "/tmp", "a1b2"
        )
        bootstrap = {
            "workspace_id": "w9",
            "tab_id": "w9:t1",
            "pane_id": "w9:p1",
            "terminal_id": "term_boot",
        }

        def fake_run(arguments):
            if arguments[:3] == ["agent", "start", "hj-a1b2-config"]:
                return {
                    "workspace_id": "w9",
                    "tab_id": "w9:t1",
                    "pane_id": "w9:p2",
                }
            if arguments[:3] == ["agent", "start", "hj-a1b2-plugins"]:
                return {
                    "workspace_id": "w9",
                    "tab_id": "w9:t2",
                    "pane_id": "w9:p3",
                }
            return {}

        with (
            mock.patch.object(herdr_jobs, "list_workspaces", return_value=[]),
            mock.patch.object(herdr_jobs, "discover_jobs", return_value=[]),
            mock.patch.object(herdr_jobs, "choose_group_token", return_value="a1b2"),
            mock.patch.object(
                herdr_jobs,
                "create_owned_workspace",
                return_value=(workspace, bootstrap, []),
            ),
            mock.patch.object(herdr_jobs, "run_herdr", side_effect=fake_run) as run,
            mock.patch.object(herdr_jobs.os.path, "isdir", return_value=True),
        ):
            payload, code = herdr_jobs.start_command(
                start_args(
                    new_workspace=True,
                    cwd="/tmp",
                    task=[
                        ["config", "Review bindings."],
                        ["plugins", "Review plugins."],
                    ],
                )
            )

        self.assertEqual(code, 0)
        self.assertEqual(len(payload["launched"]), 2)
        start_calls = [
            call.args[0]
            for call in run.call_args_list
            if call.args[0][:2] == ["agent", "start"]
        ]
        self.assertEqual(len(start_calls), 2)
        self.assertTrue(
            all(call[call.index("--workspace") + 1] == "w9" for call in start_calls)
        )
        self.assertIn(mock.call(["pane", "close", "w9:p1"]), run.call_args_list)

    def test_new_workspace_only_options_require_new_workspace(self):
        with self.assertRaises(herdr_jobs.JobError) as raised:
            herdr_jobs.start_command(start_args(workspace_label="review"))
        self.assertEqual(raised.exception.code, "new_workspace_option_required")

    def test_group_token_avoids_owned_workspace_tokens(self):
        with mock.patch.object(
            herdr_jobs.secrets, "token_hex", side_effect=["a1b2", "c3d4"]
        ):
            token = herdr_jobs.choose_group_token(
                [], [herdr_jobs.Workspace("w9", owner_group="a1b2")]
            )
        self.assertEqual(token, "c3d4")


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.workspace = herdr_jobs.Workspace(
            "w9", "hj-a1b2 · review", "/tmp", "a1b2"
        )
        self.done = herdr_jobs.Job(
            "hj-a1b2-review",
            "a1b2",
            "review",
            state="done",
            workspace_id="w9",
            tab_id="w9:t2",
            pane_id="w9:p2",
        )

    def test_exclusive_owned_workspace_closes_as_one_unit(self):
        inspection = {
            "exclusive": True,
            "safe_job_tabs": {"w9:t2"},
            "reasons": [],
        }
        with (
            mock.patch.object(
                herdr_jobs, "selected_live_jobs", return_value=("a1b2", [self.done])
            ),
            mock.patch.object(
                herdr_jobs, "list_workspaces", return_value=[self.workspace]
            ),
            mock.patch.object(
                herdr_jobs, "inspect_workspace_ownership", return_value=inspection
            ),
            mock.patch.object(herdr_jobs, "run_herdr", return_value={}) as run,
        ):
            payload, code = herdr_jobs.cleanup_command(cleanup_args())

        self.assertEqual(code, 0)
        run.assert_called_once_with(["workspace", "close", "w9"])
        self.assertEqual(len(payload["closed_workspaces"]), 1)

    def test_active_job_preserves_workspace_without_force(self):
        active = herdr_jobs.Job(**{**self.done.__dict__, "state": "working"})
        inspection = {
            "exclusive": True,
            "safe_job_tabs": {"w9:t2"},
            "reasons": [],
        }
        with (
            mock.patch.object(
                herdr_jobs, "selected_live_jobs", return_value=("a1b2", [active])
            ),
            mock.patch.object(
                herdr_jobs, "list_workspaces", return_value=[self.workspace]
            ),
            mock.patch.object(
                herdr_jobs, "inspect_workspace_ownership", return_value=inspection
            ),
            mock.patch.object(herdr_jobs, "run_herdr", return_value={}) as run,
        ):
            payload, code = herdr_jobs.cleanup_command(cleanup_args())

        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertEqual(payload["preserved"][0]["reason"], "active_job")

    def test_force_closes_exclusive_workspace_with_active_job(self):
        active = herdr_jobs.Job(**{**self.done.__dict__, "state": "blocked"})
        inspection = {
            "exclusive": True,
            "safe_job_tabs": {"w9:t2"},
            "reasons": [],
        }
        with (
            mock.patch.object(
                herdr_jobs, "selected_live_jobs", return_value=("a1b2", [active])
            ),
            mock.patch.object(
                herdr_jobs, "list_workspaces", return_value=[self.workspace]
            ),
            mock.patch.object(
                herdr_jobs, "inspect_workspace_ownership", return_value=inspection
            ),
            mock.patch.object(herdr_jobs, "run_herdr", return_value={}) as run,
        ):
            herdr_jobs.cleanup_command(cleanup_args(force=True))

        run.assert_called_once_with(["workspace", "close", "w9"])

    def test_unrelated_tab_preserves_workspace_but_closes_safe_job_tab(self):
        inspection = {
            "exclusive": False,
            "safe_job_tabs": {"w9:t2"},
            "reasons": ["workspace_contains_unrelated_tabs"],
        }
        with (
            mock.patch.object(
                herdr_jobs, "selected_live_jobs", return_value=("a1b2", [self.done])
            ),
            mock.patch.object(
                herdr_jobs, "list_workspaces", return_value=[self.workspace]
            ),
            mock.patch.object(
                herdr_jobs, "inspect_workspace_ownership", return_value=inspection
            ),
            mock.patch.object(herdr_jobs, "run_herdr", return_value={}) as run,
        ):
            payload, code = herdr_jobs.cleanup_command(cleanup_args())

        self.assertEqual(code, 0)
        run.assert_called_once_with(["tab", "close", "w9:t2"])
        self.assertEqual(
            payload["preserved_workspaces"][0]["reasons"],
            ["workspace_contains_unrelated_tabs"],
        )

    def test_extra_pane_prevents_job_tab_cleanup(self):
        inspection = {
            "exclusive": False,
            "safe_job_tabs": set(),
            "reasons": ["job_tab_contains_unowned_or_missing_panes"],
        }
        with (
            mock.patch.object(
                herdr_jobs, "selected_live_jobs", return_value=("a1b2", [self.done])
            ),
            mock.patch.object(
                herdr_jobs, "list_workspaces", return_value=[self.workspace]
            ),
            mock.patch.object(
                herdr_jobs, "inspect_workspace_ownership", return_value=inspection
            ),
            mock.patch.object(herdr_jobs, "run_herdr", return_value={}) as run,
        ):
            payload, _ = herdr_jobs.cleanup_command(cleanup_args())

        run.assert_not_called()
        self.assertEqual(
            payload["preserved"][0]["reason"], "job_tab_not_exclusively_owned"
        )

    def test_live_inventory_detects_unrelated_tabs_and_extra_panes(self):
        with (
            mock.patch.object(
                herdr_jobs,
                "list_workspace_tabs",
                return_value={
                    "w9:t2": {"tab_id": "w9:t2", "label": "a1b2 · review"},
                    "w9:t3": {"tab_id": "w9:t3", "label": "manual"},
                },
            ),
            mock.patch.object(
                herdr_jobs,
                "list_workspace_panes",
                return_value={
                    "w9:t2": ["w9:p2", "w9:p3"],
                    "w9:t3": ["w9:p4"],
                },
            ),
        ):
            inspection = herdr_jobs.inspect_workspace_ownership(
                self.workspace, [self.done], "a1b2"
            )

        self.assertFalse(inspection["exclusive"])
        self.assertIn("w9:t2", inspection["contaminated_job_tabs"])
        self.assertEqual(inspection["unrelated_tabs"][0]["tab_id"], "w9:t3")


if __name__ == "__main__":
    unittest.main()
