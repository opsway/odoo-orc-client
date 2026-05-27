"""Pure-function tests — no DB or network. Inherit from BaseCase so
they don't pay the TransactionCase setup cost."""
import os
import shutil
import subprocess
import tempfile

from odoo.tests import tagged
from odoo.tests.common import BaseCase

from odoo.addons.orc_client_build_reporter.models.build_reporter import (
    _GH_URL_RE,
    get_build_id,
    get_commit_sha,
    get_repo_from_git,
    get_stage,
    parse_dev_url,
)


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestGetBuildId(BaseCase):

    def test_pg_group_stage(self):
        self.assertEqual(get_build_id("pg-group-stage-25407779"), "25407779")

    def test_opsway_stage(self):
        self.assertEqual(get_build_id("opsway-stage-30699587"), "30699587")

    def test_feature_branch(self):
        self.assertEqual(
            get_build_id("pg-group-feature-pg-310-ai-31359772"),
            "31359772",
        )

    def test_local_dbname(self):
        self.assertIsNone(get_build_id("odoo"))

    def test_no_trailing_digits(self):
        self.assertIsNone(get_build_id("repo-without-trailing-digits"))


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestParseDevUrl(BaseCase):

    def test_canonical_dev_url(self):
        self.assertEqual(
            parse_dev_url("https://acme-32258372.dev.odoo.com"),
            ("acme", "32258372"),
        )

    def test_dev_url_with_trailing_slash(self):
        self.assertEqual(
            parse_dev_url("https://acme-32258372.dev.odoo.com/"),
            ("acme", "32258372"),
        )

    def test_dev_url_with_multi_dash_slug(self):
        self.assertEqual(
            parse_dev_url(
                "https://pg-group-feature-pg-460-ai-32258372.dev.odoo.com",
            ),
            ("pg-group-feature-pg-460-ai", "32258372"),
        )

    def test_rejects_staging_host(self):
        self.assertIsNone(parse_dev_url("https://acme.odoo.com"))

    def test_rejects_arbitrary_host(self):
        self.assertIsNone(parse_dev_url("https://evil.attacker.com"))

    def test_rejects_no_digits(self):
        self.assertIsNone(parse_dev_url("https://acme.dev.odoo.com"))


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestGetStage(BaseCase):

    def setUp(self):
        super().setUp()
        self._saved_stage = os.environ.pop("ODOO_STAGE", None)

    def tearDown(self):
        if self._saved_stage is None:
            os.environ.pop("ODOO_STAGE", None)
        else:
            os.environ["ODOO_STAGE"] = self._saved_stage
        super().tearDown()

    def test_dev_explicit(self):
        os.environ["ODOO_STAGE"] = "dev"
        self.assertEqual(get_stage(), "dev")

    def test_staging(self):
        os.environ["ODOO_STAGE"] = "staging"
        self.assertEqual(get_stage(), "staging")

    def test_production(self):
        os.environ["ODOO_STAGE"] = "production"
        self.assertEqual(get_stage(), "production")

    def test_unknown_falls_back_to_dev(self):
        os.environ["ODOO_STAGE"] = "weird"
        self.assertEqual(get_stage(), "dev")

    def test_unset_falls_back_to_dev(self):
        self.assertEqual(get_stage(), "dev")


# ---------------------------------------------------------------------------
# git helpers — spawn a real temp repo so we exercise the actual call,
# not a mock of it. Still no DB needed.
# ---------------------------------------------------------------------------


def _git(args, cwd=None):
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.check_call(
        ["git", *args],
        cwd=cwd, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


def _make_repo(origin_url=None):
    d = tempfile.mkdtemp(prefix="orc_br_test_")
    _git(["init", "-q", d])
    if origin_url:
        _git(["-C", d, "remote", "add", "origin", origin_url])
    with open(os.path.join(d, "f"), "w") as fp:
        fp.write("x")
    _git(["-C", d, "add", "f"])
    _git(["-C", d, "commit", "-q", "-m", "init"])
    return d


class _TempReposMixin:
    def setUp(self):
        super().setUp()
        self._cleanup = []

    def tearDown(self):
        for p in self._cleanup:
            shutil.rmtree(p, ignore_errors=True)
        super().tearDown()

    def _repo(self, origin_url=None):
        p = _make_repo(origin_url=origin_url)
        self._cleanup.append(p)
        return p

    def _empty_dir(self):
        d = tempfile.mkdtemp(prefix="orc_br_empty_")
        self._cleanup.append(d)
        return d


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestGetCommitSha(_TempReposMixin, BaseCase):

    def test_returns_40_char_hex(self):
        sha = get_commit_sha(self._repo())
        self.assertIsNotNone(sha)
        self.assertEqual(len(sha), 40)
        self.assertTrue(all(c in "0123456789abcdef" for c in sha))

    def test_not_a_git_dir_returns_none(self):
        self.assertIsNone(get_commit_sha(self._empty_dir()))


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestGitHubUrlRegex(BaseCase):
    """The body's `repo` field is parsed by `_GH_URL_RE`. We accept
    SSH and HTTPS shapes, with or without the `.git` suffix."""

    def _match(self, url):
        m = _GH_URL_RE.search(url)
        self.assertIsNotNone(m, f"regex did not match {url!r}")
        return f"{m.group(1)}/{m.group(2)}"

    def test_ssh_url(self):
        self.assertEqual(
            self._match("git@github.com:opsway/pg_group.git"),
            "opsway/pg_group",
        )

    def test_https_with_dot_git(self):
        self.assertEqual(
            self._match("https://github.com/opsway/pg_group.git"),
            "opsway/pg_group",
        )

    def test_https_trailing_slash(self):
        self.assertEqual(
            self._match("https://github.com/opsway/pg_group/"),
            "opsway/pg_group",
        )


@tagged('post_install', '-at_install', 'orc_client_build_reporter')
class TestGetRepoFromGit(_TempReposMixin, BaseCase):

    def test_https_dot_git(self):
        self.assertEqual(
            get_repo_from_git(
                self._repo("https://github.com/opsway/pg_group.git"),
            ),
            "opsway/pg_group",
        )

    def test_ssh(self):
        self.assertEqual(
            get_repo_from_git(
                self._repo("git@github.com:opsway/pg_group.git"),
            ),
            "opsway/pg_group",
        )

    def test_non_github_origin_returns_none(self):
        self.assertIsNone(
            get_repo_from_git(
                self._repo("https://gitlab.com/foo/bar.git"),
            ),
        )

    def test_no_origin_returns_none(self):
        self.assertIsNone(get_repo_from_git(self._repo()))

    def test_not_a_git_dir_returns_none(self):
        self.assertIsNone(get_repo_from_git(self._empty_dir()))
