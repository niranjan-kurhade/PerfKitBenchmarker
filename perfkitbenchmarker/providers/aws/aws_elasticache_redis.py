# Copyright 2018 PerfKitBenchmarker Authors. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Module containing class for AWS' elasticache redis clusters.

Clusters can be created and deleted.
"""
import json
import logging

from absl import flags
from perfkitbenchmarker import errors
from perfkitbenchmarker import managed_memory_store
from perfkitbenchmarker import provider_info
from perfkitbenchmarker import virtual_machine
from perfkitbenchmarker import vm_util
from perfkitbenchmarker.providers.aws import aws_network
from perfkitbenchmarker.providers.aws import util

FLAGS = flags.FLAGS
REDIS_VERSION_MAPPING = {
    'redis_3_2': '3.2.10',
    'redis_4_0': '4.0.10',
    'redis_5_0': '5.0.6',
    'redis_6_x': '6.x',
    'redis_7_x': '7.0',
}


class ElastiCacheRedis(managed_memory_store.BaseManagedMemoryStore):
  """Object representing a AWS Elasticache redis instance."""

  CLOUD = provider_info.AWS
  MEMORY_STORE = managed_memory_store.REDIS

  # AWS Clusters can take up to 2 hours to create
  READY_TIMEOUT = 120 * 60

  def __init__(self, spec):
    super(ElastiCacheRedis, self).__init__(spec)
    self.subnet_group_name = 'subnet-%s' % self.name
    self.version = REDIS_VERSION_MAPPING[spec.config.cloud_redis.redis_version]
    self.node_type = FLAGS.elasticache_node_type
    self.redis_region = FLAGS.cloud_redis_region
    self.failover_zone = FLAGS.elasticache_failover_zone
    self.failover_subnet = None
    self.failover_style = FLAGS.redis_failover_style

    self.subnets = []

  @staticmethod
  def CheckPrerequisites(benchmark_config):
    if (
        FLAGS.managed_memory_store_version
        and FLAGS.managed_memory_store_version
        not in managed_memory_store.REDIS_VERSIONS
    ):
      raise errors.Config.InvalidValue('Invalid Redis version.')
    if FLAGS.redis_failover_style in [
        managed_memory_store.Failover.FAILOVER_NONE,
        managed_memory_store.Failover.FAILOVER_SAME_ZONE,
    ]:
      if FLAGS.elasticache_failover_zone:
        raise errors.Config.InvalidValue(
            'The aws_elasticache_failover_zone flag is ignored. '
            'There is no need for a failover zone when there is no failover. '
            'Same zone failover will fail over to the same zone.'
        )
    else:
      if (
          not FLAGS.elasticache_failover_zone
          or FLAGS.elasticache_failover_zone[:-1]
          != FLAGS.cloud_redis_region
      ):
        raise errors.Config.InvalidValue(
            'Invalid failover zone. A failover zone in %s must be specified. '
            % FLAGS.cloud_redis_region
        )

  def GetResourceMetadata(self):
    """Returns a dict containing metadata about the instance.

    Returns:
      dict mapping string property key to value.
    """
    self.metadata.update({
        'cloud_redis_failover_style': self.failover_style,
        'cloud_redis_version': managed_memory_store.ParseReadableVersion(
            self.version
        ),
        'cloud_redis_node_type': self.node_type,
        'cloud_redis_region': self.redis_region,
        'cloud_redis_primary_zone': self.spec.vms[0].zone,
        'cloud_redis_failover_zone': self.failover_zone,
    })
    return self.metadata

  def _CreateDependencies(self):
    """Create the subnet dependencies."""
    subnet_id = self.spec.vms[0].network.subnet.id
    cmd = [
        'aws',
        'elasticache',
        'create-cache-subnet-group',
        '--region',
        self.redis_region,
        '--cache-subnet-group-name',
        self.subnet_group_name,
        '--cache-subnet-group-description',
        '"PKB redis benchmark subnet"',
        '--subnet-ids',
        subnet_id,
    ]

    if self.failover_style == (
        managed_memory_store.Failover.FAILOVER_SAME_REGION
    ):
      regional_network = self.spec.vms[0].network.regional_network
      vpc_id = regional_network.vpc.id
      cidr = regional_network.vpc.NextSubnetCidrBlock()
      self.failover_subnet = aws_network.AwsSubnet(
          self.failover_zone, vpc_id, cidr_block=cidr
      )
      self.failover_subnet.Create()
      cmd += [self.failover_subnet.id]

    # Subnets determine where shards can be placed.
    regional_network = self.spec.vms[0].network.regional_network
    vpc_id = regional_network.vpc.id
    for zone in self.zones:
      cidr = regional_network.vpc.NextSubnetCidrBlock()
      subnet = aws_network.AwsSubnet(zone, vpc_id, cidr_block=cidr)
      subnet.Create()
      cmd += [subnet.id]
      self.subnets.append(subnet)

    vm_util.IssueCommand(cmd)

  def _DeleteDependencies(self):
    """Delete the subnet dependencies."""
    cmd = [
        'aws',
        'elasticache',
        'delete-cache-subnet-group',
        '--region=%s' % self.redis_region,
        '--cache-subnet-group-name=%s' % self.subnet_group_name,
    ]
    vm_util.IssueCommand(cmd, raise_on_failure=False)

    if self.failover_subnet:
      self.failover_subnet.Delete()

    for subnet in self.subnets:
      subnet.Delete()

  def _Create(self):
    """Creates the cluster."""
    cmd = [
        'aws',
        'elasticache',
        'create-replication-group',
        '--engine',
        'redis',
        '--engine-version',
        self.version,
        '--replication-group-id',
        self.name,
        '--replication-group-description',
        self.name,
        '--region',
        self.redis_region,
        '--cache-node-type',
        self.node_type,
        '--cache-subnet-group-name',
        self.subnet_group_name,
    ]

    if not self._clustered:
      cmd += ['--preferred-cache-cluster-a-zs', self.spec.vms[0].zone]
      if (
          self.failover_style
          == managed_memory_store.Failover.FAILOVER_SAME_REGION
      ):
        cmd += [self.failover_zone]
      elif (
          self.failover_style
          == managed_memory_store.Failover.FAILOVER_SAME_ZONE
      ):
        cmd += [self.spec.vms[0].zone]
      if self.failover_style != managed_memory_store.Failover.FAILOVER_NONE:
        cmd += ['--automatic-failover-enabled', '--num-cache-clusters', '2']
    else:
      cmd += ['--num-node-groups', str(self.node_count)]

    if self.enable_tls:
      cmd += [
          '--transit-encryption-enabled',
          '--transit-encryption-mode',
          'required',
      ]

    cmd += ['--tags']
    cmd += util.MakeFormattedDefaultTags()
    _, stderr, _ = vm_util.IssueCommand(cmd, raise_on_failure=False)

    if 'InsufficientCacheClusterCapacity' in stderr:
      raise errors.Benchmarks.InsufficientCapacityCloudFailure(stderr)

  def _Delete(self):
    """Deletes the cluster."""
    cmd = [
        'aws',
        'elasticache',
        'delete-replication-group',
        '--region',
        self.redis_region,
        '--replication-group-id',
        self.name,
    ]
    vm_util.IssueCommand(cmd, raise_on_failure=False)

  def _IsDeleting(self):
    """Returns True if cluster is being deleted and false otherwise."""
    cluster_info = self.DescribeInstance()
    return cluster_info.get('Status', '') == 'deleting'

  def _IsReady(self):
    """Returns True if cluster is ready and false otherwise."""
    cluster_info = self.DescribeInstance()
    return cluster_info.get('Status', '') == 'available'

  def _Exists(self):
    """Returns true if the cluster exists and is not being deleted."""
    cluster_info = self.DescribeInstance()
    return 'Status' in cluster_info and cluster_info['Status'] not in [
        'deleting',
        'create-failed',
    ]

  def DescribeInstance(self):
    """Calls describe on cluster.

    Returns:
      dict mapping string cluster_info property key to value.
    """
    cmd = [
        'aws',
        'elasticache',
        'describe-replication-groups',
        '--region',
        self.redis_region,
        '--replication-group-id',
        self.name,
    ]
    stdout, stderr, retcode = vm_util.IssueCommand(cmd, raise_on_failure=False)
    if retcode != 0:
      logging.info('Could not find cluster %s, %s', self.name, stderr)
      return {}
    for cluster_info in json.loads(stdout)['ReplicationGroups']:
      if cluster_info['ReplicationGroupId'] == self.name:
        return cluster_info
    return {}

  @vm_util.Retry(max_retries=5)
  def _PopulateEndpoint(self):
    """Populates address and port information from cluster_info.

    Raises:
      errors.Resource.RetryableGetError:
      Failed to retrieve information on cluster
    """
    cluster_info = self.DescribeInstance()
    if not cluster_info:
      raise errors.Resource.RetryableGetError(
          f'Failed to retrieve information on {self.name}'
      )
    if self._clustered:
      primary_endpoint = cluster_info['ConfigurationEndpoint']
    else:
      primary_endpoint = cluster_info['NodeGroups'][0]['PrimaryEndpoint']
    self._ip = primary_endpoint['Address']
    self._port = primary_endpoint['Port']

  def GetShardEndpoints(
      self, client: virtual_machine.BaseVirtualMachine
  ) -> list[managed_memory_store.RedisShard]:
    """See base class."""
    shards = super().GetShardEndpoints(client)
    shards_by_slots = {shard.slots: shard for shard in shards}

    cluster_info = self.DescribeInstance()
    # See data/elasticache_describe_cluster.txt for an example
    node_groups = cluster_info['NodeGroups']
    zones_by_slots = {
        node['Slots']: node['NodeGroupMembers'][0]['PreferredAvailabilityZone']
        for node in node_groups
    }
    for slot in zones_by_slots:
      shards_by_slots[slot].zone = zones_by_slots[slot]

    return list(shards_by_slots.values())
