Running and Deploying
=====================

Both ``swift-s3-sync`` and ``swift-s3-migrator`` must be invoked with a
configuration file, specifying which containers to watch, where the
contents should be placed, as well as a number of global settings. A
sample configuration file is in the
`repository <https://github.com/swiftstack/1space/blob/master/sync.json-sample>`_.

Both of these tools run in the foreground, so starting each in their own
respective screen sessions is advisable.

To configure the Swift Proxy servers to use ``1space`` to redirect requests
for archived objects, you have to add the following to the proxy pipeline::

    [filter:swift_s3_shunt]
    use = egg:swift-s3-sync#cloud-shunt
    conf_file = <Path to swift-s3-sync config file>

This middleware should be in the pipeline before the DLO/SLO middleware.

when configuring, it's important to notice the different roles between the
sync and the migrator tools. The Sync/Lifecycle tool is used to push objects
from the local Swift cluster out to a remote object store. The Migrator is used
to copy objects from remote object store into the local Swift cluster.

swift-s3-sync configuration 
---------------------------
Below is a sample of both a sync profile setting and the sync global settings.
A profile is a mapping between one local swift container and a remote bucket.
The sync process can handle multiple profiles, the global settings apply to
all profiles:

.. code-block:: json

   {
       "containers": [
           {
               "account": "AUTH_swift",
               "container": "local",
               "aws_endpoint": "http://192.168.22.99/auth/v1.0",
               "aws_identity": "swift",
               "aws_secret": "swift",
               "aws_bucket": "remote",
               "protocol": "swift",
               "convert_dlo": false,
               "copy_after": 0,
               "exclude_pattern": "",
               "propagate_delete": false,
               "propagate_expiration": false,
               "propagate_expiration_offset": 3600,
               "remote_delete_after": 15552000,
               "remote_delete_after_addition": 86400,
               "retain_local": false,
               "sync_container_metadata": false,
               "sync_container_acl": false
           }
       ]  
       "devices": "/swift/nodes/1/node",
       "items_chunk": 1000,
       "log_file": "/var/log/swift-s3-sync.log",
       "poll_interval": 1,
       "status_dir": "/var/lib/swift-s3-sync",
       "workers": 10,
       "enumerator_workers": 10,
       "statsd_host": "localhost",
       "statsd_port": 21337
   }

Sync Profile
  - **account**: local account where data is synced from.
  - **container**: local container where data is synced from.
  - **aws_endpoint**: remote object store endpoint (supports either
    swift/s3 endpoint).
  - **aws_identity**: remote object store account/identity.
  - **aws_secret**: remote object store identity's secret/password.
  - **aws_bucket**: remote bucket where data is synced to.
  - **protocol**: remote object store API protocol: ``swift`` or ``s3``.
  - **convert_dlo**: convert dynamic large objects to static large objects. If
    the dynamic large object manifest itself contains data, it will not be
    migrated (*Optional*. Default: ``False``).
  - **copy_after**: Time in seconds to delay object sync (*Optional*.
    Default: 0).
  - **exclude_pattern**: Regular expression to be applied to object names to
    skip them instead of copying to the remote cluster. This is useful if you
    need to ignore segments for the large objects when they are in the same
    container as the manifest, for example. Python regular expression format is
    required (*Optional*, Default: '').
  - **propagate_delete**: If False, local DELETE requests won't be propagated
    to remote container (*Optional*. Default: ``True``).
  - **propagate_expiration**: If True, expiration headers will propagate when
    synced to remote cluster. Note: *remote_delete_after* takes precedence
    over this option (*Optional*. Default: ``False``. This option is only
    available to Swift protocol)
  - **propagate_expiration_offset**: If set, the value of object's expiration
    header (X-Delete-At) is incremented by the specified value (*Optional*.
    Default: 0).
  - **remote_delete_after**: Delete after setting for remote objects. For Swift
    remote clusters, this is applied to each object. For S3, it is applied as a
    lifecycle policy for the prefix. Note that in both cases, the delete after
    relates to the original object date, not the date it is copied to remote.
    A value of 0 (zero) means don't apply. (*Optional*. Default: 0)
  - **remote_delete_after_addition**: This option is used to set expiration
    header on SLO segments when *remote_delete_after* is used to set
    expiration on a manifest. This option will prevent segments from expiring
    before manifests. (*Optional*. Default: 24 hours. This option is only available
    for Swift remote clusters).
    A value of 0 (zero) means don't apply. (*Optional*. Default: 0)
  - **retain_local**: If False, local object will be deleted after sync is
    completed (*Optional*. Default: ``True``).
  - **retain_local_segments**: If False, local large object segments will be deleted
    after sync is completed. *retain_local* must also be set to ``False`` for
    segments to be deleted. (*Optional*. Default: ``False``)
  - **storage_policy**: Specify the storage policy to use for any containers
    creataed on the remote Swift cluster. If the policy is not a valid choice,
    an error will be written in the logs and the container create will fail.
    If unspecified, the default policy of the remote Swift cluster will be
    used. (*Optional*)
  - **sync_container_acl**: Preserve the container ACL (Read/Write). This
    option applies only for Swift remote clusters, **sync_container_metadata**
    must also be set to True (*Optional*. Default: ``False``).
  - **sync_container_metadata**: Propagate container metadata. This option
    applies only for Swift remote clusters (*Optional*. Default: ``False``).

Global settings
  - **devices**: Directory Swift's container devices are mounted under.
  - **items_chunk**: Number of rows to process at a time
  - **log_file**: Path to sync process log file
  - **poll_interval**: Time interval between sync runs
  - **status_dir**: Directory to where sync process saves status data
  - **workers**: Number of internal swift clients
  - **enumerator_workers**: Number of sync workers
  - **statsd_host**: StatsD host
  - **statsd_port**: StatsD port

swift-s3-migrator configuration 
-------------------------------
Below is a sample of both a migration profile setting and the migration global
settings. A profile is a mapping between one (or all for a given account)
remote container and a local account or container. The migrator process
can handle multiple profiles, the global settings apply to all profiles:

.. code-block:: json

   {
       "migrations": [
           {
               "account": "AUTH_test",
               "container": "migration-s3",
               "aws_endpoint": "http://1space-s3proxy:10080",
               "aws_identity": "s3-sync-test",
               "aws_secret": "s3-sync-test",
               "aws_bucket": "migration-s3",
               "protocol": "s3"
           },
       ],
       "migrator_settings": {
           "items_chunk": 5,
           "log_file": "/var/log/swift-s3-migrator.log",
           "poll_interval": 1,
           "status_file": "/var/lib/swift-s3-sync/migrator.status",
           "workers": 5,
           "processes": 1,
           "process": 0,
           "log_level": "debug"
       },
   }

Migrator Profile
  - **account**: local account where data is migrated to.
  - **container**: local container where data is migrated to.
  - **aws_endpoint**: remote object store endpoint (supports either
    swift/s3 endpoint).
  - **aws_identity**: remote object store account/identity.
  - **aws_secret**: remote object store identity's secret/password.
  - **aws_bucket**: remote bucket where data is migrated from.
  - **protocol**: remote object store API protocol: ``swift`` or ``s3``.
  - **storage_policy**: Specify the storage policy to use for any containers
    creataed on the local Swift cluster. If the policy is not a valid choice,
    an error will be written in the logs and the container create will fail.
    If unspecified, the default policy of the local Swift cluster will be
    used. (*Optional*)

Global settings
  - **items_chunk**: Number of items to process at a time
  - **log_file**: Path to sync process log file
  - **poll_interval**: Time interval between sync runs
  - **status_dir**: Directory to where sync process saves status data
  - **workers**: Number of internal swift clients
  - **processes**: Number of total migrator processes
  - **process**: index id of migrator process
  - **log_level**: Log level
