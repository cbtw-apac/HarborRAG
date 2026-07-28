from __future__ import annotations

from harborrag_core.schemas.vector import VectorPoint

from .schemas import VectorMutationPlan, VectorValidationResult

REQUIRED_VECTOR_PAYLOAD_FIELDS = frozenset(
    {
        "tenant_id",
        "chunk_revision_id",
        "logical_chunk_id",
        "artifact_id",
        "artifact_revision_id",
        "generation_id",
        "source_kind",
        "chunk_role",
        "structural_path",
        "page_range",
        "line_range",
        "content_hash",
        "token_count",
        "embedding_configuration_fingerprint",
        "content_reference",
        "index_state",
        "is_active",
    }
)


class VectorValidationService:
    """Validate expected vector identity, dimension, generation, and metadata."""

    def validate(
        self,
        plan: VectorMutationPlan,
        persisted: tuple[VectorPoint, ...],
    ) -> VectorValidationResult:
        """Validate persisted vector points against the staged plan."""

        errors: list[str] = []
        expected = {point.id: point for point in plan.points}
        actual = {point.id: point for point in persisted}
        if len(actual) != len(persisted):
            errors.append("vector repository returned duplicate point IDs")
        missing = sorted(set(expected) - set(actual))
        unexpected = sorted(set(actual) - set(expected))
        if missing:
            errors.append(f"staged vector points are missing: {', '.join(missing)}")
        if unexpected:
            errors.append(f"unexpected staged vector points were returned: {', '.join(unexpected)}")

        for point_id in sorted(set(expected) & set(actual)):
            planned = expected[point_id]
            stored = actual[point_id]
            if stored.tenant_id != planned.tenant_id:
                errors.append(f"point {point_id} tenant does not match the plan")
            if len(stored.vector) != plan.dimension:
                errors.append(f"point {point_id} has an invalid vector dimension")
            missing_fields = sorted(REQUIRED_VECTOR_PAYLOAD_FIELDS - stored.payload.keys())
            if missing_fields:
                errors.append(f"point {point_id} payload is missing: {', '.join(missing_fields)}")
                continue
            for field, value in planned.payload.items():
                if stored.payload.get(field) != value:
                    errors.append(f"point {point_id} payload field {field!r} does not match")
            if stored.payload.get("generation_id") != plan.generation_id:
                errors.append(f"point {point_id} belongs to another generation")
            if stored.payload.get("embedding_configuration_fingerprint") != (
                plan.embedding_configuration_fingerprint
            ):
                errors.append(f"point {point_id} has another embedding configuration")
            if stored.payload.get("index_state") != "staged":
                errors.append(f"point {point_id} is not staged")
            if stored.payload.get("is_active") is not False:
                errors.append(f"point {point_id} became active during staging")

        return VectorValidationResult(
            valid=not errors,
            checked_point_count=len(persisted),
            errors=tuple(errors),
        )
