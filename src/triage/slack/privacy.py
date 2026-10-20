"""Content fingerprinting and privacy helpers.

**Why HMAC rather than SHA-256.** A plain hash of Slack messages is not privacy.
The space of real messages is small and highly predictable: ``sha256("lgtm")``,
``sha256("deploying now")``, and ``sha256("standup in 5")`` are one precomputed
table away from plaintext. Anyone who obtains a database dump could recover a
large fraction of the corpus by hashing a wordlist.

HMAC with a key held outside the database removes that: fingerprints stay useful
for within-workspace deduplication and are meaningless without the key.

**Per-workspace key derivation.** Each workspace gets a distinct derived key, so
identical text in two workspaces produces different fingerprints. Without this,
an attacker with one workspace's plaintext could confirm the same message
appeared in another — a cross-tenant leak through a column that looks harmless.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import unicodedata

# Normalization patterns, applied before hashing so trivially different messages
# fingerprint identically. Deliberately conservative: over-normalizing collapses
# genuinely distinct messages into one bucket.
_WHITESPACE_RE = re.compile(r"\s+")
_SLACK_ENTITY_RE = re.compile(r"<[@#!][^>]+>")  # <@U123>, <#C456|general>, <!here>
_URL_RE = re.compile(r"https?://\S+")


def normalize_for_fingerprint(text: str) -> str:
    """Canonicalize message text prior to fingerprinting.

    Applies: Unicode NFKC, lowercasing, entity and URL placeholders, and
    whitespace collapsing. The goal is that "the DB is down!!" and "the db is
    down!" produce the same fingerprint, so repeated incident reports cluster.

    Args:
        text: Raw Slack message text.

    Returns:
        The normalized string.
    """
    normalized = unicodedata.normalize("NFKC", text)
    normalized = normalized.lower()
    # Placeholders rather than deletion: "<@U1> can you look?" and "can you
    # look?" are different messages and should not collide.
    normalized = _SLACK_ENTITY_RE.sub("<entity>", normalized)
    normalized = _URL_RE.sub("<url>", normalized)
    normalized = _WHITESPACE_RE.sub(" ", normalized).strip()
    return normalized


def derive_workspace_key(master_key: str, slack_team_id: str, generation: str) -> bytes:
    """Derive a per-workspace HMAC key from the master key.

    Uses HKDF-Expand (RFC 5869) with the team ID and key generation as info, so:

    * Two workspaces never share a key.
    * Rotating ``generation`` produces an entirely new key set, and
      ``workspaces.content_hmac_key_id`` records which generation produced any
      given fingerprint.
    * The master key is never used directly, limiting its exposure.

    Args:
        master_key: ``CONTENT_HMAC_MASTER_KEY`` from the environment.
        slack_team_id: The Slack team ID, e.g. ``T01ABCDEF``.
        generation: Key generation label, e.g. ``"v1"``.

    Returns:
        A 32-byte derived key.
    """
    return hashlib.pbkdf2_hmac(
        "sha256",
        master_key.encode("utf-8"),
        f"{generation}:{slack_team_id}".encode("utf-8"),
        # Fingerprinting runs on every message, so this is a deliberate
        # trade of iteration count for throughput. It is acceptable because the
        # input is a high-entropy random master key, not a human password —
        # brute-forcing it is infeasible regardless of iteration count.
        iterations=10_000,
        dklen=32,
    )


def fingerprint(text: str, workspace_key: bytes) -> bytes:
    """Compute the HMAC-SHA256 fingerprint stored on ``message_events``.

    Args:
        text: Raw message text; normalized internally.
        workspace_key: Key from :func:`derive_workspace_key`.

    Returns:
        A 32-byte digest, matching the ``length(content_fingerprint) = 32``
        CHECK constraint on the table.
    """
    normalized = normalize_for_fingerprint(text)
    return hmac.new(workspace_key, normalized.encode("utf-8"), hashlib.sha256).digest()


def key_id(generation: str, slack_team_id: str) -> str:
    """Build the ``content_hmac_key_id`` recorded on the workspace row.

    An identifier only — it names which key generation was used so fingerprints
    remain interpretable after a rotation. The key material never touches the
    database.
    """
    suffix = hashlib.sha256(slack_team_id.encode("utf-8")).hexdigest()[:8]
    return f"{generation}:{suffix}"
