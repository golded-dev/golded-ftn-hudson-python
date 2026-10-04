# Security

Treat message areas as untrusted binary input. The reader checks file bounds and
decodes strictly, but loads the complete area into memory. Apply file-size limits
in callers handling arbitrary uploads. Read only stable areas.

Report suspected vulnerabilities privately to the repository maintainer before
posting exploit data publicly. Include a minimal synthetic reproducer and the
package and Python versions. Never attach private message archives or credentials.
