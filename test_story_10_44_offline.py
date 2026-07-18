"""Smoke test for Story 10.44 - Accepted-risk and documentation sweep (SEC-01-06, SEC-15).

Verifies ACs by checking file existence and content structure. The real verification
comes from the gate suite (ruff, format, mypy, coverage) + this smoke test.
"""

import glob
import os


def test_env_example_does_not_imply_unused_receiver_secrets():
    """AC 1: .env.example no longer implies receiver-only secrets are server config."""
    with open(".env.example") as f:
        env_content = f.read()

    # Secrets like SHOPIFY_WEBHOOK_SECRET and GA4_* should either be removed
    # or clearly marked as NOT used by this server
    if "SHOPIFY_WEBHOOK_SECRET" in env_content or "GA4_" in env_content:
        # If present, they must be in a clearly-marked "NOT used" or "receiver-only" block
        assert "NOT used" in env_content or "receiver-only" in env_content.lower(), (
            ".env.example still implies receiver-only secrets are server config. "
            "Either remove them or fence them under a clear 'NOT used by this server' block."
        )


def test_security_documentation_exists():
    """AC 2 & 4: SECURITY.md or TECH_DEBT.md documents accepted-risk decisions."""
    # Either SECURITY.md exists, or TECH_DEBT.md contains the "Accepted risks" section
    security_md_exists = os.path.isfile("SECURITY.md")
    with open("TECH_DEBT.md") as f:
        tech_debt_content = f.read()

    tech_debt_has_accepted_risks = "Accepted risks" in tech_debt_content

    assert security_md_exists or tech_debt_has_accepted_risks, (
        "SECURITY.md does not exist and TECH_DEBT.md does not have an 'Accepted risks' section. "
        "At least one must document the accepted-risk decisions for SEC-01/02/05/06/15."
    )

    # If TECH_DEBT.md has Accepted risks, check for the specific items
    if tech_debt_has_accepted_risks:
        # Check for SEC-02, SEC-05, SEC-06, SEC-15 references
        assert "SEC-02" in tech_debt_content or "token fingerprint" in tech_debt_content.lower()
        assert "SEC-05" in tech_debt_content or "confirm=True" in tech_debt_content
        assert "SEC-06" in tech_debt_content or "Admin token" in tech_debt_content
        assert "SEC-15" in tech_debt_content or "rate limit" in tech_debt_content.lower()


def test_no_test_references_removed_env_keys():
    """AC 3: No test references receiver-only env keys (SHOPIFY_WEBHOOK_SECRET, GA4_*)."""
    # Search all test files for references to receiver-only keys, excluding this test
    test_files = glob.glob("test_*_offline.py")
    # Exclude this test file itself from the check
    test_files = [f for f in test_files if f != "test_story_10_44_offline.py"]
    assert test_files, "No other test files found to check"

    removed_keys = ["SHOPIFY_WEBHOOK_SECRET", "GA4_MEASUREMENT_ID", "GA4_API_SECRET"]

    for test_file in test_files:
        with open(test_file) as f:
            content = f.read()
            for key in removed_keys:
                assert key not in content, (
                    f"Test file {test_file} still references {key}, which is receiver-only. "
                    f"Remove or update the test."
                )
