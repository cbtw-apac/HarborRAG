from __future__ import annotations

import json
from io import BytesIO
from typing import Any

from harborrag_adapters.repositories.object_store.ingestion_artifacts import (
    ARTIFACT_BUCKET,
    ImmutableArtifact,
    ImmutableArtifactReader,
    ImmutableArtifactWriter,
    IngestionArtifactLayout,
)
from harborrag_core.domain import TableArtifact
from harborrag_core.ingestion import ArtifactReference, reject_runtime_fields
from harborrag_core.storage import StorageOperationContext

TABLE_PARQUET_SCHEMA_VERSION = 1


class CanonicalTableArtifactRepository:
    """Persist canonical logical table grids as deterministic Parquet files."""

    def __init__(
        self,
        writer: ImmutableArtifactWriter,
        reader: ImmutableArtifactReader,
    ) -> None:
        self._writer = writer
        self._reader = reader

    async def put_all(
        self,
        tables: tuple[TableArtifact, ...],
        *,
        document_id: str,
        document_version_id: str,
        context: StorageOperationContext,
    ) -> tuple[ArtifactReference, ...]:
        ordered = tuple(sorted(tables, key=lambda table: table.table_id))
        self._validate_identity(
            ordered,
            document_id=document_id,
            document_version_id=document_version_id,
        )
        references = []
        for table in ordered:
            references.append(
                await self._writer.put(
                    ImmutableArtifact(
                        bucket=ARTIFACT_BUCKET,
                        key=IngestionArtifactLayout.table(
                            document_id,
                            document_version_id,
                            table.table_id,
                        ),
                        payload=self._parquet_bytes(table),
                        media_type="application/vnd.apache.parquet",
                        artifact_kind="canonical-table",
                    ),
                    context=context,
                )
            )
        return tuple(references)

    async def get_rows(
        self,
        reference: ArtifactReference,
        *,
        context: StorageOperationContext,
    ) -> tuple[tuple[str | None, ...], ...]:
        arrow, parquet = _pyarrow()
        payload = await self._reader.get(reference, context=context)
        table = parquet.read_table(arrow.BufferReader(payload))
        columns = tuple(table.column(index).to_pylist() for index in range(table.num_columns))
        return tuple(
            tuple(column[row_index] for column in columns) for row_index in range(table.num_rows)
        )

    @staticmethod
    def _validate_identity(
        tables: tuple[TableArtifact, ...],
        *,
        document_id: str,
        document_version_id: str,
    ) -> None:
        if any(table.document_id != document_id for table in tables):
            raise ValueError("canonical table belongs to another document")
        if any(table.document_version_id != document_version_id for table in tables):
            raise ValueError("canonical table belongs to another document version")

    @staticmethod
    def _parquet_bytes(table: TableArtifact) -> bytes:
        arrow, parquet = _pyarrow()
        arrays = []
        fields = []
        for column_index in range(table.column_count):
            values = tuple(
                (
                    cell.text
                    if (
                        cell := table.source_cell(
                            row_index,
                            column_index,
                        )
                    )
                    is not None
                    else None
                )
                for row_index in range(table.row_count)
            )
            name = f"column_{column_index:04d}"
            arrays.append(arrow.array(values, type=arrow.string()))
            fields.append(arrow.field(name, arrow.string()))
        descriptor = table.model_dump(
            mode="json",
            exclude={"cells", "logical_grid"},
        )
        reject_runtime_fields(descriptor)
        metadata = {
            b"harborrag.schema_version": str(TABLE_PARQUET_SCHEMA_VERSION).encode("ascii"),
            b"harborrag.table": json.dumps(
                descriptor,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8"),
        }
        schema = arrow.schema(fields, metadata=metadata)
        logical_table = arrow.Table.from_arrays(arrays, schema=schema)
        output = BytesIO()
        parquet.write_table(
            logical_table,
            output,
            compression="zstd",
            use_dictionary=False,
            write_statistics=True,
            data_page_version="1.0",
            version="2.6",
        )
        return output.getvalue()


def _pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow
        import pyarrow.parquet
    except ImportError as error:
        raise RuntimeError(
            "canonical Parquet table artifacts require harborrag-adapters[tables]"
        ) from error
    return pyarrow, pyarrow.parquet
