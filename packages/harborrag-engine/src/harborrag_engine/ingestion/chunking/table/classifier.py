from __future__ import annotations

import re

from harborrag_core.contracts import TokenCounter
from harborrag_core.domain import TableArtifact, TableCellType

from ..table_policy import TableChunkingPolicy
from .errors import TableClassificationError
from .models import TableClassification, TableShape
from .rendering import TableRenderer

_TIME_HEADERS = re.compile(
    r"(?:^|[\s_-])(date|datetime|timestamp|time|day|week|month|quarter|year)(?:$|[\s_-])",
    re.IGNORECASE,
)
_MATRIX_ROW_HEADERS = frozenset(
    {"category", "region", "metric", "measure", "team", "product", "segment"}
)


class TableShapeClassifier:
    """Classify tables using LARGE > TIME_SERIES > MATRIX > WIDE > LONG > SMALL."""

    def __init__(self, token_counter: TokenCounter) -> None:
        self._token_counter = token_counter

    def classify(
        self,
        artifact: TableArtifact,
        policy: TableChunkingPolicy,
    ) -> TableClassification:
        if artifact.row_count < 1 or artifact.column_count < 1:
            raise TableClassificationError(f"table {artifact.table_id!r} has no classifiable cells")
        renderer = TableRenderer(artifact)
        estimated_tokens = self._token_counter.count(renderer.estimated_text())
        key_columns, key_confidences, key_warnings = self._key_columns(artifact, policy)
        time_index, time_confidence = self._time_signal(artifact, renderer)
        row_label_confidence, column_label_confidence, matrix_confidence = self._matrix_signals(
            artifact, renderer
        )
        shape, confidence = self._shape(
            artifact,
            policy,
            estimated_tokens,
            time_confidence,
            matrix_confidence,
        )
        warnings = list(key_warnings)
        if shape == TableShape.WIDE and not key_columns:
            warnings.append("wide table has no key column above configured confidence")
        return TableClassification(
            shape=shape,
            confidence=confidence,
            estimated_tokens=estimated_tokens,
            key_column_indices=key_columns,
            key_column_confidences=key_confidences,
            time_column_index=time_index,
            time_column_confidence=time_confidence,
            row_label_confidence=row_label_confidence,
            column_label_confidence=column_label_confidence,
            warnings=tuple(warnings),
        )

    @staticmethod
    def _key_columns(
        artifact: TableArtifact,
        policy: TableChunkingPolicy,
    ) -> tuple[tuple[int, ...], dict[int, float], tuple[str, ...]]:
        explicit: list[int] = []
        warnings: list[str] = []
        normalized_names = {
            name.casefold().strip(): index for index, name in enumerate(artifact.column_names)
        }
        for value in policy.explicit_key_columns:
            index = (
                value
                if isinstance(value, int)
                else normalized_names.get(value.casefold().strip(), -1)
            )
            if not isinstance(index, int) or not 0 <= index < artifact.column_count:
                warnings.append(f"configured key column does not exist: {value}")
                continue
            explicit.append(index)
        confidences = {
            candidate.column_index: candidate.confidence
            for candidate in artifact.key_column_candidates
        }
        inferred = [
            candidate.column_index
            for candidate in artifact.key_column_candidates
            if candidate.confidence >= policy.key_column_confidence
        ]
        selected = tuple(dict.fromkeys((*explicit, *inferred)).keys())[: policy.maximum_key_columns]
        for index in explicit:
            confidences[index] = 1.0
        if not selected and artifact.key_column_candidates:
            candidate = artifact.key_column_candidates[0]
            selected = (candidate.column_index,)
            warnings.append("selected best available key column below configured confidence")
        return selected, confidences, tuple(warnings)

    @staticmethod
    def _time_signal(
        artifact: TableArtifact,
        renderer: TableRenderer,
    ) -> tuple[int | None, float]:
        best_index: int | None = None
        best_confidence = 0.0
        data_rows = renderer.data_row_indices
        for column_index, header in enumerate(artifact.column_names):
            header_score = 0.55 if _TIME_HEADERS.search(f" {header} ") else 0.0
            typed = sum(
                (cell := renderer.cell(row_index, column_index)) is not None
                and cell.cell_type in {TableCellType.DATE, TableCellType.DATETIME}
                for row_index in data_rows
            )
            typed_ratio = typed / max(len(data_rows), 1)
            confidence = min(1.0, header_score + 0.45 * typed_ratio)
            if confidence > best_confidence:
                best_index = column_index
                best_confidence = confidence
        return best_index, best_confidence

    @staticmethod
    def _matrix_signals(
        artifact: TableArtifact,
        renderer: TableRenderer,
    ) -> tuple[float, float, float]:
        data_rows = renderer.data_row_indices
        if artifact.column_count < 3 or len(data_rows) < 2:
            return 0.0, 0.0, 0.0
        row_labels = [
            renderer.cell_text(row_index, 0).strip()
            for row_index in data_rows
            if renderer.cell_text(row_index, 0).strip()
        ]
        row_label_confidence = len(set(row_labels)) / max(len(data_rows), 1) if row_labels else 0.0
        column_label_confidence = sum(bool(path) for path in artifact.header_hierarchy[1:]) / (
            artifact.column_count - 1
        )
        interior = [
            renderer.cell(row_index, column_index)
            for row_index in data_rows
            for column_index in range(1, artifact.column_count)
        ]
        numeric_ratio = sum(
            cell is not None and cell.cell_type == TableCellType.NUMBER for cell in interior
        ) / max(len(interior), 1)
        first_header = artifact.column_names[0].casefold().strip()
        semantic_bonus = 0.1 if first_header in _MATRIX_ROW_HEADERS else 0.0
        identity_penalty = (
            0.35
            if first_header in {"id", "key", "name", "service", "owner", "item", "resource"}
            else 0.0
        )
        matrix_confidence = max(
            0.0,
            min(
                1.0,
                0.35 * row_label_confidence
                + 0.3 * column_label_confidence
                + 0.35 * numeric_ratio
                + semantic_bonus
                - identity_penalty,
            ),
        )
        return row_label_confidence, column_label_confidence, matrix_confidence

    @staticmethod
    def _shape(
        artifact: TableArtifact,
        policy: TableChunkingPolicy,
        estimated_tokens: int,
        time_confidence: float,
        matrix_confidence: float,
    ) -> tuple[TableShape, float]:
        thresholds = policy.thresholds
        cell_count = artifact.row_count * artifact.column_count
        if (
            artifact.row_count >= thresholds.large_minimum_rows
            or cell_count >= thresholds.large_minimum_cells
            or estimated_tokens >= thresholds.large_minimum_tokens
        ):
            return TableShape.LARGE, 1.0
        if time_confidence >= thresholds.time_series_confidence:
            return TableShape.TIME_SERIES, time_confidence
        if matrix_confidence >= thresholds.matrix_confidence:
            return TableShape.MATRIX, matrix_confidence
        if artifact.column_count >= thresholds.wide_minimum_columns:
            return TableShape.WIDE, 1.0
        if (
            artifact.row_count >= thresholds.long_minimum_rows
            or estimated_tokens > thresholds.small_maximum_tokens
        ):
            return TableShape.LONG, 1.0
        if (
            artifact.row_count <= thresholds.small_maximum_rows
            and artifact.column_count <= thresholds.small_maximum_columns
            and estimated_tokens <= thresholds.small_maximum_tokens
        ):
            return TableShape.SMALL, 1.0
        return TableShape.LONG, 0.75
