# Solana Validator High Availability Manager

Production-ready high availability solution for Solana validators with automatic failover, health monitoring, and split-brain prevention.

---

## 🎯 Overview

The Solana Validator HA Manager provides **automatic failover** between validator nodes using **hot identity swapping** - changing validator identity without restarting the validator process. This ensures minimal downtime during failures and maintains cluster participation.

### Key Features

- ✅ **Automatic Failover** - Detects failures and promotes passive validator to active
- ✅ **Hot Identity Swap** - No validator restart required (1-2 second transition)
- ✅ **Split-Brain Prevention** - Ensures only one active validator at a time
- ✅ **Gossip-Based Discovery** - Uses Solana's built-in gossip network
- ✅ **Comprehensive Health Checks** - Monitors slot progression and lag
- ✅ **Self-Health Verification** - Only healthy validators become active
- ✅ **Prometheus Metrics** - Full operational visibility
- ✅ **Production Ready** - Tested on mainnet validators

### How It Works

```
Normal Operation:
┌─────────────────────┐          ┌─────────────────────┐
│   Validator 1       │          │   Validator 2       │
│   ACTIVE ✅         │◄────────►│   PASSIVE           │
│                     │  Gossip  │                     │
│  HA Manager         │          │  HA Manager         │
│  - Monitors         │          │  - Monitors         │
│  - Health Checks    │          │  - Ready to Take    │
└─────────────────────┘          └─────────────────────┘

Failover (Validator 1 Fails):
┌─────────────────────┐          ┌─────────────────────┐
│   Validator 1       │          │   Validator 2       │
│   OFFLINE ❌        │    ✗     │   ACTIVE ✅         │
│                     │          │                     │
│                     │          │  HA Manager         │
│                     │          │  1. Detected (15s)  │
│                     │          │  2. Took Over (0s)  │
└─────────────────────┘          └─────────────────────┘
                                  Total: ~15 seconds
```

---

## 📋 Requirements

- **OS**: Ubuntu 20.04+ or similar Linux
- **Python**: 3.8 or higher
- **Solana**: Agave/Solana validator software
- **Network**: Stable connectivity between validators
- **RPC**: Both validators must expose RPC endpoints
- **Init State**: validator should always start in passive mode. HA will promote passive node as needed.

---

## 🚀 Quick Start

### 1. Install

```bash
# Install dependencies
pip install -r requirements.txt

# Create directory
sudo mkdir -p /opt/solana-ha/{config,logs}
cd /opt/solana-ha

# Copy files
cp solana_validator_ha.py /opt/solana-ha/
cp config.example.yaml /opt/solana-ha/config/config.yaml
chmod +x /opt/solana-ha/solana_validator_ha.py
```

### 2. Use Your Existing Validator Identities

**IMPORTANT**: Use your EXISTING validator identities (already bound to vote account with stake).

```bash
# Copy your existing PRIMARY validator identity
sudo cp /home/sol/validator-keypair.json /opt/solana-ha/config/active-identity.json

# Copy your BACKUP identity OR create new (needs stake delegation):
solana-keygen new -o /opt/solana-ha/config/passive-identity.json

```

### 3. Configure

Edit `config/config.yaml`:

```yaml
validator:
  name: "validator1"
  public_ip: "1.1.1.1"

validator_identities:
  active_keypair: "/opt/solana-ha/config/active-identity.json"
  passive_keypair: "/opt/solana-ha/config/passive-identity.json"

failover:
  dry_run: true  # Test mode first!
  peers:
    validator2:
      ip: "2.2.2.2"
```

### 4. Test

```bash
# Run in test mode
./solana_validator_ha.py config/config.yaml

# Should see:
# INFO - 🔵 Self: PASSIVE ✅ | Peers: 1/1 healthy, 1 active | ✅ LEADER PRESENT
```

### 5. Deploy

```bash
# Install systemd service
sudo cp solana-ha.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable solana-ha
sudo systemctl start solana-ha

# Check status
sudo systemctl status solana-ha
```

---

## ⚙️ Key Features Explained

### Automatic Failover

Detects when active validator fails and automatically promotes passive:

```
Detection Time: 15 seconds (3 polls × 5s)
Transition:     0 seconds (hot swap, first passive node in health state will take over.)
Total:          15 seconds
```

### Health Monitoring

**Peer Health:**
- ✅ Present in gossip
- ✅ Has correct identity
- ✅ Slot progressing (not stuck)
- ✅ Not too far behind (<300 slots)

**Self Health (before takeover):**
- ✅ In gossip (reachable)
- ✅ RPC responding
- ✅ Caught up with cluster
- ✅ Ready to serve

### Split-Brain Prevention

Ensures only ONE validator is active: The passive node that is healthy and has biggest IP will take over.
No race condition, no split-brain.

---

## 📊 Monitoring

### Prometheus Metrics (this part is not tested)

Available at `http://localhost:9099/metrics`:

```
# Current role (0=passive, 1=active)
solana_validator_ha_self_role{validator_name="validator1"} 1

# Has leader (0=no, 1=yes)
solana_validator_ha_has_leader{validator_name="validator1"} 1

# Peer count
solana_validator_ha_peer_count{validator_name="validator1"} 1

# Failover counter
solana_validator_ha_failover_total{from_role="passive",to_role="active"} 3
```

### Logs

```bash
# Live monitoring
sudo journalctl -u solana-ha -f

# Status examples:
🔵 Self: PASSIVE ✅ | Peers: 1/1 healthy, 1 active | ✅ LEADER PRESENT
🟢 Self: ACTIVE ✅ | Peers: 1/1 healthy, 0 active | ✅ LEADER PRESENT
🔴 Self: PASSIVE ⚠️  | Peers: 0/1 healthy, 0 active | ⚠️  NO LEADER (2/3)
```

---

## 🧪 Testing

### Dry-Run Mode

Test without executing real failovers:

```yaml
failover:
  dry_run: true
```

Output:
```
INFO - [DRY RUN] Would execute: solana-validator set-identity ...
INFO - [DRY RUN] Takeover complete (simulated)
```

### Manual Failover Test

1. Start both validators (one active, one passive)
2. Stop active: `sudo systemctl stop agave-validator`
3. Watch passive validator take over (~15 seconds)
4. Restart original: `sudo systemctl start agave-validator`
5. Should return as passive

---

## 🔧 Configuration

### Basic Configuration

```yaml
validator:
  name: "validator1"
  public_ip: "1.1.1.1"

validator_identities:
  active_keypair: "/opt/solana-ha/config/active-identity.json"
  passive_keypair: "/opt/solana-ha/config/passive-identity.json"

local_rpc:
  rpc_urls:
    - "http://127.0.0.1:8899"

cluster_rpc:
  rpc_urls:
    - "https://api.mainnet-beta.solana.com"

failover:
  dry_run: false
  poll_interval_duration: "5s"
  leaderless_samples_threshold: 3
  stuck_samples_threshold: 3
  max_slot_lag: 300
  
  peers:
    validator2:
      ip: "2.2.2.2"
  
  active_command:
    command: "/home/sol/solana/solana-validator"
    args: ["set-identity", "/opt/solana-ha/config/active-identity.json"]
    must_succeed: true
  
  passive_command:
    command: "/home/sol/solana/solana-validator"
    args: ["set-identity", "/opt/solana-ha/config/passive-identity.json"]
    must_succeed: true

prometheus_port: 9101
log_level: "INFO"
```

### Tuning

| Parameter | Default | Conservative | Aggressive |
|-----------|---------|--------------|------------|
| poll_int  | 5s      | 10s          | 3s         |
| threshold | 3       | 5            | 2          |
| slot_lag  | 300     | 500          | 150        |

---

## 🛡️ Production Best Practices

1. **Always start validators with passive identity**
   ```ini
   ExecStart=/home/sol/solana/solana-validator \
     --identity /opt/solana-ha/config/passive-identity.json
   ```

2. **Test in dry-run mode first**
   ```yaml
   dry_run: true
   ```

3. **Monitor Prometheus metrics**
   - Set up alerts for failover events
   - Track peer health
   - Monitor slot lag

4. **Secure identity keypairs**
   ```bash
   chmod 600 /opt/solana-ha/config/*.json
   chown sol:sol /opt/solana-ha/config/*.json
   ```

5. **Enable systemd auto-restart**
   ```ini
   [Service]
   Restart=always
   RestartSec=10
   ```

---

## 🐛 Troubleshooting

### Common Issues

**"Cannot get our own slot"**
```bash
# Test local RPC
curl -X POST http://127.0.0.1:8899 \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"getSlot"}'
```

**"Peer not found in gossip"**
```bash
# Test connectivity
ping PEER_IP
curl http://PEER_IP:8899
```

**Service won't start**
```bash
# Check logs
sudo journalctl -u solana-ha -n 50

# Test manually
./solana_validator_ha.py config/config.yaml
```

---

## 📊 Project Status

- **Version**: 1.0.0
- **Status**: ✅ Production Ready
- **Tested**: Solana Mainnet
- **Python**: 3.8+
- **Last Updated**: December 26, 2025

---

## 🤝 Contributing

Contributions welcome! Please:
1. Test thoroughly in dry-run mode
2. Update documentation
3. Follow existing code style
4. Add tests for new features

---


## 🙏 Acknowledgments

Built for the Solana validator community.

Thanks to:
- Solana Labs for Agave validator software
- Sol Stratigies's HA code.


---

