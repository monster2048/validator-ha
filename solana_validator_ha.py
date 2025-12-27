#!/usr/bin/env python3
"""
Solana Validator High Availability Manager
A Python-based HA solution for Solana validators using gossip-based peer discovery
"""

import asyncio
import json
import logging
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

import aiohttp
import yaml
from prometheus_client import Counter, Gauge, Info, start_http_server


# ============================================================================
# Configuration and Data Models
# ============================================================================

class ValidatorRole(Enum):
    """Validator role types"""
    ACTIVE = "active"
    PASSIVE = "passive"
    UNKNOWN = "unknown"


class ValidatorStatus(Enum):
    """Validator health status"""
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    MISSING = "missing"
    UNREACHABLE = "unreachable"
    UNKNOWN = "unknown"


@dataclass
class PeerConfig:
    """Configuration for a peer validator
    
    IP-ONLY MATCHING: Simple and reliable.
    We use IP address to identify peers in gossip.
    """
    name: str
    ip: str  # Required: IP address for matching
    identity: Optional[str] = None  # Optional: For documentation/reference only
    
    def __post_init__(self):
        """Validate that IP is provided"""
        if not self.ip:
            raise ValueError(f"Peer {self.name} must have 'ip' specified")


@dataclass
class CommandConfig:
    """Configuration for a command to execute"""
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)


@dataclass
class HookConfig:
    """Configuration for a hook"""
    name: str
    command: str
    args: List[str] = field(default_factory=list)
    env: Dict[str, str] = field(default_factory=dict)
    must_succeed: bool = False


@dataclass
class FailoverConfig:
    """Failover configuration"""
    dry_run: bool = False
    poll_interval_duration: float = 5.0
    leaderless_samples_threshold: int = 3
    stuck_samples_threshold: int = 3  # How many polls with no slot progress = stuck
    max_slot_lag: int = 300  # How many slots behind cluster = too far behind
    peers: Dict[str, PeerConfig] = field(default_factory=dict)
    active_command: Optional[CommandConfig] = None
    passive_command: Optional[CommandConfig] = None
    active_pre_hooks: List[HookConfig] = field(default_factory=list)
    active_post_hooks: List[HookConfig] = field(default_factory=list)
    passive_pre_hooks: List[HookConfig] = field(default_factory=list)
    passive_post_hooks: List[HookConfig] = field(default_factory=list)


@dataclass
class ValidatorIdentities:
    """Validator identity keypair paths"""
    active: Path
    passive: Path
    active_pubkey: Optional[str] = None
    passive_pubkey: Optional[str] = None


@dataclass
class Config:
    """Main configuration object"""
    validator_name: str
    validator_rpc_url: str
    validator_identities: ValidatorIdentities
    cluster_name: str = "unknown"  # Optional - only used for display
    cluster_rpc_urls: List[str] = field(default_factory=list)  # Optional - defaults to local RPC
    failover: FailoverConfig = None
    prometheus_port: int = 9099
    prometheus_labels: Dict[str, str] = field(default_factory=dict)
    log_level: str = "INFO"
    log_format: str = "text"
    public_ip: Optional[str] = None  # Manual IP override
    public_ip_service_urls: List[str] = field(default_factory=lambda: [
        "https://api.ipify.org",
        "https://checkip.amazonaws.com",
        "https://icanhazip.com"
    ])


@dataclass
class PeerState:
    """State of a peer validator"""
    name: str
    ip: str
    pubkey: Optional[str] = None
    role: ValidatorRole = ValidatorRole.UNKNOWN
    status: ValidatorStatus = ValidatorStatus.MISSING
    last_seen: Optional[datetime] = None
    gossip_port: Optional[int] = None
    rpc_port: Optional[int] = None
    version: Optional[str] = None
    current_slot: Optional[int] = None  # Current slot height
    slot_history: List[int] = field(default_factory=list)  # Last N slot heights
    is_progressing: bool = True  # Whether slot is increasing


@dataclass
class ClusterState:
    """Current state of the validator cluster"""
    peers: Dict[str, PeerState] = field(default_factory=dict)
    has_leader: bool = False
    leaderless_sample_count: int = 0
    last_poll_time: Optional[datetime] = None
    gossip_unavailable: bool = False  # True when no gossip data available


# ============================================================================
# Prometheus Metrics
# ============================================================================

class Metrics:
    """Prometheus metrics"""
    
    def __init__(self, static_labels: Dict[str, str], registry=None):
        self.static_labels = static_labels
        self.registry = registry  # If None, uses default registry
        
        # Metadata
        self.metadata = Info(
            'solana_validator_ha_metadata',
            'Validator metadata',
            labelnames=list(static_labels.keys()) + ['validator_name', 'public_ip', 'role', 'status'],
            registry=self.registry
        )
        
        # Peer count
        self.peer_count = Gauge(
            'solana_validator_ha_peer_count',
            'Number of peers visible in gossip',
            labelnames=list(static_labels.keys()) + ['validator_name'],
            registry=self.registry
        )
        
        # Self in gossip
        self.self_in_gossip = Gauge(
            'solana_validator_ha_self_in_gossip',
            'Whether this validator appears in gossip',
            labelnames=list(static_labels.keys()) + ['validator_name'],
            registry=self.registry
        )
        
        # Gossip availability
        self.gossip_available = Gauge(
            'solana_validator_ha_gossip_available',
            'Whether gossip data is available (1=available, 0=unavailable)',
            labelnames=list(static_labels.keys()) + ['validator_name'],
            registry=self.registry
        )
        
        # Failover status
        self.failover_status = Gauge(
            'solana_validator_ha_failover_status',
            'Current failover status',
            labelnames=list(static_labels.keys()) + ['validator_name', 'event_type'],
            registry=self.registry
        )
        
        # Failover events counter
        self.failover_events = Counter(
            'solana_validator_ha_failover_events_total',
            'Total number of failover events',
            labelnames=list(static_labels.keys()) + ['validator_name', 'event_type', 'result'],
            registry=self.registry
        )
        
        # Command execution
        self.command_executions = Counter(
            'solana_validator_ha_command_executions_total',
            'Total command executions',
            labelnames=list(static_labels.keys()) + ['validator_name', 'command_type', 'result'],
            registry=self.registry
        )


# ============================================================================
# Utility Functions
# ============================================================================

def setup_logging(level: str, format_type: str) -> logging.Logger:
    """Configure logging"""
    logger = logging.getLogger('solana_ha')
    logger.setLevel(getattr(logging, level.upper()))
    
    handler = logging.StreamHandler(sys.stdout)
    
    if format_type == "json":
        # Simple JSON-like formatting
        formatter = logging.Formatter(
            '{"time":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}'
        )
    else:
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
    
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    
    return logger


def load_keypair_pubkey(keypair_path: Path) -> str:
    """Load public key from Solana keypair file"""
    try:
        with open(keypair_path, 'r') as f:
            keypair_data = json.load(f)
            # Solana keypair is a 64-byte array, first 32 bytes are secret, last 32 are public
            if isinstance(keypair_data, list) and len(keypair_data) == 64:
                # Convert to base58 using simple approach (in production use base58 library)
                import base58
                return base58.b58encode(bytes(keypair_data[32:64])).decode('ascii')
            else:
                raise ValueError("Invalid keypair format")
    except Exception as e:
        raise ValueError(f"Failed to load keypair from {keypair_path}: {e}")


def render_template(template: str, context: Dict[str, str]) -> str:
    """Simple template rendering for Go-style templates"""
    result = template
    for key, value in context.items():
        result = result.replace(f"{{{{ .{key} }}}}", value)
    return result


# ============================================================================
# Solana RPC Client
# ============================================================================

class SolanaRPCClient:
    """Async Solana RPC client"""
    
    def __init__(self, rpc_urls: List[str], logger: logging.Logger):
        self.rpc_urls = rpc_urls
        self.current_url_index = 0
        self.logger = logger
        self.session: Optional[aiohttp.ClientSession] = None
    
    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.session:
            await self.session.close()
    
    def _get_next_url(self) -> str:
        """Get next RPC URL (round-robin)"""
        url = self.rpc_urls[self.current_url_index]
        self.current_url_index = (self.current_url_index + 1) % len(self.rpc_urls)
        return url
    
    async def _call(self, method: str, params: List = None) -> Dict:
        """Make RPC call with retry logic"""
        if params is None:
            params = []
        
        last_error = None
        for attempt in range(len(self.rpc_urls)):
            url = self._get_next_url()
            try:
                payload = {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": method,
                    "params": params
                }
                
                async with self.session.post(url, json=payload) as response:
                    data = await response.json()
                    
                    if "error" in data:
                        raise Exception(f"RPC error: {data['error']}")
                    
                    return data.get("result", {})
            
            except Exception as e:
                last_error = e
                self.logger.warning(f"RPC call failed on {url}: {e}")
                continue
        
        raise Exception(f"All RPC endpoints failed. Last error: {last_error}")
    
    async def get_health(self) -> str:
        """Get validator health"""
        try:
            result = await self._call("getHealth")
            return "ok" if result == "ok" else "unhealthy"
        except:
            return "unhealthy"
    
    async def get_identity(self) -> Optional[str]:
        """Get validator identity pubkey"""
        try:
            result = await self._call("getIdentity")
            return result.get("identity")
        except Exception as e:
            self.logger.error(f"Failed to get identity: {e}")
            return None
    
    async def get_cluster_nodes(self) -> List[Dict]:
        """Get cluster nodes from gossip"""
        try:
            return await self._call("getClusterNodes")
        except Exception as e:
            self.logger.error(f"Failed to get cluster nodes: {e}")
            return []
    
    async def get_vote_accounts(self) -> Dict:
        """Get vote accounts to determine voting status"""
        try:
            return await self._call("getVoteAccounts")
        except Exception as e:
            self.logger.error(f"Failed to get vote accounts: {e}")
            return {"current": [], "delinquent": []}
    
    async def get_slot(self) -> Optional[int]:
        """Get current slot height"""
        try:
            result = await self._call("getSlot")
            return result
        except Exception as e:
            self.logger.error(f"Failed to get slot: {e}")
            return None


# ============================================================================
# Main HA Manager
# ============================================================================

class SolanaValidatorHA:
    """Main HA manager class"""
    
    def __init__(self, config: Config, test_registry=None):
        self.config = config
        self.logger = setup_logging(config.log_level, config.log_format)
        
        # Use test registry if provided (for testing), otherwise use default
        registry = test_registry if test_registry is not None else None
        self.metrics = Metrics(config.prometheus_labels, registry=registry)
        
        self.cluster_state = ClusterState()
        self.self_state = PeerState(
            name=config.validator_name,
            ip="",  # Will be determined
            role=ValidatorRole.UNKNOWN,
            status=ValidatorStatus.UNKNOWN
        )
        self.local_rpc = SolanaRPCClient([config.validator_rpc_url], self.logger)
        self.cluster_rpc = SolanaRPCClient(config.cluster_rpc_urls, self.logger)
        self.running = False
        self.poll_count = 0  # Track number of polls for periodic logging
        
    def _print_startup_summary(self):
        """Print configuration summary at startup"""
        self.logger.info("=" * 70)
        self.logger.info("CONFIGURATION SUMMARY")
        self.logger.info("=" * 70)
        self.logger.info(f"Validator Name:        {self.config.validator_name}")
        self.logger.info(f"Public IP:             {self.self_state.ip}")
        self.logger.info(f"Local RPC:             {self.config.validator_rpc_url}")
        self.logger.info(f"Cluster:               {self.config.cluster_name}")
        self.logger.info(f"Active Identity:       {self.config.validator_identities.active_pubkey}")
        self.logger.info(f"Passive Identity:      {self.config.validator_identities.passive_pubkey}")
        self.logger.info(f"Poll Interval:         {self.config.failover.poll_interval_duration}s")
        self.logger.info(f"Leaderless Threshold:  {self.config.failover.leaderless_samples_threshold} samples")
        self.logger.info(f"Stuck Threshold:       {self.config.failover.stuck_samples_threshold} samples")
        self.logger.info(f"Max Slot Lag:          {self.config.failover.max_slot_lag} slots")
        self.logger.info(f"Election Method:       IP-based priority (deterministic)")
        self.logger.info(f"HA Peers:              {len(self.config.failover.peers)}")
        for peer_name in self.config.failover.peers.keys():
            self.logger.info(f"  → {peer_name}")
        self.logger.info(f"Dry Run Mode:          {'YES (safe mode)' if self.config.failover.dry_run else 'NO (production)'}")
        self.logger.info(f"Prometheus Metrics:    http://localhost:{self.config.prometheus_port}/metrics")
        self.logger.info("=" * 70)
        self.logger.info("Starting monitoring loop... (Ctrl+C to stop)")
        self.logger.info("=" * 70)
    
    async def start(self):
        """Start the HA manager"""
        self.logger.info(f"Starting Solana Validator HA Manager for {self.config.validator_name}")
        
        # Start Prometheus metrics server
        start_http_server(self.config.prometheus_port)
        self.logger.info(f"Prometheus metrics server started on port {self.config.prometheus_port}")
        
        # Determine public IP
        self.self_state.ip = await self._get_public_ip()
        self.logger.info(f"Public IP: {self.self_state.ip}")
        
        # Load identity pubkeys
        try:
            import base58
            self.config.validator_identities.active_pubkey = load_keypair_pubkey(
                self.config.validator_identities.active
            )
            self.config.validator_identities.passive_pubkey = load_keypair_pubkey(
                self.config.validator_identities.passive
            )
            self.logger.info(f"Active identity: {self.config.validator_identities.active_pubkey}")
            self.logger.info(f"Passive identity: {self.config.validator_identities.passive_pubkey}")
        except Exception as e:
            self.logger.error(f"Failed to load identity pubkeys: {e}")
            self.logger.info("Install base58: pip install base58")
            sys.exit(1)
        
        # Print configuration summary
        self._print_startup_summary()
        
        self.running = True
        
        # Main loop
        async with self.local_rpc, self.cluster_rpc:
            while self.running:
                try:
                    await self._poll_and_evaluate()
                    await asyncio.sleep(self.config.failover.poll_interval_duration)
                except KeyboardInterrupt:
                    self.logger.info("Received shutdown signal")
                    self.running = False
                except Exception as e:
                    self.logger.error(f"Error in main loop: {e}", exc_info=True)
                    await asyncio.sleep(self.config.failover.poll_interval_duration)
    
    async def _get_public_ip(self) -> str:
        """Determine public IP address with multiple fallback methods"""
        
        # Method 1: Use manually configured IP if provided
        if self.config.public_ip:
            self.logger.info(f"Using manually configured IP: {self.config.public_ip}")
            return self.config.public_ip
        
        # Method 2: Try public IP services
        if self.config.public_ip_service_urls:
            self.logger.debug("Trying to detect public IP from external services...")
            async with aiohttp.ClientSession() as session:
                for url in self.config.public_ip_service_urls:
                    try:
                        async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as response:
                            ip = (await response.text()).strip()
                            self.logger.info(f"Got public IP {ip} from {url}")
                            return ip
                    except Exception as e:
                        self.logger.debug(f"Failed to get IP from {url}: {e}")
        
        # Method 3: Try to get IP from network interfaces
        self.logger.warning("External IP services unreachable, trying to detect from network interfaces...")
        try:
            import socket
            
            # Try to connect to a public DNS server to determine which interface would be used
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                # Connect to Google DNS (doesn't actually send data)
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
                
                if local_ip and local_ip != "127.0.0.1":
                    self.logger.warning(f"Using local network IP: {local_ip}")
                    self.logger.warning("This may not be your public IP. Consider setting 'public_ip' in config.")
                    return local_ip
        except Exception as e:
            self.logger.debug(f"Failed to detect IP from network interfaces: {e}")
        
        # Method 4: Try to get from RPC cluster nodes (see if we're listed)
        self.logger.warning("Trying to find IP from cluster gossip...")
        try:
            nodes = await self.cluster_rpc.get_cluster_nodes()
            our_identity = await self.local_rpc.get_identity()
            
            if our_identity:
                for node in nodes:
                    if node.get("pubkey") == our_identity:
                        # Extract IP from gossip address
                        gossip = node.get("gossip", "")
                        if gossip and ":" in gossip:
                            ip = gossip.split(":")[0]
                            self.logger.info(f"Found our IP in cluster gossip: {ip}")
                            return ip
        except Exception as e:
            self.logger.debug(f"Failed to find IP from gossip: {e}")
        
        # If all methods fail, provide helpful error message
        error_msg = """
Failed to determine public IP address. Please use one of these solutions:

1. RECOMMENDED: Add 'public_ip' to your config.yaml:
   
   public_ip: "YOUR.PUBLIC.IP.HERE"

2. Or ensure external IP services are reachable:
   - https://api.ipify.org
   - https://checkip.amazonaws.com
   - https://icanhazip.com

3. Or start the validator so gossip can be queried

To manually find your public IP:
   curl -s https://api.ipify.org
   OR
   dig +short myip.opendns.com @resolver1.opendns.com
"""
        self.logger.error(error_msg)
        raise Exception("Failed to determine public IP from all services")
    
    async def _poll_and_evaluate(self):
        """Main polling and evaluation logic"""
        self.poll_count += 1
        
        self.logger.debug("Polling cluster state...")
        
        # 1. Update self state
        await self._update_self_state()
        
        # 2. Update peer states
        await self._update_peer_states()
        
        # 3. Evaluate failover decision
        await self._evaluate_failover()
        
        # 4. Log periodic status update (every poll for visibility)
        self._log_status_update()
        
        # 5. Show detailed summary every 10 polls
        if self.poll_count % 10 == 0:
            self._log_periodic_summary()
    
    def _log_status_update(self):
        """Log current status for monitoring visibility"""
        # Count peer states
        healthy_peers = sum(1 for p in self.cluster_state.peers.values() if p.status == ValidatorStatus.HEALTHY)
        total_peers = len(self.cluster_state.peers)
        active_peers = sum(1 for p in self.cluster_state.peers.values() if p.role == ValidatorRole.ACTIVE)
        
        # Build status message
        role_icon = "🟢" if self.self_state.role == ValidatorRole.ACTIVE else "🔵" if self.self_state.role == ValidatorRole.PASSIVE else "⚪"
        status_icon = "✅" if self.self_state.status == ValidatorStatus.HEALTHY else "❌"
        
        leader_status = "ACTIVE LEADER PRESENT" if self.cluster_state.has_leader else f"⚠️  NO LEADER ({self.cluster_state.leaderless_sample_count}/{self.config.failover.leaderless_samples_threshold})"
        
        status_msg = (
            f"{role_icon} Self: {self.self_state.role.value.upper()} {status_icon} "
            f"| Peers: {healthy_peers}/{total_peers} healthy, {active_peers} active "
            f"| {leader_status}"
        )
        
        # Use INFO level so it's always visible (not just in debug mode)
        self.logger.info(status_msg)
        
        # Additional detail in debug mode
        if self.logger.level <= logging.DEBUG:
            for name, peer in self.cluster_state.peers.items():
                peer_status = f"  → {name}: {peer.role.value} / {peer.status.value}"
                if peer.last_seen:
                    peer_status += f" (seen: {peer.last_seen.strftime('%H:%M:%S')})"
                self.logger.debug(peer_status)
    
    def _log_periodic_summary(self):
        """Log detailed summary every N polls"""
        uptime_seconds = self.poll_count * self.config.failover.poll_interval_duration
        uptime_minutes = int(uptime_seconds / 60)
        uptime_hours = int(uptime_minutes / 60)
        uptime_str = f"{uptime_hours}h {uptime_minutes % 60}m" if uptime_hours > 0 else f"{uptime_minutes}m"
        
        self.logger.info("─" * 70)
        self.logger.info(f"📊 PERIODIC SUMMARY (Poll #{self.poll_count}, Uptime: {uptime_str})")
        self.logger.info("─" * 70)
        self.logger.info(f"Self Status:    {self.self_state.role.value.upper()} / {self.self_state.status.value}")
        self.logger.info(f"Cluster State:  {'ACTIVE LEADER PRESENT ✅' if self.cluster_state.has_leader else f'NO LEADER ⚠️  (leaderless count: {self.cluster_state.leaderless_sample_count})'}")
        
        # Peer summary
        healthy = sum(1 for p in self.cluster_state.peers.values() if p.status == ValidatorStatus.HEALTHY)
        active = sum(1 for p in self.cluster_state.peers.values() if p.role == ValidatorRole.ACTIVE)
        passive = sum(1 for p in self.cluster_state.peers.values() if p.role == ValidatorRole.PASSIVE)
        total = len(self.cluster_state.peers)
        
        self.logger.info(f"Peers:          {total} configured, {healthy} healthy, {active} active, {passive} passive")
        
        # List peers with details
        for name, peer in self.cluster_state.peers.items():
            status_icon = "✅" if peer.status == ValidatorStatus.HEALTHY else "❌"
            role_icon = "🟢" if peer.role == ValidatorRole.ACTIVE else "🔵" if peer.role == ValidatorRole.PASSIVE else "⚪"
            last_seen = peer.last_seen.strftime("%H:%M:%S") if peer.last_seen else "never"
            self.logger.info(f"  {role_icon} {name:20s} {status_icon} {peer.status.value:12s} (last seen: {last_seen})")
        
        self.logger.info("─" * 70)

        
        # 4. Update metrics
        self._update_metrics()
        
        self.cluster_state.last_poll_time = datetime.now()
    
    async def _update_self_state(self):
        """Update this validator's state"""
        # Check health
        health = await self.local_rpc.get_health()
        self.self_state.status = (
            ValidatorStatus.HEALTHY if health == "ok" 
            else ValidatorStatus.UNHEALTHY
        )
        
        # Get current identity
        identity = await self.local_rpc.get_identity()
        if identity:
            self.self_state.pubkey = identity
            
            # Determine role based on identity
            if identity == self.config.validator_identities.active_pubkey:
                self.self_state.role = ValidatorRole.ACTIVE
            elif identity == self.config.validator_identities.passive_pubkey:
                self.self_state.role = ValidatorRole.PASSIVE
            else:
                self.self_state.role = ValidatorRole.UNKNOWN
        
        self.logger.debug(
            f"Self state: role={self.self_state.role.value}, "
            f"status={self.self_state.status.value}, "
            f"identity={identity}"
        )
    
    async def _update_peer_slot(self, peer_state: PeerState, node: Dict, cluster_slot: Optional[int] = None):
        """Update peer's slot height and detect if stuck or too far behind
        
        Args:
            peer_state: The peer state to update
            node: The gossip node data
            cluster_slot: Current cluster slot height (for lag detection)
        """
        try:
            # Try to get peer's RPC endpoint from gossip
            rpc_address = node.get("rpc")
            if not rpc_address:
                self.logger.debug(f"Peer {peer_state.name} has no RPC address in gossip")
                return
            
            # Extract RPC URL
            rpc_url = f"http://{rpc_address}"
            
            # Get slot height from peer's RPC
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getSlot"},
                    timeout=aiohttp.ClientTimeout(total=2)
                ) as response:
                    if response.status == 200:
                        data = await response.json()
                        slot = data.get("result")
                        
                        if slot is not None:
                            # Update current slot
                            peer_state.current_slot = slot
                            
                            # Add to history (keep last N samples)
                            peer_state.slot_history.append(slot)
                            max_history = self.config.failover.stuck_samples_threshold
                            if len(peer_state.slot_history) > max_history:
                                peer_state.slot_history = peer_state.slot_history[-max_history:]
                            
                            # Check 1: Is slot progressing? (stuck check)
                            is_stuck = False
                            if len(peer_state.slot_history) >= self.config.failover.stuck_samples_threshold:
                                # If all samples are the same, peer is stuck
                                if len(set(peer_state.slot_history)) == 1:
                                    is_stuck = True
                                    self.logger.warning(
                                        f"Peer {peer_state.name} is STUCK at slot {slot} "
                                        f"(no progress for {len(peer_state.slot_history)} polls)"
                                    )
                            
                            # Check 2: Is slot too far behind cluster? (lag check)
                            is_lagging = False
                            if cluster_slot is not None and slot is not None:
                                slot_lag = cluster_slot - slot
                                if slot_lag > self.config.failover.max_slot_lag:
                                    is_lagging = True
                                    self.logger.warning(
                                        f"Peer {peer_state.name} is TOO FAR BEHIND! "
                                        f"Peer slot: {slot}, Cluster slot: {cluster_slot}, "
                                        f"Lag: {slot_lag} slots (max: {self.config.failover.max_slot_lag})"
                                    )
                                else:
                                    self.logger.debug(
                                        f"Peer {peer_state.name}: slot {slot}, cluster {cluster_slot}, "
                                        f"lag {slot_lag} slots (ok)"
                                    )
                            
                            # Overall health: progressing if NOT stuck AND NOT lagging
                            peer_state.is_progressing = not is_stuck and not is_lagging
                        
        except asyncio.TimeoutError:
            self.logger.debug(f"Timeout getting slot from peer {peer_state.name}")
        except Exception as e:
            self.logger.debug(f"Error getting slot from peer {peer_state.name}: {e}")
    
    async def _check_self_ready_for_takeover(self) -> Tuple[bool, str]:
        """Check if we are healthy enough to take over as active validator
        
        Returns:
            Tuple[bool, str]: (is_ready, reason)
                - (True, "ready") if we're healthy enough
                - (False, "reason") if not ready with explanation
        """
        # Check 1: Are we in gossip? (basic reachability)
        if self.self_state.status != ValidatorStatus.HEALTHY:
            return False, f"Not in gossip (status: {self.self_state.status.value})"
        
        # Check 2: Can we get our own slot? (RPC working)
        our_slot = await self.local_rpc.get_slot()
        if our_slot is None:
            return False, "Cannot get our own slot (RPC error)"
        
        # Check 3: Get cluster slot for comparison
        cluster_slot = await self.cluster_rpc.get_slot()
        if cluster_slot is None:
            self.logger.warning("Cannot get cluster slot for health check")
            # If we can't get cluster slot, we can't verify we're caught up
            # But if we got here, no peer is healthy either, so take a chance
            cluster_slot = None
        
        # Check 4: Are we caught up? (not too far behind)
        if cluster_slot is not None:
            slot_lag = cluster_slot - our_slot
            if slot_lag > self.config.failover.max_slot_lag:
                return False, (
                    f"Too far behind cluster (our slot: {our_slot}, "
                    f"cluster slot: {cluster_slot}, lag: {slot_lag} slots, "
                    f"max allowed: {self.config.failover.max_slot_lag})"
                )
            
            self.logger.info(
                f"Self health check: slot {our_slot}, cluster {cluster_slot}, "
                f"lag {slot_lag} slots - WITHIN THRESHOLD ✅"
            )
        
        # Check 5: Are we progressing? (optional - check slot history if available)
        # This is already covered by being in gossip and having recent slot
        
        # All checks passed!
        return True, "ready"
    
    def _am_i_highest_priority_passive(self) -> bool:
        """Check if this node has the highest priority to become active.
        
        Priority is determined by IP address (highest IP = highest priority).
        Only considers healthy passive nodes.
        
        Returns:
            True if this node should take over, False otherwise
        """
        my_ip = self.self_state.ip
        
        # Collect all healthy passive node IPs (including self)
        healthy_passive_ips = [my_ip]
        
        for peer_name, peer_state in self.cluster_state.peers.items():
            # Only consider passive nodes that are healthy
            if (peer_state.role == ValidatorRole.PASSIVE and 
                peer_state.status == ValidatorStatus.HEALTHY):
                healthy_passive_ips.append(peer_state.ip)
                self.logger.debug(
                    f"Peer {peer_name} ({peer_state.ip}) is healthy passive - "
                    f"including in priority check"
                )
        
        # Sort IPs to find highest
        healthy_passive_ips.sort()
        highest_ip = healthy_passive_ips[-1] if healthy_passive_ips else my_ip
        
        self.logger.info(
            f"IP-based priority check: my IP={my_ip}, "
            f"highest healthy passive IP={highest_ip}, "
            f"all healthy passives={healthy_passive_ips}"
        )
        
        # Am I the highest?
        am_i_highest = (my_ip == highest_ip)
        
        if am_i_highest:
            self.logger.info(f"✅ I have the highest IP - I should take over")
        else:
            self.logger.info(
                f"❌ Node with IP {highest_ip} has higher priority - "
                f"they should take over instead"
            )
        
        return am_i_highest
    
    async def _update_peer_states(self):
        """Update peer states from gossip"""
        # CRITICAL: Get cluster nodes with fallback mechanism
        # Try cluster RPC first (public endpoints)
        nodes = await self.cluster_rpc.get_cluster_nodes()
        
        # FALLBACK: If cluster RPC failed, try local validator's RPC
        if not nodes:
            self.logger.warning(
                "Cluster RPC returned no gossip data, falling back to local RPC..."
            )
            try:
                nodes = await self.local_rpc.get_cluster_nodes()
                if nodes:
                    self.logger.info(
                        f"Using local RPC gossip data as fallback ({len(nodes)} nodes)"
                    )
            except Exception as e:
                self.logger.error(f"Local RPC fallback also failed: {e}")
        
        # SAFETY CHECK: If we still have no gossip data, mark as unavailable
        if not nodes:
            self.logger.critical(
                "NO GOSSIP DATA AVAILABLE from either cluster or local RPC! "
                "Cannot determine cluster state safely."
            )
            self.cluster_state.gossip_unavailable = True
            # IMPORTANT: Keep last known peer states, don't clear them
            return
        
        # Gossip is available again (or was always available)
        if self.cluster_state.gossip_unavailable:
            self.logger.info("Gossip data available again, resuming normal monitoring")
        self.cluster_state.gossip_unavailable = False
        
        # Get vote accounts
        vote_accounts = await self.cluster_rpc.get_vote_accounts()
        current_voters = {acc["nodePubkey"] for acc in vote_accounts.get("current", [])}
        
        # Get current cluster slot (for lag detection)
        cluster_slot = None
        try:
            cluster_slot = await self.local_rpc.get_slot()
            self.logger.debug(f"Cluster slot: {cluster_slot}")
        except Exception as e:
            self.logger.debug(f"Could not get cluster slot: {e}")
        
        # Track which peers we found
        found_peers = set()
        self_found_in_gossip = False
        
        # CRITICAL: Track if we found the active identity anywhere in gossip
        active_identity_found = False
        
        for node in nodes:
            node_ip = node.get("gossip", "").split(":")[0] if node.get("gossip") else None
            node_pubkey = node.get("pubkey")
            
            if not node_ip or not node_pubkey:
                continue
            
            # Check if this is self FIRST (before checking active identity)
            if node_ip == self.self_state.ip:
                self_found_in_gossip = True
                # Don't set active_identity_found for self - we track our own state separately
                continue
            
            # Check if this node has the active identity (shared identity)
            # This is CRITICAL for detecting when a PEER becomes active
            if node_pubkey == self.config.validator_identities.active_pubkey:
                active_identity_found = True
                self.logger.info(f"Found active identity in gossip: {node_pubkey} at {node_ip}")
            
            # Check if this is a configured peer
            # IP-ONLY MATCHING: Simple and reliable
            peer_name = None
            
            for name, peer_cfg in self.config.failover.peers.items():
                if peer_cfg.ip and peer_cfg.ip == node_ip:
                    peer_name = name
                    self.logger.debug(f"Matched peer {name} by IP: {node_ip}")
                    break
            
            if not peer_name:
                continue
            
            found_peers.add(peer_name)
            
            # Determine role based on identity
            role = ValidatorRole.UNKNOWN
            if node_pubkey == self.config.validator_identities.active_pubkey:
                # Peer has active identity = peer is ACTIVE
                # Don't check voting status - unreliable in testnet/low-stake scenarios
                role = ValidatorRole.ACTIVE
                self.logger.info(f"Peer {peer_name} is ACTIVE (has active identity)")
            elif node_pubkey == self.config.validator_identities.passive_pubkey:
                role = ValidatorRole.PASSIVE
            # If peer has their own passive identity (not the shared one), still passive
            elif peer_name:
                role = ValidatorRole.PASSIVE
            
            # Create or update peer state
            peer_state = self.cluster_state.peers.get(peer_name, PeerState(name=peer_name, ip=node_ip))
            peer_state.pubkey = node_pubkey
            peer_state.role = role
            peer_state.status = ValidatorStatus.HEALTHY  # In gossip = reachable
            peer_state.last_seen = datetime.now()
            peer_state.gossip_port = int(node.get("gossip", ":0").split(":")[1]) if ":" in node.get("gossip", "") else None
            peer_state.version = node.get("version")
            
            # Track slot progression for active peers
            if role == ValidatorRole.ACTIVE:
                await self._update_peer_slot(peer_state, node, cluster_slot)
            
            self.cluster_state.peers[peer_name] = peer_state
            
            self.logger.debug(
                f"Peer {peer_name}: role={role.value}, pubkey={node_pubkey}, "
                f"slot={peer_state.current_slot}, progressing={peer_state.is_progressing}"
            )
        
        # CRITICAL CHECK: If we found the active identity but couldn't match it to a peer,
        # this indicates a configuration problem
        if active_identity_found:
            # Check if any peer has the active role
            has_active_peer = any(
                peer.role == ValidatorRole.ACTIVE 
                for peer in self.cluster_state.peers.values()
            )
            
            if not has_active_peer:
                # Find the IP of the active validator
                active_validator_ip = None
                for node in nodes:
                    if node.get("pubkey") == self.config.validator_identities.active_pubkey:
                        active_validator_ip = node.get("gossip", "").split(":")[0]
                        break
                
                # Don't complain if the active identity is at our own IP
                # (this just means we are active)
                if active_validator_ip and active_validator_ip == self.self_state.ip:
                    self.logger.debug(
                        f"Active identity at our IP ({self.self_state.ip}) - we are active"
                    )
                else:
                    # Active identity is at a different IP that's not in peers - this is a problem
                    self.logger.error(
                        "CRITICAL: Active identity found in gossip but not matched to any peer!"
                    )
                    self.logger.error(f"Active identity: {self.config.validator_identities.active_pubkey}")
                    if active_validator_ip:
                        self.logger.error(f"Active validator IP: {active_validator_ip}")
                        self.logger.error(
                            f"FIX: Add a peer with ip={active_validator_ip} to your config.yaml peers section"
                        )
                    self.logger.error("Configured peers:")
                    for name, peer_cfg in self.config.failover.peers.items():
                        self.logger.error(f"  {name}: identity={peer_cfg.identity}, ip={peer_cfg.ip}")
        
        # Mark missing peers
        for peer_name, peer_cfg in self.config.failover.peers.items():
            if peer_name not in found_peers:
                if peer_name in self.cluster_state.peers:
                    self.cluster_state.peers[peer_name].status = ValidatorStatus.MISSING
                else:
                    self.cluster_state.peers[peer_name] = PeerState(
                        name=peer_name,
                        ip=peer_cfg.ip or "unknown",
                        status=ValidatorStatus.MISSING
                    )
        
        # Update self visibility
        if not self_found_in_gossip:
            self.logger.warning("This validator is NOT visible in gossip!")
    
    async def _should_seppuku_for_safety(self) -> bool:
        """
        Check if we should force transition to passive for safety (split-brain prevention)
        
        Returns True if:
        1. We currently have the ACTIVE identity
        2. There's already another healthy ACTIVE validator in the cluster
        3. That other validator is actually voting
        
        This handles the case where:
        - Primary was active, powered off
        - Passive took over and became active
        - Primary powered back on with active identity
        - Primary needs to immediately go passive to avoid split-brain
        """
        try:
            # Check 1: Do we have the active identity?
            our_identity = await self.local_rpc.get_identity()
            if not our_identity:
                return False
            
            is_active_identity = (our_identity == self.config.validator_identities.active_pubkey)
            if not is_active_identity:
                # We have passive identity, no split-brain risk
                return False
            
            # Check 2: Is there already an active leader in peers?
            has_active_peer = any(
                peer.role == ValidatorRole.ACTIVE and peer.status == ValidatorStatus.HEALTHY
                for peer in self.cluster_state.peers.values()
            )
            
            if not has_active_peer:
                # No other active validator, we're good
                return False
            
            # Check 3: Is the other active validator actually voting?
            # This confirms they're the legitimate active leader
            vote_accounts = await self.cluster_rpc.get_vote_accounts()
            if not vote_accounts:
                return False
            
            # Look for active identity in current vote accounts
            # If it's there and it's not us voting, someone else is voting with active identity
            for vote_account in vote_accounts.get('current', []):
                node_pubkey = vote_account.get('nodePubkey')
                if node_pubkey == self.config.validator_identities.active_pubkey:
                    # Active identity is voting
                    # If we just started up, we're NOT the one voting (we haven't caught up yet)
                    # The validator that's been running is the legitimate active
                    
                    # Additional safety: check our own vote account to confirm we're not voting
                    self.logger.debug(f"Found active identity voting in cluster: {node_pubkey}")
                    
                    # If we see active identity voting and we have active identity,
                    # we should go passive UNLESS we can confirm we're the one voting
                    # For safety, if in doubt, go passive
                    return True
            
            return False
            
        except Exception as e:
            self.logger.error(f"Error in split-brain check: {e}")
            # On error, be conservative: don't force seppuku
            return False
    
    async def _evaluate_failover(self):
        """Evaluate whether failover is needed"""
        
        # CRITICAL: Cannot safely evaluate without gossip data
        if self.cluster_state.gossip_unavailable:
            self.logger.error(
                "⚠️  GOSSIP UNAVAILABLE - Cannot safely determine cluster state! "
                "Will not attempt failover without valid gossip data. "
                "Manual intervention may be required if this persists."
            )
            # Don't increment leaderless counter without data
            # Don't attempt any transitions
            return
        
        # CRITICAL SAFETY CHECK: Split-brain prevention
        # If we have active identity but someone else is already the active leader,
        # we must immediately transition to passive
        # This handles the case where a validator restarts after power cycle
        if await self._should_seppuku_for_safety():
            self.logger.warning("=" * 70)
            self.logger.warning("⚠️  SPLIT-BRAIN PREVENTION TRIGGERED")
            self.logger.warning("⚠️  We have active identity but another validator is already active leader")
            self.logger.warning("⚠️  Forcing immediate transition to passive (seppuku)")
            self.logger.warning("=" * 70)
            await self._execute_passive_transition()
            return
        
        # Check if there's an active leader (self OR peers)
        # IMPORTANT: Check if WE are active first!
        # CRITICAL: Active peer must also be progressing (not stuck)
        has_leader = (
            self.self_state.role == ValidatorRole.ACTIVE or
            any(
                peer.role == ValidatorRole.ACTIVE 
                and peer.status == ValidatorStatus.HEALTHY
                and peer.is_progressing  # Must be catching up!
                for peer in self.cluster_state.peers.values()
            )
        )
        
        self.cluster_state.has_leader = has_leader
        
        if has_leader:
            # Reset leaderless counter
            self.cluster_state.leaderless_sample_count = 0
            self.logger.debug("Leader is present, no failover needed")
            return
        
        # No leader detected
        self.cluster_state.leaderless_sample_count += 1
        self.logger.warning(
            f"No leader detected (sample {self.cluster_state.leaderless_sample_count}/"
            f"{self.config.failover.leaderless_samples_threshold})"
        )
        
        # Check if we've crossed the threshold
        if self.cluster_state.leaderless_sample_count < self.config.failover.leaderless_samples_threshold:
            return
        
        self.logger.error(
            f"Cluster has been leaderless for {self.cluster_state.leaderless_sample_count} samples!"
        )
        
        # Decision logic
        if self.self_state.role == ValidatorRole.ACTIVE:
            # We are active but something's wrong - go passive
            self.logger.critical("We are active but cluster is leaderless - forcing passive!")
            await self._execute_passive_transition()
        
        elif self.self_state.role == ValidatorRole.PASSIVE:
            # We are passive - check if we should take over
            
            # CRITICAL: Before taking over, verify WE are healthy enough!
            is_ready, reason = await self._check_self_ready_for_takeover()
            
            if not is_ready:
                self.logger.error(
                    f"Cannot take over as active - we are NOT ready: {reason}"
                )
                self.logger.error(
                    "Cluster is leaderless but we cannot help. "
                    "Manual intervention may be required!"
                )
                return
            
            # We are ready to take over!
            self.logger.info(
                f"✅ Self-health check PASSED - we are ready to take over! ({reason})"
            )
            
            # IP-based priority election (deterministic leader election)
            # Only the node with highest IP among healthy passives should take over
            # This prevents race conditions and split-brain deterministically
            if not self._am_i_highest_priority_passive():
                self.logger.info(
                    "Another passive node has higher priority (higher IP) - "
                    "staying passive and letting them take over"
                )
                return
            
            self.logger.info(
                "I have the highest priority among healthy passive nodes - initiating takeover!"
            )
            await self._execute_active_takeover()
        
        else:
            self.logger.error(f"Unknown role: {self.self_state.role}")
    
    async def _execute_active_takeover(self):
        """Execute transition to active role"""
        self.logger.info("=" * 60)
        self.logger.info("EXECUTING ACTIVE TAKEOVER")
        self.logger.info("=" * 60)
        
        # Note: With IP-based priority election, only the highest-priority node
        # reaches this point, so no jitter or re-check is needed
        
        # CRITICAL: Final health check before transition
        # Verify our health one more time before executing
        is_ready, reason = await self._check_self_ready_for_takeover()
        if not is_ready:
            self.logger.error(
                f"Final health check FAILED - aborting takeover: {reason}"
            )
            self.logger.error(
                "We were ready moments ago, but not anymore. "
                "Will retry on next poll if still leaderless."
            )
            return
        
        self.logger.info(f"✅ Final health check PASSED - proceeding with takeover")
        
        try:
            # Execute pre-hooks
            if not await self._execute_hooks(self.config.failover.active_pre_hooks, "pre-active"):
                self.logger.error("Pre-active hooks failed - aborting takeover")
                return
            
            # Execute active command
            if not await self._execute_command(
                self.config.failover.active_command,
                "active"
            ):
                self.logger.error("Active command failed!")
                self.metrics.failover_events.labels(
                    **self.config.prometheus_labels,
                    validator_name=self.config.validator_name,
                    event_type="takeover",
                    result="failure"
                ).inc()
                return
            
            # Execute post-hooks
            await self._execute_hooks(self.config.failover.active_post_hooks, "post-active")
            
            # CRITICAL: Update our role immediately to prevent race condition
            # If we don't do this, the next poll might detect our active identity
            # in gossip but still think we're passive, counting us as a separate
            # active peer (split-brain false positive)
            self.self_state.role = ValidatorRole.ACTIVE
            self.logger.info("Updated self role to ACTIVE")
            
            self.logger.info("Active takeover completed successfully!")
            self.metrics.failover_events.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                event_type="takeover",
                result="success"
            ).inc()
            
            # Reset leaderless counter
            self.cluster_state.leaderless_sample_count = 0
            
        except Exception as e:
            self.logger.error(f"Active takeover failed: {e}", exc_info=True)
            self.metrics.failover_events.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                event_type="takeover",
                result="error"
            ).inc()
    
    async def _execute_passive_transition(self):
        """Execute transition to passive role"""
        self.logger.info("=" * 60)
        self.logger.info("EXECUTING PASSIVE TRANSITION (SEPPUKU)")
        self.logger.info("=" * 60)
        
        try:
            # Execute pre-hooks
            if not await self._execute_hooks(self.config.failover.passive_pre_hooks, "pre-passive"):
                self.logger.error("Pre-passive hooks failed - aborting (but this is critical!)")
                # Continue anyway since this is critical
            
            # Execute passive command
            if not await self._execute_command(
                self.config.failover.passive_command,
                "passive"
            ):
                self.logger.critical("Passive command failed! MANUAL INTERVENTION REQUIRED!")
                self.metrics.failover_events.labels(
                    **self.config.prometheus_labels,
                    validator_name=self.config.validator_name,
                    event_type="seppuku",
                    result="failure"
                ).inc()
                return
            
            # Execute post-hooks only if passive command succeeded
            await self._execute_hooks(self.config.failover.passive_post_hooks, "post-passive")
            
            # CRITICAL: Update our role immediately to prevent race condition
            self.self_state.role = ValidatorRole.PASSIVE
            self.logger.info("Updated self role to PASSIVE")
            
            self.logger.info("Passive transition completed successfully")
            self.metrics.failover_events.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                event_type="seppuku",
                result="success"
            ).inc()
            
        except Exception as e:
            self.logger.critical(f"Passive transition failed: {e}", exc_info=True)
            self.metrics.failover_events.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                event_type="seppuku",
                result="error"
            ).inc()
    
    async def _execute_command(self, cmd_config: CommandConfig, cmd_type: str) -> bool:
        """Execute a command with template substitution"""
        if not cmd_config:
            self.logger.error(f"No {cmd_type} command configured")
            return False
        
        # Template context
        context = {
            "ActiveIdentityKeypairFile": str(self.config.validator_identities.active.absolute()),
            "PassiveIdentityKeypairFile": str(self.config.validator_identities.passive.absolute()),
            "ActiveIdentityPubkey": self.config.validator_identities.active_pubkey,
            "PassiveIdentityPubkey": self.config.validator_identities.passive_pubkey,
            "SelfName": self.config.validator_name,
            "LocalRpcUrl": self.config.validator_rpc_url,  # Local validator RPC URL
        }
        
        # Render command and args
        command = render_template(cmd_config.command, context)
        args = [render_template(arg, context) for arg in cmd_config.args]
        env = {k: render_template(v, context) for k, v in cmd_config.env.items()}
        
        full_cmd = [command] + args
        
        if self.config.failover.dry_run:
            self.logger.warning(f"[DRY RUN] Would execute: {' '.join(full_cmd)}")
            self.logger.warning(f"[DRY RUN] With env: {env}")
            return True
        
        self.logger.info(f"Executing {cmd_type} command: {' '.join(full_cmd)}")
        
        try:
            # Merge environment
            exec_env = {**subprocess.os.environ, **env}
            
            result = subprocess.run(
                full_cmd,
                env=exec_env,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            if result.stdout:
                self.logger.info(f"Command stdout: {result.stdout}")
            if result.stderr:
                self.logger.warning(f"Command stderr: {result.stderr}")
            
            success = result.returncode == 0
            
            self.metrics.command_executions.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                command_type=cmd_type,
                result="success" if success else "failure"
            ).inc()
            
            if not success:
                self.logger.error(f"Command failed with exit code {result.returncode}")
            
            return success
            
        except subprocess.TimeoutExpired:
            self.logger.error(f"Command timed out after 300 seconds")
            self.metrics.command_executions.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                command_type=cmd_type,
                result="timeout"
            ).inc()
            return False
        
        except Exception as e:
            self.logger.error(f"Command execution failed: {e}", exc_info=True)
            self.metrics.command_executions.labels(
                **self.config.prometheus_labels,
                validator_name=self.config.validator_name,
                command_type=cmd_type,
                result="error"
            ).inc()
            return False
    
    async def _execute_hooks(self, hooks: List[HookConfig], hook_type: str) -> bool:
        """Execute a list of hooks"""
        if not hooks:
            return True
        
        self.logger.info(f"Executing {len(hooks)} {hook_type} hook(s)...")
        
        for hook in hooks:
            hook_name = hook.name.lower().replace(" ", "_")
            self.logger.info(f"Running {hook_type} hook: {hook_name}")
            
            # Create command config from hook
            cmd_config = CommandConfig(
                command=hook.command,
                args=hook.args,
                env=hook.env
            )
            
            success = await self._execute_command(cmd_config, f"hook_{hook_name}")
            
            if not success and hook.must_succeed:
                self.logger.error(f"Hook {hook_name} failed and must_succeed=True")
                return False
            
            if not success:
                self.logger.warning(f"Hook {hook_name} failed but continuing (must_succeed=False)")
        
        return True
    
    def _update_metrics(self):
        """Update Prometheus metrics"""
        # Metadata
        self.metrics.metadata.labels(
            **self.config.prometheus_labels,
            validator_name=self.config.validator_name,
            public_ip=self.self_state.ip,
            role=self.self_state.role.value,
            status=self.self_state.status.value
        ).info({})
        
        # Peer count
        healthy_peers = sum(
            1 for peer in self.cluster_state.peers.values()
            if peer.status == ValidatorStatus.HEALTHY
        )
        self.metrics.peer_count.labels(
            **self.config.prometheus_labels,
            validator_name=self.config.validator_name
        ).set(healthy_peers)
        
        # Gossip availability
        self.metrics.gossip_available.labels(
            **self.config.prometheus_labels,
            validator_name=self.config.validator_name
        ).set(0 if self.cluster_state.gossip_unavailable else 1)
        
        # Failover status
        self.metrics.failover_status.labels(
            **self.config.prometheus_labels,
            validator_name=self.config.validator_name,
            event_type="leaderless_samples"
        ).set(self.cluster_state.leaderless_sample_count)


# ============================================================================
# Configuration Loading
# ============================================================================

def load_config(config_path: str) -> Config:
    """Load configuration from YAML file"""
    with open(config_path, 'r') as f:
        data = yaml.safe_load(f)
    
    # Parse validator RPC URL (needed for template variables)
    validator_rpc_url = data['validator'].get('rpc_url', 'http://localhost:8899')
    
    # Parse validator identities
    identities = ValidatorIdentities(
        active=Path(data['validator']['identities']['active']).expanduser(),
        passive=Path(data['validator']['identities']['passive']).expanduser()
    )
    
    # Parse peers
    peers = {}
    for name, peer_data in data['failover']['peers'].items():
        peers[name] = PeerConfig(
            name=name,
            identity=peer_data.get('identity'),  # Preferred
            ip=peer_data.get('ip')                # Fallback
        )
    
    # Parse failover commands
    active_cmd = CommandConfig(
        command=data['failover']['active']['command'],
        args=data['failover']['active'].get('args', []),
        env=data['failover']['active'].get('env', {})
    )
    
    passive_cmd = CommandConfig(
        command=data['failover']['passive']['command'],
        args=data['failover']['passive'].get('args', []),
        env=data['failover']['passive'].get('env', {})
    )
    
    # Parse hooks
    def parse_hooks(hooks_data):
        if not hooks_data:
            return []
        return [
            HookConfig(
                name=hook['name'],
                command=hook['command'],
                args=hook.get('args', []),
                env=hook.get('env', {}),
                must_succeed=hook.get('must_succeed', False)
            )
            for hook in hooks_data
        ]
    
    active_pre_hooks = parse_hooks(data['failover']['active'].get('hooks', {}).get('pre', []))
    active_post_hooks = parse_hooks(data['failover']['active'].get('hooks', {}).get('post', []))
    passive_pre_hooks = parse_hooks(data['failover']['passive'].get('hooks', {}).get('pre', []))
    passive_post_hooks = parse_hooks(data['failover']['passive'].get('hooks', {}).get('post', []))
    
    # Parse failover config
    # Note: Old parameters are silently ignored for backwards compatibility
    deprecated_params = ['takeover_jitter_duration', 'takeover_recheck_attempts', 
                        'takeover_recheck_delay', 'ip_priority_election']
    found_deprecated = [p for p in deprecated_params if p in data['failover']]
    if found_deprecated:
        print(f"INFO: Ignoring deprecated config parameters (no longer needed): {', '.join(found_deprecated)}")
        print("INFO: IP-based priority election is now always enabled (v1.1.0+)")
    
    failover = FailoverConfig(
        dry_run=data['failover'].get('dry_run', False),
        poll_interval_duration=parse_duration(data['failover'].get('poll_interval_duration', '5s')),
        leaderless_samples_threshold=data['failover'].get('leaderless_samples_threshold', 3),
        stuck_samples_threshold=data['failover'].get('stuck_samples_threshold', 3),
        max_slot_lag=data['failover'].get('max_slot_lag', 300),
        peers=peers,
        active_command=active_cmd,
        passive_command=passive_cmd,
        active_pre_hooks=active_pre_hooks,
        active_post_hooks=active_post_hooks,
        passive_pre_hooks=passive_pre_hooks,
        passive_post_hooks=passive_post_hooks
    )
    
    # Get cluster configuration (OPTIONAL)
    # If not provided, defaults to using local RPC for everything
    cluster_config = data.get('cluster', {})
    cluster_name = cluster_config.get('name', 'auto-detected')
    cluster_rpc_urls = cluster_config.get('rpc_urls', [])
    
    # Apply template variables to cluster RPC URLs
    # This allows using {{ .LocalRpcUrl }} to reference validator.rpc_url
    template_context = {
        "LocalRpcUrl": validator_rpc_url,
    }
    cluster_rpc_urls = [render_template(url, template_context) for url in cluster_rpc_urls]
    
    # If no cluster RPC URLs specified, use local RPC as default
    if not cluster_rpc_urls:
        cluster_rpc_urls = [validator_rpc_url]  # Use local RPC!
        print(f"INFO: No cluster RPC URLs specified, using local RPC: {validator_rpc_url}")
    
    return Config(
        validator_name=data['validator']['name'],
        validator_rpc_url=data['validator'].get('rpc_url', 'http://localhost:8899'),
        validator_identities=identities,
        cluster_name=cluster_name,
        cluster_rpc_urls=cluster_rpc_urls,
        failover=failover,
        prometheus_port=data.get('prometheus', {}).get('port', 9099),
        prometheus_labels=data.get('prometheus', {}).get('static_labels', {}),
        log_level=data.get('log', {}).get('level', 'INFO'),
        log_format=data.get('log', {}).get('format', 'text'),
        public_ip=data['validator'].get('public_ip'),  # Optional manual IP
        public_ip_service_urls=data['validator'].get('public_ip_service_urls', [])
    )


def parse_duration(duration_str: str) -> float:
    """Parse Go-style duration string to seconds"""
    duration_str = duration_str.lower().strip()
    
    if duration_str.endswith('s'):
        return float(duration_str[:-1])
    elif duration_str.endswith('m'):
        return float(duration_str[:-1]) * 60
    elif duration_str.endswith('h'):
        return float(duration_str[:-1]) * 3600
    else:
        return float(duration_str)


# ============================================================================
# Main Entry Point
# ============================================================================

def main():
    """Main entry point"""
    if len(sys.argv) < 2:
        print("Usage: solana_validator_ha.py <config.yaml>")
        sys.exit(1)
    
    config_path = sys.argv[1]
    
    try:
        config = load_config(config_path)
        ha_manager = SolanaValidatorHA(config)
        asyncio.run(ha_manager.start())
    except KeyboardInterrupt:
        print("\nShutdown requested")
    except Exception as e:
        print(f"Fatal error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
