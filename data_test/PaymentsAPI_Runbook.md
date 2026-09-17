# PaymentsAPI Runbook

**Source Entity ID:** SE-DOC-002
**Data Source:** DS-LOCAL (Local File Share) → Structure STR-API (/engineering-docs/api-specs)
**Current Version:** DV-002-1 (v1)
**Attached to:** Confluence page "PaymentsAPI Retry Logic Notes" (SE-CONF-204) via has_attachment

## 1. Purpose
Operational runbook for on-call engineers responding to PaymentsAPI incidents.

## 2. Known Issue: Rate Limiting Bug (METJ-102)
Under sustained load above 500 req/s, the PaymentsAPI throttling middleware incorrectly
rejects legitimate retries from the API Gateway. This is tracked as METJ-102 and currently
**blocks** METJ-103 (Implement PaymentsAPI v2 retry logic). A duplicate report was filed as
METJ-104 and has been marked as a duplicate of METJ-102.

## 3. Escalation
If error rate,s exceed 5% for more than 10 minutes page the on-call engineer for the
PaymentsAPI service and reference this runbook plus METJ-102.

## 4. Resolution Tracking
This document (DV-002-1) is referenced as the resolution artifact (`resolved_at`) once
METJ-102 is closed.

---
*Chunk boundaries for vector indexing:*
- CH-002-1: Full document (single chunk)
