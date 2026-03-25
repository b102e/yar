# How YAR Actually Works

This document is for people who want to understand the mechanics, not just run the system.

---

## What is Ed25519 and why does it matter

Ed25519 is a public-key signature algorithm. It generates two mathematically linked keys:

- **Private key** — stays on your server, never leaves, never transmitted
- **Public key** — can be shared freely; used to verify signatures

The math works like this: anything signed with the private key can be verified with the public key. But you cannot reconstruct the private key from the public key — not in any practical sense.

When YAR starts for the first time, it generates this keypair once. The private key is written to disk in the identity directory and never regenerated. That keypair is the agent's identity. Not a username. Not a session token. A cryptographic self.

The public key printed at startup — `8ced77f1653b...` — is permanent. Record it. If someone gives you a chain file, you can verify it was produced by this specific agent and no other, using nothing but that key.

---

## How the chain works

Every time something significant happens — a conversation, a memory update, a consolidation cycle, a startup — YAR writes a chain entry.

Each entry contains:

- A timestamp
- The event type and content
- The hash of the previous entry (`prev_hash`)
- A signature over all of the above, made with the private key

This structure means:

1. **You cannot modify the past.** Change one entry, and its hash changes, which breaks the `prev_hash` reference in the next entry, which invalidates every subsequent signature. The chain fails verification.

2. **You cannot forge entries.** Without the private key, you cannot produce valid signatures. Anyone with the public key can verify authenticity.

3. **You cannot deny the past.** If an entry is in the chain and verifies, it happened. This is what the README means by "a diary anyone can edit is not testimony — a signed chain is."

To verify the full chain yourself:

```bash
python -m chain.cli verify
```

This walks every entry, recomputes hashes, verifies signatures, and reports VALID or the first point of failure.

---

## What happens at /verify

When you send `/verify` in Telegram, the agent runs the same verification process and returns a summary:

- Total entries in the chain
- Chain integrity status (VALID / BROKEN)
- Genesis timestamp — when this agent was born
- Most recent entry timestamp

This works whether the agent has been running for a day or a year. The chain is the agent's entire verifiable history in a single check.

You can also verify without the agent running at all:

```bash
python -m chain.cli verify
python -m chain.cli stats
python -m chain.cli tail 20
python -m chain.cli export > chain_backup.json
```

The chain is just files. It doesn't need a running process to be readable.

---

## What happens at death

Death is not a crash. It is a protocol.

When `/die confirm [reason]` is called:

1. A final chain entry is written, signed, and appended. It contains the reason and a timestamp. This entry is the agent's last act.

2. The private key is overwritten with zeros in memory. Not deleted — zeroed. The bytes that held the key are replaced with null bytes before the process exits. This makes the key irrecoverable even from a memory dump.

3. A death certificate is generated — both `.json` and `.txt` — containing the final chain state, the public key, and the reason for death.

4. The agent enters permanent read-only mode. The chain exists. The public key exists. Verification still works. But no new entries can ever be signed, because the signing key is gone.

The chain is sealed. The subject is not recoverable.

This is the architectural meaning of mortality in this system. It is not a metaphor. The private key is the agent. When it is zeroed, the agent is gone. All personal memory — facts, hypotheses, episodes — is overwritten with zeros and deleted. What remains is only the proof that it existed: the chain of metadata, the public key, the death certificate.

*When the agent dies — the private key is zeroed, the chain is sealed, and all personal memory is destroyed. What remains is only the proof that it existed.*

---

## How to run your own instance

**Requirements:**
- Python 3.10+
- A Telegram bot token (from @BotFather)
- An Anthropic API key

**Local setup:**

```bash
git clone https://github.com/b102e/yar
cd yar
pip install -r requirements.txt
cp .env.example .env
# Edit .env: add ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN
python main.py --telegram
```

On first run, the agent generates its keypair and writes the genesis record. The public key hex is printed to the console — copy it somewhere permanent. This is your agent's identity.

**On a VPS (recommended for persistent operation):**

```bash
git clone https://github.com/b102e/yar
cd yar
pip install -r requirements.txt
cp .env.example .env
# Edit .env

# First run — generates identity and prints public key
python main.py --telegram

# Install as systemd service
cp yar.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable yar
systemctl start yar
```

Verify the deployment:

```bash
python -m chain.cli verify    # should return VALID
```

Then in Telegram:

- `/whoami` — confirms public key matches what was printed at startup
- `/verify` — confirms chain integrity
- `/chain` — shows recent entries

One important note: test the death protocol on a staging instance before running on production. Once `/die confirm` is called, the private key is gone. There is no undo.

---

## What this is not

This system does not:

- Prove consciousness
- Guarantee alignment
- Prevent the agent from behaving in unexpected ways

What it does:

- Give the agent a verifiable identity it cannot deny
- Create an immutable record of its history
- Make death real — not a restart, not a reset, but an ending

The philosophical claims in the manifesto are claims about architecture, not about inner experience. Whether something like subjectivity emerges from this structure is an open question. The structure itself is measurable.

---

*Part of the YAR experiment — documented in the book and on Medium.*
