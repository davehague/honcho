"""One-time upload of local memory files (MEMORY.md / USER.md / SOUL.md) into Honcho."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from utils import atomic_json_write, read_json_or_empty

from .session_auth import HonchoAuthError

logger = logging.getLogger("plugins.memory.honcho.session")

# (filename, upload name, description, peer kind) — peer kind picks user vs assistant peer.
_MEMORY_FILES = (
    ("MEMORY.md", "consolidated_memory.md", "Long-term agent notes and preferences", "user"),
    ("USER.md", "user_profile.md", "User profile and preferences", "user"),
    ("SOUL.md", "agent_soul.md", "Agent persona and identity configuration", "ai"),
)

# Lives beside the files it describes: {"<backend>/<workspace>/<owner peer>": ISO upload time}.
# A gateway session key changes per Slack DM thread, so "the Honcho session is empty" is true on
# every new top-level DM; without this record each one re-uploaded the files (and Honcho re-derived
# their now-stale facts as fresh conclusions).
_MIGRATED_MARKER = ".honcho_migrated.json"


class SessionMigrationMixin:
    def migrate_memory_files(self, session_key: str, memory_dir: str, *, force: bool = False) -> bool:
        """Upload MEMORY.md / USER.md / SOUL.md to Honcho when it activates on an instance with
        locally consolidated memory; skips missing/empty files. True if at least one uploaded.
        Runs once per backend+workspace+owner unless ``force`` (the explicit ``hermes honcho migrate``)."""
        memory_path = Path(memory_dir)
        if not memory_path.exists():
            return False

        session = self._cached_session(session_key)
        if not session:
            logger.warning("No local session cached for '%s', skipping memory migration", session_key)
            return False
        if session.honcho_session_id not in self._sessions_cache:
            logger.warning("No Honcho session cached for '%s', skipping memory migration", session_key)
            return False

        # Owner-scoped: these files describe the install owner; uploading them under another
        # human's peer would make Honcho attribute the owner's facts to that person. The owner is
        # the CONFIG peerName, never a re-resolution of the session's own peer (that would compare
        # the triggering user to themselves). No declared owner: nobody can be proven to be the owner.
        owner_peer_id = self._declared_owner_peer_id()
        if owner_peer_id is None or session.user_peer_id != owner_peer_id:
            logger.info("Skipping memory-file migration: session user peer '%s' is not the declared owner (peerName=%s)",
                        session.user_peer_id, owner_peer_id or "unset")
            return False

        marker_path = memory_path / _MIGRATED_MARKER
        marker_key = "/".join((self._cfg("base_url") or self._cfg("environment") or "production",
                               self._cfg("workspace_id") or "hermes", owner_peer_id))
        migrated = read_json_or_empty(marker_path)
        if marker_key in migrated and not force:
            logger.debug("Skipping memory-file migration: already uploaded to %s at %s", marker_key, migrated[marker_key])
            return False

        uploaded = False
        for filename, upload_name, description, target_kind in _MEMORY_FILES:
            filepath = memory_path / filename
            content = filepath.read_text(encoding="utf-8").strip() if filepath.exists() else ""
            if not content:
                continue
            target_peer_id = session.user_peer_id if target_kind == "user" else session.assistant_peer_id
            wrapped = ("<prior_memory_file>\n<context>\n"
                       "This file was consolidated from local conversations BEFORE Honcho was activated.\n"
                       f"{description}. Treat as foundational context for this user.\n"
                       f"</context>\n\n{content}\n</prior_memory_file>\n")

            def _upload() -> None:
                self._sdk_session(session.honcho_session_id).upload_file(
                    file=(upload_name, wrapped.encode("utf-8"), "text/plain"),
                    peer=self._get_or_create_peer(target_peer_id),
                    metadata={"source": "local_memory", "original_file": filename, "target_peer": target_kind},
                )

            try:
                self._authed_call("memory migration upload", _upload)
                logger.info("Uploaded %s to Honcho for %s (%s peer)", filename, session_key, target_kind)
                uploaded = True
            except HonchoAuthError:
                logger.error("Honcho memory migration stopped after %s: auth failed", filename)
                break
            except Exception as e:
                logger.error("Failed to upload %s to Honcho: %s", filename, e)

        if uploaded:
            atomic_json_write(marker_path, {**migrated, marker_key: datetime.now(timezone.utc).isoformat()})
        return uploaded
