# Postmortem: customer-portal slowdown, 2026-10-07

Fictional document for the ops-graph demo.

## Summary

For 47 minutes, customer-portal answered in 4 to 6 seconds instead of under 1.5.
Customers could not download their contract certificates.

## Timeline

- 09:02 replication lag starts on SRV-PAR-DB01 after a batch import by billing-batch.
- 09:10 claims-api error rate rises: its writes to claims-db wait on the primary.
- 09:15 customer-portal latency alert fires. The on-call engineer restarts claims-api,
  which does not help.
- 09:49 the long transaction from billing-batch is cancelled; latency recovers.

## Root cause

The slowdown visible on customer-portal came from the database server two levels
below it. Restarting the application in front only hid the cause for a few minutes.

## Actions

- Look at the deepest alerting dependency first (now automated by root-cause analysis).
- Move billing-batch imports out of business hours.
