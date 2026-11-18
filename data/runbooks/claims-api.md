# Runbook: claims-api

Owner: Claims team. Fictional document for the ops-graph demo.

## Dependencies

claims-api runs on SRV-PAR-APP02. It reads and writes claims-db, stores attachments
in document-store and publishes events to notification-service.

## HTTP 5xx above 5%

1. Look at the error mix: database timeouts point to claims-db, not to the API.
2. If most errors are timeouts on claims-db, check the database runbook first
   (replication lag or a full disk on SRV-PAR-DB01 slows every write).
3. If errors come from document-store, check SRV-PAR-DB02, which still runs
   Windows Server 2012 R2 and has no vendor support.
4. Restarting claims-api does not help when the database is the cause; it only
   drops in-flight claims.

## Personal data

claims-api processes personal data (claimant identity, medical documents). Any
incident with possible data exposure goes to the data protection officer within 24 hours.
