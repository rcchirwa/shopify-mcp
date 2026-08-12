"""Documentation-hygiene guards for the accepted-risk sweep (Story 10.44 / SEC-01-06, SEC-15).

Verifies the ACs by checking file existence and content structure. The real
verification comes from the gate suite (ruff, format, mypy, coverage) + these
smoke checks.

Every path below is anchored to the repo root rather than the process working
directory (Story 10.45) — these checks used to read ``.env.example`` and
``docs/tech-debt.md`` by relative path, so they only passed when pytest happened to
be invoked from the repo root.

Usage:
  pytest tests/architecture/test_docs_hygiene.py -v
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TESTS_ROOT = _REPO_ROOT / "tests"


def test_env_example_does_not_imply_unused_receiver_secrets():
    """AC 1: .env.example no longer implies receiver-only secrets are server config."""
    env_content = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")

    # Secrets like SHOPIFY_WEBHOOK_SECRET and GA4_* should either be removed
    # or clearly marked as NOT used by this server
    if "SHOPIFY_WEBHOOK_SECRET" in env_content or "GA4_" in env_content:
        # If present, they must be in a clearly-marked "NOT used" or "receiver-only" block
        assert "NOT used" in env_content or "receiver-only" in env_content.lower(), (
            ".env.example still implies receiver-only secrets are server config. "
            "Either remove them or fence them under a clear 'NOT used by this server' block."
        )


def test_security_documentation_exists():
    """AC 2 & 4: SECURITY.md or docs/tech-debt.md documents accepted-risk decisions."""
    # Either SECURITY.md exists, or docs/tech-debt.md contains the "Accepted risks" section
    security_md_exists = (_REPO_ROOT / "SECURITY.md").is_file()
    tech_debt_content = (_REPO_ROOT / "docs" / "tech-debt.md").read_text(encoding="utf-8")

    tech_debt_has_accepted_risks = "Accepted risks" in tech_debt_content

    assert security_md_exists or tech_debt_has_accepted_risks, (
        "SECURITY.md does not exist and docs/tech-debt.md does not have an 'Accepted risks' section. "
        "At least one must document the accepted-risk decisions for SEC-01/02/05/06/15."
    )

    # If docs/tech-debt.md has Accepted risks, check for the specific items
    if tech_debt_has_accepted_risks:
        # Check for SEC-02, SEC-05, SEC-06, SEC-15 references
        assert "SEC-02" in tech_debt_content or "token fingerprint" in tech_debt_content.lower()
        assert "SEC-05" in tech_debt_content or "confirm=True" in tech_debt_content
        assert "SEC-06" in tech_debt_content or "Admin token" in tech_debt_content
        assert "SEC-15" in tech_debt_content or "rate limit" in tech_debt_content.lower()


def test_no_test_references_removed_env_keys():
    """AC 3: No test references receiver-only env keys (SHOPIFY_WEBHOOK_SECRET, GA4_*).

    Sweeps the whole ``tests/`` tree, live runners included. This used to glob
    the repo root for the retired filename convention; scanning by directory
    means a test added anywhere under ``tests/`` is covered without this list
    being touched again (Story 10.45).

    One coupling to know about: ``tests/live/`` is now in scope, so a future
    live runner that legitimately needs ``SHOPIFY_WEBHOOK_SECRET`` to exercise
    HMAC verification would trip this documentation-hygiene guard. If that
    happens the fix is a carve-out for ``tests/live/``, not deleting the test
    the failure message points at.
    """
    this_file = Path(__file__).resolve()
    test_files = [p for p in sorted(_TESTS_ROOT.rglob("test_*.py")) if p.resolve() != this_file]
    assert test_files, "No other test files found to check"

    removed_keys = ["SHOPIFY_WEBHOOK_SECRET", "GA4_MEASUREMENT_ID", "GA4_API_SECRET"]

    for test_file in test_files:
        content = test_file.read_text(encoding="utf-8")
        for key in removed_keys:
            assert key not in content, (
                f"Test file {test_file.relative_to(_REPO_ROOT)} still references {key}, "
                f"which is receiver-only. Remove or update the test."
            )


def test_no_test_registers_a_webhook_against_a_third_party_host():
    """AC 1 (SEC-16 / Story 10.49): no test in the repo points a live webhook
    registration at a third-party request-bin host."""
    # Split so this file's own failure message (which names the banned host)
    # doesn't trip the very check it's part of.
    banned_host = "http" + "bin"
    this_file = Path(__file__).resolve()
    test_files = [p for p in sorted(_TESTS_ROOT.rglob("*.py")) if p.resolve() != this_file]
    assert test_files, "No test files found to check"

    for test_file in test_files:
        content = test_file.read_text(encoding="utf-8")
        assert banned_host not in content.lower(), (
            f"Test file {test_file.relative_to(_REPO_ROOT)} still references a third-party "
            f"request-bin host — SEC-16 requires the live webhook runner to register "
            f"against a project-controlled endpoint instead."
        )


def test_live_webhook_opt_in_var_documented_as_test_only():
    """AC 5 (SEC-16 / Story 10.49): SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST is
    documented in .env.example (fenced as a test-only knob, not server config)
    and covered in the README's live-runner section."""
    env_content = (_REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST" in env_content, (
        "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST is not documented in .env.example"
    )
    # Tied to the file's actual "# ===...===" section-header convention (the
    # same style used for the pre-existing "NOT USED BY THIS SERVER" block)
    # rather than an arbitrary character window, so reformatting within the
    # fenced section can't silently defeat this check.
    idx = env_content.index("SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST")
    fence = "# " + "=" * 10
    fence_idx = env_content.rfind(fence, 0, idx)
    assert fence_idx != -1, (
        "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST must sit inside a '# ===...' "
        "fenced section header in .env.example."
    )
    section = env_content[fence_idx:idx]
    assert "test-only" in section.lower() or "test only" in section.lower(), (
        "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST must be fenced as a test-only knob "
        "in .env.example, not presented as ordinary server config."
    )

    readme_content = (_REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST" in readme_content, (
        "SHOPIFY_MCP_ALLOW_LIVE_WEBHOOK_TEST is not documented in README.md"
    )


def test_tech_debt_records_sec_16_closed():
    """AC 5 (SEC-16 / Story 10.49): docs/tech-debt.md records SEC-16 as closed."""
    tech_debt_content = (_REPO_ROOT / "docs" / "tech-debt.md").read_text(encoding="utf-8")
    assert "SEC-16" in tech_debt_content
