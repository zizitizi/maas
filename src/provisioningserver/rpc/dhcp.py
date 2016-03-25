# Copyright 2014-2016 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RPC helpers relating to DHCP."""

__all__ = [
    "configure",
    "DHCPv4Server",
    "DHCPv6Server",
]

from collections import namedtuple
from operator import itemgetter
import os

from provisioningserver.dhcp import (
    DHCPv4Server,
    DHCPv6Server,
)
from provisioningserver.dhcp.config import get_config
from provisioningserver.dhcp.omshell import Omshell
from provisioningserver.logger import get_maas_logger
from provisioningserver.rpc.exceptions import (
    CannotConfigureDHCP,
    CannotCreateHostMap,
    CannotRemoveHostMap,
)
from provisioningserver.service_monitor import service_monitor
from provisioningserver.utils.fs import (
    sudo_delete_file,
    sudo_write_file,
)
from provisioningserver.utils.service_monitor import (
    SERVICE_STATE,
    ServiceActionError,
)
from provisioningserver.utils.shell import ExternalProcessError
from provisioningserver.utils.twisted import (
    asynchronous,
    synchronous,
)
from twisted.internet.defer import (
    inlineCallbacks,
    maybeDeferred,
)
from twisted.internet.threads import deferToThread


maaslog = get_maas_logger("dhcp")


# Holds the current state of DHCPv4 and DHCPv6.
_current_server_state = {}


DHCPStateBase = namedtuple("DHCPStateBase", [
    "omapi_key",
    "failover_peers",
    "shared_networks",
    "hosts",
    "interfaces",
    "global_dhcp_snippets",
])


class DHCPState(DHCPStateBase):
    """Holds the current known state of the DHCP server."""

    def __new__(
            cls, omapi_key, failover_peers,
            shared_networks, hosts, interfaces, global_dhcp_snippets):
        failover_peers = sorted(failover_peers, key=itemgetter("name"))
        shared_networks = sorted(shared_networks, key=itemgetter("name"))
        hosts = {
            host["mac"]: host
            for host in hosts
        }
        interfaces = sorted(
            interface["name"]
            for interface in interfaces
        )
        global_dhcp_snippets = sorted(
            global_dhcp_snippets, key=itemgetter("name"))
        return DHCPStateBase.__new__(
            cls,
            omapi_key=omapi_key,
            failover_peers=failover_peers,
            shared_networks=shared_networks,
            hosts=hosts, interfaces=interfaces,
            global_dhcp_snippets=global_dhcp_snippets)

    def requires_restart(self, other_state):
        """Return True when this state differs from `other_state` enough to
        require a restart."""
        def gather_hosts_dhcp_snippets(hosts):
            hosts_dhcp_snippets = list()
            for _, host in hosts.items():
                for dhcp_snippet in host['dhcp_snippets']:
                    hosts_dhcp_snippets.append(dhcp_snippet)
            return sorted(hosts_dhcp_snippets, key=itemgetter('name'))

        # Currently the OMAPI doesn't allow you to add or remove arbitrary
        # config options. So gather a list of DHCP snippets from
        hosts_dhcp_snippets = gather_hosts_dhcp_snippets(self.hosts)
        other_hosts_dhcp_snippets = gather_hosts_dhcp_snippets(
            other_state.hosts)
        return (
            self.omapi_key != other_state.omapi_key or
            self.failover_peers != other_state.failover_peers or
            self.shared_networks != other_state.shared_networks or
            self.interfaces != other_state.interfaces or
            self.global_dhcp_snippets != other_state.global_dhcp_snippets or
            hosts_dhcp_snippets != other_hosts_dhcp_snippets)

    def host_diff(self, other_state):
        """Return tuple with the hosts that need to be removed, need to be
        added, and need be updated."""
        remove, add, modify = [], [], []
        for mac, host in self.hosts.items():
            if mac not in other_state.hosts:
                add.append(host)
            elif host['ip'] != other_state.hosts[mac]['ip']:
                modify.append(host)
        for mac, host in other_state.hosts.items():
            if mac not in self.hosts:
                remove.append(host)
        return remove, add, modify

    def get_config(self, server):
        """Return the configuration for `server`."""
        dhcpd_config = get_config(
            server.template_basename, omapi_key=self.omapi_key,
            failover_peers=self.failover_peers,
            shared_networks=self.shared_networks,
            hosts=sorted(self.hosts.values(), key=itemgetter("host")),
            global_dhcp_snippets=sorted(
                self.global_dhcp_snippets, key=itemgetter("name")))
        return dhcpd_config, " ".join(self.interfaces)


@synchronous
def _write_config(server, state):
    """Write the configuration file."""
    dhcpd_config, interfaces_config = state.get_config(server)
    try:
        sudo_write_file(
            server.config_filename, dhcpd_config.encode("utf-8"))
        sudo_write_file(
            server.interfaces_filename,
            interfaces_config.encode("utf-8"))
    except ExternalProcessError as e:
        # ExternalProcessError.__str__ contains a generic failure message
        # as well as the command and its error output. On the other hand,
        # ExternalProcessError.output_as_unicode contains just the error
        # output which is probably the best information on what went wrong.
        # Log the full error information, but keep the exception message
        # short and to the point.
        maaslog.error(
            "Could not rewrite %s server configuration (for network "
            "interfaces %s): %s", server.descriptive_name,
            interfaces_config, str(e))
        raise CannotConfigureDHCP(
            "Could not rewrite %s server configuration: %s" % (
                server.descriptive_name, e.output_as_unicode))


@synchronous
def _delete_config(server):
    """Delete the server config."""
    if os.path.exists(server.config_filename):
        sudo_delete_file(server.config_filename)


def _remove_host_map(omshell, mac):
    """Remove host by `mac`."""
    try:
        omshell.remove(mac)
    except ExternalProcessError as e:
        if 'not connected.' in e.output_as_unicode:
            msg = "The DHCP server could not be reached."
        else:
            msg = str(e)
        err = "Could not remove host map for %s: %s" % (mac, msg)
        maaslog.error(err)
        raise CannotRemoveHostMap(err)


def _create_host_map(omshell, mac, ip_address):
    """Create host with `mac` -> `ip_address`."""
    try:
        omshell.create(ip_address, mac)
    except ExternalProcessError as e:
        if 'not connected.' in e.output_as_unicode:
            msg = "The DHCP server could not be reached."
        else:
            msg = str(e)
        err = "Could not create host map for %s -> %s: %s" % (
            mac, ip_address, msg)
        maaslog.error(err)
        raise CannotCreateHostMap(err)


@synchronous
def _update_hosts(server, remove, add, modify):
    """Update the hosts using the OMAPI."""
    omshell = Omshell(server_address='127.0.0.1', shared_key=server.omapi_key)
    for host in remove:
        _remove_host_map(omshell, host["mac"])
    for host in add:
        _create_host_map(omshell, host["mac"], host["ip"])
    for host in modify:
        _remove_host_map(omshell, host["mac"])
        _create_host_map(omshell, host["mac"], host["ip"])


@asynchronous
def _catch_service_error(server, action, call, *args, **kwargs):
    """Helper to catch `ServiceActionError` and `Exception` when performing
    `call`."""

    def eb(failure):
        message = "%s server failed to %s: %s" % (
            server.descriptive_name, action, failure.value)
        # A ServiceActionError will have already been logged by the
        # service monitor, so don't log a second time.
        if not failure.check(ServiceActionError):
            maaslog.error(message)
        # Squash everything into CannotConfigureDHCP.
        raise CannotConfigureDHCP(message) from failure.value

    return maybeDeferred(call, *args, **kwargs).addErrback(eb)


@asynchronous
@inlineCallbacks
def configure(
        server, failover_peers, shared_networks, hosts, interfaces,
        global_dhcp_snippets=[]):
    """Configure the DHCPv6/DHCPv4 server, and restart it as appropriate.

    This method is not safe to call concurrently. The clusterserver ensures
    that this method is not called concurrently.

    :param server: A `DHCPServer` instance.
    :param failover_peers: List of dicts with failover parameters for each
        subnet where HA is enabled.
    :param shared_networks: List of dicts with shared network parameters that
        contain a list of subnets when the DHCP should server shared.
        If no shared network are defined, the DHCP server will be stopped.
    :param hosts: List of dicts with host parameters that
        contain a list of hosts the DHCP should statically.
    :param interfaces: List of interfaces that DHCP should use.
    :param global_dhcp_snippets: List of all global DHCP snippets
    """
    stopping = len(shared_networks) == 0

    if stopping:
        # Remove the config so that the even an administrator cannot turn it on
        # accidently when it should be off.
        yield deferToThread(_delete_config, server)

        # Ensure that the service is off and is staying off.
        service = service_monitor.getServiceByName(server.dhcp_service)
        service.off()
        yield _catch_service_error(
            server, "stop",
            service_monitor.ensureService, server.dhcp_service)
        _current_server_state[server.dhcp_service] = None
    else:
        # Get the new state for the DHCP server.
        new_state = DHCPState(
            server.omapi_key, failover_peers, shared_networks,
            hosts, interfaces, global_dhcp_snippets)

        # Always write the config, that way its always up-to-date. Even if
        # we are not going to restart the services. This makes sure that even
        # the comments in the file are updated.
        yield deferToThread(_write_config, server, new_state)

        # Service should always be on if shared_networks exists.
        service = service_monitor.getServiceByName(server.dhcp_service)
        service.on()

        # Perform the required action based on the state change.
        current_state = _current_server_state.get(server.dhcp_service, None)
        if current_state is None:
            yield _catch_service_error(
                server, "restart",
                service_monitor.restartService, server.dhcp_service)
        elif new_state.requires_restart(current_state):
            yield _catch_service_error(
                server, "restart",
                service_monitor.restartService, server.dhcp_service)
        else:
            # No restart required update the host mappings if needed.
            remove, add, modify = new_state.host_diff(current_state)
            if len(remove) + len(add) + len(modify) == 0:
                # Nothing has changed, do nothing but make sure its running.
                yield _catch_service_error(
                    server, "start",
                    service_monitor.ensureService, server.dhcp_service)
            else:
                # Check the state of the service. Only if the services was on
                # should the host maps be updated over the OMAPI.
                before_state = yield service_monitor.getServiceState(
                    server.dhcp_service, now=True)
                yield _catch_service_error(
                    server, "start",
                    service_monitor.ensureService, server.dhcp_service)
                if before_state.active_state == SERVICE_STATE.ON:
                    # Was already running, so update host maps over OMAPI
                    # instead of performing a full restart.
                    try:
                        yield deferToThread(
                            _update_hosts, server, remove, add, modify)
                    except:
                        # Error updating the host maps over the OMAPI.
                        # Restart the DHCP service so that the host maps
                        # are in-sync with what MAAS expects.
                        maaslog.warning(
                            "Failed to update all host maps. Restarting %s "
                            "service to ensure host maps are in-sync." % (
                                server.descriptive_name))
                        yield _catch_service_error(
                            server, "restart",
                            service_monitor.restartService,
                            server.dhcp_service)

        # Update the current state to the new state.
        _current_server_state[server.dhcp_service] = new_state
