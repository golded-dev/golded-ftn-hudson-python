# Security

Treat message archives as untrusted input. Readers validate record boundaries,
offsets and decoding, but archives must remain stable while being read.

Report vulnerabilities privately through [GitHub security advisories](https://github.com/golded-dev/golded-ftn-hudson-python/security/advisories/new).
Do not put private archives, message contents or credentials in public issues.
Security review targets the current 1.2.x development line. The writer changes
are prepared locally and have not been published on PyPI.

Writer sessions validate the base under their operation lock and roll back
handled I/O failures. Keep GoldED closed; direct file access bypasses this lock.
No recovery guarantee covers process termination or power loss. Archive mode is
for reading damaged records, never for editing them.
