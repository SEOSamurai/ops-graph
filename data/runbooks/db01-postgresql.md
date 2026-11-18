# Runbook: PostgreSQL primary SRV-PAR-DB01

Owner: Database Ops. Fictional document for the ops-graph demo.

## What runs here

SRV-PAR-DB01 (db01.par) hosts two PostgreSQL 15 databases: policy-db, the contract
store used by quote-engine, policy-admin and billing-batch, and claims-db, used by
claims-api and fraud-scoring. A streaming replica runs in Lyon.

## Replication lag above 60 seconds

Usually caused by a long write burst or a full WAL volume on the primary.
1. Check `pg_stat_replication` on the primary: `replay_lag` per standby.
2. Check free space on `/var/lib/pgsql`. Above 90%, archive WAL and expand the volume
   before anything else: a full disk stops the primary.
3. Look for long transactions (`pg_stat_activity`, `xact_start` older than 10 minutes)
   and ask the owning team before cancelling them.
Do not restart the replica while the lag is growing: it would have to resynchronise
from scratch.

## Filesystem above 90%

Rotate PostgreSQL logs, purge archived WAL older than 7 days, then expand the volume.
Expanding is online and takes about 10 minutes.

## Failover

Failover to the Lyon replica needs approval from the claims and core-insurance teams,
because claims-api and policy-admin lose writes for about 2 minutes.
