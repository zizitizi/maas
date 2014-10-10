# Copyright 2014 Canonical Ltd.  This software is licensed under the
# GNU Affero General Public License version 3 (see the file LICENSE).

"""RPC helpers relating to clusters (a.k.a. node groups)."""

from __future__ import (
    absolute_import,
    print_function,
    unicode_literals,
    )

str = None

__metaclass__ = type
__all__ = [
    "get_cluster_status",
    "register_cluster",
]

import json

from django.core.exceptions import ValidationError
from maasserver.enum import NODEGROUP_STATUS
from maasserver.forms import NodeGroupDefineForm
from maasserver.models.nodegroup import NodeGroup
from maasserver.utils.async import transactional
from provisioningserver.logger import get_maas_logger
from provisioningserver.rpc.exceptions import NoSuchCluster
from provisioningserver.utils.twisted import synchronous


maaslog = get_maas_logger('rpc.clusters')


@synchronous
@transactional
def get_cluster_status(uuid):
    """Return the status of the given cluster.

    Return it as a structure suitable for returning in the response for
    :py:class:`~provisioningserver.rpc.region.GetClusterStatus`.
    """
    try:
        nodegroup = NodeGroup.objects.get_by_natural_key(uuid)
    except NodeGroup.DoesNotExist:
        raise NoSuchCluster.from_uuid(uuid)
    else:
        return {b"status": nodegroup.status}


@synchronous
@transactional
def register_cluster(uuid, name=None, domain=None, networks=None, url=None):
    """Register a new cluster, if not already registered.

    If the master has not been configured yet, this nodegroup becomes the
    master. In that situation, if the uuid is also the one configured locally
    (meaning that the cluster controller is running on the same host as this
    region controller), the new master is also automatically accepted.

    Note that this function should only ever be called once the cluster has
    been authenticated, by a shared-secret for example. The reason is that the
    cluster will be created in an accepted state.

    """
    try:
        cluster = NodeGroup.objects.get_by_natural_key(uuid)
    except NodeGroup.DoesNotExist:
        master = NodeGroup.objects.ensure_master()
        if master.uuid in ('master', ''):
            # The master cluster is not yet configured. No actual cluster
            # controllers have registered yet. All we have is the default
            # placeholder. We let the cluster controller that's making this
            # request take the master's place.
            cluster = master
            message = "New cluster registered as master"
        else:
            cluster = None
            message = "New cluster registered"
    else:
        message = "Cluster registered"

    # Massage the data so that we can pass it into NodeGroupDefineForm.
    data = {
        "cluster_name": name,
        "name": domain,
        "uuid": uuid,
    }

    # Populate networks when there are no preexisting networks.
    if networks is not None:
        if cluster is None or not cluster.nodegroupinterface_set.exists():
            # I can't figure out how to get something other than a string
            # through Django's form machinery, hence the hoop-jumping below.
            data["interfaces"] = json.dumps(networks)

    form = NodeGroupDefineForm(
        data=data, status=NODEGROUP_STATUS.ACCEPTED,
        instance=cluster)

    if form.is_valid():
        cluster = form.save()
        maaslog.info("%s: %s (%s)" % (
            message, cluster.cluster_name, cluster.uuid))
    else:
        raise ValidationError(form.errors)

    # Update `cluster.maas_url` from the given URL, but only when the hostname
    # is not 'localhost' (i.e. the default value used when the master cluster
    # connects).
    if url is not None and url.hostname != "localhost":
        cluster.maas_url = url.geturl()
        cluster.save()

    return cluster
