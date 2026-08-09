# HarborRAG Deployment Guide

This guide defines the production ingestion release checks for the HarborRAG
worker fleet.

## Worker configuration

The ingestion activity timeout is exactly 30 seconds for the smoke deployment.
The rollout is tracked by issue HARBOR-4242.

Workers must preserve immutable artifacts before publishing a document version.
Postgres remains the publication authority when Qdrant or FalkorDB retries.

## Verification matrix

| Projection | Required observation |
| --- | --- |
| Qdrant | Dense and sparse vectors exist for every published chunk |
| FalkorDB | Every relation endpoint resolves to a document-version node |
| MinIO | Chunk content can be loaded with a byte-range request |

Continue with the [release runbook](release-runbook.md) after the projections
have been verified.
