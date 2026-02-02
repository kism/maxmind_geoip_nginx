"""Network optimization utilities for IP range management."""

import ipaddress

from rich.console import Console
from tqdm import tqdm

console = Console()

DEFAULT_COVERAGE_THRESHOLD = 0.9

IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network


def _calculate_coverage(
    parent_net: IPNetwork,
    child_networks: list[IPNetwork],
) -> float:
    """Calculate what percentage of parent network is covered by child networks."""
    parent_size = parent_net.num_addresses
    covered_addresses = sum(net.num_addresses for net in child_networks if net.subnet_of(parent_net))
    return covered_addresses / parent_size


def _optimize_networks(
    networks: list[ipaddress.IPv4Network] | list[ipaddress.IPv6Network],
) -> list[ipaddress.IPv4Network] | list[ipaddress.IPv6Network]:
    """Optimize a list of networks by merging based on coverage."""
    optimised: list[ipaddress.IPv4Network] | list[ipaddress.IPv6Network] = []
    i = 0

    if len(networks) == 0:
        console.print("No networks to optimize.")
        return optimised

    network_type = "IPv4" if isinstance(networks[0], ipaddress.IPv4Network) else "IPv6"

    with tqdm(total=len(networks), desc=f"Optimizing {network_type} networks") as pbar:
        while i < len(networks):
            current = networks[i]

            # For networks with prefix length > 0, check if we should expand to parent
            if current.prefixlen > 0:
                # Get the parent network (one bit shorter prefix)
                parent = current.supernet(prefixlen_diff=1)

                # Find all networks that would be children of this parent
                potential_children = [net for net in networks if net.subnet_of(parent) or net == parent]

                # Calculate coverage
                coverage = _calculate_coverage(parent, potential_children)

                if coverage >= coverage_threshold:
                    # Skip all the children, add the parent instead
                    # console.print(
                    #     f"Merging {len(potential_children)} subnets into {parent} (coverage: {coverage:.1%})"
                    # )
                    optimised.append(parent)
                    # Find the next network after all children of this parent
                    start_i = i
                    i += 1
                    while i < len(networks) and (networks[i].subnet_of(parent) or networks[i] == parent):
                        i += 1
                    pbar.update(i - start_i)
                    continue

                # Check if this network is a subset of any already kept network
                is_subset = False
                for kept_network in optimised:
                    if current.subnet_of(kept_network):
                        is_subset = True
                        break

                if not is_subset:
                    optimised.append(current)

                i += 1
                pbar.update(1)

        return optimised


def merge_ip_ranges(ip_ranges: list[str], coverage_threshold: float = DEFAULT_COVERAGE_THRESHOLD) -> list[str]:
    """Merge overlapping or contiguous IP ranges, removing subsets.

    If coverage_threshold or more of a parent network is covered by smaller subnets,
    replace them all with the parent network.
    """
    console.print("Length before merge: ", len(ip_ranges))

    # Convert to network objects
    networks_v4: list[ipaddress.IPv4Network] = []
    networks_v6: list[ipaddress.IPv6Network] = []
    for ip_range in ip_ranges:
        net = ipaddress.ip_network(ip_range)
        if isinstance(net, ipaddress.IPv4Network):
            networks_v4.append(net)
        else:
            networks_v6.append(net)

    # Sort by network address and prefix length (shorter prefix = larger network first)
    networks_v4.sort(key=lambda net: (net.network_address, net.prefixlen))
    networks_v6.sort(key=lambda net: (net.network_address, net.prefixlen))

    n_round = 0
    while True:
        n_round += 1
        console.print(f"Optimization round {n_round}...")
        prev_len_v4 = len(networks_v4)
        networks_v4 = _optimize_networks(networks_v4)
        if len(networks_v4) == prev_len_v4:
            break

    n_round = 0
    while True:
        n_round += 1
        console.print(f"Optimization round {n_round}...")
        prev_len_v6 = len(networks_v6)
        networks_v6 = _optimize_networks(networks_v6)
        if len(networks_v6) == prev_len_v6:
            break

    console.print("Length after optimization: ", len(networks_v4) + len(networks_v6))
    return [str(net) for net in networks_v4] + [str(net) for net in networks_v6]
