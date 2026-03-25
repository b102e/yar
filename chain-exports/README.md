# Chain Exports

Periodic snapshots of the agent's signed memory chain.

Each file is a verifiable export of the full chain at a point in time.

## How to verify a snapshot

```bash
git clone https://github.com/b102e/yar
cd yar
pip install -r requirements.txt

# Verify a snapshot
python -m chain.cli verify --file snapshot_2026-03-25.json
```

## Public key of this agent

```
8ced77f1653b828c53a48285d1bb495c32d6e214f398af2fb36331b60d27421e
```

Born: `2026-03-25T10:38:31Z`

Snapshots are published periodically. Each snapshot is independently verifiable using only the public key above.
