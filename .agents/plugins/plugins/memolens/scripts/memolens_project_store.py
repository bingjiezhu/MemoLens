"""Single-snapshot, read-only access to creative project resume facts.

This module intentionally returns private raw rows.  It owns relation selection
and head selection, while :mod:`memolens_project_resume` owns every field that is
allowed to cross the Agent boundary.
"""

from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from memolens_contracts import MemoLensError, encode_cursor
from memolens_sqlite import ReadOnlyDatabase
from memolens_timeline_store import TimelineIndexReader


_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_PROJECT_STATUSES = {"current", "draft", "active", "archived", "all"}
_PROJECT_COLUMNS = {"id", "title", "status", "created_at", "updated_at"}
_BRIEF_COLUMNS = {
    "project_id",
    "revision",
    "brief_json",
    "content_sha256",
    "provenance_json",
    "created_at",
}
@dataclass(frozen=True)
class TimelineRelation:
    """One fixed, allowlisted persisted Timeline relation."""

    table: str
    timeline_id_column: str
    columns: frozenset[str]

    @property
    def mode(self) -> str:
        return "canonical" if self.table == "timelines" else "legacy_compatibility"


def validate_project_identifier(value: Any, *, field: str = "project_id") -> str:
    """Accept only opaque MemoLens identifiers, never paths, URLs, or SQL text."""

    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise MemoLensError(f"{field} is invalid.", code="invalid_argument")
    return value


class ProjectIndexReader:
    """Read project, brief, and Timeline facts from exactly one snapshot per call."""

    def __init__(self, database: ReadOnlyDatabase) -> None:
        self.database = database
        self.timelines = TimelineIndexReader(database)

    def _project_schema_available(self, connection: sqlite3.Connection) -> bool:
        return _PROJECT_COLUMNS <= self.database.columns(
            connection, "creative_projects"
        )

    def exists_in_connection(
        self, connection: sqlite3.Connection, project_id: str
    ) -> bool:
        """Resolve one opaque project ID inside a caller-owned snapshot."""

        normalized = validate_project_identifier(project_id)
        if not self._project_schema_available(connection):
            return False
        try:
            return (
                connection.execute(
                    "SELECT 1 FROM creative_projects WHERE id=? LIMIT 1",
                    (normalized,),
                ).fetchone()
                is not None
            )
        except sqlite3.Error as exc:
            raise MemoLensError(
                "Project reference availability could not be read.",
                code="database_unavailable",
            ) from exc

    def brief_revision_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        revision: int,
    ) -> dict[str, Any] | None:
        """Read one exact legacy brief ref without selecting a fallback revision."""

        normalized = validate_project_identifier(project_id)
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise MemoLensError("revision is invalid.", code="invalid_argument")
        if not self._brief_schema_available(connection):
            return None
        try:
            row = connection.execute(
                "SELECT project_id,revision,brief_json,content_sha256,created_at "
                "FROM creative_briefs WHERE project_id=? AND revision=? LIMIT 1",
                (normalized, revision),
            ).fetchone()
            return dict(row) if row is not None else None
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The exact creative brief revision could not be read.",
                code="database_unavailable",
            ) from exc

    def timeline_revision_in_connection(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        timeline_id: str,
        revision: int,
    ) -> dict[str, Any] | None:
        """Read one exact persisted Timeline ref in the established relation."""

        normalized_project = validate_project_identifier(project_id)
        normalized_timeline = validate_project_identifier(
            timeline_id, field="timeline_id"
        )
        if type(revision) is not int or not 1 <= revision <= 1_000_000:
            raise MemoLensError("revision is invalid.", code="invalid_argument")
        relation = self._timeline_relation(connection)
        if relation is None:
            return None
        try:
            row = connection.execute(
                f"SELECT {self._timeline_select(relation)} FROM {relation.table} t "
                f"WHERE t.project_id=? AND t.{relation.timeline_id_column}=? "
                "AND t.revision=? LIMIT 1",
                (normalized_project, normalized_timeline, revision),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            self._attach_brief_binding(
                connection, normalized_project, result, self.schema_capabilities(connection)
            )
            return result
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The exact Timeline revision could not be read.",
                code="database_unavailable",
            ) from exc

    def _brief_schema_available(self, connection: sqlite3.Connection) -> bool:
        return _BRIEF_COLUMNS <= self.database.columns(
            connection, "creative_briefs"
        )

    def _timeline_relation(
        self, connection: sqlite3.Connection
    ) -> TimelineRelation | None:
        # Reuse the established canonical/compatibility priority. Never merge stores.
        schema = self.timelines.schema(connection)
        if schema is None:
            return None
        table, identifier_column = schema
        columns = self.database.columns(connection, table)
        return TimelineRelation(table, identifier_column, frozenset(columns))

    def _database_identity_available(
        self, connection: sqlite3.Connection
    ) -> bool:
        columns = self.database.columns(connection, "database_meta")
        if not {"database_uuid", "schema_version"} <= columns:
            return False
        try:
            return (
                connection.execute(
                    "SELECT 1 FROM database_meta WHERE database_uuid IS NOT NULL "
                    "AND schema_version IS NOT NULL LIMIT 1"
                ).fetchone()
                is not None
            )
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The MemoLens database identity could not be inspected.",
                code="database_unavailable",
            ) from exc

    def schema_capabilities(self, connection: sqlite3.Connection) -> dict[str, Any]:
        """Describe only capabilities proven by the supplied snapshot connection."""

        project_available = self._project_schema_available(connection)
        brief_available = self._brief_schema_available(connection)
        timeline = self._timeline_relation(connection)
        project_count = 0
        if project_available:
            try:
                project_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM creative_projects"
                    ).fetchone()[0]
                )
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "Creative projects could not be counted.",
                    code="database_unavailable",
                ) from exc
        return {
            "project_list_available": project_available,
            "project_resume_available": bool(project_available and brief_available),
            "project_history_available": bool(project_available and timeline),
            "project_count": project_count,
            "brief_schema_available": brief_available,
            "timeline_schema_available": timeline is not None,
            "timeline_schema_mode": timeline.mode if timeline else "unavailable",
            "database_identity_available": self._database_identity_available(connection),
        }

    @staticmethod
    def _status(value: str) -> str:
        if value not in _PROJECT_STATUSES:
            raise MemoLensError(
                "status must be current, draft, active, archived, or all.",
                code="invalid_argument",
            )
        return value

    @staticmethod
    def _limit(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
            raise MemoLensError(
                "limit must be an integer between 1 and 100.",
                code="invalid_argument",
            )
        return value

    @staticmethod
    def _project_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "project_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def list(
        self,
        *,
        status: str,
        limit: int,
        cursor: str | None,
    ) -> dict[str, Any]:
        """Return bounded raw project summaries from one private snapshot."""

        normalized_status = self._status(status)
        normalized_limit = self._limit(limit)
        normalized_cursor = (
            validate_project_identifier(cursor, field="cursor")
            if cursor is not None
            else None
        )
        with closing(self.database.connection()) as connection:
            capabilities = self.schema_capabilities(connection)
            if not capabilities["project_list_available"]:
                return {
                    "capabilities": capabilities,
                    "status_filter": normalized_status,
                    "projects": [],
                    "next_cursor": None,
                }
            where: list[str] = []
            parameters: list[Any] = []
            if normalized_status == "current":
                # The safe default view combines draft and active work in progress.
                where.append("status IN ('draft','active')")
            elif normalized_status != "all":
                where.append("status = ?")
                parameters.append(normalized_status)
            if normalized_cursor is not None:
                where.append("id > ?")
                parameters.append(normalized_cursor)
            where_sql = f"WHERE {' AND '.join(where)}" if where else ""
            try:
                rows = connection.execute(
                    "SELECT id,title,status,created_at,updated_at "
                    f"FROM creative_projects {where_sql} ORDER BY id ASC LIMIT ?",
                    [*parameters, normalized_limit + 1],
                ).fetchall()
                selected = rows[:normalized_limit]
                projects = [self._project_row(row) for row in selected]
                self._attach_project_counts(connection, projects, capabilities)
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "Creative projects could not be listed.",
                    code="database_unavailable",
                ) from exc
        has_more = len(rows) > normalized_limit
        return {
            "capabilities": capabilities,
            "status_filter": normalized_status,
            "projects": projects,
            "next_cursor": (
                encode_cursor(str(selected[-1]["id"]))
                if has_more and selected
                else None
            ),
        }

    def _attach_project_counts(
        self,
        connection: sqlite3.Connection,
        projects: list[dict[str, Any]],
        capabilities: dict[str, Any],
    ) -> None:
        if not projects:
            return
        identifiers = [project["project_id"] for project in projects]
        placeholders = ",".join("?" for _item in identifiers)
        if capabilities["brief_schema_available"]:
            rows = connection.execute(
                "SELECT project_id,MAX(revision) AS brief_revision,"
                "COUNT(*) AS brief_revision_count FROM creative_briefs "
                f"WHERE project_id IN ({placeholders}) GROUP BY project_id",
                identifiers,
            ).fetchall()
            briefs = {row["project_id"]: dict(row) for row in rows}
            for project in projects:
                brief = briefs.get(project["project_id"])
                project["brief_revision"] = (
                    int(brief["brief_revision"]) if brief is not None else None
                )
                project["brief_revision_count"] = (
                    int(brief["brief_revision_count"]) if brief is not None else 0
                )
        else:
            for project in projects:
                project["brief_revision"] = None
                project["brief_revision_count"] = 0

        relation = self._timeline_relation(connection)
        if relation is None:
            for project in projects:
                project["timeline_branch_count"] = 0
                project["timeline_revision_count"] = 0
            return
        rows = connection.execute(
            f"SELECT project_id,COUNT(DISTINCT {relation.timeline_id_column}) "
            "AS timeline_branch_count,COUNT(*) AS timeline_revision_count "
            f"FROM {relation.table} WHERE project_id IN ({placeholders}) "
            "GROUP BY project_id",
            identifiers,
        ).fetchall()
        timelines = {row["project_id"]: dict(row) for row in rows}
        for project in projects:
            timeline = timelines.get(project["project_id"])
            project["timeline_branch_count"] = (
                int(timeline["timeline_branch_count"])
                if timeline is not None
                else 0
            )
            project["timeline_revision_count"] = (
                int(timeline["timeline_revision_count"])
                if timeline is not None
                else 0
            )

    def open(self, project_id: str) -> dict[str, Any]:
        """Read one project Resume source set without leaving its snapshot."""

        normalized_id = validate_project_identifier(project_id)
        with closing(self.database.connection()) as connection:
            return self.open_in_connection(connection, normalized_id)

    def open_in_connection(
        self, connection: sqlite3.Connection, project_id: str
    ) -> dict[str, Any]:
        """Read one project source set through a caller-owned snapshot.

        Blueprint validation uses this entry point so project, baseline, and
        evidence checks cannot drift across separate SQLite snapshots.
        """

        normalized_id = validate_project_identifier(project_id)
        capabilities = self.schema_capabilities(connection)
        if not capabilities["project_list_available"]:
            return {
                "capabilities": capabilities,
                "project": None,
                "brief": None,
                "timeline_head": None,
                "timeline_branch_count": 0,
            }
        try:
            project_row = connection.execute(
                "SELECT id,title,status,created_at,updated_at "
                "FROM creative_projects WHERE id = ? LIMIT 1",
                (normalized_id,),
            ).fetchone()
            if project_row is None:
                raise MemoLensError(
                    "Creative project was not found.", code="project_not_found"
                )
            project = self._project_row(project_row)
            brief = self._latest_brief(connection, normalized_id, capabilities)
            relation = self._timeline_relation(connection)
            head, branch_count = self._latest_observed_head(
                connection, normalized_id, relation
            )
            if head is not None:
                self._attach_brief_binding(
                    connection, normalized_id, head, capabilities
                )
        except MemoLensError:
            raise
        except sqlite3.Error as exc:
            raise MemoLensError(
                "The creative project Resume source could not be read.",
                code="database_unavailable",
            ) from exc
        return {
            "capabilities": capabilities,
            "project": project,
            "brief": brief,
            "timeline_head": head,
            "timeline_branch_count": branch_count,
            "timeline_relation_mode": relation.mode if relation else "unavailable",
        }

    @staticmethod
    def _latest_brief(
        connection: sqlite3.Connection,
        project_id: str,
        capabilities: dict[str, Any],
    ) -> dict[str, Any] | None:
        if not capabilities["brief_schema_available"]:
            return None
        row = connection.execute(
            "SELECT project_id,revision,brief_json,content_sha256,created_at "
            "FROM creative_briefs WHERE project_id = ? "
            "ORDER BY revision DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return dict(row) if row is not None else None

    @staticmethod
    def _timeline_select(relation: TimelineRelation, *, alias: str = "t") -> str:
        columns = relation.columns
        identifier = relation.timeline_id_column

        def optional(name: str) -> str:
            return f"{alias}.{name} AS {name}" if name in columns else f"NULL AS {name}"

        def availability(name: str) -> str:
            return f"{1 if name in columns else 0} AS {name}_available"

        return ",".join(
            (
                f"{alias}.{identifier} AS timeline_id",
                f"{alias}.project_id",
                f"{alias}.revision",
                optional("parent_revision"),
                availability("parent_revision"),
                optional("brief_revision"),
                availability("brief_revision"),
                f"{alias}.schema_version",
                f"{alias}.timeline_json",
                f"{alias}.content_sha256",
                f"{alias}.provenance_json",
                f"{alias}.validation_status",
                optional("validation_errors_json"),
                availability("validation_errors_json"),
                f"{alias}.created_at",
            )
        )

    def _latest_observed_head(
        self,
        connection: sqlite3.Connection,
        project_id: str,
        relation: TimelineRelation | None,
    ) -> tuple[dict[str, Any] | None, int]:
        if relation is None:
            return None, 0
        identifier = relation.timeline_id_column
        branch_count = int(
            connection.execute(
                f"SELECT COUNT(DISTINCT {identifier}) FROM {relation.table} "
                "WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
        )
        row = connection.execute(
            "WITH branch_heads AS ("
            f"SELECT {identifier} AS timeline_id,MAX(revision) AS revision "
            f"FROM {relation.table} WHERE project_id = ? GROUP BY {identifier}"
            ") "
            f"SELECT {self._timeline_select(relation)} FROM {relation.table} t "
            "JOIN branch_heads h ON h.timeline_id = "
            f"t.{identifier} AND h.revision = t.revision "
            "WHERE t.project_id = ? "
            f"ORDER BY t.created_at DESC,t.revision DESC,t.{identifier} ASC LIMIT 1",
            (project_id, project_id),
        ).fetchone()
        return (dict(row) if row is not None else None), branch_count

    @staticmethod
    def _attach_brief_binding(
        connection: sqlite3.Connection,
        project_id: str,
        timeline: dict[str, Any],
        capabilities: dict[str, Any],
    ) -> None:
        revision = timeline.get("brief_revision")
        if (
            not capabilities["brief_schema_available"]
            or isinstance(revision, bool)
            or not isinstance(revision, int)
        ):
            timeline["brief_binding_exists"] = None
            return
        timeline["brief_binding_exists"] = (
            connection.execute(
                "SELECT 1 FROM creative_briefs WHERE project_id = ? "
                "AND revision = ? LIMIT 1",
                (project_id, revision),
            ).fetchone()
            is not None
        )

    def history(
        self,
        *,
        project_id: str,
        limit: int,
        timeline_id: str | None = None,
    ) -> dict[str, Any]:
        """Read bounded revision summaries across all or one Timeline branch."""

        normalized_project = validate_project_identifier(project_id)
        normalized_timeline = (
            validate_project_identifier(timeline_id, field="timeline_id")
            if timeline_id is not None
            else None
        )
        normalized_limit = self._limit(limit)
        with closing(self.database.connection()) as connection:
            capabilities = self.schema_capabilities(connection)
            if not capabilities["project_list_available"]:
                return {
                    "capabilities": capabilities,
                    "project": None,
                    "timeline_id": normalized_timeline,
                    "revisions": [],
                    "truncated": False,
                }
            try:
                project_row = connection.execute(
                    "SELECT id,title,status,created_at,updated_at "
                    "FROM creative_projects WHERE id = ? LIMIT 1",
                    (normalized_project,),
                ).fetchone()
                if project_row is None:
                    raise MemoLensError(
                        "Creative project was not found.", code="project_not_found"
                    )
                project = self._project_row(project_row)
                relation = self._timeline_relation(connection)
                if relation is None:
                    rows: list[sqlite3.Row] = []
                else:
                    where = "t.project_id = ?"
                    parameters: list[Any] = [normalized_project]
                    if normalized_timeline is not None:
                        where += f" AND t.{relation.timeline_id_column} = ?"
                        parameters.append(normalized_timeline)
                    rows = connection.execute(
                        f"SELECT {self._timeline_select(relation)} "
                        f"FROM {relation.table} t WHERE {where} "
                        "ORDER BY t.created_at DESC,t.revision DESC,"
                        f"t.{relation.timeline_id_column} ASC LIMIT ?",
                        [*parameters, normalized_limit + 1],
                    ).fetchall()
                selected = [dict(row) for row in rows[:normalized_limit]]
                for revision in selected:
                    self._attach_brief_binding(
                        connection, normalized_project, revision, capabilities
                    )
            except MemoLensError:
                raise
            except sqlite3.Error as exc:
                raise MemoLensError(
                    "Creative project history could not be read.",
                    code="database_unavailable",
                ) from exc
        return {
            "capabilities": capabilities,
            "project": project,
            "timeline_id": normalized_timeline,
            "revisions": selected,
            "truncated": len(rows) > normalized_limit,
            "timeline_relation_mode": relation.mode if relation else "unavailable",
        }

    # Raw names make the trust boundary explicit at Store integration sites.
    def list_raw(
        self, *, status: str, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        return self.list(status=status, limit=limit, cursor=cursor)

    def open_raw(self, project_id: str) -> dict[str, Any]:
        return self.open(project_id)

    def history_raw(self, project_id: str, *, limit: int) -> dict[str, Any]:
        return self.history(project_id=project_id, limit=limit)


__all__ = [
    "ProjectIndexReader",
    "TimelineRelation",
    "validate_project_identifier",
]
