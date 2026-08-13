from __future__ import annotations

from collections import defaultdict

from core.media_db import MediaRepository, MediaRevisionConflictError, canonical_json


PROFILE_ID = "default"
PROFILE_FIELDS = (
    "platform",
    "audience",
    "duration_ms",
    "aspect_ratio",
    "tone",
    "pace",
    "narrative_arc",
    "must_include",
    "must_exclude",
)
PROFILE_SOURCES = frozenset({"user_edit", "confirmed_suggestion", "reset"})
ASPECT_RATIOS = frozenset({"16:9", "9:16", "1:1", "4:5"})


class ProfileRevisionConflictError(RuntimeError):
    def __init__(self, current_profile: dict[str, object]):
        super().__init__("The creator profile changed after this screen was loaded.")
        self.current_profile = current_profile


class CreatorMemoryService:
    """Transparent, revisioned creator preferences scoped to one media database."""

    def __init__(self, repository: MediaRepository):
        self.repository = repository

    def current_profile(self, profile_id: str = PROFILE_ID) -> dict[str, object]:
        return self.profile_revision(profile_id=profile_id)

    def profile_revision(
        self,
        *,
        profile_id: str = PROFILE_ID,
        revision: int | None = None,
    ) -> dict[str, object]:
        self._validate_profile_id(profile_id)
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise ValueError("Creator profile revision must be a positive integer.")
        stored = self.repository.get_creator_profile(profile_id=profile_id, revision=revision)
        if stored is None:
            if revision is not None:
                raise LookupError("Creator profile revision does not exist.")
            return self._empty_profile(profile_id)
        return stored

    def update_profile(
        self,
        payload: dict[str, object],
        *,
        idempotency_scope: str,
        idempotency_key: str,
        request_sha256: str,
    ) -> tuple[dict[str, object], bool]:
        allowed = {"profile_id", "base_revision", "profile", "evidence", "source", "db_path"}
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise ValueError(f"Unsupported creator profile fields: {', '.join(unknown)}.")
        profile_id = payload.get("profile_id", PROFILE_ID)
        if not isinstance(profile_id, str):
            raise ValueError("`profile_id` must be `default`.")
        self._validate_profile_id(profile_id)
        base_revision = payload.get("base_revision")
        if isinstance(base_revision, bool) or not isinstance(base_revision, int) or base_revision < 0:
            raise ValueError("`base_revision` must be a non-negative integer.")
        raw_profile = payload.get("profile")
        if not isinstance(raw_profile, dict):
            raise ValueError("`profile` must be a JSON object containing the full snapshot.")
        source = payload.get("source", "user_edit")
        if not isinstance(source, str) or source not in PROFILE_SOURCES:
            raise ValueError("`source` must be user_edit, confirmed_suggestion, or reset.")
        if source == "reset" and raw_profile:
            raise ValueError("A reset profile must be an empty object.")
        profile = self.validate_profile(raw_profile)
        evidence = self._validate_evidence(payload.get("evidence", []))
        replay = self.repository.replay_idempotent_write(
            scope=str(idempotency_scope),
            key=str(idempotency_key),
            request_sha256=str(request_sha256),
        )
        if replay is not None:
            return replay.response, True
        evidence = self._validated_provenance(
            profile=profile,
            source=source,
            evidence=evidence,
        )
        try:
            return self.repository.put_creator_profile(
                profile_id=profile_id,
                base_revision=base_revision,
                profile=profile,
                evidence=evidence,
                source=source,
                idempotency_scope=str(idempotency_scope),
                idempotency_key=str(idempotency_key),
                request_sha256=str(request_sha256),
            )
        except MediaRevisionConflictError as exc:
            raise ProfileRevisionConflictError(exc.current) from exc

    def _validated_provenance(
        self,
        *,
        profile: dict[str, object],
        source: str,
        evidence: list[dict[str, object]],
    ) -> list[dict[str, object]]:
        if source != "confirmed_suggestion":
            if evidence:
                raise ValueError("Only confirmed suggestions may retain project evidence.")
            return []

        current = self.current_profile()["profile"]
        current_profile = current if isinstance(current, dict) else {}
        changed_fields = [
            field
            for field in PROFILE_FIELDS
            if current_profile.get(field) != profile.get(field)
            or (field in current_profile) != (field in profile)
        ]
        if len(changed_fields) != 1:
            raise ValueError("A confirmed suggestion must change exactly one creator preference.")

        field = changed_fields[0]
        matching = next(
            (
                suggestion
                for suggestion in self.suggestions()
                if suggestion["field"] == field and suggestion["value"] == profile.get(field)
            ),
            None,
        )
        if matching is None:
            raise ValueError("The confirmed preference is not a current Creator Memory suggestion.")

        expected = matching["evidence"]
        expected_keys = {
            (str(item["project_id"]), int(item["brief_revision"]))
            for item in expected
        }
        supplied_keys = {
            (str(item["project_id"]), int(item["brief_revision"]))
            for item in evidence
        }
        if supplied_keys != expected_keys:
            raise ValueError("Confirmed suggestion evidence does not match the current projects.")
        return [dict(item) for item in expected]

    def suggestions(self) -> list[dict[str, object]]:
        """Derive read-only candidates from the latest immutable brief in each project."""
        rows = self.repository.latest_creative_briefs()
        observations: dict[tuple[str, str], dict[str, object]] = {}
        evidence_by_value: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
        for row in rows:
            brief = row["brief"] if isinstance(row.get("brief"), dict) else {}
            raw_applied_fields = brief.get("applied_profile_fields")
            applied_profile_fields = {
                field
                for field in raw_applied_fields
                if isinstance(field, str)
            } if isinstance(raw_applied_fields, list) else set()
            for field in PROFILE_FIELDS:
                if field not in brief or field in applied_profile_fields:
                    continue
                value = brief[field]
                try:
                    validated = self.validate_profile({field: value})[field]
                except (KeyError, ValueError):
                    continue
                key = (field, canonical_json(validated))
                observations[key] = {"field": field, "value": validated}
                evidence_by_value[key].append(
                    {
                        "project_id": str(row["project_id"]),
                        "brief_revision": int(row["brief_revision"]),
                    }
                )

        current = self.current_profile()["profile"]
        suggestions: list[dict[str, object]] = []
        for key, evidence in evidence_by_value.items():
            project_ids = {str(item["project_id"]) for item in evidence}
            if len(project_ids) < 2:
                continue
            observation = observations[key]
            field = str(observation["field"])
            value = observation["value"]
            if isinstance(current, dict) and field in current and current[field] == value:
                continue
            suggestions.append(
                {
                    "field": field,
                    "value": value,
                    "evidence_count": len(project_ids),
                    "evidence": evidence,
                }
            )
        suggestions.sort(
            key=lambda item: (
                -int(item["evidence_count"]),
                str(item["field"]),
                canonical_json(item["value"]),
            )
        )
        return suggestions

    @classmethod
    def validate_profile(cls, raw_profile: dict[str, object]) -> dict[str, object]:
        unknown = sorted(set(raw_profile) - set(PROFILE_FIELDS))
        if unknown:
            raise ValueError(f"Unsupported creator preference fields: {', '.join(unknown)}.")
        profile: dict[str, object] = {}
        for field in ("platform", "audience", "tone", "pace"):
            if field in raw_profile:
                text = cls._optional_text(raw_profile[field], field, 200)
                if text is not None:
                    profile[field] = text
        if "narrative_arc" in raw_profile:
            text = cls._optional_text(
                raw_profile["narrative_arc"], "narrative_arc", 1_000
            )
            if text is not None:
                profile["narrative_arc"] = text
        if "duration_ms" in raw_profile:
            duration = raw_profile["duration_ms"]
            if duration is not None:
                if (
                    isinstance(duration, bool)
                    or not isinstance(duration, int)
                    or not 1_000 <= duration <= 30 * 60 * 1_000
                ):
                    raise ValueError("`duration_ms` must be null or an integer from 1000 to 1800000.")
                profile["duration_ms"] = duration
        if "aspect_ratio" in raw_profile:
            aspect_ratio = raw_profile["aspect_ratio"]
            if aspect_ratio is not None and aspect_ratio != "":
                if not isinstance(aspect_ratio, str) or aspect_ratio not in ASPECT_RATIOS:
                    raise ValueError("`aspect_ratio` must be empty, 16:9, 9:16, 1:1, or 4:5.")
                profile["aspect_ratio"] = aspect_ratio
        for field in ("must_include", "must_exclude"):
            if field in raw_profile:
                values = cls._string_list(raw_profile[field], field)
                if values:
                    profile[field] = values
        return profile

    def resolve_profile_ref(self, raw_ref: object) -> dict[str, object]:
        if not isinstance(raw_ref, dict):
            raise ValueError("`creator_profile_ref` must be a JSON object.")
        unknown = sorted(set(raw_ref) - {"profile_id", "revision", "content_sha256"})
        if unknown:
            raise ValueError(f"Unsupported creator profile reference fields: {', '.join(unknown)}.")
        profile_id = raw_ref.get("profile_id", PROFILE_ID)
        revision = raw_ref.get("revision")
        digest = raw_ref.get("content_sha256")
        if not isinstance(profile_id, str):
            raise ValueError("Creator profile reference is invalid.")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
            raise ValueError("Creator profile reference revision must be a positive integer.")
        if not isinstance(digest, str) or len(digest) != 64:
            raise ValueError("Creator profile reference must include its content SHA-256.")
        stored = self.profile_revision(profile_id=profile_id, revision=revision)
        if stored["content_sha256"] != digest:
            raise ValueError("Creator profile reference content hash does not match the saved revision.")
        return stored

    @staticmethod
    def _optional_text(value: object, field: str, limit: int) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or len(value.strip()) > limit:
            raise ValueError(f"`{field}` must be empty or a string of at most {limit} characters.")
        return value.strip() or None

    @staticmethod
    def _string_list(value: object, field: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 32:
            raise ValueError(f"`{field}` must be a string array with at most 32 entries.")
        if any(not isinstance(item, str) or not item.strip() or len(item.strip()) > 120 for item in value):
            raise ValueError(f"`{field}` entries must be non-empty strings of at most 120 characters.")
        return list(dict.fromkeys(item.strip() for item in value))

    @staticmethod
    def _validate_evidence(value: object) -> list[dict[str, object]]:
        if not isinstance(value, list) or len(value) > 64:
            raise ValueError("`evidence` must contain at most 64 project references.")
        evidence: list[dict[str, object]] = []
        seen: set[tuple[str, int]] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"project_id", "brief_revision"}:
                raise ValueError("Each evidence item must contain project_id and brief_revision.")
            project_id = item.get("project_id")
            revision = item.get("brief_revision")
            if (
                not isinstance(project_id, str)
                or not project_id
                or len(project_id) > 200
                or isinstance(revision, bool)
                or not isinstance(revision, int)
                or revision < 1
            ):
                raise ValueError("Creator profile evidence contains an invalid project reference.")
            key = (project_id, revision)
            if key not in seen:
                seen.add(key)
                evidence.append({"project_id": project_id, "brief_revision": revision})
        return evidence

    @staticmethod
    def _validate_profile_id(profile_id: str) -> None:
        if profile_id != PROFILE_ID:
            raise ValueError("Only the database-scoped `default` creator profile is supported.")

    @staticmethod
    def _empty_profile(profile_id: str) -> dict[str, object]:
        return {
            "profile_id": profile_id,
            "revision": 0,
            "content_sha256": None,
            "profile": {},
            "evidence": [],
            "source": None,
            "created_at": None,
        }
