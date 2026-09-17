# PDF Upload Error Handling Guide

**Source Entity ID:** SE-DOC-003
**Data Source:** DS-LOCAL (Local File Share) → Structure STR-API (/engineering-docs/api-specs)
**Current Version:** DV-003-1 (v1)

## 1. Purpose
This guide describes how the document ingestion pipeline handles corrupted PDF uploads.

## 2. Detection
Uploaded PDFs are validated against the PDF 1.7 spec at ingestion time. A file that fails
`%%EOF` trailer validation, has a malformed xref table, or cannot be opened by the parser
is flagged as corrupted rather than silently dropped.

## 3. Handling Corrupted PDF Uploads
When a corrupted PDF is detected:
1. The upload is rejected with a descriptive error rather than partially ingested.
2. The event is logged and linked to ticket METJ-101 ("Fix corrupted PDF upload validation").
3. The user is prompted to re-upload or convert the file to PDF/A before retrying.

## 4. Related Investigation
METJ-105 ("Investigate corrupted PDF uploads in production") tracks the broader investigation
into recurring corruption reports and **relates_to** METJ-101.

---
*Chunk boundaries for vector indexing:*
- CH-003-1: Sections 1–2 (Purpose, Detection)
- CH-003-2: Sections 3–4 (Handling Corrupted PDF Uploads, Related Investigation)
