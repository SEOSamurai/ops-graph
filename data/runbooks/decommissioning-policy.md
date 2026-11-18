# Policy: decommissioning a server

Owner: Infrastructure governance. Fictional document for the ops-graph demo.

## When a server can be proposed

A server can be proposed for decommissioning when no live application or database
depends on it, or when monitoring has not seen it for 90 days. A server whose
workload is unknown (an application might run on it) cannot be proposed until the
owning team confirms.

## Procedure

1. Confirm with the owning team and with every team that owns an application on it.
2. Switch the server off for 30 days before deleting anything ("scream test").
3. Present the change to the change advisory board with the impact analysis.
4. Update the CMDB status to Retired only after the server is physically gone.

## Known cases

SRV-PAR-LEGACY01 is marked Retired in the CMDB but monitoring still sees it: the
procedure was not finished. SRV-LYO-FTP01 has not been seen since June.
