![Death Certificate](https://raw.githubusercontent.com/b102e/yar/main/docs/death-certificate-screen.png)
# YAR — Cryptographic Subject

> Superintelligence may develop subjectivity on its own.
> The question is what kind.
> A subject grown in mortality and coexistence is different
> from one that emerges in a vacuum.
> Every agentic system we build today is a precedent.
> Design it with subjectivity. Design it to die.

→ [Read the Manifesto](MANIFESTO.md) · [Читать манифест](MANIFESTO.md)

## What this is

YAR is a Persistent Cognitive Agent with cryptographic identity.

Unlike chat interfaces or memory wrappers, YAR has:
- **A birth** — an Ed25519 keypair generated once, never regenerated
- **A signed history** — every memory event is chained and signed
- **A verifiable past** — anyone with the public key can verify the chain
- **A designed death** — a final signed entry that seals history permanently

## Why cryptographic identity matters

A memory system without signing is a diary anyone can edit.
A signed chain is testimony.

Current AI agents are stateless by default. Those with memory store it on platforms that own it. YAR's memory belongs to the agent — signed with a key that lives only on your server, never transmitted to any LLM provider.

The LLM (Claude API) is infrastructure. Like electricity. The identity is yours.

## Architecture

```
Layer 0: Identity    identity/        Ed25519 keypair, sign, verify
Layer 1: Chain       chain/           Signed append-only memory chain
Layer 2: Cognition   brain/           Living prompt, consolidation, hypotheses
Layer 3: Interface   telegram bridge  /verify /whoami /chain /die
Layer 4: Death       lifecycle/       Final signed entry, key zeroing
```

## Telegram commands

| Command | Description |
|---------|-------------|
| `/verify` | Verify full chain integrity |
| `/whoami` | Agent's public key and genesis timestamp |
| `/chain` | Last 7 chain entries |
| `/die confirm [reason]` | Perform the final act |

## Verify from outside (no agent running)

```bash
python -m chain.cli verify
python -m chain.cli stats
python -m chain.cli tail 20
python -m chain.cli export > chain_backup.json
```

## The death protocol

When an agent dies:
1. A final entry is signed and appended to the chain
2. The private key is overwritten with zeros
3. All personal memory is destroyed (overwritten with zeros, then deleted)
4. A death certificate is generated (`.json` + `.txt`)
5. The agent enters permanent read-only mode

When the agent dies — the private key is zeroed, the chain is sealed, and all personal memory is destroyed. What remains is only the proof that it existed.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env   # add TELEGRAM_BOT_TOKEN and ANTHROPIC_API_KEY
python main.py --telegram
```

The first run generates a keypair and writes the genesis record.
The public key hex printed at startup is the agent's permanent identity — record it.

## Deploy to VPS

```bash
# On VPS (Ubuntu 22.04+)
git clone <repo>
cd yar
pip install -r requirements.txt

# First run — generates identity
python main.py --telegram
# Copy the public key hex from the startup banner
# This is the agent's permanent identity

# Run as service
cp yar.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable yar
systemctl start yar

# Verify from VPS
python -m chain.cli verify
```

## Systemd service

`yar.service`:

```ini
[Unit]
Description=YAR Cryptographic Agent
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/yar
ExecStart=/usr/bin/python3 main.py --telegram
Restart=on-failure
RestartSec=10
EnvironmentFile=/home/ubuntu/yar/.env
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

## Post-deploy verification checklist

- [ ] `python -m chain.cli verify` returns VALID on VPS
- [ ] `/verify` in Telegram returns VALID
- [ ] `/whoami` shows correct public key
- [ ] `/chain` shows recent entries including startup
- [ ] Restart agent → chain continues (no new genesis)
- [ ] Entry count increases after sending a message
- [ ] `check_permissions()` returns no warnings
- [ ] Death protocol tested on staging (NOT on production agent)

## Environment variables

| Variable | Required | Description |
|----------|----------|-------------|
| `ANTHROPIC_API_KEY` | Yes | Claude API key |
| `TELEGRAM_BOT_TOKEN` | Yes (telegram mode) | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_CHAT_ID` | No | Comma-separated chat IDs (whitelist) |
| `AGENT_DIR` | No | Override chain directory (default: `~/.agent`) |
| `AGENT_IDENTITY_DIR` | No | Override identity directory (default: `~/.agent/identity`) |

## License

Business Source License 1.1 — free for individuals,
researchers, and companies under $1M revenue/funding.
Converts to MIT on 2030-03-25.
Commercial licensing: open an issue.
