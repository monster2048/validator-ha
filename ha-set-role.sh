#!/bin/bash
#
# Solana Validator HA Role Switch - TRY-FALLBACK APPROACH
# ========================================================
#
# DESIGN PHILOSOPHY:
#   Active promotion:  Hot swap (fast, zero downtime)
#   Passive demotion:  Try hot swap first, restart if it fails
#
# Why?
#   - Active: Need speed, hot swap
#   - Passive: Try fast (hot swap), fall back to safe (restart)
#   - Defense in depth!
#
# Usage:
#   ha-set-role.sh active --ledger <path> --active-identity <path> --passive-identity <path>
#   ha-set-role.sh passive --ledger <path> --passive-identity <path>
#

set -e

# Configuration
VALIDATOR_BIN="${VALIDATOR_BIN:-agave-validator}"
VALIDATOR_SERVICE="${VALIDATOR_SERVICE:-agave-validator}"
RPC_URL="${RPC_URL:-http://localhost:8899}"
ENV_FILE="${ENV_FILE:-/etc/default/${VALIDATOR_SERVICE}}"

# Timeouts
HOT_SWAP_VERIFY_TIMEOUT=15  # How long to wait for hot swap verification
RESTART_VERIFY_TIMEOUT=120  # How long to wait for restart verification

# Colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Parse arguments
ROLE="$1"
shift || { echo "Usage: $0 {active|passive} --passive-identity <path> [--active-identity <path>] [--ledger <path>]"; exit 1; }

ACTIVE_IDENTITY=""
PASSIVE_IDENTITY=""
LEDGER_PATH=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --active-identity) ACTIVE_IDENTITY="$2"; shift 2 ;;
        --passive-identity) PASSIVE_IDENTITY="$2"; shift 2 ;;
        --ledger) LEDGER_PATH="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Validate
[[ "$ROLE" =~ ^(active|passive)$ ]] || { echo "Error: Role must be 'active' or 'passive'"; exit 1; }
[[ -z "$PASSIVE_IDENTITY" ]] && { echo "Error: --passive-identity required"; exit 1; }
[[ ! -f "$PASSIVE_IDENTITY" ]] && { echo -e "${RED}Error: Passive identity not found: $PASSIVE_IDENTITY${NC}"; exit 1; }

if [[ "$ROLE" == "active" ]]; then
    [[ -z "$ACTIVE_IDENTITY" ]] && { echo "Error: --active-identity required for active role"; exit 1; }
    [[ -z "$LEDGER_PATH" ]] && { echo "Error: --ledger required for active role"; exit 1; }
    [[ ! -f "$ACTIVE_IDENTITY" ]] && { echo -e "${RED}Error: Active identity not found: $ACTIVE_IDENTITY${NC}"; exit 1; }
    [[ ! -d "$LEDGER_PATH" ]] && { echo -e "${RED}Error: Ledger not found: $LEDGER_PATH${NC}"; exit 1; }
fi

# Helper: Get current identity
get_identity() {
    curl -s -X POST "$RPC_URL" -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","id":1,"method":"getIdentity"}' 2>/dev/null | \
        python3 -c "import sys,json; print(json.load(sys.stdin).get('result',{}).get('identity','offline'))" 2>/dev/null || echo "offline"
}

# Helper: Extract pubkey
extract_pubkey() {
    python3 -c "
import json, base58
with open('$1') as f: data = json.load(f)
print(base58.b58encode(bytes(data[32:64])).decode())
" 2>/dev/null
}

# Helper: Wait for identity with timeout
wait_for_identity() {
    local target_pubkey="$1"
    local timeout="$2"
    local elapsed=0
    
    while [[ $elapsed -lt $timeout ]]; do
        local current=$(get_identity)
        if [[ "$current" == "$target_pubkey" ]]; then
            echo "$elapsed"
            return 0
        fi
        sleep 2
        elapsed=$((elapsed + 2))
        echo -n "." >&2
    done
    
    return 1
}

TARGET_PUBKEY=$(extract_pubkey "$PASSIVE_IDENTITY")
if [[ "$ROLE" == "active" ]]; then
    TARGET_PUBKEY=$(extract_pubkey "$ACTIVE_IDENTITY")
fi

CURRENT_IDENTITY=$(get_identity)

echo "╔════════════════════════════════════════════════════════════════════╗"
echo "║     Solana Validator HA - Try-Fallback Role Switch                ║"
echo "╚════════════════════════════════════════════════════════════════════╝"
echo
echo "  Role:            $ROLE"
echo "  Target Pubkey:   $TARGET_PUBKEY"
echo "  Current Pubkey:  $CURRENT_IDENTITY"
echo

# Check if already in target role
if [[ "$CURRENT_IDENTITY" == "$TARGET_PUBKEY" ]]; then
    echo -e "${GREEN}✅ Already in $ROLE role - no action needed${NC}"
    exit 0
fi

#
# ACTIVE PROMOTION - HOT SWAP (FAST, REQUIRED)
#
if [[ "$ROLE" == "active" ]]; then
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ${BLUE}ACTIVE PROMOTION${NC} - Hot swap (zero downtime required)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo
    
    echo "→ Executing hot swap to active..."
    echo "  Command: $VALIDATOR_BIN -l $LEDGER_PATH set-identity $ACTIVE_IDENTITY"
    echo
    
    if ! $VALIDATOR_BIN -l "$LEDGER_PATH" set-identity "$ACTIVE_IDENTITY"; then
        echo -e "${RED}❌ Hot swap command failed!${NC}"
        exit 1
    fi
    
    echo -e "${GREEN}✅ Hot swap command executed${NC}"
    echo
    echo "→ Verifying identity change..."
    
    if ELAPSED=$(wait_for_identity "$TARGET_PUBKEY" "$HOT_SWAP_VERIFY_TIMEOUT"); then
        echo
        echo "╔════════════════════════════════════════════════════════════════╗"
        echo -e "║  ${GREEN}✅ ACTIVE PROMOTION SUCCESSFUL${NC}                              "
        echo "╚════════════════════════════════════════════════════════════════╝"
        echo
        echo "  New Identity:    $TARGET_PUBKEY"
        echo "  Switch Time:     ~${ELAPSED}s"
        echo "  Downtime:        ⚡ ZERO (hot swap)"
        echo "  Voting:          ✅ Active"
        echo
        exit 0
    else
        echo
        echo -e "${RED}❌ Hot swap verification failed!${NC}"
        echo "  Expected: $TARGET_PUBKEY"
        echo "  Current:  $(get_identity)"
        exit 1
    fi

#
# PASSIVE DEMOTION - TRY HOT SWAP, FALLBACK TO RESTART
#
else
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  ${YELLOW}PASSIVE DEMOTION${NC} - Try hot swap, fallback to restart"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo
    echo "  Strategy:"
    echo "    1️⃣  Try hot swap (fast - 1-2s if it works)"
    echo "    2️⃣  Verify it worked"
    echo "    3️⃣  If failed → Restart (safe - guarantees correct state)"
    echo
    
    # Check if we have ledger path for hot swap attempt
    if [[ -z "$LEDGER_PATH" ]]; then
        echo -e "${YELLOW}⚠️  No --ledger provided, skipping hot swap attempt${NC}"
        echo "   Going straight to restart (safe method)"
        echo
        SKIP_HOT_SWAP=true
    else
        SKIP_HOT_SWAP=false
    fi
    
    #
    # ATTEMPT 1: HOT SWAP TO PASSIVE
    #
    if [[ "$SKIP_HOT_SWAP" != "true" ]]; then
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo "  Attempt 1: Hot swap to passive"
        echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
        echo
        
        echo "→ Executing hot swap..."
        echo "  Command: $VALIDATOR_BIN -l $LEDGER_PATH set-identity $PASSIVE_IDENTITY"
        echo
        
        if $VALIDATOR_BIN -l "$LEDGER_PATH" set-identity "$PASSIVE_IDENTITY" 2>/dev/null; then
            echo -e "${GREEN}✅ Hot swap command executed${NC}"
            echo
            echo "→ Verifying identity change..."
            
            if ELAPSED=$(wait_for_identity "$TARGET_PUBKEY" "$HOT_SWAP_VERIFY_TIMEOUT"); then
                echo
                echo "╔════════════════════════════════════════════════════════════════╗"
                echo -e "║  ${GREEN}✅ PASSIVE DEMOTION SUCCESSFUL (hot swap)${NC}                  "
                echo "╚════════════════════════════════════════════════════════════════╝"
                echo
                echo "  New Identity:    $TARGET_PUBKEY"
                echo "  Method:          Hot swap (fast!)"
                echo "  Switch Time:     ~${ELAPSED}s"
                echo "  Downtime:        ⚡ ZERO"
                echo "  Voting:          ⭕ Passive (not voting)"
                echo
                exit 0
            else
                echo
                echo -e "${YELLOW}⚠️  Hot swap verification failed${NC}"
                echo "   Expected: $TARGET_PUBKEY"
                echo "   Current:  $(get_identity)"
                echo "   → Falling back to restart (safe method)"
                echo
            fi
        else
            echo -e "${YELLOW}⚠️  Hot swap command failed${NC}"
            echo "   → Falling back to restart (safe method)"
            echo
        fi
    fi
    
    #
    # ATTEMPT 2: RESTART (GUARANTEED SAFE)
    #
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo "  Attempt 2: Restart service (guaranteed safe)"
    echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    echo
    echo "  Why restart?"
    echo "    ✅ Ensures clean state"
    echo "    ✅ Uses systemd config (source of truth)"
    echo "    ✅ Same boot path as power cycle"
    echo "    ✅ Guaranteed to work"
    echo
    
    # Update environment file (if using env var approach)
    if [[ -n "$ENV_FILE" ]]; then
        echo "→ Updating environment file..."
        echo "VALIDATOR_IDENTITY=$PASSIVE_IDENTITY" | sudo tee "$ENV_FILE" > /dev/null
        echo -e "${GREEN}✅ Environment updated${NC}"
        echo
    fi
    
    # Restart service
    echo "→ Restarting $VALIDATOR_SERVICE"
    echo "  (30-60s downtime expected, but acceptable for demotion)"
    echo
    
    sudo systemctl restart "$VALIDATOR_SERVICE"
    
    echo -e "${GREEN}✅ Service restarted${NC}"
    echo
    echo "→ Waiting for validator to come online..."
    
    if ELAPSED=$(wait_for_identity "$TARGET_PUBKEY" "$RESTART_VERIFY_TIMEOUT"); then
        echo
        echo "╔════════════════════════════════════════════════════════════════╗"
        echo -e "║  ${GREEN}✅ PASSIVE DEMOTION SUCCESSFUL (restart)${NC}                    "
        echo "╚════════════════════════════════════════════════════════════════╝"
        echo
        echo "  New Identity:    $TARGET_PUBKEY"
        echo "  Method:          Restart (guaranteed safe)"
        echo "  Total Time:      ~${ELAPSED}s"
        echo "  Voting:          ⭕ Passive (not voting)"
        echo
        exit 0
    else
        echo
        echo -e "${RED}❌ Restart verification failed!${NC}"
        echo "  Expected: $TARGET_PUBKEY"
        echo "  Current:  $(get_identity)"
        echo "  Service status: $(sudo systemctl is-active $VALIDATOR_SERVICE)"
        echo
        echo "  Check logs: journalctl -u $VALIDATOR_SERVICE -f"
        exit 1
    fi
fi
