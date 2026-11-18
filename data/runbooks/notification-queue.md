# Runbook: notification queue on mq01

Owner: Digital Channels. Fictional document for the ops-graph demo.

## Context

notification-service consumes the queue notifications.outbound on mq01.par.acme.local
and sends emails and text messages to customers. mq01 was installed during a project
and never registered in the CMDB: open a registration request before any change.

## Queue depth above 10 000 messages

1. Check that notification-service consumers are running.
2. If consumers are healthy, the backlog comes from a burst upstream (claims-api
   publishes one event per claim update). It drains on its own at about 2 000
   messages per minute.
3. Never purge the queue: messages are customer notifications with legal value.
