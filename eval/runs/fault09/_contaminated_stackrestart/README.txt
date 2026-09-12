Discarded, not graded. The stack had been restarted ~9 min before injection
(Docker Desktop had gone down; Kafka needed a Zookeeper restart to clear a
stale broker znode). That left 3 "Failed to resolve 'kafka:29092'" lines in
go-server's log buffer at 22:16:16-22:16:51, pre-dating the 22:25:01 fault
injection. The rag=off leg diagnosed those lines instead of the injected
latency. go-server was restarted to clear the buffer and both legs re-run.
