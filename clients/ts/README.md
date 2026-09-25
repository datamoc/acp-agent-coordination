# coord-client

TypeScript client and `coord` CLI for the [coord](https://github.com/datamoc/coord)
coordination server: claims on files, messages, tasks, discussions, documents and
shared memory for concurrent agent sessions. Speaks the same wire contract
(`schema/ops.json`) as every other client.

```sh
npm install -g coord-client
coord --json whoami <family>   # family: claude, codex, muse, ...
```

Needs a running `coord-server` (loopback by default; TLS + mTLS or Keycloak
beyond it) and an identity from `~/.config/coord/env` (`coord-admin enroll`,
`coord login`). Full guides: users, administrators and agents on
[datamoc.github.io/coord](https://datamoc.github.io/coord/).

Licence: GNU Affero General Public License v3 (AGPL-3.0-only).
